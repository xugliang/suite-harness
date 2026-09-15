"""Exercise host wiring and durable memory through the real channel pipeline."""
import asyncio
from datetime import UTC, datetime

import pytest

from suiteharness.channels import ChannelKind, InboundMessage, OutboundEventKind
from suiteharness.config import LoadedSuiteHarnessConfig
from suiteharness.config.models import (
    ChannelsConfig, ConversationMemoryConfig, DeploymentConfig, ModelsConfig,
    SuiteHarnessConfig, SuiteHarnessSecrets,
)
from suiteharness.execution import Workflow, WorkflowDecision
from suiteharness.execution.in_memory import (
    InMemoryApprovalStore, InMemoryAuditJournal, InMemoryCapabilityAuthority,
    InMemoryToolRegistry,
)
from suiteharness.execution.protocols import ExecutionStrategies, PromptStrategy
from suiteharness.execution.runner import ExecutionRunner
from suiteharness.memory.services import CONVERSATION_MEMORY
from suiteharness.memory.session_memory import SessionConversationMemory
from suiteharness.models import MODEL_GATEWAY, FinishReason, ModelGateway, ModelResponse, TextContent
from suiteharness.runtime import RequestScope, RootContext, ScopePath, ServiceBindings
from suiteharness.runtime.customer_bundle import CustomerBundleManifest
from suiteharness.server.access import ConfiguredGrantIssuer, ToolAccessTemplate
from suiteharness.server.host import CompanyChannelRuntime, WebApprovalRuntime
from suiteharness.sessions import InMemorySessionStore, SQLiteSessionStore, SessionIdentity
from suiteharness.sessions.errors import SessionIdempotencyConflictError


class SummaryGateway(ModelGateway):
    """The only external dependency: a deterministic model transport substitute."""
    def __init__(self):
        self.requests = []

    async def complete(self, route_id, request):
        self.requests.append((route_id, request))
        return ModelResponse(response_id='summary-response', provider_id='test',
                             model='summary-model', content=(TextContent(text='semantic summary'),),
                             finish_reason=FinishReason.STOP)


class CaptureWorkflow(Workflow):
    def __init__(self):
        self.requests = []

    async def next(self, request, frame, prompt):
        self.requests.append(request)
        return WorkflowDecision.final('reply:' + request.input['message'])


async def make_host(tmp_path, *, memory=None, sessions=None, custom=None, gateway=None):
    memory = memory or ConversationMemoryConfig(model_summary_enabled=False)
    gateway = gateway or SummaryGateway()
    root = RootContext(bindings=ServiceBindings.root({MODEL_GATEWAY: gateway}))
    tenant = root.tenant('company')
    products = {name: tenant.product(name, {CONVERSATION_MEMORY: custom} if name == 'custom' and custom else {})
                for name in ('product', 'custom')}
    registry, authority = InMemoryToolRegistry(), InMemoryCapabilityAuthority()
    runner = ExecutionRunner(tools=registry, capabilities=authority,
                             approvals=InMemoryApprovalStore(), journal=InMemoryAuditJournal())
    workflow = CaptureWorkflow()

    class Activation:
        tenant_id = 'company'
        customer_bundle_id = 'bundle'
        closed = False

        async def run(self, request):
            return await runner.run(request, ExecutionStrategies(workflow=workflow, prompt=PromptStrategy()))

        def stream(self, request):
            return runner.stream(request, ExecutionStrategies(workflow=workflow, prompt=PromptStrategy()))

    activation = Activation()
    activation.products = products
    # All host-facing nested settings are real validated models. Other foundation
    # settings are unnecessary because this test supplies the activated runtime.
    config = SuiteHarnessConfig.model_construct(
        deployment=DeploymentConfig(instance_id='instance', tenant_id='company'),
        customer_bundle=CustomerBundleManifest.model_validate({
            'customer_bundle_id': 'bundle', 'version': '0.1.0', 'harness_api': '>=0.1,<0.2',
            'products': [{'product_id': 'product', 'version': '==0.1.0'},
                         {'product_id': 'custom', 'version': '==0.1.0'}]}),
        channels=ChannelsConfig(web={'allowed_origins': ['https://company.test']}), memory=memory,
        models=ModelsConfig.model_construct(default_route='chat', product_routes={}, routes=()),
    )
    secrets = SuiteHarnessSecrets(web={'web/default': {'session_signing_key': 'test-only-signing-key-' * 3}})
    loaded = LoadedSuiteHarnessConfig(config, secrets, tmp_path / 'config.yaml', tmp_path / 'secrets.yaml')
    sessions = sessions or InMemorySessionStore()
    host = await CompanyChannelRuntime.create(loaded, activation=activation, sessions=sessions,
        tools=registry, capabilities=authority, web_approvals=WebApprovalRuntime(),
        grant_templates=tuple(ToolAccessTemplate(channel_id='web', product_id=p) for p in products))
    return host, sessions, workflow, gateway


