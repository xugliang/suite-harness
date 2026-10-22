"""Character diagnostics never call an external model or log prompt contents."""

import asyncio
import json
import logging
from datetime import datetime, timezone

import pytest

from suiteharness import diagnostics as diag
from suiteharness.context_labels import CHARACTER_CATEGORIES
from suiteharness.models import (
    AdapterKind, DocumentContent, FinishReason, ImageContent, ModelCapabilities,
    ModelMessage, ModelProfile, ModelRequest, ModelResponse, ModelRole, ModelRoute,
    ModelStreamEvent, ModelStreamEventType, ModelTool, ModelToolCall, ModelUsage,
    ProviderAuthKind, ProviderDescriptor, TextContent,
)
from suiteharness.models.characters import comparison_fields, measure_characters
from suiteharness.models.context import ContextCapacityError
from suiteharness.models.errors import ModelErrorCode, ModelProviderError
from suiteharness.models.gateway import ModelGateway
from suiteharness.models.protocols import ModelProvider
from suiteharness.models.registry import ProviderRegistry
from suiteharness.models.secrets import MappingSecretResolver


@pytest.fixture
def records():
    result = []
    class Capture(logging.Handler):
        def emit(self, record):
            result.append(json.loads(record.getMessage()))
    handler = Capture()
    old = diag._logger.level
    diag._logger.setLevel(logging.INFO)
    diag._logger.addHandler(handler)
    yield result
    diag._logger.removeHandler(handler)
    diag._logger.setLevel(old)


def request_messages():
    return (
        ModelMessage.text(ModelRole.SYSTEM, "规则", context_category="system_prompt"),
        ModelMessage.text(ModelRole.USER, "旧问题", context_category="history_memory"),
        ModelMessage.text(ModelRole.ASSISTANT, "旧回答", context_category="history_memory"),
        ModelMessage.text(ModelRole.USER, "新问题🙂\n", context_category="current_question"),
        ModelMessage(role=ModelRole.ASSISTANT, context_category="current_execution",
                     tool_calls=(ModelToolCall(call_id="opaque-call", name="lookup",
                                               arguments={"q": "词语"}),)),
        ModelMessage.text(ModelRole.TOOL, "工具结果", tool_call_id="opaque-call", name="lookup",
                          context_category="current_execution"),
    )


class Provider(ModelProvider):
    provider_id = "test"

    def __init__(self, fail_until=0, error_type=ModelProviderError):
        self.calls = []
        self.fail_until, self.error_type = fail_until, error_type

    async def complete(self, request):
        self.calls.append(request)
        if len(self.calls) <= self.fail_until:
            if self.error_type is ModelProviderError:
                raise ModelProviderError(ModelErrorCode.TIMEOUT, "SECRET_HTTP_BODY", retryable=True)
            raise self.error_type("SECRET_HTTP_BODY")
        return ModelResponse(response_id="response1", provider_id="test", model="test",
                             content=(TextContent(text="SECRET_RESPONSE"),),
                             usage=ModelUsage(input_tokens=123, output_tokens=7))

    async def stream(self, request):
        response = await self.complete(request)
        yield ModelStreamEvent(type=ModelStreamEventType.TEXT_DELTA, text_delta="SECRET_RESPONSE")
        yield ModelStreamEvent(type=ModelStreamEventType.USAGE, usage=response.usage)
        yield ModelStreamEvent(type=ModelStreamEventType.MESSAGE_END, finish_reason=FinishReason.STOP)


def gateway(*, window=32768, retries=0, provider=None):
    provider = provider or Provider()
    registry = ProviderRegistry()
    registry.register(ProviderDescriptor(provider_id="test", display_name="test",
        adapter=AdapterKind.OPENAI_CHAT, auth=ProviderAuthKind.INTERNAL_NONE), lambda *args: provider)
    instance = ModelGateway(registry,
        {"test": ModelProfile(profile_id="test", provider_id="test", model="test", max_retries=retries,
                              capabilities=ModelCapabilities(max_context_tokens=window))},
        {"test": ModelRoute(route_id="test", primary_profile="test")}, None, MappingSecretResolver({}))
    return instance, provider


