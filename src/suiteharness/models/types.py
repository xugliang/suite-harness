"""Provider-neutral model vocabulary.

The public models deliberately contain no SDK objects.  A product can change
providers without changing its workflow state, and credentials never enter a
request or a transcript.
"""

from __future__ import annotations

import math
import re
from enum import Enum
from typing import Annotated, Literal
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field, JsonValue, field_validator, model_validator

_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,191}$")
_TOOL_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_.:-]{0,127}$")


class _FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class ModelRole(str, Enum):
    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"


class FinishReason(str, Enum):
    STOP = "stop"
    TOOL_CALLS = "tool_calls"
    LENGTH = "length"
    CONTENT_FILTER = "content_filter"
    CANCELLED = "cancelled"
    ERROR = "error"
    UNKNOWN = "unknown"


class ModelStreamEventType(str, Enum):
    MESSAGE_START = "message_start"
    TEXT_DELTA = "text_delta"
    REASONING_DELTA = "reasoning_delta"
    TOOL_CALL_DELTA = "tool_call_delta"
    USAGE = "usage"
    MESSAGE_END = "message_end"


class ProviderAuthKind(str, Enum):
    API_KEY = "api_key"
    AWS_IAM = "aws_iam"
    GOOGLE_SERVICE_ACCOUNT = "google_service_account"
    MANAGED_IDENTITY = "managed_identity"
    INTERNAL_NONE = "internal_none"


class AdapterKind(str, Enum):
    OPENAI_CHAT = "openai_chat"
    OPENAI_RESPONSES = "openai_responses"
    ANTHROPIC_MESSAGES = "anthropic_messages"
    GEMINI = "gemini"
    BEDROCK_CONVERSE = "bedrock_converse"
    VERTEX_GEMINI = "vertex_gemini"


class TextContent(_FrozenModel):
    type: Literal["text"] = "text"
    text: str


class ImageContent(_FrozenModel):
    type: Literal["image"] = "image"
    mime_type: str = "image/png"
    url: str | None = None
    data_base64: str | None = None

    @model_validator(mode="after")
    def exactly_one_source(self) -> ImageContent:
        if (self.url is None) == (self.data_base64 is None):
            raise ValueError("image content requires exactly one of url or data_base64")
        if self.url is not None:
            parsed = urlparse(self.url)
            if parsed.scheme not in {"https", "data"}:
                raise ValueError("image url must use https or data")
            if parsed.scheme == "https" and (
                not parsed.netloc
                or parsed.username is not None
                or parsed.password is not None
            ):
                raise ValueError("https image urls must not contain user information")
        return self


class DocumentContent(_FrozenModel):
    type: Literal["document"] = "document"
    mime_type: str
    name: str = "document"
    data_base64: str


ContentPart = Annotated[
    TextContent | ImageContent | DocumentContent,
    Field(discriminator="type"),
]


class ModelToolCall(_FrozenModel):
    call_id: str
    name: str
    arguments: dict[str, JsonValue] = Field(default_factory=dict)

    @field_validator("call_id")
    @classmethod
    def valid_call_id(cls, value: str) -> str:
        if not _ID.fullmatch(value):
            raise ValueError("invalid model tool call id")
        return value

    @field_validator("name")
    @classmethod
    def valid_name(cls, value: str) -> str:
        if not _TOOL_NAME.fullmatch(value):
            raise ValueError("invalid model tool name")
        return value


class ModelMessage(_FrozenModel):
    role: ModelRole
    content: tuple[ContentPart, ...] = ()
    tool_calls: tuple[ModelToolCall, ...] = ()
    tool_call_id: str | None = None
    name: str | None = None

    @model_validator(mode="after")
    def role_shape(self) -> ModelMessage:
        if self.role is ModelRole.TOOL and not self.tool_call_id:
            raise ValueError("tool messages require tool_call_id")
        if self.role is not ModelRole.TOOL and self.tool_call_id is not None:
            raise ValueError("tool_call_id is only valid for tool messages")
        if self.tool_calls and self.role is not ModelRole.ASSISTANT:
            raise ValueError("only assistant messages may contain tool calls")
        if not self.content and not self.tool_calls:
            raise ValueError("a message requires content or tool calls")
        return self

    @classmethod
    def text(cls, role: ModelRole, value: str, **kwargs: str) -> ModelMessage:
        return cls(role=role, content=(TextContent(text=value),), **kwargs)


class ModelTool(_FrozenModel):
    name: str
    description: str = ""
    input_schema: dict[str, JsonValue] = Field(default_factory=dict)

    @field_validator("name")
    @classmethod
    def valid_name(cls, value: str) -> str:
        if not _TOOL_NAME.fullmatch(value):
            raise ValueError("invalid model tool name")
        return value


