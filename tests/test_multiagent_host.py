"""Foundation installs delegation only after a selected product is active."""

import asyncio

import pytest

from suiteharness import __version__
from suiteharness.config import LoadedSuiteHarnessConfig
from suiteharness.config.models import (
    AgentRoleDefinition,
    ChannelsConfig,
    DeploymentConfig,
    DirectFetchConfig,
    HtmlSearchConfig,
    LocalDevelopmentSandboxConfig,
    ModelProfileConfig,
    ModelRouteConfig,
    ModelsConfig,
    MultiAgentConfig,
    MultiAgentProductConfig,
    StorageConfig,
    SuiteHarnessConfig,
    SuiteHarnessSecrets,
    WebFetchConfig,
    WebSearchConfig,
    WebToolsConfig,
    WorkspaceConfig,
)
from suiteharness.memory.in_memory import InMemoryProfileProvider
from suiteharness.memory.services import PROFILE_PROVIDER
from suiteharness.multiagent.sqlite import SQLiteAgentTaskStore
from suiteharness.runtime import RequestScope, RootContext, ScopePath
from suiteharness.runtime.customer_bundle import (
    CustomerBundleManifest,
    ProductCatalog,
    resolve_customer_bundle,
)
from suiteharness.runtime.descriptor import EmptyProductConfig, ProductDescriptor
from suiteharness.runtime.kernel import PreparedProduct, ProductActivator
from suiteharness.server.foundation import FoundationRuntime, FoundationStartupError


def _loaded(tmp_path, *, enabled):
    multi_agent = MultiAgentConfig(
        products={
            "product": MultiAgentProductConfig(
                enabled=enabled,
                roles=(
                    AgentRoleDefinition(
                        role_id="reader",
                        instruction="Read within this product only.",
                        allowed_tools=(),
                        model_route="chat",
                    ),
                ) if enabled else (),
            )
        }
    )
    config = SuiteHarnessConfig(
        deployment=DeploymentConfig(
            environment="development", instance_id="instance", tenant_id="tenant",
        ),
        customer_bundle=CustomerBundleManifest.model_validate({
            "customer_bundle_id": "bundle",
            "version": "0.1.0",
            "harness_api": ">=0.1,<0.2",
            "products": [{"product_id": "product", "version": "==0.1.0"}],
        }),
        workspace=WorkspaceConfig(root=tmp_path / "workspace"),
        storage=StorageConfig(root=tmp_path / "state"),
        sandbox=LocalDevelopmentSandboxConfig(acknowledge_unsafe=True),
        models=ModelsConfig(
            profiles=(
                ModelProfileConfig(profile_id="local", provider_id="ollama", model="test"),
            ),
            routes=(ModelRouteConfig(route_id="chat", primary_profile="local"),),
            default_route="chat",
        ),
        web_tools=WebToolsConfig(
            search=WebSearchConfig(
                default_provider="bing",
                providers=(HtmlSearchConfig(
                    provider_id="bing", engine="bing",
                    endpoint="https://www.bing.com/search",
                ),),
            ),
            fetch=WebFetchConfig(
                default_route="direct", routes=(DirectFetchConfig(),),
            ),
        ),
        multi_agent=multi_agent,
        channels=ChannelsConfig(),
    )
    return LoadedSuiteHarnessConfig(
        config, SuiteHarnessSecrets(),
        tmp_path / "config.yaml", tmp_path / "secrets.yaml",
    )


class _Activation:
    tenant_id = "tenant"
    customer_bundle_id = "bundle"
    closed = False

    def __init__(self):
        product = RootContext().tenant("tenant").product("product")
        self.products = {"product": product}

    def product(self, product_id):
        return self.products[product_id]

    def strategies(self, product_id):
        raise AssertionError("no child run is requested by this host test")

    async def run_trusted_child(self, request, strategies, aggregate_budget):
        raise AssertionError("no child run is requested by this host test")


class _Product(ProductActivator):
    descriptor = ProductDescriptor(
        product_id="product", version="0.1.0",
        harness_api=">=0.1,<0.2", config_model=EmptyProductConfig,
    )

    async def prepare(self, product):
        return PreparedProduct(
            self.descriptor, product.config,
            {PROFILE_PROVIDER: InMemoryProfileProvider()},
        )


