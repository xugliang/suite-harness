"""Bounded, immutable packages from explicitly registered directories.

Markdown is data: no execution, interpolation, link fetching or ambient discovery.
On Linux each path component is opened relative to an already-open directory
with O_NOFOLLOW so a symlink swap cannot escape a configured root.
"""

from __future__ import annotations

import hashlib
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType

import yaml

from .models import (
    SkillAccess,
    SkillDescriptor,
    SkillDocument,
    SkillError,
    SkillLimits,
    content_sha256,
    skill_identifier,
)
from .protocols import SkillAccessPolicy, SkillProvider
from .registry import SkillRegistry


class _MetadataLoader(yaml.SafeLoader):
    def compose_node(self, parent, index):
        if self.check_event(yaml.AliasEvent):
            raise SkillError("skill_invalid", "YAML aliases are not allowed in skill metadata")
        return super().compose_node(parent, index)


def _unique_mapping(loader, node, deep=False):
    result = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if not isinstance(key, str) or key in result:
            raise SkillError("skill_invalid", "Duplicate or invalid skill metadata key")
        result[key] = loader.construct_object(value_node, deep=deep)
    return result


_MetadataLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _unique_mapping)


@dataclass(frozen=True, slots=True)
class _Entry:
    descriptor: SkillDescriptor
    relative_path: Path
    body: str
    reply_path: Path | None
    reply_sha256: str | None
    reply_body: str | None


