"""Trusted activation boundary for resolved customer product combinations.

Static product packages only describe metadata.  This module is the separate,
privileged step that turns an already resolved :class:`CustomerBundlePlan` into
tenant and product scopes.  It deliberately does not discover or import product
code, and it never derives capability grants from product-controlled metadata.

``ProductActivator.prepare`` is a staging contract: it may allocate resources,
but it must not publish routes, tools, tasks or other externally visible effects.
It returns a cleanup callback for those staged resources.  Visible installation
happens later through ``ProductActivationContext`` and every registered effect is
owned by the product activation's :class:`~suiteharness.runtime.effects.EffectScope`.
"""

from __future__ import annotations

import asyncio
import secrets
from collections.abc import Awaitable, Callable, Iterable, Mapping
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, field
from enum import Enum
from types import MappingProxyType
from typing import Any, Protocol, TypeAlias, TypeVar, runtime_checkable

from pydantic import BaseModel

from suiteharness.execution.in_memory import (
    InMemoryApprovalStore,
    InMemoryAuditJournal,
    InMemoryCapabilityAuthority,
    InMemoryToolRegistry,
)
from suiteharness.execution.models import RunRequest, RunResult, ToolSpec
from suiteharness.execution.protocols import (
    ApprovalStore,
    AuditJournal,
    CapabilityAuthority,
    ExecutionStrategies,
    InteractiveApprovalBroker,
    ToolAuthorizationPolicy,
    ToolHandler,
    ToolRegistry,
)
from suiteharness.execution.runner import ExecutionPolicy, ExecutionRunner
from suiteharness.memory.in_memory import (
    InMemoryCustomer360Provider,
    InMemoryProfileDirectory,
)
from suiteharness.memory.models import SHARING_ADMIN
from suiteharness.memory.protocols import (
    Customer360Provider,
    ProfileProvider,
    ProfileProviderResolver,
)
from suiteharness.memory.services import CUSTOMER360_PROVIDER, PROFILE_PROVIDER
from suiteharness.runtime.bundle import (
    Bundle,
    BundlePlan,
    BundleResolutionError,
    install_bundle_plan,
    resolve_bundle_plan,
)
from suiteharness.runtime.customer_bundle import (
    CustomerBundlePlan,
    ResolvedCustomerProduct,
)
from suiteharness.runtime.descriptor import ProductDescriptor
from suiteharness.runtime.effects import EffectCleanupError, EffectScope, EffectScopeState
from suiteharness.runtime.scopes import (
    ProductContext,
    RequestScope,
    RootContext,
    ScopeKind,
    ScopePath,
    ServiceBindingError,
    ServiceBindings,
    ServiceKey,
    TenantScope,
)

Disposer: TypeAlias = Callable[[], Awaitable[None] | None]
ProductInstaller: TypeAlias = Callable[["ProductActivationContext"], Awaitable[None]]
Customer360Factory: TypeAlias = Callable[[ProfileProviderResolver], Customer360Provider]
InstallResourceT = TypeVar("InstallResourceT")
InstallServiceT = TypeVar("InstallServiceT")


@runtime_checkable
class ScopedToolRegistry(ToolRegistry, Protocol):
    """Root-owned registry surface exposed narrowly during trusted install."""

    def register_owned(
        self,
        scope: ScopePath,
        effects: EffectScope,
        spec: ToolSpec,
        handler: ToolHandler,
    ) -> object: ...


@dataclass(frozen=True, slots=True)
class PreparedProduct:
    """Side-effect-free preparation result for one resolved product.

    ``cleanup`` owns resources allocated while preparing the immutable service
    bindings.  An externally managed resource may omit it, but activation-owned
    database clients, provider adapters, threads and processes should always
    supply one.
    """

    descriptor: ProductDescriptor
    config: BaseModel
    bindings: Mapping[ServiceKey[Any], object]
    install: ProductInstaller | None = None
    cleanup: Disposer | None = None
    bundles: tuple[Bundle, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.descriptor, ProductDescriptor):
            raise TypeError("prepared descriptor must be a ProductDescriptor")
        if not isinstance(self.config, BaseModel):
            raise TypeError("prepared config must be a pydantic BaseModel")
        if not isinstance(self.bindings, Mapping):
            raise TypeError("prepared bindings must be a mapping")
        if self.install is not None and not callable(self.install):
            raise TypeError("prepared install must be callable")
        if self.cleanup is not None and not callable(self.cleanup):
            raise TypeError("prepared cleanup must be callable")
        if not isinstance(self.bundles, tuple):
            raise TypeError("prepared bundles must be a tuple")
        object.__setattr__(self, "bindings", MappingProxyType(dict(self.bindings)))


@runtime_checkable
class ProductActivator(Protocol):
    """Trusted activation code, intentionally separate from descriptor packages."""

    descriptor: ProductDescriptor

    async def prepare(self, product: ResolvedCustomerProduct) -> PreparedProduct: ...


