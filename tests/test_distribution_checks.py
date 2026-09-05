from pathlib import Path

import pytest

from scripts.check_distributions import (
    _check_no_entry_points,
    _check_no_private_artifacts,
    _only,
    check_all,
)


@pytest.mark.parametrize(
    "member",
    [
        "suiteharness/data/customer.json",
        "suiteharness/.suiteharness/state.json",
        "suiteharness/.env.local",
        "suiteharness/.pytest_cache/state",
        "suiteharness/.testdeps/package.py",
        "suiteharness/__pycache__/module.pyc",
        "suiteharness/runtime.db-wal",
        "suiteharness/runtime.sqlite3-shm",
        "suiteharness/private.pem",
    ],
)
def test_distribution_check_rejects_private_and_generated_members(member: str) -> None:
    with pytest.raises(AssertionError, match="private/generated"):
        _check_no_private_artifacts([member], Path("suiteharness.whl"))


def test_distribution_check_allows_normal_package_members() -> None:
    _check_no_private_artifacts(
        [
            "suiteharness/runtime/kernel.py",
            "suiteharness-0.1.0.dist-info/METADATA",
            "suiteharness-0.1.0.dist-info/licenses/LICENSE",
        ],
        Path("suiteharness.whl"),
    )


@pytest.mark.parametrize(
    ("entry_points", "message"),
    [
        ("", "unexpected entry points"),
        ("[console_scripts]\nsuiteharness = package:main\n", "unexpected entry points"),
        (
            "[suiteharness.product_packages]\nlegacy = product.plugin:LegacyProduct\n",
            "legacy SuiteHarness product entry points",
        ),
    ],
)
def test_distribution_check_rejects_every_entry_point(
    entry_points: str,
    message: str,
) -> None:
    with pytest.raises(AssertionError, match=message):
        _check_no_entry_points(entry_points, Path("suiteharness.whl"))


def test_distribution_check_accepts_absent_entry_points() -> None:
    _check_no_entry_points(None, Path("suiteharness.whl"))


def test_distribution_root_must_contain_exactly_one_artifact_of_each_kind(
    tmp_path: Path,
) -> None:
    wheel = tmp_path / "suiteharness-0.1.0-py3-none-any.whl"
    wheel.touch()
    assert _only(tmp_path, "*.whl") == wheel

    (tmp_path / "unexpected-0.1.0-py3-none-any.whl").touch()
    with pytest.raises(AssertionError, match="expected one"):
        _only(tmp_path, "*.whl")


def test_distribution_root_rejects_unexpected_artifacts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    wheel = tmp_path / "suiteharness-0.1.0-py3-none-any.whl"
    sdist = tmp_path / "suiteharness-0.1.0.tar.gz"
    wheel.touch()
    sdist.touch()
    (tmp_path / "checksums.txt").touch()
    monkeypatch.setattr(
        "scripts.check_distributions._project_metadata",
        lambda: {},
    )

    with pytest.raises(AssertionError, match="unexpected distribution artifacts"):
        check_all(tmp_path)
