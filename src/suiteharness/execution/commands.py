"""Optional, host-installed command routes; never an additional capability grant.

The runner resolves a route once from the current request, before calling any
replaceable strategy. Products own command grammar, handlers and business rules.
Historical messages and input metadata are deliberately not authority sources.
"""

from __future__ import annotations

import hashlib
import re
from abc import ABC, abstractmethod
from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from suiteharness.runtime.scopes import RequestScope, ScopeKind, ScopePath

if TYPE_CHECKING:
    from suiteharness.execution.models import RunRequest

_NAME = re.compile(r"^[a-z][a-z0-9]*(?:[._:/-][a-z0-9]+)*$")


def command_text(value: object) -> str:
    """Read current words without stripping prefix whitespace or using metadata."""
    if isinstance(value, str):
        return value
    if isinstance(value, Mapping):
        message = value.get("message")
        if isinstance(message, str):
            return message
        if isinstance(message, Mapping) and isinstance(message.get("text"), str):
            return message["text"]
    return ""


@dataclass(frozen=True, slots=True)
class CommandSpec:
    command_id: str
    aliases: tuple[str, ...] = ()
    match: Literal["contains", "prefix", "exact"] = "prefix"
    allowed_tools: frozenset[str] = frozenset()
    prefix_boundary: bool = True
    preload_skills: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.command_id, str) or not _NAME.fullmatch(self.command_id):
            raise ValueError("command_id must be a stable command name")
        if self.match not in {"contains", "prefix", "exact"}:
            raise ValueError("unsupported command match mode")
        if not isinstance(self.prefix_boundary, bool):
            raise TypeError("prefix_boundary must be a boolean")
        if isinstance(self.aliases, str) or isinstance(self.allowed_tools, str):
            raise TypeError("aliases and allowed_tools must be collections")
        aliases = tuple(self.aliases)
        allowed = frozenset(self.allowed_tools)
        if any(not isinstance(alias, str) or not alias.startswith("/")
               or len(alias) < 2 or any(char.isspace() for char in alias) for alias in aliases):
            raise ValueError("command aliases must be nonempty slash words")
        if len(set(aliases)) != len(aliases):
            raise ValueError("command aliases must be unique")
        if any(not isinstance(name, str) or not _NAME.fullmatch(name) for name in allowed):
            raise ValueError("allowed_tools must contain stable tool names")
        object.__setattr__(self, "aliases", aliases)
        object.__setattr__(self, "allowed_tools", allowed)
        if isinstance(self.preload_skills, str):
            raise TypeError("preload_skills must be a collection")
        preload = tuple(self.preload_skills)
        if len(preload) != len(set(preload)) or any(
            not isinstance(name, str) or not _NAME.fullmatch(name) for name in preload
        ):
            raise ValueError("preload_skills must contain unique registered skill IDs")
        object.__setattr__(self, "preload_skills", preload)

    def matches(self, text: str) -> bool:
        for alias in self.aliases:
            if self.match == "contains" and alias in text:
                return True
            if self.match == "exact" and text == alias:
                return True
            if self.match == "prefix" and text.startswith(alias):
                suffix = text[len(alias):]
                if not self.prefix_boundary or not suffix or suffix[0].isspace():
                    return True
        return False


@dataclass(frozen=True, slots=True)
class CommandContext:
    """Immutable routing evidence, not an approval token or model input option."""

    scope: RequestScope
    run_id: str
    input_digest: str
    text: str
    command_id: str | None
    matched_commands: tuple[str, ...]
    allowed_tools: frozenset[str] | None
    denied_tools: frozenset[str] = frozenset()
    preload_skills: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "matched_commands", tuple(self.matched_commands))
        object.__setattr__(self, "denied_tools", frozenset(self.denied_tools))
        object.__setattr__(self, "preload_skills", tuple(self.preload_skills))
        if self.allowed_tools is not None:
            object.__setattr__(self, "allowed_tools", frozenset(self.allowed_tools))

    @property
    def principal_id(self) -> str:
        return self.scope.principal_id


class CommandHandler(ABC):
    """Trusted product parser for ambiguity and command-specific syntax.

Return a registered command ID (including an internal route without aliases),
or None for ordinary text. Returning None after a match fails closed.
Business execution still occurs through workflow ToolIntents and the runner.
"""

    @abstractmethod
    def select(self, text: str, matches: tuple[CommandSpec, ...]) -> str | None:
        raise NotImplementedError