@dataclass(frozen=True, slots=True)
class ProductActivationContext:
    """Narrow installation view for one product activation.

    Products can publish tools only at their own product path and can own other
    effects only in their installation transaction.  The full ProductContext,
    EffectScope, grant authority and approval authority are intentionally absent.
    This protects against accidental scope escape by trusted in-process code; it
    is not a sandbox for hostile Python.
    """

    path: ScopePath
    config: BaseModel
    _bindings: ServiceBindings = field(repr=False, compare=False)
    _effects: EffectScope = field(repr=False, compare=False)
    _tools: ScopedToolRegistry = field(repr=False, compare=False)
    _declared_capabilities: frozenset[str] = field(repr=False, compare=False)

    def register_tool(self, spec: ToolSpec, handler: ToolHandler) -> object:
        if not isinstance(spec, ToolSpec):
            raise TypeError("spec must be a ToolSpec")
        undeclared = spec.required_capabilities - self._declared_capabilities
        if undeclared:
            raise CustomerBundleActivationError(
                CustomerBundleActivationCode.TOOL_CAPABILITY_UNDECLARED,
                f"tool {spec.name!r} requires capabilities not declared by its product",
                product_id=self.path.product_id,
                tool_name=spec.name,
                undeclared_capabilities=tuple(sorted(undeclared)),
                declared_capabilities=tuple(sorted(self._declared_capabilities)),
            )
        return self._tools.register_owned(
            self.path,
            self._effects,
            spec,
            handler,
        )

    def resolve(self, key: ServiceKey[InstallServiceT]) -> InstallServiceT:
        """Resolve a service visible to this product without exposing bindings."""

        return self._bindings.resolve(key)

    def own(self, label: str, dispose: Disposer) -> None:
        """Attach any other installed side effect to this product transaction."""

        self._effects.callback(label, dispose)

    async def enter(
        self,
        label: str,
        resource: AbstractAsyncContextManager[InstallResourceT],
    ) -> InstallResourceT:
        """Enter and own an async resource in this product transaction."""

        return await self._effects.enter(label, resource)


class CustomerBundleActivationCode(str, Enum):
    INVALID_PLAN = "invalid_plan"
    INVALID_SCOPE = "invalid_scope"
    ACTIVATION_CONFLICT = "activation_conflict"
    KERNEL_CLOSED = "kernel_closed"
    INVALID_ACTIVATOR = "invalid_activator"
    DUPLICATE_ACTIVATOR = "duplicate_activator"
    MISSING_ACTIVATOR = "missing_activator"
    EXTRA_ACTIVATOR = "extra_activator"
    DESCRIPTOR_MISMATCH = "descriptor_mismatch"
    PREPARE_FAILED = "prepare_failed"
    INVALID_PREPARED_PRODUCT = "invalid_prepared_product"
    CONFIG_MISMATCH = "config_mismatch"
    BUNDLE_RESOLUTION_FAILED = "bundle_resolution_failed"
    BUNDLE_INSTALL_FAILED = "bundle_install_failed"
    INVALID_BINDING = "invalid_binding"
    PROFILE_REQUIRED = "profile_provider_required"
    PROFILE_INSTANCE_REUSED = "profile_provider_instance_reused"
    CUSTOMER360_INVALID = "customer360_invalid"
    POLICY_CONFIGURATION_FAILED = "policy_configuration_failed"
    INSTALL_FAILED = "install_failed"
    TOOL_CAPABILITY_UNDECLARED = "tool_capability_undeclared"
    INVALID_RUN_REQUEST = "invalid_run_request"
    RUN_SCOPE_MISMATCH = "run_scope_mismatch"
    WORKFLOW_UNAVAILABLE = "workflow_unavailable"
    ACTIVATION_CLOSED = "activation_closed"


class CustomerBundleActivationError(RuntimeError):
    """Machine-readable activation failure, including incomplete rollback."""

    def __init__(
        self,
        code: CustomerBundleActivationCode,
        message: str,
        *,
        cause: BaseException | None = None,
        cleanup_error: EffectCleanupError | None = None,
        **detail: object,
    ) -> None:
        self.code = code
        self.activation_cause = cause
        self.cleanup_error = cleanup_error
        self.detail: Mapping[str, object] = MappingProxyType(dict(detail))
        suffix = " and rollback was incomplete" if cleanup_error is not None else ""
        super().__init__(f"{message}{suffix}")

    def with_cleanup(self, cleanup: EffectCleanupError) -> CustomerBundleActivationError:
        return CustomerBundleActivationError(
            self.code,
            str(self).removesuffix(" and rollback was incomplete"),
            cause=self.activation_cause,
            cleanup_error=cleanup,
            **dict(self.detail),
        )


@dataclass(slots=True)
class _ActivationSlot:
    activation_id: str
    activation: ActivatedCustomerBundle | None = None
    ready: asyncio.Event = field(default_factory=asyncio.Event)


