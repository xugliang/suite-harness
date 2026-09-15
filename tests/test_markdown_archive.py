"""Portable archives must never read or write outside the exact supplied identity."""

from pathlib import Path

import pytest

from suiteharness.sessions.models import SessionIdentity


IDENTITY = SessionIdentity(tenant_id="tenant", product_id="product", agent_id="agent",
                           session_id="session", principal_id="sales")


def archive(root, **options):
    from suiteharness.memory.markdown_archive import MarkdownArchive
    return MarkdownArchive(root, **options)


def test_enabled_archive_is_utf8_markdown_reloadable_searchable_and_replaceable(tmp_path):
    root = tmp_path / "archives"
    archive(root, long_term_enabled=True).store(IDENTITY, "# 客户\n\n喜欢茶。\n\n下周联系。")
    files = list(root.glob("*.md"))
    assert len(files) == 1
    assert files[0].read_text(encoding="utf-8") == "# 客户\n\n喜欢茶。\n\n下周联系。"
    fresh = archive(root, long_term_enabled=True)
    assert "喜欢茶" in fresh.retrieve(IDENTITY, "茶")
    assert fresh.retrieve(IDENTITY, "不存在") == ""
    fresh.store(IDENTITY, "# Updated\n\nnew fact")
    assert fresh.retrieve(IDENTITY) == "# Updated\n\nnew fact"
    assert list(root.glob("*.tmp")) == []


def test_disabled_does_no_filesystem_io_even_with_existing_archives(tmp_path, monkeypatch):
    root = tmp_path / "archives"
    archive(root, long_term_enabled=True).store(IDENTITY, "stored")

    def forbidden(*args, **kwargs):
        raise AssertionError("disabled archive attempted filesystem I/O")

    for method in ("stat", "lstat", "open", "mkdir", "resolve", "unlink"):
        monkeypatch.setattr(Path, method, forbidden)
    disabled = archive(root)
    disabled.store(IDENTITY, "ignored")
    assert disabled.retrieve(IDENTITY) == ""
    assert disabled.delete(IDENTITY) is False


def test_disabled_never_creates_root_or_deletes_existing_data(tmp_path):
    missing = tmp_path / "missing"
    archive(missing).store(IDENTITY, "ignored")
    assert not missing.exists()
    enabled = archive(tmp_path / "existing", long_term_enabled=True)
    enabled.store(IDENTITY, "keep")
    assert not archive(tmp_path / "existing").delete(IDENTITY)
    assert enabled.retrieve(IDENTITY) == "keep"


@pytest.mark.parametrize("dimension", ["tenant_id", "product_id", "agent_id", "session_id", "principal_id"])
def test_archive_scope_isolates_every_identity_dimension(tmp_path, dimension):
    store = archive(tmp_path, long_term_enabled=True)
    store.store(IDENTITY, "private")
    other = IDENTITY.model_copy(update={dimension: "different"})
    assert store.retrieve(other) == ""
    store.store(other, "other")
    assert store.retrieve(IDENTITY) == "private"
    assert store.retrieve(other) == "other"


def test_separator_like_ids_do_not_collide_or_escape(tmp_path):
    store = archive(tmp_path, long_term_enabled=True)
    first = IDENTITY.model_copy(update={"tenant_id": "a:b", "product_id": "c"})
    second = IDENTITY.model_copy(update={"tenant_id": "a", "product_id": "b:c"})
    store.store(first, "one")
    store.store(second, "two")
    assert store.retrieve(first) == "one"
    assert store.retrieve(second) == "two"
    assert len(list(tmp_path.glob("*.md"))) == 2
    invalid = IDENTITY.model_copy(update={"session_id": "../../escape"})
    with pytest.raises(ValueError):
        store.store(invalid, "bad")


def test_archive_enforces_byte_limit_and_output_char_limit(tmp_path):
    store = archive(tmp_path, long_term_enabled=True, max_file_bytes=128)
    store.store(IDENTITY, "中文" * 20)
    assert store.retrieve(IDENTITY, max_chars=3) == "中文中"
    with pytest.raises(ValueError, match="size|bytes|limit"):
        store.store(IDENTITY, "中文" * 100)
    assert store.retrieve(IDENTITY) == "中文" * 20
    path = next(tmp_path.glob("*.md"))
    path.write_text("z" * 129, encoding="utf-8")
    with pytest.raises(ValueError, match="size|bytes|limit"):
        store.retrieve(IDENTITY)


def test_explicit_delete_only_removes_requested_archive(tmp_path):
    store = archive(tmp_path, long_term_enabled=True)
    other = IDENTITY.model_copy(update={"principal_id": "other"})
    store.store(IDENTITY, "remove")
    store.store(other, "keep")
    assert store.delete(IDENTITY)
    assert not store.delete(IDENTITY)
    assert store.retrieve(IDENTITY) == ""
    assert store.retrieve(other) == "keep"


def symlink_or_skip(link, target, *, directory=False):
    try:
        link.symlink_to(target, target_is_directory=directory)
    except OSError as exc:
        pytest.skip(f"platform does not permit test symlinks: {exc}")


@pytest.mark.parametrize("operation", ["retrieve", "store", "delete"])
def test_rejects_leaf_symlink(tmp_path, operation):
    root = tmp_path / "archives"
    store = archive(root, long_term_enabled=True)
    store.store(IDENTITY, "old")
    leaf = next(root.glob("*.md"))
    leaf.unlink()
    outside = tmp_path / "outside.md"
    outside.write_text("untouched", encoding="utf-8")
    symlink_or_skip(leaf, outside)
    with pytest.raises(ValueError, match="link|regular|root"):
        getattr(store, operation)(IDENTITY, "replacement") if operation == "store" else getattr(store, operation)(IDENTITY)
    assert outside.read_text(encoding="utf-8") == "untouched"


def test_rejects_symlink_root_and_parent(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    link = tmp_path / "linked"
    symlink_or_skip(link, outside, directory=True)
    with pytest.raises(ValueError, match="link|root"):
        archive(link / "nested", long_term_enabled=True).store(IDENTITY, "bad")
    assert list(outside.iterdir()) == []
