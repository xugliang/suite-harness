"""Provider-neutral failures for the memory and Customer360 capability boundary."""

from __future__ import annotations

from enum import Enum
from typing import Any


class MemoryErrorCode(str, Enum):
    """Stable failure categories exposed by memory providers."""

    PERMISSION_DENIED = "PERMISSION_DENIED"
    POLICY_DENIED = "POLICY_DENIED"
    NOT_FOUND = "NOT_FOUND"
    ENTITY_ALIAS_NOT_FOUND = "ENTITY_ALIAS_NOT_FOUND"
    REVISION_CONFLICT = "REVISION_CONFLICT"
    IDEMPOTENCY_CONFLICT = "IDEMPOTENCY_CONFLICT"
    INVALID_STATE = "INVALID_STATE"
    INVALID_ARGUMENT = "INVALID_ARGUMENT"


class MemoryCapabilityError(Exception):
    """A typed error which does not expose provider implementation details."""

    def __init__(self, code: MemoryErrorCode, message: str = "", **detail: Any) -> None:
        super().__init__(message or code.value)
        self.code = code
        self.message = message or code.value
        self.detail = detail