def invoke(instance, request, streaming):
    async def run():
        if streaming:
            return [event async for event in instance.stream("test", request)]
        return await instance.complete("test", request)
    return asyncio.run(run())


def assert_totals(record):
    for prefix in ("before", "after", "reduced"):
        if f"{prefix}_total_chars" in record:
            assert record[f"{prefix}_total_chars"] == sum(
                record[f"{prefix}_{name}_chars"] for name in CHARACTER_CATEGORIES)
    if record.get("after_known"):
        assert record["reduced_total_chars"] == record["before_total_chars"] - record["after_total_chars"]


def test_unicode_count_categories_tools_and_nontext():
    message = ModelMessage(role=ModelRole.USER, context_category="current_question", content=(
        TextContent(text="中文🙂é\n"),
        ImageContent(data_base64="SECRET_BASE64" * 1000),
        ImageContent(url="data:image/png;base64,SECRET_IMAGE"),
        DocumentContent(mime_type="application/pdf", data_base64="SECRET_PDF" * 1000),
    ))
    request = ModelRequest(messages=(*request_messages(), message,
        ModelMessage.text(ModelRole.USER, "未知来源")),
        tools=(ModelTool(name="lookup", description="查代码", input_schema={"type": "object"}),),
        metadata={"secret": "SECRET_TOKEN"}, response_schema={"type": "string"})
    counts = measure_characters(request).fields("before")
    assert counts["before_system_prompt_chars"] == 2
    assert counts["before_history_memory_chars"] == 6
    assert counts["before_current_question_chars"] == len("新问题🙂\n中文🙂é\n")
    assert counts["before_current_execution_chars"] == len('lookup{"q":"词语"}lookup工具结果')
    assert counts["before_tool_definitions_chars"] == len('lookup查代码{"type":"object"}')
    assert counts["before_unclassified_chars"] == len('未知来源{"type":"string"}')
    assert counts["before_nontext_parts"] == 3
    assert_totals(counts)
    assert "SECRET" not in repr(measure_characters(request))


def test_unlabelled_user_and_tool_are_not_guessed_as_history():
    request = ModelRequest(messages=(ModelMessage.text(ModelRole.SYSTEM, "系统"),
        ModelMessage.text(ModelRole.USER, "可能是资料也可能是问题"),
        ModelMessage.text(ModelRole.TOOL, "历史工具或新工具", tool_call_id="c1")))
    fields = measure_characters(request).fields("before")
    assert fields["before_system_prompt_chars"] == 2
    assert fields["before_unclassified_chars"] == len("可能是资料也可能是问题历史工具或新工具")
    assert fields["before_history_memory_chars"] == fields["before_current_execution_chars"] == 0


@pytest.mark.parametrize("streaming", [False, True])
def test_measured_actual_calls_keep_usage_without_content(records, streaming):
    instance, provider = gateway()
    request = ModelRequest(messages=request_messages(), max_output_tokens=100)
    invoke(instance, request, streaming)
    context = next(r for r in records if r["event"] == "model.context_ready")
    attempt = next(r for r in records if r["event"] == "model.attempt_started")
    assert context["provider_invoked"] is False and attempt["provider_invoked"] is True
    assert attempt["streaming"] is streaming
    assert context["before_total_chars"] == context["after_total_chars"]
    assert context["reduced_total_chars"] == 0
    assert len(provider.calls) == 1
    assert_totals(context)
    assert_totals(attempt)
    finished = next(r for r in records if r["event"] == "model.finished")
    assert finished["input_tokens"] == 123 and finished["output_tokens"] == 7
    output = json.dumps(records)
    assert all(text not in output for text in ("SECRET", "新问题", "工具结果", "词语"))


