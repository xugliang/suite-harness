"""Conservative JSON Schema policy for declarations not yet trusted as code."""

from __future__ import annotations

from copy import deepcopy
from typing import Any

_UNSAFE_KEYWORDS = frozenset(
    {
        "$dynamicRef",
        "$ref",
        "contains",
        "pattern",
        "patternProperties",
        "uniqueItems",
    }
)
_SCHEMA_MAP_CHILDREN = frozenset(
    {"$defs", "definitions", "dependencies", "dependentSchemas", "properties"}
)
_SCHEMA_SEQUENCE_CHILDREN = frozenset({"allOf", "anyOf", "oneOf", "prefixItems"})
_SCHEMA_SINGLE_CHILDREN = frozenset(
    {
        "additionalItems",
        "additionalProperties",
        "contentSchema",
        "else",
        "if",
        "items",
        "not",
        "propertyNames",
        "then",
        "unevaluatedItems",
        "unevaluatedProperties",
    }
)
_MAX_SCHEMA_NODES = 512
_MAX_SCHEMA_DEPTH = 32
_MAX_SCHEMA_BRANCHES = 16
_MAX_SCHEMA_COLLECTION = 256
_MAX_ARRAY_ITEMS = 4_096


class UnsafeJsonSchemaError(ValueError):
    """An untrusted schema exceeds the deterministic local validation subset."""


def harden_untrusted_json_schema(
    root: dict[str, Any],
    *,
    label: str = "untrusted JSON Schema",
) -> dict[str, Any]:
    """Return a detached schema constrained to bounded local validation work.

    Schema-bearing keywords are followed explicitly, which means a data
    property whose *name* is ``pattern`` remains legal. ``format`` and the
    ``content*`` vocabulary are annotations because callers do not install a
    format checker or content decoder.
    """

    try:
        safe = deepcopy(root)
    except (MemoryError, RecursionError, TypeError, ValueError) as exc:
        raise UnsafeJsonSchemaError(f"{label} cannot be copied safely") from exc
    stack: list[tuple[object, int]] = [(safe, 1)]
    nodes = 0
    branches = 0
    while stack:
        schema, depth = stack.pop()
        if isinstance(schema, bool):
            continue
        if not isinstance(schema, dict):
            continue
        nodes += 1
        if nodes > _MAX_SCHEMA_NODES or depth > _MAX_SCHEMA_DEPTH:
            raise UnsafeJsonSchemaError(f"{label} exceeds the safe complexity limit")
        blocked = _UNSAFE_KEYWORDS.intersection(schema)
        if blocked:
            keyword = sorted(blocked)[0]
            raise UnsafeJsonSchemaError(
                f"{label} keyword {keyword!r} is not supported"
            )
        for keyword in ("enum", "required"):
            collection = schema.get(keyword)
            if isinstance(collection, list) and len(collection) > _MAX_SCHEMA_COLLECTION:
                raise UnsafeJsonSchemaError(
                    f"{label} {keyword!r} exceeds the safe collection limit"
                )
        for keyword in _SCHEMA_MAP_CHILDREN:
            children = schema.get(keyword)
            if not isinstance(children, dict):
                continue
            if len(children) > _MAX_SCHEMA_COLLECTION:
                raise UnsafeJsonSchemaError(
                    f"{label} {keyword!r} exceeds the safe collection limit"
                )
            for child in children.values():
                if isinstance(child, dict | bool):
                    stack.append((child, depth + 1))
        for keyword in _SCHEMA_SEQUENCE_CHILDREN:
            children = schema.get(keyword)
            if not isinstance(children, list):
                continue
            if len(children) > _MAX_SCHEMA_BRANCHES:
                raise UnsafeJsonSchemaError(
                    f"{label} {keyword!r} exceeds the safe branch limit"
                )
            branches += len(children)
            if branches > _MAX_SCHEMA_COLLECTION:
                raise UnsafeJsonSchemaError(
                    f"{label} exceeds the aggregate branch limit"
                )
            for child in children:
                if isinstance(child, dict | bool):
                    stack.append((child, depth + 1))
        for keyword in _SCHEMA_SINGLE_CHILDREN:
            child = schema.get(keyword)
            if isinstance(child, dict | bool):
                stack.append((child, depth + 1))
        if any(keyword in schema for keyword in ("items", "prefixItems", "unevaluatedItems")):
            declared = schema.get("maxItems")
            if declared is None:
                maximum = _MAX_ARRAY_ITEMS
            elif isinstance(declared, int) and not isinstance(declared, bool) and declared >= 0:
                maximum = min(declared, _MAX_ARRAY_ITEMS)
            else:
                maximum = None
            if maximum is not None:
                ordered = {"maxItems": maximum}
                ordered.update(
                    (key, value) for key, value in schema.items() if key != "maxItems"
                )
                schema.clear()
                schema.update(ordered)
    return safe


__all__ = ["UnsafeJsonSchemaError", "harden_untrusted_json_schema"]
