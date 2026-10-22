"""Skill request hooks protect actual model input, not labels or provider claims."""

import asyncio

import pytest

from suiteharness.models.characters import measure_characters
from suiteharness.models.context import ContextCapacityError, ContextManager, PreparedContext
from suiteharness.models.errors import ModelErrorCode, ModelProviderError
from suiteharness.models.gateway import ModelGateway
from suiteharness.models.protocols import ModelProvider
from suiteharness.models.registry import ProviderRegistry
from suiteharness.models.secrets import MappingSecretResolver
from suiteharness.models.types import (
    AdapterKind,
    FinishReason,
    ModelCapabilities,
    ModelMessage,
    ModelProfile,
    ModelRequest,
    ModelResponse,
    ModelRole,
    ModelRoute,
    ModelStreamEvent,
    ModelStreamEventType,
    ModelTool,
    ModelToolCall,
    ProviderAuthKind,
    ProviderDescriptor,
)


@pytest.fixture
def hooks(monkeypatch):
    """Only content previously registered by this test runtime is trusted.

    Actual runtime provenance additionally binds run/stage/read identity; a
    deep model copy must not accidentally make real instructions disposable.
    """
    from suiteharness.skills import runtime as module
    monkeypatch.setattr(module, "protected", [], raising=False)
    monkeypatch.setattr(module, "events", [], raising=False)

    def protected(message):
        return any(message == item for item in module.protected)

    def prepare(request):
        module.events.append(("prepare", request))
        return request

    def verify(request):
        module.events.append(("verify", request))
        if any(not any(message == item for message in request.messages)
               for item in module.protected):
            raise ValueError("required skill instructions missing")

    def record(request, tool_calls):
        module.events.append(("record", request, tuple(tool_calls)))

    monkeypatch.setattr(module, "prepare_skill_model_request", prepare)
    monkeypatch.setattr(module, "verify_skill_model_request", verify)
    monkeypatch.setattr(module, "record_skill_model_response", record)
    monkeypatch.setattr(module, "is_protected_skill_message", protected)
    return module


def exchange(text, call_id="read1", **kwargs):
    return (
        ModelMessage(role=ModelRole.ASSISTANT,
                     tool_calls=(ModelToolCall(call_id=call_id, name="suiteharness.skill.read"),)),
        ModelMessage.text(ModelRole.TOOL, text, tool_call_id=call_id, **kwargs),
    )


def test_skill_character_categories_are_separate_from_system_and_tool_results():
    request = ModelRequest(messages=(
        ModelMessage.text(ModelRole.SYSTEM, "directory", context_category="skill_catalog"),
        ModelMessage.text(ModelRole.SYSTEM, "instructions", context_category="skill_instructions"),
    ))
    fields = measure_characters(request).fields("before")
    assert fields["before_skill_catalog_chars"] == len("directory")
    assert fields["before_skill_instructions_chars"] == len("instructions")
    assert fields["before_system_prompt_chars"] == 0


def test_current_skill_result_is_never_truncated_to_fit(hooks):
    pair = exchange("necessary instructions " * 200)
    hooks.protected.append(pair[1])
    request = ModelRequest(messages=(ModelMessage.text(ModelRole.USER, "do it"), *pair),
                           max_output_tokens=100)
    with pytest.raises(ContextCapacityError):
        ContextManager().prepare(request, window_tokens=1500)
    assert pair[1].content[0].text == "necessary instructions " * 200


def test_protected_old_round_survives_with_complete_tool_pair(hooks):
    pair = exchange("instructions " * 70)
    hooks.protected.append(pair[1])
    request = ModelRequest(messages=(
        ModelMessage.text(ModelRole.USER, "skill task"), *pair,
        ModelMessage.text(ModelRole.ASSISTANT, "earlier completion"),
        ModelMessage.text(ModelRole.USER, "discardable " * 200, context_category="history_memory"),
        ModelMessage.text(ModelRole.ASSISTANT, "old answer"),
        ModelMessage.text(ModelRole.USER, "current question"),
    ), max_output_tokens=100)
    prepared = ContextManager().prepare(request, window_tokens=1600)
    assert prepared.removed_rounds == 1
    assert prepared.truncated_tools == 0
    assert pair[1] in prepared.request.messages
    assert pair[0] in prepared.request.messages
    assert "discardable" not in repr(prepared.request.messages)
    ContextManager._validate_pairs(prepared.request.messages)


