# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# Copyright (c) 2026 Phidipus Agents (see NOTICE)
"""
utils/json_utils.py — Phidipus v1.0
Strict JSON parsing, safe serialisation, and structural schema validation.

Design:
  - All loads are strict: trailing commas, comments, and NaN/Infinity are
    rejected (Python's json.loads already handles this correctly).
  - Dumps never emit NaN or Infinity (both are JSON-invalid); they raise
    ValueError instead.
  - Schema validation is implemented entirely in stdlib.  It supports the
    subset of JSON Schema required by Phidipus:
      type, required, properties, additionalProperties,
      items, minLength, maxLength, minimum, maximum, enum, pattern.
  - No external dependencies.

Used by:
  ipc/action_schema.py   — validate inbound IPC action messages
  ipc/action_log.py      — serialise audit log entries
  memory/episodic_memory.py — serialise/deserialise episodes
  skill_validator/schema_gate.py — validate Docker stdout against output schema
  patcher/runtime_patcher.py — read/write patch ledger entries
"""

from __future__ import annotations

import json
import math
import re
from typing import Any, Union

# ---------------------------------------------------------------------------
# Public type alias
# ---------------------------------------------------------------------------

Schema = dict[str, Any]   # A dict that describes a validation schema


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class JsonDecodeError(ValueError):
    """Raised when JSON parsing fails."""


class JsonSchemaError(ValueError):
    """Raised when a value does not conform to a schema."""


# ---------------------------------------------------------------------------
# Strict JSON load
# ---------------------------------------------------------------------------

# FIX B-02: Python 3.12's json.loads() silently accepts NaN/Infinity.
# These are NOT valid JSON — pre-check and reject them explicitly.
_NAN_INF_RE = re.compile(r'\b(NaN|-?Infinity)\b')


def loads(text: str) -> Any:
    """
    Parse *text* as JSON and return the result.

    Rejects:
      - Trailing commas (Python json module already does this)
      - Comments (// and /* */ are not valid JSON; Python rejects them)
      - NaN, Infinity, -Infinity literal tokens (FIX B-02: explicitly
        rejected via regex pre-check because Python 3.12+ accepts them)

    Args:
        text: JSON string to parse.

    Returns:
        Parsed Python object.

    Raises:
        JsonDecodeError: on any parse failure.
    """
    if not isinstance(text, str):
        raise JsonDecodeError(
            f"Expected str, got {type(text).__name__}"
        )
    # FIX B-02: reject NaN/Infinity before json.loads can accept them
    if _NAN_INF_RE.search(text):
        raise JsonDecodeError(
            "JSON is invalid: NaN and Infinity are not permitted "
            "in strict JSON"
        )
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise JsonDecodeError(str(exc)) from exc


def load_bytes(data: bytes, *, encoding: str = "utf-8") -> Any:
    """
    Decode *data* as *encoding* then parse as JSON.

    Raises:
        JsonDecodeError: on decode or parse failure.
    """
    try:
        text = data.decode(encoding)
    except UnicodeDecodeError as exc:
        raise JsonDecodeError(
            f"Byte sequence is not valid {encoding}: {exc}"
        ) from exc
    return loads(text)


# ---------------------------------------------------------------------------
# Safe JSON dump
# ---------------------------------------------------------------------------

def dumps(
    obj: Any,
    *,
    indent: int | None = None,
    sort_keys: bool = False,
    ensure_ascii: bool = False,
) -> str:
    """
    Serialise *obj* to a JSON string.

    Rejects:
      - NaN and Infinity float values (not valid JSON).
      - Any object type that is not JSON-serialisable (raises ValueError).

    Args:
        obj:          Python object to serialise.
        indent:       Pretty-print with this indentation level.
        sort_keys:    Sort dict keys alphabetically.
        ensure_ascii: Escape non-ASCII characters.

    Returns:
        JSON string.

    Raises:
        ValueError: if *obj* contains NaN, Infinity, or non-serialisable types.
    """
    try:
        return json.dumps(
            obj,
            indent=indent,
            sort_keys=sort_keys,
            ensure_ascii=ensure_ascii,
            allow_nan=False,   # Reject NaN / Infinity — not valid JSON
        )
    except (TypeError, ValueError) as exc:
        raise ValueError(f"JSON serialisation failed: {exc}") from exc


def dumps_bytes(
    obj: Any,
    *,
    encoding: str = "utf-8",
    indent: int | None = None,
    sort_keys: bool = False,
) -> bytes:
    """Serialise *obj* to JSON and encode as *encoding* bytes."""
    return dumps(obj, indent=indent, sort_keys=sort_keys).encode(encoding)


# ---------------------------------------------------------------------------
# Schema validation (stdlib-only, JSON Schema subset)
# ---------------------------------------------------------------------------

# Mapping from JSON Schema type name → Python type(s)
_TYPE_MAP: dict[str, type | tuple[type, ...]] = {
    "string":  str,
    "integer": int,
    "number":  (int, float),
    "boolean": bool,
    "array":   list,
    "object":  dict,
    "null":    type(None),
}


def validate(obj: Any, schema: Schema, *, _path: str = "$") -> None:
    """
    Validate *obj* against a structural *schema*.

    Supported JSON Schema keywords:
      type                 — one of: string, integer, number, boolean, array,
                             object, null.  May be a list of types.
      required             — list of required property names (for objects).
      properties           — per-property sub-schemas (for objects).
      additionalProperties — False to reject unknown keys; or a sub-schema
                             that every additional property must satisfy.
      items                — sub-schema for every array element.
      minLength            — minimum string length (inclusive).
      maxLength            — maximum string length (inclusive).
      minimum              — minimum numeric value (inclusive).
      maximum              — maximum numeric value (inclusive).
      enum                 — list of allowed values (strict equality).
      pattern              — regex pattern a string must fully match.

    Args:
        obj:    Python object to validate.
        schema: Schema dict.

    Raises:
        JsonSchemaError: if *obj* does not conform to *schema*.
        TypeError:       if *schema* is not a dict.
    """
    if not isinstance(schema, dict):
        raise TypeError(f"Schema must be a dict, got {type(schema).__name__}")

    _validate_node(obj, schema, _path)