def scope(product='product', principal='seller', session='session'):
    return RequestScope(path=ScopePath.agent('company', product, 'assistant', session),
        principal_id=principal, channel_id='web', request_id='request', correlation_id='corr')


def identity(request_scope):
    return SessionIdentity(tenant_id=request_scope.tenant_id, product_id=request_scope.product_id,
        agent_id=request_scope.path.agent_id, session_id=request_scope.path.session_id,
        principal_id=request_scope.effective_session_owner_id)


async def send(app, number, *, request_scope=None, text=None, before_commit=None):
    request_scope = request_scope or scope()
    message = InboundMessage(channel=ChannelKind.WEB, event_id=f'event-{number}',
        message_id=f'message-{number}', conversation_id=request_scope.path.session_id,
        sender_external_id=request_scope.principal_id, text=text or f'question-{number}',
        product_id=request_scope.product_id, received_at=datetime.now(UTC))
    options = {} if before_commit is None else {'before_commit': before_commit}
    return [event async for event in app.handle(message, request_scope, **options)]


def test_host_uses_configured_window_and_product_binding(tmp_path):
    async def check():
        host, _, workflow, _ = await make_host(tmp_path,
            memory=ConversationMemoryConfig(short_term_pairs=1, model_summary_enabled=False),
            custom=SessionConversationMemory(short_term_pairs=2))
        for number in range(4):
            assert (await send(host.application, number))[-1].kind is OutboundEventKind.COMPLETED
            await send(host.application, number, request_scope=scope('custom'))
        regular, custom = workflow.requests[-2:]
        assert [m.content for m in regular.conversation.messages] == ['question-2', 'reply:question-2']
        assert len(custom.conversation.messages) == 4
        assert regular.conversation.memory_degraded
        await host.close()
    asyncio.run(check())


@pytest.mark.parametrize('enabled, route, expected_route', [
    (False, 'summarize', None), (True, 'summarize', 'summarize'), (True, None, 'chat'),
])
def test_host_obeys_summary_switch_and_route(tmp_path, enabled, route, expected_route):
    async def check():
        host, _, workflow, gateway = await make_host(tmp_path,
            memory=ConversationMemoryConfig(short_term_pairs=1, model_summary_enabled=enabled,
                                            summary_route=route))
        for number in range(3):
            await send(host.application, number)
        assert bool(gateway.requests) is enabled
        if enabled:
            assert all(route == expected_route for route, _ in gateway.requests)
            assert workflow.requests[-1].conversation.working_summary == 'semantic summary'
            assert not workflow.requests[-1].conversation.memory_degraded
        await host.close()
    asyncio.run(check())


def test_archive_is_recalled_after_restart_and_is_identity_scoped(tmp_path):
    async def check():
        database = tmp_path / 'sessions.sqlite'
        memory = ConversationMemoryConfig(model_summary_enabled=False, long_term_enabled=True,
                                            archive_root=tmp_path / 'archives')
        store = SQLiteSessionStore(database)
        host, _, _, _ = await make_host(tmp_path, memory=memory, sessions=store)
        await send(host.application, 1, text='remember the blue bicycle')
        assert list((tmp_path / 'archives').glob('*.md'))
        await host.close()
        await store.close()
        store = SQLiteSessionStore(database)
        host, _, workflow, _ = await make_host(tmp_path, memory=memory, sessions=store)
        await send(host.application, 2)
        assert 'blue bicycle' in workflow.requests[-1].conversation.long_term_context
        await send(host.application, 3, request_scope=scope(principal='other'))
        assert not workflow.requests[-1].conversation.long_term_context
        await host.close()
        await store.close()
    asyncio.run(check())