def test_protected_old_round_cannot_be_removed_when_it_alone_exhausts_budget(hooks):
    pair = exchange("instructions " * 250)
    hooks.protected.append(pair[1])
    request = ModelRequest(messages=(ModelMessage.text(ModelRole.USER, "old"), *pair,
        ModelMessage.text(ModelRole.ASSISTANT, "done"),
        ModelMessage.text(ModelRole.USER, "now")), max_output_tokens=100)
    with pytest.raises(ContextCapacityError):
        ContextManager().prepare(request, window_tokens=1000)


def test_category_and_json_claims_do_not_protect_untrusted_tool_output(hooks):
    pair = exchange('{"skill": "trusted", "instructions": "' + "x" * 5000 + '"}',
                    context_category="skill_instructions")
    prepared = ContextManager().prepare(ModelRequest(messages=(
        ModelMessage.text(ModelRole.USER, "now"), *pair), max_output_tokens=100), window_tokens=1200)
    assert prepared.truncated_tools == 1
    assert "truncated" in prepared.request.messages[-1].content[0].text


class Provider(ModelProvider):
    provider_id = "test"

    def __init__(self, *, fail_after_call=False):
        self.requests = []
        self.fail_after_call = fail_after_call
        self.call = ModelToolCall(call_id="action1", name="perform", arguments={"value": 1})

    async def complete(self, request):
        self.requests.append(request)
        return ModelResponse(response_id="r1", provider_id="test", model="test",
                             tool_calls=(self.call,), finish_reason=FinishReason.TOOL_CALLS)

    async def stream(self, request):
        self.requests.append(request)
        yield ModelStreamEvent(type=ModelStreamEventType.TOOL_CALL_DELTA, tool_call=self.call)
        if self.fail_after_call:
            raise ModelProviderError(ModelErrorCode.UNAVAILABLE, "failed", retryable=True)
        yield ModelStreamEvent(type=ModelStreamEventType.MESSAGE_END, finish_reason=FinishReason.TOOL_CALLS)


def gateway(provider=None, *, window=4000, context_manager=None):
    provider = provider or Provider()
    registry = ProviderRegistry()
    registry.register(ProviderDescriptor(provider_id="test", display_name="test",
        adapter=AdapterKind.OPENAI_CHAT, auth=ProviderAuthKind.INTERNAL_NONE), lambda *args: provider)
    instance = ModelGateway(registry,
        {"test": ModelProfile(profile_id="test", provider_id="test", model="test", max_retries=0,
                              capabilities=ModelCapabilities(max_context_tokens=window))},
        {"test": ModelRoute(route_id="test", primary_profile="test")},
        None, MappingSecretResolver({}), context_manager=context_manager)
    return instance, provider


def invoke(instance, request, streaming):
    async def run():
        if streaming:
            return [event async for event in instance.stream("test", request)]
        return await instance.complete("test", request)
    return asyncio.run(run())


@pytest.mark.parametrize("streaming", [False, True])
def test_gateway_prepares_verifies_and_binds_exact_provider_request(hooks, streaming):
    instance, provider = gateway(window=1600)
    protected = ModelMessage.text(ModelRole.SYSTEM, "active instructions")
    hooks.protected.append(protected)

    def prepare(request):
        hooks.events.append(("prepare", request))
        return request.model_copy(update={"messages": (protected, *request.messages)})

    hooks.prepare_skill_model_request = prepare
    request = ModelRequest(messages=(ModelMessage.text(ModelRole.USER, "old" * 800),
        ModelMessage.text(ModelRole.ASSISTANT, "done"),
        ModelMessage.text(ModelRole.USER, "new")), max_output_tokens=100)
    invoke(instance, request, streaming)
    assert [event[0] for event in hooks.events] == ["prepare", "verify", "record"]
    assert hooks.events[1][1] is provider.requests[0]
    assert hooks.events[2][1] is provider.requests[0]
    assert hooks.events[2][2] == (provider.call,)
    assert provider.requests[0].messages == (protected, request.messages[-1])
    assert len(request.messages) == 3


