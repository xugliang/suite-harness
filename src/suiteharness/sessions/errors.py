"""Structured failures raised by session stores."""

from __future__ import annotations


class SessionStoreError(RuntimeError):
    """Base class for durable session-state failures."""


class SessionCapacityError(SessionStoreError):
    """A durable idempotency collection reached its configured hard limit."""


class SessionPayloadTooLargeError(SessionStoreError):
    """A transcript payload or checkpoint exceeds its configured byte limit."""


class SessionNotFoundError(SessionStoreError):
    """The authenticated session identity does not exist."""


class SessionRevisionConflictError(SessionStoreError):
    """A compare-and-swap operation observed a different revision."""

    def __init__(self, expected: int, actual: int) -> None:
        super().__init__(f"session revision conflict: expected {expected}, actual {actual}")
        self.expected = expected
        self.actual = actual


class SessionIdempotencyConflictError(SessionStoreError):
    """An idempotency key was reused for a different logical operation."""


__all__ = [
    "SessionCapacityError",
    "SessionIdempotencyConflictError",
    "SessionNotFoundError",
    "SessionPayloadTooLargeError",
    "SessionRevisionConflictError",
    "SessionStoreError",
]
