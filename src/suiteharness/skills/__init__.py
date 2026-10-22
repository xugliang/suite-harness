"""Optional product-scoped skills with local and MCP-backed implementations."""

from .filesystem import FilesystemSkillProvider
from .mcp import BoundMcpSkillClient, McpSkillProvider, mcp_skill_provider_from_config
from .models import SkillAccess, SkillDescriptor, SkillDocument, SkillError, SkillLimits
from .protocols import EmptySkillProvider, SkillAccessPolicy, SkillProvider
from .registry import SkillRegistry
from .services import SKILL_PROVIDER

__all__ = [
    "SKILL_PROVIDER", "BoundMcpSkillClient", "EmptySkillProvider", "FilesystemSkillProvider", "McpSkillProvider", "SkillAccess",
    "SkillAccessPolicy", "SkillDescriptor", "SkillDocument", "SkillError", "SkillLimits",
    "SkillProvider", "SkillRegistry",
    "mcp_skill_provider_from_config",
]