@pytest.mark.parametrize("streaming", [False, True])
def test_request_missing_required_instructions_is_rejected_before_provider(hooks, streaming):
    class BrokenPreparation(ContextManager):
        def prepare(self, request, *, window_tokens):
            changed = request.model_copy(update={"messages": request.messages[-1:]})
            return PreparedContext(changed, 10)

    protected = ModelMessage.text(ModelRole.SYSTEM, "instructions")
    hooks.protected.append(protected)
    instance, provider = gateway(context_manager=BrokenPreparation())
    request = ModelRequest(messages=(protected, ModelMessage.text(ModelRole.USER, "now")))
    with pytest.raises(ValueError, match="instructions missing"):
        invoke(instance, request, streaming)
    assert provider.requests == []
    assert not any(event[0] == "record" for event in hooks.events)


@pytest.mark.parametrize("streaming", [False, True])
def test_capacity_failure_does_not_create_model_call_proof(hooks, streaming):
    protected = ModelMessage.text(ModelRole.SYSTEM, "instructions " * 400)
    hooks.protected.append(protected)
    instance, provider = gateway(window=1000)
    request = ModelRequest(messages=(protected, ModelMessage.text(ModelRole.USER, "now")),
                           max_output_tokens=100)
    with pytest.raises(ContextCapacityError):
        invoke(instance, request, streaming)
    assert provider.requests == []
    assert not any(event[0] == "record" for event in hooks.events)


def test_failed_partial_stream_does_not_bind_returned_tool_calls(hooks):
    instance, provider = gateway(Provider(fail_after_call=True))
    with pytest.raises(ModelProviderError):
        invoke(instance, ModelRequest(messages=(ModelMessage.text(ModelRole.USER, "now"),),
                                      max_output_tokens=100), True)
    assert len(provider.requests) == 1
    assert not any(event[0] == "record" for event in hooks.events)


@pytest.mark.parametrize("reason", [FinishReason.ERROR, FinishReason.LENGTH, FinishReason.STOP])
@pytest.mark.parametrize("streaming", [False, True])
def test_unsuccessful_or_inconsistent_finish_reason_does_not_bind_calls(hooks, reason, streaming):
    class UnsuccessfulProvider(Provider):
        async def complete(self, request):
            self.requests.append(request)
            return ModelResponse(response_id="r1", provider_id="test", model="test",
                                 tool_calls=(self.call,), finish_reason=reason)

        async def stream(self, request):
            self.requests.append(request)
            yield ModelStreamEvent(type=ModelStreamEventType.TOOL_CALL_DELTA, tool_call=self.call)
            yield ModelStreamEvent(type=ModelStreamEventType.MESSAGE_END, finish_reason=reason)

    instance, provider = gateway(UnsuccessfulProvider())
    invoke(instance, ModelRequest(messages=(ModelMessage.text(ModelRole.USER, "now"),),
                                  max_output_tokens=100), streaming)
    assert len(provider.requests) == 1
    assert not any(event[0] == "record" for event in hooks.events)


def test_stream_without_terminal_event_does_not_bind_calls(hooks):
    class IncompleteProvider(Provider):
        async def stream(self, request):
            self.requests.append(request)
            yield ModelStreamEvent(type=ModelStreamEventType.TOOL_CALL_DELTA, tool_call=self.call)

    instance, _ = gateway(IncompleteProvider())
    invoke(instance, ModelRequest(messages=(ModelMessage.text(ModelRole.USER, "now"),),
                                  max_output_tokens=100), True)
    assert not any(event[0] == "record" for event in hooks.events)


