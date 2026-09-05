"""Built-in server-side model adapters."""

from suiteharness.models.adapters.anthropic import AnthropicMessagesAdapter
from suiteharness.models.adapters.bedrock import (
    BedrockConverseAdapter,
    BedrockRuntimeClient,
    Boto3BedrockRuntimeClient,
    bedrock_factory,
)
from suiteharness.models.adapters.gemini import GeminiAdapter
from suiteharness.models.adapters.openai import OpenAIChatAdapter
from suiteharness.models.adapters.openai_responses import OpenAIResponsesAdapter
from suiteharness.models.adapters.vertex import (
    GoogleAuthTokenProvider,
    VertexGeminiAdapter,
    vertex_factory,
)

__all__ = [
    "AnthropicMessagesAdapter",
    "BedrockConverseAdapter",
    "BedrockRuntimeClient",
    "Boto3BedrockRuntimeClient",
    "bedrock_factory",
    "GeminiAdapter",
    "OpenAIChatAdapter",
    "OpenAIResponsesAdapter",
    "GoogleAuthTokenProvider",
    "VertexGeminiAdapter",
    "vertex_factory",
]