class _RunLease:
    """Keep product resources alive while accepted runs are in flight."""

    def __init__(self) -> None:
        self._active = 0
        self._lock = asyncio.Lock()
        self._drained = asyncio.Event()
        self._drained.set()

    async def enter(self, tenant: TenantScope, product: ProductContext) -> bool:
        async with self._lock:
            if (
                tenant.effects.state is not EffectScopeState.OPEN
                or product.effects.state is not EffectScopeState.OPEN
            ):
                return False
            self._active += 1
            self._drained.clear()
            return True

    async def leave(self) -> None:
        async with self._lock:
            self._active -= 1
            if self._active == 0:
                self._drained.set()

    async def drain(self) -> None:
        await self._drained.wait()


@dataclass(frozen=True, slots=True)
class ActivatedCustomerBundle:
    """The single active customer bundle for one tenant."""

    plan: CustomerBundlePlan
    tenant: TenantScope
    products: Mapping[str, ProductContext]
    runner: ExecutionRunner
    _kernel: HarnessKernel = field(repr=False, compare=False)
    _activation_id: str = field(repr=False, compare=False)
    _run_leases: Mapping[str, _RunLease] = field(repr=False, compare=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "products", MappingProxyType(dict(self.products)))
        object.__setattr__(self, "_run_leases", MappingProxyType(dict(self._run_leases)))

    @property
    def tenant_id(self) -> str:
        return str(self.tenant.path.tenant_id)

    @property
    def customer_bundle_id(self) -> str:
        return self.plan.manifest.customer_bundle_id

    @property
    def closed(self) -> bool:
        return self.tenant.effects.closed

    def product(self, product_id: str) -> ProductContext:
        try:
            return self.products[product_id]
        except KeyError as exc:
            raise LookupError(f"product is not active in this customer bundle: {product_id}") from exc

    def strategies(self, product_id: str) -> ExecutionStrategies:
        """Resolve product-overridable strategies; the runner remains fixed."""

        return ExecutionStrategies.from_bindings(self.product(product_id).bindings)

    async def run(self, request: RunRequest) -> RunResult:
        """Run with strategies selected from the request's exact product scope.

        This is the application-facing path.  It prevents a host adapter from
        accidentally combining product A's authenticated scope with product B's
        workflow, prompt or reflection strategy.  Capability grants are still
        issued separately by the trusted control plane.
        """

        if not isinstance(request, RunRequest):
            raise CustomerBundleActivationError(
                CustomerBundleActivationCode.INVALID_RUN_REQUEST,
                "request must be a validated RunRequest",
                request_type=type(request).__name__,
            )
        if self.tenant.effects.state is not EffectScopeState.OPEN:
            raise CustomerBundleActivationError(
                CustomerBundleActivationCode.ACTIVATION_CLOSED,
                "customer bundle activation is closed",
                tenant_id=self.tenant_id,
            )
        path = request.scope.path
        product = self.products.get(request.scope.product_id)
        belongs_to_product = product is not None and (
            (path.kind is ScopeKind.PRODUCT and path == product.path)
            or (path.kind is ScopeKind.AGENT and path.parent() == product.path)
        )
        if request.scope.tenant_id != self.tenant_id or not belongs_to_product:
            raise CustomerBundleActivationError(
                CustomerBundleActivationCode.RUN_SCOPE_MISMATCH,
                "run scope does not belong to this customer bundle activation",
                expected_tenant_id=self.tenant_id,
                selected_product_ids=tuple(self.products),
                actual_tenant_id=request.scope.tenant_id,
                actual_product_id=request.scope.product_id,
                actual_scope_kind=path.kind.value,
            )
        assert product is not None
        lease = self._run_leases[request.scope.product_id]
        if not await lease.enter(self.tenant, product):
            raise CustomerBundleActivationError(
                CustomerBundleActivationCode.ACTIVATION_CLOSED,
                "customer bundle product activation is closing or closed",
                tenant_id=self.tenant_id,
                product_id=request.scope.product_id,
            )
        try:
            try:
                strategies = self.strategies(request.scope.product_id)
            except (LookupError, ServiceBindingError, TypeError) as exc:
                raise CustomerBundleActivationError(
                    CustomerBundleActivationCode.WORKFLOW_UNAVAILABLE,
                    f"execution workflow is unavailable for product {request.scope.product_id!r}",
                    cause=exc,
                    product_id=request.scope.product_id,
                ) from exc
            return await self.runner.run(request, strategies)
        finally:
            await lease.leave()

    async def close(self) -> None:
        try:
            await self.tenant.close()
        except EffectCleanupError as cleanup:
            await self._kernel._finish_close(
                self.tenant_id,
                self._activation_id,
                cleanup,
            )
            raise
        await self._kernel._finish_close(self.tenant_id, self._activation_id)


