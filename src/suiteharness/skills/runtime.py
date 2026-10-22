"""Host-owned, request-local skill preparation and model provenance.

Reading instructions never grants tool authority. Only this run's actual model
request can satisfy a model-originated tool's declared skill prerequisites.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any

from suiteharness.diagnostics import emit as diagnostic
from suiteharness.models.types import ModelMessage, ModelRequest, ModelRole, TextContent

from .models import SkillAccess, SkillDocument, SkillError, SkillLimits
from .protocols import SkillProvider


@dataclass(slots=True)
class _Stage:
    name: str
    documents: dict[str, SkillDocument] = field(default_factory=dict)
    read_calls: dict[str, SkillDocument] = field(default_factory=dict)
    protected_texts: set[tuple[str, str | None, str]] = field(default_factory=set)
    show_catalog: bool = True


@dataclass(frozen=True, slots=True)
class _Invocation:
    stage: _Stage
    documents: tuple[SkillDocument, ...]


@dataclass(frozen=True, slots=True)
class _Binding:
    runtime: SkillRuntime
    stage: _Stage


_CURRENT: ContextVar[_Binding | None] = ContextVar("suiteharness_skill_run", default=None)


def _text(message: ModelMessage) -> str:
    return "\n".join(part.text for part in message.content if isinstance(part, TextContent))


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _document_key(document: SkillDocument) -> str:
    return document.skill_id + ":" + document.resource_id


def _message_key(message: ModelMessage) -> tuple[str, str | None, str]:
    return message.role.value, message.tool_call_id, _digest(_text(message))


class SkillRuntime:
    """One authenticated execution; no state survives in another run or user."""

    def __init__(self, request: Any, provider: SkillProvider) -> None:
        self.request = request
        self.provider = provider
        self.stage = _Stage("main")
        self.catalog = ""
        self.limits = getattr(provider, "limits", SkillLimits())
        self.pending_reads: dict[str, SkillDocument] = {}
        self.invocations: dict[str, _Invocation] = {}
        self.call_proofs: dict[str, tuple[str, dict[str, str]]] = {}

    @property
    def enabled(self) -> bool:
        """An explicit empty binding behaves like an unconfigured product."""
        return bool(self.catalog or self.provider.static_manifest())

    async def initialize(self) -> None:
        try:
            descriptors = await self.provider.list_skills(SkillAccess(
                scope=self.request.scope, source="model", run_id=self.request.run_id,
            ))
            lines = [
                "# 可用技能", "请求匹配时，先用 suiteharness.skill.read 读取对应技能再执行。",
            ]
            for descriptor in descriptors:
                if descriptor.model_invocable:
                    lines.append(f"- {descriptor.skill_id}：{descriptor.description}")
            self.catalog = "\n".join(lines) if len(lines) > 2 else ""
        except SkillError as exc:
            # An unavailable remote catalog must not disable unrelated QA.
            diagnostic("skill.catalog_failed", code=exc.code, retryable=exc.retryable)
            self.catalog = ""

    def access(self, source: str, command_name: str | None = None) -> SkillAccess:
        return SkillAccess(scope=self.request.scope, source=source,
                           run_id=self.request.run_id, command_name=command_name)

    def store(self, stage: _Stage, document: SkillDocument) -> None:
        key = _document_key(document)
        previous = stage.documents.get(key)
        if previous is not None and previous.instructions_sha256 != document.instructions_sha256:
            raise SkillError("skill_content_changed", "技能版本已变化，本次未执行业务动作。")
        prospective = {**stage.documents, key: document}
        if len(prospective) > self.limits.max_active_skills or sum(
            len(item.instructions.encode("utf-8")) for item in prospective.values()
        ) > self.limits.max_active_bytes:
            raise SkillError("skill_budget_exceeded", "本轮技能说明超过配置上限。")
        stage.documents[key] = document

    def commit_read(self, call_id: str, result: Any) -> None:
        document = self.pending_reads.pop(call_id, None)
        if document is None or not isinstance(result, dict):
            raise SkillError("skill_context_unavailable", "技能读取记录无效。")
        if (result.get("instructions") != document.instructions
                or result.get("instructions_sha256") != document.instructions_sha256):
            raise SkillError("skill_content_changed", "技能结果未完整提供。")
        binding = _CURRENT.get()
        if binding is None or binding.runtime is not self:
            raise SkillError("skill_context_unavailable", "技能运行上下文已失效。")
        self.store(binding.stage, document)
        binding.stage.read_calls[call_id] = document
        diagnostic("skill.loaded", skill_id=document.skill_id, version=document.version,
                   skill_source=document.source, instructions_sha256=document.instructions_sha256,
                   characters=len(document.instructions), stage=binding.stage.name)

    def missing(self, call_id: str, required: tuple[str, ...]) -> tuple[str, ...]:
        proof = self.call_proofs.get(call_id)
        if proof is None:
            binding = _CURRENT.get()
            if binding is not None and binding.runtime is self and all(
                name + ":instructions" in binding.stage.documents for name in required
            ):
                raise SkillError(
                    "skill_context_unavailable",
                    "技能已准备，但模型网关未提供实际请求的加载凭证，本次未执行业务动作。",
                )
            return required
        token, hashes = proof
        invocation = self.invocations.get(token)
        binding = _CURRENT.get()
        if (binding is None or binding.runtime is not self or invocation is None
                or invocation.stage is not binding.stage):
            return required
        return tuple(name for name in required if name not in hashes)


@contextmanager
def bind_skill_runtime(runtime: SkillRuntime | None) -> Iterator[None]:
    token = _CURRENT.set(None if runtime is None else _Binding(runtime, runtime.stage))
    try:
        yield
    finally:
        _CURRENT.reset(token)


def _binding_for(request: Any | None = None) -> _Binding | None:
    binding = _CURRENT.get()
    if binding is not None and request is not None and (
        binding.runtime.request.run_id != request.run_id
        or binding.runtime.request.scope != request.scope
    ):
        raise SkillError("skill_context_unavailable", "技能上下文不属于当前身份或执行。")
    return binding


async def current_skill_catalog(request: Any) -> str:
    binding = _binding_for(request)
    return "" if binding is None else binding.runtime.catalog


async def preload_skill(
    request: Any, skill_id: str, *, resource_id: str = "instructions", source: str = "workflow",
) -> SkillDocument:
    binding = _binding_for(request)
    if binding is None:
        raise SkillError("skill_context_unavailable", "当前执行未启用技能。")
    command_name = None
    if source == "command":
        context = getattr(binding.runtime, "command_context", None)
        if context is None or context.command_id is None:
            raise SkillError("skill_context_unavailable", "缺少可信命令上下文。")
        command_name = context.command_id
        binding.stage.show_catalog = False
    # Freeze the approved body for this stage/run. Command routing and workflow
    # initialization may both ensure it is loaded, without a second MCP read or
    # re-evaluating the same document under a different invocation source.
    existing = binding.stage.documents.get(skill_id + ":" + resource_id)
    if existing is not None:
        return existing
    document = await binding.runtime.provider.read_skill(
        binding.runtime.access(source, command_name), skill_id, resource_id=resource_id,
    )
    if resource_id == "instructions":
        binding.runtime.store(binding.stage, document)
    diagnostic("skill.preloaded", skill_id=document.skill_id, version=document.version,
               operation=resource_id, reason=source, stage=binding.stage.name)
    return document


@contextmanager
def isolated_skill_stage(
    request: Any, stage_id: str, *, documents: tuple[SkillDocument, ...] = (),
) -> Iterator[None]:
    binding = _binding_for(request)
    if binding is None:
        yield
        return
    stage = _Stage(stage_id, show_catalog=False)
    for document in documents:
        binding.runtime.store(stage, document)
    token = _CURRENT.set(_Binding(binding.runtime, stage))
    try:
        yield
    finally:
        _CURRENT.reset(token)


def _contains_document(message: ModelMessage, stage: _Stage, document: SkillDocument) -> bool:
    if message.role is ModelRole.SYSTEM:
        return document.instructions in _text(message)
    if message.role is not ModelRole.TOOL or stage.read_calls.get(message.tool_call_id) != document:
        return False
    try:
        payload = json.loads(_text(message))
        result = payload.get("result", {})
        return (payload.get("status") == "succeeded"
                and result.get("instructions") == document.instructions
                and result.get("instructions_sha256") == document.instructions_sha256)
    except (ValueError, AttributeError, TypeError):
        return False


def prepare_skill_model_request(request: ModelRequest) -> ModelRequest:
    binding = _CURRENT.get()
    if binding is None:
        return request
    runtime, stage = binding.runtime, binding.stage
    messages = list(request.messages)
    if stage.show_catalog and runtime.catalog and not any(
        message.role is ModelRole.SYSTEM and runtime.catalog in _text(message) for message in messages
    ):
        messages.insert(0, ModelMessage.text(ModelRole.SYSTEM, runtime.catalog,
                                            context_category="skill_catalog"))
    for document in stage.documents.values():
        found = False
        for index, message in enumerate(messages):
            if _contains_document(message, stage, document):
                stage.protected_texts.add(_message_key(message))
                messages[index] = message.model_copy(update={"context_category": "skill_instructions"})
                found = True
        if not found:
            message = ModelMessage.text(ModelRole.SYSTEM, document.instructions,
                                        context_category="skill_instructions")
            stage.protected_texts.add(_message_key(message))
            # System roles remain standard; this is a host-loaded instruction,
            # never user-supplied memory or an arbitrary tool's claimed rule.
            messages.insert(0, message)
    token = request.skill_context_token
    previous = runtime.invocations.get(token or "")
    documents = tuple(stage.documents.values())
    if previous is None or previous.stage is not stage or previous.documents != documents:
        token = "skill-context-" + uuid.uuid4().hex
        runtime.invocations[token] = _Invocation(stage, documents)
    return request.model_copy(update={"messages": tuple(messages), "skill_context_token": token})


def is_protected_skill_message(message: ModelMessage) -> bool:
    binding = _CURRENT.get()
    return bool(binding and message.role in {ModelRole.SYSTEM, ModelRole.TOOL}
                and _message_key(message) in binding.stage.protected_texts)


def verify_skill_model_request(request: ModelRequest) -> None:
    binding = _CURRENT.get()
    if binding is None:
        return
    invocation = binding.runtime.invocations.get(request.skill_context_token or "")
    if invocation is None or invocation.stage is not binding.stage:
        raise SkillError("skill_context_unavailable", "模型请求未绑定本任务的技能上下文。")
    for document in invocation.documents:
        if not any(_contains_document(message, binding.stage, document) for message in request.messages):
            raise SkillError("skill_context_unavailable", "技能正文未完整进入本次模型请求。")


def record_skill_model_response(request: ModelRequest, tool_calls: Any) -> None:
    binding = _CURRENT.get()
    if binding is None:
        return
    verify_skill_model_request(request)
    invocation = binding.runtime.invocations[request.skill_context_token]
    hashes = {document.skill_id: document.instructions_sha256 for document in invocation.documents
              if document.resource_id == "instructions"}
    for call in tool_calls:
        # An inner gateway's actual prepared request has precedence when a
        # trusted wrapper also participates in this host protocol.
        binding.runtime.call_proofs.setdefault(call.call_id, (request.skill_context_token, dict(hashes)))


def skill_read_spec():
    from suiteharness.execution.models import ToolEffect, ToolSpec
    return ToolSpec(
        name="suiteharness.skill.read", effects=frozenset({ToolEffect.READ}),
        description="按当前可用技能目录中的完整 ID 读取操作说明；只读取受控技能，不接受文件路径。",
        input_schema={"type": "object", "properties": {"skill_id": {
            "type": "string", "minLength": 1, "maxLength": 128,
        }}, "required": ["skill_id"], "additionalProperties": False},
    )


async def read_skill_tool(context: Any, arguments: dict[str, Any]) -> dict[str, Any]:
    binding = _CURRENT.get()
    if (binding is None or context.scope != binding.runtime.request.scope
            or context.run_id != binding.runtime.request.run_id
            or context.tool_identity.namespace != "suiteharness"
            or context.tool_identity.origin != "suiteharness.builtin.skill"
            or context.tool_identity.name != "suiteharness.skill.read"):
        raise SkillError("skill_not_available", "当前执行无法读取该技能。")
    document = await binding.runtime.provider.read_skill(
        binding.runtime.access("model"), arguments["skill_id"],
    )
    result = {"skill_id": document.skill_id, "version": document.version,
              "instructions_sha256": document.instructions_sha256,
              "instructions": document.instructions}
    if len(json.dumps(result, ensure_ascii=False).encode("utf-8")) > binding.runtime.limits.max_instruction_bytes:
        raise SkillError("skill_budget_exceeded", "技能正文超过完整读取上限。")
    binding.runtime.pending_reads[context.call_id] = document
    return result


__all__ = [
    "SkillRuntime", "bind_skill_runtime", "current_skill_catalog", "preload_skill",
    "isolated_skill_stage", "prepare_skill_model_request", "verify_skill_model_request",
    "record_skill_model_response", "is_protected_skill_message", "skill_read_spec", "read_skill_tool",
]
