"""Bounded, non-interpolating YAML configuration loader."""

from __future__ import annotations

import json
import os
import stat
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from .models import SuiteHarnessConfig, SuiteHarnessSecrets, validate_secret_references

YamlDecoder = Callable[[str], object]
_YAML_SUFFIXES = {".yaml", ".yml"}


class ConfigLoadError(ValueError):
    """A safe configuration error that never contains secret input values."""


def _validation_summary(error: ValidationError) -> str:
    items: list[str] = []
    for entry in error.errors(include_url=False, include_context=False, include_input=False):
        location = ".".join(str(part) for part in entry["loc"])
        items.append(f"{location}: {entry['msg']}")
    return "; ".join(items)


@dataclass(frozen=True)
class LoadedSuiteHarnessConfig:
    """Validated public settings plus separately held credentials."""

    config: SuiteHarnessConfig
    secrets: SuiteHarnessSecrets = field(repr=False)
    config_path: Path
    secrets_path: Path = field(repr=False)


def _default_yaml_decoder(text: str) -> object:
    """Decode YAML with PyYAML when present, with a dependency-free JSON fallback.

    JSON is a strict subset of YAML. The fallback therefore keeps bootstrap and
    diagnostics usable in a clean source tree while returning an explicit error
    for YAML syntax when the deployment omitted its YAML runtime.
    """

    try:
        import yaml
    except ModuleNotFoundError:
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            raise ConfigLoadError(
                "a YAML decoder is unavailable; install PyYAML or use JSON syntax in the .yaml file"
            ) from exc
    return yaml.safe_load(text)


def _read_document(
    path: Path,
    *,
    label: str,
    maximum_bytes: int,
    decoder: YamlDecoder,
    check_private_permissions: bool,
) -> Mapping[str, Any]:
    if path.suffix.lower() not in _YAML_SUFFIXES:
        raise ConfigLoadError(f"{label} must use a .yaml or .yml file")
    if path.is_symlink():
        raise ConfigLoadError(f"{label} must not be a symbolic link")
    if not path.is_file():
        raise ConfigLoadError(f"{label} is not a regular file: {path}")
    if check_private_permissions and os.name != "nt":
        permissions = stat.S_IMODE(path.stat().st_mode)
        if permissions & 0o077:
            raise ConfigLoadError(f"{label} must not be accessible by group or other users")
    size = path.stat().st_size
    if size > maximum_bytes:
        raise ConfigLoadError(f"{label} exceeds the {maximum_bytes}-byte limit")
    try:
        text = path.read_text(encoding="utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ConfigLoadError(f"{label} must be UTF-8 encoded") from exc
    try:
        decoded = decoder(text)
    except ConfigLoadError:
        raise
    except Exception as exc:
        # Decoder exceptions can echo the source line. This generic message is
        # deliberately used for both files to prevent a secret from leaking.
        raise ConfigLoadError(f"{label} is not valid YAML") from exc
    if not isinstance(decoded, Mapping):
        raise ConfigLoadError(f"{label} document root must be a mapping")
    return decoded


class SuiteHarnessConfigLoader:
    """Load two explicit files without consulting or expanding environment variables."""

    def __init__(
        self,
        *,
        maximum_bytes: int = 1_048_576,
        yaml_decoder: YamlDecoder | None = None,
    ) -> None:
        if maximum_bytes < 1_024:
            raise ValueError("maximum_bytes must be at least 1024")
        self._maximum_bytes = maximum_bytes
        self._decoder = yaml_decoder or _default_yaml_decoder

    def load(self, config_path: str | Path, secrets_path: str | Path) -> LoadedSuiteHarnessConfig:
        public_path = Path(config_path).absolute()
        private_path = Path(secrets_path).absolute()
        if public_path == private_path:
            raise ConfigLoadError("config and secrets must be separate files")
        try:
            if public_path.exists() and private_path.exists() and public_path.samefile(private_path):
                raise ConfigLoadError("config and secrets must be separate files")
        except OSError as exc:
            raise ConfigLoadError("config file identity could not be verified") from exc

        public_raw = _read_document(
            public_path,
            label="configuration file",
            maximum_bytes=self._maximum_bytes,
            decoder=self._decoder,
            check_private_permissions=False,
        )
        private_raw = _read_document(
            private_path,
            label="secrets file",
            maximum_bytes=self._maximum_bytes,
            decoder=self._decoder,
            check_private_permissions=True,
        )
        try:
            config = SuiteHarnessConfig.model_validate(public_raw)
        except ValidationError as exc:
            raise ConfigLoadError(
                f"configuration validation failed: {_validation_summary(exc)}"
            ) from exc
        try:
            secrets = SuiteHarnessSecrets.model_validate(private_raw)
        except ValidationError as exc:
            # Do not interpolate the ValidationError: its input field can hold
            # a raw credential even though SecretStr would later redact it.
            raise ConfigLoadError("secrets file validation failed") from exc
        try:
            validate_secret_references(config, secrets)
        except ValueError as exc:
            raise ConfigLoadError(str(exc)) from exc
        return LoadedSuiteHarnessConfig(
            config=config,
            secrets=secrets,
            config_path=public_path,
            secrets_path=private_path,
        )


__all__ = ["ConfigLoadError", "SuiteHarnessConfigLoader", "LoadedSuiteHarnessConfig", "YamlDecoder"]
