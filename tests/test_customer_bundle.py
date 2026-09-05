"""Customer product combinations are strict, deterministic, and side-effect free."""

from __future__ import annotations

from typing import Literal

import pytest
from packaging.version import Version
from pydantic import BaseModel, ConfigDict, ValidationError

from suiteharness.runtime.customer_bundle import (
    Customer360Mode,
    Customer360SharingPlan,
    CustomerBundleManifest,
    CustomerBundlePlan,
    CustomerBundleResolutionCode,
    CustomerBundleResolutionError,
    CustomerProductSelection,
    ProductCatalog,
    ResolvedCustomer360Rule,
    ResolvedCustomerProduct,
    resolve_customer_bundle,
)
from suiteharness.runtime.descriptor import ProductDescriptor


class AlphaConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    tier: Literal["standard", "premium"] = "standard"


class BetaConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    retry_limit: int = 3


class GammaConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class NestedConfig(BaseModel):
    # Deliberately not frozen: product configs are allowed to contain ordinary
    # mutable containers and the bundle boundary must still snapshot them.
    model_config = ConfigDict(extra="forbid")

    routing: dict[str, list[str]]


def descriptor(
    product_id: str,
    *,
    version: str = "1.2.0",
    harness_api: str = ">=0.1,<1",
) -> ProductDescriptor:
    models = {
        "alpha": AlphaConfig,
        "beta": BetaConfig,
        "gamma": GammaConfig,
    }
    return ProductDescriptor(
        product_id=product_id,
        version=version,
        harness_api=harness_api,
        config_model=models[product_id],
    )


def isolated_manifest() -> dict[str, object]:
    return {
        "schema_version": "1",
        "customer_bundle_id": "alpha-only",
        "version": "0.1.0",
        "harness_api": ">=0.1,<1",
        "products": [
            {
                "product_id": "alpha",
                "version": ">=0.1,<2",
                "config": {},
            }
        ],
    }


def curated_manifest() -> dict[str, object]:
    return {
        "schema_version": "1",
        "customer_bundle_id": "alpha-beta",
        "version": "0.1.0",
        "harness_api": ">=0.1,<1",
        "products": [
            {"product_id": "alpha", "version": ">=0.1,<2", "config": {}},
            {"product_id": "beta", "version": ">=0.1,<2", "config": {}},
        ],
        "customer360": {
            "mode": "curated",
            "rules": [
                {
                    "producer": "alpha",
                    "consumer": "beta",
                    "predicates": [
                        "profile.identity.display-name",
                        "profile.attribute.one",
                    ],
                }
            ],
        },
    }


def test_single_product_defaults_to_private_profile_isolation() -> None:
    manifest = CustomerBundleManifest.model_validate(isolated_manifest())
    plan = resolve_customer_bundle(
        manifest,
        ProductCatalog([descriptor("alpha")]),
        harness_version="0.1.0",
    )

    assert plan.product_ids == ("alpha",)
    assert plan.private_profile_product_ids == ("alpha",)
    assert plan.sharing.mode is Customer360Mode.ISOLATED
    assert plan.sharing.rules == ()
    assert plan.configs["alpha"] == AlphaConfig()