async def _activate(foundation):
    product = _Product()
    foundation.prepare_product_workspace("product")
    plan = resolve_customer_bundle(
        foundation.config.customer_bundle,
        ProductCatalog((product.descriptor,)),
        harness_version=__version__,
    )
    return await foundation.kernel.activate_customer_bundle(
        "tenant", plan, (product,),
    )


def _request_scope():
    return RequestScope(
        path=ScopePath.agent("tenant", "product", "parent", "session"),
        principal_id="seller-one", channel_id="web",
    )


def test_default_foundation_never_registers_delegate(tmp_path):
    async def check():
        foundation = FoundationRuntime(_loaded(tmp_path, enabled=False))
        try:
            await foundation.start_extensions(await _activate(foundation))
            assert foundation.tools.resolve(
                _request_scope(), "agent.delegate"
            ) is None
        finally:
            await foundation.close()

    asyncio.run(check())


def test_enabled_foundation_registers_and_releases_protected_delegate(tmp_path):
    async def check():
        foundation = FoundationRuntime(_loaded(tmp_path, enabled=True))
        request_scope = _request_scope()
        try:
            assert foundation.tools.resolve(request_scope, "agent.delegate") is None
            await foundation.start_extensions(await _activate(foundation))
            item = foundation.tools.resolve(request_scope, "agent.delegate")
            assert item is not None
            assert item.registration_scope.kind.value == "root"
        finally:
            await foundation.close()
        assert foundation.tools.resolve(request_scope, "agent.delegate") is None

    asyncio.run(check())


def test_enabled_foundation_rejects_a_lookalike_activation(tmp_path):
    async def check():
        foundation = FoundationRuntime(_loaded(tmp_path, enabled=True))
        try:
            with pytest.raises(FoundationStartupError, match="trusted customer"):
                await foundation.start_extensions(_Activation())
            assert foundation.tools.resolve(
                _request_scope(), "agent.delegate"
            ) is None
        finally:
            await foundation.close()

    asyncio.run(check())


def test_concurrent_enabled_extension_starts_register_only_one_delegate(
    tmp_path, monkeypatch,
):
    async def check():
        foundation = FoundationRuntime(_loaded(tmp_path, enabled=True))
        original_recover = SQLiteAgentTaskStore.recover_interrupted
        recover_entries = 0
        first_entered = asyncio.Event()
        release_recovery = asyncio.Event()

        async def pause_recovery(store):
            nonlocal recover_entries
            recover_entries += 1
            first_entered.set()
            await release_recovery.wait()
            return await original_recover(store)

        monkeypatch.setattr(
            SQLiteAgentTaskStore, "recover_interrupted", pause_recovery,
        )
        try:
            activation = await _activate(foundation)
            first_start = asyncio.create_task(foundation.start_extensions(activation))
            await asyncio.wait_for(first_entered.wait(), timeout=5.0)
            second_start = asyncio.create_task(foundation.start_extensions(activation))
            await asyncio.sleep(0)
            release_recovery.set()
            first, second = await asyncio.wait_for(
                asyncio.gather(first_start, second_start),
                timeout=5.0,
            )
            assert first == second
            assert recover_entries == 1
            assert foundation.tools.resolve(
                _request_scope(), "agent.delegate"
            ) is not None
        finally:
            await foundation.close()

    asyncio.run(check())


def test_failed_extension_start_rolls_back_new_delegate_registration(
    tmp_path, monkeypatch,
):
    async def check():
        foundation = FoundationRuntime(_loaded(tmp_path, enabled=True))
        original_start = foundation.extensions.start

        async def fail_start(activation):
            raise RuntimeError("extension startup failed")

        try:
            activation = await _activate(foundation)
            monkeypatch.setattr(foundation.extensions, "start", fail_start)
            with pytest.raises(RuntimeError, match="extension startup failed"):
                await foundation.start_extensions(activation)
            assert foundation.tools.resolve(
                _request_scope(), "agent.delegate"
            ) is None
            monkeypatch.setattr(foundation.extensions, "start", original_start)
            await foundation.start_extensions(activation)
            assert foundation.tools.resolve(
                _request_scope(), "agent.delegate"
            ) is not None
        finally:
            await foundation.close()

    asyncio.run(check())