class FilesystemSkillProvider(SkillProvider):
    def __init__(
        self,
        root: Path | str,
        *,
        namespace: str,
        access_policy: SkillAccessPolicy | None = None,
        limits: SkillLimits | None = None,
    ) -> None:
        self.namespace = skill_identifier(namespace)
        self.limits = limits or SkillLimits()
        self._access_policy = access_policy
        self._root = Path(root).absolute()
        try:
            if self._root.is_symlink() or self._root.resolve(strict=True) != self._root:
                raise SkillError("skill_invalid", "Skill root must be a real registered directory")
            if not self._root.is_dir():
                raise SkillError("skill_invalid", "Skill root must be a directory")
            root_stat = self._root.stat()
            self._root_identity = (root_stat.st_dev, root_stat.st_ino)
        except OSError as exc:
            raise SkillError("skill_invalid", "Skill root is not available") from exc
        entries: dict[str, _Entry] = {}
        visited = 0
        for directory, directories, filenames in os.walk(self._root, followlinks=False):
            directories.sort()
            filenames.sort()
            current = Path(directory)
            # Do not silently omit a symlinked subtree containing required rules.
            for name in directories:
                if (current / name).is_symlink():
                    raise SkillError("skill_invalid", "Symlinks are not allowed in a skill package")
            visited += len(directories) + len(filenames)
            if visited > self.limits.max_files:
                raise SkillError("skill_budget_exceeded", "Skill package file limit exceeded")
            if "SKILL.md" not in filenames:
                continue
            relative = (current / "SKILL.md").relative_to(self._root)
            raw = self._safe_read(relative)
            metadata, body = self._parse(raw)
            descriptor = self._descriptor(metadata, raw)
            if descriptor.skill_id in entries:
                raise SkillError("skill_invalid", "Duplicate registered skill identifier")
            reply_path = relative.with_name("REPLY.md") if "REPLY.md" in filenames else None
            reply_raw = self._safe_read(reply_path) if reply_path is not None else None
            reply_body = self._text(reply_raw).strip() if reply_raw is not None else None
            if reply_body is not None and not reply_body:
                raise SkillError("skill_invalid", "Skill reply resource is empty")
            entries[descriptor.skill_id] = _Entry(
                descriptor, relative, body, reply_path,
                hashlib.sha256(reply_raw).hexdigest() if reply_raw is not None else None,
                reply_body,
            )
            if len(entries) > self.limits.max_skills:
                raise SkillError("skill_budget_exceeded", "Skill package registration limit exceeded")
        self._entries = MappingProxyType(entries)
        self._registry = SkillRegistry(
            namespace, (entry.descriptor for entry in entries.values()),
            max_dependency_depth=self.limits.max_dependency_depth,
        )
        # Startup verifies all resources, including command-only skills. There is
        # no synthetic employee and no permission granted by this static check.
        for skill_id, entry in entries.items():
            self.compile_resource(skill_id)
            if entry.reply_path is not None:
                self.compile_resource(skill_id, resource_id="reply")

    def static_manifest(self) -> tuple[SkillDescriptor, ...]:
        return self._registry.manifest()

    def _allowed(self, access: SkillAccess, descriptor: SkillDescriptor, resource: str) -> bool:
        if access.scope.product_id != self.namespace:
            return False
        return self._access_policy is None or bool(self._access_policy(access, descriptor, resource))

    @staticmethod
    def _invocable(access: SkillAccess, descriptor: SkillDescriptor) -> bool:
        return not (
            (access.source == "model" and not descriptor.model_invocable)
            or (access.source == "command" and not descriptor.user_invocable)
        )

    async def list_skills(self, access: SkillAccess) -> tuple[SkillDescriptor, ...]:
        return tuple(
            descriptor for descriptor in self.static_manifest()
            if self._invocable(access, descriptor) and self._allowed(access, descriptor, "instructions")
        )

    async def read_skill(
        self, access: SkillAccess, skill_id: str, *, resource_id: str = "instructions"
    ) -> SkillDocument:
        descriptor = self._registry.descriptor(skill_id)
        if resource_id not in {"instructions", "reply"} or (
            resource_id == "reply" and access.source != "workflow"
        ):
            raise SkillError("skill_not_available", "Skill is not available")
        if not self._invocable(access, descriptor):
            raise SkillError("skill_not_available", "Skill is not available")
        expanded = self._registry.expanded_ids(skill_id) if resource_id == "instructions" else (skill_id,)
        for dependency in expanded:
            if not self._allowed(access, self._registry.descriptor(dependency), resource_id):
                raise SkillError("skill_not_available", "Skill is not available")
        return self.compile_resource(skill_id, resource_id=resource_id)

    def compile_resource(self, skill_id: str, *, resource_id: str = "instructions") -> SkillDocument:
        """Trusted host initialization/shim only; never grants runtime active state."""
        descriptor = self._registry.descriptor(skill_id)
        if resource_id == "instructions":
            expanded = self._registry.expanded_ids(skill_id)
            bodies = []
            for dependency in expanded:
                entry = self._entries[dependency]
                self._verify(entry.relative_path, entry.descriptor.source_sha256)
                bodies.append(entry.body)
            instructions = "\n\n".join(bodies)
            dependencies = tuple(item for item in expanded if item != skill_id)
        elif resource_id == "reply":
            entry = self._entries[skill_id]
            if entry.reply_path is None or entry.reply_sha256 is None or entry.reply_body is None:
                raise SkillError("skill_not_available", "Skill is not available")
            self._verify(entry.relative_path, entry.descriptor.source_sha256)
            self._verify(entry.reply_path, entry.reply_sha256)
            instructions = entry.reply_body
            dependencies = ()
        else:
            raise SkillError("skill_not_available", "Skill is not available")
        if len(instructions.encode("utf-8")) > self.limits.max_instruction_bytes:
            raise SkillError("skill_budget_exceeded", "Complete skill instructions exceed the byte limit")
        return SkillDocument(
            descriptor, resource_id, instructions, content_sha256(instructions), dependencies,
            source="filesystem",
        )

    def _verify(self, relative: Path, expected: str) -> None:
        if hashlib.sha256(self._safe_read(relative)).hexdigest() != expected:
            raise SkillError("skill_content_changed", "Registered skill content changed; reload the product")

    def _safe_read(self, relative: Path) -> bytes:
        if relative.is_absolute() or any(part in {"", ".", ".."} for part in relative.parts):
            raise SkillError("skill_invalid", "Unsafe registered skill resource")
        fd: int | None = None
        directories: list[int] = []
        try:
            if os.name == "posix":
                flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
                parent_fd = os.open(self._root, flags)
                directories.append(parent_fd)
                root_stat = os.fstat(parent_fd)
                if (root_stat.st_dev, root_stat.st_ino) != self._root_identity:
                    raise SkillError("skill_content_changed", "Registered skill root changed; reload the product")
                for part in relative.parts[:-1]:
                    parent_fd = os.open(part, flags, dir_fd=parent_fd)
                    directories.append(parent_fd)
                fd = os.open(relative.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent_fd)
            else:
                root_stat = self._root.lstat()
                if (root_stat.st_dev, root_stat.st_ino) != self._root_identity or self._root.is_symlink():
                    raise SkillError("skill_content_changed", "Registered skill root changed; reload the product")
                candidate = self._root
                for part in relative.parts:
                    candidate = candidate / part
                    if candidate.is_symlink():
                        raise SkillError("skill_invalid", "Symlinks are not allowed in a skill package")
                if not candidate.resolve(strict=True).is_relative_to(self._root):
                    raise SkillError("skill_invalid", "Skill resource escaped its registered root")
                fd = os.open(candidate, os.O_RDONLY | getattr(os, "O_BINARY", 0))
                after = candidate.lstat()
                opened = os.fstat(fd)
                if (after.st_dev, after.st_ino) != (opened.st_dev, opened.st_ino):
                    raise SkillError("skill_read_failed", "Skill resource changed while being opened")
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode):
                raise SkillError("skill_invalid", "Skill resource must be a regular file")
            if info.st_size > self.limits.max_file_bytes:
                raise SkillError("skill_budget_exceeded", "Skill source file limit exceeded")
            chunks = []
            remaining = self.limits.max_file_bytes + 1
            while remaining:
                chunk = os.read(fd, min(remaining, 65536))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            raw = b"".join(chunks)
            if len(raw) > self.limits.max_file_bytes:
                raise SkillError("skill_budget_exceeded", "Skill source file limit exceeded")
            return raw
        except OSError as exc:
            raise SkillError("skill_read_failed", "Registered skill resource could not be read") from exc
        finally:
            if fd is not None:
                os.close(fd)
            for directory_fd in reversed(directories):
                os.close(directory_fd)

    @staticmethod
    def _text(raw: bytes) -> str:
        try:
            return raw.decode("utf-8")
        except UnicodeError as exc:
            raise SkillError("skill_invalid", "Skill files must be UTF-8") from exc

    def _parse(self, raw: bytes) -> tuple[dict, str]:
        text = self._text(raw).replace("\r\n", "\n")
        if not text.startswith("---\n"):
            raise SkillError("skill_invalid", "Skill metadata frontmatter is required")
        end = text.find("\n---\n", 4)
        if end < 0:
            raise SkillError("skill_invalid", "Skill metadata closing delimiter is missing")
        header = text[4:end]
        if len(header.encode("utf-8")) > self.limits.max_metadata_bytes:
            raise SkillError("skill_budget_exceeded", "Skill metadata limit exceeded")
        try:
            metadata = yaml.load(header, Loader=_MetadataLoader)
        except yaml.YAMLError as exc:
            raise SkillError("skill_invalid", "Invalid skill metadata YAML") from exc
        if not isinstance(metadata, dict):
            raise SkillError("skill_invalid", "Skill metadata must be a mapping")
        body = text[end + 5:].strip()
        if not body:
            raise SkillError("skill_invalid", "Skill instructions are empty")
        return metadata, body

    def _descriptor(self, metadata: dict, raw: bytes) -> SkillDescriptor:
        allowed = {
            "name", "display_name", "description", "version", "includes",
            "user-invocable", "disable-model-invocation",
        }
        if metadata.keys() - allowed or not {"name", "description", "version"} <= metadata.keys():
            raise SkillError("skill_invalid", "Unknown or missing skill metadata fields")
        name = skill_identifier(metadata["name"])
        includes = metadata.get("includes", [])
        if not isinstance(includes, list) or any(not isinstance(item, str) for item in includes):
            raise SkillError("skill_invalid", "Skill includes must be a list of package-local names")
        for flag in ("user-invocable", "disable-model-invocation"):
            if flag in metadata and type(metadata[flag]) is not bool:
                raise SkillError("skill_invalid", "Skill invocation flags must be boolean")
        return SkillDescriptor(
            skill_id=f"{self.namespace}.{name}", name=name,
            display_name=metadata.get("display_name"), description=metadata["description"],
            version=metadata["version"], source_sha256=hashlib.sha256(raw).hexdigest(),
            includes=tuple(f"{self.namespace}.{skill_identifier(item)}" for item in includes),
            model_invocable=not metadata.get("disable-model-invocation", False),
            user_invocable=metadata.get("user-invocable", True),
        )


__all__ = ["FilesystemSkillProvider"]
