"""Server-generated runtime context, composed after product prompt rendering."""

from datetime import datetime
from zoneinfo import ZoneInfo

from suiteharness.execution.models import PromptEnvelope, PromptMessage

_BEIJING = ZoneInfo("Asia/Shanghai")
_WEEKDAYS = ("星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日")


def with_runtime_environment(prompt: PromptEnvelope, *, instant: datetime) -> PromptEnvelope:
    if instant.tzinfo is None or instant.utcoffset() is None:
        raise ValueError("runtime clock must be timezone-aware")
    local = instant.astimezone(_BEIJING)
    environment = (
        "# 当前运行环境（服务器提供）\n"
        f"- 当前日期：{local:%Y-%m-%d}\n"
        f"- 当前时间：{local:%H:%M:%S}\n"
        f"- 星期：{_WEEKDAYS[local.weekday()]}\n"
        "- 时区：Asia/Shanghai（北京时间，UTC+08:00）\n"
        "今天、昨天等相对时间以本次请求的服务器时间为准，历史对话中的日期不是当前日期。"
    )
    messages = list(prompt.messages)
    # Keep system messages together for providers requiring a system prefix.
    last_system = next((i for i in range(len(messages) - 1, -1, -1)
                        if messages[i].role == "system"), None)
    if last_system is None:
        messages.insert(0, PromptMessage(role="system", content=environment))
    else:
        message = messages[last_system]
        messages[last_system] = message.model_copy(update={"content": message.content + "\n\n" + environment})
    return prompt.model_copy(update={"messages": tuple(messages)}, deep=True)
