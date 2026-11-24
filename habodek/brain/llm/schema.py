"""A small JSON Schema checker for tool arguments.

Covers the subset our tool schemas use: type, properties, required,
additionalProperties, enum, items, minimum/maximum, minItems/maxItems, minLength.
Errors are phrased for the model to read and fix.
"""

from __future__ import annotations

from typing import Any

_TYPES: dict[str, tuple[type, ...]] = {
    "object": (dict,),
    "array": (list,),
    "string": (str,),
    "integer": (int,),
    "number": (int, float),
    "boolean": (bool,),
    "null": (type(None),),
}


def _type_ok(value: Any, expected: str | list[str]) -> bool:
    names = [expected] if isinstance(expected, str) else expected
    for name in names:
        if name in ("integer", "number") and isinstance(value, bool):
            continue
        if isinstance(value, _TYPES.get(name, (object,))):
            return True
    return False


def validate(value: Any, schema: dict[str, Any] | None, path: str = "") -> list[str]:
    """Return a list of human-readable violations (empty when valid)."""
    if not schema:
        return []
    where = path or "arguments"
    errors: list[str] = []
    expected = schema.get("type")
    if expected and not _type_ok(value, expected):
        return [f"{where}: expected {expected}, got {type(value).__name__}"]
    if "enum" in schema and value not in schema["enum"]:
        errors.append(f"{where}: {value!r} is not one of {schema['enum']}")
    if isinstance(value, dict):
        props = schema.get("properties") or {}
        for key in schema.get("required") or []:
            if key not in value:
                errors.append(f"{where}: missing required field {key!r}")
        if schema.get("additionalProperties") is False:
            for key in value:
                if key not in props:
                    errors.append(f"{where}: unexpected field {key!r}")
        for key, sub in props.items():
            if key in value:
                errors.extend(validate(value[key], sub, f"{path}.{key}" if path else key))
    elif isinstance(value, list):
        if "minItems" in schema and len(value) < schema["minItems"]:
            errors.append(f"{where}: needs at least {schema['minItems']} items")
        if "maxItems" in schema and len(value) > schema["maxItems"]:
            errors.append(f"{where}: allows at most {schema['maxItems']} items")
        item_schema = schema.get("items")
        if isinstance(item_schema, dict):
            for i, item in enumerate(value):
                errors.extend(validate(item, item_schema, f"{where}[{i}]"))
    elif isinstance(value, str):
        if "minLength" in schema and len(value) < schema["minLength"]:
            errors.append(f"{where}: must be at least {schema['minLength']} characters")
    elif isinstance(value, (int, float)) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            errors.append(f"{where}: must be >= {schema['minimum']}")
        if "maximum" in schema and value > schema["maximum"]:
            errors.append(f"{where}: must be <= {schema['maximum']}")
    return errors