@pytest.mark.parametrize("streaming", [False, True])
def test_compaction_and_terminal_capacity_failure_have_after_sizes(records, streaming):
    instance, provider = gateway(window=1000)
    request = ModelRequest(messages=(
        ModelMessage.text(ModelRole.USER, "旧问题" * 500, context_category="history_memory"),
        ModelMessage.text(ModelRole.ASSISTANT, "旧回答", context_category="history_memory"),
        ModelMessage.text(ModelRole.USER, "当前问题" * 500, context_category="current_question"),
        ModelMessage(role=ModelRole.ASSISTANT, context_category="current_execution",
                     tool_calls=(ModelToolCall(call_id="c1", name="lookup", arguments={}),)),
        ModelMessage.text(ModelRole.TOOL, "巨大结果" * 500, tool_call_id="c1",
                          context_category="current_execution"),
    ), max_output_tokens=100)
    with pytest.raises(ContextCapacityError):
        invoke(instance, request, streaming)
    record = next(r for r in records if r["event"] == "model.context_failed")
    assert record["provider_invoked"] is False and record["after_known"] is True
    assert record["removed_rounds"] == record["truncated_tools"] == 1
    assert record["after_history_memory_chars"] == 0
    assert record["after_current_question_chars"] == len("当前问题" * 500)
    assert record["after_current_execution_chars"] < record["before_current_execution_chars"]
    assert_totals(record)
    assert provider.calls == []
    assert not any(r["event"] == "model.attempt_started" for r in records)


@pytest.mark.parametrize("streaming", [False, True])
def test_successful_compaction_preserves_category_and_original(records, streaming):
    instance, provider = gateway(window=1000)
    request = ModelRequest(messages=(
        ModelMessage.text(ModelRole.USER, "旧问题" * 500, context_category="history_memory"),
        ModelMessage.text(ModelRole.ASSISTANT, "旧回答", context_category="history_memory"),
        ModelMessage.text(ModelRole.USER, "现在", context_category="current_question"),
    ), max_output_tokens=100)
    invoke(instance, request, streaming)
    record = next(r for r in records if r["event"] == "model.context_ready")
    assert record["after_total_chars"] == 2
    assert record["after_current_question_chars"] == 2
    assert record["after_history_memory_chars"] == 0
    assert record["before_history_memory_chars"] == 1503
    assert len(request.messages) == 3 and len(provider.calls[0].messages) == 1
    assert provider.calls[0].messages[0].context_category == "current_question"
    assert_totals(record)


@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize("succeeds", [False, True])
def test_retry_attempts_each_have_same_sizes_and_distinct_attempt(records, streaming, succeeds):
    instance, provider = gateway(retries=2, provider=Provider(fail_until=2 if succeeds else 3))
    request = ModelRequest(messages=request_messages(), max_output_tokens=100)
    if succeeds:
        invoke(instance, request, streaming)
    else:
        with pytest.raises(ModelProviderError):
            invoke(instance, request, streaming)
    starts = [r for r in records if r["event"] == "model.attempt_started"]
    finishes = [r for r in records if r["event"] == "model.attempt_finished"]
    assert [r["attempt"] for r in starts] == [1, 2, 3]
    assert len(finishes) == len(provider.calls) == 3
    assert {r["model_call_id"] for r in starts} == {starts[0]["model_call_id"]}
    assert len({r["before_total_chars"] for r in starts}) == 1
    assert all(r["provider_invoked"] for r in starts + finishes)
    assert [r["status"] for r in finishes] == ["failed", "failed", "succeeded" if succeeds else "failed"]
    assert "SECRET" not in json.dumps(records)


@pytest.mark.parametrize("streaming", [False, True])
def test_unexpected_provider_failure_is_counted(records, streaming):
    instance, provider = gateway(provider=Provider(fail_until=1, error_type=RuntimeError))
    with pytest.raises(RuntimeError):
        invoke(instance, ModelRequest(messages=request_messages()), streaming)
    assert len(provider.calls) == 1
    assert next(r for r in records if r["event"] == "model.attempt_finished")["status"] == "failed"
    assert "SECRET" not in json.dumps(records)


def test_unknown_after_is_explicit_not_fabricated():
    counts = measure_characters(ModelRequest(messages=request_messages()))
    fields = comparison_fields(counts, None)
    assert fields["after_known"] is False
    assert "after_total_chars" not in fields and "reduced_total_chars" not in fields


@pytest.mark.parametrize("streaming", [False, True])
def test_validation_failure_logs_before_size_but_does_not_call_provider(records, streaming):
    instance, provider = gateway()
    request = ModelRequest(messages=(ModelMessage.text(ModelRole.USER, "private"),), model="forbidden-model")
    with pytest.raises(ModelProviderError):
        invoke(instance, request, streaming)
    started = next(r for r in records if r["event"] == "model.started")
    assert started["before_total_chars"] == 7
    assert started["max_output_tokens"] == 4096
    assert "output_tokens" not in started
    failed = next(r for r in records if r["event"] == "model.context_failed")
    assert failed["before_total_chars"] == 7
    assert failed["after_known"] is False and failed["provider_invoked"] is False
    assert "after_total_chars" not in failed
    assert provider.calls == []


