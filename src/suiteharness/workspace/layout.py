"""Tenant-compatible, product-isolated server workspace layout."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

_IDENTIFIER = re.compile(r"^[a-z][a-z0-9]*(?:[._-][a-z0-9]+)*$")


class WorkspaceError(ValueError):
    """Base error for an invalid or unsafe workspace operation."""


class WorkspacePathError(WorkspaceError):
    """A path was malformed or escaped its selected workspace space."""


def _validate_identifier(value: str, label: str) -> str:
    if not _IDENTIFIER.fullmatch(value):
        raise WorkspacePathError(f"invalid {label}: {value!r}")
    return value


def _segments(relative_path: str | Path) -> tuple[str, ...]:
    raw = str(relative_path)
    if not raw or "\x00" in raw:
        raise WorkspacePathError("relative path must not be empty or contain NUL")
    portable = raw.replace("\\", "/")
    if portable == ".":
        return ()
    if portable.startswith("/") or portable.startswith("//"):
        raise WorkspacePathError("absolute and UNC paths are not allowed")
    if re.match(r"^[A-Za-z]:", portable):
        raise WorkspacePathError("drive-prefixed paths are not allowed")
    raw_parts = portable.split("/")
    if any(part in {"", ".", ".."} for part in raw_parts):
        raise WorkspacePathError("path must be normalized and must not contain '..'")
    parts = PurePosixPath(portable).parts
    if tuple(raw_parts) != parts:
        raise WorkspacePathError("path must be normalized")
    return parts


def _is_within(path: Path, root: Path) -> bool:
    return path == root or path.is_relative_to(root)


def resolve_beneath(
    root: Path,
    relative_path: str | Path,
    *,
    require_exists: bool = False,
) -> Path:
    """Resolve a portable relative path without crossing ``root``.

    Existing symlink components are resolved. A symlink that points outside the
    selected space is therefore rejected, including when the final leaf does
    not exist yet.
    """

    if not root.exists() or not root.is_dir():
        raise WorkspacePathError(f"workspace root is not an existing directory: {root}")
    root_real = root.resolve(strict=True)
    candidate = root.joinpath(*_segments(relative_path))
    try:
        resolved = candidate.resolve(strict=require_exists)
    except (FileNotFoundError, OSError) as exc:
        raise WorkspacePathError(f"workspace path does not exist: {relative_path}") from exc
    if not _is_within(resolved, root_real):
        raise WorkspacePathError("workspace path escapes its selected space")
    return resolved


@dataclass(frozen=True)
class WorkspacePaths:
    deployment_root: Path
    tenant_root: Path
    shared_root: Path
    products_root: Path
    product_root: Path


class WorkspaceLayout:
    """Compute and prepare ``root/tenants/<tenant>/{shared,products/<product>}``."""

    def __init__(self, deployment_root: str | Path) -> None:
        root = Path(deployment_root)
        if not root.is_absolute():
            raise WorkspacePathError("deployment workspace root must be absolute")
        if root.exists() and root.is_symlink():
            raise WorkspacePathError("deployment workspace root must not be a symbolic link")
        self._root = root

    @property
    def deployment_root(self) -> Path:
        return self._root

    def paths_for(self, tenant_id: str, product_id: str) -> WorkspacePaths:
        tenant = _validate_identifier(tenant_id, "tenant_id")
        product = _validate_identifier(product_id, "product_id")
        tenant_root = self._root / "tenants" / tenant
        products_root = tenant_root / "products"
        return WorkspacePaths(
            deployment_root=self._root,
            tenant_root=tenant_root,
            shared_root=tenant_root / "shared",
            products_root=products_root,
            product_root=products_root / product,
        )

    def prepare_product(self, tenant_id: str, product_id: str) -> WorkspacePaths:
        """Create exactly the fixed directories for one configured product."""

        paths = self.paths_for(tenant_id, product_id)
        for directory in (
            paths.deployment_root,
            paths.tenant_root,
            paths.shared_root,
            paths.products_root,
            paths.product_root,
        ):
            if directory.is_symlink() or (directory.exists() and not directory.is_dir()):
                raise WorkspacePathError(f"workspace component is not a real directory: {directory}")
            directory.mkdir(parents=True, exist_ok=True)
        root_real = paths.deployment_root.resolve(strict=True)
        for directory in (
            paths.tenant_root,
            paths.shared_root,
            paths.products_root,
            paths.product_root,
        ):
            if not _is_within(directory.resolve(strict=True), root_real):
                raise WorkspacePathError("workspace component escapes the deployment root")
        return paths

    def resolve_product(
        self,
        tenant_id: str,
        product_id: str,
        relative_path: str | Path,
        *,
        require_exists: bool = False,
    ) -> Path:
        paths = self.paths_for(tenant_id, product_id)
        return resolve_beneath(
            paths.product_root,
            relative_path,
            require_exists=require_exists,
        )

    def resolve_shared(
        self,
        tenant_id: str,
        relative_path: str | Path,
        *,
        require_exists: bool = False,
    ) -> Path:
        tenant = _validate_identifier(tenant_id, "tenant_id")
        shared_root = self._root / "tenants" / tenant / "shared"
        return resolve_beneath(shared_root, relative_path, require_exists=require_exists)


__all__ = [
    "WorkspaceError",
    "WorkspaceLayout",
    "WorkspacePathError",
    "WorkspacePaths",
    "resolve_beneath",
]
