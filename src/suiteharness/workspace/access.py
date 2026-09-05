"""Channel-neutral workspace authorization policies."""

from __future__ import annotations

import os
from collections.abc import Iterable
from dataclasses import dataclass
from enum import Enum
from pathlib import Path, PurePosixPath

from .layout import WorkspacePathError, WorkspacePaths, resolve_beneath


class WorkspaceOperation(str, Enum):
    READ = "read"
    WRITE = "write"
    DELETE = "delete"


class WorkspaceAccessDenied(PermissionError):
    """The channel policy does not authorize the requested filesystem effect."""


class WorkspaceSpace(str, Enum):
    PRODUCT = "product"
    SHARED = "shared"


@dataclass(frozen=True)
class WritableWorkspaceRoot:
    space: WorkspaceSpace
    relative_path: str


def _canonical(path: Path) -> Path:
    try:
        return path.resolve(strict=path.exists())
    except OSError as exc:
        raise WorkspaceAccessDenied("workspace target could not be resolved") from exc


def _under_any(path: Path, roots: tuple[Path, ...]) -> bool:
    return any(path == root or path.is_relative_to(root) for root in roots)


def _lexical(path: Path) -> Path:
    """Normalize an absolute path without consulting mutable filesystem state."""

    if not path.is_absolute():
        raise WorkspaceAccessDenied("workspace policy paths must be absolute")
    return Path(os.path.abspath(os.path.normpath(path)))


def _logical_candidate(root: Path, relative_path: str) -> Path:
    portable = relative_path.replace("\\", "/")
    if portable == ".":
        parts: tuple[str, ...] = ()
    else:
        raw = portable.split("/")
        if (
            not portable
            or portable.startswith("/")
            or any(part in {"", ".", ".."} for part in raw)
            or tuple(raw) != PurePosixPath(portable).parts
        ):
            raise WorkspaceAccessDenied("workspace path must be normalized and relative")
        parts = tuple(raw)
    return _lexical(root).joinpath(*parts)


class WorkspaceAccessPolicy:
    """Authorize canonical paths against read/write roots and deletion policy."""

    def __init__(
        self,
        *,
        readable_roots: Iterable[Path],
        writable_roots: Iterable[Path] = (),
        allow_delete: bool = False,
    ) -> None:
        readable_items = tuple(Path(item) for item in readable_roots)
        writable_items = tuple(Path(item) for item in writable_roots)
        readable = tuple(dict.fromkeys(_canonical(item) for item in readable_items))
        writable = tuple(dict.fromkeys(_canonical(item) for item in writable_items))
        if not readable:
            raise ValueError("an access policy requires at least one readable root")
        if any(not root.is_dir() for root in (*readable, *writable)):
            raise WorkspacePathError("policy roots must be existing directories")
        if any(not _under_any(root, readable) for root in writable):
            raise ValueError("every writable root must be inside a readable root")
        self._readable_roots = readable
        self._writable_roots = writable
        self._logical_readable_roots = tuple(
            dict.fromkeys(_lexical(item) for item in readable_items)
        )
        self._logical_writable_roots = tuple(
            dict.fromkeys(_lexical(item) for item in writable_items)
        )
        self._allow_delete = allow_delete

    @property
    def readable_roots(self) -> tuple[Path, ...]:
        return self._readable_roots

    @property
    def writable_roots(self) -> tuple[Path, ...]:
        return self._writable_roots

    @property
    def allow_delete(self) -> bool:
        return self._allow_delete

    def authorize(self, target: str | Path, operation: WorkspaceOperation) -> Path:
        path = Path(target)
        if not path.is_absolute():
            raise WorkspaceAccessDenied("authorization requires an absolute workspace path")
        canonical = _canonical(path)
        if operation is WorkspaceOperation.READ:
            allowed = _under_any(canonical, self._readable_roots)
        elif operation is WorkspaceOperation.WRITE:
            allowed = _under_any(canonical, self._writable_roots)
        else:
            allowed = self._allow_delete and _under_any(canonical, self._writable_roots)
        if not allowed:
            raise WorkspaceAccessDenied(f"workspace {operation.value} operation is not allowed")
        return canonical

    def authorize_relative(
        self,
        selected_root: Path,
        relative_path: str,
        operation: WorkspaceOperation,
    ) -> Path:
        """Authorize a normalized logical path without resolving symlinks.

        Secure filesystem backends consume the same ``selected_root`` and
        ``relative_path`` through directory file descriptors, so authorization
        cannot be separated from I/O by a resolve/open race.
        """

        candidate = _logical_candidate(selected_root, relative_path)
        if operation is WorkspaceOperation.READ:
            allowed = _under_any(candidate, self._logical_readable_roots)
        elif operation is WorkspaceOperation.WRITE:
            allowed = _under_any(candidate, self._logical_writable_roots)
        else:
            allowed = self._allow_delete and _under_any(
                candidate, self._logical_writable_roots
            )
        if not allowed:
            raise WorkspaceAccessDenied(f"workspace {operation.value} operation is not allowed")
        return candidate

    @classmethod
    def read_only(cls, paths: WorkspacePaths, *, include_shared: bool = True) -> WorkspaceAccessPolicy:
        roots = [paths.product_root]
        if include_shared:
            roots.append(paths.shared_root)
        return cls(readable_roots=roots)

    @classmethod
    def for_feishu(
        cls,
        paths: WorkspacePaths,
        writable_roots: Iterable[WritableWorkspaceRoot] = (),
        *,
        include_shared_read: bool = True,
    ) -> WorkspaceAccessPolicy:
        """Build Feishu's non-interactive policy: readable, explicit writes, no delete."""

        readable = [paths.product_root]
        if include_shared_read:
            readable.append(paths.shared_root)
        writable: list[Path] = []
        for item in writable_roots:
            selected = paths.product_root if item.space is WorkspaceSpace.PRODUCT else paths.shared_root
            writable.append(resolve_beneath(selected, item.relative_path, require_exists=True))
        return cls(readable_roots=readable, writable_roots=writable, allow_delete=False)


__all__ = [
    "WorkspaceAccessDenied",
    "WorkspaceAccessPolicy",
    "WorkspaceOperation",
    "WorkspaceSpace",
    "WritableWorkspaceRoot",
]
