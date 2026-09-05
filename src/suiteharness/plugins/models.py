"""Strict, data-only plugin declarations.

Manifests are deliberately safe to parse before any plugin module is imported.
They describe requested authority; they never grant that authority themselves.
"""

from __future__ import annotations

import re
from enum import Enum
from pathlib import Path
from typing import Any

from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.version import InvalidVersion, Version
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

PLUGIN_SCHEMA_VERSION = "1"
_ID = re.compile(r"^[a-z][a-z0-9]*(?:[._/-][a-z0-9]+)*$")
_ENTRYPOINT = re.compile(r"^[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*:[A-Za-z_]\w*$")
_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_KEY_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")


def _identifier(value: str, label: str) -> str:
    if not _ID.fullmatch(value):
        raise ValueError(f"invalid {label}: {value!r}")
    return value


def _canonical_version(value: str, label: str) -> str:
    if not value or value.strip() != value:
        raise ValueError(f"{label} must be a non-empty canonical version")
    try:
        parsed = Version(value)
    except InvalidVersion as exc:
        raise ValueError(f"invalid {label}: {value!r}") from exc
    if str(parsed) != value:
        raise ValueError(f"{label} must be canonical; use {parsed!s}")
    return value


def _version_specifier(value: str, label: str, *, allow_empty: bool) -> str:
    if value.strip() != value or (not value and not allow_empty):
        raise ValueError(f"{label} must be a valid PEP 440 specifier")
    try:
        SpecifierSet(value)
    except InvalidSpecifier as exc:
        raise ValueError(f"invalid {label}: {value!r}") from exc
    return value


class _FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class TrustMode(str, Enum):
    """Where code executes; ``node:vm`` is intentionally not an option."""

    TRUSTED_IN_PROCESS = "trusted_in_process"
    ISOLATED_WORKER = "isolated_worker"
    MCP = "mcp"


class Permission(str, Enum):
    """Declarative upper bound, not an authority-bearing object."""

    WORKSPACE_READ = "workspace.read"
    WORKSPACE_WRITE = "workspace.write"
    NETWORK_EGRESS = "network.egress"
    PROCESS_SPAWN = "process.spawn"
    MODEL_INVOKE = "model.invoke"
    MEMORY_READ = "memory.read"
    MEMORY_WRITE = "memory.write"
    KNOWLEDGE_READ = "knowledge.read"
    KNOWLEDGE_WRITE = "knowledge.write"
    MCP_CONNECT = "mcp.connect"
    CHANNEL_RECEIVE = "channel.receive"
    CHANNEL_SEND = "channel.send"
    TOOL_REGISTER = "tool.register"


class PermissionSet(_FrozenModel):
    """A closed permission vocabulary that cannot express root authorities."""

    values: frozenset[Permission] = frozenset()

    def permits(self, permission: Permission) -> bool:
        return permission in self.values

    def contains(self, required: PermissionSet) -> bool:
        return required.values.issubset(self.values)


class SignatureMetadata(_FrozenModel):
    algorithm: str
    key_id: str
    value: str

    @field_validator("algorithm")
    @classmethod
    def algorithm_is_named(cls, value: str) -> str:
        return _identifier(value, "signature algorithm")

    @field_validator("key_id")
    @classmethod
    def key_id_is_safe(cls, value: str) -> str:
        if not _KEY_ID.fullmatch(value):
            raise ValueError("invalid signature key_id")
        return value

    @field_validator("value")
    @classmethod
    def signature_is_present(cls, value: str) -> str:
        if not value or value.strip() != value or len(value) > 16384:
            raise ValueError("signature value must contain 1-16384 characters")
        return value


class ArtifactIntegrity(_FrozenModel):
    """Digest covers every regular artifact file except the manifest itself."""

    digest: str
    signature: SignatureMetadata | None = None

    @field_validator("digest")
    @classmethod
    def sha256_digest(cls, value: str) -> str:
        if not _DIGEST.fullmatch(value):
            raise ValueError("artifact digest must use sha256:<64 lowercase hex>")
        return value


class ProvidedService(_FrozenModel):
    service: str
    version: str

    @field_validator("service")
    @classmethod
    def service_id(cls, value: str) -> str:
        return _identifier(value, "provided service")

    @field_validator("version")
    @classmethod
    def service_version(cls, value: str) -> str:
        return _canonical_version(value, "provided service version")


class RequiredService(_FrozenModel):
    service: str
    version: str = ""
    optional: bool = False

    @field_validator("service")
    @classmethod
    def service_id(cls, value: str) -> str:
        return _identifier(value, "required service")

    @field_validator("version")
    @classmethod
    def service_version(cls, value: str) -> str:
        return _version_specifier(value, "required service version", allow_empty=True)

    def accepts(self, version: str | Version) -> bool:
        candidate = version if isinstance(version, Version) else Version(version)
        return candidate in SpecifierSet(self.version)


class ContributionKind(str, Enum):
    PRODUCT = "product"
    MEMORY = "memory"
    KNOWLEDGE = "knowledge"
    WORKFLOW = "workflow"
    REFLECTION = "reflection"
    PROMPT = "prompt"
    TOOL = "tools"
    MODEL = "models"
    MCP = "mcp"
    CHANNEL = "channels"


