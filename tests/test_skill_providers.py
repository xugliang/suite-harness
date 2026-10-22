"""Local and MCP skill contracts, without network or external model calls."""

from __future__ import annotations

import asyncio
import hashlib
import os
from pathlib import Path

import pytest

from suiteharness.runtime.scopes import RequestScope, ScopePath
from suiteharness.skills import (
    EmptySkillProvider,
    FilesystemSkillProvider,
    SkillAccess,
    SkillError,
    SkillLimits,
)


def access(*, product: str = "demo", source: str = "model") -> SkillAccess:
    return SkillAccess(
        RequestScope(ScopePath.product("company", product), "employee"), source=source
    )


def write_skill(root: Path, name: str, body: str, *, includes: str = "", extra: str = "") -> Path:
    path = root / name / "SKILL.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"---\nname: {name}\ndescription: Use {name}.\nversion: 1.0.0\n"
        f"{includes}{extra}---\n\n{body}\n",
        encoding="utf-8",
    )
    return path


def test_empty_provider_is_optional() -> None:
    provider = EmptySkillProvider()
    assert asyncio.run(provider.list_skills(access())) == ()
    with pytest.raises(SkillError, match="not available") as caught:
        asyncio.run(provider.read_skill(access(), "demo.missing"))
    assert caught.value.code == "skill_not_available"


def test_filesystem_expands_dependencies_and_keeps_reply_separate(tmp_path: Path) -> None:
    write_skill(tmp_path, "common", "Shared rules.")
    target = write_skill(tmp_path, "work", "Work steps.", includes="includes: [common]\n")
    target.with_name("REPLY.md").write_text("Only this result.", encoding="utf-8")
    provider = FilesystemSkillProvider(tmp_path, namespace="demo")
    document = asyncio.run(provider.read_skill(access(), "demo.work"))
    assert "Shared rules." in document.instructions
    assert "Work steps." in document.instructions
    assert document.dependencies == ("demo.common",)
    assert document.instructions_sha256 == hashlib.sha256(document.instructions.encode()).hexdigest()
    assert provider.compile_resource("demo.work", resource_id="reply").instructions == "Only this result."
    with pytest.raises(SkillError):
        asyncio.run(provider.read_skill(access(), "demo.work", resource_id="reply"))
    assert asyncio.run(provider.read_skill(access(source="workflow"), "demo.work", resource_id="reply"))


def test_model_visibility_and_dependency_policy_are_checked(tmp_path: Path) -> None:
    write_skill(tmp_path, "common", "Shared rules.", extra="disable-model-invocation: true\n")
    write_skill(tmp_path, "work", "Work steps.", includes="includes: [common]\n")
    provider = FilesystemSkillProvider(tmp_path, namespace="demo")
    assert [d.name for d in asyncio.run(provider.list_skills(access()))] == ["work"]
    # A non-discoverable common dependency remains loadable internally as part
    # of the visible instruction, but an explicit policy denial cannot be bypassed.
    assert asyncio.run(provider.read_skill(access(), "demo.work"))
    denied = FilesystemSkillProvider(
        tmp_path, namespace="demo",
        access_policy=lambda _a, d, _r: d.name != "common",
    )
    with pytest.raises(SkillError) as caught:
        asyncio.run(denied.read_skill(access(), "demo.work"))
    assert caught.value.code == "skill_not_available"
    with pytest.raises(SkillError):
        asyncio.run(provider.read_skill(access(product="other"), "demo.work"))


@pytest.mark.parametrize("extra", ["unknown: value\n", "name: duplicate\n", "includes: &x [*x]\n"])
def test_invalid_metadata_is_not_silently_accepted(tmp_path: Path, extra: str) -> None:
    write_skill(tmp_path, "work", "Work steps.", extra=extra)
    with pytest.raises(SkillError) as caught:
        FilesystemSkillProvider(tmp_path, namespace="demo")
    assert caught.value.code == "skill_invalid"


