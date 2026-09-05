"""Plan customer product combinations without installing product code.

``BundleManifest`` describes components *inside one product*.  This module sits
one level above it: ``CustomerBundleManifest`` declares which products a
customer bought, their version/config constraints, and the only permitted
cross-product Customer360 flows.

The plan is declarative.  It does not install products or configure memory.
The runtime must still enforce tenant/product scopes and the curated
Customer360 protocol.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from copy import deepcopy
from dataclasses import dataclass, field
from enum import Enum
from types import MappingProxyType
from typing import Any

from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.version import InvalidVersion, Version
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from suiteharness.runtime.descriptor import ProductDescriptor

_IDENTIFIER = re.compile(r"^[a-z][a-z0-9]*(?:[._-][a-z0-9]+)*$")
_PREDICATE = re.compile(r"^[a-z][a-z0-9]*(?:[._:-][a-z0-9]+)*$")


def _identifier(value: str, label: str) -> str:
    if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
        raise ValueError(f"invalid {label}: {value!r}")
    return value


def _canonical_version(value: str, label: str) -> str:
    clean = value.strip()
    if not clean or clean != value:
        raise ValueError(f"{label} must be a non-empty canonical version string")
    try:
        parsed = Version(clean)
    except InvalidVersion as exc:
        raise ValueError(f"invalid {label}: {value!r}") from exc
    if str(parsed) != clean:
        raise ValueError(f"{label} must be canonical; use {parsed!s}")
    return clean


def _specifier(value: str, label: str) -> str:
    clean = value.strip()
    if not clean or clean != value:
        raise ValueError(f"{label} must be a non-empty PEP 440 specifier")
    try:
        SpecifierSet(clean)
    except InvalidSpecifier as exc:
        raise ValueError(f"invalid {label}: {value!r}") from exc
    return clean


class _FrozenManifest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class CustomerProductSelection(_FrozenManifest):
    """One selected product and its customer-specific, not-yet-parsed config."""

    product_id: str
    version: str
    config: dict[str, Any] = Field(default_factory=dict)

    @field_validator("config")
    @classmethod
    def snapshot_raw_config(cls, value: dict[str, Any]) -> dict[str, Any]:
        """Detach nested raw values from the caller before retaining them.

        Pydantic copies the outer ``dict`` but values annotated as ``Any`` may
        otherwise keep caller-owned nested dictionaries and lists by reference.
        """

        try:
            return deepcopy(value)
        except Exception as exc:
            raise ValueError("product config must be deep-copyable") from exc

    @field_validator("product_id")
    @classmethod
    def validate_product_id(cls, value: str) -> str:
        return _identifier(value, "product_id")

    @field_validator("version")
    @classmethod
    def validate_version(cls, value: str) -> str:
        return _specifier(value, "product version constraint")

    def accepts(self, candidate: str | Version) -> bool:
        parsed = candidate if isinstance(candidate, Version) else Version(candidate)
        return parsed in SpecifierSet(self.version)


class Customer360Mode(str, Enum):
    ISOLATED = "isolated"
    CURATED = "curated"


class Customer360SharingRule(_FrozenManifest):
    """One directional and field-level allowlist for curated sharing."""

    producer: str
    consumer: str
    predicates: tuple[str, ...]

    @field_validator("producer", "consumer")
    @classmethod
    def validate_product_id(cls, value: str) -> str:
        return _identifier(value, "sharing product_id")

    @field_validator("predicates")
    @classmethod
    def validate_predicates(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if not values:
            raise ValueError("sharing predicates must not be empty")
        for value in values:
            if (
                not isinstance(value, str)
                or value.strip() != value
                or not _PREDICATE.fullmatch(value)
            ):
                raise ValueError(f"invalid sharing predicate: {value!r}")
        if len(values) != len(set(values)):
            raise ValueError("sharing predicates must not repeat")
        return values

    @model_validator(mode="after")
    def reject_self_edge(self) -> Customer360SharingRule:
        if self.producer == self.consumer:
            raise ValueError("Customer360 sharing cannot target the producer itself")
        return self


class Customer360SharingManifest(_FrozenManifest):
    """Deny-by-default Customer360 declaration for this product combination."""

    mode: Customer360Mode = Customer360Mode.ISOLATED
    rules: tuple[Customer360SharingRule, ...] = ()

    @model_validator(mode="after")
    def require_explicit_curated_mode(self) -> Customer360SharingManifest:
        if self.mode is Customer360Mode.ISOLATED and self.rules:
            raise ValueError("sharing rules require mode='curated'")
        if self.mode is Customer360Mode.CURATED and not self.rules:
            raise ValueError("mode='curated' requires at least one explicit sharing rule")
        return self


class CustomerBundleManifest(_FrozenManifest):
    """A customer's selected products, separate from product-internal bundles.

    Product-private profiles remain the default.  Curated rules describe which
    copied predicates may cross product boundaries; they do not grant direct
    access to another product's private profile.
    """

    schema_version: str = "1"
    customer_bundle_id: str
    version: str
    harness_api: str
    products: tuple[CustomerProductSelection, ...]
    customer360: Customer360SharingManifest = Field(default_factory=Customer360SharingManifest)

    @field_validator("schema_version")
    @classmethod
    def validate_schema_version(cls, value: str) -> str:
        if value != "1":
            raise ValueError("unsupported customer bundle schema_version")
        return value

    @field_validator("customer_bundle_id")
    @classmethod
    def validate_customer_bundle_id(cls, value: str) -> str:
        return _identifier(value, "customer_bundle_id")

    @field_validator("version")
    @classmethod
    def validate_version(cls, value: str) -> str:
        return _canonical_version(value, "customer bundle version")

    @field_validator("harness_api")
    @classmethod
    def validate_harness_api(cls, value: str) -> str:
        return _specifier(value, "customer bundle harness_api")

    @model_validator(mode="after")
    def validate_product_graph(self) -> CustomerBundleManifest:
        if not self.products:
            raise ValueError("a customer bundle must select at least one product")

        selected = [item.product_id for item in self.products]
        if len(selected) != len(set(selected)):
            raise ValueError("customer bundle products must not repeat a product_id")
        selected_set = set(selected)

        edges: set[tuple[str, str]] = set()
        for rule in self.customer360.rules:
            missing = {rule.producer, rule.consumer} - selected_set
            if missing:
                names = ", ".join(sorted(missing))
                raise ValueError(f"Customer360 rule references unselected product(s): {names}")
            edge = (rule.producer, rule.consumer)
            if edge in edges:
                raise ValueError("Customer360 producer/consumer edges must not repeat")
            edges.add(edge)
        return self

    @property
    def parsed_version(self) -> Version:
        return Version(self.version)

    def supports_harness(self, version: str | Version) -> bool:
        parsed = version if isinstance(version, Version) else Version(version)
        return parsed in SpecifierSet(self.harness_api)


class CustomerBundleResolutionCode(str, Enum):
    INVALID_CATALOG = "invalid_product_catalog"
    DUPLICATE_CATALOG_PRODUCT = "duplicate_catalog_product"
    INVALID_HARNESS_VERSION = "invalid_harness_version"
    BUNDLE_HARNESS_API_MISMATCH = "customer_bundle_harness_api_mismatch"
    UNKNOWN_PRODUCT = "unknown_product"
    VERSION_MISMATCH = "product_version_mismatch"
    HARNESS_API_MISMATCH = "product_harness_api_mismatch"
    CONFIG_INVALID = "product_config_invalid"


class CustomerBundleResolutionError(ValueError):
    """Deterministic, machine-readable customer bundle planning failure."""

    def __init__(
        self,
        code: CustomerBundleResolutionCode,
        message: str,
        **detail: object,
    ) -> None:
        self.code = code
        self.detail: Mapping[str, object] = MappingProxyType(dict(detail))
        super().__init__(message)


class ProductCatalog:
    """Immutable product descriptor catalog with one active version per product."""

    __slots__ = ("_descriptors",)

    def __init__(self, descriptors: Iterable[ProductDescriptor]) -> None:
        indexed: dict[str, ProductDescriptor] = {}
        for descriptor in descriptors:
            if not isinstance(descriptor, ProductDescriptor):
                raise CustomerBundleResolutionError(
                    CustomerBundleResolutionCode.INVALID_CATALOG,
                    f"catalog item is not a ProductDescriptor: {type(descriptor).__name__}",
                    item_type=type(descriptor).__name__,
                )
            if descriptor.product_id in indexed:
                raise CustomerBundleResolutionError(
                    CustomerBundleResolutionCode.DUPLICATE_CATALOG_PRODUCT,
                    f"duplicate catalog product id: {descriptor.product_id}",
                    product_id=descriptor.product_id,
                )
            indexed[descriptor.product_id] = descriptor
        self._descriptors: Mapping[str, ProductDescriptor] = MappingProxyType(
            dict(sorted(indexed.items()))
        )

    @property
    def descriptors(self) -> tuple[ProductDescriptor, ...]:
        return tuple(self._descriptors.values())

    @property
    def product_ids(self) -> tuple[str, ...]:
        return tuple(self._descriptors)

    def get(self, product_id: str) -> ProductDescriptor | None:
        return self._descriptors.get(product_id)


@dataclass(frozen=True, slots=True)
class ResolvedCustomerProduct:
    selection: CustomerProductSelection
    descriptor: ProductDescriptor
    config: BaseModel
    _trusted_selection: CustomerProductSelection = field(
        init=False,
        repr=False,
        compare=False,
    )
    _trusted_config: BaseModel = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        if not isinstance(self.selection, CustomerProductSelection):
            raise TypeError("selection must be a CustomerProductSelection")
        if not isinstance(self.descriptor, ProductDescriptor):
            raise TypeError("descriptor must be a ProductDescriptor")
        if not isinstance(self.config, BaseModel):
            raise TypeError("config must be a pydantic BaseModel")

        trusted_selection = self.selection.model_copy(deep=True)
        trusted_config = self.config.model_copy(deep=True)
        # Public fields preserve the original API, while a separate copy remains
        # available to the activation boundary if a consumer mutates a nested
        # list/dict through those public objects.
        object.__setattr__(self, "selection", trusted_selection.model_copy(deep=True))
        object.__setattr__(self, "config", trusted_config.model_copy(deep=True))
        object.__setattr__(self, "_trusted_selection", trusted_selection)
        object.__setattr__(self, "_trusted_config", trusted_config)

    def _snapshot(self) -> ResolvedCustomerProduct:
        """Return a detached copy sourced from the resolver-owned snapshot."""

        return ResolvedCustomerProduct(
            selection=self._trusted_selection.model_copy(deep=True),
            descriptor=self.descriptor,
            config=self._trusted_config.model_copy(deep=True),
        )

    def _config_snapshot(self) -> BaseModel:
        return self._trusted_config.model_copy(deep=True)

    @property
    def product_id(self) -> str:
        return self.descriptor.product_id

    @property
    def version(self) -> Version:
        return self.descriptor.parsed_version


@dataclass(frozen=True, slots=True)
class ResolvedCustomer360Rule:
    producer: str
    consumer: str
    predicates: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class Customer360SharingPlan:
    mode: Customer360Mode
    rules: tuple[ResolvedCustomer360Rule, ...]

    @property
    def enabled(self) -> bool:
        return self.mode is Customer360Mode.CURATED


def _validated_manifest_snapshot(
    manifest: CustomerBundleManifest,
) -> CustomerBundleManifest:
    """Re-run model validation instead of trusting a possibly forged model instance."""

    try:
        raw = manifest.model_dump(mode="python", round_trip=True)
        return CustomerBundleManifest.model_validate(raw)
    except (TypeError, ValueError, ValidationError) as exc:
        raise ValueError("customer bundle plan contains an invalid manifest") from exc


def _validated_descriptor_snapshot(descriptor: ProductDescriptor) -> ProductDescriptor:
    """Detach and revalidate descriptor metadata retained by a public plan."""

    try:
        raw = descriptor.model_dump(mode="python", round_trip=True)
        return ProductDescriptor.model_validate(raw)
    except (TypeError, ValueError, ValidationError) as exc:
        raise ValueError("customer bundle plan contains an invalid product descriptor") from exc


def _sharing_plan_from_manifest(manifest: CustomerBundleManifest) -> Customer360SharingPlan:
    rules = tuple(
        ResolvedCustomer360Rule(
            producer=rule.producer,
            consumer=rule.consumer,
            predicates=tuple(sorted(rule.predicates)),
        )
        for rule in sorted(
            manifest.customer360.rules,
            key=lambda item: (item.producer, item.consumer, tuple(sorted(item.predicates))),
        )
    )
    return Customer360SharingPlan(mode=manifest.customer360.mode, rules=rules)


def _equivalent_config(actual: BaseModel, expected: BaseModel) -> bool:
    if type(actual) is not type(expected):
        return False
    try:
        return actual.model_dump(mode="python", round_trip=True) == expected.model_dump(
            mode="python",
            round_trip=True,
        )
    except (TypeError, ValueError):
        return False


@dataclass(frozen=True, slots=True)
class CustomerBundlePlan:
    """Validated plan only; applying it is a separate privileged runtime step."""

    manifest: CustomerBundleManifest
    harness_version: Version
    products: tuple[ResolvedCustomerProduct, ...]
    sharing: Customer360SharingPlan
    _trusted_manifest: CustomerBundleManifest = field(
        init=False,
        repr=False,
        compare=False,
    )

    def __post_init__(self) -> None:
        if not isinstance(self.manifest, CustomerBundleManifest):
            raise TypeError("manifest must be a CustomerBundleManifest")
        if not isinstance(self.harness_version, Version):
            raise TypeError("harness_version must be a packaging Version")
        if not isinstance(self.products, tuple):
            raise TypeError("products must be a tuple of ResolvedCustomerProduct values")
        if not isinstance(self.sharing, Customer360SharingPlan):
            raise TypeError("sharing must be a Customer360SharingPlan")
        if any(not isinstance(product, ResolvedCustomerProduct) for product in self.products):
            raise TypeError("products must contain ResolvedCustomerProduct values")
        if not isinstance(self.sharing.mode, Customer360Mode):
            raise TypeError("sharing mode must be a Customer360Mode")
        if not isinstance(self.sharing.rules, tuple) or any(
            not isinstance(rule, ResolvedCustomer360Rule) for rule in self.sharing.rules
        ):
            raise TypeError("sharing rules must contain ResolvedCustomer360Rule values")

        trusted_manifest = _validated_manifest_snapshot(self.manifest)
        if not trusted_manifest.supports_harness(self.harness_version):
            raise ValueError("customer bundle manifest does not support the plan harness version")

        manifest_selections = {
            selection.product_id: selection for selection in trusted_manifest.products
        }
        supplied_products = tuple(product._snapshot() for product in self.products)
        for product in supplied_products:
            if product.descriptor.product_id != product.selection.product_id:
                raise ValueError(
                    "resolved product descriptor and selection product_id must match"
                )
        supplied_ids = tuple(product.selection.product_id for product in supplied_products)
        if len(supplied_ids) != len(set(supplied_ids)):
            raise ValueError("customer bundle plan products must not repeat a product_id")
        if set(supplied_ids) != set(manifest_selections):
            raise ValueError("customer bundle plan products must exactly match manifest selections")

        canonical_products: list[ResolvedCustomerProduct] = []
        for product in sorted(
            supplied_products,
            key=lambda item: item.selection.product_id,
        ):
            manifest_selection = manifest_selections[product.selection.product_id]
            if product.selection != manifest_selection:
                raise ValueError(
                    f"resolved selection does not match manifest for {product.product_id!r}"
                )

            descriptor = _validated_descriptor_snapshot(product.descriptor)
            if descriptor.product_id != manifest_selection.product_id:
                raise ValueError(
                    f"descriptor product_id does not match selection for "
                    f"{manifest_selection.product_id!r}"
                )
            if not manifest_selection.accepts(descriptor.parsed_version):
                raise ValueError(
                    f"descriptor version does not match selection for {product.product_id!r}"
                )
            if not descriptor.supports_harness(self.harness_version):
                raise ValueError(
                    f"descriptor does not support the plan harness version for "
                    f"{product.product_id!r}"
                )
            try:
                parsed_config = descriptor.parse_config(manifest_selection.config)
            except (TypeError, ValueError, ValidationError) as exc:
                raise ValueError(
                    f"manifest config is invalid for {product.product_id!r}"
                ) from exc
            if not _equivalent_config(product._config_snapshot(), parsed_config):
                raise ValueError(
                    f"resolved config does not match manifest for {product.product_id!r}"
                )
            canonical_products.append(
                ResolvedCustomerProduct(
                    selection=manifest_selection,
                    descriptor=descriptor,
                    config=parsed_config,
                )
            )

        expected_sharing = _sharing_plan_from_manifest(trusted_manifest)
        if self.sharing != expected_sharing:
            raise ValueError("customer bundle sharing plan does not match the manifest")

        object.__setattr__(self, "manifest", trusted_manifest.model_copy(deep=True))
        object.__setattr__(self, "products", tuple(canonical_products))
        object.__setattr__(self, "sharing", expected_sharing)
        object.__setattr__(self, "_trusted_manifest", trusted_manifest)

    def _snapshot(self) -> CustomerBundlePlan:
        """Create an activation-owned plan immune to mutations of public copies."""

        return CustomerBundlePlan(
            manifest=self._trusted_manifest.model_copy(deep=True),
            harness_version=self.harness_version,
            products=tuple(product._snapshot() for product in self.products),
            sharing=deepcopy(self.sharing),
        )

    @property
    def product_ids(self) -> tuple[str, ...]:
        return tuple(product.product_id for product in self.products)

    @property
    def configs(self) -> Mapping[str, BaseModel]:
        return MappingProxyType(
            {
                product.product_id: product._config_snapshot()
                for product in self.products
            }
        )

    @property
    def private_profile_product_ids(self) -> tuple[str, ...]:
        """Owners that must retain distinct private profile namespaces at runtime."""

        return self.product_ids

    @property
    def sharing_plan(self) -> Customer360SharingPlan:
        return self.sharing


def resolve_customer_bundle(
    manifest: CustomerBundleManifest | Mapping[str, Any],
    catalog: ProductCatalog,
    *,
    harness_version: str | Version,
) -> CustomerBundlePlan:
    """Validate a customer combination fully before any product is installed."""

    parsed_manifest = (
        manifest.model_copy(deep=True)
        if isinstance(manifest, CustomerBundleManifest)
        else CustomerBundleManifest.model_validate(manifest).model_copy(deep=True)
    )
    if not isinstance(catalog, ProductCatalog):
        raise CustomerBundleResolutionError(
            CustomerBundleResolutionCode.INVALID_CATALOG,
            "catalog must be a ProductCatalog",
            catalog_type=type(catalog).__name__,
        )
    try:
        runtime_version = (
            harness_version if isinstance(harness_version, Version) else Version(harness_version)
        )
    except InvalidVersion as exc:
        raise CustomerBundleResolutionError(
            CustomerBundleResolutionCode.INVALID_HARNESS_VERSION,
            f"invalid harness version: {harness_version!r}",
            harness_version=str(harness_version),
        ) from exc

    if not parsed_manifest.supports_harness(runtime_version):
        raise CustomerBundleResolutionError(
            CustomerBundleResolutionCode.BUNDLE_HARNESS_API_MISMATCH,
            f"customer bundle {parsed_manifest.customer_bundle_id!r} does not support "
            f"harness {runtime_version}",
            customer_bundle_id=parsed_manifest.customer_bundle_id,
            customer_bundle_version=parsed_manifest.version,
            required=parsed_manifest.harness_api,
            actual=str(runtime_version),
        )

    resolved: list[ResolvedCustomerProduct] = []
    for selection in sorted(parsed_manifest.products, key=lambda item: item.product_id):
        descriptor = catalog.get(selection.product_id)
        if descriptor is None:
            raise CustomerBundleResolutionError(
                CustomerBundleResolutionCode.UNKNOWN_PRODUCT,
                f"unknown product: {selection.product_id}",
                product_id=selection.product_id,
            )
        if not selection.accepts(descriptor.parsed_version):
            raise CustomerBundleResolutionError(
                CustomerBundleResolutionCode.VERSION_MISMATCH,
                f"product {selection.product_id!r} does not match {selection.version}",
                product_id=selection.product_id,
                required=selection.version,
                actual=descriptor.version,
            )
        if not descriptor.supports_harness(runtime_version):
            raise CustomerBundleResolutionError(
                CustomerBundleResolutionCode.HARNESS_API_MISMATCH,
                f"product {selection.product_id!r} does not support harness {runtime_version}",
                product_id=selection.product_id,
                required=descriptor.harness_api,
                actual=str(runtime_version),
            )
        try:
            config = descriptor.parse_config(selection.config)
        except ValidationError as exc:
            raise CustomerBundleResolutionError(
                CustomerBundleResolutionCode.CONFIG_INVALID,
                f"invalid config for product {selection.product_id!r}",
                product_id=selection.product_id,
                errors=tuple(exc.errors(include_url=False)),
            ) from exc
        resolved.append(
            ResolvedCustomerProduct(
                selection=selection,
                descriptor=descriptor,
                config=config,
            )
        )

    return CustomerBundlePlan(
        manifest=parsed_manifest,
        harness_version=runtime_version,
        products=tuple(resolved),
        sharing=_sharing_plan_from_manifest(parsed_manifest),
    )


__all__ = [
    "Customer360Mode",
    "Customer360SharingManifest",
    "Customer360SharingPlan",
    "Customer360SharingRule",
    "CustomerBundleManifest",
    "CustomerBundlePlan",
    "CustomerBundleResolutionCode",
    "CustomerBundleResolutionError",
    "CustomerProductSelection",
    "ProductCatalog",
    "ResolvedCustomer360Rule",
    "ResolvedCustomerProduct",
    "resolve_customer_bundle",
]