class ContributionDeclaration(_FrozenModel):
    contribution_id: str
    version: str = "1.0.0"
    permissions: PermissionSet = Field(default_factory=PermissionSet)

    @field_validator("contribution_id")
    @classmethod
    def contribution_name(cls, value: str) -> str:
        return _identifier(value, "contribution_id")

    @field_validator("version")
    @classmethod
    def contribution_version(cls, value: str) -> str:
        return _canonical_version(value, "contribution version")


class Contributions(_FrozenModel):
    """Every extension point is explicit; unknown contribution kinds fail."""

    product: tuple[ContributionDeclaration, ...] = ()
    memory: tuple[ContributionDeclaration, ...] = ()
    knowledge: tuple[ContributionDeclaration, ...] = ()
    workflow: tuple[ContributionDeclaration, ...] = ()
    reflection: tuple[ContributionDeclaration, ...] = ()
    prompt: tuple[ContributionDeclaration, ...] = ()
    tools: tuple[ContributionDeclaration, ...] = ()
    models: tuple[ContributionDeclaration, ...] = ()
    mcp: tuple[ContributionDeclaration, ...] = ()
    channels: tuple[ContributionDeclaration, ...] = ()

    def items(self) -> tuple[tuple[ContributionKind, ContributionDeclaration], ...]:
        result: list[tuple[ContributionKind, ContributionDeclaration]] = []
        for kind in ContributionKind:
            values = getattr(self, kind.value)
            result.extend((kind, item) for item in values)
        return tuple(result)

    @model_validator(mode="after")
    def unique_within_kind(self) -> Contributions:
        for kind in ContributionKind:
            values = getattr(self, kind.value)
            names = [item.contribution_id for item in values]
            if len(names) != len(set(names)):
                raise ValueError(f"duplicate {kind.value} contribution_id")
        return self


class PluginManifest(_FrozenModel):
    """Untrusted metadata parsed without importing the plugin."""

    schema_version: str
    plugin_id: str
    version: str
    harness_api: str
    trust_mode: TrustMode
    entrypoint: str
    artifact: ArtifactIntegrity
    provides: tuple[ProvidedService, ...] = ()
    requires: tuple[RequiredService, ...] = ()
    contributions: Contributions = Field(default_factory=Contributions)
    permissions: PermissionSet = Field(default_factory=PermissionSet)
    config_schema: dict[str, Any] = Field(
        default_factory=lambda: {"type": "object", "additionalProperties": False}
    )

    @field_validator("schema_version")
    @classmethod
    def supported_schema(cls, value: str) -> str:
        if value != PLUGIN_SCHEMA_VERSION:
            raise ValueError(f"unsupported plugin schema_version: {value!r}")
        return value

    @field_validator("plugin_id")
    @classmethod
    def plugin_name(cls, value: str) -> str:
        return _identifier(value, "plugin_id")

    @field_validator("version")
    @classmethod
    def plugin_version(cls, value: str) -> str:
        return _canonical_version(value, "plugin version")

    @field_validator("harness_api")
    @classmethod
    def harness_range(cls, value: str) -> str:
        return _version_specifier(value, "harness_api", allow_empty=False)

    @field_validator("entrypoint")
    @classmethod
    def entrypoint_syntax(cls, value: str) -> str:
        if not _ENTRYPOINT.fullmatch(value):
            raise ValueError("entrypoint must use package.module:Attribute syntax")
        return value

    @model_validator(mode="after")
    def relationships(self) -> PluginManifest:
        provided = [item.service for item in self.provides]
        required = [item.service for item in self.requires]
        if len(provided) != len(set(provided)):
            raise ValueError("provides must not repeat a service")
        if len(required) != len(set(required)):
            raise ValueError("requires must not repeat a service")
        overlap = sorted(set(provided).intersection(required))
        if overlap:
            raise ValueError(f"a plugin cannot require its own provided service: {overlap}")
        for kind, contribution in self.contributions.items():
            if not self.permissions.contains(contribution.permissions):
                raise ValueError(
                    f"{kind.value}:{contribution.contribution_id} requests permissions "
                    "outside the plugin permission set"
                )
        return self

    def supports_harness(self, version: str | Version) -> bool:
        candidate = version if isinstance(version, Version) else Version(version)
        return candidate in SpecifierSet(self.harness_api)

    @property
    def parsed_version(self) -> Version:
        return Version(self.version)

    def contribution_map(
        self,
    ) -> dict[tuple[ContributionKind, str], ContributionDeclaration]:
        return {
            (kind, declaration.contribution_id): declaration
            for kind, declaration in self.contributions.items()
        }


class PluginSourceDeclaration(_FrozenModel):
    """One explicit server configuration entry; no directory scanning occurs."""

    path: Path
    expected_digest: str
    config: dict[str, Any] = Field(default_factory=dict)

    @field_validator("path")
    @classmethod
    def absolute_server_path(cls, value: Path) -> Path:
        if not value.is_absolute():
            raise ValueError("plugin source path must be absolute")
        return value

    @field_validator("expected_digest")
    @classmethod
    def sha256_digest(cls, value: str) -> str:
        if not _DIGEST.fullmatch(value):
            raise ValueError("expected_digest must use sha256:<64 lowercase hex>")
        return value


__all__ = [
    "ArtifactIntegrity",
    "ContributionDeclaration",
    "ContributionKind",
    "Contributions",
    "Permission",
    "PermissionSet",
    "PLUGIN_SCHEMA_VERSION",
    "PluginManifest",
    "PluginSourceDeclaration",
    "ProvidedService",
    "RequiredService",
    "SignatureMetadata",
    "TrustMode",
]