def test_successful_fallback_binds_its_actual_request_not_failed_primary(hooks):
    class FailedProvider(Provider):
        async def complete(self, request):
            self.requests.append(request)
            raise ModelProviderError(ModelErrorCode.UNAVAILABLE, "failed", retryable=True)

    instance, failed = gateway(FailedProvider(), window=4000)
    success = Provider()
    instance._profiles["fallback"] = instance._profiles["test"].model_copy(update={
        "profile_id": "fallback", "capabilities": ModelCapabilities(max_context_tokens=1000)})
    instance._routes["test"] = ModelRoute(route_id="test", primary_profile="test", fallback_profiles=("fallback",))
    instance._providers["fallback"] = success
    request = ModelRequest(messages=(ModelMessage.text(ModelRole.USER, "x" * 2000),
        ModelMessage.text(ModelRole.ASSISTANT, "old"), ModelMessage.text(ModelRole.USER, "now")),
        max_output_tokens=100)
    invoke(instance, request, False)
    records = [event for event in hooks.events if event[0] == "record"]
    assert len(records) == 1
    assert records[0][1] is success.requests[0]
    assert records[0][1] is not failed.requests[0]
    assert len(failed.requests[0].messages) == 3
    assert len(success.requests[0].messages) == 1


class NamedStopProvider(Provider):
    """Some compatible APIs return STOP for an explicitly named function."""

    def __init__(self, names=("perform",), *, reason=FinishReason.STOP, end=True, fail=False):
        super().__init__()
        self.calls = tuple(ModelToolCall(call_id=f"named{i}", name=name, arguments={"value": 1})
                           for i, name in enumerate(names))
        self.reason, self.end, self.fail = reason, end, fail

    async def complete(self, request):
        self.requests.append(request)
        if self.fail:
            raise ModelProviderError(ModelErrorCode.UNAVAILABLE, "failed", retryable=True)
        return ModelResponse(response_id="named1", provider_id="test", model="test",
                             tool_calls=self.calls, finish_reason=self.reason)

    async def stream(self, request):
        self.requests.append(request)
        for call in self.calls:
            yield ModelStreamEvent(type=ModelStreamEventType.TOOL_CALL_DELTA, tool_call=call)
        if self.fail:
            raise ModelProviderError(ModelErrorCode.UNAVAILABLE, "failed", retryable=True)
        if self.end:
            yield ModelStreamEvent(type=ModelStreamEventType.MESSAGE_END, finish_reason=self.reason)


def named_choice_request(choice=None, *, tools=("perform",)):
    request = ModelRequest(messages=(ModelMessage.text(ModelRole.USER, "act"),),
                           tools=tuple(ModelTool(name=name, input_schema={"type": "object"})
                                       for name in tools), max_output_tokens=100)
    # Existing product-local named-choice extension is already supported by
    # compatible provider adapters. Do not expand the public request schema here.
    return request.model_copy(update={"tool_choice": choice if choice is not None else {
        "type": "function", "function": {"name": "perform"},
    }})


@pytest.mark.parametrize("streaming", [False, True])
def test_explicit_named_function_stop_can_bind_exact_calls(hooks, streaming):
    provider = NamedStopProvider()
    instance, _ = gateway(provider)
    result = invoke(instance, named_choice_request(), streaming)
    record, = [event for event in hooks.events if event[0] == "record"]
    assert record[1] is provider.requests[0]
    assert record[2] == provider.calls
    # The gateway proof does not normalize provider output or silently enable
    # the compatibility path for ordinary ReAct requests.
    assert (result[-1].finish_reason if streaming else result.finish_reason) is FinishReason.STOP


