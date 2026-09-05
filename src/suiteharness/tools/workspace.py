"""Workspace-confined built-in file tools."""

from __future__ import annotations

import asyncio
import fnmatch
import heapq
import os
import re
import tempfile
import time
from collections.abc import Mapping
from dataclasses import dataclass, fields
from pathlib import Path, PurePosixPath
from threading import RLock
from typing import Protocol

import regex as safe_regex
from pydantic import JsonValue

from suiteharness.execution import ToolCallContext
from suiteharness.workspace import (
    SecurePosixWorkspaceFileSystem,
    WorkspaceAccessPolicy,
    WorkspaceOperation,
    WorkspacePathError,
    WorkspacePaths,
    WorkspaceSpace,
    resolve_beneath,
    secure_workspace_supported,
)


@dataclass(frozen=True, slots=True)
class WorkspaceToolBinding:
    paths: WorkspacePaths
    access_policy: WorkspaceAccessPolicy


class WorkspaceBindingResolver(Protocol):
    def resolve(self, context: ToolCallContext) -> WorkspaceToolBinding: ...


class MappingWorkspaceBindingResolver:
    """Process-local reference resolver keyed by authenticated tenant/product."""

    def __init__(
        self,
        bindings: Mapping[
            tuple[str, str] | tuple[str, str, str], WorkspaceToolBinding
        ]
        | None = None,
    ) -> None:
        self._items = dict(bindings or {})
        self._lock = RLock()

    def register(
        self,
        tenant_id: str,
        product_id: str,
        binding: WorkspaceToolBinding,
        *,
        channel_id: str | None = None,
    ) -> None:
        with self._lock:
            key = (
                (tenant_id, product_id)
                if channel_id is None
                else (tenant_id, product_id, channel_id)
            )
            if key in self._items:
                raise ValueError("workspace binding is already registered")
            self._items[key] = binding

    def resolve(self, context: ToolCallContext) -> WorkspaceToolBinding:
        channel_key = (
            context.scope.tenant_id,
            context.scope.product_id,
            context.scope.channel_id,
        )
        fallback_key = (context.scope.tenant_id, context.scope.product_id)
        with self._lock:
            try:
                return self._items.get(channel_key) or self._items[fallback_key]
            except KeyError as exc:
                raise WorkspacePathError("no workspace is configured for this product") from exc


@dataclass(frozen=True, slots=True)
class FileToolLimits:
    max_read_bytes: int = 2_097_152
    max_write_bytes: int = 2_097_152
    max_list_entries: int = 1_000
    max_glob_matches: int = 2_000
    max_grep_files: int = 1_000
    max_grep_matches: int = 2_000
    max_grep_file_bytes: int = 2_097_152
    max_grep_milliseconds: int = 2_000
    max_glob_milliseconds: int = 2_000
    max_list_milliseconds: int = 1_000
    max_walk_depth: int = 64
    max_walk_entries: int = 50_000
    max_result_characters: int = 1_000_000

    def __post_init__(self) -> None:
        for item in fields(self):
            name = item.name
            value = getattr(self, name)
            if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
                raise ValueError(f"{name} must be a positive integer")


def _space(value: object) -> WorkspaceSpace:
    try:
        return WorkspaceSpace(str(value))
    except ValueError as exc:
        raise ValueError("space must be 'product' or 'shared'") from exc


def _root(binding: WorkspaceToolBinding, space: WorkspaceSpace) -> Path:
    return binding.paths.product_root if space is WorkspaceSpace.PRODUCT else binding.paths.shared_root


def _relative(value: object, *, allow_root: bool = True) -> str:
    if not isinstance(value, str) or "\x00" in value or len(value) > 4_096:
        raise ValueError("path must be a bounded NUL-free string")
    portable = value.replace("\\", "/")
    if portable == "." and allow_root:
        return portable
    path = PurePosixPath(portable)
    if not portable or path.is_absolute() or portable.startswith("//"):
        raise ValueError("path must be relative to the selected workspace")
    if re.match(r"^[A-Za-z]:", portable):
        raise ValueError("drive-prefixed paths are forbidden")
    if any(part in {"", ".", ".."} for part in portable.split("/")):
        raise ValueError("path must be normalized and cannot contain '..'")
    return portable


def _resolve(
    binding: WorkspaceToolBinding,
    space: WorkspaceSpace,
    relative_path: str,
    operation: WorkspaceOperation,
    *,
    require_exists: bool,
) -> Path:
    root = _root(binding, space)
    if operation is WorkspaceOperation.WRITE:
        current = root
        parts = () if relative_path == "." else PurePosixPath(relative_path).parts
        for part in parts:
            current /= part
            if current.is_symlink():
                raise WorkspacePathError("symbolic links are forbidden in write paths")
    path = resolve_beneath(root, relative_path, require_exists=require_exists)
    return binding.access_policy.authorize(path, operation)


