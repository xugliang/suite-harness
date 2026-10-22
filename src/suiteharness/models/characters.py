"""Content-free Unicode character measurements, independent of token budgets.

Text is counted with len(str): Chinese, whitespace, newlines and punctuation
each count as Unicode code points, not bytes or provider tokens. Tool schemas
and arguments use compact Unicode JSON, including their JSON punctuation.
Provider framing, sampling/stop controls, credentials, routing metadata and binary/base64 attachments
are not text content and are excluded. Unclassified is explicit, never guessed
from the wording of confidential input.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from suiteharness.context_labels import CHARACTER_CATEGORIES
from suiteharness.models.types import ModelRequest, ModelRole, TextContent


def _json_chars(value) -> int:
    return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")))


@dataclass(frozen=True)
class CharacterCounts:
    # Fixed ordered integer tuple: no messages, names or content are retained.
    categories: tuple[int, ...]
    nontext_parts: int = 0

    @property
    def total(self) -> int:
        return sum(self.categories)

    def fields(self, prefix: str) -> dict[str, int]:
        return {
            **{f"{prefix}_{name}_chars": count
               for name, count in zip(CHARACTER_CATEGORIES, self.categories, strict=True)},
            f"{prefix}_total_chars": self.total,
            f"{prefix}_nontext_parts": self.nontext_parts,
        }


def measure_characters(request: ModelRequest) -> CharacterCounts:
    counts = dict.fromkeys(CHARACTER_CATEGORIES, 0)
    nontext = 0
    for tool in request.tools:
        counts["tool_definitions"] += (
            len(tool.name) + len(tool.description) + _json_chars(tool.input_schema)
        )
    # A final-answer schema is not a tool definition. Keep it explicit in the
    # unclassified bucket rather than pretending it is ordinary system text.
    if request.response_schema is not None:
        counts["unclassified"] += _json_chars(request.response_schema)
    for message in request.messages:
        category = message.context_category
        if category is None:
            category = "system_prompt" if message.role is ModelRole.SYSTEM else "unclassified"
        elif not isinstance(category, str) or category not in counts:
            # Trusted extensions sometimes use model_copy(update=...), which
            # bypasses validation. A bad optional label must not break a call.
            category = "unclassified"
        counts[category] += len(message.name or "")
        for part in message.content:
            if isinstance(part, TextContent):
                counts[category] += len(part.text)
            else:
                nontext += 1
        for call in message.tool_calls:
            counts[category] += len(call.name) + _json_chars(call.arguments)
    return CharacterCounts(tuple(counts[name] for name in CHARACTER_CATEGORIES), nontext)


def comparison_fields(before: CharacterCounts, after: CharacterCounts | None) -> dict:
    result = {"character_unit": "unicode_codepoints_v1", "after_known": after is not None,
              **before.fields("before")}
    if after is not None:
        result.update(after.fields("after"))
        # Signed delta: JSON truncation wrappers can occasionally add characters.
        result.update({f"reduced_{name}_chars": old - new
                       for name, old, new in zip(CHARACTER_CATEGORIES, before.categories,
                                                 after.categories, strict=True)})
        result["reduced_total_chars"] = before.total - after.total
    return result