def test_changed_file_and_oversize_fail_closed(tmp_path: Path) -> None:
    target = write_skill(tmp_path, "work", "Work steps.")
    provider = FilesystemSkillProvider(tmp_path, namespace="demo")
    target.write_text(target.read_text(encoding="utf-8") + "Changed", encoding="utf-8")
    with pytest.raises(SkillError) as caught:
        asyncio.run(provider.read_skill(access(), "demo.work"))
    assert caught.value.code == "skill_content_changed"
    with pytest.raises(SkillError):
        FilesystemSkillProvider(tmp_path, namespace="demo", limits=SkillLimits(max_instruction_bytes=5))


def test_cycle_and_unsafe_inputs_fail(tmp_path: Path) -> None:
    write_skill(tmp_path, "one", "One", includes="includes: [two]\n")
    write_skill(tmp_path, "two", "Two", includes="includes: [one]\n")
    with pytest.raises(SkillError):
        FilesystemSkillProvider(tmp_path, namespace="demo")
    other = tmp_path / "clean"
    write_skill(other, "work", "Work")
    provider = FilesystemSkillProvider(other, namespace="demo")
    for name in ("../work", "/etc/passwd", "other.work", "https://example.org"):
        with pytest.raises(SkillError):
            asyncio.run(provider.read_skill(access(), name))


def test_static_manifest_does_not_depend_on_a_fake_employee(tmp_path: Path) -> None:
    write_skill(tmp_path, "admin", "Admin", extra="disable-model-invocation: true\n")
    provider = FilesystemSkillProvider(tmp_path, namespace="demo", access_policy=lambda *_: False)
    assert [d.skill_id for d in provider.static_manifest()] == ["demo.admin"]
    assert asyncio.run(provider.list_skills(access())) == ()


def test_dependency_diamond_has_unique_content(tmp_path: Path) -> None:
    write_skill(tmp_path, "common", "Shared uniquely.")
    write_skill(tmp_path, "left", "Left", includes="includes: [common]\n")
    write_skill(tmp_path, "right", "Right", includes="includes: [common]\n")
    write_skill(tmp_path, "work", "Work", includes="includes: [left, right]\n")
    provider = FilesystemSkillProvider(tmp_path, namespace="demo")
    document = provider.compile_resource("demo.work")
    assert document.instructions.count("Shared uniquely.") == 1
    assert document.dependencies == ("demo.common", "demo.left", "demo.right")


def test_long_dependency_route_is_not_hidden_by_a_shorter_route(tmp_path: Path) -> None:
    write_skill(tmp_path, "leaf", "Leaf")
    write_skill(tmp_path, "common", "Common", includes="includes: [leaf]\n")
    write_skill(tmp_path, "long", "Long", includes="includes: [common]\n")
    write_skill(tmp_path, "work", "Work", includes="includes: [common, long]\n")
    with pytest.raises(SkillError) as caught:
        FilesystemSkillProvider(tmp_path, namespace="demo", limits=SkillLimits(max_dependency_depth=3))
    assert caught.value.code == "skill_budget_exceeded"


@pytest.mark.skipif(os.name != "posix", reason="Production Linux descriptor-based path checks")
def test_symlinks_and_nonregular_files_are_rejected(tmp_path: Path) -> None:
    outside = tmp_path / "outside.md"
    outside.write_text("not a skill", encoding="utf-8")
    root = tmp_path / "package"
    root.mkdir()
    (root / "SKILL.md").symlink_to(outside)
    with pytest.raises(SkillError):
        FilesystemSkillProvider(root, namespace="demo")
    (root / "SKILL.md").unlink()
    os.mkfifo(root / "SKILL.md")
    with pytest.raises(SkillError):
        FilesystemSkillProvider(root, namespace="demo")


@pytest.mark.skipif(os.name != "posix", reason="Production Linux root inode pinning")
def test_replaced_registered_root_is_not_accepted(tmp_path: Path) -> None:
    root = tmp_path / "package"
    write_skill(root, "work", "Work")
    provider = FilesystemSkillProvider(root, namespace="demo")
    root.rename(tmp_path / "previous")
    write_skill(root, "work", "Work")
    with pytest.raises(SkillError) as caught:
        provider.compile_resource("demo.work")
    assert caught.value.code == "skill_content_changed"