def _portable_path(path: Path, root: Path) -> str:
    relative = path.relative_to(root.resolve(strict=True))
    return "." if not relative.parts else relative.as_posix()


def _decode_text(content: bytes, encoding: str) -> str:
    normalized = encoding.lower().replace("_", "-")
    if normalized not in {"utf-8", "utf8", "gbk", "gb18030"}:
        raise ValueError("encoding must be UTF-8, GBK, or GB18030")
    try:
        return content.decode(normalized)
    except UnicodeDecodeError as exc:
        raise ValueError(f"file is not valid {normalized} text") from exc


def _atomic_write(target: Path, content: bytes, *, overwrite: bool) -> None:
    if target.exists() and not target.is_file():
        raise ValueError("write target must be a regular file")
    if target.exists() and not overwrite:
        raise FileExistsError("target exists; set overwrite=true to replace it")
    parent = target.parent.resolve(strict=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=".suiteharness-write-", dir=parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
    finally:
        if temporary.exists():
            temporary.unlink()


class WorkspaceFileTools:
    def __init__(
        self,
        resolver: WorkspaceBindingResolver,
        limits: FileToolLimits | None = None,
        secure_filesystem: SecurePosixWorkspaceFileSystem | None = None,
    ) -> None:
        self._resolver = resolver
        self._limits = limits or FileToolLimits()
        self._secure_filesystem = secure_filesystem
        if self._secure_filesystem is None and secure_workspace_supported():
            self._secure_filesystem = SecurePosixWorkspaceFileSystem()

    @property
    def production_safe(self) -> bool:
        """Whether file operations use the Linux descriptor-anchored backend."""

        return self._secure_filesystem is not None

    async def list(self, context: ToolCallContext, arguments: dict[str, JsonValue]) -> JsonValue:
        return await asyncio.to_thread(self._list, context, arguments)

    def _list(self, context: ToolCallContext, arguments: dict[str, JsonValue]) -> JsonValue:
        binding = self._resolver.resolve(context)
        space = _space(arguments.get("space", "product"))
        relative = _relative(arguments.get("path", "."))
        requested_limit = int(arguments.get("limit", self._limits.max_list_entries))
        limit = min(max(1, requested_limit), self._limits.max_list_entries)
        if self._secure_filesystem is not None:
            root = _root(binding, space)
            binding.access_policy.authorize_relative(
                root, relative, WorkspaceOperation.READ
            )
            listing = self._secure_filesystem.list_directory(
                root,
                relative,
                limit=limit,
                max_scanned_entries=self._limits.max_walk_entries,
                deadline=time.monotonic() + self._limits.max_list_milliseconds / 1000,
            )
            entries: list[dict[str, JsonValue]] = []
            for entry in listing.entries:
                binding.access_policy.authorize_relative(
                    root, entry.path, WorkspaceOperation.READ
                )
                entries.append(
                    {
                        "name": entry.name,
                        "path": entry.path,
                        "type": entry.kind,
                        "size": entry.size,
                    }
                )
            return {
                "space": space.value,
                "path": relative,
                "entries": entries,
                "truncated": listing.truncated,
            }
        root = _root(binding, space).resolve(strict=True)
        directory = _resolve(
            binding,
            space,
            relative,
            WorkspaceOperation.READ,
            require_exists=True,
        )
        if not directory.is_dir():
            raise ValueError("list path must be a directory")
        entries: list[dict[str, JsonValue]] = []
        children = heapq.nsmallest(
            limit + 1,
            directory.iterdir(),
            key=lambda item: item.name.casefold(),
        )
        for child in children[:limit]:
            safe = resolve_beneath(root, child.relative_to(root), require_exists=True)
            binding.access_policy.authorize(safe, WorkspaceOperation.READ)
            stat = safe.stat()
            entries.append(
                {
                    "name": child.name,
                    "path": _portable_path(safe, root),
                    "type": "directory" if safe.is_dir() else "file",
                    "size": stat.st_size if safe.is_file() else None,
                }
            )
        return {
            "space": space.value,
            "path": _portable_path(directory, root),
            "entries": entries,
            "truncated": len(children) > limit,
        }

    async def read(self, context: ToolCallContext, arguments: dict[str, JsonValue]) -> JsonValue:
        return await asyncio.to_thread(self._read, context, arguments)

    def _read(self, context: ToolCallContext, arguments: dict[str, JsonValue]) -> JsonValue:
        binding = self._resolver.resolve(context)
        space = _space(arguments.get("space", "product"))
        relative = _relative(arguments.get("path"), allow_root=False)
        requested_limit = int(arguments.get("max_bytes", self._limits.max_read_bytes))
        limit = min(max(1, requested_limit), self._limits.max_read_bytes)
        if self._secure_filesystem is not None:
            root = _root(binding, space)
            binding.access_policy.authorize_relative(
                root, relative, WorkspaceOperation.READ
            )
            content = self._secure_filesystem.read_file(root, relative, limit)
            truncated = len(content) > limit
            content = content[:limit]
            text = _decode_text(content, str(arguments.get("encoding", "utf-8")))
            if len(text) > self._limits.max_result_characters:
                text = text[: self._limits.max_result_characters]
                truncated = True
            return {
                "space": space.value,
                "path": relative,
                "content": text,
                "bytes": len(content),
                "truncated": truncated,
            }
        path = _resolve(
            binding,
            space,
            relative,
            WorkspaceOperation.READ,
            require_exists=True,
        )
        if not path.is_file():
            raise ValueError("read path must be a regular file")
        with path.open("rb") as stream:
            content = stream.read(limit + 1)
        truncated = len(content) > limit
        content = content[:limit]
        text = _decode_text(content, str(arguments.get("encoding", "utf-8")))
        if len(text) > self._limits.max_result_characters:
            text = text[: self._limits.max_result_characters]
            truncated = True
        return {
            "space": space.value,
            "path": _portable_path(path, _root(binding, space)),
            "content": text,
            "bytes": len(content),
            "truncated": truncated,
        }

    async def write(self, context: ToolCallContext, arguments: dict[str, JsonValue]) -> JsonValue:
        return await asyncio.to_thread(self._write, context, arguments)

    def _write(self, context: ToolCallContext, arguments: dict[str, JsonValue]) -> JsonValue:
        binding = self._resolver.resolve(context)
        space = _space(arguments.get("space", "product"))
        relative = _relative(arguments.get("path"), allow_root=False)
        content_value = arguments.get("content")
        if not isinstance(content_value, str):
            raise ValueError("content must be a string")
        content = content_value.encode("utf-8")
        if len(content) > self._limits.max_write_bytes:
            raise ValueError("write content exceeds the configured byte limit")
        if self._secure_filesystem is not None:
            root = _root(binding, space)
            parent_parts = PurePosixPath(relative).parts[:-1]
            parent = "/".join(parent_parts) if parent_parts else "."
            binding.access_policy.authorize_relative(
                root, relative, WorkspaceOperation.WRITE
            )
            binding.access_policy.authorize_relative(
                root, parent, WorkspaceOperation.WRITE
            )
            self._secure_filesystem.write_file(
                root,
                relative,
                content,
                overwrite=arguments.get("overwrite") is True,
            )
            return {"space": space.value, "path": relative, "bytes": len(content)}
        target = _resolve(
            binding,
            space,
            relative,
            WorkspaceOperation.WRITE,
            require_exists=False,
        )
        binding.access_policy.authorize(target.parent.resolve(strict=True), WorkspaceOperation.WRITE)
        _atomic_write(target, content, overwrite=arguments.get("overwrite") is True)
        return {"space": space.value, "path": relative, "bytes": len(content)}

    async def edit(self, context: ToolCallContext, arguments: dict[str, JsonValue]) -> JsonValue:
        return await asyncio.to_thread(self._edit, context, arguments)

    def _edit(self, context: ToolCallContext, arguments: dict[str, JsonValue]) -> JsonValue:
        binding = self._resolver.resolve(context)
        space = _space(arguments.get("space", "product"))
        relative = _relative(arguments.get("path"), allow_root=False)
        old = arguments.get("old_text")
        new = arguments.get("new_text")
        expected = arguments.get("expected_replacements", 1)
        if not isinstance(old, str) or not old:
            raise ValueError("old_text must be a non-empty string")
        if not isinstance(new, str):
            raise ValueError("new_text must be a string")
        if not isinstance(expected, int) or isinstance(expected, bool) or expected < 1:
            raise ValueError("expected_replacements must be a positive integer")
        if self._secure_filesystem is not None:
            root = _root(binding, space)
            binding.access_policy.authorize_relative(
                root, relative, WorkspaceOperation.READ
            )
            binding.access_policy.authorize_relative(
                root, relative, WorkspaceOperation.WRITE
            )

            def transform(content: bytes) -> tuple[bytes, int]:
                text = _decode_text(content, "utf-8")
                actual = text.count(old)
                if actual != expected:
                    raise ValueError(
                        f"exact edit refused: expected {expected} matches but found {actual}"
                    )
                return text.replace(old, new).encode("utf-8"), actual

            actual = self._secure_filesystem.transform_file(
                root,
                relative,
                max_read_bytes=self._limits.max_read_bytes,
                max_write_bytes=self._limits.max_write_bytes,
                transform=transform,
            )
            return {"space": space.value, "path": relative, "replacements": actual}
        readable = _resolve(
            binding, space, relative, WorkspaceOperation.READ, require_exists=True
        )
        writable = _resolve(
            binding, space, relative, WorkspaceOperation.WRITE, require_exists=True
        )
        if readable != writable or not writable.is_file() or writable.is_symlink():
            raise ValueError("edit target must be one authorized regular file")
        with writable.open("rb") as stream:
            content = stream.read(self._limits.max_read_bytes + 1)
        if len(content) > self._limits.max_read_bytes:
            raise ValueError("edit target exceeds the configured byte limit")
        text = _decode_text(content, "utf-8")
        actual = text.count(old)
        if actual != expected:
            raise ValueError(
                f"exact edit refused: expected {expected} matches but found {actual}"
            )
        updated = text.replace(old, new)
        encoded = updated.encode("utf-8")
        if len(encoded) > self._limits.max_write_bytes:
            raise ValueError("edited content exceeds the configured byte limit")
        _atomic_write(writable, encoded, overwrite=True)
        return {"space": space.value, "path": relative, "replacements": actual}

    async def glob(self, context: ToolCallContext, arguments: dict[str, JsonValue]) -> JsonValue:
        return await asyncio.to_thread(self._glob, context, arguments)

    def _glob(self, context: ToolCallContext, arguments: dict[str, JsonValue]) -> JsonValue:
        binding = self._resolver.resolve(context)
        space = _space(arguments.get("space", "product"))
        pattern = _relative(arguments.get("pattern"), allow_root=False)
        requested_limit = int(arguments.get("limit", self._limits.max_glob_matches))
        limit = min(max(1, requested_limit), self._limits.max_glob_matches)
        if self._secure_filesystem is not None:
            root = _root(binding, space)
            binding.access_policy.authorize_relative(root, ".", WorkspaceOperation.READ)
            found = self._secure_filesystem.glob(
                root,
                pattern,
                max_results=limit + 1,
                max_scanned_entries=self._limits.max_walk_entries,
                max_depth=self._limits.max_walk_depth,
                deadline=time.monotonic() + self._limits.max_glob_milliseconds / 1000,
            )
            matches: list[str] = []
            result_characters = 0
            truncated = found.truncated
            for portable in found.paths:
                binding.access_policy.authorize_relative(
                    root, portable, WorkspaceOperation.READ
                )
                if len(matches) >= limit:
                    truncated = True
                    break
                if result_characters + len(portable) > self._limits.max_result_characters:
                    truncated = True
                    break
                matches.append(portable)
                result_characters += len(portable)
            return {"space": space.value, "matches": matches, "truncated": truncated}
        root = _root(binding, space).resolve(strict=True)
        matches: list[str] = []
        truncated = False
        result_characters = 0
        for candidate in root.glob(pattern):
            safe = resolve_beneath(root, candidate.relative_to(root), require_exists=True)
            binding.access_policy.authorize(safe, WorkspaceOperation.READ)
            portable = _portable_path(safe, root)
            if result_characters + len(portable) > self._limits.max_result_characters:
                truncated = True
                break
            matches.append(portable)
            result_characters += len(portable)
            if len(matches) > limit:
                truncated = True
                matches.pop()
                break
        return {"space": space.value, "matches": sorted(matches), "truncated": truncated}

    async def grep(self, context: ToolCallContext, arguments: dict[str, JsonValue]) -> JsonValue:
        return await asyncio.to_thread(self._grep, context, arguments)

    def _grep(self, context: ToolCallContext, arguments: dict[str, JsonValue]) -> JsonValue:
        binding = self._resolver.resolve(context)
        space = _space(arguments.get("space", "product"))
        relative_base = _relative(arguments.get("path", "."))
        query = arguments.get("query")
        if not isinstance(query, str) or not query or len(query) > 512:
            raise ValueError("grep query must contain 1 to 512 characters")
        file_pattern = str(arguments.get("file_pattern", "*"))
        _relative(file_pattern, allow_root=False)
        regex = arguments.get("regex") is True
        case_sensitive = arguments.get("case_sensitive") is not False
        flags = 0 if case_sensitive else re.IGNORECASE
        if regex:
            # Python's stdlib engine has no timeout. Reject constructs commonly
            # used for catastrophic backtracking; file/byte limits provide a
            # second bound. Deployments needing richer expressions can expose
            # sandboxed ripgrep as a separate plugin tool.
            if "(?" in query or re.search(r"\\[1-9]", query):
                raise ValueError("grep regex lookarounds and backreferences are forbidden")
            if re.search(r"\([^)]*[*+{][^)]*\)[*+{]", query):
                raise ValueError("grep regex contains a nested repetition")
            compiled = safe_regex.compile(query, flags)
        else:
            compiled = safe_regex.compile(re.escape(query), flags)
        requested_limit = int(arguments.get("limit", self._limits.max_grep_matches))
        limit = min(max(1, requested_limit), self._limits.max_grep_matches)
        matches: list[dict[str, JsonValue]] = []
        files_examined = 0
        truncated = False
        result_characters = 0
        deadline = time.monotonic() + self._limits.max_grep_milliseconds / 1000

        def search_content(portable: str, content: bytes) -> bool:
            nonlocal result_characters, truncated
            if len(content) > self._limits.max_grep_file_bytes:
                return False
            if b"\x00" in content[:8_192]:
                return False
            try:
                text = content.decode("utf-8")
            except UnicodeDecodeError:
                try:
                    text = content.decode("gb18030")
                except UnicodeDecodeError:
                    return False
            for line_number, line in enumerate(text.splitlines(), 1):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    truncated = True
                    return True
                try:
                    matched = compiled.search(line, timeout=min(0.02, remaining))
                except TimeoutError as exc:
                    raise ValueError("grep regex exceeded the safe execution time") from exc
                if not matched:
                    continue
                rendered = line[:2_000]
                if result_characters + len(rendered) > self._limits.max_result_characters:
                    truncated = True
                    return True
                matches.append(
                    {"path": portable, "line": line_number, "text": rendered}
                )
                result_characters += len(rendered)
                if len(matches) >= limit:
                    truncated = True
                    return True
            return False

        if self._secure_filesystem is not None:
            root = _root(binding, space)
            binding.access_policy.authorize_relative(
                root, relative_base, WorkspaceOperation.READ
            )
            candidates = self._secure_filesystem.walk_regular_files(
                root,
                relative_base,
                file_pattern=file_pattern,
                max_files=self._limits.max_grep_files + 1,
                max_scanned_entries=self._limits.max_walk_entries,
                max_depth=self._limits.max_walk_depth,
                deadline=deadline,
            )
            truncated = candidates.truncated
            for portable in candidates.paths:
                if files_examined >= self._limits.max_grep_files:
                    truncated = True
                    break
                if time.monotonic() >= deadline:
                    truncated = True
                    break
                binding.access_policy.authorize_relative(
                    root, portable, WorkspaceOperation.READ
                )
                files_examined += 1
                content = self._secure_filesystem.read_file(
                    root, portable, self._limits.max_grep_file_bytes
                )
                if search_content(portable, content):
                    break
            return {
                "space": space.value,
                "matches": matches,
                "files_examined": files_examined,
                "truncated": truncated,
            }

        root = _root(binding, space).resolve(strict=True)
        base = _resolve(
            binding,
            space,
            relative_base,
            WorkspaceOperation.READ,
            require_exists=True,
        )
        path_candidates = [base] if base.is_file() else base.rglob("*")
        for candidate in path_candidates:
            if time.monotonic() >= deadline:
                truncated = True
                break
            if not candidate.is_file():
                continue
            relative_from_base = candidate.name if base.is_file() else candidate.relative_to(base).as_posix()
            if not fnmatch.fnmatch(relative_from_base, file_pattern):
                continue
            safe = resolve_beneath(root, candidate.relative_to(root), require_exists=True)
            binding.access_policy.authorize(safe, WorkspaceOperation.READ)
            files_examined += 1
            if files_examined > self._limits.max_grep_files:
                truncated = True
                break
            if safe.stat().st_size > self._limits.max_grep_file_bytes:
                continue
            with safe.open("rb") as stream:
                content = stream.read(self._limits.max_grep_file_bytes + 1)
            if search_content(_portable_path(safe, root), content):
                break
        return {
            "space": space.value,
            "matches": matches,
            "files_examined": min(files_examined, self._limits.max_grep_files),
            "truncated": truncated,
        }


__all__ = [
    "FileToolLimits",
    "MappingWorkspaceBindingResolver",
    "WorkspaceBindingResolver",
    "WorkspaceFileTools",
    "WorkspaceToolBinding",
]