@pytest.mark.parametrize("streaming", [False, True])
def test_fallback_profiles_have_separate_sizes_and_only_fitting_one_calls(records, streaming):
    instance, provider = gateway(window=1000)
    instance._profiles["larger"] = instance._profiles["test"].model_copy(update={
        "profile_id": "larger", "capabilities": ModelCapabilities(max_context_tokens=10000)})
    instance._routes["test"] = ModelRoute(route_id="test", primary_profile="test", fallback_profiles=("larger",))
    request = ModelRequest(messages=(ModelMessage.text(ModelRole.USER, "问题" * 500,
        context_category="current_question"),), max_output_tokens=100)
    invoke(instance, request, streaming)
    failed = next(r for r in records if r["event"] == "model.context_failed")
    called = next(r for r in records if r["event"] == "model.attempt_started")
    assert failed["profile_id"] == "test" and failed["provider_invoked"] is False
    assert called["profile_id"] == "larger" and called["provider_invoked"] is True
    assert failed["before_total_chars"] == called["before_total_chars"] == 1000
    assert failed["model_call_id"] == called["model_call_id"]
    assert len(provider.calls) == 1


def test_diagnostics_ignore_bad_optional_copy_label():
    message = ModelMessage.text(ModelRole.USER, "input").model_copy(update={"context_category": ["bad"]})
    counts = measure_characters(ModelRequest(messages=(message,)))
    assert counts.fields("before")["before_unclassified_chars"] == 5


def test_labels_are_internal_and_openai_wire_body_unchanged():
    from suiteharness.models.adapters.openai import OpenAIChatAdapter
    from suiteharness.execution import PromptMessage
    descriptor = ProviderDescriptor(provider_id="test", display_name="test", adapter=AdapterKind.OPENAI_CHAT,
        auth=ProviderAuthKind.INTERNAL_NONE, default_base_url="https://example.invalid/v1")
    adapter = OpenAIChatAdapter(descriptor, ModelProfile(profile_id="test", provider_id="test", model="test"), None, None)
    tagged = ModelRequest(messages=request_messages())
    untagged = ModelRequest.model_validate_json(tagged.model_dump_json())
    assert all(m.context_category is None for m in untagged.messages)
    assert adapter._body(tagged, stream=False) == adapter._body(untagged, stream=False)
    assert "context_category" not in PromptMessage(role="user", content="x", context_category="current_question").model_dump()


def test_default_prompt_provenance_and_react_checkpoint_compatibility():
    from suiteharness.agents.react import ReActWorkflow
    from suiteharness.execution import ConversationContext, ConversationMessage, PromptStrategy, RunRequest, WorkflowFrame
    from suiteharness.runtime import RequestScope, ScopePath
    request = RunRequest(run_id="char-test", grant_id="grant1", input="当前问题",
        scope=RequestScope(path=ScopePath.product("company", "product"), principal_id="employee"),
        conversation=ConversationContext(working_summary="摘要", long_term_context="长期记忆",
            messages=(ConversationMessage(role="user", content="以前问题", principal_id="employee",
                                          occurred_at=datetime.now(timezone.utc)),)))
    frame = WorkflowFrame(iteration=1, tool_calls_used=0, elapsed_seconds=0.0)
    workflow = ReActWorkflow(None, "test")
    prompt = asyncio.run(PromptStrategy().render(request, frame))
    messages = workflow._initial_messages(request, prompt)
    assert [m.context_category for m in messages] == ["system_prompt", "conversation_summary", "history_memory", "current_question"]
    state = workflow._dump_state(request, messages, 0, set())
    assert all("context_category" not in item for item in state["messages"])
    resumed, _, _ = workflow._restore_state(request, frame.model_copy(update={"workflow_state": state}), None)
    assert [m.context_category for m in resumed] == state["context_categories"]
    legacy = {k: v for k, v in state.items() if k != "context_categories"}
    old, _, _ = workflow._restore_state(request, frame.model_copy(update={"workflow_state": legacy}), None)
    assert all(m.context_category is None for m in old)


