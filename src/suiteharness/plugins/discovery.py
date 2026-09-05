"""Side-effect-free discovery and supply-chain verification for source plugins."""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Any, Protocol

from jsonschema import Draft202012Validator, SchemaError, ValidationError
from pydantic import ValidationError as PydanticValidationError

from suiteharness.plugins.errors import (
    PluginDiscoveryError,
    PluginErrorCode,
)
from suiteharness.plugins.models import (
    PluginManifest,
    PluginSourceDeclaration,
    SignatureMetadata,
    TrustMode,
)
from suiteharness.security import UnsafeJsonSchemaError, harden_untrusted_json_schema

DEFAULT_MANIFEST_NAME = "suiteharness-plugin.json"


class SignatureVerifier(Protocol):
    """Cryptographic verification seam backed by deployment key management."""

    def verify(self, payload: bytes, signature: SignatureMetadata) -> bool: ...


class TrustPolicy(Protocol):
    """Deployment policy evaluated after digest and signature checks."""

    def allows(
        self,
        *,
        manifest: PluginManifest,
        source_path: Path,
        digest: str,
        signature_verified: bool,
    ) -> bool: ...


@dataclass(frozen=True, slots=True)
class AllowlistedTrustPolicy:
    """Static server policy: exact artifact digests and explicit in-process IDs."""

    allowed_digests: Mapping[str, frozenset[str]]
    trusted_in_process_plugins: frozenset[str] = frozenset()
    require_signatures: bool = False

    def __post_init__(self) -> None:
        copied = {
            plugin_id: frozenset(digests)
            for plugin_id, digests in self.allowed_digests.items()
        }
        object.__setattr__(self, "allowed_digests", MappingProxyType(copied))
        object.__setattr__(
            self,
            "trusted_in_process_plugins",
            frozenset(self.trusted_in_process_plugins),
        )

    def allows(
        self,
        *,
        manifest: PluginManifest,
        source_path: Path,
        digest: str,
        signature_verified: bool,
    ) -> bool:
        del source_path
        if digest not in self.allowed_digests.get(manifest.plugin_id, frozenset()):
            return False
        if self.require_signatures and not signature_verified:
            return False
        if (
            manifest.trust_mode is TrustMode.TRUSTED_IN_PROCESS
            and manifest.plugin_id not in self.trusted_in_process_plugins
        ):
            return False
        return True