def test_manifest_and_plan_deep_snapshot_nested_product_config() -> None:
    caller_config = {"routing": {"priority": ["route-one"]}}
    raw = {
        "customer_bundle_id": "nested",
        "version": "1.0.0",
        "harness_api": ">=0.1,<1",
        "products": [
            {
                "product_id": "nested",
                "version": ">=1,<2",
                "config": caller_config,
            }
        ],
    }
    manifest = CustomerBundleManifest.model_validate(raw)

    # Pydantic's outer dict copy is not enough for Any-valued nested containers.
    caller_config["routing"]["priority"].append("caller-mutation")
    assert manifest.products[0].config == {
        "routing": {"priority": ["route-one"]}
    }

    nested_descriptor = ProductDescriptor(
        product_id="nested",
        version="1.0.0",
        harness_api=">=0.1,<1",
        config_model=NestedConfig,
    )
    plan = resolve_customer_bundle(
        manifest,
        ProductCatalog([nested_descriptor]),
        harness_version="0.1.0",
    )

    # Mutating the source manifest after resolution cannot rewrite the plan.
    manifest.products[0].config["routing"]["priority"].append("manifest-mutation")
    assert plan.manifest.products[0].config == {
        "routing": {"priority": ["route-one"]}
    }

    # Public plan/config views keep their existing mutable model semantics, but
    # those views are detached from the resolver-owned activation snapshot.
    exposed_product_config = plan.products[0].config
    assert isinstance(exposed_product_config, NestedConfig)
    exposed_product_config.routing["priority"].append("product-view-mutation")
    exposed_config_map = plan.configs["nested"]
    assert isinstance(exposed_config_map, NestedConfig)
    exposed_config_map.routing["priority"].append("mapping-view-mutation")
    assert plan.configs["nested"].model_dump() == {
        "routing": {"priority": ["route-one"]}
    }

    # Passing an already parsed config model is detached as well.
    caller_model = NestedConfig(routing={"priority": ["route-two"]})
    parsed_model = nested_descriptor.parse_config(caller_model)
    caller_model.routing["priority"].append("caller-model-mutation")
    assert parsed_model.model_dump() == {"routing": {"priority": ["route-two"]}}


def test_explicit_curated_sharing_and_resolve_order_are_deterministic() -> None:
    raw = curated_manifest()
    raw["products"] = list(reversed(raw["products"]))  # type: ignore[index]
    raw["customer360"]["rules"][0]["predicates"].reverse()  # type: ignore[index]
    first = resolve_customer_bundle(
        raw,
        ProductCatalog([descriptor("alpha"), descriptor("beta")]),
        harness_version="0.1.0",
    )
    second = resolve_customer_bundle(
        curated_manifest(),
        ProductCatalog([descriptor("beta"), descriptor("alpha")]),
        harness_version="0.1.0",
    )

    assert first.product_ids == second.product_ids == ("alpha", "beta")
    assert first.sharing.mode is Customer360Mode.CURATED
    assert first.sharing.rules == second.sharing.rules
    assert first.sharing.rules[0].producer == "alpha"
    assert first.sharing.rules[0].consumer == "beta"
    assert first.sharing.rules[0].predicates == (
        "profile.attribute.one",
        "profile.identity.display-name",
    )


@pytest.mark.parametrize(
    "mutate, message",
    [
        (
            lambda raw: raw["products"].append(raw["products"][0].copy()),
            "must not repeat a product_id",
        ),
        (
            lambda raw: raw["customer360"]["rules"][0].update(consumer="alpha"),
            "cannot target the producer itself",
        ),
        (
            lambda raw: raw["customer360"]["rules"][0].update(predicates=[]),
            "must not be empty",
        ),
        (
            lambda raw: raw["customer360"]["rules"][0].update(predicates=[""]),
            "invalid sharing predicate",
        ),
        (
            lambda raw: raw["customer360"]["rules"][0].update(consumer="gamma"),
            "references unselected product",
        ),
    ],
)
def test_invalid_product_graphs_fail_during_manifest_validation(mutate, message: str) -> None:
    raw = curated_manifest()
    mutate(raw)
    with pytest.raises(ValidationError, match=message):
        CustomerBundleManifest.model_validate(raw)


def test_sharing_requires_an_explicit_curated_declaration() -> None:
    raw = curated_manifest()
    raw["customer360"]["mode"] = "isolated"  # type: ignore[index]
    with pytest.raises(ValidationError, match="require mode='curated'"):
        CustomerBundleManifest.model_validate(raw)