def test_end_to_end_react_second_model_call_counts_tool_exchange(records):
    from suiteharness.agents.react import ReActWorkflow
    from suiteharness.execution import PromptStrategy, RunRequest, RunStatus, ToolEffect, ToolSpec
    from suiteharness.execution.in_memory import (
        InMemoryApprovalStore, InMemoryAuditJournal, InMemoryCapabilityAuthority, InMemoryToolRegistry,
    )
    from suiteharness.execution.protocols import ExecutionStrategies
    from suiteharness.execution.runner import ExecutionRunner
    from suiteharness.runtime import RequestScope, ScopePath

    class TwoTurns(Provider):
        async def stream(self, request):
            self.calls.append(request)
            if len(self.calls) == 1:
                yield ModelStreamEvent(type=ModelStreamEventType.TOOL_CALL_DELTA,
                    tool_call=ModelToolCall(call_id="c1", name="lookup", arguments={"q": "模块"}))
                yield ModelStreamEvent(type=ModelStreamEventType.MESSAGE_END, finish_reason=FinishReason.TOOL_CALLS)
            else:
                yield ModelStreamEvent(type=ModelStreamEventType.TEXT_DELTA, text_delta="完成")
                yield ModelStreamEvent(type=ModelStreamEventType.MESSAGE_END, finish_reason=FinishReason.STOP)

    instance, provider = gateway(provider=TwoTurns())
    async def run():
        registry, authority = InMemoryToolRegistry(), InMemoryCapabilityAuthority()
        scope = RequestScope(path=ScopePath.product("company", "product"), principal_id="employee")
        async def lookup(context, arguments):
            return {"features": ["秘密模块"]}
        registry.register(scope.path, ToolSpec(name="lookup", description="功能检索",
            input_schema={"type": "object", "properties": {"q": {"type": "string"}}},
            effects=frozenset({ToolEffect.READ})), lookup)
        grant = await authority.issue(scope.path, tool_identities=(registry.resolve(scope, "lookup").identity,))
        runner = ExecutionRunner(tools=registry, capabilities=authority, approvals=InMemoryApprovalStore(),
                                 journal=InMemoryAuditJournal())
        request = RunRequest(run_id="measured-react", scope=scope, grant_id=grant.grant_id, input="查询功能")
        result = await runner.run(request, ExecutionStrategies(workflow=ReActWorkflow(instance, "test"),
                                                               prompt=PromptStrategy()))
        assert result.status is RunStatus.SUCCEEDED
        assert result.output == "完成"
    asyncio.run(run())
    assert len(provider.calls) == 2
    contexts = [r for r in records if r["event"] == "model.context_ready"]
    assert [r["iteration"] for r in contexts] == [1, 2]
    assert contexts[0]["before_current_execution_chars"] == 0
    assert contexts[1]["before_current_execution_chars"] > 0
    assert contexts[0]["before_current_question_chars"] == contexts[1]["before_current_question_chars"] == 4
    assert contexts[0]["before_tool_definitions_chars"] == contexts[1]["before_tool_definitions_chars"] > 0
    assert all(r["before_unclassified_chars"] == 0 for r in contexts)
    assert "秘密模块" not in json.dumps(records, ensure_ascii=False)


def test_counts_survive_safe_export_and_no_secrets(tmp_path, capsys):
    request_id = "request-" + "a" * 40
    counts = measure_characters(ModelRequest(messages=request_messages()))
    event = {"schema": "suiteharness.diagnostic.v1", "event": "model.context_ready",
             "request_id": request_id, "provider_invoked": False,
             **comparison_fields(counts, counts), "raw_prompt": "SECRET"}
    (tmp_path / "diagnostics.jsonl").write_text(json.dumps(event), encoding="utf-8")
    diag.main(["--request-id", request_id, "--logs", str(tmp_path)])
    text = capsys.readouterr().out
    assert "SECRET" not in text
    exported = json.loads(text)
    assert exported["before_total_chars"] == counts.total
    assert exported["after_total_chars"] == counts.total
    assert_totals(exported)
