"""Immutable, provider-neutral skill descriptions; no product business rules."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Literal

from suiteharness.runtime.scopes import RequestScope

_IDENTIFIER = re.compile(r"^[a-z][a-z0-9]*(?:[._-][a-z0-9]+)*$")
_SHA256 = re.compile(r"^[a-f0-9]{64}$")


class SkillError(RuntimeError):
    """Closed, model-safe failure. Do not include file paths or credentials."""

    def __init__(self, code: str, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable


def skill_identifier(value: str) -> str:
    if not isinstance(value, str) or len(value) > 128 or not _IDENTIFIER.fullmatch(value):
        raise SkillError("skill_invalid", "Invalid registered skill identifier")
    return value


def content_sha256(value: str) -> str:
    try:
        return hashlib.sha256(value.encode("utf-8")).hexdigest()
    except UnicodeError as exc:
        raise SkillError("skill_invalid", "Skill content must be valid UTF-8") from exc


@dataclass(frozen=True, slots=True)
class SkillAccess:
    """Trusted request identity, created by the host rather than tool arguments."""

    scope: RequestScope
    source: Literal["model", "command", "workflow"] = "model"
    run_id: str = ""
    command_name: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.scope, RequestScope):
            raise TypeError("SkillAccess requires RequestScope")
        if self.source not in {"model", "command", "workflow"}:
            raise ValueError("invalid skill access source")
        if self.command_name is not None and self.source != "command":
            raise ValueError("command_name is only valid for command access")


@dataclass(frozen=True, slots=True)
class SkillLimits:
    max_metadata_bytes: int = 4096
    max_instruction_bytes: int = 12288
    max_active_bytes: int = 49152
    max_active_skills: int = 8
    max_dependency_depth: int = 4
    max_file_bytes: int = 131072
    max_skills: int = 128
    max_files: int = 1024

    def __post_init__(self) -> None:
        for name in self.__dataclass_fields__:
            value = getattr(self, name)
            if type(value) is not int or value <= 0:
                raise ValueError(f"{name} must be a positive integer")


@dataclass(frozen=True, slots=True)
class SkillDescriptor:
    skill_id: str
    name: str
    description: str
    version: str
    source_sha256: str
    display_name: str | None = None
    model_invocable: bool = True
    user_invocable: bool = True
    includes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        skill_identifier(self.skill_id)
        skill_identifier(self.name)
        if not isinstance(self.description, str) or not self.description.strip() or len(self.description) > 256:
            raise SkillError("skill_invalid", "Skill description must contain 1 to 256 characters")
        if not isinstance(self.version, str) or not self.version.strip() or len(self.version) > 64:
            raise SkillError("skill_invalid", "Skill version must contain 1 to 64 characters")
        if not isinstance(self.source_sha256, str) or not _SHA256.fullmatch(self.source_sha256):
            raise SkillError("skill_invalid", "Skill source hash must be SHA-256")
        if self.display_name is not None and (
            not isinstance(self.display_name, str) or not self.display_name.strip() or len(self.display_name) > 128
        ):
            raise SkillError("skill_invalid", "Invalid skill display name")
        if type(self.model_invocable) is not bool or type(self.user_invocable) is not bool:
            raise SkillError("skill_invalid", "Skill invocation flags must be boolean")
        for value in (self.description, self.version, self.display_name or ""):
            content_sha256(value)
        if not isinstance(self.includes, tuple | list) or any(
            not isinstance(item, str) for item in self.includes
        ):
            raise SkillError("skill_invalid", "Skill includes must contain registered identifiers")
        includes = tuple(self.includes)
        if len(includes) != len(set(includes)):
            raise SkillError("skill_invalid", "Duplicate skill dependency")
        for item in includes:
            skill_identifier(item)
        object.__setattr__(self, "includes", includes)


@dataclass(frozen=True, slots=True)
class SkillDocument:
    descriptor: SkillDescriptor
    resource_id: str
    instructions: str
    instructions_sha256: str
    dependencies: tuple[str, ...] = ()
    source: str = "filesystem"

    def __post_init__(self) -> None:
        if not isinstance(self.descriptor, SkillDescriptor):
            raise SkillError("skill_invalid", "Skill document requires a valid descriptor")
        if self.resource_id not in {"instructions", "reply"}:
            raise SkillError("skill_invalid", "Unknown skill resource")
        if not isinstance(self.instructions, str) or not self.instructions.strip():
            raise SkillError("skill_invalid", "Skill instructions must not be empty")
        if self.instructions_sha256 != content_sha256(self.instructions):
            raise SkillError("skill_content_changed", "Skill instructions hash does not match")
        dependencies = tuple(self.dependencies)
        for item in dependencies:
            skill_identifier(item)
        if len(dependencies) != len(set(dependencies)):
            raise SkillError("skill_invalid", "Duplicate expanded skill dependency")
        object.__setattr__(self, "dependencies", dependencies)

    @property
    def skill_id(self) -> str:
        return self.descriptor.skill_id

    @property
    def version(self) -> str:
        return self.descriptor.version


__all__ = ["SkillAccess", "SkillDescriptor", "SkillDocument", "SkillError", "SkillLimits"]
