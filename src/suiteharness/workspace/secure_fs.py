"""Race-resistant Linux filesystem operations for product workspaces.

Every pathname component is opened relative to an already-open directory file
descriptor.  Symbolic links are never followed.  This is intentionally a
Linux/POSIX security boundary; callers must not describe the pathlib fallback
used by development hosts as production safe.
"""

from __future__ import annotations

import errno
import fnmatch
import os
import secrets
import stat
import sys
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import TypeVar

from .layout import WorkspacePathError

_T = TypeVar("_T")


@dataclass(frozen=True, slots=True)
class SecureFileStat:
    """A no-follow snapshot of one workspace entry."""

    kind: str
    size: int | None


@dataclass(frozen=True, slots=True)
class SecureDirectoryEntry:
    """One directory entry observed without following symbolic links."""

    name: str
    path: str
    kind: str
    size: int | None


@dataclass(frozen=True, slots=True)
class SecureDirectoryListing:
    entries: tuple[SecureDirectoryEntry, ...]
    truncated: bool


@dataclass(frozen=True, slots=True)
class SecurePathCollection:
    paths: tuple[str, ...]
    truncated: bool


def secure_workspace_supported() -> bool:
    """Return whether this interpreter exposes the required Linux primitives."""

    if not sys.platform.startswith("linux") or os.name != "posix":
        return False
    required_constants = ("O_CLOEXEC", "O_DIRECTORY", "O_NOFOLLOW", "O_NONBLOCK")
    if any(not hasattr(os, name) for name in required_constants):
        return False
    dir_fd_functions = (os.open, os.stat, os.unlink, os.link, os.rename)
    if any(function not in os.supports_dir_fd for function in dir_fd_functions):
        return False
    if os.stat not in os.supports_follow_symlinks or os.link not in os.supports_follow_symlinks:
        return False
    return os.scandir in os.supports_fd


def _segments(relative_path: str, *, allow_root: bool) -> tuple[str, ...]:
    if not isinstance(relative_path, str) or not relative_path or "\x00" in relative_path:
        raise WorkspacePathError("workspace path must be a non-empty NUL-free string")
    portable = relative_path.replace("\\", "/")
    if portable == "." and allow_root:
        return ()
    if portable.startswith("/") or portable.startswith("//"):
        raise WorkspacePathError("workspace path must be relative")
    raw = portable.split("/")
    if any(part in {"", ".", ".."} for part in raw):
        raise WorkspacePathError("workspace path must be normalized and must not contain '..'")
    parts = PurePosixPath(portable).parts
    if tuple(raw) != parts:
        raise WorkspacePathError("workspace path must be normalized")
    return parts


def _portable(parts: tuple[str, ...]) -> str:
    return "." if not parts else "/".join(parts)


def _kind(mode: int) -> str:
    if stat.S_ISREG(mode):
        return "file"
    if stat.S_ISDIR(mode):
        return "directory"
    if stat.S_ISLNK(mode):
        return "symlink"
    return "other"


def _path_error(action: str, relative_path: str, exc: OSError) -> WorkspacePathError:
    if exc.errno in {errno.ELOOP, errno.ENOTDIR}:
        detail = "contains forbidden symbolic links or non-directory components; escapes are blocked"
    elif exc.errno == errno.ENOENT:
        detail = "does not exist"
    else:
        detail = "could not be accessed safely"
    return WorkspacePathError(f"workspace path {relative_path!r} {detail} during {action}")


class _WalkStopped(Exception):
    pass


