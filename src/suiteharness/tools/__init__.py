"""Protected platform tools inherited by every product."""

from .builtins import (
    BuiltinRegistrationSet,
    BuiltinToolInstaller,
    ProtectedToolRegistry,
    RegistrationHandle,
    builtin_specs,
)
from .egress import ProductEgressArgumentPolicy
from .shell import BashTool, BashToolConfig
from .web import WebFetchTool, WebSearchTool
from .workspace import (
    FileToolLimits,
    MappingWorkspaceBindingResolver,
    WorkspaceBindingResolver,
    WorkspaceFileTools,
    WorkspaceToolBinding,
)

__all__ = [
    "BashTool",
    "BashToolConfig",
    "BuiltinRegistrationSet",
    "BuiltinToolInstaller",
    "FileToolLimits",
    "MappingWorkspaceBindingResolver",
    "ProtectedToolRegistry",
    "ProductEgressArgumentPolicy",
    "RegistrationHandle",
    "WebFetchTool",
    "WebSearchTool",
    "WorkspaceBindingResolver",
    "WorkspaceFileTools",
    "WorkspaceToolBinding",
    "builtin_specs",
]
