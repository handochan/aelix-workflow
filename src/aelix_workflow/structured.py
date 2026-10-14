"""Structured results: a JSON Schema subset, data paths, conditions and item templates.

Plan-time checks raise WorkflowError so a bad plan is refused before any child runs.
Run-time checks return a message (or a boolean) instead of raising, because they judge
agent-authored data rather than the plan.
"""

from __future__ import annotations

import json
import math
import re
from typing import Any

from aelix_workflow.errors import WorkflowError

SCHEMA_KEYWORDS = frozenset(
    {
        "type",
        "description",
        "properties",
        "required",
        "additionalProperties",
        "items",
        "enum",
        "minItems",
        "maxItems",
        "minLength",
        "maxLength",
        "minimum",
        "maximum",
    }
)
_BY_TYPE = {
    "object": {"properties", "required", "additionalProperties"},
    "array": {"items", "minItems", "maxItems"},
    "string": {"minLength", "maxLength"},
    "number": {"minimum", "maximum"},
    "integer": {"minimum", "maximum"},
    "boolean": set(),
    "null": set(),
}
MAX_SCHEMA_BYTES = 4_000
MAX_SCHEMA_DEPTH = 6
MAX_SUBSTITUTION_BYTES = 2_000
UNARY_OPS = frozenset({"exists", "empty", "nonempty", "true", "false"})
BINARY_OPS = frozenset({"equals", "not_equals", "contains", "gt", "gte", "lt", "lte"})
_NUMERIC_OPS = frozenset({"gt", "gte", "lt", "lte"})
_SEGMENT = re.compile(r"[A-Za-z0-9_-]{1,64}\Z")
_PLACEHOLDER = re.compile(r"\{\{\s*(index|item(?:\.[A-Za-z0-9_-]{1,64}){0,8})\s*\}\}")
CHECK_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"passed": {"type": "boolean"}, "exit_code": {"type": "integer"}},
}


class _Missing:
    def __repr__(self) -> str:
        return "MISSING"


MISSING: Any = _Missing()


def utf8_len(text: str) -> int:
    return len(text.encode("utf-8", "surrogatepass"))


def clip_bytes(text: str, limit: int, marker: str = "…") -> str:
    """Cut at a character boundary so the result, marker included, fits ``limit`` bytes."""
    if utf8_len(text) <= limit:
        return text
    room = max(0, limit - utf8_len(marker))
    return text.encode("utf-8", "surrogatepass")[:room].decode("utf-8", "ignore") + marker


