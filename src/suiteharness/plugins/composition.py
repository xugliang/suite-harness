"""Consume verified plugin contributions at the product activation boundary.

Factories create product-owned services, never tenant-global memory instances.
Selections do not issue capabilities: the existing tool/grant boundary still does.
Bindings are an activation snapshot; changing plugins requires a server rebuild.
"""
from __future__ import annotations

import inspect
from abc import ABC, abstractmethod
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

from suiteharness.execution.models import ToolSpec
from suiteharness.execution.protocols import WORKFLOW, PROMPT_STRATEGY, REFLECTION_STRATEGY
from suiteharness.memory.services import CONVERSATION_MEMORY, KNOWLEDGE_PROVIDER
from suiteharness.runtime.customer_bundle import ProductCatalog
from suiteharness.runtime.kernel import ProductActivator

from .models import ContributionKind

if TYPE_CHECKING:
    from suiteharness.runtime.kernel import Disposer


@dataclass(frozen=True)
class PreparedContribution:
    value: object
    cleanup: Disposer | None = None


@dataclass(frozen=True)
class ToolContribution:
    spec: ToolSpec
    handler: object


class ProductContributionFactory(ABC):
    @abstractmethod
    async def prepare(self, product) -> PreparedContribution:
        """Create a fresh product-scoped implementation and optional disposer."""


_SERVICES = {
    "memory": (ContributionKind.MEMORY, CONVERSATION_MEMORY),
    "knowledge": (ContributionKind.KNOWLEDGE, KNOWLEDGE_PROVIDER),
    "workflow": (ContributionKind.WORKFLOW, WORKFLOW),
    "reflection": (ContributionKind.REFLECTION, REFLECTION_STRATEGY),
    "prompt": (ContributionKind.PROMPT, PROMPT_STRATEGY),
}


async def _cleanup(callbacks):
    failures = []
    for callback in reversed(callbacks):
        try:
            result = callback()
            if inspect.isawaitable(result):
                await result
        except BaseException as exc:
            failures.append(exc)
    if failures:
        raise BaseExceptionGroup("plugin contribution cleanup failed", failures)


class _ComposedProduct(ProductActivator):
    def __init__(self, delegate, selections):
        self.descriptor = delegate.descriptor
        self._delegate = delegate
        self._selections = selections

    async def prepare(self, product):
        prepared = await self._delegate.prepare(product)
        cleanup = [prepared.cleanup] if prepared.cleanup else []
        bindings = dict(prepared.bindings)
        tools = []
        try:
            for key, factory in self._selections:
                contribution = await factory.prepare(product)
                if not isinstance(contribution, PreparedContribution):
                    raise TypeError("plugin factory must return PreparedContribution")
                if contribution.cleanup is not None:
                    if not callable(contribution.cleanup):
                        raise TypeError("plugin cleanup must be callable")
                    cleanup.append(contribution.cleanup)
                if key is None:
                    tool = contribution.value
                    if not isinstance(tool, ToolContribution) or not isinstance(tool.spec, ToolSpec) or not callable(tool.handler):
                        raise TypeError("tool factory must return a valid ToolContribution")
                    tools.append(tool)
                else:
                    key.validate(contribution.value)
                    bindings[key] = contribution.value
        except BaseException as exc:
            try:
                await _cleanup(cleanup)
            except BaseException as cleanup_exc:
                raise BaseExceptionGroup("plugin prepare and rollback failed", [exc, cleanup_exc]) from exc
            raise

        async def install(context):
            if prepared.install:
                await prepared.install(context)
            for tool in tools:
                context.register_tool(tool.spec, tool.handler)

        closed = False

        async def close():
            nonlocal closed
            if not closed:
                closed = True
                await _cleanup(cleanup)

        return replace(prepared, bindings=bindings, install=install, cleanup=close)


def compose_plugin_products(catalog, activators, contributions, bindings_by_product):
    """Build catalog BEFORE bundle resolution and wrap explicitly selected slots."""
    descriptors = list(catalog.descriptors)
    activators = list(activators)
    inventory = {(item.kind, item.contribution_id): item.value for item in contributions}
    for item in contributions:
        if item.kind is ContributionKind.PRODUCT:
            if not isinstance(item.value, ProductActivator):
                raise TypeError("product contribution must implement ProductActivator")
            descriptors.append(item.value.descriptor)
            activators.append(item.value)
    merged = ProductCatalog(descriptors)  # rejects shadowing even by trusted code
    unknown = set(bindings_by_product) - set(merged.product_ids)
    if unknown:
        raise ValueError(f"plugin bindings reference unknown products: {sorted(unknown)}")
    result = []
    for activator in activators:
        selected = bindings_by_product.get(activator.descriptor.product_id)
        selections = []
        if selected:
            slots = [(kind, getattr(selected, name), key) for name, (kind, key) in _SERVICES.items()]
            slots += [(ContributionKind.TOOL, name, None) for name in selected.tools]
            for kind, name, key in slots:
                if name is None:
                    continue
                factory = inventory.get((kind, name))
                if factory is None:
                    raise ValueError(f"missing plugin contribution: {kind.value}/{name}")
                if not isinstance(factory, ProductContributionFactory):
                    raise TypeError(f"{kind.value}/{name} must implement ProductContributionFactory")
                selections.append((key, factory))
        result.append(_ComposedProduct(activator, selections) if selections else activator)
    return merged, tuple(result)
