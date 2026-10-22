"""Product-specific conversation cleanup, separate from business-data deletion."""
from abc import ABC
from dataclasses import dataclass

from suiteharness.runtime import RequestScope
from suiteharness.sessions.models import SessionIdentity


@dataclass(frozen=True, slots=True)
class CleanupContext:
    scope: RequestScope
    identity: SessionIdentity
    cutoff_revision: int
    idempotency_key: str


class ConversationCleanup(ABC):
    """Usable no-extra-storage default; products override only this step.

    Hooks must be scope-bound and idempotent, may be retried after a crash,
    and must not delete business entities or clear other conversations.
    Raise on incomplete cleanup; the host reports partial failure.
    """

    async def clear_product_memory(self, context: CleanupContext) -> int:
        return 0


@dataclass(frozen=True, slots=True)
class ConversationClearResult:
    cutoff_revision: int
    failed_layers: tuple[str, ...] = ()
    replayed: bool = False
