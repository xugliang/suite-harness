"""Bounded, non-interpolating YAML configuration loader."""

from __future__ import annotations

import json
import os
import stat
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

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


@dataclass(frozen=True)
class _DecodedDocument:
    value: Mapping[str, Any]
    identity: tuple[int, int]


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
) -> _DecodedDocument:
    if path.suffix.lower() not in _YAML_SUFFIXES:
        raise ConfigLoadError(f"{label} must use a .yaml or .yml file")

    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_BINARY", 0)
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    try:
        before = path.lstat()
        if stat.S_ISLNK(before.st_mode):
            raise ConfigLoadError(f"{label} must not be a symbolic link")
        descriptor = os.open(path, flags | nofollow)
    except ConfigLoadError:
        raise
    except OSError as exc:
        raise ConfigLoadError(f"{label} could not be opened safely") from exc

    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode):
            raise ConfigLoadError(f"{label} is not a regular file")
        # Windows does not expose O_NOFOLLOW.  Comparing the opened object to
        # lstat closes the check/open gap there; Linux additionally rejects a
        # link atomically in os.open.
        if nofollow == 0:
            try:
                after = path.lstat()
            except OSError as exc:
                raise ConfigLoadError(f"{label} identity changed while opening") from exc
            if stat.S_ISLNK(after.st_mode) or (after.st_dev, after.st_ino) != (
                opened.st_dev,
                opened.st_ino,
            ):
                raise ConfigLoadError(f"{label} identity changed while opening")
        if check_private_permissions and os.name != "nt":
            permissions = stat.S_IMODE(opened.st_mode)
            if permissions & 0o077:
                raise ConfigLoadError(
                    f"{label} must not be accessible by group or other users"
                )
        if opened.st_size > maximum_bytes:
            raise ConfigLoadError(f"{label} exceeds the {maximum_bytes}-byte limit")
        with os.fdopen(descriptor, "rb", closefd=True) as source:
            descriptor = -1
            payload = source.read(maximum_bytes + 1)
            after_read = os.fstat(source.fileno())
        if (
            (after_read.st_dev, after_read.st_ino) != (opened.st_dev, opened.st_ino)
            or after_read.st_mode != opened.st_mode
            or after_read.st_size != opened.st_size
            or after_read.st_mtime_ns != opened.st_mtime_ns
            or after_read.st_ctime_ns != opened.st_ctime_ns
            or len(payload) != opened.st_size
        ):
            raise ConfigLoadError(f"{label} changed while being read")
        if len(payload) > maximum_bytes:
            raise ConfigLoadError(f"{label} exceeds the {maximum_bytes}-byte limit")
        text = payload.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ConfigLoadError(f"{label} must be UTF-8 encoded") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
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
    return _DecodedDocument(
        value=decoded,
        identity=(opened.st_dev, opened.st_ino),
    )


def _is_placeholder(value: str) -> bool:
    normalized = value.strip().casefold()
    return normalized in {"replace", "secret", "token", "password"} or any(
        marker in normalized
        for marker in (
            "replace-with",
            ":replace@",
            "change-me",
            "changeme",
            "your-api-key",
            "your-secret",
            "placeholder",
        )
    )


def _is_reserved_url(value: str) -> bool:
    parsed = urlsplit(value if "://" in value else f"//{value}")
    hostname = (parsed.hostname or "").rstrip(".").casefold()
    return hostname in {"example", "example.com", "example.org", "example.net"} or any(
        hostname.endswith(suffix)
        for suffix in (".example", ".example.com", ".example.org", ".example.net", ".invalid")
    )


def _iter_secret_values(secrets: SuiteHarnessSecrets) -> list[str]:
    values: list[str] = []
    for credential in secrets.web.values():
        values.append(credential.session_signing_key.get_secret_value())
    for credential in secrets.feishu.values():
        values.append(credential.app_secret.get_secret_value())
        for optional in (credential.verification_token, credential.encrypt_key):
            if optional is not None:
                values.append(optional.get_secret_value())
    for credential in secrets.model_providers.values():
        for optional in (
            credential.api_key,
            credential.access_key_id,
            credential.secret_access_key,
            credential.session_token,
            credential.service_account_json,
            credential.endpoint_credential,
        ):
            if optional is not None:
                values.append(optional.get_secret_value())
    for credential in secrets.services.values():
        for optional in (
            credential.token,
            credential.api_key,
            credential.client_id,
            credential.client_secret,
        ):
            if optional is not None:
                values.append(optional.get_secret_value())
        values.extend(value.get_secret_value() for value in credential.headers.values())
    return values


def _validate_production_placeholders(
    config: SuiteHarnessConfig,
    secrets: SuiteHarnessSecrets,
) -> None:
    if config.deployment.environment != "production":
        return

    public_values = [profile.model for profile in config.models.profiles]
    if config.channels.feishu.enabled:
        public_values.append(config.channels.feishu.app_id)
    if any(_is_placeholder(value) for value in public_values):
        raise ConfigLoadError("production configuration contains a placeholder value")
    secret_values = _iter_secret_values(secrets)
    if any(_is_placeholder(value) for value in secret_values):
        raise ConfigLoadError("production secrets contain a placeholder value")
    if any(
        len(value.strip()) >= 16 and len(set(value.strip().rstrip("="))) <= 1
        for value in secret_values
    ):
        raise ConfigLoadError("production secrets contain an obvious dummy value")

    image = config.sandbox.image
    if "@sha256:" in image and image.rsplit("@sha256:", 1)[1] == "0" * 64:
        raise ConfigLoadError("production sandbox image uses the all-zero example digest")
    endpoint_urls = [
        profile.base_url for profile in config.models.profiles if profile.base_url is not None
    ]
    endpoint_urls.extend(config.channels.web.allowed_origins)
    if _is_reserved_url(image.split("@", 1)[0]) or any(
        _is_reserved_url(value) for value in endpoint_urls
    ):
        raise ConfigLoadError("production configuration uses a reserved example hostname")


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

        public_document = _read_document(
            public_path,
            label="configuration file",
            maximum_bytes=self._maximum_bytes,
            decoder=self._decoder,
            check_private_permissions=False,
        )
        private_document = _read_document(
            private_path,
            label="secrets file",
            maximum_bytes=self._maximum_bytes,
            decoder=self._decoder,
            check_private_permissions=True,
        )
        if public_document.identity == private_document.identity:
            raise ConfigLoadError("config and secrets must be separate files")
        public_raw = public_document.value
        private_raw = private_document.value
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
            # Search credentials are selected from trusted runtime grants, which
            # are deliberately not part of the public YAML.  Keep validating
            # every supplied secret's structure here and defer only the
            # reachability-dependent existence check to server startup.
            validate_secret_references(config, secrets, search_provider_ids=())
        except ValueError as exc:
            raise ConfigLoadError(str(exc)) from exc
        _validate_production_placeholders(config, secrets)
        return LoadedSuiteHarnessConfig(
            config=config,
            secrets=secrets,
            config_path=public_path,
            secrets_path=private_path,
        )


__all__ = ["ConfigLoadError", "SuiteHarnessConfigLoader", "LoadedSuiteHarnessConfig", "YamlDecoder"]
