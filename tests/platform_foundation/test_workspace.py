from __future__ import annotations

import os
from pathlib import Path

import pytest

from suiteharness.workspace import (
    WorkspaceAccessDenied,
    WorkspaceAccessPolicy,
    WorkspaceLayout,
    WorkspaceOperation,
    WorkspacePathError,
    WorkspaceSpace,
    WritableWorkspaceRoot,
)


def test_layout_is_tenant_compatible_and_has_no_generation_directory(tmp_path: Path) -> None:
    layout = WorkspaceLayout(tmp_path / "deployment")
    paths = layout.prepare_product("acme", "sales")

    assert paths.shared_root == tmp_path / "deployment" / "tenants" / "acme" / "shared"
    assert paths.product_root == (
        tmp_path / "deployment" / "tenants" / "acme" / "products" / "sales"
    )
    assert paths.shared_root.is_dir()
    assert paths.product_root.is_dir()
    assert "generation" not in paths.product_root.parts


@pytest.mark.parametrize(
    "candidate",
    [
        "../shared/secret.txt",
        "folder/../../secret",
        "folder/./secret",
        "folder//secret",
        "/etc/passwd",
        r"C:\Windows\win.ini",
    ],
)
def test_resolver_rejects_escape_and_absolute_paths(tmp_path: Path, candidate: str) -> None:
    layout = WorkspaceLayout(tmp_path / "deployment")
    layout.prepare_product("acme", "sales")

    with pytest.raises(WorkspacePathError):
        layout.resolve_product("acme", "sales", candidate)


def test_products_cannot_address_a_sibling_workspace(tmp_path: Path) -> None:
    layout = WorkspaceLayout(tmp_path / "deployment")
    sales = layout.prepare_product("acme", "sales")
    support = layout.prepare_product("acme", "support")
    (support.product_root / "private.txt").write_text("private", encoding="utf-8")

    with pytest.raises(WorkspacePathError):
        layout.resolve_product("acme", "sales", "../support/private.txt")
    assert sales.product_root != support.product_root


def test_resolver_rejects_symlink_that_leaves_product_space(tmp_path: Path) -> None:
    layout = WorkspaceLayout(tmp_path / "deployment")
    paths = layout.prepare_product("acme", "sales")
    outside = tmp_path / "outside"
    outside.mkdir()
    link = paths.product_root / "escape"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("creating symlinks is not permitted on this host")

    with pytest.raises(WorkspacePathError, match="escapes"):
        layout.resolve_product("acme", "sales", "escape/new.txt")


def test_read_only_policy_rejects_write_and_delete(tmp_path: Path) -> None:
    layout = WorkspaceLayout(tmp_path / "deployment")
    paths = layout.prepare_product("acme", "sales")
    document = paths.product_root / "readme.txt"
    document.write_text("hello", encoding="utf-8")
    policy = WorkspaceAccessPolicy.read_only(paths)

    assert policy.authorize(document, WorkspaceOperation.READ) == document.resolve()
    with pytest.raises(WorkspaceAccessDenied):
        policy.authorize(document, WorkspaceOperation.WRITE)
    with pytest.raises(WorkspaceAccessDenied):
        policy.authorize(document, WorkspaceOperation.DELETE)


def test_feishu_policy_writes_only_configured_roots_and_never_deletes(tmp_path: Path) -> None:
    layout = WorkspaceLayout(tmp_path / "deployment")
    paths = layout.prepare_product("acme", "sales")
    exports = paths.product_root / "exports"
    exports.mkdir()
    policy = WorkspaceAccessPolicy.for_feishu(
        paths,
        [WritableWorkspaceRoot(WorkspaceSpace.PRODUCT, "exports")],
    )

    writable_file = exports / "answer.md"
    private_file = paths.product_root / "private.md"
    assert policy.authorize(writable_file, WorkspaceOperation.WRITE) == writable_file.resolve()
    with pytest.raises(WorkspaceAccessDenied):
        policy.authorize(private_file, WorkspaceOperation.WRITE)
    with pytest.raises(WorkspaceAccessDenied):
        policy.authorize(writable_file, WorkspaceOperation.DELETE)


def test_policy_rejects_an_outside_target_even_when_it_does_not_exist(tmp_path: Path) -> None:
    layout = WorkspaceLayout(tmp_path / "deployment")
    paths = layout.prepare_product("acme", "sales")
    policy = WorkspaceAccessPolicy.read_only(paths)

    with pytest.raises(WorkspaceAccessDenied):
        policy.authorize(tmp_path / "outside" / "new.txt", WorkspaceOperation.READ)


def test_dangling_symlink_workspace_component_is_rejected(tmp_path: Path) -> None:
    if os.name == "nt":
        pytest.skip("symlink creation commonly requires elevated Windows privileges")
    root = tmp_path / "deployment"
    product = root / "tenants" / "acme" / "products" / "sales"
    product.parent.mkdir(parents=True)
    product.symlink_to(tmp_path / "missing", target_is_directory=True)

    with pytest.raises(WorkspacePathError):
        WorkspaceLayout(root).prepare_product("acme", "sales")