@pytest.mark.parametrize(
    ("raw", "catalog", "harness_version", "code"),
    [
        (
            {
                "customer_bundle_id": "unknown",
                "version": "1.0.0",
                "harness_api": ">=0.1,<1",
                "products": [{"product_id": "gamma", "version": ">=1", "config": {}}],
            },
            ProductCatalog([descriptor("alpha")]),
            "0.1.0",
            CustomerBundleResolutionCode.UNKNOWN_PRODUCT,
        ),
        (
            {
                "customer_bundle_id": "version",
                "version": "1.0.0",
                "harness_api": ">=0.1,<1",
                "products": [{"product_id": "alpha", "version": ">=2", "config": {}}],
            },
            ProductCatalog([descriptor("alpha")]),
            "0.1.0",
            CustomerBundleResolutionCode.VERSION_MISMATCH,
        ),
        (
            {
                "customer_bundle_id": "config",
                "version": "1.0.0",
                "harness_api": ">=0.1,<1",
                "products": [
                    {"product_id": "alpha", "version": ">=1", "config": {"unknown": True}}
                ],
            },
            ProductCatalog([descriptor("alpha")]),
            "0.1.0",
            CustomerBundleResolutionCode.CONFIG_INVALID,
        ),
        (
            {
                "customer_bundle_id": "api",
                "version": "1.0.0",
                "harness_api": ">=0.1,<1",
                "products": [{"product_id": "alpha", "version": ">=1", "config": {}}],
            },
            ProductCatalog([descriptor("alpha", harness_api=">=2")]),
            "0.1.0",
            CustomerBundleResolutionCode.HARNESS_API_MISMATCH,
        ),
    ],
)
def test_unknown_version_config_and_harness_errors_are_typed(
    raw: dict[str, object],
    catalog: ProductCatalog,
    harness_version: str,
    code: CustomerBundleResolutionCode,
) -> None:
    with pytest.raises(CustomerBundleResolutionError) as captured:
        resolve_customer_bundle(raw, catalog, harness_version=harness_version)
    assert captured.value.code is code


def test_three_product_plan_is_sorted_and_catalog_rejects_duplicates() -> None:
    raw = {
        "customer_bundle_id": "abc",
        "version": "1.0.0",
        "harness_api": ">=0.1,<1",
        "products": [
            {"product_id": "gamma", "version": ">=1", "config": {}},
            {"product_id": "alpha", "version": ">=1", "config": {}},
            {"product_id": "beta", "version": ">=1", "config": {}},
        ],
    }
    plan = resolve_customer_bundle(
        raw,
        ProductCatalog([descriptor("gamma"), descriptor("beta"), descriptor("alpha")]),
        harness_version="0.1.0",
    )
    assert plan.product_ids == ("alpha", "beta", "gamma")

    with pytest.raises(CustomerBundleResolutionError) as captured:
        ProductCatalog([descriptor("alpha"), descriptor("alpha")])
    assert captured.value.code is CustomerBundleResolutionCode.DUPLICATE_CATALOG_PRODUCT


def test_manifest_rejects_unknown_fields_and_unselected_sharing_product() -> None:
    raw = isolated_manifest()
    raw["unexpected"] = True
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        CustomerBundleManifest.model_validate(raw)

    invalid_sharing = isolated_manifest()
    invalid_sharing["customer360"] = {
        "mode": "curated",
        "rules": [
            {
                "producer": "alpha",
                "consumer": "beta",
                "predicates": ["profile.attribute.one"],
            }
        ],
    }
    with pytest.raises(ValidationError, match="references unselected product"):
        CustomerBundleManifest.model_validate(invalid_sharing)


def test_customer_bundle_version_is_canonical_and_its_harness_api_is_enforced() -> None:
    raw = isolated_manifest()
    raw["version"] = "01.0"
    with pytest.raises(ValidationError, match="must be canonical"):
        CustomerBundleManifest.model_validate(raw)

    raw = isolated_manifest()
    raw["harness_api"] = ">=2"
    with pytest.raises(CustomerBundleResolutionError) as captured:
        resolve_customer_bundle(
            raw,
            ProductCatalog([descriptor("alpha")]),
            harness_version="0.1.0",
        )
    assert captured.value.code is CustomerBundleResolutionCode.BUNDLE_HARNESS_API_MISMATCH