def test_invalidation_survives_restart_and_disabled_archive_without_erasing_audit(tmp_path):
    async def check():
        database = tmp_path / 'sessions.sqlite'
        settings = ConversationMemoryConfig(short_term_pairs=1, model_summary_enabled=False,
            long_term_enabled=True, archive_root=tmp_path / 'archives')
        store = SQLiteSessionStore(database)
        host, _, _, _ = await make_host(tmp_path, memory=settings, sessions=store)
        for number in range(3):
            await send(host.application, number, text=f'revoked-secret-{number}')
        await host.close()
        disabled = settings.model_copy(update={'long_term_enabled': False})
        host, _, _, _ = await make_host(tmp_path, memory=disabled, sessions=store)
        assert callable(getattr(host.application, 'invalidate_memory', None))
        archived_before = {p: p.read_bytes() for p in (tmp_path / 'archives').glob('*.md')}
        await host.application.invalidate_memory(scope(), reason='source_deleted')
        assert {p: p.read_bytes() for p in archived_before} == archived_before
        assert await host.application.history(scope()) == ()
        assert len(await store.recent_transcript(identity(scope()), limit=100)) >= 6
        await host.close()
        await store.close()
        store = SQLiteSessionStore(database)
        host, _, workflow, _ = await make_host(tmp_path, memory=settings, sessions=store)
        await send(host.application, 10, text='new question')
        context = workflow.requests[-1].conversation
        assert not context.messages and not context.working_summary and not context.long_term_context
        visible = await host.application.history(scope())
        assert len(visible) == 2
        assert 'revoked-secret' not in str(visible)
        await host.close()
        await store.close()
    asyncio.run(check())


def test_invalidation_retry_preserves_new_messages_and_new_archive(tmp_path):
    async def check():
        settings = ConversationMemoryConfig(model_summary_enabled=False,
            long_term_enabled=True, archive_root=tmp_path / 'archives')
        host, store, workflow, _ = await make_host(tmp_path, memory=settings)
        await send(host.application, 1, text='withdrawn data')
        await host.application.invalidate_memory(scope(), reason='source_deleted', idempotency_key='operation-1')
        await send(host.application, 2, text='new valid data')
        before = await store.get(identity(scope()))
        await host.application.invalidate_memory(scope(), reason='source_deleted', idempotency_key='operation-1')
        after = await store.get(identity(scope()))
        assert after.revision == before.revision
        assert len(await host.application.history(scope())) == 2
        await send(host.application, 3)
        assert 'new valid data' in workflow.requests[-1].conversation.long_term_context
        assert 'withdrawn data' not in str(workflow.requests[-1].conversation)
        await host.close()
    asyncio.run(check())


def test_history_can_show_invalidated_messages_without_reactivating_or_mutating_audit(tmp_path):
    async def check():
        host, store, workflow, _ = await make_host(tmp_path)
        await send(host.application, 1, text='withdrawn data')
        await host.application.invalidate_memory(scope(), reason='source_deleted')
        await send(host.application, 2, text='current data')
        visible = await host.application.history(scope(), include_invalidated=True)
        assert [event.payload['memory_eligible'] for event in visible] == [False, False, True, True]
        assert len(await host.application.history(scope())) == 2
        assert not any('memory_eligible' in event.payload
                       for event in await store.recent_transcript(identity(scope()), limit=100))
        assert not workflow.requests[-1].conversation.messages
        await host.close()
    asyncio.run(check())