@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize("choice,names,tools", [
    ("auto", ("perform",), ("perform",)),
    ("required", ("perform",), ("perform",)),
    ("none", ("perform",), ("perform",)),
    ({"type": "function", "function": {"name": "perform"}}, ("perform", "perform"), ("perform",)),
    ({"type": "function", "function": {"name": "perform"}}, ("other",), ("perform", "other")),
    ({"type": "function", "function": {"name": "perform"}}, ("perform", "other"), ("perform", "other")),
    ({"type": "function", "function": {"name": "perform"}}, ("perform",), ("other",)),
    ({"type": "function", "function": {"name": "perform", "unexpected": True}}, ("perform",), ("perform",)),
    ({"type": "function", "function": {"name": "perform"}, "unexpected": True}, ("perform",), ("perform",)),
    ({"type": "not_function", "function": {"name": "perform"}}, ("perform",), ("perform",)),
    ({"type": "function", "function": {}}, ("perform",), ("perform",)),
    ({"type": "function", "function": {"name": 1}}, ("perform",), ("perform",)),
])
def test_stop_named_choice_compatibility_rejects_unmatched_or_malformed_calls(hooks, streaming, choice, names, tools):
    instance, _ = gateway(NamedStopProvider(names))
    invoke(instance, named_choice_request(choice, tools=tools), streaming)
    assert not any(event[0] == "record" for event in hooks.events)


@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize("reason", [FinishReason.ERROR, FinishReason.LENGTH, FinishReason.UNKNOWN])
def test_named_choice_never_binds_failed_finish_reason(hooks, streaming, reason):
    instance, _ = gateway(NamedStopProvider(reason=reason))
    invoke(instance, named_choice_request(), streaming)
    assert not any(event[0] == "record" for event in hooks.events)


def test_named_choice_partial_or_interrupted_stream_never_binds(hooks):
    for provider in (NamedStopProvider(end=False), NamedStopProvider(fail=True)):
        instance, _ = gateway(provider)
        if provider.fail:
            with pytest.raises(ModelProviderError):
                invoke(instance, named_choice_request(), True)
        else:
            invoke(instance, named_choice_request(), True)
    assert not any(event[0] == "record" for event in hooks.events)


@pytest.mark.parametrize("streaming", [False, True])
def test_real_runtime_preloaded_document_reaches_provider_and_binds_call(tmp_path, streaming):
    from suiteharness.execution import RunRequest
    from suiteharness.runtime import RequestScope, ScopePath
    from suiteharness.skills import FilesystemSkillProvider
    from suiteharness.skills.runtime import SkillRuntime, bind_skill_runtime, preload_skill

    directory = tmp_path / "work"
    directory.mkdir()
    (directory / "SKILL.md").write_text(
        "---\nname: work\ndescription: Work task.\nversion: 1.0.0\n---\n\nFull work instructions.\n",
        encoding="utf-8")
    request = RunRequest(run_id="skill-run", grant_id="grant1", input="work",
        scope=RequestScope(path=ScopePath.product("company", "demo"), principal_id="employee"))
    runtime = SkillRuntime(request, FilesystemSkillProvider(tmp_path, namespace="demo"))
    instance, provider = gateway()

    async def run():
        await runtime.initialize()
        with bind_skill_runtime(runtime):
            document = await preload_skill(request, "demo.work")
            model_request = ModelRequest(messages=(ModelMessage.text(ModelRole.USER, "work"),),
                                         max_output_tokens=100)
            if streaming:
                [event async for event in instance.stream("test", model_request)]
            else:
                await instance.complete("test", model_request)
            assert any(document.instructions in message.content[0].text
                       for message in provider.requests[0].messages)
            assert runtime.missing(provider.call.call_id, ("demo.work",)) == ()
            assert provider.requests[0].skill_context_token
            assert "skill_context_token" not in provider.requests[0].model_dump()

    asyncio.run(run())