def test_public_plan_constructor_revalidates_and_canonicalizes_valid_input() -> None:
    resolved = resolve_customer_bundle(
        curated_manifest(),
        ProductCatalog([descriptor("alpha"), descriptor("beta")]),
        harness_version="0.1.0",
    )

    rebuilt = CustomerBundlePlan(
        manifest=resolved.manifest,
        harness_version=resolved.harness_version,
        products=tuple(reversed(resolved.products)),
        sharing=resolved.sharing,
    )

    assert rebuilt.product_ids == ("alpha", "beta")
    assert rebuilt.sharing == resolved.sharing
    assert rebuilt.configs == resolved.configs


def test_public_plan_constructor_rejects_forged_product_collection() -> None:
    resolved = resolve_customer_bundle(
        isolated_manifest(),
        ProductCatalog([descriptor("alpha")]),
        harness_version="0.1.0",
    )
    product = resolved.products[0]

    with pytest.raises(ValueError, match="exactly match manifest selections"):
        CustomerBundlePlan(
            manifest=resolved.manifest,
            harness_version=resolved.harness_version,
            products=(),
            sharing=resolved.sharing,
        )

    with pytest.raises(ValueError, match="must not repeat"):
        CustomerBundlePlan(
            manifest=resolved.manifest,
            harness_version=resolved.harness_version,
            products=(product, product),
            sharing=resolved.sharing,
        )

    forged_selection = ResolvedCustomerProduct(
        selection=CustomerProductSelection(
            product_id="alpha",
            version=">=1",
            config={},
        ),
        descriptor=product.descriptor,
        config=product.config,
    )
    with pytest.raises(ValueError, match="resolved selection does not match manifest"):
        CustomerBundlePlan(
            manifest=resolved.manifest,
            harness_version=resolved.harness_version,
            products=(forged_selection,),
            sharing=resolved.sharing,
        )

    mismatched_identity = ResolvedCustomerProduct(
        selection=product.selection,
        descriptor=descriptor("beta"),
        config=BetaConfig(),
    )
    with pytest.raises(ValueError, match="descriptor and selection product_id must match"):
        CustomerBundlePlan(
            manifest=resolved.manifest,
            harness_version=resolved.harness_version,
            products=(mismatched_identity,),
            sharing=resolved.sharing,
        )


@pytest.mark.parametrize(
    ("forged_product", "message"),
    [
        (
            ResolvedCustomerProduct(
                selection=CustomerProductSelection(
                    product_id="alpha",
                    version=">=0.1,<2",
                    config={},
                ),
                descriptor=descriptor("alpha", version="3.0.0"),
                config=AlphaConfig(),
            ),
            "descriptor version does not match selection",
        ),
        (
            ResolvedCustomerProduct(
                selection=CustomerProductSelection(
                    product_id="alpha",
                    version=">=0.1,<2",
                    config={},
                ),
                descriptor=descriptor("alpha", harness_api=">=2"),
                config=AlphaConfig(),
            ),
            "descriptor does not support the plan harness version",
        ),
        (
            ResolvedCustomerProduct(
                selection=CustomerProductSelection(
                    product_id="alpha",
                    version=">=0.1,<2",
                    config={},
                ),
                descriptor=descriptor("alpha"),
                config=AlphaConfig(tier="premium"),
            ),
            "resolved config does not match manifest",
        ),
        (
            ResolvedCustomerProduct(
                selection=CustomerProductSelection(
                    product_id="alpha",
                    version=">=0.1,<2",
                    config={},
                ),
                descriptor=descriptor("alpha"),
                config=BetaConfig(),
            ),
            "resolved config does not match manifest",
        ),
    ],
)
def test_public_plan_constructor_rejects_forged_descriptor_or_config(
    forged_product: ResolvedCustomerProduct,
    message: str,
) -> None:
    manifest = CustomerBundleManifest.model_validate(isolated_manifest())
    with pytest.raises(ValueError, match=message):
        CustomerBundlePlan(
            manifest=manifest,
            harness_version=Version("0.1.0"),
            products=(forged_product,),
            sharing=Customer360SharingPlan(mode=Customer360Mode.ISOLATED, rules=()),
        )


