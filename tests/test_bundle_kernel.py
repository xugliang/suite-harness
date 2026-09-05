"""Strict descriptors, dependency planning and transactional bundle install."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

import pytest
from pydantic import BaseModel, ConfigDict, ValidationError

from suiteharness.runtime import (
    BundleInstallContext,
    BundleResolutionCode,
    BundleResolutionError,
    install_bundle_plan,
    resolve_bundle_plan,
)
from suiteharness.runtime.descriptor import (
    BundleConflict,
    BundleManifest,
    BundleRequirement,
    ProductDescriptor,
)
from suiteharness.runtime.scopes import RootContext


class ProductConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    mode: str


@dataclass
class DummyBundle:
    manifest: BundleManifest
    events: list[str] | None = None
    fail: bool = False

    async def install(self, context: BundleInstallContext) -> None:
        if self.events is not None:
            bundle_id = self.manifest.bundle_id
            self.events.append(f"install:{bundle_id}")
            context.own(
                f"dispose:{bundle_id}",
                lambda: self.events.append(f"dispose:{bundle_id}"),
            )
        if self.fail:
            raise RuntimeError(f"failed:{self.manifest.bundle_id}")


def manifest(
    bundle_id: str,
    *,
    version: str = "1.0.0",
    requires: tuple[BundleRequirement, ...] = (),
    conflicts: tuple[BundleConflict, ...] = (),
    provides: frozenset[str] = frozenset(),
    capabilities: frozenset[str] = frozenset(),
    harness_api: str = ">=0.1,<1",
) -> BundleManifest:
    return BundleManifest(
        bundle_id=bundle_id,
        version=version,
        harness_api=harness_api,
        requires=requires,
        conflicts=conflicts,
        provides=provides,
        capabilities=capabilities,
    )


def descriptor(
    *requirements: BundleRequirement,
    capabilities: frozenset[str] = frozenset(),
) -> ProductDescriptor:
    return ProductDescriptor(
        product_id="alpha",
        version="1.0.0",
        harness_api=">=0.1,<1",
        bundles=requirements,
        capabilities=capabilities,
        config_model=ProductConfig,
    )


def test_descriptors_are_strict_and_validate_product_config() -> None:
    with pytest.raises(ValidationError):
        BundleManifest(
            bundle_id="memory",
            version="1.0.0",
            harness_api=">=0.1",
            unknown=True,
        )
    with pytest.raises(ValidationError):
        manifest("Bad Name")
    with pytest.raises(ValidationError):
        manifest("memory", version="01.0")

    product = descriptor()
    assert product.parse_config({"mode": "standard"}) == ProductConfig(mode="standard")
    with pytest.raises(ValidationError):
        product.parse_config({"mode": "standard", "unknown": True})


def test_plan_resolves_dependency_closure_in_topological_order() -> None:
    core = DummyBundle(
        manifest(
            "memory-core",
            provides=frozenset({"memory.store"}),
            capabilities=frozenset({"memory:read"}),
        )
    )
    workflow = DummyBundle(
        manifest(
            "alpha-workflow",
            requires=(
                BundleRequirement(bundle_id="memory-core", version=">=1,<2"),
                BundleRequirement(bundle_id="optional-ui", optional=True),
            ),
            provides=frozenset({"workflow.agent"}),
            capabilities=frozenset({"workflow:run"}),
        )
    )
    product = descriptor(
        BundleRequirement(bundle_id="alpha-workflow"),
        capabilities=frozenset({"memory:read", "workflow:run"}),
    )
    plan = resolve_bundle_plan(product, [workflow, core], harness_version="0.1.0")
    assert plan.bundle_ids == ("memory-core", "alpha-workflow")


@pytest.mark.parametrize(
    ("product", "bundles", "code"),
    [
        (
            descriptor(BundleRequirement(bundle_id="missing")),
            [],
            BundleResolutionCode.MISSING,
        ),
        (
            descriptor(BundleRequirement(bundle_id="core", version=">=2")),
            [DummyBundle(manifest("core"))],
            BundleResolutionCode.VERSION,
        ),
        (
            descriptor(BundleRequirement(bundle_id="future")),
            [DummyBundle(manifest("future", harness_api=">=2"))],
            BundleResolutionCode.HARNESS_API,
        ),
        (
            descriptor(BundleRequirement(bundle_id="writer")),
            [DummyBundle(manifest("writer", capabilities=frozenset({"db:write"})))],
            BundleResolutionCode.CAPABILITY_UNDECLARED,
        ),
    ],
)
def test_plan_rejects_missing_or_incompatible_dependencies(
    product: ProductDescriptor,
    bundles: list[DummyBundle],
    code: BundleResolutionCode,
) -> None:
    with pytest.raises(BundleResolutionError) as captured:
        resolve_bundle_plan(product, bundles, harness_version="0.1.0")
    assert captured.value.code is code


def test_plan_rejects_cycles_conflicts_duplicate_ids_and_providers() -> None:
    a = DummyBundle(
        manifest("a", requires=(BundleRequirement(bundle_id="b"),))
    )
    b = DummyBundle(
        manifest("b", requires=(BundleRequirement(bundle_id="a"),))
    )
    with pytest.raises(BundleResolutionError) as captured:
        resolve_bundle_plan(
            descriptor(BundleRequirement(bundle_id="a")),
            [a, b],
            harness_version="0.1.0",
        )
    assert captured.value.code is BundleResolutionCode.CYCLE

    conflict_a = DummyBundle(
        manifest("conflict-a", conflicts=(BundleConflict(bundle_id="conflict-b"),))
    )
    conflict_b = DummyBundle(manifest("conflict-b"))
    both = descriptor(
        BundleRequirement(bundle_id="conflict-a"),
        BundleRequirement(bundle_id="conflict-b"),
    )
    with pytest.raises(BundleResolutionError) as captured:
        resolve_bundle_plan(both, [conflict_a, conflict_b], harness_version="0.1.0")
    assert captured.value.code is BundleResolutionCode.CONFLICT

    duplicate = DummyBundle(manifest("conflict-a"))
    with pytest.raises(BundleResolutionError) as captured:
        resolve_bundle_plan(both, [conflict_a, duplicate], harness_version="0.1.0")
    assert captured.value.code is BundleResolutionCode.DUPLICATE

    provider_a = DummyBundle(manifest("provider-a", provides=frozenset({"memory.store"})))
    provider_b = DummyBundle(manifest("provider-b", provides=frozenset({"memory.store"})))
    providers = descriptor(
        BundleRequirement(bundle_id="provider-a"),
        BundleRequirement(bundle_id="provider-b"),
    )
    with pytest.raises(BundleResolutionError) as captured:
        resolve_bundle_plan(providers, [provider_a, provider_b], harness_version="0.1.0")
    assert captured.value.code is BundleResolutionCode.PROVIDER_CONFLICT


def test_install_is_effect_owned_and_rolls_back_in_reverse_order() -> None:
    async def exercise() -> list[str]:
        events: list[str] = []
        first = DummyBundle(manifest("first"), events=events)
        second = DummyBundle(
            manifest(
                "second",
                requires=(BundleRequirement(bundle_id="first"),),
            ),
            events=events,
            fail=True,
        )
        product_descriptor = descriptor(BundleRequirement(bundle_id="second"))
        plan = resolve_bundle_plan(
            product_descriptor,
            [second, first],
            harness_version="0.1.0",
        )
        root = RootContext()
        product = root.tenant("tenant-a").product("alpha")
        with pytest.raises(RuntimeError, match="failed:second"):
            await install_bundle_plan(plan, product, ProductConfig(mode="standard"))
        await root.close()
        return events

    assert asyncio.run(exercise()) == [
        "install:first",
        "install:second",
        "dispose:second",
        "dispose:first",
    ]


def test_bundle_install_context_does_not_publish_scope_escape_hatches() -> None:
    observed: list[object] = []

    @dataclass
    class InspectingBundle:
        manifest: BundleManifest

        async def install(self, context: BundleInstallContext) -> None:
            observed.extend(
                [
                    context.path,
                    context.config,
                    hasattr(context, "product"),
                    hasattr(context, "effects"),
                ]
            )

    async def exercise() -> None:
        bundle = InspectingBundle(manifest("inspect"))
        product_descriptor = descriptor(BundleRequirement(bundle_id="inspect"))
        plan = resolve_bundle_plan(
            product_descriptor,
            [bundle],
            harness_version="0.1.0",
        )
        root = RootContext()
        product = root.tenant("tenant-a").product("alpha")
        await install_bundle_plan(plan, product, ProductConfig(mode="standard"))
        await root.close()

    asyncio.run(exercise())
    assert observed[0].product_id == "alpha"
    assert observed[1] == ProductConfig(mode="standard")
    assert observed[2:] == [False, False]
