"""Regression coverage for explicit execution extension contracts."""

import asyncio
from abc import ABC
from types import SimpleNamespace

import pytest

from suiteharness.execution.protocols import (
    ApprovalStore, AuditJournal, CapabilityAuthority, InteractiveApprovalBroker,
    PromptStrategy, ReflectionStrategy, ToolAuthorizationPolicy, ToolRegistry, Workflow,
)
from suiteharness.agents.protocols import AgentEventSink, AgentEventStream, ProductModelRouteResolver
from suiteharness.plugins.discovery import SignatureVerifier, TrustPolicy
from suiteharness.plugins.protocols import (
    ContributionHandle, ContributionRegistry, PluginActivationContextProtocol,
    PluginPrepareContextProtocol, PluginRuntime, PreparedPlugin, PluginLauncher, TrustedPluginLoader,
)
from suiteharness.runtime.bundle import Bundle
from suiteharness.runtime.kernel import ProductActivator, ScopedToolRegistry
from suiteharness.runtime.resources import ResourceAuthorizer


@pytest.mark.parametrize('contract', [
    ApprovalStore, AuditJournal, CapabilityAuthority, InteractiveApprovalBroker,
    ToolAuthorizationPolicy, ToolRegistry, Workflow, AgentEventSink, AgentEventStream,
    ProductModelRouteResolver, SignatureVerifier, TrustPolicy, ContributionHandle,
    ContributionRegistry, PluginActivationContextProtocol, PluginPrepareContextProtocol,
    PluginRuntime, PreparedPlugin, PluginLauncher, TrustedPluginLoader, Bundle,
    ProductActivator, ScopedToolRegistry, ResourceAuthorizer,
])
def test_incomplete_extension_cannot_instantiate(contract):
    class Incomplete(contract):
        pass
    with pytest.raises(TypeError):
        Incomplete()
    assert issubclass(contract, ABC)


def test_reflection_inherits_identity_review():
    class Review(ReflectionStrategy):
        pass
    decision = object()
    assert asyncio.run(Review().review(None, None, decision)) is decision


def test_prompt_inherits_generic_rendering():
    from suiteharness.execution.models import RunRequest
    from suiteharness.runtime import RequestScope, ScopePath
    class Prompt(PromptStrategy):
        system_prompt = 'Be helpful.'
    request = RunRequest(
        input={'question': 'hello'}, run_id='r1',
        scope=RequestScope(path=ScopePath.product('t1', 'app'), principal_id='p1'),
        grant_id='test-grant',
    )
    result = asyncio.run(Prompt().render(request, None))
    assert [(m.role, m.content) for m in result.messages] == [
        ('system', 'Be helpful.'), ('user', '{"question":"hello"}'),
    ]
    assert result.metadata['tenant_id'] == 't1'


def test_duck_workflow_is_not_registered_contract():
    class Duck:
        async def next(self, request, frame, prompt):
            pass
    assert not isinstance(Duck(), Workflow)


def test_security_base_never_silently_accepts_activation():
    class DelegatingAuthority(CapabilityAuthority):
        async def resolve(self, grant_id):
            return await super().resolve(grant_id)

        async def bind_activation(self, tenant_id, product_ids, owner_token):
            return await super().bind_activation(tenant_id, product_ids, owner_token)

        async def release_activation(self, owner_token):
            return await super().release_activation(owner_token)

    with pytest.raises(NotImplementedError):
        asyncio.run(DelegatingAuthority().bind_activation('tenant', ('product',), 'owner'))


def test_builtin_strategies_and_services_are_explicit_extensions():
    import inspect
    from suiteharness.agents.prompts import DefaultPromptStrategy
    from suiteharness.agents.react import ReActWorkflow, ProductRoutedReActWorkflow
    from suiteharness.agents.reflection import NoOpReflectionStrategy, SinglePassModelReflectionStrategy
    from suiteharness.execution.in_memory import (
        InMemoryApprovalStore, InMemoryAuditJournal, InMemoryCapabilityAuthority, InMemoryToolRegistry,
    )
    from suiteharness.execution.sqlite import SQLiteAuditJournal
    from suiteharness.plugins.discovery import AllowlistedTrustPolicy
    from suiteharness.plugins.loader import PythonEntrypointLoader, _NamespaceRuntime
    from suiteharness.plugins.manager import (
        PluginActivationContext, PluginPrepareContext, _PendingContributionHandle,
    )
    from suiteharness.plugins.registry import InMemoryContributionRegistry, _RegistryHandle

    for concrete, contract in [
        (DefaultPromptStrategy, PromptStrategy), (NoOpReflectionStrategy, ReflectionStrategy),
        (SinglePassModelReflectionStrategy, ReflectionStrategy), (ReActWorkflow, Workflow),
        (ProductRoutedReActWorkflow, Workflow), (InMemoryApprovalStore, ApprovalStore),
        (InMemoryAuditJournal, AuditJournal), (SQLiteAuditJournal, AuditJournal),
        (InMemoryCapabilityAuthority, CapabilityAuthority), (InMemoryToolRegistry, ScopedToolRegistry),
        (AllowlistedTrustPolicy, TrustPolicy), (PythonEntrypointLoader, TrustedPluginLoader),
        (_NamespaceRuntime, PluginRuntime), (PluginActivationContext, PluginActivationContextProtocol),
        (PluginPrepareContext, PluginPrepareContextProtocol), (_PendingContributionHandle, ContributionHandle),
        (InMemoryContributionRegistry, ContributionRegistry), (_RegistryHandle, ContributionHandle),
    ]:
        assert issubclass(concrete, contract)
        assert not inspect.isabstract(concrete)
    assert DefaultPromptStrategy().render.__func__ is PromptStrategy.render
    assert NoOpReflectionStrategy().review.__func__ is ReflectionStrategy.review
    with pytest.raises(ValueError):
        DefaultPromptStrategy(system_prompt=' ')