class HarnessKernel:
    """Root-owned composer and non-replaceable execution boundary.

    The in-memory infrastructure defaults make the contract executable in tests
    and local prototypes.  A production host should inject durable capability,
    approval and audit adapters.  Product activation never issues a grant: that
    remains a trusted control-plane decision after activation succeeds.
    """

    def __init__(
        self,
        *,
        root: RootContext | None = None,
        tools: ScopedToolRegistry | None = None,
        capabilities: CapabilityAuthority | None = None,
        approvals: ApprovalStore | None = None,
        journal: AuditJournal | None = None,
        execution_policy: ExecutionPolicy | None = None,
        tool_authorization_policy: ToolAuthorizationPolicy | None = None,
        interactive_approvals: InteractiveApprovalBroker | None = None,
        customer360_factory: Customer360Factory | None = None,
    ) -> None:
        if root is not None and not isinstance(root, RootContext):
            raise TypeError("root must be a RootContext")
        self._root = root if root is not None else RootContext()
        selected_tools = tools if tools is not None else InMemoryToolRegistry()
        if not isinstance(selected_tools, ScopedToolRegistry):
            raise TypeError("tools must support scoped, effect-owned registration")
        self._tools = selected_tools
        self._capabilities = (
            capabilities if capabilities is not None else InMemoryCapabilityAuthority()
        )
        self._approvals = approvals if approvals is not None else InMemoryApprovalStore()
        self._journal = journal if journal is not None else InMemoryAuditJournal()
        self._runner = ExecutionRunner(
            tools=self._tools,
            capabilities=self._capabilities,
            approvals=self._approvals,
            journal=self._journal,
            policy=execution_policy,
            authorization_policy=tool_authorization_policy,
            interactive_approvals=interactive_approvals,
        )
        self._customer360_factory = customer360_factory or InMemoryCustomer360Provider
        self._slots: dict[str, _ActivationSlot] = {}
        self._poisoned: dict[str, EffectCleanupError] = {}
        self._lock = asyncio.Lock()
        self._closed = False

    @property
    def root(self) -> RootContext:
        return self._root

    @property
    def runner(self) -> ExecutionRunner:
        return self._runner

    @property
    def tools(self) -> ToolRegistry:
        return self._tools

    @property
    def capabilities(self) -> CapabilityAuthority:
        return self._capabilities

    @property
    def approvals(self) -> ApprovalStore:
        return self._approvals

    @property
    def journal(self) -> AuditJournal:
        return self._journal

    async def active(
        self,
        tenant_id: str,
    ) -> ActivatedCustomerBundle | None:
        async with self._lock:
            slot = self._slots.get(tenant_id)
            return None if slot is None else slot.activation

    async def close(self) -> None:
        """Stop new activations, await in-flight staging, then close the root tree."""

        async with self._lock:
            self._closed = True
            slots = tuple(self._slots.items())
        if slots:
            await asyncio.gather(*(slot.ready.wait() for _, slot in slots))
        try:
            await self._root.close()
        except EffectCleanupError as cleanup:
            for tenant_id, slot in slots:
                await self._finish_close(tenant_id, slot.activation_id, cleanup)
            raise
        for tenant_id, slot in slots:
            await self._finish_close(tenant_id, slot.activation_id)

    async def activate_customer_bundle(
        self,
        tenant_id: str,
        plan: CustomerBundlePlan,
        activators: Iterable[ProductActivator],
    ) -> ActivatedCustomerBundle:
        """Prepare, validate and transactionally install one customer bundle."""

        if not isinstance(plan, CustomerBundlePlan):
            raise CustomerBundleActivationError(
                CustomerBundleActivationCode.INVALID_PLAN,
                "plan must be a resolved CustomerBundlePlan",
                plan_type=type(plan).__name__,
            )
        try:
            # Never stage directly from public plan objects: frozen dataclasses
            # and Pydantic models do not recursively freeze nested dict/list
            # values.  The resolver keeps independent trusted snapshots for this
            # exact boundary.
            plan = plan._snapshot()
        except (KeyboardInterrupt, SystemExit, asyncio.CancelledError):
            raise
        except BaseException as exc:
            raise CustomerBundleActivationError(
                CustomerBundleActivationCode.INVALID_PLAN,
                "customer bundle plan could not be snapshotted",
                cause=exc,
            ) from exc
        try:
            ScopePath.product(tenant_id, plan.product_ids[0])
        except (IndexError, TypeError, ValueError) as exc:
            raise CustomerBundleActivationError(
                CustomerBundleActivationCode.INVALID_SCOPE,
                "tenant_id must identify a valid product scope",
                cause=exc,
                tenant_id=tenant_id,
            ) from exc

        activator_map = self._validate_activators(plan, activators)
        activation_id = await self._reserve(tenant_id)

        prepared: list[tuple[ResolvedCustomerProduct, PreparedProduct]] = []
        expected_configs = {
            product.product_id: product._config_snapshot()
            for product in plan.products
        }
        tenant: TenantScope | None = None
        staged_disposers: list[tuple[str, Disposer]] = []
        try:
            for resolved in plan.products:
                activator = activator_map[resolved.product_id]
                try:
                    returned_item = await activator.prepare(resolved)
                except (KeyboardInterrupt, SystemExit, asyncio.CancelledError):
                    raise
                except BaseException as exc:
                    raise CustomerBundleActivationError(
                        CustomerBundleActivationCode.PREPARE_FAILED,
                        f"product {resolved.product_id!r} preparation failed",
                        cause=exc,
                        product_id=resolved.product_id,
                    ) from exc
                if not isinstance(returned_item, PreparedProduct):
                    raise CustomerBundleActivationError(
                        CustomerBundleActivationCode.INVALID_PREPARED_PRODUCT,
                        f"product {resolved.product_id!r} returned an invalid preparation result",
                        product_id=resolved.product_id,
                        prepared_type=type(returned_item).__name__,
                    )
                if returned_item.cleanup is not None:
                    staged_disposers.append(
                        (f"prepared:{resolved.product_id}", returned_item.cleanup)
                    )
                try:
                    # The activator may retain the returned object.  Keep a
                    # kernel-owned config copy so later nested mutation cannot
                    # alter validation or installation state.
                    item = PreparedProduct(
                        descriptor=returned_item.descriptor,
                        config=returned_item.config.model_copy(deep=True),
                        bindings=returned_item.bindings,
                        install=returned_item.install,
                        cleanup=returned_item.cleanup,
                        bundles=tuple(returned_item.bundles),
                    )
                except (KeyboardInterrupt, SystemExit, asyncio.CancelledError):
                    raise
                except BaseException as exc:
                    raise CustomerBundleActivationError(
                        CustomerBundleActivationCode.INVALID_PREPARED_PRODUCT,
                        f"product {resolved.product_id!r} preparation could not be snapshotted",
                        cause=exc,
                        product_id=resolved.product_id,
                    ) from exc
                prepared.append((resolved, item))

            base_tenant_bindings = self._root.bindings.derive(ScopeKind.TENANT)
            product_profiles: dict[str, ProfileProvider] = {}
            self._validate_prepared(
                prepared,
                tenant_bindings=base_tenant_bindings,
                profiles=product_profiles,
                expected_configs=expected_configs,
            )
            bundle_plans: dict[str, BundlePlan] = {}
            for resolved, item in prepared:
                try:
                    bundle_plans[resolved.product_id] = resolve_bundle_plan(
                        resolved.descriptor,
                        item.bundles,
                        harness_version=plan.harness_version,
                    )
                except (KeyboardInterrupt, SystemExit, asyncio.CancelledError):
                    raise
                except BundleResolutionError as exc:
                    raise CustomerBundleActivationError(
                        CustomerBundleActivationCode.BUNDLE_RESOLUTION_FAILED,
                        f"runtime bundles could not be resolved for product "
                        f"{resolved.product_id!r}",
                        cause=exc,
                        product_id=resolved.product_id,
                        bundle_resolution_code=exc.code.value,
                        bundle_resolution_detail=dict(exc.detail),
                        required_bundle_ids=tuple(
                            requirement.bundle_id
                            for requirement in resolved.descriptor.bundles
                        ),
                    ) from exc
                except BaseException as exc:
                    raise CustomerBundleActivationError(
                        CustomerBundleActivationCode.BUNDLE_RESOLUTION_FAILED,
                        f"runtime bundle resolution failed for product "
                        f"{resolved.product_id!r}",
                        cause=exc,
                        product_id=resolved.product_id,
                    ) from exc

            tenant_values: dict[ServiceKey[Any], object] = {}
            customer360: Customer360Provider | None = None
            if plan.sharing.enabled:
                directory = InMemoryProfileDirectory()
                for product_id, provider in product_profiles.items():
                    directory.register(tenant_id, product_id, provider)
                try:
                    customer360 = self._customer360_factory(directory)
                except (KeyboardInterrupt, SystemExit, asyncio.CancelledError):
                    raise
                except BaseException as exc:
                    raise CustomerBundleActivationError(
                        CustomerBundleActivationCode.CUSTOMER360_INVALID,
                        "Customer360 provider construction failed",
                        cause=exc,
                    ) from exc
                close_customer360 = getattr(customer360, "close", None)
                if callable(close_customer360):
                    staged_disposers.append(("customer360", close_customer360))
                if not isinstance(customer360, Customer360Provider):
                    raise CustomerBundleActivationError(
                        CustomerBundleActivationCode.CUSTOMER360_INVALID,
                        "Customer360 factory returned an invalid provider",
                        provider_type=type(customer360).__name__,
                    )
                tenant_values[CUSTOMER360_PROVIDER] = customer360

            try:
                tenant_bindings = self._root.bindings.derive(
                    ScopeKind.TENANT,
                    tenant_values,
                )
                # Revalidate against the final tenant layer so a product cannot
                # shadow Customer360 or any other tenant-owned service.
                for _, item in prepared:
                    tenant_bindings.derive(ScopeKind.PRODUCT, item.bindings)
            except (ServiceBindingError, TypeError) as exc:
                raise CustomerBundleActivationError(
                    CustomerBundleActivationCode.INVALID_BINDING,
                    "prepared product contains an invalid scoped service binding",
                    cause=exc,
                ) from exc

            tenant = self._root.tenant(tenant_id, tenant_values)
            if customer360 is not None:
                tenant.effects.callback("customer360", customer360.close)
                staged_disposers = [
                    item for item in staged_disposers if item[0] != "customer360"
                ]

            try:
                await self._capabilities.bind_activation(
                    tenant_id,
                    plan.product_ids,
                    activation_id,
                )
                tenant.effects.callback(
                    "capability-grants",
                    lambda: self._capabilities.release_activation(activation_id),
                )
            except (KeyboardInterrupt, SystemExit, asyncio.CancelledError):
                raise
            except BaseException as exc:
                # If binding succeeded but ownership registration failed, release is
                # exact-token guarded and therefore safe to call defensively.
                await self._capabilities.release_activation(activation_id)
                raise CustomerBundleActivationError(
                    CustomerBundleActivationCode.INSTALL_FAILED,
                    "capability activation binding failed",
                    cause=exc,
                    tenant_id=tenant_id,
                ) from exc

            products: dict[str, ProductContext] = {}
            for resolved, item in prepared:
                product = tenant.product(
                    resolved.product_id,
                    item.bindings,
                )
                products[resolved.product_id] = product
                if item.cleanup is not None:
                    product.effects.callback(
                        f"prepared:{resolved.product_id}",
                        item.cleanup,
                    )
                    staged_disposers = [
                        staged
                        for staged in staged_disposers
                        if staged[0] != f"prepared:{resolved.product_id}"
                    ]

            if customer360 is not None:
                try:
                    self._configure_sharing_policies(
                        tenant_id,
                        plan,
                        products,
                        customer360,
                    )
                except (KeyboardInterrupt, SystemExit, asyncio.CancelledError):
                    raise
                except BaseException as exc:
                    raise CustomerBundleActivationError(
                        CustomerBundleActivationCode.POLICY_CONFIGURATION_FAILED,
                        "Customer360 policy configuration failed",
                        cause=exc,
                    ) from exc

            # Resolve every product first, then install every bundle closure before
            # any product installer can publish tools or other visible effects.
            for resolved, _item in prepared:
                product = products[resolved.product_id]
                bundle_plan = bundle_plans[resolved.product_id]
                try:
                    await install_bundle_plan(
                        bundle_plan,
                        product,
                        resolved._config_snapshot(),
                    )
                except (KeyboardInterrupt, SystemExit, asyncio.CancelledError):
                    raise
                except BaseException as exc:
                    raise CustomerBundleActivationError(
                        CustomerBundleActivationCode.BUNDLE_INSTALL_FAILED,
                        f"runtime bundle installation failed for product "
                        f"{resolved.product_id!r}",
                        cause=exc,
                        product_id=resolved.product_id,
                        bundle_ids=bundle_plan.bundle_ids,
                    ) from exc

            for resolved, item in prepared:
                if item.install is None:
                    continue
                product = products[resolved.product_id]
                install_scope = product.effects.child(f"activator:{resolved.product_id}")
                install_product = resolved._snapshot()
                context = ProductActivationContext(
                    path=product.path,
                    # Preparation received another public copy.  Installation
                    # gets a fresh trusted snapshot, preventing a retained
                    # preparation reference from rewriting runtime config.
                    config=install_product.config,
                    _bindings=product.bindings,
                    _effects=install_scope,
                    _tools=self._tools,
                    _declared_capabilities=install_product.descriptor.capabilities,
                )
                try:
                    await item.install(context)
                except (KeyboardInterrupt, SystemExit, asyncio.CancelledError):
                    raise
                except CustomerBundleActivationError:
                    raise
                except BaseException as exc:
                    raise CustomerBundleActivationError(
                        CustomerBundleActivationCode.INSTALL_FAILED,
                        f"product {resolved.product_id!r} installation failed",
                        cause=exc,
                        product_id=resolved.product_id,
                    ) from exc

            run_leases: dict[str, _RunLease] = {}
            for product_id, product in products.items():
                lease = _RunLease()
                run_leases[product_id] = lease
                # Registered last, therefore drained before tool/provider cleanup.
                product.effects.callback("execution-runs:drain", lease.drain)

            activation = ActivatedCustomerBundle(
                # Do not publish the objects that were exposed to preparation
                # callbacks.  They may contain mutable nested config values.
                plan=plan._snapshot(),
                tenant=tenant,
                products=products,
                runner=self._runner,
                _kernel=self,
                _activation_id=activation_id,
                _run_leases=run_leases,
            )
            async with self._lock:
                slot = self._slots.get(tenant_id)
                if slot is None or slot.activation_id != activation_id:
                    raise RuntimeError("activation reservation disappeared")
                slot.activation = activation
                slot.ready.set()
            return activation
        except BaseException as exc:
            cleanup_errors: list[EffectCleanupError] = []
            if tenant is not None:
                try:
                    await tenant.close()
                except EffectCleanupError as cleanup_exc:
                    cleanup_errors.append(cleanup_exc)
            staged_cleanup = await self._cleanup_staged(staged_disposers, tenant_id)
            if staged_cleanup is not None:
                cleanup_errors.append(staged_cleanup)

            cleanup = self._merge_cleanup(cleanup_errors, tenant_id)
            await self._finish_close(tenant_id, activation_id, cleanup)
            if isinstance(exc, KeyboardInterrupt | SystemExit | asyncio.CancelledError):
                if cleanup is not None:
                    exc.add_note(str(cleanup))
                raise
            failure = (
                exc
                if isinstance(exc, CustomerBundleActivationError)
                else CustomerBundleActivationError(
                    CustomerBundleActivationCode.INSTALL_FAILED,
                    "trusted customer bundle activation failed",
                    cause=exc,
                )
            )
            if cleanup is not None:
                failure = failure.with_cleanup(cleanup)
            if failure.activation_cause is not None:
                raise failure from failure.activation_cause
            raise failure from None

    def _validate_activators(
        self,
        plan: CustomerBundlePlan,
        activators: Iterable[ProductActivator],
    ) -> Mapping[str, ProductActivator]:
        try:
            supplied = tuple(activators)
        except Exception as exc:
            raise CustomerBundleActivationError(
                CustomerBundleActivationCode.INVALID_ACTIVATOR,
                "activators must be iterable",
                cause=exc,
            ) from exc

        indexed: dict[str, ProductActivator] = {}
        for activator in supplied:
            descriptor = getattr(activator, "descriptor", None)
            prepare = getattr(activator, "prepare", None)
            if not isinstance(descriptor, ProductDescriptor) or not callable(prepare):
                raise CustomerBundleActivationError(
                    CustomerBundleActivationCode.INVALID_ACTIVATOR,
                    f"invalid product activator: {type(activator).__name__}",
                    activator_type=type(activator).__name__,
                )
            if descriptor.product_id in indexed:
                raise CustomerBundleActivationError(
                    CustomerBundleActivationCode.DUPLICATE_ACTIVATOR,
                    f"duplicate activator for product {descriptor.product_id!r}",
                    product_id=descriptor.product_id,
                )
            indexed[descriptor.product_id] = activator

        expected = set(plan.product_ids)
        actual = set(indexed)
        missing = tuple(sorted(expected - actual))
        extra = tuple(sorted(actual - expected))
        if missing:
            raise CustomerBundleActivationError(
                CustomerBundleActivationCode.MISSING_ACTIVATOR,
                "customer bundle has products without activators",
                product_ids=missing,
            )
        if extra:
            raise CustomerBundleActivationError(
                CustomerBundleActivationCode.EXTRA_ACTIVATOR,
                "activators contain products outside the customer bundle",
                product_ids=extra,
            )
        for resolved in plan.products:
            descriptor = indexed[resolved.product_id].descriptor
            if descriptor != resolved.descriptor:
                raise CustomerBundleActivationError(
                    CustomerBundleActivationCode.DESCRIPTOR_MISMATCH,
                    f"activator descriptor does not match plan for {resolved.product_id!r}",
                    product_id=resolved.product_id,
                )
        return MappingProxyType(indexed)

    @staticmethod
    def _validate_prepared(
        prepared: list[tuple[ResolvedCustomerProduct, PreparedProduct]],
        *,
        tenant_bindings: ServiceBindings,
        profiles: dict[str, ProfileProvider],
        expected_configs: Mapping[str, BaseModel],
    ) -> None:
        provider_owners: dict[int, str] = {}
        for resolved, item in prepared:
            if item.descriptor != resolved.descriptor:
                raise CustomerBundleActivationError(
                    CustomerBundleActivationCode.DESCRIPTOR_MISMATCH,
                    f"prepared descriptor does not match plan for {resolved.product_id!r}",
                    product_id=resolved.product_id,
                )
            expected_config = expected_configs[resolved.product_id]
            if (
                type(resolved.config) is not type(expected_config)
                or resolved.config != expected_config
                or type(item.config) is not type(expected_config)
                or item.config != expected_config
            ):
                raise CustomerBundleActivationError(
                    CustomerBundleActivationCode.CONFIG_MISMATCH,
                    f"prepared config does not match plan for {resolved.product_id!r}",
                    product_id=resolved.product_id,
                )
            try:
                bindings = tenant_bindings.derive(ScopeKind.PRODUCT, item.bindings)
            except (ServiceBindingError, TypeError) as exc:
                raise CustomerBundleActivationError(
                    CustomerBundleActivationCode.INVALID_BINDING,
                    f"invalid bindings for product {resolved.product_id!r}",
                    cause=exc,
                    product_id=resolved.product_id,
                ) from exc
            if not bindings.contains(PROFILE_PROVIDER):
                raise CustomerBundleActivationError(
                    CustomerBundleActivationCode.PROFILE_REQUIRED,
                    f"product {resolved.product_id!r} requires a private profile provider",
                    product_id=resolved.product_id,
                )
            profile = bindings.resolve(PROFILE_PROVIDER)
            previous = provider_owners.get(id(profile))
            if previous is not None:
                raise CustomerBundleActivationError(
                    CustomerBundleActivationCode.PROFILE_INSTANCE_REUSED,
                    "products must not share one live private profile provider instance",
                    product_ids=(previous, resolved.product_id),
                )
            provider_owners[id(profile)] = resolved.product_id
            profiles[resolved.product_id] = profile

    @staticmethod
    def _configure_sharing_policies(
        tenant_id: str,
        plan: CustomerBundlePlan,
        products: Mapping[str, ProductContext],
        provider: Customer360Provider,
    ) -> None:
        outgoing: dict[str, dict[str, set[str]]] = {
            product_id: {} for product_id in plan.product_ids
        }
        incoming: dict[str, dict[str, set[str]]] = {
            product_id: {} for product_id in plan.product_ids
        }
        for rule in plan.sharing.rules:
            outgoing[rule.producer].setdefault(rule.consumer, set()).update(rule.predicates)
            incoming[rule.consumer].setdefault(rule.producer, set()).update(rule.predicates)

        for product_id in plan.product_ids:
            publish_to = frozenset(outgoing[product_id])
            publish_predicates = frozenset(
                predicate
                for predicates in outgoing[product_id].values()
                for predicate in predicates
            )
            consume_from = frozenset(incoming[product_id])
            consume_predicates = frozenset(
                predicate
                for predicates in incoming[product_id].values()
                for predicate in predicates
            )
            scope = RequestScope(
                path=products[product_id].path,
                principal_id="suiteharness-kernel",
                roles=frozenset({SHARING_ADMIN}),
                purpose="customer360-policy",
                request_id=f"activate-{product_id}",
                correlation_id=f"bundle-{plan.manifest.customer_bundle_id}",
            )
            current_policy = provider.get_policy(scope)
            provider.configure_policy(
                scope,
                publish_enabled=bool(publish_to and publish_predicates),
                consume_enabled=bool(consume_from and consume_predicates),
                publish_predicates=publish_predicates,
                consume_predicates=consume_predicates,
                publish_to_products=publish_to,
                consume_from_products=consume_from,
                expected_revision=current_policy.revision,
                idempotency_key=(
                    f"activate:{tenant_id}:{plan.manifest.customer_bundle_id}:"
                    f"{plan.manifest.version}:{product_id}"
                ),
                publish_rules={
                    peer: frozenset(predicates)
                    for peer, predicates in outgoing[product_id].items()
                },
                consume_rules={
                    peer: frozenset(predicates)
                    for peer, predicates in incoming[product_id].items()
                },
            )

    async def _reserve(self, tenant_id: str) -> str:
        async with self._lock:
            if self._closed or self._root.effects.closed:
                raise CustomerBundleActivationError(
                    CustomerBundleActivationCode.KERNEL_CLOSED,
                    "the harness kernel is closed",
                )
            if tenant_id in self._poisoned:
                raise CustomerBundleActivationError(
                    CustomerBundleActivationCode.ACTIVATION_CONFLICT,
                    "this tenant is quarantined after incomplete activation cleanup",
                    tenant_id=tenant_id,
                    poisoned=True,
                )
            if tenant_id in self._slots:
                raise CustomerBundleActivationError(
                    CustomerBundleActivationCode.ACTIVATION_CONFLICT,
                    "this tenant already has an activating or active customer bundle",
                    tenant_id=tenant_id,
                )
            activation_id = secrets.token_hex(16)
            self._slots[tenant_id] = _ActivationSlot(activation_id=activation_id)
            return activation_id

    async def _finish_close(
        self,
        tenant_id: str,
        activation_id: str,
        cleanup: EffectCleanupError | None = None,
    ) -> None:
        """Release only the slot owned by ``activation_id`` and quarantine atomically."""

        async with self._lock:
            current = self._slots.get(tenant_id)
            if current is not None and current.activation_id == activation_id:
                current.ready.set()
                self._slots.pop(tenant_id, None)
                if cleanup is not None:
                    self._poisoned[tenant_id] = cleanup

    @staticmethod
    async def _cleanup_staged(
        disposers: list[tuple[str, Disposer]],
        tenant_id: str,
    ) -> EffectCleanupError | None:
        if not disposers:
            return None
        cleanup_scope = EffectScope(f"staged:{tenant_id}")
        for label, dispose in disposers:
            cleanup_scope.callback(label, dispose)
        try:
            await cleanup_scope.close()
        except EffectCleanupError as cleanup:
            return cleanup
        return None

    @staticmethod
    def _merge_cleanup(
        errors: list[EffectCleanupError],
        tenant_id: str,
    ) -> EffectCleanupError | None:
        if not errors:
            return None
        if len(errors) == 1:
            return errors[0]
        return EffectCleanupError(
            f"activation:{tenant_id}",
            tuple(failure for error in errors for failure in error.failures),
        )


__all__ = [
    "ActivatedCustomerBundle",
    "CustomerBundleActivationCode",
    "CustomerBundleActivationError",
    "HarnessKernel",
    "PreparedProduct",
    "ProductActivationContext",
    "ProductActivator",
    "ScopedToolRegistry",
]
