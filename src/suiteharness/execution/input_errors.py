"""Schema-only repair hints: never echo rejected input or validator messages."""
from __future__ import annotations

import json
import math
import re
from jsonschema.exceptions import ValidationError

_FIELD = re.compile(r"[A-Za-z_][A-Za-z0-9_-]{0,63}\Z")
_NUMERIC = {"minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum",
            "minLength", "maxLength", "minItems", "maxItems", "multipleOf"}


def input_error_hint(error: ValidationError) -> tuple[str, dict]:
    # Derive names from the trusted schema, not error.path (which may contain
    # arbitrary attacker-controlled object keys). Do not stringify error itself.
    schema_path = list(error.absolute_schema_path)
    names = [schema_path[i + 1] for i, item in enumerate(schema_path[:-1])
             if item == "properties" and isinstance(schema_path[i + 1], str)
             and _FIELD.fullmatch(schema_path[i + 1])]
    path = ".".join(names)[:160] or "root"
    rule = error.validator if isinstance(error.validator, str) and _FIELD.fullmatch(error.validator) else "schema"
    details = {"validation_path": path, "validation_rule": rule}
    expected = error.validator_value
    if rule in _NUMERIC and isinstance(expected, (int, float)) and not isinstance(expected, bool) and math.isfinite(expected):
        details["validation_constraint"] = expected
    elif rule == "type":
        types = expected if isinstance(expected, list) else [expected]
        allowed = {"object", "array", "string", "number", "integer", "boolean", "null"}
        details["validation_constraint"] = "/".join(t for t in types if isinstance(t, str) and t in allowed)
    elif rule == "required" and isinstance(expected, list):
        # Required schema field names are safe; do not include instance values.
        fields = [v for v in expected if isinstance(v, str) and _FIELD.fullmatch(v)]
        details["validation_constraint"] = "/".join(fields[:8])[:160]
    message = ("Tool was not executed: arguments do not match the declared input schema. "
               + json.dumps(details, ensure_ascii=False, separators=(",", ":"))
               + ". Correct the indicated arguments before calling again; do not repeat unchanged arguments.")
    return message, details
