"""Internal provenance labels for character diagnostics, never prompt instructions."""

from typing import Literal

ContextCategory = Literal[
    "system_prompt", "skill_catalog", "skill_instructions", "conversation_summary", "history_memory",
    "current_execution", "current_question", "unclassified"
]

CHARACTER_CATEGORIES = (
    "system_prompt", "skill_catalog", "skill_instructions", "conversation_summary", "history_memory",
    "tool_definitions", "current_execution",
    "current_question", "unclassified",
)
