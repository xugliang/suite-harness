"""Default workflows, prompts, reflection policies and live event contracts."""

from suiteharness.agents.events import AgentEvent, AgentEventType, AgentRunContext
from suiteharness.agents.prompts import DefaultPromptStrategy
from suiteharness.agents.protocols import (
    AgentEventSink,
    AgentEventStream,
    ModelGatewayLike,
    ProductModelRouteResolver,
)
from suiteharness.agents.react import (
    ProductRoutedReActWorkflow,
    ReActStateError,
    ReActWorkflow,
)
from suiteharness.agents.reflection import (
    NoOpReflectionStrategy,
    SinglePassModelReflectionStrategy,
)

__all__ = [
    "AgentEvent",
    "AgentEventSink",
    "AgentEventStream",
    "AgentEventType",
    "AgentRunContext",
    "DefaultPromptStrategy",
    "ModelGatewayLike",
    "NoOpReflectionStrategy",
    "ProductModelRouteResolver",
    "ProductRoutedReActWorkflow",
    "ReActStateError",
    "ReActWorkflow",
    "SinglePassModelReflectionStrategy",
]