def compact(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


# --- Schema subset -------------------------------------------------------------------


def check_schema(schema: Any, where: str) -> None:
    if not isinstance(schema, dict):
        raise WorkflowError(f"{where} must be a JSON Schema object.")
    try:
        size = utf8_len(json.dumps(schema, ensure_ascii=False))
    except (TypeError, ValueError) as exc:
        raise WorkflowError(f"{where} is not JSON: {exc}") from None
    if size > MAX_SCHEMA_BYTES:
        raise WorkflowError(f"{where} exceeds {MAX_SCHEMA_BYTES} bytes.")
    _check(schema, where, 0)


def _check(schema: Any, where: str, depth: int) -> None:
    if depth > MAX_SCHEMA_DEPTH:
        raise WorkflowError(f"{where} nests deeper than {MAX_SCHEMA_DEPTH} levels.")
    if not isinstance(schema, dict):
        raise WorkflowError(f"{where} must be a schema object.")
    kind = schema.get("type")
    if kind not in _BY_TYPE:
        raise WorkflowError(f"{where}.type must be one of {sorted(_BY_TYPE)}.")
    unknown = set(schema) - SCHEMA_KEYWORDS
    if unknown:
        raise WorkflowError(
            f"{where} uses unsupported keywords {sorted(unknown)}; the supported subset is "
            f"{sorted(SCHEMA_KEYWORDS)}."
        )
    misplaced = (set(schema) - {"type", "description", "enum"}) - _BY_TYPE[kind]
    if misplaced:
        raise WorkflowError(f"{where}: {sorted(misplaced)} do not apply to type {kind}.")
    if "description" in schema and not isinstance(schema["description"], str):
        raise WorkflowError(f"{where}.description must be a string.")
    if "enum" in schema:
        options = schema["enum"]
        if not isinstance(options, list) or not options:
            raise WorkflowError(f"{where}.enum must be a non-empty array.")
        if any(validate(option, {"type": kind}) for option in options):
            raise WorkflowError(f"{where}.enum values must have type {kind}.")
    for key in ("minItems", "maxItems", "minLength", "maxLength"):
        if key in schema and not _count(schema[key]):
            raise WorkflowError(f"{where}.{key} must be a non-negative integer.")
    for key in ("minimum", "maximum"):
        if key in schema and not _finite(schema[key]):
            raise WorkflowError(f"{where}.{key} must be a finite number.")
    if kind == "object":
        properties = schema.get("properties", {})
        if not isinstance(properties, dict):
            raise WorkflowError(f"{where}.properties must be an object.")
        for name, sub in properties.items():
            if not _SEGMENT.fullmatch(name):
                raise WorkflowError(
                    f"{where}: property name {name!r} must use letters, digits, _ or -."
                )
            _check(sub, f"{where}.{name}", depth + 1)
        required = schema.get("required", [])
        if (
            not isinstance(required, list)
            or any(not isinstance(name, str) or name not in properties for name in required)
            or len(set(required)) != len(required)
        ):
            raise WorkflowError(f"{where}.required must list declared properties once each.")
        if "additionalProperties" in schema and not isinstance(
            schema["additionalProperties"], bool
        ):
            raise WorkflowError(f"{where}.additionalProperties must be true or false.")
    elif kind == "array" and "items" in schema:
        _check(schema["items"], f"{where}[]", depth + 1)


def _count(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _finite(value: Any) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool) and math.isfinite(value)


def _has_type(value: Any, kind: str) -> bool:
    if kind == "object":
        return isinstance(value, dict)
    if kind == "array":
        return isinstance(value, list)
    if kind == "string":
        return isinstance(value, str)
    if kind == "boolean":
        return isinstance(value, bool)
    if kind == "null":
        return value is None
    if not _finite(value):
        return False
    return kind == "number" or isinstance(value, int) or float(value).is_integer()


def _same(left: Any, right: Any) -> bool:
    if isinstance(left, bool) or isinstance(right, bool):
        return type(left) is type(right) and left == right
    return left == right


def validate(value: Any, schema: dict[str, Any], where: str = "$") -> str | None:
    """Return the first violation in ``value``, or None when it satisfies ``schema``."""
    kind = schema["type"]
    if not _has_type(value, kind):
        return f"{where}: expected {kind}"
    if "enum" in schema and not any(_same(value, option) for option in schema["enum"]):
        return f"{where}: must be one of {compact(schema['enum'])}"
    if kind == "object":
        properties = schema.get("properties", {})
        for name in schema.get("required", []):
            if name not in value:
                return f"{where}.{name}: is required"
        for name, item in value.items():
            if name in properties:
                problem = validate(item, properties[name], f"{where}.{name}")
                if problem:
                    return problem
            elif schema.get("additionalProperties") is False:
                return f"{where}.{name}: is not allowed"
    elif kind == "array":
        if len(value) < schema.get("minItems", 0):
            return f"{where}: needs at least {schema['minItems']} items"
        if "maxItems" in schema and len(value) > schema["maxItems"]:
            return f"{where}: allows at most {schema['maxItems']} items"
        if "items" in schema:
            for index, item in enumerate(value):
                problem = validate(item, schema["items"], f"{where}[{index}]")
                if problem:
                    return problem
    elif kind == "string":
        if len(value) < schema.get("minLength", 0):
            return f"{where}: needs at least {schema['minLength']} characters"
        if "maxLength" in schema and len(value) > schema["maxLength"]:
            return f"{where}: allows at most {schema['maxLength']} characters"
    elif kind in {"number", "integer"}:
        if "minimum" in schema and value < schema["minimum"]:
            return f"{where}: must be >= {schema['minimum']}"
        if "maximum" in schema and value > schema["maximum"]:
            return f"{where}: must be <= {schema['maximum']}"
    return None


# --- Paths -----------------------------------------------------------------------------


def split_path(path: Any, where: str, *, allow_empty: bool = False) -> list[str]:
    if not isinstance(path, str):
        raise WorkflowError(f"{where} must be a string path.")
    text = path.strip()
    if not text:
        if allow_empty:
            return []
        raise WorkflowError(f"{where} must name a dependency, e.g. 'review.findings'.")
    segments = text.split(".")
    if len(segments) > 9 or any(not _SEGMENT.fullmatch(s) for s in segments):
        raise WorkflowError(f"{where}: {path!r} is not a dotted path of names or indexes.")
    return segments


def schema_at(schema: dict[str, Any] | None, segments: list[str]) -> dict[str, Any] | None:
    """The schema node a path leads to, or None when the schema does not declare it."""
    node = schema
    for segment in segments:
        if node is None:
            return None
        if node["type"] == "object":
            node = node.get("properties", {}).get(segment)
        elif node["type"] == "array" and segment.isdigit():
            node = node.get("items")
        else:
            return None
    return node


def resolve(value: Any, segments: list[str]) -> Any:
    for segment in segments:
        if isinstance(value, dict) and segment in value:
            value = value[segment]
        elif isinstance(value, list) and segment.isdigit() and int(segment) < len(value):
            value = value[int(segment)]
        else:
            return MISSING
    return value


# --- Conditions ------------------------------------------------------------------------


def check_condition(raw: Any, where: str, *, relative: bool = False) -> dict[str, Any]:
    """Normalize a condition. ``relative`` paths start inside a for_each item."""
    if not isinstance(raw, dict) or set(raw) - {"path", "op", "value"} or "op" not in raw:
        raise WorkflowError(f"{where} must be an object with path, op and an optional value.")
    path = raw.get("path", "")
    split_path(path, f"{where}.path", allow_empty=relative)
    op = raw["op"]
    if op not in UNARY_OPS | BINARY_OPS:
        raise WorkflowError(f"{where}.op must be one of {sorted(UNARY_OPS | BINARY_OPS)}.")
    condition: dict[str, Any] = {"path": path.strip(), "op": op}
    value = raw.get("value")
    if op in UNARY_OPS:
        if value is not None:
            raise WorkflowError(f"{where}: op {op} takes no value.")
        return condition
    if not isinstance(value, str) or len(value) > 500:
        raise WorkflowError(f"{where}: op {op} needs a string value of at most 500 characters.")
    if op in _NUMERIC_OPS:
        try:
            if not math.isfinite(float(value)):
                raise ValueError
        except ValueError:
            raise WorkflowError(f"{where}: op {op} needs a numeric value.") from None
    condition["value"] = value
    return condition


def holds(condition: dict[str, Any], root: Any) -> bool:
    """Evaluate a normalized condition. A missing value is empty and equals nothing."""
    path = condition["path"]
    value = resolve(root, path.split(".") if path else [])
    op = condition["op"]
    if op == "exists":
        return value is not MISSING
    if op in {"empty", "nonempty"}:
        empty = value is MISSING or value is None or value in ("", [], {})
        return empty if op == "empty" else not empty
    if op == "true":
        return value is True
    if op == "false":
        return value is False
    if value is MISSING:
        return op == "not_equals"
    expected = condition["value"]
    if op in {"equals", "not_equals"}:
        same = _matches_text(value, expected)
        return same if op == "equals" else not same
    if op == "contains":
        if isinstance(value, str):
            return expected in value
        return isinstance(value, list) and any(_matches_text(v, expected) for v in value)
    if not _finite(value):
        return False
    bound = float(expected)
    return {
        "gt": value > bound,
        "gte": value >= bound,
        "lt": value < bound,
        "lte": value <= bound,
    }[op]


def _matches_text(value: Any, expected: str) -> bool:
    return value == expected if isinstance(value, str) else compact(value) == expected


def describe(condition: dict[str, Any]) -> str:
    target = condition["path"] or "item"
    if "value" in condition:
        return f"{target} {condition['op']} {condition['value']!r}"
    return f"{target} {condition['op']}"


# --- Item templates --------------------------------------------------------------------


def placeholders(text: str) -> list[list[str]]:
    """Item paths referenced by ``{{item...}}`` placeholders; ``{{index}}`` is omitted."""
    return [
        match.group(1).split(".")[1:]
        for match in _PLACEHOLDER.finditer(text)
        if match.group(1) != "index"
    ]


def render(text: str, item: Any, index: int) -> str:
    """Substitute placeholders once; agent-authored item text is never re-expanded."""

    def substitute(match: re.Match[str]) -> str:
        name = match.group(1)
        if name == "index":
            return str(index)
        value = resolve(item, name.split(".")[1:])
        if value is MISSING:
            return "(missing)"
        rendered = value if isinstance(value, str) else compact(value)
        return clip_bytes(rendered, MAX_SUBSTITUTION_BYTES)

    return _PLACEHOLDER.sub(substitute, text)


# --- Result extraction -----------------------------------------------------------------


def _no_constants(name: str) -> Any:
    raise ValueError(f"{name} is not valid JSON")


_JSON_FENCE = re.compile(r"```json", re.I)


def extract_json(text: str) -> tuple[Any, str]:
    """Return the reply's final ```json block and the reply without it.

    Only the LAST tagged block counts, so an example earlier in the reply can never stand
    in for the result. A reply that is bare JSON is accepted as well.
    """
    fences = list(_JSON_FENCE.finditer(text))
    try:
        if fences:
            start = fences[-1].start()
            body_start = text.find("\n", start)
            if body_start == -1:
                raise ValueError("The final ```json block is empty.")
            end = text.rfind("```", body_start)
            body = text[body_start + 1 : end] if end != -1 else text[body_start + 1 :]
            try:
                value = json.loads(body, parse_constant=_no_constants)
            except ValueError as exc:
                raise ValueError(f"The final ```json block is not valid JSON: {exc}") from None
            remainder = text[:start] + (text[end + 3 :] if end != -1 else "")
            return value, remainder.strip()
        try:
            return json.loads(text.strip(), parse_constant=_no_constants), ""
        except ValueError:
            raise ValueError("The reply does not end with a ```json result block.") from None
    except RecursionError:
        raise ValueError("The JSON result is nested too deeply.") from None