class SecurePosixWorkspaceFileSystem:
    """Filesystem backend anchored by directory descriptors and no-follow opens."""

    production_safe = True

    def __init__(self) -> None:
        if not secure_workspace_supported():
            raise RuntimeError(
                "the secure workspace backend requires Linux dir_fd, O_NOFOLLOW, and fd scandir"
            )
        self._directory_flags = (
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
        )
        self._file_flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK

    @contextmanager
    def _open_root(self, root: Path) -> Iterator[int]:
        path = Path(root)
        if not path.is_absolute() or path.anchor != "/":
            raise WorkspacePathError("secure workspace roots must be absolute POSIX paths")
        parts = path.parts[1:]
        if any(part in {"", ".", ".."} for part in parts):
            raise WorkspacePathError("secure workspace roots must be normalized")
        descriptor = os.open("/", self._directory_flags)
        try:
            for part in parts:
                try:
                    child = os.open(part, self._directory_flags, dir_fd=descriptor)
                except OSError as exc:
                    raise _path_error("root traversal", str(path), exc) from exc
                os.close(descriptor)
                descriptor = child
            yield descriptor
        finally:
            os.close(descriptor)

    def _open_child_directory(self, parent_fd: int, name: str, relative: str) -> int:
        try:
            return os.open(name, self._directory_flags, dir_fd=parent_fd)
        except OSError as exc:
            raise _path_error("directory traversal", relative, exc) from exc

    @contextmanager
    def _open_directory(self, root: Path, relative_path: str) -> Iterator[int]:
        parts = _segments(relative_path, allow_root=True)
        with self._open_root(root) as root_fd:
            descriptor = os.dup(root_fd)
            try:
                for index, part in enumerate(parts):
                    child = self._open_child_directory(
                        descriptor, part, _portable(parts[: index + 1])
                    )
                    os.close(descriptor)
                    descriptor = child
                yield descriptor
            finally:
                os.close(descriptor)

    @contextmanager
    def _open_parent(self, root: Path, relative_path: str) -> Iterator[tuple[int, str]]:
        parts = _segments(relative_path, allow_root=False)
        with self._open_root(root) as root_fd:
            descriptor = os.dup(root_fd)
            try:
                for index, part in enumerate(parts[:-1]):
                    child = self._open_child_directory(
                        descriptor, part, _portable(parts[: index + 1])
                    )
                    os.close(descriptor)
                    descriptor = child
                yield descriptor, parts[-1]
            finally:
                os.close(descriptor)

    @staticmethod
    def _stat_at(parent_fd: int, name: str) -> os.stat_result:
        return os.stat(name, dir_fd=parent_fd, follow_symlinks=False)

    def stat(self, root: Path, relative_path: str) -> SecureFileStat:
        parts = _segments(relative_path, allow_root=True)
        if not parts:
            with self._open_root(root) as descriptor:
                snapshot = os.fstat(descriptor)
        else:
            try:
                with self._open_parent(root, relative_path) as (parent_fd, leaf):
                    snapshot = self._stat_at(parent_fd, leaf)
            except OSError as exc:
                raise _path_error("stat", relative_path, exc) from exc
        kind = _kind(snapshot.st_mode)
        return SecureFileStat(kind, snapshot.st_size if kind == "file" else None)

    def _read_at(
        self,
        parent_fd: int,
        leaf: str,
        relative_path: str,
        max_bytes: int,
    ) -> tuple[bytes, tuple[int, int]]:
        try:
            descriptor = os.open(leaf, self._file_flags, dir_fd=parent_fd)
        except OSError as exc:
            raise _path_error("read", relative_path, exc) from exc
        try:
            snapshot = os.fstat(descriptor)
            if not stat.S_ISREG(snapshot.st_mode):
                raise ValueError("workspace read target must be a regular file")
            remaining = max_bytes + 1
            chunks: list[bytes] = []
            while remaining:
                chunk = os.read(descriptor, min(remaining, 64 * 1024))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            return b"".join(chunks), (snapshot.st_dev, snapshot.st_ino)
        finally:
            os.close(descriptor)

    def read_file(self, root: Path, relative_path: str, max_bytes: int) -> bytes:
        with self._open_parent(root, relative_path) as (parent_fd, leaf):
            content, _identity = self._read_at(
                parent_fd, leaf, relative_path, max_bytes
            )
            return content

    @staticmethod
    def _write_all(descriptor: int, content: bytes) -> None:
        view = memoryview(content)
        offset = 0
        while offset < len(view):
            written = os.write(descriptor, view[offset:])
            if written <= 0:  # pragma: no cover - defensive OS contract check
                raise OSError("short workspace write")
            offset += written

    def _create_temporary(self, parent_fd: int) -> tuple[int, str]:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC
        for _attempt in range(32):
            name = f".suiteharness-write-{secrets.token_hex(16)}"
            try:
                return os.open(name, flags, 0o600, dir_fd=parent_fd), name
            except FileExistsError:
                continue
        raise WorkspacePathError("could not allocate a unique workspace temporary file")

    def _atomic_write_at(
        self,
        parent_fd: int,
        leaf: str,
        relative_path: str,
        content: bytes,
        *,
        overwrite: bool,
        expected_identity: tuple[int, int] | None = None,
    ) -> None:
        if overwrite:
            try:
                existing = self._stat_at(parent_fd, leaf)
            except FileNotFoundError:
                existing = None
            if existing is not None:
                if not stat.S_ISREG(existing.st_mode):
                    raise ValueError("write target must be a regular file")
                if expected_identity is not None and (
                    existing.st_dev,
                    existing.st_ino,
                ) != expected_identity:
                    raise WorkspacePathError("edit target changed during the operation")

        descriptor, temporary = self._create_temporary(parent_fd)
        temporary_exists = True
        try:
            try:
                self._write_all(descriptor, content)
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            if overwrite:
                os.rename(
                    temporary,
                    leaf,
                    src_dir_fd=parent_fd,
                    dst_dir_fd=parent_fd,
                )
                temporary_exists = False
            else:
                # linkat is an atomic no-replace publication: an attacker cannot
                # insert the destination between an existence check and commit.
                os.link(
                    temporary,
                    leaf,
                    src_dir_fd=parent_fd,
                    dst_dir_fd=parent_fd,
                    follow_symlinks=False,
                )
                os.unlink(temporary, dir_fd=parent_fd)
                temporary_exists = False
            os.fsync(parent_fd)
        except FileExistsError:
            raise FileExistsError(
                "target exists; set overwrite=true to replace it"
            ) from None
        except OSError as exc:
            raise _path_error("write", relative_path, exc) from exc
        finally:
            if temporary_exists:
                try:
                    os.unlink(temporary, dir_fd=parent_fd)
                except FileNotFoundError:
                    pass

    def write_file(
        self,
        root: Path,
        relative_path: str,
        content: bytes,
        *,
        overwrite: bool,
    ) -> None:
        with self._open_parent(root, relative_path) as (parent_fd, leaf):
            self._atomic_write_at(
                parent_fd,
                leaf,
                relative_path,
                content,
                overwrite=overwrite,
            )

    def transform_file(
        self,
        root: Path,
        relative_path: str,
        *,
        max_read_bytes: int,
        max_write_bytes: int,
        transform: Callable[[bytes], tuple[bytes, _T]],
    ) -> _T:
        """Read, transform and replace one file while retaining its parent fd."""

        with self._open_parent(root, relative_path) as (parent_fd, leaf):
            content, identity = self._read_at(
                parent_fd, leaf, relative_path, max_read_bytes
            )
            if len(content) > max_read_bytes:
                raise ValueError("edit target exceeds the configured byte limit")
            updated, result = transform(content)
            if len(updated) > max_write_bytes:
                raise ValueError("edited content exceeds the configured byte limit")
            self._atomic_write_at(
                parent_fd,
                leaf,
                relative_path,
                updated,
                overwrite=True,
                expected_identity=identity,
            )
            return result

    def list_directory(
        self,
        root: Path,
        relative_path: str,
        *,
        limit: int,
        max_scanned_entries: int,
        deadline: float,
    ) -> SecureDirectoryListing:
        with self._open_directory(root, relative_path) as descriptor:
            entries: list[SecureDirectoryEntry] = []
            truncated = False
            scanned = 0
            with os.scandir(descriptor) as iterator:
                for entry in iterator:
                    if time.monotonic() >= deadline or scanned >= max_scanned_entries:
                        truncated = True
                        break
                    scanned += 1
                    try:
                        snapshot = entry.stat(follow_symlinks=False)
                    except FileNotFoundError:
                        continue
                    kind = _kind(snapshot.st_mode)
                    path = entry.name if relative_path == "." else f"{relative_path}/{entry.name}"
                    entries.append(
                        SecureDirectoryEntry(
                            name=entry.name,
                            path=path,
                            kind=kind,
                            size=snapshot.st_size if kind == "file" else None,
                        )
                    )
            entries.sort(key=lambda item: (item.name.casefold(), item.name))
            if len(entries) > limit:
                entries = entries[:limit]
                truncated = True
            return SecureDirectoryListing(tuple(entries), truncated)

    def glob(
        self,
        root: Path,
        pattern: str,
        *,
        max_results: int,
        max_scanned_entries: int,
        max_depth: int,
        deadline: float,
    ) -> SecurePathCollection:
        pattern_parts = _segments(pattern, allow_root=False)
        if len(pattern_parts) > max_depth + 1:
            raise ValueError("glob pattern exceeds the configured traversal depth")
        matches: set[str] = set()
        visited_states: set[tuple[tuple[str, ...], int]] = set()
        scanned = 0
        truncated = False

        def stop_if_bounded() -> None:
            nonlocal truncated
            if time.monotonic() >= deadline or scanned >= max_scanned_entries:
                truncated = True
                raise _WalkStopped
            if len(matches) >= max_results:
                truncated = True
                raise _WalkStopped

        def scan(descriptor: int) -> list[tuple[str, int, int]]:
            nonlocal scanned, truncated
            snapshots: list[tuple[str, int, int]] = []
            with os.scandir(descriptor) as iterator:
                for entry in iterator:
                    if time.monotonic() >= deadline or scanned >= max_scanned_entries:
                        truncated = True
                        raise _WalkStopped
                    scanned += 1
                    try:
                        snapshot = entry.stat(follow_symlinks=False)
                    except FileNotFoundError:
                        continue
                    snapshots.append((entry.name, snapshot.st_mode, snapshot.st_size))
            snapshots.sort(key=lambda item: (item[0].casefold(), item[0]))
            return snapshots

        def match_from(
            descriptor: int,
            relative: tuple[str, ...],
            pattern_index: int,
            depth: int,
        ) -> None:
            state = (relative, pattern_index)
            if state in visited_states:
                return
            visited_states.add(state)
            stop_if_bounded()
            if pattern_index == len(pattern_parts):
                matches.add(_portable(relative))
                return
            segment = pattern_parts[pattern_index]
            if segment == "**":
                match_from(descriptor, relative, pattern_index + 1, depth)
                if depth >= max_depth:
                    return
                for name, mode, _size in scan(descriptor):
                    if not stat.S_ISDIR(mode):
                        continue
                    child_relative = (*relative, name)
                    child = self._open_child_directory(
                        descriptor, name, _portable(child_relative)
                    )
                    try:
                        match_from(child, child_relative, pattern_index, depth + 1)
                    finally:
                        os.close(child)
                return
            for name, mode, _size in scan(descriptor):
                if not fnmatch.fnmatchcase(name, segment):
                    continue
                kind = _kind(mode)
                child_relative = (*relative, name)
                if pattern_index == len(pattern_parts) - 1:
                    if kind in {"file", "directory"}:
                        matches.add(_portable(child_relative))
                        stop_if_bounded()
                    continue
                if kind != "directory" or depth >= max_depth:
                    continue
                child = self._open_child_directory(
                    descriptor, name, _portable(child_relative)
                )
                try:
                    match_from(child, child_relative, pattern_index + 1, depth + 1)
                finally:
                    os.close(child)

        try:
            with self._open_root(root) as root_fd:
                match_from(root_fd, (), 0, 0)
        except _WalkStopped:
            pass
        ordered = sorted(matches)
        if len(ordered) > max_results:
            ordered = ordered[:max_results]
            truncated = True
        return SecurePathCollection(tuple(ordered), truncated)

    def walk_regular_files(
        self,
        root: Path,
        base_path: str,
        *,
        file_pattern: str,
        max_files: int,
        max_scanned_entries: int,
        max_depth: int,
        deadline: float,
    ) -> SecurePathCollection:
        base_parts = _segments(base_path, allow_root=True)
        base_stat = self.stat(root, base_path)
        if base_stat.kind == "file":
            name = base_parts[-1]
            paths = (base_path,) if fnmatch.fnmatchcase(name, file_pattern) else ()
            return SecurePathCollection(paths, False)
        if base_stat.kind != "directory":
            raise ValueError("grep path must be a regular file or directory")

        files: list[str] = []
        scanned = 0
        truncated = False

        def visit(descriptor: int, relative: tuple[str, ...], depth: int) -> None:
            nonlocal scanned, truncated
            snapshots: list[tuple[str, int]] = []
            with os.scandir(descriptor) as iterator:
                for entry in iterator:
                    if time.monotonic() >= deadline or scanned >= max_scanned_entries:
                        truncated = True
                        raise _WalkStopped
                    scanned += 1
                    try:
                        snapshot = entry.stat(follow_symlinks=False)
                    except FileNotFoundError:
                        continue
                    snapshots.append((entry.name, snapshot.st_mode))
            snapshots.sort(key=lambda item: (item[0].casefold(), item[0]))
            for name, mode in snapshots:
                child_relative = (*relative, name)
                logical = _portable(child_relative)
                if stat.S_ISREG(mode):
                    from_base = "/".join(child_relative[len(base_parts) :])
                    if fnmatch.fnmatchcase(from_base, file_pattern):
                        files.append(logical)
                        if len(files) >= max_files:
                            truncated = True
                            raise _WalkStopped
                elif stat.S_ISDIR(mode) and depth < max_depth:
                    child = self._open_child_directory(descriptor, name, logical)
                    try:
                        visit(child, child_relative, depth + 1)
                    finally:
                        os.close(child)

        try:
            with self._open_directory(root, base_path) as base_fd:
                visit(base_fd, base_parts, 0)
        except _WalkStopped:
            pass
        return SecurePathCollection(tuple(files), truncated)


__all__ = [
    "SecureDirectoryEntry",
    "SecureDirectoryListing",
    "SecureFileStat",
    "SecurePathCollection",
    "SecurePosixWorkspaceFileSystem",
    "secure_workspace_supported",
]
