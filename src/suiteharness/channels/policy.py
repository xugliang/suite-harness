"""Execution policy shared by the Web and Feishu company channels."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import PurePosixPath

from pydantic import JsonValue

from suiteharness.execution.models import (
    RunRequest,
    ToolAuthorization,
    ToolEffect,
    ToolIdentity,
    ToolSpec,
)

_DRIVE = re.compile(r"^[A-Za-z]:")
_FEISHU_WRITABLE_TOOLS = frozenset({"suiteharness.fs.write", "suiteharness.fs.edit"})


def _parts(value: object, *, allow_root: bool) -> tuple[str, ...] | None:
    if not isinstance(value, str) or not value or "\x00" in value:
        return None
    portable = value.replace("\\", "/")
    if portable == ".":
        return () if allow_root else None
    if portable.startswith("/") or portable.startswith("//") or _DRIVE.match(portable):
        return None
    raw = portable.split("/")
    if any(part in {"", ".", ".."} for part in raw):
        return None
    parsed = PurePosixPath(portable).parts
    return parsed if tuple(raw) == parsed else None


@dataclass(frozen=True, slots=True)
class WorkspaceWriteRule:
    """One Feishu-writable path relative to a product/shared workspace."""

    space: str
    path: str

    def __post_init__(self) -> None:
        if self.space not in {"product", "shared"}:
            raise ValueError("workspace rule space must be 'product' or 'shared'")
        if _parts(self.path, allow_root=True) is None:
            raise ValueError("workspace rule path must be normalized and relative")

    def permits(self, space: object, path: object) -> bool:
        if space is None:
            space = "product"
        if space != self.space:
            return False
        configured = _parts(self.path, allow_root=True)
        requested = _parts(path, allow_root=False)
        if configured is None or requested is None:
            return False
        return not configured or requested[: len(configured)] == configured


class CompanyChannelAuthorizationPolicy:
    """Keep channel UX decisions inside the non-replaceable execution path.

    Web reads run directly and writes require an exact single-use approval.
    Feishu reads run directly; only built-in file write/edit calls underneath a
    configured relative root may run without an impossible chat approval.
    Feishu destructive, shell, MCP mutation, and other write tools are denied.
    The workspace handler performs the canonical/symlink check again.
    """

    def __init__(self, *, feishu_writable_roots: tuple[WorkspaceWriteRule, ...] = ()) -> None:
        self._feishu_writable_roots = tuple(feishu_writable_roots)

    def evaluate(
        self,
        request: RunRequest,
        identity: ToolIdentity,
        spec: ToolSpec,
        arguments: dict[str, JsonValue],
    ) -> ToolAuthorization:
        channel = request.scope.channel_id
        if channel == "web":
            if ToolEffect.WRITE in spec.effects or ToolEffect.DESTRUCTIVE in spec.effects:
                return ToolAuthorization.REQUIRE_APPROVAL
            return ToolAuthorization.ALLOW
        if channel != "feishu":
            return ToolAuthorization.DEFAULT
        if ToolEffect.DESTRUCTIVE in spec.effects:
            return ToolAuthorization.DENY
        if spec.is_read:
            return ToolAuthorization.ALLOW
        is_builtin_file_write = (
            identity.namespace == "suiteharness"
            and identity.origin == "suiteharness.builtin.fs"
            and identity.name in _FEISHU_WRITABLE_TOOLS
            and spec.name == identity.name
        )
        if not is_builtin_file_write:
            return ToolAuthorization.DENY
        if any(
            rule.permits(arguments.get("space", "product"), arguments.get("path"))
            for rule in self._feishu_writable_roots
        ):
            return ToolAuthorization.ALLOW
        return ToolAuthorization.DENY


__all__ = ["CompanyChannelAuthorizationPolicy", "WorkspaceWriteRule"]
