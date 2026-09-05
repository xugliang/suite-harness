"""Server-side SQLite persistence primitives.

These stores are deliberately separate from the session transcript and audit
journal implementations. They persist channel deduplication and bounded
non-secret product checkpoints only.
"""

from .database import SQLiteDatabase
from .errors import (
    PersistenceCapacityError,
    PersistenceClosedError,
    PersistenceError,
    StateIdempotencyConflictError,
    StatePayloadTooLargeError,
    StateRevisionConflictError,
)
from .events import SQLiteChannelEventDeduplicator
from .mcp import McpHttpResumeCheckpoint, SQLiteMcpHttpResumeStore
from .models import (
    ChannelEventScope,
    EventScopeKind,
    ProductStateKey,
    StateRecord,
    StateWriteResult,
)
from .sandbox import SQLiteSandboxQuarantineStore
from .state import SQLiteProductStateStore

__all__ = [
    "ChannelEventScope",
    "EventScopeKind",
    "McpHttpResumeCheckpoint",
    "PersistenceCapacityError",
    "PersistenceClosedError",
    "PersistenceError",
    "ProductStateKey",
    "SQLiteChannelEventDeduplicator",
    "SQLiteDatabase",
    "SQLiteMcpHttpResumeStore",
    "SQLiteSandboxQuarantineStore",
    "SQLiteProductStateStore",
    "StateIdempotencyConflictError",
    "StatePayloadTooLargeError",
    "StateRecord",
    "StateRevisionConflictError",
    "StateWriteResult",
]
