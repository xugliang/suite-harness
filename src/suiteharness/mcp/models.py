"""Typed MCP 2025-11-25 vocabulary and deployment configuration.

The module intentionally contains only provider-neutral data. Transport,
authentication, model sampling and user elicitation are injected by the host.
Unknown extension payloads remain possible through ``experimental`` fields,
while security-sensitive configuration stays strict and closed by default.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path
from typing import Annotated, Literal
from urllib.parse import urlparse

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    SecretStr,
    field_validator,
    model_validator,
)

LATEST_PROTOCOL_VERSION = "2025-11-25"

_ID = re.compile(r"^[a-z][a-z0-9]*(?:[._-][a-z0-9]+)*$")
_METHOD = re.compile(r"^[A-Za-z][A-Za-z0-9_./-]{0,255}$")


class _FrozenModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        # MCP is a JSON wire protocol: JSON arrays must validate into immutable
        # tuples and string enum values into their typed enum members.
        strict=False,
        populate_by_name=True,
    )


class _ExtensibleModel(_FrozenModel):
    model_config = ConfigDict(extra="allow", frozen=True, strict=False, populate_by_name=True)


class McpErrorCode(int, Enum):
    PARSE_ERROR = -32700
    INVALID_REQUEST = -32600
    METHOD_NOT_FOUND = -32601
    INVALID_PARAMS = -32602
    INTERNAL_ERROR = -32603


class McpProtocolError(RuntimeError):
    """A remote peer or local transport violated the MCP/JSON-RPC contract."""

    def __init__(
        self,
        message: str,
        *,
        code: int = McpErrorCode.INTERNAL_ERROR,
        data: JsonValue | None = None,
    ) -> None:
        super().__init__(message)
        self.code = int(code)
        self.data = data


class McpCapabilityError(McpProtocolError):
    """A method was attempted without a negotiated capability."""


class McpSecurityError(PermissionError):
    """A deployment or call attempted to cross a trusted boundary."""


class McpTransportError(ConnectionError):
    """A bounded MCP transport failed or became unavailable."""


JsonRpcId = str | int


def _validate_method(value: str) -> str:
    if not _METHOD.fullmatch(value):
        raise ValueError(f"invalid JSON-RPC method: {value!r}")
    return value


class JsonRpcErrorObject(_FrozenModel):
    code: int
    message: str
    data: JsonValue | None = None

    @field_validator("message")
    @classmethod
    def nonblank_message(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("JSON-RPC error message must not be blank")
        return value


class JsonRpcRequest(_FrozenModel):
    jsonrpc: Literal["2.0"] = "2.0"
    id: JsonRpcId
    method: str
    params: dict[str, JsonValue] | list[JsonValue] | None = None

    @field_validator("id", mode="before")
    @classmethod
    def valid_id(cls, value: JsonRpcId) -> JsonRpcId:
        if isinstance(value, bool):
            raise ValueError("JSON-RPC id must not be boolean")
        if isinstance(value, str) and not value:
            raise ValueError("JSON-RPC string id must not be empty")
        return value

    @field_validator("method")
    @classmethod
    def valid_method(cls, value: str) -> str:
        return _validate_method(value)


class JsonRpcNotification(_FrozenModel):
    jsonrpc: Literal["2.0"] = "2.0"
    method: str
    params: dict[str, JsonValue] | list[JsonValue] | None = None

    @field_validator("method")
    @classmethod
    def valid_method(cls, value: str) -> str:
        return _validate_method(value)


class JsonRpcResponse(_FrozenModel):
    jsonrpc: Literal["2.0"] = "2.0"
    id: JsonRpcId
    result: JsonValue | None = None
    error: JsonRpcErrorObject | None = None

    @field_validator("id", mode="before")
    @classmethod
    def valid_id(cls, value: JsonRpcId) -> JsonRpcId:
        if isinstance(value, bool):
            raise ValueError("JSON-RPC id must not be boolean")
        return value

    @model_validator(mode="after")
    def exactly_one_outcome(self) -> JsonRpcResponse:
        result_set = "result" in self.model_fields_set
        error_set = "error" in self.model_fields_set and self.error is not None
        if result_set == error_set:
            raise ValueError("JSON-RPC response requires exactly one of result or error")
        return self


JsonRpcMessage = Annotated[
    JsonRpcRequest | JsonRpcNotification | JsonRpcResponse,
    Field(union_mode="left_to_right"),
]


class Implementation(_FrozenModel):
    name: str
    version: str
    title: str | None = None
    description: str | None = None

    @field_validator("name", "version")
    @classmethod
    def nonblank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("implementation name/version must not be blank")
        return value


class RootsCapability(_FrozenModel):
    list_changed: bool = Field(default=False, alias="listChanged")


class EmptyCapability(_FrozenModel):
    """Presence-only MCP capability encoded as an empty JSON object."""


class SamplingCapability(_FrozenModel):
    tools: EmptyCapability | None = None
    context: EmptyCapability | None = None

    @field_validator("tools", "context", mode="before")
    @classmethod
    def legacy_boolean_marker(cls, value: object) -> object:
        if value is True:
            return {}
        if value is False:
            return None
        return value


class ElicitationCapability(_FrozenModel):
    form: EmptyCapability | None = None
    url: EmptyCapability | None = None

    @field_validator("form", "url", mode="before")
    @classmethod
    def legacy_boolean_marker(cls, value: object) -> object:
        if value is True:
            return {}
        if value is False:
            return None
        return value


class SamplingTaskRequests(_FrozenModel):
    create_message: EmptyCapability | None = Field(default=None, alias="createMessage")


class ElicitationTaskRequests(_FrozenModel):
    create: EmptyCapability | None = None


class ClientTaskRequests(_FrozenModel):
    sampling: SamplingTaskRequests | None = None
    elicitation: ElicitationTaskRequests | None = None


class TaskClientCapability(_FrozenModel):
    list: EmptyCapability | None = None
    cancel: EmptyCapability | None = None
    requests: ClientTaskRequests | None = None

    @field_validator("list", "cancel", mode="before")
    @classmethod
    def legacy_boolean_marker(cls, value: object) -> object:
        if value is True:
            return {}
        if value is False:
            return None
        return value

    @field_validator("requests", mode="before")
    @classmethod
    def legacy_requests_marker(cls, value: object) -> object:
        if value is True:
            return {
                "sampling": {"createMessage": {}},
                "elicitation": {"create": {}},
            }
        if value is False:
            return None
        return value


class ClientCapabilities(_ExtensibleModel):
    roots: RootsCapability | None = None
    sampling: SamplingCapability | None = None
    elicitation: ElicitationCapability | None = None
    tasks: TaskClientCapability | None = None
    experimental: dict[str, JsonValue] = Field(default_factory=dict)
    extensions: dict[str, JsonValue] = Field(default_factory=dict)


class ListChangedCapability(_FrozenModel):
    list_changed: bool = Field(default=False, alias="listChanged")


class ResourcesCapability(ListChangedCapability):
    subscribe: bool = False


class ToolTaskRequests(_FrozenModel):
    call: EmptyCapability | None = None


class ServerTaskRequests(_FrozenModel):
    tools: ToolTaskRequests | None = None


class TaskServerCapability(_FrozenModel):
    list: EmptyCapability | None = None
    cancel: EmptyCapability | None = None
    requests: ServerTaskRequests | None = None

    @field_validator("list", "cancel", mode="before")
    @classmethod
    def legacy_boolean_marker(cls, value: object) -> object:
        if value is True:
            return {}
        if value is False:
            return None
        return value

    @field_validator("requests", mode="before")
    @classmethod
    def normalize_requests(cls, value: object) -> object:
        if value is True:
            return {"tools": {"call": {}}}
        if value is False:
            return None
        if isinstance(value, dict) and "tools/call" in value:
            return {"tools": {"call": value["tools/call"]}}
        return value


class ServerCapabilities(_ExtensibleModel):
    logging: dict[str, JsonValue] | None = None
    prompts: ListChangedCapability | None = None
    resources: ResourcesCapability | None = None
    tools: ListChangedCapability | None = None
    completions: dict[str, JsonValue] | None = None
    tasks: TaskServerCapability | None = None
    experimental: dict[str, JsonValue] = Field(default_factory=dict)
    extensions: dict[str, JsonValue] = Field(default_factory=dict)


class InitializeParams(_FrozenModel):
    protocol_version: str = Field(alias="protocolVersion")
    capabilities: ClientCapabilities
    client_info: Implementation = Field(alias="clientInfo")


class InitializeResult(_FrozenModel):
    protocol_version: str = Field(alias="protocolVersion")
    capabilities: ServerCapabilities
    server_info: Implementation = Field(alias="serverInfo")
    instructions: str | None = None


class PaginatedRequest(_FrozenModel):
    cursor: str | None = None


class PaginatedResult(_FrozenModel):
    next_cursor: str | None = Field(default=None, alias="nextCursor")


class Annotations(_FrozenModel):
    audience: tuple[Literal["user", "assistant"], ...] = ()
    priority: float | None = Field(default=None, ge=0.0, le=1.0)
    last_modified: str | None = Field(default=None, alias="lastModified")


class Icon(_FrozenModel):
    src: str
    mime_type: str | None = Field(default=None, alias="mimeType")
    sizes: tuple[str, ...] = ()
    theme: Literal["light", "dark"] | None = None


class ToolAnnotations(_FrozenModel):
    title: str | None = None
    read_only_hint: bool | None = Field(default=None, alias="readOnlyHint")
    destructive_hint: bool | None = Field(default=None, alias="destructiveHint")
    idempotent_hint: bool | None = Field(default=None, alias="idempotentHint")
    open_world_hint: bool | None = Field(default=None, alias="openWorldHint")


class McpTool(_FrozenModel):
    name: str
    title: str | None = None
    description: str = ""
    input_schema: dict[str, JsonValue] = Field(alias="inputSchema")
    output_schema: dict[str, JsonValue] | None = Field(default=None, alias="outputSchema")
    annotations: ToolAnnotations | None = None
    icons: tuple[Icon, ...] = ()
    meta: dict[str, JsonValue] = Field(default_factory=dict, alias="_meta")

    @field_validator("name")
    @classmethod
    def nonblank_name(cls, value: str) -> str:
        if not value.strip() or len(value) > 256:
            raise ValueError("MCP tool name must contain 1-256 characters")
        return value


class ListToolsResult(PaginatedResult):
    tools: tuple[McpTool, ...]


class TaskMetadata(_FrozenModel):
    """Request for asynchronous task execution and retention in milliseconds."""

    ttl: int | None = Field(default=None, ge=0)


class TextContent(_FrozenModel):
    type: Literal["text"] = "text"
    text: str
    annotations: Annotations | None = None
    meta: dict[str, JsonValue] = Field(default_factory=dict, alias="_meta")


class ImageContent(_FrozenModel):
    type: Literal["image"] = "image"
    data: str
    mime_type: str = Field(alias="mimeType")
    annotations: Annotations | None = None
    meta: dict[str, JsonValue] = Field(default_factory=dict, alias="_meta")


class AudioContent(_FrozenModel):
    type: Literal["audio"] = "audio"
    data: str
    mime_type: str = Field(alias="mimeType")
    annotations: Annotations | None = None
    meta: dict[str, JsonValue] = Field(default_factory=dict, alias="_meta")


class ResourceLink(_FrozenModel):
    type: Literal["resource_link"] = "resource_link"
    uri: str
    name: str
    title: str | None = None
    description: str | None = None
    mime_type: str | None = Field(default=None, alias="mimeType")
    size: int | None = Field(default=None, ge=0)
    annotations: Annotations | None = None
    icons: tuple[Icon, ...] = ()
    meta: dict[str, JsonValue] = Field(default_factory=dict, alias="_meta")


class TextResourceContents(_FrozenModel):
    uri: str
    mime_type: str | None = Field(default=None, alias="mimeType")
    text: str
    meta: dict[str, JsonValue] = Field(default_factory=dict, alias="_meta")


class BlobResourceContents(_FrozenModel):
    uri: str
    mime_type: str | None = Field(default=None, alias="mimeType")
    blob: str
    meta: dict[str, JsonValue] = Field(default_factory=dict, alias="_meta")


ResourceContents = TextResourceContents | BlobResourceContents


class EmbeddedResource(_FrozenModel):
    type: Literal["resource"] = "resource"
    resource: ResourceContents
    annotations: Annotations | None = None
    meta: dict[str, JsonValue] = Field(default_factory=dict, alias="_meta")


class ToolUseContent(_FrozenModel):
    type: Literal["tool_use"] = "tool_use"
    id: str
    name: str
    input: dict[str, JsonValue] = Field(default_factory=dict)
    meta: dict[str, JsonValue] = Field(default_factory=dict, alias="_meta")


class ToolResultContent(_FrozenModel):
    type: Literal["tool_result"] = "tool_result"
    tool_use_id: str = Field(alias="toolUseId")
    content: tuple[TextContent | ImageContent | AudioContent | ResourceLink | EmbeddedResource, ...]
    structured_content: dict[str, JsonValue] | None = Field(
        default=None, alias="structuredContent"
    )
    is_error: bool = Field(default=False, alias="isError")
    meta: dict[str, JsonValue] = Field(default_factory=dict, alias="_meta")


ContentBlock = Annotated[
    TextContent
    | ImageContent
    | AudioContent
    | ResourceLink
    | EmbeddedResource,
    Field(discriminator="type"),
]

SamplingContent = Annotated[
    TextContent | ImageContent | AudioContent | ToolUseContent | ToolResultContent,
    Field(discriminator="type"),
]

# Backwards-compatible public name for ordinary tool/resource content. Sampling
# uses the narrower SamplingContent union defined above.
McpContent = ContentBlock


class CallToolParams(_FrozenModel):
    name: str
    arguments: dict[str, JsonValue] = Field(default_factory=dict)
    task: TaskMetadata | None = None


class CallToolResult(_FrozenModel):
    content: tuple[ContentBlock, ...]
    structured_content: dict[str, JsonValue] | None = Field(
        default=None, alias="structuredContent"
    )
    is_error: bool = Field(default=False, alias="isError")
    meta: dict[str, JsonValue] = Field(default_factory=dict, alias="_meta")


class McpResource(_FrozenModel):
    uri: str
    name: str
    title: str | None = None
    description: str | None = None
    mime_type: str | None = Field(default=None, alias="mimeType")
    size: int | None = Field(default=None, ge=0)
    annotations: Annotations | None = None
    icons: tuple[Icon, ...] = ()
    meta: dict[str, JsonValue] = Field(default_factory=dict, alias="_meta")


class ResourceTemplate(_FrozenModel):
    uri_template: str = Field(alias="uriTemplate")
    name: str
    title: str | None = None
    description: str | None = None
    mime_type: str | None = Field(default=None, alias="mimeType")
    annotations: Annotations | None = None
    icons: tuple[Icon, ...] = ()


class ListResourcesResult(PaginatedResult):
    resources: tuple[McpResource, ...]


class ListResourceTemplatesResult(PaginatedResult):
    resource_templates: tuple[ResourceTemplate, ...] = Field(alias="resourceTemplates")


class ReadResourceResult(_FrozenModel):
    contents: tuple[ResourceContents, ...]


class PromptArgument(_FrozenModel):
    name: str
    description: str | None = None
    required: bool = False


class McpPrompt(_FrozenModel):
    name: str
    title: str | None = None
    description: str | None = None
    arguments: tuple[PromptArgument, ...] = ()
    icons: tuple[Icon, ...] = ()
    meta: dict[str, JsonValue] = Field(default_factory=dict, alias="_meta")


class ListPromptsResult(PaginatedResult):
    prompts: tuple[McpPrompt, ...]


class PromptMessage(_FrozenModel):
    role: Literal["user", "assistant"]
    content: ContentBlock


class GetPromptResult(_FrozenModel):
    description: str | None = None
    messages: tuple[PromptMessage, ...]


class CompletionReference(_FrozenModel):
    type: Literal["ref/prompt", "ref/resource"]
    name: str | None = None
    uri: str | None = None

    @model_validator(mode="after")
    def reference_shape(self) -> CompletionReference:
        if self.type == "ref/prompt" and not self.name:
            raise ValueError("prompt completion reference requires name")
        if self.type == "ref/resource" and not self.uri:
            raise ValueError("resource completion reference requires uri")
        return self


class CompletionArgument(_FrozenModel):
    name: str
    value: str


class CompletionContext(_FrozenModel):
    arguments: dict[str, str] = Field(default_factory=dict)


class CompleteParams(_FrozenModel):
    ref: CompletionReference
    argument: CompletionArgument
    context: CompletionContext | None = None


class CompletionValues(_FrozenModel):
    values: tuple[str, ...] = Field(max_length=100)
    total: int | None = Field(default=None, ge=0)
    has_more: bool | None = Field(default=None, alias="hasMore")


class CompleteResult(_FrozenModel):
    completion: CompletionValues


class LoggingLevel(str, Enum):
    DEBUG = "debug"
    INFO = "info"
    NOTICE = "notice"
    WARNING = "warning"
    ERROR = "error"
    CRITICAL = "critical"
    ALERT = "alert"
    EMERGENCY = "emergency"


class LoggingMessage(_FrozenModel):
    level: LoggingLevel
    data: JsonValue
    logger: str | None = None


class McpRoot(_FrozenModel):
    uri: str
    name: str | None = None

    @field_validator("uri")
    @classmethod
    def file_uri_only(cls, value: str) -> str:
        parsed = urlparse(value)
        if parsed.scheme != "file":
            raise ValueError("MCP roots must use file URIs")
        return value


class ListRootsResult(_FrozenModel):
    roots: tuple[McpRoot, ...]


class SamplingTool(_FrozenModel):
    name: str
    description: str = ""
    input_schema: dict[str, JsonValue] = Field(alias="inputSchema")


class SamplingMessage(_FrozenModel):
    role: Literal["user", "assistant"]
    content: SamplingContent | tuple[SamplingContent, ...]


class ModelPreferences(_FrozenModel):
    hints: tuple[dict[str, JsonValue], ...] = ()
    cost_priority: float | None = Field(default=None, alias="costPriority", ge=0.0, le=1.0)
    speed_priority: float | None = Field(default=None, alias="speedPriority", ge=0.0, le=1.0)
    intelligence_priority: float | None = Field(
        default=None, alias="intelligencePriority", ge=0.0, le=1.0
    )


class CreateMessageParams(_FrozenModel):
    messages: tuple[SamplingMessage, ...]
    max_tokens: int = Field(alias="maxTokens", ge=1)
    system_prompt: str | None = Field(default=None, alias="systemPrompt")
    include_context: Literal["none", "thisServer", "allServers"] | None = Field(
        default=None, alias="includeContext"
    )
    temperature: float | None = Field(default=None, ge=0.0, le=2.0)
    stop_sequences: tuple[str, ...] = Field(default=(), alias="stopSequences")
    model_preferences: ModelPreferences | None = Field(default=None, alias="modelPreferences")
    tools: tuple[SamplingTool, ...] = ()
    tool_choice: dict[str, JsonValue] | None = Field(default=None, alias="toolChoice")
    metadata: dict[str, JsonValue] = Field(default_factory=dict)
    task: TaskMetadata | None = None


class CreateMessageResult(_FrozenModel):
    role: Literal["assistant"] = "assistant"
    content: SamplingContent | tuple[SamplingContent, ...]
    model: str
    stop_reason: str | None = Field(default=None, alias="stopReason")


class ElicitationMode(str, Enum):
    FORM = "form"
    URL = "url"


class ElicitationStringSchema(_FrozenModel):
    type: Literal["string"] = "string"
    title: str | None = None
    description: str | None = None
    min_length: int | None = Field(default=None, alias="minLength", ge=0)
    max_length: int | None = Field(default=None, alias="maxLength", ge=0)
    format: Literal["email", "uri", "date", "date-time"] | None = None
    enum: tuple[str, ...] | None = None
    one_of: tuple[dict[str, str], ...] | None = Field(default=None, alias="oneOf")
    enum_names: tuple[str, ...] | None = Field(default=None, alias="enumNames")
    default: str | None = None

    @model_validator(mode="after")
    def valid_enum(self) -> ElicitationStringSchema:
        if self.enum is not None and not self.enum:
            raise ValueError("elicitation enum must not be empty")
        if self.one_of is not None:
            if not self.one_of or any(set(item) != {"const", "title"} for item in self.one_of):
                raise ValueError("elicitation oneOf requires const/title entries")
        if self.enum is not None and self.one_of is not None:
            raise ValueError("elicitation enum and oneOf are mutually exclusive")
        if self.enum_names is not None:
            if self.enum is None or len(self.enum_names) != len(self.enum):
                raise ValueError("enumNames must align with enum")
        if (
            self.min_length is not None
            and self.max_length is not None
            and self.min_length > self.max_length
        ):
            raise ValueError("minLength must not exceed maxLength")
        return self


class ElicitationNumberSchema(_FrozenModel):
    type: Literal["number", "integer"]
    title: str | None = None
    description: str | None = None
    minimum: float | None = None
    maximum: float | None = None
    default: float | int | None = None

    @model_validator(mode="after")
    def valid_range(self) -> ElicitationNumberSchema:
        if self.minimum is not None and self.maximum is not None and self.minimum > self.maximum:
            raise ValueError("minimum must not exceed maximum")
        return self


class ElicitationBooleanSchema(_FrozenModel):
    type: Literal["boolean"] = "boolean"
    title: str | None = None
    description: str | None = None
    default: bool | None = None


class ElicitationMultiSelectItems(_FrozenModel):
    type: Literal["string"] | None = None
    enum: tuple[str, ...] | None = None
    any_of: tuple[dict[str, str], ...] | None = Field(default=None, alias="anyOf")

    @model_validator(mode="after")
    def one_enum_form(self) -> ElicitationMultiSelectItems:
        if (self.enum is None) == (self.any_of is None):
            raise ValueError("multi-select items require exactly one of enum or anyOf")
        if self.enum is not None and (self.type != "string" or not self.enum):
            raise ValueError("multi-select enum items require type=string and values")
        if self.any_of is not None and (
            not self.any_of or any(set(item) != {"const", "title"} for item in self.any_of)
        ):
            raise ValueError("multi-select anyOf requires const/title entries")
        return self


class ElicitationMultiSelectSchema(_FrozenModel):
    type: Literal["array"] = "array"
    title: str | None = None
    description: str | None = None
    min_items: int | None = Field(default=None, alias="minItems", ge=0)
    max_items: int | None = Field(default=None, alias="maxItems", ge=0)
    items: ElicitationMultiSelectItems
    default: tuple[str, ...] | None = None

    @model_validator(mode="after")
    def valid_size(self) -> ElicitationMultiSelectSchema:
        if self.min_items is not None and self.max_items is not None and self.min_items > self.max_items:
            raise ValueError("minItems must not exceed maxItems")
        return self


ElicitationPrimitiveSchema = (
    ElicitationStringSchema
    | ElicitationNumberSchema
    | ElicitationBooleanSchema
    | ElicitationMultiSelectSchema
)


class ElicitationFormSchema(_FrozenModel):
    schema_uri: str | None = Field(default=None, alias="$schema")
    type: Literal["object"] = "object"
    properties: dict[str, ElicitationPrimitiveSchema]
    required: tuple[str, ...] = ()

    @model_validator(mode="after")
    def valid_required(self) -> ElicitationFormSchema:
        unknown = set(self.required).difference(self.properties)
        if unknown:
            raise ValueError(f"required elicitation fields are not properties: {sorted(unknown)!r}")
        return self


class ElicitationParams(_FrozenModel):
    mode: ElicitationMode = ElicitationMode.FORM
    message: str
    requested_schema: ElicitationFormSchema | None = Field(
        default=None, alias="requestedSchema"
    )
    url: str | None = None
    elicitation_id: str | None = Field(default=None, alias="elicitationId")
    task: TaskMetadata | None = None

    @model_validator(mode="after")
    def mode_shape(self) -> ElicitationParams:
        if self.mode is ElicitationMode.FORM:
            if self.requested_schema is None or self.url is not None or self.elicitation_id is not None:
                raise ValueError("form elicitation requires only requestedSchema")
        elif self.url is None or self.requested_schema is not None or not self.elicitation_id:
            raise ValueError("URL elicitation requires url and elicitationId")
        if self.url is not None:
            parsed = urlparse(self.url)
            if (
                parsed.scheme != "https"
                or not parsed.netloc
                or parsed.username is not None
                or parsed.password is not None
            ):
                raise ValueError("elicitation URL must be absolute HTTPS")
        return self


class ElicitationResult(_FrozenModel):
    action: Literal["accept", "decline", "cancel"]
    content: dict[str, str | int | float | bool | tuple[str, ...]] | None = None

    @model_validator(mode="after")
    def accepted_content(self) -> ElicitationResult:
        if self.action != "accept" and self.content is not None:
            raise ValueError("declined/cancelled elicitation cannot contain content")
        return self


class TaskStatus(str, Enum):
    WORKING = "working"
    INPUT_REQUIRED = "input_required"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class McpTask(_FrozenModel):
    task_id: str = Field(alias="taskId")
    status: TaskStatus
    status_message: str | None = Field(default=None, alias="statusMessage")
    created_at: datetime = Field(alias="createdAt")
    last_updated_at: datetime = Field(alias="lastUpdatedAt")
    ttl: int | None
    poll_interval: int | None = Field(default=None, alias="pollInterval", ge=0)

    @field_validator("created_at", "last_updated_at")
    @classmethod
    def aware_timestamp(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("MCP task timestamps must be timezone-aware")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def chronological(self) -> McpTask:
        if self.last_updated_at < self.created_at:
            raise ValueError("task lastUpdatedAt must not precede createdAt")
        return self


class CreateTaskResult(_FrozenModel):
    task: McpTask


GetTaskResult = McpTask


class ListTasksResult(PaginatedResult):
    tasks: tuple[McpTask, ...]


class ProgressNotification(_FrozenModel):
    progress_token: str | int = Field(alias="progressToken")
    progress: float = Field(ge=0.0)
    total: float | None = Field(default=None, ge=0.0)
    message: str | None = None

    @model_validator(mode="after")
    def bounded_progress(self) -> ProgressNotification:
        if self.total is not None and self.progress > self.total:
            raise ValueError("progress must not exceed total")
        return self


class CancelledNotification(_FrozenModel):
    request_id: JsonRpcId = Field(alias="requestId")
    reason: str | None = None

    @field_validator("request_id", mode="before")
    @classmethod
    def nonboolean_id(cls, value: object) -> object:
        if isinstance(value, bool):
            raise ValueError("requestId must not be boolean")
        return value


class ResourceUpdatedNotification(_FrozenModel):
    uri: str


class McpTransportKind(str, Enum):
    STDIO = "stdio"
    STREAMABLE_HTTP = "streamable_http"


class LegacySseCompatibility(str, Enum):
    DISABLED = "disabled"
    ADAPTER_REQUIRED = "adapter_required"


class McpFeatureFlags(_FrozenModel):
    experimental_tasks: bool = False
    url_elicitation: bool = False


class McpServerConfig(_FrozenModel):
    """Administrator-controlled MCP server declaration without secret values."""

    server_id: str
    transport: McpTransportKind
    command: str | None = None
    arguments: tuple[str, ...] = ()
    endpoint: str | None = None
    credential_ref: str | None = None
    enabled: bool = True
    timeout_seconds: float = Field(default=60.0, gt=0.0, le=3600.0)
    discovery_timeout_seconds: float = Field(default=120.0, gt=0.0, le=600.0)
    max_message_bytes: int = Field(default=4_194_304, ge=1_024, le=67_108_864)
    max_stream_events: int = Field(default=256, ge=1, le=10_000)
    max_list_pages: int = Field(default=100, ge=1, le=1_000)
    max_list_items: int = Field(default=10_000, ge=1, le=100_000)
    require_production_sandbox: bool = True
    resume_sessions: bool = True
    legacy_sse: LegacySseCompatibility = LegacySseCompatibility.DISABLED
    protocol_versions: tuple[str, ...] = (LATEST_PROTOCOL_VERSION,)
    features: McpFeatureFlags = Field(default_factory=McpFeatureFlags)

    @field_validator(
        "max_message_bytes",
        "max_stream_events",
        "max_list_pages",
        "max_list_items",
        mode="before",
    )
    @classmethod
    def integer_transport_limits(cls, value: object) -> object:
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError("MCP transport limits must be integers")
        return value

    @field_validator("server_id")
    @classmethod
    def valid_server_id(cls, value: str) -> str:
        if not _ID.fullmatch(value):
            raise ValueError("server_id must be a lowercase deployment identifier")
        return value

    @field_validator("credential_ref")
    @classmethod
    def valid_credential_ref(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("credential_ref must not be blank")
        return value

    @model_validator(mode="after")
    def transport_shape(self) -> McpServerConfig:
        if not self.protocol_versions or self.protocol_versions[0] != LATEST_PROTOCOL_VERSION:
            raise ValueError("protocol_versions must prefer MCP 2025-11-25")
        if self.transport is McpTransportKind.STDIO:
            if not self.command or self.endpoint is not None or self.credential_ref is not None:
                raise ValueError("stdio requires command and forbids endpoint/credential_ref")
            if "\x00" in self.command or any("\x00" in item for item in self.arguments):
                raise ValueError("stdio command arguments must not contain NUL")
        else:
            if self.command is not None or self.arguments:
                raise ValueError("Streamable HTTP forbids command/arguments")
            if self.endpoint is None:
                raise ValueError("Streamable HTTP requires endpoint")
            parsed = urlparse(self.endpoint)
            if (
                parsed.scheme != "https"
                or not parsed.netloc
                or parsed.username is not None
                or parsed.password is not None
            ):
                raise ValueError("MCP HTTP endpoint must be an absolute HTTPS URL")
        return self


class McpConnectionState(str, Enum):
    DISCONNECTED = "disconnected"
    INITIALIZING = "initializing"
    READY = "ready"
    DEGRADED = "degraded"
    CLOSED = "closed"


class McpServerHealth(_FrozenModel):
    server_id: str
    state: McpConnectionState
    negotiated_version: str | None = None
    server_info: Implementation | None = None
    detail: str = ""


class HttpResumeState(_FrozenModel):
    session_id: str | None = None
    last_event_id: str | None = None

    @field_validator("session_id", "last_event_id")
    @classmethod
    def safe_header_value(cls, value: str | None) -> str | None:
        if value is not None and (not value or len(value) > 4096 or "\r" in value or "\n" in value):
            raise ValueError("HTTP resumption values must be non-empty and header-safe")
        return value


def _https_url(value: str, label: str) -> str:
    parsed = urlparse(value)
    if (
        parsed.scheme != "https"
        or not parsed.netloc
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise ValueError(f"{label} must be an absolute HTTPS URL")
    return value


class OAuthProtectedResourceMetadata(_ExtensibleModel):
    """RFC 9728 protected-resource metadata used by MCP authorization."""

    resource: str
    authorization_servers: tuple[str, ...] = ()
    jwks_uri: str | None = None
    scopes_supported: tuple[str, ...] = ()
    bearer_methods_supported: tuple[str, ...] = ()
    resource_name: str | None = None
    resource_documentation: str | None = None
    resource_policy_uri: str | None = None
    resource_tos_uri: str | None = None
    tls_client_certificate_bound_access_tokens: bool | None = None
    authorization_details_types_supported: tuple[str, ...] = ()
    dpop_signing_alg_values_supported: tuple[str, ...] = ()
    dpop_bound_access_tokens_required: bool | None = None

    @field_validator("resource")
    @classmethod
    def secure_resource(cls, value: str) -> str:
        return _https_url(value, "OAuth resource")

    @field_validator("authorization_servers")
    @classmethod
    def secure_authorization_servers(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(_https_url(item, "OAuth authorization server") for item in value)


class OAuthAuthorizationServerMetadata(_ExtensibleModel):
    """RFC 8414 fields needed by a server-managed MCP OAuth client."""

    issuer: str
    authorization_endpoint: str
    token_endpoint: str
    registration_endpoint: str | None = None
    scopes_supported: tuple[str, ...] = ()
    response_types_supported: tuple[str, ...]
    response_modes_supported: tuple[str, ...] = ()
    grant_types_supported: tuple[str, ...] = ()
    token_endpoint_auth_methods_supported: tuple[str, ...] = ()
    revocation_endpoint: str | None = None
    code_challenge_methods_supported: tuple[str, ...] = ()
    client_id_metadata_document_supported: bool | None = None

    @field_validator(
        "issuer",
        "authorization_endpoint",
        "token_endpoint",
        "registration_endpoint",
        "revocation_endpoint",
    )
    @classmethod
    def secure_metadata_urls(cls, value: str | None, info: object) -> str | None:
        if value is None:
            return None
        return _https_url(value, f"OAuth {getattr(info, 'field_name', 'metadata URL')}")


class OAuthTokenSet(_FrozenModel):
    """Non-serializable-by-accident server token returned by a credential issuer."""

    access_token: SecretStr
    token_type: str = "Bearer"
    expires_at: datetime | None = None
    scopes: frozenset[str] = frozenset()
    refresh_token: SecretStr | None = None

    @field_validator("expires_at")
    @classmethod
    def aware_expiry(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("OAuth token expiry must be timezone-aware")
        return value.astimezone(UTC)

    @field_validator("token_type")
    @classmethod
    def bearer_only(cls, value: str) -> str:
        if value.lower() != "bearer":
            raise ValueError("initial MCP HTTP adapter supports Bearer tokens only")
        return "Bearer"


class McpToolEffectOverride(str, Enum):
    READ = "read"
    WRITE = "write"
    DESTRUCTIVE = "destructive"


class McpToolSecurityOverride(_FrozenModel):
    """Explicit administrator decision; remote annotations never create this."""

    effect: McpToolEffectOverride
    required_capabilities: frozenset[str] = frozenset()
    rationale: str

    @field_validator("rationale")
    @classmethod
    def rationale_required(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("a security override requires an administrator rationale")
        return value


class McpOperationContext(_FrozenModel):
    """Authenticated scope propagated across every MCP operation/callback."""

    tenant_id: str
    product_id: str
    principal_id: str
    roles: frozenset[str] = frozenset()
    purpose: str
    request_id: str = ""
    correlation_id: str = ""

    @field_validator("tenant_id", "product_id")
    @classmethod
    def deployment_ids(cls, value: str) -> str:
        if not _ID.fullmatch(value):
            raise ValueError("tenant/product ids must be lowercase deployment identifiers")
        return value

    @field_validator("principal_id", "purpose")
    @classmethod
    def nonblank_context(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("operation context fields must not be blank")
        return value


class ProductMcpConfig(_FrozenModel):
    tenant_id: str
    product_id: str
    product_workspace: Path
    servers: tuple[McpServerConfig, ...]

    @model_validator(mode="after")
    def isolated_product(self) -> ProductMcpConfig:
        if not _ID.fullmatch(self.tenant_id) or not _ID.fullmatch(self.product_id):
            raise ValueError("tenant_id/product_id must be lowercase deployment identifiers")
        root = Path(self.product_workspace)
        if not root.is_absolute() or not root.exists() or not root.is_dir():
            raise ValueError("product_workspace must be an existing absolute directory")
        if root.is_symlink():
            raise ValueError("product_workspace must not be a symbolic link")
        ids = [item.server_id for item in self.servers]
        if len(ids) != len(set(ids)):
            raise ValueError("MCP server ids must be unique within a product")
        object.__setattr__(self, "product_workspace", root.resolve(strict=True))
        return self


__all__ = [name for name in globals() if name.startswith(("Mcp", "JsonRpc"))]
__all__ += [
    "Annotations",
    "AudioContent",
    "BlobResourceContents",
    "CallToolParams",
    "CallToolResult",
    "CancelledNotification",
    "ClientCapabilities",
    "CompleteParams",
    "CompleteResult",
    "CompletionArgument",
    "CompletionContext",
    "CompletionReference",
    "CompletionValues",
    "CreateMessageParams",
    "CreateMessageResult",
    "CreateTaskResult",
    "ElicitationCapability",
    "ElicitationMode",
    "ElicitationParams",
    "ElicitationResult",
    "EmbeddedResource",
    "GetPromptResult",
    "GetTaskResult",
    "HttpResumeState",
    "Icon",
    "ImageContent",
    "Implementation",
    "InitializeParams",
    "InitializeResult",
    "LATEST_PROTOCOL_VERSION",
    "LegacySseCompatibility",
    "ListPromptsResult",
    "ListResourceTemplatesResult",
    "ListResourcesResult",
    "ListRootsResult",
    "ListTasksResult",
    "ListToolsResult",
    "LoggingLevel",
    "LoggingMessage",
    "PaginatedRequest",
    "PaginatedResult",
    "ProductMcpConfig",
    "ProgressNotification",
    "PromptArgument",
    "PromptMessage",
    "ReadResourceResult",
    "ResourceContents",
    "ResourceLink",
    "ResourceTemplate",
    "ResourceUpdatedNotification",
    "ResourcesCapability",
    "RootsCapability",
    "SamplingCapability",
    "SamplingMessage",
    "SamplingTool",
    "ServerCapabilities",
    "TaskClientCapability",
    "TaskServerCapability",
    "TaskStatus",
    "TextContent",
    "TextResourceContents",
    "ToolAnnotations",
    "ToolResultContent",
    "ToolUseContent",
]