class ModelRequest(_FrozenModel):
    messages: tuple[ModelMessage, ...]
    model: str | None = None
    tools: tuple[ModelTool, ...] = ()
    tool_choice: Literal["auto", "none", "required"] = "auto"
    temperature: float | None = Field(default=None, ge=0.0, le=2.0)
    top_p: float | None = Field(default=None, gt=0.0, le=1.0)
    max_output_tokens: int = Field(default=4096, ge=1, le=1_000_000)
    stop: tuple[str, ...] = ()
    response_schema: dict[str, JsonValue] | None = None
    metadata: dict[str, JsonValue] = Field(default_factory=dict)

    @field_validator("model")
    @classmethod
    def valid_model(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("model must not be blank")
        return value

    @model_validator(mode="after")
    def request_shape(self) -> ModelRequest:
        if not self.messages:
            raise ValueError("a model request requires at least one message")
        names = [item.name for item in self.tools]
        if len(names) != len(set(names)):
            raise ValueError("model tool names must be unique")
        if self.tool_choice == "required" and not self.tools:
            raise ValueError("required tool choice needs at least one tool")
        for value in (self.temperature, self.top_p):
            if value is not None and not math.isfinite(value):
                raise ValueError("sampling values must be finite")
        return self


class ModelUsage(_FrozenModel):
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    cached_input_tokens: int = Field(default=0, ge=0)
    reasoning_tokens: int = Field(default=0, ge=0)

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens


class ModelResponse(_FrozenModel):
    response_id: str
    provider_id: str
    model: str
    content: tuple[ContentPart, ...] = ()
    tool_calls: tuple[ModelToolCall, ...] = ()
    finish_reason: FinishReason = FinishReason.UNKNOWN
    usage: ModelUsage = Field(default_factory=ModelUsage)
    provider_metadata: dict[str, JsonValue] = Field(default_factory=dict)

    @field_validator("response_id", "provider_id")
    @classmethod
    def valid_ids(cls, value: str) -> str:
        if not _ID.fullmatch(value):
            raise ValueError("invalid response/provider id")
        return value


class ModelStreamEvent(_FrozenModel):
    type: ModelStreamEventType
    response_id: str | None = None
    text_delta: str | None = None
    reasoning_delta: str | None = None
    tool_call: ModelToolCall | None = None
    usage: ModelUsage | None = None
    finish_reason: FinishReason | None = None

    @model_validator(mode="after")
    def event_payload(self) -> ModelStreamEvent:
        expected = {
            ModelStreamEventType.TEXT_DELTA: self.text_delta,
            ModelStreamEventType.REASONING_DELTA: self.reasoning_delta,
            ModelStreamEventType.TOOL_CALL_DELTA: self.tool_call,
            ModelStreamEventType.USAGE: self.usage,
            ModelStreamEventType.MESSAGE_END: self.finish_reason,
        }
        if self.type in expected and expected[self.type] is None:
            raise ValueError(f"{self.type.value} event is missing its payload")
        return self


class ProviderCapabilities(_FrozenModel):
    streaming: bool = True
    tools: bool = True
    parallel_tool_calls: bool = True
    json_schema: bool = False
    reasoning: bool = False
    vision: bool = False
    documents: bool = False
    max_context_tokens: int | None = Field(default=None, ge=1)


class ProviderDescriptor(_FrozenModel):
    provider_id: str
    display_name: str
    adapter: AdapterKind
    auth: ProviderAuthKind
    default_base_url: str | None = None
    aliases: frozenset[str] = frozenset()
    capabilities: ProviderCapabilities = Field(default_factory=ProviderCapabilities)
    server_only: Literal[True] = True

    @field_validator("provider_id")
    @classmethod
    def valid_provider_id(cls, value: str) -> str:
        if not _ID.fullmatch(value):
            raise ValueError("invalid provider id")
        return value

    @field_validator("default_base_url")
    @classmethod
    def valid_url(cls, value: str | None) -> str | None:
        if value is None:
            return None
        parsed = urlparse(value)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.netloc
            or parsed.username is not None
            or parsed.password is not None
        ):
            raise ValueError("provider base URL must be an absolute HTTP(S) URL")
        return value.rstrip("/")


class ModelProfile(_FrozenModel):
    profile_id: str
    provider_id: str
    model: str
    credential_ref: str | None = None
    base_url: str | None = None
    timeout_seconds: float = Field(default=120.0, gt=0.0, le=3600.0)
    max_retries: int = Field(default=2, ge=0, le=10)
    allowed_models: frozenset[str] = frozenset()
    default_headers: dict[str, str] = Field(default_factory=dict)
    provider_options: dict[str, JsonValue] = Field(default_factory=dict)
    allow_plain_http: bool = False

    @field_validator("profile_id", "provider_id")
    @classmethod
    def valid_ids(cls, value: str) -> str:
        if not _ID.fullmatch(value):
            raise ValueError("invalid model profile/provider id")
        return value

    @field_validator("model")
    @classmethod
    def non_empty_model(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("profile model must not be blank")
        return value

    @field_validator("allowed_models")
    @classmethod
    def non_empty_allowed_models(cls, value: frozenset[str]) -> frozenset[str]:
        if any(not model.strip() for model in value):
            raise ValueError("allowed_models must not contain blank model names")
        return value

    @model_validator(mode="after")
    def endpoint_security(self) -> ModelProfile:
        if self.base_url is None:
            return self
        parsed = urlparse(self.base_url)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.netloc
            or parsed.username is not None
            or parsed.password is not None
        ):
            raise ValueError("profile base URL must be an absolute HTTP(S) URL")
        if parsed.scheme == "http" and not self.allow_plain_http:
            raise ValueError("plain HTTP model endpoints require allow_plain_http=true")
        return self

    def permits_model(self, model: str) -> bool:
        return model == self.model or model in self.allowed_models


class ModelRoute(_FrozenModel):
    route_id: str
    primary_profile: str
    fallback_profiles: tuple[str, ...] = ()

    @model_validator(mode="after")
    def unique_profiles(self) -> ModelRoute:
        values = (self.primary_profile, *self.fallback_profiles)
        if len(values) != len(set(values)):
            raise ValueError("model route profiles must be unique")
        return self
