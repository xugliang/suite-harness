"""Validate the repository's wheel and sdist using only the standard library."""

from __future__ import annotations

import argparse
import email
import tarfile
import tomllib
import zipfile
from pathlib import Path

PROJECT_NAME = "suite-harness"
IMPORT_PACKAGE = "suiteharness"
REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
EXPECTED_LICENSE = (REPOSITORY_ROOT / "LICENSE").read_bytes()


def _only(directory: Path, pattern: str) -> Path:
    matches = sorted(directory.glob(pattern))
    if len(matches) != 1:
        raise AssertionError(f"expected one {pattern!r} in {directory}, found {matches}")
    return matches[0]


def _check_no_private_artifacts(members: list[str], artifact: Path) -> None:
    forbidden_directories = {
        ".deps",
        ".testdeps",
        ".git",
        ".suiteharness",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        "__pycache__",
        "build",
        "data",
        "dist",
    }
    forbidden_suffixes = (
        ".db",
        ".db-journal",
        ".db-shm",
        ".db-wal",
        ".key",
        ".p12",
        ".pem",
        ".pfx",
        ".pyc",
        ".sqlite",
        ".sqlite-journal",
        ".sqlite-shm",
        ".sqlite-wal",
        ".sqlite3",
        ".sqlite3-journal",
        ".sqlite3-shm",
        ".sqlite3-wal",
    )
    for member in members:
        normalized = member.replace("\\", "/").lower()
        segments = tuple(segment for segment in normalized.split("/") if segment)
        if any(segment in forbidden_directories for segment in segments):
            raise AssertionError(f"private/generated path in {artifact}: {member}")
        if any(segment.startswith(".env") for segment in segments):
            raise AssertionError(f"private/generated path in {artifact}: {member}")
        if segments and segments[-1].endswith(forbidden_suffixes):
            raise AssertionError(f"private/generated file in {artifact}: {member}")


def _check_no_entry_points(entry_points: str | None, artifact: Path) -> None:
    """Reject executable installation hooks from this framework-only package."""

    if entry_points is None:
        return
    if "[suiteharness.products]" in entry_points or "[suiteharness.product_packages]" in entry_points:
        raise AssertionError(f"legacy SuiteHarness product entry points in {artifact}")
    raise AssertionError(f"unexpected entry points in {artifact}")


def _project_metadata() -> dict[str, object]:
    document = tomllib.loads((REPOSITORY_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    project = document["project"]
    if any(key in project for key in ("scripts", "gui-scripts", "entry-points")):
        raise AssertionError("suite-harness must not declare executable entry points")
    return project


def _check_wheel(wheel: Path, project: dict[str, object]) -> None:
    with zipfile.ZipFile(wheel) as archive:
        members = archive.namelist()
        _check_no_private_artifacts(members, wheel)

        marker = f"{IMPORT_PACKAGE}/py.typed"
        if marker not in members:
            raise AssertionError(f"{wheel} does not contain {marker}")

        metadata_name = next(
            (name for name in members if name.endswith(".dist-info/METADATA")), None
        )
        if metadata_name is None:
            raise AssertionError(f"{wheel} has no dist-info/METADATA")
        metadata = email.message_from_bytes(archive.read(metadata_name))
        if metadata["Name"] != project["name"] or metadata["Name"] != PROJECT_NAME:
            raise AssertionError(f"unexpected Name in {wheel}: {metadata['Name']!r}")
        if metadata["Version"] != project["version"]:
            raise AssertionError(f"unexpected Version in {wheel}: {metadata['Version']!r}")
        if project["requires-python"] != ">=3.11":
            raise AssertionError(f"{PROJECT_NAME} must require Python >=3.11")
        if metadata["Requires-Python"] != project["requires-python"]:
            raise AssertionError(
                f"unexpected Requires-Python in {wheel}: {metadata['Requires-Python']!r}"
            )
        classifiers = metadata.get_all("Classifier", [])
        if "Typing :: Typed" not in classifiers:
            raise AssertionError(f"{wheel} does not advertise PEP 561 typing")
        if metadata["License-Expression"] != "MIT":
            raise AssertionError(
                f"unexpected License-Expression in {wheel}: {metadata['License-Expression']!r}"
            )
        license_name = next(
            (name for name in members if name.endswith(".dist-info/licenses/LICENSE")), None
        )
        if license_name is None:
            raise AssertionError(f"{wheel} does not contain its MIT license text")
        if archive.read(license_name) != EXPECTED_LICENSE:
            raise AssertionError(f"{wheel} contains a modified MIT license text")

        entry_point_name = next(
            (name for name in members if name.endswith(".dist-info/entry_points.txt")), None
        )
        entry_points = (
            archive.read(entry_point_name).decode("utf-8")
            if entry_point_name is not None
            else None
        )
        _check_no_entry_points(entry_points, wheel)


def _check_sdist(sdist: Path) -> None:
    with tarfile.open(sdist, "r:gz") as archive:
        members = archive.getnames()
        _check_no_private_artifacts(members, sdist)
        marker = f"/src/{IMPORT_PACKAGE}/py.typed"
        if not any(name.endswith(marker) for name in members):
            raise AssertionError(f"{sdist} does not contain {marker.lstrip('/')}")
        if not any(name.endswith("/pyproject.toml") for name in members):
            raise AssertionError(f"{sdist} does not contain pyproject.toml")
        if not any(name.endswith("/README.md") for name in members):
            raise AssertionError(f"{sdist} does not contain README.md")
        license_name = next(
            (
                name
                for name in members
                if name.endswith("/LICENSE") and name.count("/") == 1
            ),
            None,
        )
        if license_name is None:
            raise AssertionError(f"{sdist} does not contain its MIT license text")
        extracted = archive.extractfile(license_name)
        if extracted is None or extracted.read() != EXPECTED_LICENSE:
            raise AssertionError(f"{sdist} contains a modified MIT license text")


def check_all(dist_root: Path) -> None:
    if not dist_root.is_dir():
        raise AssertionError(f"distribution directory does not exist: {dist_root}")
    project = _project_metadata()
    wheel = _only(dist_root, "*.whl")
    sdist = _only(dist_root, "*.tar.gz")
    unexpected = sorted(
        path.name for path in dist_root.iterdir() if path not in {wheel, sdist}
    )
    if unexpected:
        raise AssertionError(f"unexpected distribution artifacts in {dist_root}: {unexpected}")
    _check_wheel(wheel, project)
    _check_sdist(sdist)
    print(f"validated {PROJECT_NAME}: {wheel.name}, {sdist.name}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("dist_root", type=Path, help="directory containing wheel and sdist")
    args = parser.parse_args()
    check_all(args.dist_root.resolve())


if __name__ == "__main__":
    main()