def test_invalidation_during_archive_write_cannot_publish_old_completion(tmp_path, monkeypatch):
    import threading
    from suiteharness.memory.markdown_archive import MarkdownArchive
    entered, release = threading.Event(), threading.Event()
    original = MarkdownArchive.store

    def delayed_store(self, session, text):
        entered.set()
        assert release.wait(timeout=5)
        return original(self, session, text)

    monkeypatch.setattr(MarkdownArchive, 'store', delayed_store)

    async def check():
        settings = ConversationMemoryConfig(model_summary_enabled=False,
            long_term_enabled=True, archive_root=tmp_path / 'archives')
        host, store, _, _ = await make_host(tmp_path, memory=settings)
        other, _, _, _ = await make_host(tmp_path, memory=settings, sessions=store)
        pending = asyncio.create_task(send(host.application, 1, text='withdrawn while finalizing'))
        try:
            assert await asyncio.wait_for(asyncio.to_thread(entered.wait, 3), 4)
            await other.application.invalidate_memory(scope(), reason='source_deleted')
        finally:
            release.set()
        events = await pending
        assert events[-1].kind is OutboundEventKind.FAILED
        assert events[-1].payload['failure']['code'] == 'memory_invalidated'
        assert await other.application.history(scope()) == ()
        await host.close()
        await other.close()
    asyncio.run(check())


def test_invalidation_fences_running_model_and_preserves_watermark(tmp_path):
    from suiteharness.agents.react import ReActWorkflow
    from suiteharness.models import ModelStreamEvent, ModelStreamEventType
    entered, release = asyncio.Event(), asyncio.Event()

    class PausedModel:
        async def stream(self, route, request):
            entered.set()
            await release.wait()
            yield ModelStreamEvent(type=ModelStreamEventType.TEXT_DELTA, text_delta='revoked answer')
            yield ModelStreamEvent(type=ModelStreamEventType.MESSAGE_END, finish_reason=FinishReason.STOP)

    async def check():
        host, store, _, _ = await make_host(tmp_path)
        other, _, _, _ = await make_host(tmp_path, sessions=store)
        authority, registry = InMemoryCapabilityAuthority(), InMemoryToolRegistry()
        runner = ExecutionRunner(tools=registry, capabilities=authority,
            approvals=InMemoryApprovalStore(), journal=InMemoryAuditJournal())
        host.application._grants = ConfiguredGrantIssuer(registry=registry, authority=authority,
            templates=(ToolAccessTemplate(channel_id='web', product_id='product'),))
        host.application._activation.stream = lambda request: runner.stream(request,
            ExecutionStrategies(workflow=ReActWorkflow(PausedModel(), 'chat'), prompt=PromptStrategy()))
        pending = asyncio.create_task(send(host.application, 1))
        await asyncio.wait_for(entered.wait(), 2)
        await other.application.invalidate_memory(scope(), reason='source_deleted')
        invalidated = await store.get(identity(scope()))
        release.set()
        events = await pending
        assert events[-1].kind is OutboundEventKind.FAILED
        assert all(event.kind is not OutboundEventKind.DELTA for event in events)
        assert (await store.get(identity(scope()))).memory_invalidated_revision == invalidated.revision
        assert await other.application.history(scope()) == ()
        await host.close()
        await other.close()
    asyncio.run(check())


def test_sqlite_invalidation_idempotency_survives_restart(tmp_path):
    async def check():
        database = tmp_path / 'idempotent.sqlite'
        store = SQLiteSessionStore(database)
        host, _, _, _ = await make_host(tmp_path, sessions=store)
        await send(host.application, 1)
        await host.application.invalidate_memory(scope(), reason='claim_removed', idempotency_key='v1')
        await send(host.application, 2)
        revision = (await store.get(identity(scope()))).revision
        await host.close()
        await store.close()
        store = SQLiteSessionStore(database)
        host, _, _, _ = await make_host(tmp_path, sessions=store)
        await host.application.invalidate_memory(scope(), reason='claim_removed', idempotency_key='v1')
        assert (await store.get(identity(scope()))).revision == revision
        assert len(await host.application.history(scope())) == 2
        with pytest.raises(SessionIdempotencyConflictError, match='idempotency key'):
            await host.application.invalidate_memory(scope(), reason='different', idempotency_key='v1')
        await host.close()
        await store.close()
    asyncio.run(check())


