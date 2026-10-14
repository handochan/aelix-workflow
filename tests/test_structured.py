from __future__ import annotations

import pytest

from aelix_workflow import structured
from aelix_workflow.errors import WorkflowError

FINDINGS = {
    "type": "object",
    "properties": {
        "findings": {
            "type": "array",
            "maxItems": 5,
            "items": {
                "type": "object",
                "properties": {
                    "file": {"type": "string", "minLength": 1},
                    "severity": {"type": "string", "enum": ["low", "high"]},
                    "line": {"type": "integer", "minimum": 1},
                },
                "required": ["file", "severity"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["findings"],
}


def test_supported_schema_subset_is_accepted():
    structured.check_schema(FINDINGS, "schema")


@pytest.mark.parametrize(
    "schema, match",
    [
        ({"type": "object", "pattern": "x"}, "unsupported keywords"),
        ({"type": "string", "items": {"type": "string"}}, "do not apply"),
        ({"type": "object", "required": ["missing"]}, "declared properties"),
        ({"type": "string", "enum": [1]}, "enum values"),
        ({"type": "tuple"}, "type must be one of"),
        ({"type": "object", "properties": {"a b": {"type": "string"}}}, "property name"),
        ({"type": "array", "maxItems": -1}, "non-negative"),
        ([], "JSON Schema object"),
    ],
)
def test_unsupported_or_inconsistent_schemas_are_refused(schema, match):
    with pytest.raises(WorkflowError, match=match):
        structured.check_schema(schema, "schema")


def test_deep_schemas_are_refused():
    schema: dict = {"type": "string"}
    for _ in range(8):
        schema = {"type": "array", "items": schema}
    with pytest.raises(WorkflowError, match="nests deeper"):
        structured.check_schema(schema, "schema")


@pytest.mark.parametrize(
    "value, problem",
    [
        ({"findings": [{"file": "a.py", "severity": "high", "line": 3}]}, None),
        ({"findings": [{"file": "a.py", "severity": "high", "line": 3.0}]}, None),
        ({}, "findings: is required"),
        ({"findings": [{"file": "", "severity": "high"}]}, "at least 1 characters"),
        ({"findings": [{"file": "a", "severity": "urgent"}]}, "must be one of"),
        ({"findings": [{"file": "a", "severity": "low", "line": 0}]}, ">= 1"),
        ({"findings": [{"file": "a", "severity": "low", "extra": 1}]}, "not allowed"),
        ({"findings": [{"file": "a", "severity": "low", "line": True}]}, "expected integer"),
        ({"findings": [{"file": "a", "severity": "low"}] * 6}, "at most 5 items"),
    ],
)
def test_validation_reports_the_first_violation(value, problem):
    result = structured.validate(value, FINDINGS)
    if problem is None:
        assert result is None
    else:
        assert result is not None and problem in result


def test_paths_resolve_against_data_and_schemas():
    data = {"findings": [{"file": "a.py"}]}
    assert structured.resolve(data, ["findings", "0", "file"]) == "a.py"
    assert structured.resolve(data, ["findings", "3"]) is structured.MISSING
    node = structured.schema_at(FINDINGS, ["findings", "0", "severity"])
    assert node is not None and node["type"] == "string"
    assert structured.schema_at(FINDINGS, ["nope"]) is None
    with pytest.raises(WorkflowError):
        structured.split_path("findings..file", "path")


@pytest.mark.parametrize(
    "condition, root, expected",
    [
        ({"path": "r.items", "op": "nonempty"}, {"r": {"items": [1]}}, True),
        ({"path": "r.items", "op": "empty"}, {"r": {"items": []}}, True),
        ({"path": "r.missing", "op": "empty"}, {"r": {}}, True),
        ({"path": "r.missing", "op": "exists"}, {"r": {}}, False),
        ({"path": "r.ok", "op": "true"}, {"r": {"ok": True}}, True),
        ({"path": "r.ok", "op": "true"}, {"r": {"ok": 1}}, False),
        ({"path": "r.level", "op": "equals", "value": "high"}, {"r": {"level": "high"}}, True),
        ({"path": "r.count", "op": "equals", "value": "3"}, {"r": {"count": 3}}, True),
        ({"path": "r.count", "op": "gt", "value": "2.5"}, {"r": {"count": 3}}, True),
        ({"path": "r.count", "op": "gt", "value": "2"}, {"r": {"count": True}}, False),
        ({"path": "r.tags", "op": "contains", "value": "auth"}, {"r": {"tags": ["auth"]}}, True),
        ({"path": "r.gone", "op": "not_equals", "value": "x"}, {"r": {}}, True),
        ({"path": "", "op": "true"}, True, True),
    ],
)
def test_conditions(condition, root, expected):
    normalized = structured.check_condition(condition, "when", relative=True)
    assert structured.holds(normalized, root) is expected


@pytest.mark.parametrize(
    "raw, match",
    [
        ({"path": "r", "op": "between"}, "op must be one of"),
        ({"path": "r", "op": "empty", "value": "x"}, "takes no value"),
        ({"path": "r", "op": "gt", "value": "high"}, "numeric value"),
        ({"path": "r", "op": "equals"}, "needs a string value"),
        ({"path": "", "op": "true"}, "must name a dependency"),
    ],
)
def test_condition_validation(raw, match):
    with pytest.raises(WorkflowError, match=match):
        structured.check_condition(raw, "when")


def test_templates_substitute_once_and_bound_each_value():
    item = {"file": "a.py", "claim": "{{index}} is not expanded", "nested": {"n": 2}}
    text = "#{{index}} {{ item.file }}: {{item.claim}} {{item.nested}} {{item.none}} {{other}}"
    assert structured.render(text, item, 4) == (
        '#4 a.py: {{index}} is not expanded {"n":2} (missing) {{other}}'
    )
    assert structured.placeholders(text) == [["file"], ["claim"], ["nested"], ["none"]]
    long = structured.render("{{item}}", "가" * 5_000, 1)
    assert structured.utf8_len(long) <= structured.MAX_SUBSTITUTION_BYTES


def test_only_the_final_json_block_is_the_result():
    reply = 'Example:\n```json\n{"example": true}\n```\nResult:\n```json\n{"ok": 1}\n```\nDone.'
    assert structured.extract_json(reply) == (
        {"ok": 1},
        'Example:\n```json\n{"example": true}\n```\nResult:\n\nDone.',
    )
    with pytest.raises(ValueError, match="not valid JSON"):
        structured.extract_json('```json\n{"ok": 1}\n```\n```json\n{broken\n```')
    assert structured.extract_json('{"bare": [1, 2]}') == ({"bare": [1, 2]}, "")
    assert structured.extract_json('Text\n```JSON\n{"open": true}')[0] == {"open": True}
    with pytest.raises(ValueError):
        structured.extract_json('```json\n{"n": NaN}\n```')
    with pytest.raises(ValueError, match="does not end"):
        structured.extract_json("No structured result here.")


def test_byte_clipping_never_splits_a_character():
    clipped = structured.clip_bytes("한국어" * 10, 10, "…")
    assert structured.utf8_len(clipped) <= 10
    assert clipped.endswith("…")
    clipped.encode("utf-8")


def test_case_folding_text_before_the_result_does_not_shift_it():
    reply = 'İİİ TAMAM.\n```json\n{"ok": 1}\n```'
    assert structured.extract_json(reply) == ({"ok": 1}, "İİİ TAMAM.")
    with pytest.raises(ValueError, match="nested too deeply"):
        structured.extract_json("```json\n" + "[" * 100_000 + "]" * 100_000 + "\n```")
