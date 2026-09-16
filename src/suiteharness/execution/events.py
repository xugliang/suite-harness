"""Runner events; checkpoints are internal and must never be sent to clients."""
from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import aclosing
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue

from suiteharness.execution.models import RunResult, WorkflowDecision


class WorkflowEvent(BaseModel):
    model_config = ConfigDict(extra='forbid', frozen=True, strict=True)
    kind: Literal['delta', 'status', 'usage', 'compact_started', 'compact_completed']
    text: str = ''
    data: dict[str, JsonValue] = Field(default_factory=dict)


class RunEvent(BaseModel):
    model_config = ConfigDict(extra='forbid', frozen=True, strict=True)
    run_id: str
    sequence: int = Field(ge=1)
    kind: Literal['started', 'delta', 'status', 'usage', 'compact_started',
                  'compact_completed', 'tool_started', 'tool_completed', 'completed', 'failed', 'cancelled',
                  'checkpoint']
    text: str = ''
    data: dict[str, JsonValue] = Field(default_factory=dict)
    result: RunResult | None = None


async def collect_decision(source: AsyncIterator[WorkflowEvent | WorkflowDecision]) -> WorkflowDecision:
    decision = None
    async with aclosing(source):
        async for item in source:
            if decision is not None:
                raise ValueError('workflow emitted after its decision')
            if isinstance(item, WorkflowDecision):
                decision = item
            elif not isinstance(item, WorkflowEvent):
                raise TypeError('invalid workflow event')
    if decision is None:
        raise ValueError('workflow stream ended without a decision')
    return decision