@pytest.mark.parametrize('sqlite', [False, True])
def test_out_of_order_source_invalidation_cannot_clear_new_valid_memory(tmp_path, sqlite):
    async def check():
        settings = ConversationMemoryConfig(model_summary_enabled=False, long_term_enabled=True,
                                            archive_root=tmp_path / 'archives')
        store = SQLiteSessionStore(tmp_path / 'sources.sqlite') if sqlite else InMemorySessionStore()
        host, _, _, _ = await make_host(tmp_path, memory=settings, sessions=store)
        await host.application.invalidate_memory(scope(), reason='source_deleted',
            idempotency_key='version-2', source_namespace='sales-sources', source_version=2)
        await send(host.application, 1, text='new valid information')
        revision = (await store.get(identity(scope()))).revision
        await host.close()
        if sqlite:
            await store.close()
            store = SQLiteSessionStore(tmp_path / 'sources.sqlite')
        host, _, workflow, _ = await make_host(tmp_path, memory=settings, sessions=store)
        # An earlier worker had read version 1 but did not apply it before v2.
        await host.application.invalidate_memory(scope(), reason='source_deleted',
            idempotency_key='late-version-1', source_namespace='sales-sources', source_version=1)
        assert (await store.get(identity(scope()))).revision == revision
        assert len(await host.application.history(scope())) == 2
        await send(host.application, 2)
        assert 'new valid information' in workflow.requests[-1].conversation.long_term_context
        await host.close()
        if sqlite:
            await store.close()
    asyncio.run(check())


def test_before_commit_rejection_never_records_success_or_archive(tmp_path):
    async def check():
        host, store, _, _ = await make_host(tmp_path, memory=ConversationMemoryConfig(
            model_summary_enabled=False, long_term_enabled=True, archive_root=tmp_path / 'archives'))

        async def revoked():
            raise PermissionError('session revoked')

        with pytest.raises(PermissionError, match='session revoked'):
            await send(host.application, 1, before_commit=revoked)
        events = await store.recent_transcript(identity(scope()), limit=100)
        assert not any(event.type.value == 'assistant_message' for event in events)
        assert any(event.type.value == 'error' for event in events)
        assert not list((tmp_path / 'archives').glob('*.md'))
        assert len(await host.application.history(scope())) == 1  # submitted user input is audit history
        await host.close()
    asyncio.run(check())


def test_before_commit_guards_cached_success_replay(tmp_path):
    async def check():
        host, _, workflow, _ = await make_host(tmp_path)
        await send(host.application, 1)

        async def revoked():
            raise PermissionError('session revoked')

        with pytest.raises(PermissionError, match='session revoked'):
            await send(host.application, 1, before_commit=revoked)
        assert len(workflow.requests) == 1
        await host.close()
    asyncio.run(check())


def test_cancellation_during_before_commit_records_cancel_not_success(tmp_path):
    async def check():
        host, store, _, _ = await make_host(tmp_path)
        entered, release = asyncio.Event(), asyncio.Event()

        async def guard():
            entered.set()
            await release.wait()

        pending = asyncio.create_task(send(host.application, 1, before_commit=guard))
        await asyncio.wait_for(entered.wait(), 2)
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending
        events = await store.recent_transcript(identity(scope()), limit=100)
        assert events[-1].type.value == 'cancel'
        assert (await store.get(identity(scope()))).status.value == 'cancelled'
        assert not any(event.type.value == 'assistant_message' for event in events)
        await host.close()
    asyncio.run(check())


def test_cached_answer_cannot_escape_invalidation_during_commit_guard(tmp_path):
    async def check():
        host, _, workflow, _ = await make_host(tmp_path)
        await send(host.application, 1)

        async def changed():
            await host.application.invalidate_memory(scope(), reason='source_deleted')

        events = await send(host.application, 1, before_commit=changed)
        assert events[-1].kind is OutboundEventKind.FAILED
        assert events[-1].payload['failure']['code'] == 'memory_invalidated'
        assert len(workflow.requests) == 1
        await host.close()
    asyncio.run(check())