@pytest.mark.parametrize("streaming", [False, True])
def test_real_runtime_oversize_document_never_falls_back_to_truncated_instructions(tmp_path, streaming):
    from suiteharness.execution import RunRequest
    from suiteharness.runtime import RequestScope, ScopePath
    from suiteharness.skills import FilesystemSkillProvider
    from suiteharness.skills.runtime import SkillRuntime, bind_skill_runtime, preload_skill

    directory = tmp_path / "work"
    directory.mkdir()
    (directory / "SKILL.md").write_text(
        "---\nname: work\ndescription: Work task.\nversion: 1.0.0\n---\n\n" + "mandatory " * 400,
        encoding="utf-8")
    request = RunRequest(run_id="oversize-run", grant_id="grant1", input="work",
        scope=RequestScope(path=ScopePath.product("company", "demo"), principal_id="employee"))
    runtime = SkillRuntime(request, FilesystemSkillProvider(tmp_path, namespace="demo"))
    instance, provider = gateway(window=1000)

    async def run():
        await runtime.initialize()
        with bind_skill_runtime(runtime):
            await preload_skill(request, "demo.work")
            model_request = ModelRequest(messages=(ModelMessage.text(ModelRole.USER, "work"),),
                                         max_output_tokens=100)
            with pytest.raises(ContextCapacityError):
                if streaming:
                    [event async for event in instance.stream("test", model_request)]
                else:
                    await instance.complete("test", model_request)
            assert provider.requests == []
            assert runtime.call_proofs == {}

    asyncio.run(run())


@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize("explicit", [False, True])
def test_real_runtime_named_stop_compatibility_requires_explicit_tool(tmp_path, streaming, explicit):
    from suiteharness.execution import RunRequest
    from suiteharness.runtime import RequestScope, ScopePath
    from suiteharness.skills import FilesystemSkillProvider
    from suiteharness.skills.runtime import SkillRuntime, bind_skill_runtime, preload_skill

    directory = tmp_path / "work"
    directory.mkdir()
    (directory / "SKILL.md").write_text(
        "---\nname: work\ndescription: Work task.\nversion: 1.0.0\n---\n\nFull work instructions.\n",
        encoding="utf-8")
    request = RunRequest(run_id="named-run", grant_id="grant1", input="work",
        scope=RequestScope(path=ScopePath.product("company", "demo"), principal_id="employee"))
    runtime = SkillRuntime(request, FilesystemSkillProvider(tmp_path, namespace="demo"))
    provider = NamedStopProvider()
    instance, _ = gateway(provider)

    async def run():
        await runtime.initialize()
        with bind_skill_runtime(runtime):
            document = await preload_skill(request, "demo.work")
            model_request = named_choice_request() if explicit else named_choice_request("auto")
            if streaming:
                [event async for event in instance.stream("test", model_request)]
            else:
                await instance.complete("test", model_request)
            assert any(document.instructions in message.content[0].text
                       for message in provider.requests[0].messages)
            assert bool(runtime.call_proofs) is explicit
            if explicit:
                assert runtime.missing(provider.calls[0].call_id, ("demo.work",)) == ()

    asyncio.run(run())


def test_named_stop_proof_waits_for_full_stream_exhaustion(hooks):
    instance, _ = gateway(NamedStopProvider())

    async def run():
        source = instance.stream("test", named_choice_request())
        try:
            async for event in source:
                if event.type is ModelStreamEventType.MESSAGE_END:
                    break
        finally:
            await source.aclose()

    asyncio.run(run())
    assert not any(event[0] == "record" for event in hooks.events)


def test_named_stop_checks_final_prepared_tool_list(hooks):
    class RemoveTools(ContextManager):
        def prepare(self, request, *, window_tokens):
            prepared = super().prepare(request, window_tokens=window_tokens)
            return PreparedContext(prepared.request.model_copy(update={"tools": ()}),
                                   prepared.estimated_input_tokens)

    instance, provider = gateway(NamedStopProvider(), context_manager=RemoveTools())
    invoke(instance, named_choice_request(), False)
    assert provider.requests[0].tools == ()
    assert not any(event[0] == "record" for event in hooks.events)
