"""End-to-end tests for trusted customer-bundle activation."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

import pytest
from pydantic import BaseModel, ConfigDict

from suiteharness.execution import (
    WORKFLOW,
    InMemoryAuditJournal,
    InMemoryCapabilityAuthority,
    RunRequest,
    ToolEffect,
    ToolSpec,
    WorkflowDecision,
)
from suiteharness.memory import (
    CURATOR,
    CUSTOMER360_PROVIDER,
    ENTITY_ADMIN,
    PRIVATE_READ,
    PRIVATE_WRITE,
    PROFILE_PROVIDER,
    SHARE_PROPOSE,
    SHARED_READ,
    CurationAction,
    InMemoryCustomer360Provider,
    InMemoryProfileProvider,
)
from suiteharness.runtime.bundle import Bundle, BundleInstallContext, BundleResolutionCode
from suiteharness.runtime.customer_bundle import (
    CustomerBundlePlan,
    ProductCatalog,
    ResolvedCustomerProduct,
    resolve_customer_bundle,
)
from suiteharness.runtime.descriptor import (
    BundleManifest,
    BundleRequirement,
    ProductDescriptor,
)
from suiteharness.runtime.effects import EffectScopeState
from suiteharness.runtime.kernel import (
    CustomerBundleActivationCode,
    CustomerBundleActivationError,
    HarnessKernel,
    PreparedProduct,
    ProductActivationContext,
)
from suiteharness.runtime.scopes import RequestScope, ScopePath, ServiceKey


class ProductConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class NestedProductConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    routing: dict[str, list[str]]


def descriptor(
    product_id: str,
    *,
    version: str = "1.0.0",
    bundles: tuple[BundleRequirement, ...] = (),
    capabilities: frozenset[str] = frozenset(),
) -> ProductDescriptor:
    return ProductDescriptor(
        product_id=product_id,
        version=version,
        harness_api=">=0.1,<1",
        bundles=bundles,
        capabilities=capabilities,
        config_model=ProductConfig,
    )


def make_plan(
    *product_ids: str,
    rules: tuple[tuple[str, str, tuple[str, ...]], ...] = (),
) -> CustomerBundlePlan:
    customer360: dict[str, object] = {"mode": "isolated"}
    if rules:
        customer360 = {
            "mode": "curated",
            "rules": [
                {
                    "producer": producer,
                    "consumer": consumer,
                    "predicates": list(predicates),
                }
                for producer, consumer, predicates in rules
            ],
        }
    raw = {
        "customer_bundle_id": "test-bundle",
        "version": "1.0.0",
        "harness_api": ">=0.1,<1",
        "products": [
            {"product_id": product_id, "version": ">=1,<2", "config": {}}
            for product_id in product_ids
        ],
        "customer360": customer360,
    }
    return resolve_customer_bundle(
        raw,
        ProductCatalog(descriptor(product_id) for product_id in product_ids),
        harness_version="0.1.0",
    )


def make_descriptor_plan(*descriptors: ProductDescriptor) -> CustomerBundlePlan:
    return resolve_customer_bundle(
        {
            "customer_bundle_id": "test-bundle",
            "version": "1.0.0",
            "harness_api": ">=0.1,<1",
            "products": [
                {
                    "product_id": item.product_id,
                    "version": ">=1,<2",
                    "config": {},
                }
                for item in descriptors
            ],
        },
        ProductCatalog(descriptors),
        harness_version="0.1.0",
    )


InstallCallback = Callable[[ProductActivationContext], Awaitable[None]]


@dataclass
class RuntimeBundle:
    manifest: BundleManifest
    events: list[str]
    fail: bool = False

    async def install(self, context: BundleInstallContext) -> None:
        bundle_id = self.manifest.bundle_id
        self.events.append(f"install:{bundle_id}")
        context.own(
            f"dispose:{bundle_id}",
            lambda: self.events.append(f"dispose:{bundle_id}"),
        )
        if self.fail:
            raise RuntimeError(f"bundle failed:{bundle_id}")


@dataclass
class TestActivator:
    __test__ = False

    descriptor: ProductDescriptor
    install_callback: InstallCallback | None = None
    shared_profile: InMemoryProfileProvider | None = None
    prepared_descriptor: ProductDescriptor | None = None
    bindings_override: Mapping[ServiceKey[Any], object] | None = None
    bundles: tuple[Bundle, ...] = ()
    fail_cleanup: bool = False
    prepared: int = 0
    installed: int = 0
    cleaned: int = 0
    profiles: list[InMemoryProfileProvider] = field(default_factory=list)

    async def prepare(self, product: ResolvedCustomerProduct) -> PreparedProduct:
        self.prepared += 1
        profile = self.shared_profile or InMemoryProfileProvider()
        self.profiles.append(profile)

        async def install(context: ProductActivationContext) -> None:
            self.installed += 1
            if self.install_callback is not None:
                await self.install_callback(context)

        async def cleanup() -> None:
            self.cleaned += 1
            profile.close()
            if self.fail_cleanup:
                raise OSError(f"cleanup failed for {self.descriptor.product_id}")

        bindings = (
            self.bindings_override
            if self.bindings_override is not None
            else {PROFILE_PROVIDER: profile}
        )
        return PreparedProduct(
            descriptor=self.prepared_descriptor or self.descriptor,
            config=product.config,
            bindings=bindings,
            install=install,
            cleanup=cleanup,
            bundles=self.bundles,
        )


def scope_for(activation, product_id: str, *roles: str) -> RequestScope:  # type: ignore[no-untyped-def]
    return RequestScope(
        path=activation.product(product_id).path,
        principal_id="user-1",
        roles=frozenset(roles),
        purpose="test",
    )


def test_single_product_isolated_activation_has_no_customer360() -> None:
    async def exercise():  # type: ignore[no-untyped-def]
        plan = make_plan("a")
        activator = TestActivator(plan.products[0].descriptor)
        kernel = HarnessKernel()
        activation = await kernel.activate_customer_bundle(
            "tenant-1", plan, [activator]
        )
        assert activation.customer_bundle_id == "test-bundle"
        assert activation._activation_id not in repr(activation)
        assert not activation.tenant.bindings.contains(CUSTOMER360_PROVIDER)
        assert not activation.product("a").bindings.contains(CUSTOMER360_PROVIDER)
        assert activation.runner is kernel.runner
        await activation.close()
        return activation, activator, await kernel.active("tenant-1")

    activation, activator, active = asyncio.run(exercise())
    assert activation.closed is True
    assert activator.prepared == activator.installed == activator.cleaned == 1
    assert active is None


def test_required_runtime_bundle_must_be_supplied_before_any_install() -> None:
    required = descriptor(
        "a",
        bundles=(BundleRequirement(bundle_id="required-runtime"),),
    )
    plan = make_descriptor_plan(required)
    activator = TestActivator(required)

    async def exercise():  # type: ignore[no-untyped-def]
        kernel = HarnessKernel()
        with pytest.raises(CustomerBundleActivationError) as captured:
            await kernel.activate_customer_bundle("tenant-1", plan, [activator])
        return captured.value, await kernel.active("tenant-1")

    failure, active = asyncio.run(exercise())
    assert failure.code is CustomerBundleActivationCode.BUNDLE_RESOLUTION_FAILED
    assert failure.detail["product_id"] == "a"
    assert failure.detail["bundle_resolution_code"] == BundleResolutionCode.MISSING.value
    assert failure.detail["bundle_resolution_detail"] == {
        "source": "a",
        "bundle_id": "required-runtime",
        "required": "",
    }
    assert failure.detail["required_bundle_ids"] == ("required-runtime",)
    assert activator.prepared == activator.cleaned == 1
    assert activator.installed == 0
    assert active is None


def test_runtime_bundles_install_in_dependency_order_before_product() -> None:
    events: list[str] = []
    core = RuntimeBundle(
        BundleManifest(
            bundle_id="runtime-core",
            version="1.0.0",
            harness_api=">=0.1,<1",
        ),
        events,
    )
    feature = RuntimeBundle(
        BundleManifest(
            bundle_id="runtime-feature",
            version="1.0.0",
            harness_api=">=0.1,<1",
            requires=(BundleRequirement(bundle_id="runtime-core"),),
        ),
        events,
    )
    selected = descriptor(
        "a",
        bundles=(BundleRequirement(bundle_id="runtime-feature"),),
    )
    plan = make_descriptor_plan(selected)

    async def install_product(context: ProductActivationContext) -> None:
        events.append("install:product")
        context.own("dispose:product", lambda: events.append("dispose:product"))

    async def exercise() -> None:
        activation = await HarnessKernel().activate_customer_bundle(
            "tenant-1",
            plan,
            [
                TestActivator(
                    selected,
                    install_callback=install_product,
                    bundles=(feature, core),
                )
            ],
        )
        assert events == ["install:runtime-core", "install:runtime-feature", "install:product"]
        await activation.close()

    asyncio.run(exercise())
    assert events == [
        "install:runtime-core",
        "install:runtime-feature",
        "install:product",
        "dispose:product",
        "dispose:runtime-feature",
        "dispose:runtime-core",
    ]


def test_runtime_bundle_failure_rolls_back_and_skips_product_install() -> None:
    events: list[str] = []
    core = RuntimeBundle(
        BundleManifest(
            bundle_id="runtime-core",
            version="1.0.0",
            harness_api=">=0.1,<1",
        ),
        events,
    )
    failing = RuntimeBundle(
        BundleManifest(
            bundle_id="runtime-failing",
            version="1.0.0",
            harness_api=">=0.1,<1",
            requires=(BundleRequirement(bundle_id="runtime-core"),),
        ),
        events,
        fail=True,
    )
    selected = descriptor(
        "a",
        bundles=(BundleRequirement(bundle_id="runtime-failing"),),
    )
    plan = make_descriptor_plan(selected)

    async def install_product(context: ProductActivationContext) -> None:
        events.append("install:product")

    activator = TestActivator(
        selected,
        install_callback=install_product,
        bundles=(failing, core),
    )

    async def exercise():  # type: ignore[no-untyped-def]
        kernel = HarnessKernel()
        with pytest.raises(CustomerBundleActivationError) as captured:
            await kernel.activate_customer_bundle("tenant-1", plan, [activator])
        return captured.value, await kernel.active("tenant-1")

    failure, active = asyncio.run(exercise())
    assert failure.code is CustomerBundleActivationCode.BUNDLE_INSTALL_FAILED
    assert failure.detail == {
        "product_id": "a",
        "bundle_ids": ("runtime-core", "runtime-failing"),
    }
    assert isinstance(failure.activation_cause, RuntimeError)
    assert events == [
        "install:runtime-core",
        "install:runtime-failing",
        "dispose:runtime-failing",
        "dispose:runtime-core",
    ]
    assert activator.installed == 0
    assert activator.cleaned == 1
    assert active is None


def test_tool_capabilities_must_be_declared_by_product() -> None:
    selected = descriptor("a")
    plan = make_descriptor_plan(selected)

    async def handler(call, arguments):  # type: ignore[no-untyped-def]
        return None

    async def install_product(context: ProductActivationContext) -> None:
        context.register_tool(
            ToolSpec(
                name="resource.read",
                effects=frozenset({ToolEffect.READ}),
                required_capabilities=frozenset({"resource:read"}),
            ),
            handler,
        )

    activator = TestActivator(selected, install_callback=install_product)

    async def exercise():  # type: ignore[no-untyped-def]
        kernel = HarnessKernel()
        with pytest.raises(CustomerBundleActivationError) as captured:
            await kernel.activate_customer_bundle("tenant-1", plan, [activator])
        scope = RequestScope(
            path=ScopePath.product("tenant-1", "a"),
            principal_id="user-1",
        )
        return captured.value, kernel.tools.resolve(scope, "resource.read")

    failure, registered = asyncio.run(exercise())
    assert failure.code is CustomerBundleActivationCode.TOOL_CAPABILITY_UNDECLARED
    assert failure.detail == {
        "product_id": "a",
        "tool_name": "resource.read",
        "undeclared_capabilities": ("resource:read",),
        "declared_capabilities": (),
    }
    assert registered is None
    assert activator.cleaned == 1


def test_declared_product_capability_allows_tool_registration() -> None:
    selected = descriptor("a", capabilities=frozenset({"resource:read"}))
    plan = make_descriptor_plan(selected)

    async def handler(call, arguments):  # type: ignore[no-untyped-def]
        return None

    async def install_product(context: ProductActivationContext) -> None:
        context.register_tool(
            ToolSpec(
                name="resource.read",
                effects=frozenset({ToolEffect.READ}),
                required_capabilities=frozenset({"resource:read"}),
            ),
            handler,
        )

    async def exercise():  # type: ignore[no-untyped-def]
        kernel = HarnessKernel()
        activation = await kernel.activate_customer_bundle(
            "tenant-1",
            plan,
            [TestActivator(selected, install_callback=install_product)],
        )
        scope = scope_for(activation, "a")
        registered = kernel.tools.resolve(scope, "resource.read")
        await activation.close()
        return registered, kernel.tools.resolve(scope, "resource.read")

    registered, after_close = asyncio.run(exercise())
    assert registered is not None
    assert registered.handler is handler
    assert after_close is None


def test_product_install_context_is_scoped_without_parent_escape_hatches() -> None:
    observed: list[object] = []
    disposed: list[str] = []

    async def inspect(context: ProductActivationContext) -> None:
        observed.extend(
            [
                context.path,
                context.config,
                context.resolve(PROFILE_PROVIDER),
                hasattr(context, "product"),
                hasattr(context, "effects"),
                hasattr(context, "resolved"),
            ]
        )
        context.own("inspect-resource", lambda: disposed.append("owned"))

    async def exercise() -> TestActivator:
        plan = make_plan("a")
        activator = TestActivator(plan.products[0].descriptor, install_callback=inspect)
        activation = await HarnessKernel().activate_customer_bundle(
            "tenant-1", plan, [activator]
        )
        await activation.close()
        return activator

    activator = asyncio.run(exercise())
    assert observed[0].product_id == "a"
    assert observed[1] == ProductConfig()
    assert observed[2] is activator.profiles[0]
    assert observed[3:] == [False, False, False]
    assert disposed == ["owned"]


def test_activation_uses_trusted_deep_config_snapshots_across_callbacks() -> None:
    nested_descriptor = ProductDescriptor(
        product_id="nested",
        version="1.0.0",
        harness_api=">=0.1,<1",
        config_model=NestedProductConfig,
    )
    caller_config = {"routing": {"priority": ["route-one"]}}
    plan = resolve_customer_bundle(
        {
            "customer_bundle_id": "nested-bundle",
            "version": "1.0.0",
            "harness_api": ">=0.1,<1",
            "products": [
                {
                    "product_id": "nested",
                    "version": ">=1,<2",
                    "config": caller_config,
                }
            ],
        },
        ProductCatalog([nested_descriptor]),
        harness_version="0.1.0",
    )

    @dataclass
    class RetainingActivator:
        descriptor: ProductDescriptor
        retained_prepare_config: NestedProductConfig | None = None
        install_config: dict[str, object] | None = None

        async def prepare(self, product: ResolvedCustomerProduct) -> PreparedProduct:
            assert isinstance(product.config, NestedProductConfig)
            self.retained_prepare_config = product.config
            profile = InMemoryProfileProvider()

            async def install(context: ProductActivationContext) -> None:
                assert self.retained_prepare_config is not None
                # This is the same mutable object returned in PreparedProduct.
                # Installation must nevertheless receive a clean config copy.
                self.retained_prepare_config.routing["priority"].append(
                    "late-activator-mutation"
                )
                self.install_config = context.config.model_dump()

            async def cleanup() -> None:
                profile.close()

            return PreparedProduct(
                descriptor=self.descriptor,
                config=self.retained_prepare_config,
                bindings={PROFILE_PROVIDER: profile},
                install=install,
                cleanup=cleanup,
            )

    # Both the caller-owned source and a public plan view are mutable.  Neither
    # is the resolver-owned value consumed by the kernel.
    caller_config["routing"]["priority"].append("caller-mutation")
    exposed = plan.products[0].config
    assert isinstance(exposed, NestedProductConfig)
    exposed.routing["priority"].append("plan-view-mutation")
    activator = RetainingActivator(nested_descriptor)

    async def exercise():  # type: ignore[no-untyped-def]
        activation = await HarnessKernel().activate_customer_bundle(
            "tenant-1", plan, [activator]
        )
        assert activator.retained_prepare_config is not None
        activator.retained_prepare_config.routing["priority"].append(
            "post-activation-mutation"
        )
        published_product_config = activation.plan.products[0].config.model_dump()
        published_config_map = activation.plan.configs["nested"].model_dump()
        await activation.close()
        return published_product_config, published_config_map

    published_product_config, published_config_map = asyncio.run(exercise())
    expected = {"routing": {"priority": ["route-one"]}}
    assert activator.install_config == expected
    assert published_product_config == published_config_map == expected


def test_prepare_time_nested_config_mutation_fails_closed() -> None:
    nested_descriptor = ProductDescriptor(
        product_id="nested",
        version="1.0.0",
        harness_api=">=0.1,<1",
        config_model=NestedProductConfig,
    )
    plan = resolve_customer_bundle(
        {
            "customer_bundle_id": "nested-bundle",
            "version": "1.0.0",
            "harness_api": ">=0.1,<1",
            "products": [
                {
                    "product_id": "nested",
                    "version": ">=1,<2",
                    "config": {"routing": {"priority": ["route-one"]}},
                }
            ],
        },
        ProductCatalog([nested_descriptor]),
        harness_version="0.1.0",
    )

    @dataclass
    class MutatingActivator:
        descriptor: ProductDescriptor
        cleaned: int = 0

        async def prepare(self, product: ResolvedCustomerProduct) -> PreparedProduct:
            assert isinstance(product.config, NestedProductConfig)
            product.config.routing["priority"].append("unauthorized-mutation")
            profile = InMemoryProfileProvider()

            async def cleanup() -> None:
                self.cleaned += 1
                profile.close()

            return PreparedProduct(
                descriptor=self.descriptor,
                config=product.config,
                bindings={PROFILE_PROVIDER: profile},
                cleanup=cleanup,
            )

    activator = MutatingActivator(nested_descriptor)

    async def exercise():  # type: ignore[no-untyped-def]
        kernel = HarnessKernel()
        with pytest.raises(CustomerBundleActivationError) as captured:
            await kernel.activate_customer_bundle("tenant-1", plan, [activator])
        return captured.value, await kernel.active("tenant-1")

    failure, active = asyncio.run(exercise())
    assert failure.code is CustomerBundleActivationCode.CONFIG_MISMATCH
    assert active is None
    assert activator.cleaned == 1


def test_same_profile_implementation_is_separate_by_product_and_data() -> None:
    async def exercise():  # type: ignore[no-untyped-def]
        plan = make_plan("a", "b")
        activators = [TestActivator(item.descriptor) for item in plan.products]
        kernel = HarnessKernel()
        activation = await kernel.activate_customer_bundle(
            "tenant-1", plan, activators
        )
        a = activation.product("a").resolve(PROFILE_PROVIDER)
        b = activation.product("b").resolve(PROFILE_PROVIDER)
        assert type(a) is type(b) is InMemoryProfileProvider
        assert a is not b
        claim = a.assert_claim(
            scope_for(activation, "a", PRIVATE_WRITE),
            local_subject_id="same-local-user",
            predicate="profile.attribute.local",
            value="value-local",
            source_ref="test",
            idempotency_key="claim-a",
        )
        assert (
            b.list_claims(
                scope_for(activation, "b", PRIVATE_READ),
                "same-local-user",
            )
            == ()
        )
        await activation.close()
        return claim

    claim = asyncio.run(exercise())
    assert claim.space.owner_product_id == "a"


def test_curated_mode_binds_one_tenant_customer360_and_exact_bilateral_policies() -> None:
    async def exercise():  # type: ignore[no-untyped-def]
        plan = make_plan(
            "a",
            "b",
            rules=(("a", "b", ("profile.attribute.one",)),),
        )
        kernel = HarnessKernel()
        activation = await kernel.activate_customer_bundle(
            "tenant-1",
            plan,
            [TestActivator(item.descriptor) for item in plan.products],
        )
        shared = activation.tenant.resolve(CUSTOMER360_PROVIDER)
        assert activation.product("a").resolve(CUSTOMER360_PROVIDER) is shared
        assert activation.product("b").resolve(CUSTOMER360_PROVIDER) is shared
        producer = shared.get_policy(scope_for(activation, "a"))
        consumer = shared.get_policy(scope_for(activation, "b"))
        profile = activation.product("a").resolve(PROFILE_PROVIDER)
        claim = profile.assert_claim(
            scope_for(activation, "a", PRIVATE_WRITE),
            local_subject_id="subject-a",
            predicate="profile.attribute.one",
            value="value-one",
            source_ref="source:test",
            idempotency_key="attribute-claim",
        )
        shared.bind_entity_alias(
            scope_for(activation, "a", ENTITY_ADMIN),
            source_product_id="a",
            local_subject_id="subject-a",
            entity_id="entity-1",
            expected_revision=0,
            idempotency_key="alias-a",
        )
        candidate = shared.propose_share(
            scope_for(activation, "a", SHARE_PROPOSE, PRIVATE_READ),
            source_claim_id=claim.claim_id,
            idempotency_key="share-attribute",
        )
        shared.curate(
            scope_for(activation, "b", CURATOR),
            candidate_id=candidate.candidate_id,
            action=CurationAction.ACCEPT,
            reason="verified",
            expected_revision=0,
            idempotency_key="curate-attribute",
        )
        visible = shared.read_shared(
            scope_for(activation, "b", SHARED_READ),
            "entity-1",
        )
        await activation.close()
        return producer, consumer, candidate, visible

    producer, consumer, candidate, visible = asyncio.run(exercise())
    assert producer.publish_enabled is True
    assert producer.consume_enabled is False
    assert producer.publish_predicates == frozenset({"profile.attribute.one"})
    assert producer.publish_to_products == frozenset({"b"})
    assert consumer.consume_enabled is True
    assert consumer.publish_enabled is False
    assert consumer.consume_predicates == frozenset({"profile.attribute.one"})
    assert consumer.consume_from_products == frozenset({"a"})
    assert candidate.audience_product_ids == frozenset({"b"})
    assert [claim.value for claim in visible] == ["value-one"]


def test_non_rectangular_abc_sharing_preserves_every_exact_peer_predicate_rule() -> None:
    async def exercise():  # type: ignore[no-untyped-def]
        plan = make_plan(
            "a",
            "b",
            "c",
            rules=(
                ("a", "b", ("profile.attribute.one",)),
                ("a", "c", ("profile.attribute.two",)),
                ("c", "b", ("profile.attribute.two",)),
            ),
        )
        activators = [TestActivator(item.descriptor) for item in plan.products]
        activation = await HarnessKernel().activate_customer_bundle(
            "tenant-1", plan, activators
        )
        shared = activation.tenant.resolve(CUSTOMER360_PROVIDER)
        profile = activation.product("a").resolve(PROFILE_PROVIDER)
        write_scope = scope_for(activation, "a", PRIVATE_WRITE)
        first = profile.assert_claim(
            write_scope,
            local_subject_id="subject-a",
            predicate="profile.attribute.one",
            value="value-one",
            source_ref="source:test",
            idempotency_key="abc-first",
        )
        second = profile.assert_claim(
            write_scope,
            local_subject_id="subject-a",
            predicate="profile.attribute.two",
            value="value-two",
            source_ref="source:test",
            idempotency_key="abc-second",
        )
        shared.bind_entity_alias(
            scope_for(activation, "a", ENTITY_ADMIN),
            source_product_id="a",
            local_subject_id="subject-a",
            entity_id="entity-1",
            expected_revision=0,
            idempotency_key="abc-alias",
        )
        propose_scope = scope_for(activation, "a", SHARE_PROPOSE, PRIVATE_READ)
        first_candidate = shared.propose_share(
            propose_scope,
            source_claim_id=first.claim_id,
            idempotency_key="abc-share-first",
        )
        second_candidate = shared.propose_share(
            propose_scope,
            source_claim_id=second.claim_id,
            idempotency_key="abc-share-second",
        )
        curator = scope_for(activation, "b", CURATOR)
        shared.curate(
            curator,
            candidate_id=first_candidate.candidate_id,
            action=CurationAction.ACCEPT,
            reason="verified",
            expected_revision=0,
            idempotency_key="abc-curate-first",
        )
        shared.curate(
            curator,
            candidate_id=second_candidate.candidate_id,
            action=CurationAction.ACCEPT,
            reason="verified",
            expected_revision=1,
            idempotency_key="abc-curate-second",
        )
        visible_b = shared.read_shared(
            scope_for(activation, "b", SHARED_READ), "entity-1"
        )
        visible_c = shared.read_shared(
            scope_for(activation, "c", SHARED_READ), "entity-1"
        )
        policy_a = shared.get_policy(scope_for(activation, "a"))
        policy_b = shared.get_policy(scope_for(activation, "b"))
        await activation.close()
        return (
            first_candidate,
            second_candidate,
            visible_b,
            visible_c,
            policy_a,
            policy_b,
            activators,
        )

    first_candidate, second_candidate, visible_b, visible_c, policy_a, policy_b, activators = (
        asyncio.run(exercise())
    )
    assert first_candidate.audience_product_ids == frozenset({"b"})
    assert second_candidate.audience_product_ids == frozenset({"c"})
    assert [(claim.predicate, claim.value) for claim in visible_b] == [
        ("profile.attribute.one", "value-one")
    ]
    assert [(claim.predicate, claim.value) for claim in visible_c] == [
        ("profile.attribute.two", "value-two")
    ]
    assert {
        rule.peer_product_id: rule.predicates for rule in policy_a.publish_rules
    } == {
        "b": frozenset({"profile.attribute.one"}),
        "c": frozenset({"profile.attribute.two"}),
    }
    assert {
        rule.peer_product_id: rule.predicates for rule in policy_b.consume_rules
    } == {
        "a": frozenset({"profile.attribute.one"}),
        "c": frozenset({"profile.attribute.two"}),
    }
    assert all(item.prepared == item.installed == item.cleaned == 1 for item in activators)


def test_same_tool_name_resolves_by_product_and_precisely_unloads() -> None:
    async def exercise():  # type: ignore[no-untyped-def]
        plan = make_plan("a", "b")
        handlers: dict[str, object] = {}

        def installer(product_id: str) -> InstallCallback:
            async def install(context: ProductActivationContext) -> None:
                async def handler(call, arguments):  # type: ignore[no-untyped-def]
                    return product_id

                handlers[product_id] = handler
                context.register_tool(
                    ToolSpec(
                        name="common.lookup",
                        effects=frozenset({ToolEffect.READ}),
                    ),
                    handler,
                )

            return install

        activators = [
            TestActivator(item.descriptor, install_callback=installer(item.product_id))
            for item in plan.products
        ]
        kernel = HarnessKernel()
        activation = await kernel.activate_customer_bundle(
            "tenant-1", plan, activators
        )
        a_scope = scope_for(activation, "a")
        b_scope = scope_for(activation, "b")
        assert kernel.tools.resolve(a_scope, "common.lookup").handler is handlers["a"]
        assert kernel.tools.resolve(b_scope, "common.lookup").handler is handlers["b"]

        await activation.product("a").close()
        assert kernel.tools.resolve(a_scope, "common.lookup") is None
        assert kernel.tools.resolve(b_scope, "common.lookup").handler is handlers["b"]
        await activation.close()
        return kernel.tools.resolve(b_scope, "common.lookup")

    assert asyncio.run(exercise()) is None


def test_partial_install_failure_rolls_back_every_product_and_tool() -> None:
    async def exercise():  # type: ignore[no-untyped-def]
        plan = make_plan("a", "b")

        async def install_a(context: ProductActivationContext) -> None:
            async def handler(call, arguments):  # type: ignore[no-untyped-def]
                return "a"

            context.register_tool(
                ToolSpec(name="a.tool", effects=frozenset({ToolEffect.READ})),
                handler,
            )

        async def install_b(context: ProductActivationContext) -> None:
            async def handler(call, arguments):  # type: ignore[no-untyped-def]
                return "b"

            context.register_tool(
                ToolSpec(name="b.tool", effects=frozenset({ToolEffect.READ})),
                handler,
            )
            raise RuntimeError("install b exploded")

        by_id = {item.product_id: item for item in plan.products}
        a = TestActivator(by_id["a"].descriptor, install_callback=install_a)
        b = TestActivator(by_id["b"].descriptor, install_callback=install_b)
        kernel = HarnessKernel()
        with pytest.raises(CustomerBundleActivationError) as captured:
            await kernel.activate_customer_bundle("tenant-1", plan, [a, b])
        assert captured.value.code is CustomerBundleActivationCode.INSTALL_FAILED
        assert isinstance(captured.value.activation_cause, RuntimeError)
        a_scope = RequestScope(
            path=ScopePath.product("tenant-1", "a"),
            principal_id="user-1",
        )
        b_scope = RequestScope(
            path=ScopePath.product("tenant-1", "b"),
            principal_id="user-1",
        )
        return (
            kernel.tools.resolve(a_scope, "a.tool"),
            kernel.tools.resolve(b_scope, "b.tool"),
            await kernel.active("tenant-1"),
            a,
            b,
        )

    a_tool, b_tool, active, a, b = asyncio.run(exercise())
    assert a_tool is b_tool is active is None
    assert a.installed == b.installed == 1
    assert a.cleaned == b.cleaned == 1


@pytest.mark.parametrize(
    ("case", "expected"),
    [
        ("missing", CustomerBundleActivationCode.MISSING_ACTIVATOR),
        ("extra", CustomerBundleActivationCode.EXTRA_ACTIVATOR),
        ("duplicate", CustomerBundleActivationCode.DUPLICATE_ACTIVATOR),
        ("descriptor", CustomerBundleActivationCode.DESCRIPTOR_MISMATCH),
        ("prepared", CustomerBundleActivationCode.DESCRIPTOR_MISMATCH),
        ("profile", CustomerBundleActivationCode.PROFILE_REQUIRED),
        ("binding", CustomerBundleActivationCode.INVALID_BINDING),
    ],
)
def test_invalid_activation_inputs_fail_before_install(case, expected) -> None:  # type: ignore[no-untyped-def]
    plan = make_plan("a")
    correct = plan.products[0].descriptor
    primary = TestActivator(correct)
    supplied: list[TestActivator] = [primary]
    if case == "missing":
        supplied = []
    elif case == "extra":
        supplied.append(TestActivator(descriptor("b")))
    elif case == "duplicate":
        supplied.append(TestActivator(correct))
    elif case == "descriptor":
        primary.descriptor = descriptor("a", version="1.0.1")
    elif case == "prepared":
        primary.prepared_descriptor = descriptor("a", version="1.0.1")
    elif case == "profile":
        primary.bindings_override = {}
    elif case == "binding":
        primary.bindings_override = {
            PROFILE_PROVIDER: InMemoryProfileProvider(),
            CUSTOMER360_PROVIDER: InMemoryCustomer360Provider(
                InMemoryProfileProvider()
            ),
        }

    async def exercise():  # type: ignore[no-untyped-def]
        kernel = HarnessKernel()
        with pytest.raises(CustomerBundleActivationError) as captured:
            await kernel.activate_customer_bundle("tenant-1", plan, supplied)
        return captured.value, await kernel.active("tenant-1")

    failure, active = asyncio.run(exercise())
    assert failure.code is expected
    assert active is None
    assert all(item.installed == 0 for item in supplied)
    if primary.prepared:
        assert primary.cleaned == 1


def test_reused_profile_instance_is_rejected_and_staged_resources_close() -> None:
    plan = make_plan("a", "b")
    shared = InMemoryProfileProvider()
    activators = [
        TestActivator(item.descriptor, shared_profile=shared) for item in plan.products
    ]

    async def exercise():  # type: ignore[no-untyped-def]
        with pytest.raises(CustomerBundleActivationError) as captured:
            await HarnessKernel().activate_customer_bundle(
                "tenant-1", plan, activators
            )
        return captured.value

    failure = asyncio.run(exercise())
    assert failure.code is CustomerBundleActivationCode.PROFILE_INSTANCE_REUSED
    assert [item.cleaned for item in activators] == [1, 1]
    assert all(item.installed == 0 for item in activators)


def test_tenant_conflict_stale_release_and_kernel_close() -> None:
    async def exercise():  # type: ignore[no-untyped-def]
        plan = make_plan("a")
        kernel = HarnessKernel()
        first = await kernel.activate_customer_bundle(
            "tenant-1", plan, [TestActivator(plan.products[0].descriptor)]
        )
        with pytest.raises(CustomerBundleActivationError) as captured:
            await kernel.activate_customer_bundle(
                "tenant-1", plan, [TestActivator(plan.products[0].descriptor)]
            )
        stale_activation_id = first._activation_id
        await first.close()
        replacement = await kernel.activate_customer_bundle(
            "tenant-1", plan, [TestActivator(plan.products[0].descriptor)]
        )
        await kernel._finish_close("tenant-1", stale_activation_id)
        assert await kernel.active("tenant-1") is replacement
        await kernel.close()
        with pytest.raises(CustomerBundleActivationError) as closed:
            await kernel.activate_customer_bundle(
                "tenant-2", plan, [TestActivator(plan.products[0].descriptor)]
            )
        return captured.value, closed.value, replacement

    conflict, closed, replacement = asyncio.run(exercise())
    assert conflict.code is CustomerBundleActivationCode.ACTIVATION_CONFLICT
    assert closed.code is CustomerBundleActivationCode.KERNEL_CLOSED
    assert replacement.closed is True


def test_install_and_cleanup_failure_preserve_both_and_quarantine_tenant() -> None:
    async def exercise():  # type: ignore[no-untyped-def]
        plan = make_plan("a")

        async def fail(context: ProductActivationContext) -> None:
            raise RuntimeError("original install failure")

        activator = TestActivator(
            plan.products[0].descriptor,
            install_callback=fail,
            fail_cleanup=True,
        )
        kernel = HarnessKernel()
        with pytest.raises(CustomerBundleActivationError) as captured:
            await kernel.activate_customer_bundle("tenant-1", plan, [activator])
        with pytest.raises(CustomerBundleActivationError) as retry:
            await kernel.activate_customer_bundle(
                "tenant-1", plan, [TestActivator(plan.products[0].descriptor)]
            )
        return captured.value, retry.value

    failure, retry = asyncio.run(exercise())
    assert failure.code is CustomerBundleActivationCode.INSTALL_FAILED
    assert str(failure.activation_cause) == "original install failure"
    assert failure.cleanup_error is not None
    assert retry.code is CustomerBundleActivationCode.ACTIVATION_CONFLICT
    assert retry.detail["poisoned"] is True


def test_activation_run_selects_the_exact_product_workflow_and_rejects_bad_scopes() -> None:
    @dataclass
    class FinalWorkflow:
        output: str
        calls: int = 0

        async def next(self, request, frame, prompt):  # type: ignore[no-untyped-def]
            self.calls += 1
            return WorkflowDecision.final(self.output)

    async def exercise():  # type: ignore[no-untyped-def]
        plan = make_plan("a", "b")
        workflows = {product_id: FinalWorkflow(product_id) for product_id in plan.product_ids}
        activators = [
            TestActivator(
                item.descriptor,
                bindings_override={
                    PROFILE_PROVIDER: InMemoryProfileProvider(),
                    WORKFLOW: workflows[item.product_id],
                },
            )
            for item in plan.products
        ]
        authority = InMemoryCapabilityAuthority()
        journal = InMemoryAuditJournal()
        kernel = HarnessKernel(capabilities=authority, journal=journal)
        activation = await kernel.activate_customer_bundle(
            "tenant-1", plan, activators
        )

        async def run_product(product_id: str, run_id: str):  # type: ignore[no-untyped-def]
            product_scope = activation.product(product_id).path
            grant = await authority.issue(product_scope, tool_identities=())
            request_scope = RequestScope(
                path=product_scope,
                principal_id="user-1",
                purpose="e2e-run",
            )
            return await activation.run(
                RunRequest(
                    run_id=run_id,
                    scope=request_scope,
                    grant_id=grant.grant_id,
                    input={"question": product_id},
                )
            ), grant.grant_id

        a_result, grant_id = await run_product("a", "run-a")
        b_result, _ = await run_product("b", "run-b")
        events_before_invalid = await journal.events()

        invalid_paths = (
            ScopePath.product("other-tenant", "a"),
            ScopePath.product("tenant-1", "unselected"),
            ScopePath.agent(
                "other-tenant",
                "a",
                "agent-1",
                "session-1",
            ),
        )
        invalid_codes: list[CustomerBundleActivationCode] = []
        for index, path in enumerate(invalid_paths):
            with pytest.raises(CustomerBundleActivationError) as captured:
                await activation.run(
                    RunRequest(
                        run_id=f"invalid-{index}",
                        scope=RequestScope(path=path, principal_id="user-1"),
                        grant_id=grant_id,
                        input=None,
                    )
                )
            invalid_codes.append(captured.value.code)
        assert await journal.events() == events_before_invalid

        disposer_started = asyncio.Event()
        allow_close = asyncio.Event()

        async def block_close() -> None:
            disposer_started.set()
            await allow_close.wait()

        activation.tenant.effects.callback("test:block-close", block_close)
        close_task = asyncio.create_task(activation.close())
        await disposer_started.wait()
        with pytest.raises(CustomerBundleActivationError) as closing:
            await activation.run(
                RunRequest(
                    run_id="closing-run",
                    scope=RequestScope(
                        path=activation.product("a").path,
                        principal_id="user-1",
                    ),
                    grant_id=grant_id,
                    input=None,
                )
            )
        allow_close.set()
        await close_task
        with pytest.raises(CustomerBundleActivationError) as closed:
            await activation.run(
                RunRequest(
                    run_id="closed-run",
                    scope=RequestScope(
                        path=ScopePath.product("tenant-1", "a"),
                        principal_id="user-1",
                    ),
                    grant_id=grant_id,
                    input=None,
                )
            )
        return (
            a_result,
            b_result,
            workflows,
            invalid_codes,
            closing.value,
            closed.value,
        )

    a_result, b_result, workflows, invalid_codes, closing, closed = asyncio.run(
        exercise()
    )
    assert a_result.output == "a"
    assert b_result.output == "b"
    assert workflows["a"].calls == workflows["b"].calls == 1
    assert invalid_codes == [CustomerBundleActivationCode.RUN_SCOPE_MISMATCH] * 3
    assert closing.code is CustomerBundleActivationCode.ACTIVATION_CLOSED
    assert closed.code is CustomerBundleActivationCode.ACTIVATION_CLOSED


def test_capability_grants_cannot_cross_customer_bundle_reactivation() -> None:
    @dataclass
    class FinalWorkflow:
        async def next(self, request, frame, prompt):  # type: ignore[no-untyped-def]
            return WorkflowDecision.final("ok")

    async def exercise():  # type: ignore[no-untyped-def]
        plan = make_plan("a")
        authority = InMemoryCapabilityAuthority()
        kernel = HarnessKernel(capabilities=authority)

        def activator():  # type: ignore[no-untyped-def]
            return TestActivator(
                plan.products[0].descriptor,
                bindings_override={
                    PROFILE_PROVIDER: InMemoryProfileProvider(),
                    WORKFLOW: FinalWorkflow(),
                },
            )

        first = await kernel.activate_customer_bundle("tenant-1", plan, [activator()])
        first_grant = await authority.issue(first.product("a").path, tool_identities=())
        assert await authority.resolve(first_grant.grant_id) == first_grant
        await first.close()
        assert await authority.resolve(first_grant.grant_id) is None

        # A grant created while no activation owns the scope is deliberately not
        # accepted after a new activation takes ownership.
        between_grant = await authority.issue(
            ScopePath.product("tenant-1", "a"),
            tool_identities=(),
        )
        second = await kernel.activate_customer_bundle("tenant-1", plan, [activator()])
        second_grant = await authority.issue(second.product("a").path, tool_identities=())
        assert await authority.resolve(first_grant.grant_id) is None
        assert await authority.resolve(between_grant.grant_id) is None
        assert await authority.resolve(second_grant.grant_id) == second_grant
        await second.close()

    asyncio.run(exercise())


def test_activation_run_reports_missing_workflow_before_entering_runner() -> None:
    async def exercise():  # type: ignore[no-untyped-def]
        plan = make_plan("a")
        authority = InMemoryCapabilityAuthority()
        journal = InMemoryAuditJournal()
        kernel = HarnessKernel(capabilities=authority, journal=journal)
        activation = await kernel.activate_customer_bundle(
            "tenant-1", plan, [TestActivator(plan.products[0].descriptor)]
        )
        grant = await authority.issue(activation.product("a").path, tool_identities=())
        request = RunRequest(
            run_id="missing-workflow",
            scope=scope_for(activation, "a"),
            grant_id=grant.grant_id,
            input=None,
        )
        with pytest.raises(CustomerBundleActivationError) as captured:
            await activation.run(request)
        events = await journal.events()
        await activation.close()
        return captured.value, events

    failure, events = asyncio.run(exercise())
    assert failure.code is CustomerBundleActivationCode.WORKFLOW_UNAVAILABLE
    assert events == ()


def test_close_drains_an_accepted_run_before_unloading_product_resources() -> None:
    class BlockingWorkflow:
        def __init__(self) -> None:
            self.started = asyncio.Event()
            self.finish = asyncio.Event()

        async def next(self, request, frame, prompt):  # type: ignore[no-untyped-def]
            self.started.set()
            await self.finish.wait()
            return WorkflowDecision.final("finished")

    async def exercise():  # type: ignore[no-untyped-def]
        plan = make_plan("a")
        workflow = BlockingWorkflow()
        authority = InMemoryCapabilityAuthority()
        activator = TestActivator(
            plan.products[0].descriptor,
            bindings_override={
                PROFILE_PROVIDER: InMemoryProfileProvider(),
                WORKFLOW: workflow,
            },
        )
        kernel = HarnessKernel(capabilities=authority)
        activation = await kernel.activate_customer_bundle(
            "tenant-1", plan, [activator]
        )
        product = activation.product("a")
        grant = await authority.issue(product.path, tool_identities=())
        request = RunRequest(
            run_id="drained-run",
            scope=scope_for(activation, "a"),
            grant_id=grant.grant_id,
            input=None,
        )
        run_task = asyncio.create_task(activation.run(request))
        await workflow.started.wait()
        close_task = asyncio.create_task(activation.close())
        for _ in range(100):
            if product.effects.state is EffectScopeState.CLOSING:
                break
            await asyncio.sleep(0)
        assert product.effects.state is EffectScopeState.CLOSING
        assert close_task.done() is False
        assert activator.cleaned == 0
        workflow.finish.set()
        result = await run_task
        await close_task
        return result, activator

    result, activator = asyncio.run(exercise())
    assert result.output == "finished"
    assert activator.cleaned == 1
