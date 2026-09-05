"""Errors raised by the server-side persistence foundation."""

from __future__ import annotations


class PersistenceError(RuntimeError):
    """Base class for durable-state failures with safe public messages."""


class PersistenceClosedError(PersistenceError):
    """An operation targeted a database that has already been closed."""


class PersistenceCapacityError(PersistenceError):
    """A configured durable-state capacity limit was reached."""


class StatePayloadTooLargeError(PersistenceError):
    """A state value exceeds the configured serialized byte limit."""


class StateRevisionConflictError(PersistenceError):
    """A compare-and-set command observed a different current revision."""

    def __init__(self, *, expected_revision: int, actual_revision: int) -> None:
        self.expected_revision = expected_revision
        self.actual_revision = actual_revision
        super().__init__(
            "state revision conflict: "
            f"expected {expected_revision}, current revision is {actual_revision}"
        )


class StateIdempotencyConflictError(PersistenceError):
    """An idempotency key was reused for a different state command."""


__all__ = [
    "PersistenceCapacityError",
    "PersistenceClosedError",
    "PersistenceError",
    "StateIdempotencyConflictError",
    "StatePayloadTooLargeError",
    "StateRevisionConflictError",
]