def test_public_plan_constructor_rejects_forged_harness_and_sharing() -> None:
    isolated = resolve_customer_bundle(
        isolated_manifest(),
        ProductCatalog([descriptor("alpha")]),
        harness_version="0.1.0",
    )
    with pytest.raises(ValueError, match="manifest does not support"):
        CustomerBundlePlan(
            manifest=isolated.manifest,
            harness_version=Version("2.0.0"),
            products=isolated.products,
            sharing=isolated.sharing,
        )

    curated = resolve_customer_bundle(
        curated_manifest(),
        ProductCatalog([descriptor("alpha"), descriptor("beta")]),
        harness_version="0.1.0",
    )
    with pytest.raises(ValueError, match="sharing plan does not match"):
        CustomerBundlePlan(
            manifest=curated.manifest,
            harness_version=curated.harness_version,
            products=curated.products,
            sharing=Customer360SharingPlan(mode=Customer360Mode.ISOLATED, rules=()),
        )

    forged_rule = ResolvedCustomer360Rule(
        producer="alpha",
        consumer="beta",
        predicates=("profile.attribute.one",),
    )
    with pytest.raises(ValueError, match="sharing plan does not match"):
        CustomerBundlePlan(
            manifest=curated.manifest,
            harness_version=curated.harness_version,
            products=curated.products,
            sharing=Customer360SharingPlan(
                mode=Customer360Mode.CURATED,
                rules=(forged_rule,),
            ),
        )

    with pytest.raises(TypeError, match="sharing mode"):
        CustomerBundlePlan(
            manifest=curated.manifest,
            harness_version=curated.harness_version,
            products=curated.products,
            sharing=Customer360SharingPlan(
                mode="curated",  # type: ignore[arg-type]
                rules=curated.sharing.rules,
            ),
        )


def test_public_plan_constructor_revalidates_constructed_models_and_raw_config() -> None:
    resolved = resolve_customer_bundle(
        isolated_manifest(),
        ProductCatalog([descriptor("alpha")]),
        harness_version="0.1.0",
    )
    selection = resolved.manifest.products[0]
    forged_manifest = CustomerBundleManifest.model_construct(
        schema_version="1",
        customer_bundle_id="forged",
        version="1.0.0",
        harness_api=">=0.1,<1",
        products=(selection, selection),
        customer360=resolved.manifest.customer360,
    )
    with pytest.raises(ValueError, match="invalid manifest"):
        CustomerBundlePlan(
            manifest=forged_manifest,
            harness_version=resolved.harness_version,
            products=resolved.products,
            sharing=resolved.sharing,
        )

    forged_descriptor = ProductDescriptor.model_construct(
        product_id="alpha",
        version="not-a-version",
        harness_api=">=0.1,<1",
        bundles=(),
        capabilities=frozenset(),
        config_model=AlphaConfig,
    )
    forged_product = ResolvedCustomerProduct(
        selection=selection,
        descriptor=forged_descriptor,
        config=AlphaConfig(),
    )
    with pytest.raises(ValueError, match="invalid product descriptor"):
        CustomerBundlePlan(
            manifest=resolved.manifest,
            harness_version=resolved.harness_version,
            products=(forged_product,),
            sharing=resolved.sharing,
        )

    invalid_config_raw = isolated_manifest()
    invalid_config_raw["products"][0]["config"] = {"unknown": True}  # type: ignore[index]
    invalid_config_manifest = CustomerBundleManifest.model_validate(invalid_config_raw)
    forged_config_product = ResolvedCustomerProduct(
        selection=invalid_config_manifest.products[0],
        descriptor=descriptor("alpha"),
        config=AlphaConfig(),
    )
    with pytest.raises(ValueError, match="manifest config is invalid"):
        CustomerBundlePlan(
            manifest=invalid_config_manifest,
            harness_version=resolved.harness_version,
            products=(forged_config_product,),
            sharing=resolved.sharing,
        )
