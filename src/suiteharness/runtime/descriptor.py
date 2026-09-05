"""Strict declarations for products and composable runtime bundles."""

from __future__ import annotations

import re

from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.version import InvalidVersion, Version
from pydantic import BaseModel, ConfigDict, field_validator, model_validator

_COMPONENT_ID = re.compile(r"^[a-z][a-z0-9]*(?:[._-][a-z0-9]+)*$")
_RESOURCE_ID = re.compile(r"^[a-z][a-z0-9]*(?:[._:/-][a-z0-9]+)*$")


def _component_id(value: str, label: str) -> str:
    if not isinstance(value, str) or not _COMPONENT_ID.fullmatch(value):
        raise ValueError(f"invalid {label}: {value!r}")
    return value


def _resource_set(values: frozenset[str], label: str) -> frozenset[str]:
    for value in values:
        if not _RESOURCE_ID.fullmatch(value):
            raise ValueError(f"invalid {label}: {value!r}")
    return values


def _version(value: str, label: str) -> str:
    clean = value.strip()
    if clean != value or not clean:
        raise ValueError(f"{label} must be a non-empty canonical version string")
    try:
        parsed = Version(clean)
    except InvalidVersion as exc:
        raise ValueError(f"invalid {label}: {value!r}") from exc
    if str(parsed) != clean:
        raise ValueError(f"{label} must be canonical; use {parsed!s}")
    return clean


def _specifier(value: str, label: str, *, allow_empty: bool) -> str:
    clean = value.strip()
    if clean != value or (not clean and not allow_empty):
        qualifier = "a version specifier" if allow_empty else "a non-empty version specifier"
        raise ValueError(f"{label} must be {qualifier}")
    try:
        SpecifierSet(clean)
    except InvalidSpecifier as exc:
        raise ValueError(f"invalid {label}: {value!r}") from exc
    return clean


class _FrozenManifest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class EmptyProductConfig(BaseModel):
    """Default product config: deliberately accepts no undeclared settings."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class BundleRequirement(_FrozenManifest):
    """A dependency on one named bundle and an optional PEP 440 version range."""

    bundle_id: str
    version: str = ""
    optional: bool = False

    @field_validator("bundle_id")
    @classmethod
    def validate_bundle_id(cls, value: str) -> str:
        return _component_id(value, "bundle_id")

    @field_validator("version")
    @classmethod
    def validate_version_spec(cls, value: str) -> str:
        return _specifier(value, "bundle version constraint", allow_empty=True)

    def accepts(self, candidate: str | Version) -> bool:
        parsed = candidate if isinstance(candidate, Version) else Version(candidate)
        return parsed in SpecifierSet(self.version)


class BundleConflict(_FrozenManifest):
    """A bundle that cannot coexist when its version matches this constraint."""

    bundle_id: str
    version: str = ""

    @field_validator("bundle_id")
    @classmethod
    def validate_bundle_id(cls, value: str) -> str:
        return _component_id(value, "bundle_id")

    @field_validator("version")
    @classmethod
    def validate_version_spec(cls, value: str) -> str:
        return _specifier(value, "conflict version constraint", allow_empty=True)

    def matches(self, candidate: str | Version) -> bool:
        parsed = candidate if isinstance(candidate, Version) else Version(candidate)
        return parsed in SpecifierSet(self.version)


class BundleManifest(_FrozenManifest):
    """Side-effect-free metadata used before a bundle is installed."""

    bundle_id: str
    version: str
    harness_api: str
    requires: tuple[BundleRequirement, ...] = ()
    conflicts: tuple[BundleConflict, ...] = ()
    provides: frozenset[str] = frozenset()
    capabilities: frozenset[str] = frozenset()

    @field_validator("bundle_id")
    @classmethod
    def validate_bundle_id(cls, value: str) -> str:
        return _component_id(value, "bundle_id")

    @field_validator("version")
    @classmethod
    def validate_version(cls, value: str) -> str:
        return _version(value, "bundle version")

    @field_validator("harness_api")
    @classmethod
    def validate_harness_api(cls, value: str) -> str:
        return _specifier(value, "harness_api", allow_empty=False)

    @field_validator("provides")
    @classmethod
    def validate_provides(cls, value: frozenset[str]) -> frozenset[str]:
        return _resource_set(value, "provided service")

    @field_validator("capabilities")
    @classmethod
    def validate_capabilities(cls, value: frozenset[str]) -> frozenset[str]:
        return _resource_set(value, "capability")

    @model_validator(mode="after")
    def validate_relationships(self) -> BundleManifest:
        dependency_ids = [item.bundle_id for item in self.requires]
        if len(dependency_ids) != len(set(dependency_ids)):
            raise ValueError("bundle requirements must not repeat a bundle_id")
        conflict_ids = [item.bundle_id for item in self.conflicts]
        if len(conflict_ids) != len(set(conflict_ids)):
            raise ValueError("bundle conflicts must not repeat a bundle_id")
        if self.bundle_id in dependency_ids:
            raise ValueError("a bundle cannot depend on itself")
        if self.bundle_id in conflict_ids:
            raise ValueError("a bundle cannot conflict with itself")
        return self

    @property
    def parsed_version(self) -> Version:
        return Version(self.version)

    def supports_harness(self, version: str | Version) -> bool:
        parsed = version if isinstance(version, Version) else Version(version)
        return parsed in SpecifierSet(self.harness_api)


class ProductDescriptor(_FrozenManifest):
    """Validated product identity, compatibility range and bundle composition."""

    product_id: str
    version: str
    harness_api: str
    bundles: tuple[BundleRequirement, ...] = ()
    capabilities: frozenset[str] = frozenset()
    config_model: type[BaseModel] = EmptyProductConfig

    @field_validator("product_id")
    @classmethod
    def validate_product_id(cls, value: str) -> str:
        return _component_id(value, "product_id")

    @field_validator("version")
    @classmethod
    def validate_version(cls, value: str) -> str:
        return _version(value, "product version")

    @field_validator("harness_api")
    @classmethod
    def validate_harness_api(cls, value: str) -> str:
        return _specifier(value, "harness_api", allow_empty=False)

    @field_validator("capabilities")
    @classmethod
    def validate_capabilities(cls, value: frozenset[str]) -> frozenset[str]:
        return _resource_set(value, "capability")

    @field_validator("config_model")
    @classmethod
    def validate_config_model(cls, value: type[BaseModel]) -> type[BaseModel]:
        if not isinstance(value, type) or not issubclass(value, BaseModel):
            raise TypeError("config_model must be a pydantic BaseModel subclass")
        if value.model_config.get("extra") != "forbid":
            raise ValueError("config_model must set model_config extra='forbid'")
        return value

    @model_validator(mode="after")
    def validate_bundles(self) -> ProductDescriptor:
        bundle_ids = [item.bundle_id for item in self.bundles]
        if len(bundle_ids) != len(set(bundle_ids)):
            raise ValueError("product bundles must not repeat a bundle_id")
        return self

    @property
    def parsed_version(self) -> Version:
        return Version(self.version)

    def supports_harness(self, version: str | Version) -> bool:
        parsed = version if isinstance(version, Version) else Version(version)
        return parsed in SpecifierSet(self.harness_api)

    def parse_config(self, raw: object) -> BaseModel:
        """Validate raw configuration with the product's strict model."""

        # ``model_validate`` may return an existing model instance and fields
        # containing arbitrary/mutable values may retain caller references.
        # A recursive model copy ensures the resolved plan owns its data while
        # preserving every input form accepted by the product model.
        parsed = self.config_model.model_validate(raw)
        return parsed.model_copy(deep=True)