@dataclass(frozen=True, slots=True)
class VerifiedPluginSource:
    """Attested source returned only after all data-only checks complete."""

    root: Path
    manifest_path: Path
    manifest: PluginManifest
    digest: str
    manifest_digest: str
    config: Mapping[str, Any]
    signature_verified: bool
    _attestation: object = field(repr=False, compare=False)

    def is_attested_by(self, token: object) -> bool:
        return self._attestation is token


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _inside(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _deep_freeze(value: Any) -> Any:
    if isinstance(value, dict):
        return MappingProxyType({key: _deep_freeze(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_deep_freeze(item) for item in value)
    return value


def deterministic_directory_digest(
    root: Path,
    *,
    manifest_name: str = DEFAULT_MANIFEST_NAME,
    max_files: int = 10_000,
    max_bytes: int = 512 * 1024 * 1024,
) -> str:
    """Hash names, lengths and bytes of regular files in stable POSIX order.

    The root manifest is excluded to avoid a self-referential digest.  Symlinks
    and special files are rejected rather than followed.  Empty directories do
    not affect the artifact identity.
    """

    resolved_root = root.resolve(strict=True)
    if not resolved_root.is_dir() or root.is_symlink():
        raise PluginDiscoveryError(
            PluginErrorCode.INVALID_SOURCE,
            "plugin source must be a real directory, not a symlink",
            path=str(root),
        )

    def walk_error(error: OSError) -> None:
        raise PluginDiscoveryError(
            PluginErrorCode.INVALID_SOURCE,
            "plugin artifact could not be read completely",
            path=error.filename,
        ) from error

    files: list[tuple[str, Path]] = []
    for current, directories, names in os.walk(
        resolved_root, followlinks=False, onerror=walk_error
    ):
        current_path = Path(current)
        for directory in directories:
            candidate = current_path / directory
            if candidate.is_symlink():
                raise PluginDiscoveryError(
                    PluginErrorCode.INVALID_SOURCE,
                    "plugin artifacts must not contain symbolic links",
                    path=str(candidate),
                )
        for name in names:
            candidate = current_path / name
            relative = candidate.relative_to(resolved_root).as_posix()
            if relative == manifest_name:
                continue
            if candidate.is_symlink() or not candidate.is_file():
                raise PluginDiscoveryError(
                    PluginErrorCode.INVALID_SOURCE,
                    "plugin artifacts may contain regular files only",
                    path=str(candidate),
                )
            files.append((relative, candidate))

    files.sort(key=lambda item: item[0])
    if len(files) > max_files:
        raise PluginDiscoveryError(
            PluginErrorCode.INVALID_SOURCE,
            "plugin artifact exceeds the file-count limit",
            count=len(files),
            limit=max_files,
        )

    hasher = hashlib.sha256(b"suiteharness-plugin-directory-v1\0")
    total = 0
    for relative, candidate in files:
        size = candidate.stat().st_size
        total += size
        if total > max_bytes:
            raise PluginDiscoveryError(
                PluginErrorCode.INVALID_SOURCE,
                "plugin artifact exceeds the byte limit",
                bytes=total,
                limit=max_bytes,
            )
        encoded_name = relative.encode("utf-8")
        hasher.update(len(encoded_name).to_bytes(8, "big"))
        hasher.update(encoded_name)
        hasher.update(size.to_bytes(8, "big"))
        with candidate.open("rb") as stream:
            while chunk := stream.read(1024 * 1024):
                hasher.update(chunk)
    return f"sha256:{hasher.hexdigest()}"


def signature_payload(manifest: PluginManifest) -> bytes:
    """Canonical signed payload; the signature value itself is omitted."""

    payload = manifest.model_dump(mode="json")
    payload["artifact"]["signature"] = None
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")


class PluginDiscovery:
    """Discover only explicit paths beneath administrator-approved roots."""

    def __init__(
        self,
        *,
        allowed_roots: Iterable[Path],
        trust_policy: TrustPolicy,
        signature_verifier: SignatureVerifier | None = None,
        manifest_name: str = DEFAULT_MANIFEST_NAME,
        max_manifest_bytes: int = 1024 * 1024,
        max_artifact_files: int = 10_000,
        max_artifact_bytes: int = 512 * 1024 * 1024,
    ) -> None:
        roots = tuple(Path(root).resolve(strict=True) for root in allowed_roots)
        if not roots:
            raise ValueError("at least one plugin allowlist root is required")
        if any(not root.is_dir() for root in roots):
            raise ValueError("every plugin allowlist root must be a directory")
        if not manifest_name or Path(manifest_name).name != manifest_name:
            raise ValueError("manifest_name must be one file name")
        self._allowed_roots = roots
        self._trust_policy = trust_policy
        self._signature_verifier = signature_verifier
        self._manifest_name = manifest_name
        self._max_manifest_bytes = max_manifest_bytes
        self._max_artifact_files = max_artifact_files
        self._max_artifact_bytes = max_artifact_bytes
        self._attestation = object()

    @property
    def attestation(self) -> object:
        """Opaque identity used to reject plans forged outside this discovery."""

        return self._attestation

    def discover(self, declaration: PluginSourceDeclaration) -> VerifiedPluginSource:
        configured = Path(declaration.path)
        if configured.is_symlink():
            raise PluginDiscoveryError(
                PluginErrorCode.INVALID_SOURCE,
                "configured plugin path must not be a symbolic link",
                path=str(configured),
            )
        try:
            root = configured.resolve(strict=True)
        except OSError as exc:
            raise PluginDiscoveryError(
                PluginErrorCode.INVALID_SOURCE,
                "configured plugin path does not exist",
                path=str(configured),
            ) from exc
        if not root.is_dir():
            raise PluginDiscoveryError(
                PluginErrorCode.INVALID_SOURCE,
                "configured plugin path must be a directory",
                path=str(root),
            )
        if not any(_inside(root, allowed) for allowed in self._allowed_roots):
            raise PluginDiscoveryError(
                PluginErrorCode.PATH_NOT_ALLOWED,
                "plugin path is outside the configured allowlist roots",
                path=str(root),
            )

        manifest_path = root / self._manifest_name
        if manifest_path.is_symlink() or not manifest_path.is_file():
            raise PluginDiscoveryError(
                PluginErrorCode.INVALID_SOURCE,
                "plugin manifest must be a regular non-symlink file",
                path=str(manifest_path),
            )
        try:
            raw = manifest_path.read_bytes()
        except OSError as exc:
            raise PluginDiscoveryError(
                PluginErrorCode.INVALID_MANIFEST,
                "plugin manifest could not be read",
                path=str(manifest_path),
            ) from exc
        if len(raw) > self._max_manifest_bytes:
            raise PluginDiscoveryError(
                PluginErrorCode.INVALID_MANIFEST,
                "plugin manifest exceeds the configured size limit",
                bytes=len(raw),
                limit=self._max_manifest_bytes,
            )
        try:
            document = json.loads(raw.decode("utf-8"), object_pairs_hook=_reject_duplicate_keys)
            manifest = PluginManifest.model_validate(document)
        except (
            UnicodeDecodeError,
            RecursionError,
            TypeError,
            ValueError,
            PydanticValidationError,
        ) as exc:
            raise PluginDiscoveryError(
                PluginErrorCode.INVALID_MANIFEST,
                "plugin manifest is invalid",
                path=str(manifest_path),
            ) from exc

        if declaration.expected_digest != manifest.artifact.digest:
            raise PluginDiscoveryError(
                PluginErrorCode.DIGEST_MISMATCH,
                "configured and declared plugin digests must match",
                expected=declaration.expected_digest,
                declared=manifest.artifact.digest,
            )

        digest = deterministic_directory_digest(
            root,
            manifest_name=self._manifest_name,
            max_files=self._max_artifact_files,
            max_bytes=self._max_artifact_bytes,
        )
        if manifest.artifact.digest != digest:
            raise PluginDiscoveryError(
                PluginErrorCode.DIGEST_MISMATCH,
                "configured, declared and computed plugin digests must match",
                expected=declaration.expected_digest,
                declared=manifest.artifact.digest,
                computed=digest,
            )

        signature_verified = False
        signature = manifest.artifact.signature
        if signature is not None:
            if self._signature_verifier is None or not self._signature_verifier.verify(
                signature_payload(manifest), signature
            ):
                raise PluginDiscoveryError(
                    PluginErrorCode.SIGNATURE_REJECTED,
                    "plugin signature verification failed",
                    plugin_id=manifest.plugin_id,
                    key_id=signature.key_id,
                )
            signature_verified = True

        if not self._trust_policy.allows(
            manifest=manifest,
            source_path=root,
            digest=digest,
            signature_verified=signature_verified,
        ):
            raise PluginDiscoveryError(
                PluginErrorCode.TRUST_REJECTED,
                "deployment trust policy rejected the plugin artifact",
                plugin_id=manifest.plugin_id,
                trust_mode=manifest.trust_mode.value,
            )

        try:
            safe_config_schema = harden_untrusted_json_schema(
                manifest.config_schema,
                label="plugin config_schema",
            )
            Draft202012Validator.check_schema(safe_config_schema)
            # A plugin config comes from a server file, never as an injected
            # Python object.  The round trip both enforces JSON values and breaks
            # mutable references held by the caller.
            frozen_config = json.loads(
                json.dumps(declaration.config, allow_nan=False, sort_keys=True)
            )
            Draft202012Validator(safe_config_schema).validate(frozen_config)
        except (
            RecursionError,
            TypeError,
            ValueError,
            PydanticValidationError,
            SchemaError,
            UnsafeJsonSchemaError,
        ) as exc:
            raise PluginDiscoveryError(
                PluginErrorCode.INVALID_MANIFEST,
                "plugin manifest is invalid",
                path=str(manifest_path),
            ) from exc
        except ValidationError as exc:
            raise PluginDiscoveryError(
                PluginErrorCode.INVALID_MANIFEST,
                "plugin configuration does not match config_schema",
                path=str(manifest_path),
            ) from exc
        manifest = manifest.model_copy(
            update={"config_schema": safe_config_schema},
            deep=True,
        )

        return VerifiedPluginSource(
            root=root,
            manifest_path=manifest_path,
            manifest=manifest,
            digest=digest,
            manifest_digest=f"sha256:{hashlib.sha256(raw).hexdigest()}",
            config=_deep_freeze(frozen_config),
            signature_verified=signature_verified,
            _attestation=self._attestation,
        )

    def discover_all(
        self, declarations: Iterable[PluginSourceDeclaration]
    ) -> tuple[VerifiedPluginSource, ...]:
        return tuple(self.discover(item) for item in declarations)

    def assert_current(self, source: VerifiedPluginSource) -> None:
        """Narrow the verify/import race immediately before trusted loading."""

        if not source.is_attested_by(self._attestation):
            raise PluginDiscoveryError(
                PluginErrorCode.TRUST_REJECTED,
                "plugin source was not attested by this discovery instance",
            )
        current = deterministic_directory_digest(
            source.root,
            manifest_name=self._manifest_name,
            max_files=self._max_artifact_files,
            max_bytes=self._max_artifact_bytes,
        )
        if current != source.digest:
            raise PluginDiscoveryError(
                PluginErrorCode.DIGEST_MISMATCH,
                "plugin artifact changed after discovery",
                expected=source.digest,
                computed=current,
            )
        try:
            manifest_bytes = (source.root / self._manifest_name).read_bytes()
        except OSError as exc:
            raise PluginDiscoveryError(
                PluginErrorCode.INVALID_MANIFEST,
                "plugin manifest is no longer readable",
                path=str(source.manifest_path),
            ) from exc
        if len(manifest_bytes) > self._max_manifest_bytes:
            raise PluginDiscoveryError(
                PluginErrorCode.INVALID_MANIFEST,
                "plugin manifest changed beyond the configured size limit",
            )
        manifest_digest = f"sha256:{hashlib.sha256(manifest_bytes).hexdigest()}"
        if manifest_digest != source.manifest_digest:
            raise PluginDiscoveryError(
                PluginErrorCode.INVALID_MANIFEST,
                "plugin manifest changed after discovery",
            )


__all__ = [
    "AllowlistedTrustPolicy",
    "DEFAULT_MANIFEST_NAME",
    "PluginDiscovery",
    "SignatureVerifier",
    "TrustPolicy",
    "VerifiedPluginSource",
    "deterministic_directory_digest",
    "signature_payload",
]
