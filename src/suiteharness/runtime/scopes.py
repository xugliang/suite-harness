"""Strongly typed runtime scopes and immutable hierarchical service bindings."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import Enum
from types import MappingProxyType
from typing import Any, Generic, TypeVar, cast

from suiteharness.runtime.effects import EffectScope

T = TypeVar("T")

_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_SERVICE_PATTERN = re.compile(r"^[a-z][a-z0-9]*(?:[._:/-][a-z0-9]+)*$")


def _identifier(value: str, label: str) -> str:
    if not isinstance(value, str) or not _ID_PATTERN.fullmatch(value):
        raise ValueError(f"{label} is not a valid identifier: {value!r}")
    return value


class ScopeKind(str, Enum):
    ROOT = "root"
    TENANT = "tenant"
    PRODUCT = "product"
    AGENT = "agent"


_CHILD_KIND: dict[ScopeKind, ScopeKind] = {
    ScopeKind.ROOT: ScopeKind.TENANT,
    ScopeKind.TENANT: ScopeKind.PRODUCT,
    ScopeKind.PRODUCT: ScopeKind.AGENT,
}


@dataclass(frozen=True, slots=True)
class ScopePath:
    """Validated identity path; impossible scope combinations fail at construction."""

    kind: ScopeKind
    tenant_id: str | None = None
    product_id: str | None = None
    agent_id: str | None = None
    session_id: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.kind, ScopeKind):
            raise TypeError("scope kind must be ScopeKind")
        if self.kind is ScopeKind.ROOT:
            if any(
                value is not None
                for value in (
                    self.tenant_id,
                    self.product_id,
                    self.agent_id,
                    self.session_id,
                )
            ):
                raise ValueError("root scope cannot carry child identifiers")
            return

        _identifier(cast(str, self.tenant_id), "tenant_id")
        if self.kind is ScopeKind.TENANT:
            if any(
                value is not None
                for value in (self.product_id, self.agent_id, self.session_id)
            ):
                raise ValueError("tenant scope cannot carry product or agent identifiers")
            return

        _identifier(cast(str, self.product_id), "product_id")
        if self.kind is ScopeKind.PRODUCT:
            if self.agent_id is not None or self.session_id is not None:
                raise ValueError("product scope cannot carry agent identifiers")
            return

        if self.kind is not ScopeKind.AGENT:
            raise ValueError(f"unsupported scope kind: {self.kind!r}")
        _identifier(cast(str, self.agent_id), "agent_id")
        _identifier(cast(str, self.session_id), "session_id")

    @classmethod
    def root(cls) -> ScopePath:
        return cls(ScopeKind.ROOT)

    @classmethod
    def tenant(cls, tenant_id: str) -> ScopePath:
        return cls(ScopeKind.TENANT, tenant_id=tenant_id)

    @classmethod
    def product(cls, tenant_id: str, product_id: str) -> ScopePath:
        return cls(
            ScopeKind.PRODUCT,
            tenant_id=tenant_id,
            product_id=product_id,
        )

    @classmethod
    def agent(
        cls,
        tenant_id: str,
        product_id: str,
        agent_id: str,
        session_id: str,
    ) -> ScopePath:
        return cls(
            ScopeKind.AGENT,
            tenant_id=tenant_id,
            product_id=product_id,
            agent_id=agent_id,
            session_id=session_id,
        )

    def parent(self) -> ScopePath | None:
        if self.kind is ScopeKind.ROOT:
            return None
        if self.kind is ScopeKind.TENANT:
            return ScopePath.root()
        if self.kind is ScopeKind.PRODUCT:
            return ScopePath.tenant(cast(str, self.tenant_id))
        return ScopePath.product(
            cast(str, self.tenant_id),
            cast(str, self.product_id),
        )


@dataclass(frozen=True, slots=True)
class RequestScope:
    """Server-authenticated execution identity passed to scoped providers."""

    path: ScopePath
    principal_id: str
    channel_id: str = "internal"
    roles: frozenset[str] = frozenset()
    purpose: str = "runtime"
    request_id: str = ""
    correlation_id: str = ""
    session_owner_id: str | None = None

    def __post_init__(self) -> None:
        if self.path.kind not in {ScopeKind.PRODUCT, ScopeKind.AGENT}:
            raise ValueError("request scope must target a product or agent")
        _identifier(self.principal_id, "principal_id")
        _identifier(self.channel_id, "channel_id")
        if not isinstance(self.purpose, str) or not self.purpose.strip():
            raise ValueError("purpose must not be empty")
        object.__setattr__(self, "roles", frozenset(self.roles))
        for role in self.roles:
            _identifier(role, "role")
        if self.request_id:
            _identifier(self.request_id, "request_id")
        if self.correlation_id:
            _identifier(self.correlation_id, "correlation_id")
        if self.session_owner_id is not None:
            _identifier(self.session_owner_id, "session_owner_id")

    @property
    def tenant_id(self) -> str:
        return cast(str, self.path.tenant_id)

    @property
    def product_id(self) -> str:
        return cast(str, self.path.product_id)

    @property
    def effective_session_owner_id(self) -> str:
        """Return the trusted isolation owner for durable conversation state."""

        return self.session_owner_id or self.principal_id


@dataclass(frozen=True, slots=True)
class ServiceKey(Generic[T]):
    """Typed key plus the policy governing where its binding may be replaced."""

    name: str
    contract: type[T]
    declared_at: ScopeKind
    overridable_at: frozenset[ScopeKind] = frozenset()

    def __post_init__(self) -> None:
        if not _SERVICE_PATTERN.fullmatch(self.name):
            raise ValueError(f"invalid service key: {self.name!r}")
        if not isinstance(self.contract, type):
            raise TypeError("service contract must be a runtime class or runtime-checkable protocol")
        overrides = frozenset(self.overridable_at)
        if self.declared_at in overrides:
            raise ValueError("declared scope must not be repeated in overridable_at")
        object.__setattr__(self, "overridable_at", overrides)

    @property
    def binding_scopes(self) -> frozenset[ScopeKind]:
        return frozenset({self.declared_at, *self.overridable_at})

    def validate(self, value: object) -> None:
        try:
            valid = isinstance(value, self.contract)
        except TypeError as exc:
            raise TypeError(
                f"service contract for {self.name!r} is not runtime-checkable"
            ) from exc
        if not valid:
            raise TypeError(
                f"service {self.name!r} requires {self.contract.__name__}, "
                f"got {type(value).__name__}"
            )


class ServiceBindingError(ValueError):
    """Invalid binding graph or forbidden service override."""


@dataclass(frozen=True, slots=True)
class _Binding:
    key: ServiceKey[Any]
    value: object


class ServiceBindings:
    """One immutable binding layer with validated parent fallback."""

    __slots__ = ("_kind", "_local", "_parent")

    def __init__(
        self,
        kind: ScopeKind,
        values: Mapping[ServiceKey[Any], object] | None = None,
        *,
        parent: ServiceBindings | None = None,
    ) -> None:
        if parent is None and kind is not ScopeKind.ROOT:
            raise ServiceBindingError("only root bindings may omit a parent")
        if parent is not None and _CHILD_KIND.get(parent.kind) is not kind:
            raise ServiceBindingError(
                f"{kind.value} bindings cannot inherit directly from {parent.kind.value}"
            )

        local: dict[str, _Binding] = {}
        for key, value in (values or {}).items():
            if not isinstance(key, ServiceKey):
                raise TypeError("binding keys must be ServiceKey instances")
            if key.name in local:
                raise ServiceBindingError(f"duplicate local service name: {key.name}")
            if kind not in key.binding_scopes:
                raise ServiceBindingError(
                    f"service {key.name!r} cannot be bound at {kind.value} scope"
                )
            key.validate(value)

            inherited = parent._find(key.name) if parent is not None else None
            if inherited is not None:
                if inherited.key != key:
                    raise ServiceBindingError(
                        f"service {key.name!r} was redefined with an incompatible ServiceKey"
                    )
                if kind not in key.overridable_at:
                    raise ServiceBindingError(
                        f"service {key.name!r} cannot be overridden at {kind.value} scope"
                    )
            local[key.name] = _Binding(key, value)

        self._kind = kind
        self._parent = parent
        self._local = MappingProxyType(local)

    @classmethod
    def root(cls, values: Mapping[ServiceKey[Any], object] | None = None) -> ServiceBindings:
        return cls(ScopeKind.ROOT, values)

    @property
    def kind(self) -> ScopeKind:
        return self._kind

    @property
    def parent(self) -> ServiceBindings | None:
        return self._parent

    @property
    def local_names(self) -> tuple[str, ...]:
        return tuple(sorted(self._local))

    def derive(
        self,
        kind: ScopeKind,
        values: Mapping[ServiceKey[Any], object] | None = None,
    ) -> ServiceBindings:
        return ServiceBindings(kind, values, parent=self)

    def resolve(self, key: ServiceKey[T]) -> T:
        binding = self._find(key.name)
        if binding is None:
            raise LookupError(f"service is not bound: {key.name}")
        if binding.key != key:
            raise ServiceBindingError(
                f"service {key.name!r} was requested with an incompatible ServiceKey"
            )
        return cast(T, binding.value)

    def contains(self, key: ServiceKey[Any]) -> bool:
        binding = self._find(key.name)
        return binding is not None and binding.key == key

    def snapshot(self) -> Mapping[str, object]:
        """Return a detached, read-only effective-name map for diagnostics."""

        values: dict[str, object] = {}
        lineage: list[ServiceBindings] = []
        current: ServiceBindings | None = self
        while current is not None:
            lineage.append(current)
            current = current.parent
        for layer in reversed(lineage):
            values.update({name: binding.value for name, binding in layer._local.items()})
        return MappingProxyType(values)

    def _find(self, name: str) -> _Binding | None:
        binding = self._local.get(name)
        if binding is not None:
            return binding
        if self._parent is None:
            return None
        return self._parent._find(name)


@dataclass(frozen=True, slots=True)
class RootContext:
    """Process scope.  It owns infrastructure, never product or subject data."""

    bindings: ServiceBindings = field(default_factory=ServiceBindings.root)
    effects: EffectScope = field(default_factory=lambda: EffectScope("root"))
    path: ScopePath = field(default_factory=ScopePath.root, init=False)

    def __post_init__(self) -> None:
        if self.bindings.kind is not ScopeKind.ROOT:
            raise ValueError("RootContext requires root bindings")
        if self.effects.parent is not None:
            raise ValueError("RootContext effect scope cannot have a parent")

    def resolve(self, key: ServiceKey[T]) -> T:
        return self.bindings.resolve(key)

    def tenant(
        self,
        tenant_id: str,
        bindings: Mapping[ServiceKey[Any], object] | None = None,
    ) -> TenantScope:
        return TenantScope(
            root=self,
            path=ScopePath.tenant(tenant_id),
            bindings=self.bindings.derive(ScopeKind.TENANT, bindings),
            effects=self.effects.child(f"tenant:{tenant_id}"),
        )

    async def close(self) -> None:
        await self.effects.close()


@dataclass(frozen=True, slots=True)
class TenantScope:
    root: RootContext
    path: ScopePath
    bindings: ServiceBindings
    effects: EffectScope

    def __post_init__(self) -> None:
        if self.path.kind is not ScopeKind.TENANT or self.bindings.kind is not ScopeKind.TENANT:
            raise ValueError("TenantScope requires tenant path and bindings")
        if self.path.parent() != self.root.path:
            raise ValueError("TenantScope path does not descend from RootContext")
        if self.bindings.parent is not self.root.bindings:
            raise ValueError("TenantScope bindings do not descend from RootContext")
        if self.effects.parent is not self.root.effects:
            raise ValueError("TenantScope effects do not descend from RootContext")

    def resolve(self, key: ServiceKey[T]) -> T:
        return self.bindings.resolve(key)

    def product(
        self,
        product_id: str,
        bindings: Mapping[ServiceKey[Any], object] | None = None,
    ) -> ProductContext:
        tenant_id = cast(str, self.path.tenant_id)
        return ProductContext(
            tenant=self,
            path=ScopePath.product(tenant_id, product_id),
            bindings=self.bindings.derive(ScopeKind.PRODUCT, bindings),
            effects=self.effects.child(f"product:{product_id}"),
        )

    async def close(self) -> None:
        await self.effects.close()


@dataclass(frozen=True, slots=True)
class ProductContext:
    tenant: TenantScope
    path: ScopePath
    bindings: ServiceBindings
    effects: EffectScope

    def __post_init__(self) -> None:
        if self.path.kind is not ScopeKind.PRODUCT or self.bindings.kind is not ScopeKind.PRODUCT:
            raise ValueError("ProductContext requires product path and bindings")
        if self.path.parent() != self.tenant.path:
            raise ValueError("ProductContext path does not descend from TenantScope")
        if self.bindings.parent is not self.tenant.bindings:
            raise ValueError("ProductContext bindings do not descend from TenantScope")
        if self.effects.parent is not self.tenant.effects:
            raise ValueError("ProductContext effects do not descend from TenantScope")

    def resolve(self, key: ServiceKey[T]) -> T:
        return self.bindings.resolve(key)

    def agent(
        self,
        agent_id: str,
        session_id: str,
        *,
        principal_id: str,
        channel_id: str = "internal",
        roles: frozenset[str] = frozenset(),
        purpose: str = "agent-run",
        request_id: str = "",
        correlation_id: str = "",
        bindings: Mapping[ServiceKey[Any], object] | None = None,
    ) -> AgentContext:
        path = ScopePath.agent(
            cast(str, self.path.tenant_id),
            cast(str, self.path.product_id),
            agent_id,
            session_id,
        )
        return AgentContext(
            product=self,
            path=path,
            request=RequestScope(
                path=path,
                principal_id=principal_id,
                channel_id=channel_id,
                roles=roles,
                purpose=purpose,
                request_id=request_id,
                correlation_id=correlation_id,
            ),
            bindings=self.bindings.derive(ScopeKind.AGENT, bindings),
            effects=self.effects.child(f"agent:{agent_id}:{session_id}"),
        )

    async def close(self) -> None:
        await self.effects.close()


@dataclass(frozen=True, slots=True)
class AgentContext:
    product: ProductContext
    path: ScopePath
    request: RequestScope
    bindings: ServiceBindings
    effects: EffectScope

    def __post_init__(self) -> None:
        if self.path.kind is not ScopeKind.AGENT or self.bindings.kind is not ScopeKind.AGENT:
            raise ValueError("AgentContext requires agent path and bindings")
        if self.path.parent() != self.product.path:
            raise ValueError("AgentContext path does not descend from ProductContext")
        if self.bindings.parent is not self.product.bindings:
            raise ValueError("AgentContext bindings do not descend from ProductContext")
        if self.effects.parent is not self.product.effects:
            raise ValueError("AgentContext effects do not descend from ProductContext")
        if self.request.path != self.path:
            raise ValueError("AgentContext request path must match the agent path")

    def resolve(self, key: ServiceKey[T]) -> T:
        return self.bindings.resolve(key)

    async def close(self) -> None:
        await self.effects.close()
