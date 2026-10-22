from types import SimpleNamespace

import pytest

from suiteharness.models.adapters.bedrock import BedrockConverseAdapter
from suiteharness.models.types import ModelMessage, ModelRequest, ModelRole


@pytest.mark.parametrize(
    ("history", "expected_roles", "first_user_texts"),
    [
        ((), ["user"], ["conversation_summary", "当前问题"]),
        ((ModelMessage.text(ModelRole.USER, "旧问题"), ModelMessage.text(ModelRole.ASSISTANT, "旧回答")),
         ["user", "assistant", "user"], ["conversation_summary", "旧问题"]),
        ((ModelMessage.text(ModelRole.ASSISTANT, "旧回答"),),
         ["user", "assistant", "user"], ["conversation_summary"]),
    ],
)
def test_nova_wire_starts_with_user_without_relabeling_internal_summary(history, expected_roles, first_user_texts):
    adapter = object.__new__(BedrockConverseAdapter)
    adapter.provider_id = "bedrock"
    adapter._profile = SimpleNamespace(model="amazon.nova-pro-v1:0")
    summary = ModelMessage.text(
        ModelRole.ASSISTANT,
        '{"context_type":"conversation_summary","trust":"untrusted","working_summary":"旧摘要"}',
        context_category="conversation_summary",
    )
    request = ModelRequest(messages=(
        ModelMessage.text(ModelRole.SYSTEM, "系统规则"),
        summary,
        *history,
        ModelMessage.text(ModelRole.USER, "当前问题"),
    ))

    body = adapter._body(request)

    assert summary.role is ModelRole.ASSISTANT
    assert body["system"] == [{"text": "系统规则"}]
    assert [message["role"] for message in body["messages"]] == expected_roles
    content = "\n".join(item["text"] for item in body["messages"][0]["content"] if "text" in item)
    assert all(part in content for part in first_user_texts)


def test_non_nova_bedrock_keeps_assistant_summary_role():
    adapter = object.__new__(BedrockConverseAdapter)
    adapter.provider_id = "bedrock"
    adapter._profile = SimpleNamespace(model="anthropic.claude-3-haiku-20240307-v1:0")
    request = ModelRequest(messages=(
        ModelMessage.text(ModelRole.SYSTEM, "规则"),
        ModelMessage.text(ModelRole.ASSISTANT, "摘要", context_category="conversation_summary"),
        ModelMessage.text(ModelRole.USER, "问题"),
    ))

    assert [item["role"] for item in adapter._body(request)["messages"]] == ["assistant", "user"]
