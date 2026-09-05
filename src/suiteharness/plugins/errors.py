"""Machine-readable failures for plugin discovery, planning and lifecycle."""

from __future__ import annotations

from collections.abc import Mapping
from enum import Enum
from types import MappingProxyType


class PluginErrorCode(str, Enum):
    PATH_NOT_ALLOWED = "path_not_allowed"
    INVALID_SOURCE = "invalid_source"
    INVALID_MANIFEST = "invalid_manifest"
    DIGEST_MISMATCH = "digest_mismatch"
    SIGNATURE_REJECTED = "signature_rejected"
    TRUST_REJECTED = "trust_rejected"
    HARNESS_API_MISMATCH = "harness_api_mismatch"
    DUPLICATE_PLUGIN = "duplicate_plugin"
    DUPLICATE_PROVIDER = "duplicate_provider"
    MISSING_PROVIDER = "missing_provider"
    PROVIDER_VERSION_MISMATCH = "provider_version_mismatch"
    DEPENDENCY_CYCLE = "dependency_cycle"
    CONTRIBUTION_CONFLICT = "contribution_conflict"
    INVALID_EXPORTS = "invalid_exports"
    INVALID_CONTRIBUTION = "invalid_contribution"
    UNSUPPORTED_TRUST_MODE = "unsupported_trust_mode"
    ALREADY_ACTIVE = "already_active"
    NOT_ACTIVE = "not_active"
    ACTIVATION_FAILED = "activation_failed"
    CLEANUP_FAILED = "cleanup_failed"


class PluginError(RuntimeError):
    """Base plugin error with a stable code and immutable diagnostics."""

    def __init__(self, code: PluginErrorCode, message: str, **detail: object) -> None:
        self.code = code
        self.detail: Mapping[str, object] = MappingProxyType(dict(detail))
        super().__init__(message)


class PluginDiscoveryError(PluginError):
    """A configured artifact did not pass side-effect-free verification."""


class PluginPlanError(PluginError):
    """A set of verified plugins cannot form one deterministic graph."""


class PluginLifecycleError(PluginError):
    """Plugin preparation, activation, publication or cleanup failed."""


__all__ = [
    "PluginDiscoveryError",
    "PluginError",
    "PluginErrorCode",
    "PluginLifecycleError",
    "PluginPlanError",
]
