import pytest

from suiteharness.models.adapters._common import decode_tool_arguments
from suiteharness.models.errors import ModelProviderError


def test_complete_nested_payload_with_only_outer_closing_brace_missing():
    text = '{"assignment_id":"asmt-test1234","answers":[{"number":1,"answer":"不会"}]'
    assert decode_tool_arguments(text, "dashscope") == {
        "assignment_id": "asmt-test1234", "answers": [{"number": 1, "answer": "不会"}],
    }


@pytest.mark.parametrize("text", [
    '{"answer":"cut off', '{"answer":', '{"answer":10', '{"answers":[{"number":1',
    '{"answers":[{"number":1}] garbage', '{"answers":[{"number":1}',
    '[{"number":1}]', '{"answers":[1],}', '{"answers":[1]} trailing',
])
def test_other_malformed_payloads_remain_rejected(text):
    with pytest.raises(ModelProviderError):
        decode_tool_arguments(text, "dashscope")