class UniqueCommandHandler(CommandHandler):
    def select(self, text: str, matches: tuple[CommandSpec, ...]) -> str | None:
        if len(matches) > 1:
            raise ValueError("multiple commands require a product conflict handler")
        return matches[0].command_id if matches else None


class CommandRegistry(ABC):
    """Host-owned routing dependency, installed separately from product strategies."""

    @abstractmethod
    def resolve(self, request: RunRequest) -> CommandContext | None:
        raise NotImplementedError


@dataclass(frozen=True, slots=True)
class _Registration:
    specs: tuple[CommandSpec, ...]
    handler: CommandHandler
    command_only_tools: frozenset[str]


class InMemoryCommandRegistry(CommandRegistry):
    """A registry for one kernel lifetime; absent product registrations are a no-op.

    The trusted deployment owns this instance, registers before accepting runs,
    and unregisters only after draining the affected product. Do not share one
    mutable registry between kernels. Active runs retain their captured routes.
    """

    def __init__(self) -> None:
        self._registrations: dict[ScopePath, _Registration] = {}

    def register(
        self, scope: ScopePath, specs: tuple[CommandSpec, ...], *,
        handler: CommandHandler | None = None,
        command_only_tools: frozenset[str] = frozenset(),
    ) -> None:
        if not isinstance(scope, ScopePath) or scope.kind is not ScopeKind.PRODUCT:
            raise ValueError("command registration must have an exact product scope")
        if scope in self._registrations:
            raise ValueError("commands are already registered for this product")
        specs = tuple(specs)
        if not all(isinstance(spec, CommandSpec) for spec in specs):
            raise TypeError("specs must contain CommandSpec values")
        if len({spec.command_id for spec in specs}) != len(specs):
            raise ValueError("command IDs must be unique within a product")
        aliases = [alias for spec in specs for alias in spec.aliases]
        if len(set(aliases)) != len(aliases):
            raise ValueError("command aliases must be unique within a product")
        selected_handler = handler if handler is not None else UniqueCommandHandler()
        if not isinstance(selected_handler, CommandHandler):
            raise TypeError("handler must implement CommandHandler")
        if isinstance(command_only_tools, str):
            raise TypeError("command_only_tools must be a collection")
        restricted = frozenset(command_only_tools)
        if any(not isinstance(name, str) or not _NAME.fullmatch(name) for name in restricted):
            raise ValueError("command_only_tools must contain stable tool names")
        self._registrations[scope] = _Registration(specs, selected_handler, restricted)

    def unregister(self, scope: ScopePath) -> bool:
        """Remove exactly one drained product's routes; leave other scopes intact."""
        if not isinstance(scope, ScopePath) or scope.kind is not ScopeKind.PRODUCT:
            raise ValueError("command registration must have an exact product scope")
        return self._registrations.pop(scope, None) is not None

    def clear(self) -> None:
        """Release this deployment-owned registry after all of its runs drain."""
        self._registrations.clear()

    def resolve(self, request: RunRequest) -> CommandContext | None:
        scope = ScopePath.product(request.scope.tenant_id, request.scope.product_id)
        registration = self._registrations.get(scope)
        if registration is None:
            return None
        text = command_text(request.input)
        matches = tuple(spec for spec in registration.specs if spec.matches(text))
        selected_id = registration.handler.select(text, matches)
        indexed = {spec.command_id: spec for spec in registration.specs}
        if selected_id is None and matches:
            raise ValueError("a matched command cannot fall back to unrestricted execution")
        if selected_id is not None and selected_id not in indexed:
            raise ValueError("command handler selected an unregistered route")
        return CommandContext(
            scope=request.scope,
            run_id=request.run_id,
            input_digest=hashlib.sha256(text.encode("utf-8")).hexdigest(),
            text=text,
            command_id=selected_id,
            matched_commands=tuple(spec.command_id for spec in matches),
            allowed_tools=None if selected_id is None else indexed[selected_id].allowed_tools,
            denied_tools=registration.command_only_tools if selected_id is None else frozenset(),
            preload_skills=() if selected_id is None else indexed[selected_id].preload_skills,
        )


__all__ = [
    "CommandContext", "CommandHandler", "CommandRegistry", "CommandSpec",
    "InMemoryCommandRegistry", "UniqueCommandHandler", "command_text",
]
