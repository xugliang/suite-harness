"""Small in-memory reference implementations for the execution contracts."""

from __future__ import annotations

import asyncio
import hashlib
import secrets
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from threading import RLock

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError

from suiteharness.execution.models import (
    ApprovalBinding,
    ApprovalCredential,
    ApprovalTarget,
    AuditEvent,
    CapabilityGrant,
    ToolIdentity,
    ToolSpec,
)
from suiteharness.execution.protocols import (
    RegisteredTool,
    ToolHandler,
    registration_candidates,
)
from suiteharness.runtime.effects import EffectScope
from suiteharness.runtime.scopes import RequestScope, ScopeKind, ScopePath


def _now() -> datetime:
    return datetime.now(UTC)


def _at_utc(value: datetime, label: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{label} must be timezone-aware")
    return value.astimezone(UTC)


class InMemoryToolRegistry:
    """Scope-aware registry suitable for tests and single processes.

    Products may use the same logical tool name with different implementations.
    Registrations cannot be replaced in place; their opaque ownership handle
    removes only that exact entry during scoped shutdown.  Framework identities
    and aliases are installed through :meth:`register_protected` and cannot be
    shadowed by a more-specific scope.
    """

    _PROTECTED_NAMESPACES = frozenset({"suiteharness", "framework"})

    def __init__(self) -> None:
        self._items: dict[tuple[ScopePath, str], _RegisteredEntry] = {}
        self._protected_names: dict[str, str] = {}
        self._lock = RLock()

    def register(
        self,
        scope: ScopePath,
        spec: ToolSpec,
        handler: ToolHandler,
    ) -> ToolRegistrationHandle:
        """Register an ordinary scoped tool with a host-derived identity."""

        identity = ToolIdentity.scoped(scope, spec.name)
        return self._register(scope, spec, handler, identity=identity, protected=False)

    def register_protected(
        self,
        spec: ToolSpec,
        handler: ToolHandler,
        *,
        identity: ToolIdentity | None = None,
    ) -> ToolRegistrationHandle:
        """Register a trusted root tool whose namespace and alias are reserved."""

        protected_identity = identity or ToolIdentity(
            namespace="suiteharness",
            name=spec.name,
            origin="suiteharness.framework",
            version="1",
        )
        if protected_identity.namespace not in self._PROTECTED_NAMESPACES:
            raise ValueError(
                "protected tools must use a reserved framework namespace"
            )
        return self._register(
            ScopePath.root(),
            spec,
            handler,
            identity=protected_identity,
            protected=True,
        )

    def _register(
        self,
        scope: ScopePath,
        spec: ToolSpec,
        handler: ToolHandler,
        *,
        identity: ToolIdentity,
        protected: bool,
    ) -> ToolRegistrationHandle:
        if scope.kind not in {ScopeKind.ROOT, ScopeKind.TENANT, ScopeKind.PRODUCT}:
            raise ValueError("tools may only be registered at root, tenant, or product scope")
        if identity.name != spec.name:
            raise ValueError("tool identity name must match the model-facing tool name")
        if not protected and identity.namespace in self._PROTECTED_NAMESPACES:
            raise ValueError("reserved framework namespaces require protected registration")
        if not callable(handler):
            raise TypeError("tool handler must be callable")
        _reject_remote_references(spec.input_schema)
        Draft202012Validator.check_schema(spec.input_schema)
        safe_spec = spec.model_copy(deep=True)
        validator = Draft202012Validator(safe_spec.input_schema)
        key = (scope, spec.name)
        registration_id = f"registration-{secrets.token_urlsafe(24)}"
        item = RegisteredTool(
            registration_scope=scope,
            identity=identity,
            spec=safe_spec,
            handler=handler,
            input_validator=validator,
        )
        with self._lock:
            if scope.kind is not ScopeKind.ROOT and spec.name in self._protected_names:
                raise ValueError(f"protected root tool {spec.name!r} cannot be shadowed")
            if protected and any(
                name == spec.name and candidate_scope.kind is not ScopeKind.ROOT
                for candidate_scope, name in self._items
            ):
                raise ValueError(
                    f"cannot protect root tool {spec.name!r} while a shadow exists"
                )
            if key in self._items:
                raise ValueError(
                    f"tool {spec.name!r} is already registered at {scope.kind.value} scope"
                )
            if any(entry.item.identity == identity for entry in self._items.values()):
                raise ValueError(
                    f"tool identity {identity.canonical_id!r} is already registered"
                )
            self._items[key] = _RegisteredEntry(
                item=item,
                registration_id=registration_id,
                protected=protected,
            )
            if protected:
                self._protected_names[spec.name] = registration_id
        return ToolRegistrationHandle(
            self,
            scope,
            spec.name,
            registration_id,
            identity,
        )

    def register_owned(
        self,
        scope: ScopePath,
        effects: EffectScope,
        spec: ToolSpec,
        handler: ToolHandler,
    ) -> ToolRegistrationHandle:
        """Register a tool whose exact registration is removed with ``effects``."""

        registration = self.register(scope, spec, handler)
        try:
            effects.callback(
                f"tool-registration:{scope.kind.value}:{spec.name}",
                registration.close,
            )
        except BaseException:
            registration.close()
            raise
        return registration

    def unregister(
        self,
        scope: ScopePath,
        tool_name: str,
        *,
        owner: ToolRegistrationHandle,
    ) -> bool:
        """Remove only the exact registration represented by ``owner``.

        A product registration handle cannot remove a root, tenant, other
        product registration.
        """

        if not isinstance(owner, ToolRegistrationHandle) or owner._registry is not self:
            return False
        if owner.scope != scope or owner.tool_name != tool_name:
            return False
        key = (scope, tool_name)
        with self._lock:
            current = self._items.get(key)
            if current is None or current.registration_id != owner._registration_id:
                return False
            self._items.pop(key)
            if (
                current.protected
                and self._protected_names.get(tool_name) == current.registration_id
            ):
                self._protected_names.pop(tool_name, None)
            owner._closed = True
            return True

    def resolve(self, scope: RequestScope, tool_name: str) -> RegisteredTool | None:
        with self._lock:
            for candidate in registration_candidates(scope):
                item = self._items.get((candidate, tool_name))
                if item is not None:
                    return item.item
        return None


@dataclass(frozen=True, slots=True)
class _RegisteredEntry:
    item: RegisteredTool
    registration_id: str
    protected: bool


class ToolRegistrationHandle:
    """Opaque ownership proof for one exact registry entry."""

    __slots__ = (
        "_closed",
        "_registration_id",
        "_registry",
        "identity",
        "scope",
        "tool_name",
    )

    def __init__(
        self,
        registry: InMemoryToolRegistry,
        scope: ScopePath,
        tool_name: str,
        registration_id: str,
        identity: ToolIdentity,
    ) -> None:
        self._registry = registry
        self.scope = scope
        self.tool_name = tool_name
        self.identity = identity
        self._registration_id = registration_id
        self._closed = False

    @property
    def closed(self) -> bool:
        return self._closed

    def close(self) -> None:
        if self._closed:
            return
        self._registry.unregister(self.scope, self.tool_name, owner=self)


def _reject_remote_references(schema: object) -> None:
    """Disallow schema validation from becoming an implicit network client."""

    if isinstance(schema, dict):
        for key, value in schema.items():
            if key in {"$ref", "$dynamicRef"} and isinstance(value, str):
                if not value.startswith("#"):
                    raise SchemaError("remote JSON Schema references are not supported")
            _reject_remote_references(value)
    elif isinstance(schema, list):
        for value in schema:
            _reject_remote_references(value)


class InMemoryCapabilityAuthority:
    """Trusted control-plane grant store with explicit revocation."""

    def __init__(self) -> None:
        self._grants: dict[str, CapabilityGrant] = {}
        self._grant_owners: dict[str, str | None] = {}
        self._active_owners: dict[tuple[str, str], str] = {}
        self._owner_grants: dict[str, set[str]] = {}
        self._lock = asyncio.Lock()

    async def register(self, grant: CapabilityGrant) -> None:
        async with self._lock:
            if grant.grant_id in self._grants:
                raise ValueError(f"duplicate capability grant: {grant.grant_id}")
            self._grants[grant.grant_id] = grant
            owner = self._active_owners.get((grant.tenant_id, grant.product_id))
            self._grant_owners[grant.grant_id] = owner
            if owner is not None:
                self._owner_grants.setdefault(owner, set()).add(grant.grant_id)

    async def issue(
        self,
        scope: ScopePath,
        *,
        tool_identities: Iterable[ToolIdentity],
        capabilities: Iterable[str] = (),
        principal_id: str | None = None,
        lifetime: timedelta = timedelta(hours=1),
        now: datetime | None = None,
    ) -> CapabilityGrant:
        if scope.kind not in {ScopeKind.PRODUCT, ScopeKind.AGENT}:
            raise ValueError("capability grants require a product or agent scope")
        if lifetime <= timedelta(0):
            raise ValueError("grant lifetime must be positive")
        issued_at = _at_utc(now or _now(), "now")
        grant = CapabilityGrant(
            grant_id=f"grant-{secrets.token_urlsafe(24)}",
            tenant_id=str(scope.tenant_id),
            product_id=str(scope.product_id),
            tool_identities=frozenset(tool_identities),
            capabilities=frozenset(capabilities),
            principal_id=principal_id,
            issued_at=issued_at,
            expires_at=issued_at + lifetime,
        )
        await self.register(grant)
        return grant

    async def resolve(self, grant_id: str) -> CapabilityGrant | None:
        async with self._lock:
            grant = self._grants.get(grant_id)
            if grant is None:
                return None
            current_owner = self._active_owners.get(
                (grant.tenant_id, grant.product_id)
            )
            grant_owner = self._grant_owners.get(grant_id)
            # An unbound grant is useful for a raw ExecutionRunner contract test,
            # but it becomes invalid as soon as the kernel owns that product scope.
            if current_owner is None:
                return grant if grant_owner is None else None
            return grant if grant_owner == current_owner else None

    async def revoke(self, grant_id: str) -> bool:
        async with self._lock:
            grant = self._grants.pop(grant_id, None)
            owner = self._grant_owners.pop(grant_id, None)
            if owner is not None:
                owned = self._owner_grants.get(owner)
                if owned is not None:
                    owned.discard(grant_id)
                    if not owned:
                        self._owner_grants.pop(owner, None)
            return grant is not None

    async def bind_activation(
        self,
        tenant_id: str,
        product_ids: tuple[str, ...],
        owner_token: str,
    ) -> None:
        """Atomically reserve product grant scopes for a kernel activation."""

        if not owner_token:
            raise ValueError("activation owner token must not be empty")
        if not product_ids or len(product_ids) != len(set(product_ids)):
            raise ValueError("activation product_ids must be non-empty and unique")
        keys = tuple((tenant_id, product_id) for product_id in product_ids)
        async with self._lock:
            conflicts = [key for key in keys if key in self._active_owners]
            if conflicts:
                raise ValueError("capability scope already belongs to an activation")
            for key in keys:
                self._active_owners[key] = owner_token
            self._owner_grants.setdefault(owner_token, set())

    async def release_activation(self, owner_token: str) -> None:
        """Remove only the exact owner's scopes and all grants issued under it."""

        async with self._lock:
            keys = [
                key
                for key, current_owner in self._active_owners.items()
                if current_owner == owner_token
            ]
            for key in keys:
                self._active_owners.pop(key, None)
            grant_ids = self._owner_grants.pop(owner_token, set())
            for grant_id in grant_ids:
                self._grants.pop(grant_id, None)
                self._grant_owners.pop(grant_id, None)


class InMemoryApprovalStore:
    """Opaque bearer approvals stored by token digest and atomically consumed."""

    def __init__(self) -> None:
        self._pending: dict[str, ApprovalBinding] = {}
        self._lock = asyncio.Lock()

    async def issue(self, binding: ApprovalBinding, *, now: datetime | None = None) -> ApprovalCredential:
        current = _at_utc(now or _now(), "now")
        if binding.issued_at > current:
            raise ValueError("cannot issue an approval with a future issued_at")
        if binding.expires_at <= current:
            raise ValueError("cannot issue an already-expired approval")
        async with self._lock:
            while True:
                token = secrets.token_urlsafe(32)
                token_digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
                if token_digest not in self._pending:
                    break
            self._pending[token_digest] = binding
        return ApprovalCredential(token=token, binding=binding)

    async def consume_if_matches(
        self,
        token: str,
        expected: ApprovalTarget,
    ) -> ApprovalBinding | None:
        """Consume only when the stored target exactly matches ``expected``.

        A token presented for the wrong tenant, principal, session, run, call,
        tool or arguments is invalid, but it must not burn the legitimate
        approval.  The comparison and removal therefore share one lock.
        """

        if not isinstance(expected, ApprovalTarget):
            raise TypeError("expected must be an ApprovalTarget")
        token_digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
        async with self._lock:
            binding = self._pending.get(token_digest)
            if binding is None:
                return None
            if binding.expires_at <= _now():
                self._pending.pop(token_digest, None)
                return None
            if binding.target != expected:
                return None
            # Consume before the effect starts.  A failed handler does not make
            # an approval reusable, which closes retry/replay ambiguity.
            self._pending.pop(token_digest, None)
            return binding

    async def revoke(self, token: str) -> bool:
        token_digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
        async with self._lock:
            return self._pending.pop(token_digest, None) is not None


class InMemoryAuditJournal:
    """Append-only process-local journal; production adapters should be durable."""

    def __init__(self) -> None:
        self._events: list[str] = []
        self._lock = asyncio.Lock()

    async def append(self, event: AuditEvent) -> None:
        async with self._lock:
            # Store a detached serialization so nested JSON dictionaries cannot
            # mutate an event after it was appended.
            self._events.append(event.model_dump_json())

    async def events(self, *, run_id: str | None = None) -> tuple[AuditEvent, ...]:
        async with self._lock:
            events = tuple(AuditEvent.model_validate_json(raw) for raw in self._events)
            if run_id is None:
                return events
            return tuple(event for event in events if event.run_id == run_id)
