"""Bundle planning and transactional installation for the scoped plugin kernel."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, field
from enum import Enum
from types import MappingProxyType
from typing import Protocol, TypeVar, runtime_checkable

from packaging.version import InvalidVersion, Version
from pydantic import BaseModel

from suiteharness.runtime.descriptor import (
    BundleManifest,
    BundleRequirement,
    ProductDescriptor,
)
from suiteharness.runtime.effects import Disposer, EffectCleanupError, EffectScope
from suiteharness.runtime.scopes import (
    ProductContext,
    ScopePath,
    ServiceBindings,
    ServiceKey,
)

InstallResourceT = TypeVar("InstallResourceT")
InstallServiceT = TypeVar("InstallServiceT")


@dataclass(frozen=True, slots=True)
class BundleInstallContext:
    """Narrow installation view owned by one bundle effect transaction.

    The full :class:`ProductContext` and :class:`EffectScope` are deliberately
    not public.  Exposing either would let ordinary bundle code walk to a
    parent scope and accidentally register cleanup outside its transaction.
    In-process bundles remain trusted Python code; this API is an engineering
    boundary, not a sandbox.
    """

    path: ScopePath
    config: BaseModel
    _bindings: ServiceBindings = field(repr=False, compare=False)
    _effects: EffectScope = field(repr=False, compare=False)

    def resolve(self, key: ServiceKey[InstallServiceT]) -> InstallServiceT:
        """Resolve a service visible from this product without exposing bindings."""

        return self._bindings.resolve(key)

    def own(self, label: str, dispose: Disposer) -> None:
        """Attach an installed side effect to this bundle transaction."""

        self._effects.callback(label, dispose)

    async def enter(
        self,
        label: str,
        resource: AbstractAsyncContextManager[InstallResourceT],
    ) -> InstallResourceT:
        """Enter and own an async resource in this bundle transaction."""

        return await self._effects.enter(label, resource)


@runtime_checkable
class Bundle(Protocol):
    """New bundle contract: metadata first, all installation effects scoped."""

    manifest: BundleManifest

    async def install(self, context: BundleInstallContext) -> None: ...


class BundleResolutionCode(str, Enum):
    DUPLICATE = "duplicate_bundle"
    MISSING = "missing_dependency"
    VERSION = "version_mismatch"
    HARNESS_API = "harness_api_mismatch"
    CONFLICT = "bundle_conflict"
    CYCLE = "dependency_cycle"
    PROVIDER_CONFLICT = "provider_conflict"
    CAPABILITY_UNDECLARED = "capability_undeclared"
    INVALID_BUNDLE = "invalid_bundle"


class BundleResolutionError(ValueError):
    """Deterministic, machine-readable bundle planning failure."""

    def __init__(
        self,
        code: BundleResolutionCode,
        message: str,
        **detail: object,
    ) -> None:
        self.code = code
        self.detail: Mapping[str, object] = MappingProxyType(dict(detail))
        super().__init__(message)


class BundleInstallError(RuntimeError):
    """Installation failed and rollback also reported cleanup failures."""

    def __init__(self, cause: BaseException, cleanup: EffectCleanupError) -> None:
        self.install_cause = cause
        self.cleanup_error = cleanup
        super().__init__("bundle installation failed and rollback was incomplete")


@dataclass(frozen=True, slots=True)
class BundlePlan:
    product: ProductDescriptor
    harness_version: Version
    bundles: tuple[Bundle, ...]

    @property
    def bundle_ids(self) -> tuple[str, ...]:
        return tuple(bundle.manifest.bundle_id for bundle in self.bundles)


@dataclass(frozen=True, slots=True)
class BundleActivation:
    """Installed bundle set; its scope is also owned by ProductContext."""

    plan: BundlePlan
    effects: EffectScope

    async def close(self) -> None:
        await self.effects.close()


def resolve_bundle_plan(
    product: ProductDescriptor,
    available: Iterable[Bundle],
    *,
    harness_version: str | Version,
) -> BundlePlan:
    """Select the product closure and return dependencies before dependants.

    Optional dependencies are included when present.  If present but incompatible,
    they fail validation rather than being silently ignored.
    """

    try:
        runtime_version = (
            harness_version if isinstance(harness_version, Version) else Version(harness_version)
        )
    except InvalidVersion as exc:
        raise BundleResolutionError(
            BundleResolutionCode.VERSION,
            f"invalid harness version: {harness_version!r}",
            harness_version=str(harness_version),
        ) from exc

    if not product.supports_harness(runtime_version):
        raise BundleResolutionError(
            BundleResolutionCode.HARNESS_API,
            f"product {product.product_id!r} does not support harness {runtime_version}",
            product_id=product.product_id,
            required=product.harness_api,
            actual=str(runtime_version),
        )

    catalog: dict[str, Bundle] = {}
    for bundle in available:
        manifest = getattr(bundle, "manifest", None)
        install = getattr(bundle, "install", None)
        if not isinstance(manifest, BundleManifest) or not callable(install):
            raise BundleResolutionError(
                BundleResolutionCode.INVALID_BUNDLE,
                f"object is not a valid bundle: {type(bundle).__name__}",
                bundle_type=type(bundle).__name__,
            )
        if manifest.bundle_id in catalog:
            raise BundleResolutionError(
                BundleResolutionCode.DUPLICATE,
                f"duplicate bundle id: {manifest.bundle_id}",
                bundle_id=manifest.bundle_id,
            )
        catalog[manifest.bundle_id] = bundle

    selected: dict[str, Bundle] = {}
    visiting: list[str] = []
    resolved: set[str] = set()
    order: list[Bundle] = []

    def visit(requirement: BundleRequirement, source: str) -> None:
        bundle = catalog.get(requirement.bundle_id)
        if bundle is None:
            if requirement.optional:
                return
            raise BundleResolutionError(
                BundleResolutionCode.MISSING,
                f"{source!r} requires missing bundle {requirement.bundle_id!r}",
                source=source,
                bundle_id=requirement.bundle_id,
                required=requirement.version,
            )

        manifest = bundle.manifest
        if not requirement.accepts(manifest.parsed_version):
            raise BundleResolutionError(
                BundleResolutionCode.VERSION,
                f"{source!r} requires {requirement.bundle_id!r}{requirement.version}, "
                f"found {manifest.version}",
                source=source,
                bundle_id=requirement.bundle_id,
                required=requirement.version,
                actual=manifest.version,
            )
        if not manifest.supports_harness(runtime_version):
            raise BundleResolutionError(
                BundleResolutionCode.HARNESS_API,
                f"bundle {manifest.bundle_id!r} does not support harness {runtime_version}",
                bundle_id=manifest.bundle_id,
                required=manifest.harness_api,
                actual=str(runtime_version),
            )
        if manifest.bundle_id in visiting:
            start = visiting.index(manifest.bundle_id)
            cycle = tuple((*visiting[start:], manifest.bundle_id))
            raise BundleResolutionError(
                BundleResolutionCode.CYCLE,
                f"bundle dependency cycle: {' -> '.join(cycle)}",
                cycle=cycle,
            )
        if manifest.bundle_id in resolved:
            return

        selected[manifest.bundle_id] = bundle
        visiting.append(manifest.bundle_id)
        for dependency in manifest.requires:
            visit(dependency, manifest.bundle_id)
        visiting.pop()
        resolved.add(manifest.bundle_id)
        order.append(bundle)

    for requirement in product.bundles:
        visit(requirement, product.product_id)

    for bundle in order:
        manifest = bundle.manifest
        undeclared = manifest.capabilities - product.capabilities
        if undeclared:
            raise BundleResolutionError(
                BundleResolutionCode.CAPABILITY_UNDECLARED,
                f"bundle {manifest.bundle_id!r} requests undeclared product capabilities",
                bundle_id=manifest.bundle_id,
                capabilities=tuple(sorted(undeclared)),
            )
        for conflict in manifest.conflicts:
            target = selected.get(conflict.bundle_id)
            if target is not None and conflict.matches(target.manifest.parsed_version):
                raise BundleResolutionError(
                    BundleResolutionCode.CONFLICT,
                    f"bundle {manifest.bundle_id!r} conflicts with {conflict.bundle_id!r}",
                    bundle_id=manifest.bundle_id,
                    conflict_id=conflict.bundle_id,
                    conflict_version=conflict.version,
                    actual=target.manifest.version,
                )

    provider_owner: dict[str, str] = {}
    for bundle in order:
        for service in sorted(bundle.manifest.provides):
            previous = provider_owner.get(service)
            if previous is not None:
                raise BundleResolutionError(
                    BundleResolutionCode.PROVIDER_CONFLICT,
                    f"service {service!r} is provided by both {previous!r} "
                    f"and {bundle.manifest.bundle_id!r}",
                    service=service,
                    providers=(previous, bundle.manifest.bundle_id),
                )
            provider_owner[service] = bundle.manifest.bundle_id

    return BundlePlan(product=product, harness_version=runtime_version, bundles=tuple(order))


async def install_bundle_plan(
    plan: BundlePlan,
    product: ProductContext,
    config: BaseModel,
) -> BundleActivation:
    """Install a plan as one effect transaction owned by the product activation."""

    if product.path.product_id != plan.product.product_id:
        raise ValueError("bundle plan product does not match ProductContext")
    if not isinstance(config, plan.product.config_model):
        raise TypeError(
            f"config must be an instance of {plan.product.config_model.__name__}"
        )

    transaction = product.effects.child(f"bundles:{plan.product.product_id}")
    try:
        for bundle in plan.bundles:
            bundle_scope = transaction.child(f"bundle:{bundle.manifest.bundle_id}")
            await bundle.install(
                BundleInstallContext(
                    path=product.path,
                    config=config,
                    _bindings=product.bindings,
                    _effects=bundle_scope,
                )
            )
    except BaseException as exc:
        try:
            await transaction.close()
        except EffectCleanupError as cleanup:
            raise BundleInstallError(exc, cleanup) from exc
        raise
    return BundleActivation(plan=plan, effects=transaction)
