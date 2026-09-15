"""Default company-agent prompt strategy."""

from __future__ import annotations

from dataclasses import dataclass

from suiteharness.execution import PromptStrategy


@dataclass(frozen=True, slots=True)
class DefaultPromptStrategy(PromptStrategy):
    """Render server-owned instructions and the untrusted user input separately."""

    system_prompt: str = PromptStrategy.system_prompt

    def __post_init__(self) -> None:
        if not self.system_prompt.strip():
            raise ValueError("system_prompt must not be blank")
