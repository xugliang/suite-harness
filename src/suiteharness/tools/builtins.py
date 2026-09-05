"""Atomic installer for SuiteHarness's protected root built-in tools."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from suiteharness.execution import ToolEffect, ToolHandler, ToolIdentity, ToolSpec
from suiteharness.sandbox import SandboxBackend
from suiteharness.web import WebFetchService, WebSearchService
from suiteharness.workspace import SecurePosixWorkspaceFileSystem

from .egress import ProductEgressArgumentPolicy
from .shell import BashTool, BashToolConfig
from .web import WebFetchTool, WebSearchTool
from .workspace import FileToolLimits, WorkspaceBindingResolver, WorkspaceFileTools


class RegistrationHandle(Protocol):
    @property
    def closed(self) -> bool: ...

    def close(self) -> None: ...


class ProtectedToolRegistry(Protocol):
    def register_protected(
        self,
        spec: ToolSpec,
        handler: ToolHandler,
        *,
        identity: ToolIdentity | None = None,
    ) -> RegistrationHandle: ...


@dataclass(frozen=True, slots=True)
class BuiltinRegistrationSet:
    handles: tuple[RegistrationHandle, ...]

    def close(self) -> None:
        for handle in reversed(self.handles):
            handle.close()

    @property
    def identities(self) -> tuple[ToolIdentity, ...]:
        identities: list[ToolIdentity] = []
        for handle in self.handles:
            identity = getattr(handle, "identity", None)
            if isinstance(identity, ToolIdentity):
                identities.append(identity)
        return tuple(identities)


def _object_schema(
    properties: dict[str, object],
    *,
    required: tuple[str, ...] = (),
) -> dict[str, object]:
    schema: dict[str, object] = {
        "type": "object",
        "properties": properties,
        "additionalProperties": False,
    }
    if required:
        schema["required"] = list(required)
    return schema


_SPACE = {"type": "string", "enum": ["product", "shared"], "default": "product"}
_PATH = {"type": "string", "minLength": 1, "maxLength": 4096}


def _specs() -> dict[str, ToolSpec]:
    read = frozenset({ToolEffect.READ})
    write = frozenset({ToolEffect.WRITE})
    external_read = frozenset({ToolEffect.READ, ToolEffect.EXTERNAL})
    return {
        "suiteharness.fs.list": ToolSpec(
            name="suiteharness.fs.list",
            description="列出当前产品或共享工作区中的目录；结果数量受限。",
            effects=read,
            required_capabilities=frozenset({"workspace.read"}),
            input_schema=_object_schema(
                {
                    "space": _SPACE,
                    "path": {**_PATH, "default": "."},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 1000},
                }
            ),
        ),
        "suiteharness.fs.read": ToolSpec(
            name="suiteharness.fs.read",
            description="读取工作区文本文件，支持 UTF-8、GBK、GB18030。",
            effects=read,
            required_capabilities=frozenset({"workspace.read"}),
            input_schema=_object_schema(
                {
                    "space": _SPACE,
                    "path": _PATH,
                    "encoding": {"type": "string", "enum": ["utf-8", "gbk", "gb18030"]},
                    "max_bytes": {"type": "integer", "minimum": 1},
                },
                required=("path",),
            ),
        ),
        "suiteharness.fs.write": ToolSpec(
            name="suiteharness.fs.write",
            description="在策略授权目录内原子写入 UTF-8 文本；不提供删除能力。",
            effects=write,
            required_capabilities=frozenset({"workspace.write"}),
            input_schema=_object_schema(
                {
                    "space": _SPACE,
                    "path": _PATH,
                    "content": {"type": "string"},
                    "overwrite": {"type": "boolean", "default": False},
                },
                required=("path", "content"),
            ),
        ),
        "suiteharness.fs.edit": ToolSpec(
            name="suiteharness.fs.edit",
            description="对授权文本执行匹配次数严格校验的原子替换。",
            effects=write,
            required_capabilities=frozenset({"workspace.read", "workspace.write"}),
            input_schema=_object_schema(
                {
                    "space": _SPACE,
                    "path": _PATH,
                    "old_text": {"type": "string", "minLength": 1},
                    "new_text": {"type": "string"},
                    "expected_replacements": {"type": "integer", "minimum": 1, "default": 1},
                },
                required=("path", "old_text", "new_text"),
            ),
        ),
        "suiteharness.fs.glob": ToolSpec(
            name="suiteharness.fs.glob",
            description="在授权工作区中进行有上限的文件模式匹配。",
            effects=read,
            required_capabilities=frozenset({"workspace.read"}),
            input_schema=_object_schema(
                {
                    "space": _SPACE,
                    "pattern": _PATH,
                    "limit": {"type": "integer", "minimum": 1, "maximum": 2000},
                },
                required=("pattern",),
            ),
        ),
        "suiteharness.fs.grep": ToolSpec(
            name="suiteharness.fs.grep",
            description="在授权文本文件中执行有文件数和匹配数上限的搜索。",
            effects=read,
            required_capabilities=frozenset({"workspace.read"}),
            input_schema=_object_schema(
                {
                    "space": _SPACE,
                    "path": {**_PATH, "default": "."},
                    "query": {"type": "string", "minLength": 1, "maxLength": 512},
                    "file_pattern": {"type": "string", "minLength": 1, "maxLength": 4096},
                    "regex": {"type": "boolean", "default": False},
                    "case_sensitive": {"type": "boolean", "default": True},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 2000},
                },
                required=("query",),
            ),
        ),
        "suiteharness.shell.bash": ToolSpec(
            name="suiteharness.shell.bash",
            description="仅通过配置的沙箱运行 Bash；网络默认关闭，任意 Shell 属破坏性能力。",
            effects=frozenset({ToolEffect.WRITE, ToolEffect.DESTRUCTIVE, ToolEffect.EXTERNAL}),
            required_capabilities=frozenset(
                {"shell.execute", "workspace.read", "workspace.write", "network.egress"}
            ),
            input_schema=_object_schema(
                {
                    "command": {"type": "string", "minLength": 1, "maxLength": 32768},
                    "space": _SPACE,
                    "working_directory": {**_PATH, "default": "."},
                    "egress_profile": {"type": "string", "minLength": 1, "maxLength": 128},
                },
                required=("command",),
            ),
        ),
        "suiteharness.web.search": ToolSpec(
            name="suiteharness.web.search",
            description="通过百度千帆或 Microsoft Foundry 结构化搜索接口检索网页。",
            effects=external_read,
            required_capabilities=frozenset({"web.search", "network.egress"}),
            input_schema=_object_schema(
                {
                    "query": {"type": "string", "minLength": 1, "maxLength": 2000},
                    "count": {"type": "integer", "minimum": 1, "maximum": 50, "default": 10},
                    "provider": {"type": "string", "minLength": 1, "maxLength": 128},
                    "freshness": {"type": "string", "minLength": 1, "maxLength": 128},
                },
                required=("query",),
            ),
        ),
        "suiteharness.web.fetch": ToolSpec(
            name="suiteharness.web.fetch",
            description="经 SSRF 防护、逐跳重定向校验和内容上限抓取公开网页。",
            effects=external_read,
            required_capabilities=frozenset({"web.fetch", "network.egress"}),
            input_schema=_object_schema(
                {
                    "url": {"type": "string", "minLength": 1, "maxLength": 8192},
                    "egress_profile": {"type": "string", "minLength": 1, "maxLength": 128},
                },
                required=("url",),
            ),
        ),
    }


class BuiltinToolInstaller:
    """Install all platform tools as one rollback-safe protected registration set."""

    def __init__(
        self,
        registry: ProtectedToolRegistry,
        *,
        workspaces: WorkspaceBindingResolver,
        sandbox: SandboxBackend,
        web_search: WebSearchService,
        web_fetch: WebFetchService,
        file_limits: FileToolLimits | None = None,
        secure_filesystem: SecurePosixWorkspaceFileSystem | None = None,
        bash_config: BashToolConfig | None = None,
        egress_policy: ProductEgressArgumentPolicy | None = None,
    ) -> None:
        self._registry = registry
        files = WorkspaceFileTools(
            workspaces,
            file_limits,
            secure_filesystem=secure_filesystem,
        )
        self.workspace_files_production_safe = files.production_safe
        self._handlers: dict[str, ToolHandler] = {
            "suiteharness.fs.list": files.list,
            "suiteharness.fs.read": files.read,
            "suiteharness.fs.write": files.write,
            "suiteharness.fs.edit": files.edit,
            "suiteharness.fs.glob": files.glob,
            "suiteharness.fs.grep": files.grep,
            "suiteharness.shell.bash": BashTool(workspaces, sandbox, bash_config, egress_policy),
            "suiteharness.web.search": WebSearchTool(web_search, egress_policy),
            "suiteharness.web.fetch": WebFetchTool(web_fetch, egress_policy),
        }

    def install(self) -> BuiltinRegistrationSet:
        handles: list[RegistrationHandle] = []
        try:
            for name, spec in _specs().items():
                family = name.split(".", 2)[1]
                identity = ToolIdentity(
                    namespace="suiteharness",
                    name=name,
                    origin=f"suiteharness.builtin.{family}",
                    version="1",
                )
                handles.append(
                    self._registry.register_protected(
                        spec,
                        self._handlers[name],
                        identity=identity,
                    )
                )
        except BaseException:
            for handle in reversed(handles):
                handle.close()
            raise
        return BuiltinRegistrationSet(tuple(handles))


def builtin_specs() -> tuple[ToolSpec, ...]:
    """Return detached declarative metadata for administration/documentation."""

    return tuple(spec.model_copy(deep=True) for spec in _specs().values())


__all__ = [
    "BuiltinRegistrationSet",
    "BuiltinToolInstaller",
    "ProtectedToolRegistry",
    "RegistrationHandle",
    "builtin_specs",
]
