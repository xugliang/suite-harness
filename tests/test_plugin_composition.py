import asyncio

import pytest
from pydantic import BaseModel

from suiteharness.config.models import ProductPluginBindings
from suiteharness.execution.protocols import PROMPT_STRATEGY, PromptStrategy
from suiteharness.plugins.composition import (
    PreparedContribution, ProductContributionFactory, compose_plugin_products,
)
from suiteharness.plugins.models import ContributionKind
from suiteharness.plugins.registry import VisibleContribution
from suiteharness.runtime.customer_bundle import ProductCatalog, CustomerProductSelection, ResolvedCustomerProduct
from suiteharness.runtime.descriptor import ProductDescriptor
from suiteharness.runtime.kernel import ProductActivator, PreparedProduct


class Config(BaseModel):
    model_config = {"extra": "forbid"}


class Product(ProductActivator):
    descriptor = ProductDescriptor(product_id="test-product", version="1.0.0", harness_api=">=0", config_model=Config)

    async def prepare(self, product):
        return PreparedProduct(self.descriptor, product.config, {})


class Factory(ProductContributionFactory):
    async def prepare(self, product):
        return PreparedContribution(PromptStrategy())


def visible(kind, value, name="custom"):
    return VisibleContribution(kind, name, "plugin", "1.0.0", "activation", value)


def test_plugin_product_enters_catalog_before_bundle_resolution():
    catalog, activators = compose_plugin_products(ProductCatalog(()), (), (visible(ContributionKind.PRODUCT, Product()),), {})
    assert catalog.product_ids == ("test-product",)
    assert activators[0].descriptor.product_id == "test-product"


def test_selected_prompt_is_bound_and_instances_are_product_owned():
    async def scenario():
        product = Product()
        _, activators = compose_plugin_products(ProductCatalog((product.descriptor,)), (product,), (visible(ContributionKind.PROMPT, Factory()),), {"test-product": ProductPluginBindings(prompt="custom")})
        resolved = ResolvedCustomerProduct(CustomerProductSelection(product_id="test-product", version=">=0"), product.descriptor, Config())
        first = await activators[0].prepare(resolved)
        second = await activators[0].prepare(resolved)
        assert isinstance(first.bindings[PROMPT_STRATEGY], PromptStrategy)
        assert first.bindings[PROMPT_STRATEGY] is not second.bindings[PROMPT_STRATEGY]
    asyncio.run(scenario())


def test_missing_selection_fails_startup_instead_of_silent_default():
    with pytest.raises(ValueError, match="missing"):
        compose_plugin_products(ProductCatalog((Product.descriptor,)), (Product(),), (), {"test-product": ProductPluginBindings(prompt="absent")})


def test_duplicate_plugin_product_cannot_replace_host_product():
    with pytest.raises(ValueError):
        compose_plugin_products(ProductCatalog((Product.descriptor,)), (Product(),), (visible(ContributionKind.PRODUCT, Product()),), {})


def test_configured_source_plugin_is_loaded_consumed_and_revoked(tmp_path):
    import json
    from types import SimpleNamespace
    from suiteharness.config.models import PluginsConfig
    from suiteharness.plugins.discovery import deterministic_directory_digest
    from suiteharness.server.extensions import ServerExtensionHost
    from suiteharness.execution.in_memory import InMemoryToolRegistry
    source = tmp_path / 'plugin'
    source.mkdir()
    (source / 'main.py').write_text('''
from suiteharness.plugins import PluginRuntime, PreparedPlugin, ContributionKind
from suiteharness.plugins.composition import ProductContributionFactory, PreparedContribution
from suiteharness.execution.protocols import PromptStrategy
class Factory(ProductContributionFactory):
    async def prepare(self, product):
        return PreparedContribution(PromptStrategy())
class Prepared(PreparedPlugin):
    @property
    def exports(self): return {}
    async def activate(self, context):
        context.contribute(ContributionKind.PROMPT, 'live-prompt', Factory())
    async def close(self): pass
class Plugin(PluginRuntime):
    def __init__(self, config): pass
    async def prepare(self, context): return Prepared()
    async def close(self): pass
''', encoding='utf-8')
    digest = deterministic_directory_digest(source)
    manifest = dict(schema_version='1', plugin_id='test-plugin', version='1.0.0',
        harness_api='>=0', trust_mode='trusted_in_process', entrypoint='main:Plugin',
        artifact={'digest': digest}, contributions={'prompt': [{'contribution_id': 'live-prompt'}]})
    (source / 'suiteharness-plugin.json').write_text(json.dumps(manifest), encoding='utf-8')
    plugins = PluginsConfig(allowed_roots=(tmp_path,), sources=({'path': source, 'expected_digest': digest},),
        trusted_in_process_plugins={'test-plugin'}, require_signatures=False)
    loaded = SimpleNamespace(config=SimpleNamespace(plugins=plugins))
    async def run():
        host = ServerExtensionHost(loaded, sandbox=None, tools=InMemoryToolRegistry(), product_workspace=lambda _: tmp_path)
        assert await host.start_plugins() == ('test-plugin',)
        registry = host.plugin_contributions
        _, activators = compose_plugin_products(ProductCatalog((Product.descriptor,)), (Product(),),
            await registry.snapshot(), {'test-product': ProductPluginBindings(prompt='live-prompt')})
        resolved = ResolvedCustomerProduct(CustomerProductSelection(product_id='test-product', version='>=0'), Product.descriptor, Config())
        prepared = await activators[0].prepare(resolved)
        assert isinstance(prepared.bindings[PROMPT_STRATEGY], PromptStrategy)
        await prepared.cleanup()
        await host.close()
        assert await registry.snapshot() == ()
    asyncio.run(run())