def _validate_node(obj: Any, schema: Schema, path: str) -> None:
    """Recursive validation worker."""

    # --- type ---
    if "type" in schema:
        type_spec = schema["type"]
        _check_type(obj, type_spec, path)

    # --- enum ---
    if "enum" in schema:
        allowed = schema["enum"]
        if not isinstance(allowed, list):
            raise TypeError(f"Schema 'enum' at {path} must be a list")
        if obj not in allowed:
            raise JsonSchemaError(
                f"{path}: value {obj!r} is not one of {allowed}"
            )

    # --- string constraints ---
    if isinstance(obj, str):
        if "minLength" in schema and len(obj) < schema["minLength"]:
            raise JsonSchemaError(
                f"{path}: string length {len(obj)} is below minLength "
                f"{schema['minLength']}"
            )
        if "maxLength" in schema and len(obj) > schema["maxLength"]:
            raise JsonSchemaError(
                f"{path}: string length {len(obj)} exceeds maxLength "
                f"{schema['maxLength']}"
            )
        if "pattern" in schema:
            pat = schema["pattern"]
            if not re.fullmatch(pat, obj):
                raise JsonSchemaError(
                    f"{path}: string {obj!r} does not match pattern {pat!r}"
                )

    # --- numeric constraints ---
    if isinstance(obj, (int, float)) and not isinstance(obj, bool):
        if "minimum" in schema and obj < schema["minimum"]:
            raise JsonSchemaError(
                f"{path}: value {obj} is below minimum {schema['minimum']}"
            )
        if "maximum" in schema and obj > schema["maximum"]:
            raise JsonSchemaError(
                f"{path}: value {obj} exceeds maximum {schema['maximum']}"
            )

    # --- object constraints ---
    if isinstance(obj, dict):
        # required properties
        required = schema.get("required", [])
        for key in required:
            if key not in obj:
                raise JsonSchemaError(
                    f"{path}: missing required property {key!r}"
                )

        # per-property sub-schemas
        properties = schema.get("properties", {})
        for key, sub_schema in properties.items():
            if key in obj:
                _validate_node(obj[key], sub_schema, f"{path}.{key}")

        # additionalProperties
        if "additionalProperties" in schema:
            ap = schema["additionalProperties"]
            extra_keys = set(obj.keys()) - set(properties.keys())
            if ap is False:
                if extra_keys:
                    raise JsonSchemaError(
                        f"{path}: additional properties not allowed: "
                        f"{sorted(extra_keys)}"
                    )
            elif isinstance(ap, dict):
                for key in extra_keys:
                    _validate_node(obj[key], ap, f"{path}.{key}")

    # --- array constraints ---
    if isinstance(obj, list):
        if "items" in schema:
            item_schema = schema["items"]
            for idx, item in enumerate(obj):
                _validate_node(item, item_schema, f"{path}[{idx}]")


def _check_type(obj: Any, type_spec: Union[str, list], path: str) -> None:
    """Assert *obj* matches *type_spec* (a type name or list of type names)."""
    if isinstance(type_spec, str):
        type_names = [type_spec]
    elif isinstance(type_spec, list):
        type_names = type_spec
    else:
        raise TypeError(
            f"Schema 'type' at {path} must be a string or list, "
            f"got {type(type_spec).__name__}"
        )

    for type_name in type_names:
        if type_name not in _TYPE_MAP:
            raise TypeError(
                f"Unknown JSON Schema type {type_name!r} at {path}. "
                f"Supported: {list(_TYPE_MAP)}"
            )
        expected = _TYPE_MAP[type_name]
        # Special case: bool is a subclass of int in Python; JSON "integer"
        # should NOT accept True/False.
        if type_name == "integer":
            if isinstance(obj, int) and not isinstance(obj, bool):
                return
        elif type_name == "number":
            if isinstance(obj, (int, float)) and not isinstance(obj, bool):
                if not math.isnan(obj) and not math.isinf(obj):
                    return
        else:
            if isinstance(obj, expected):
                return

    # None of the allowed types matched
    actual_type = type(obj).__name__
    raise JsonSchemaError(
        f"{path}: expected type(s) {type_names}, got {actual_type!r} "
        f"(value: {obj!r})"
    )


# ---------------------------------------------------------------------------
# Convenience: load + validate in one call
# ---------------------------------------------------------------------------

def loads_validated(text: str, schema: Schema) -> Any:
    """
    Parse *text* as JSON and validate against *schema*.

    Args:
        text:   JSON string.
        schema: Validation schema dict.

    Returns:
        Parsed and validated Python object.

    Raises:
        JsonDecodeError:  on parse failure.
        JsonSchemaError:  on schema validation failure.
    """
    obj = loads(text)
    validate(obj, schema)
    return obj


def load_bytes_validated(data: bytes, schema: Schema, *, encoding: str = "utf-8") -> Any:
    """
    Decode *data*, parse as JSON, and validate against *schema*.

    Raises:
        JsonDecodeError:  on decode/parse failure.
        JsonSchemaError:  on schema validation failure.
    """
    obj = load_bytes(data, encoding=encoding)
    validate(obj, schema)
    return obj
