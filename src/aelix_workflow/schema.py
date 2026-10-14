"""Provider-portable schemas with conditional requirements checked in the handler."""

_CONDITION_OPS = [
    "exists",
    "empty",
    "nonempty",
    "true",
    "false",
    "equals",
    "not_equals",
    "contains",
    "gt",
    "gte",
    "lt",
    "lte",
]


def _condition(description: str) -> dict:
    return {
        "type": "object",
        "description": description,
        "properties": {
            "path": {"type": "string"},
            "op": {"type": "string", "enum": _CONDITION_OPS},
            "value": {
                "type": "string",
                "description": "Comparison text; non-strings compare as compact JSON, e.g. '3'.",
            },
        },
        "required": ["path", "op"],
        "additionalProperties": False,
    }


TASK_SCHEMA = {
    "type": "object",
    "properties": {
        "id": {"type": "string", "description": "Stable task ID (letters, digits, _ and -)."},
        "title": {"type": "string"},
        "kind": {
            "type": "string",
            "enum": ["agent", "check"],
            "description": "agent (default) delegates to a profile. check runs a user-approved "
            "command (no shell) and passes on exit code 0.",
        },
        "prompt": {
            "type": "string",
            "description": "agent: self-contained job with acceptance criteria. for_each "
            "prompts may use {{item}}, {{item.field}} and {{index}}.",
        },
        "profile": {
            "type": "string",
            "description": "agent: an existing user/global Aelix agent profile.",
        },
        "access": {
            "type": "string",
            "enum": ["read", "write"],
            "description": "write if the task must change files; it fails as read_only when "
            "its child had no write authority.",
        },
        "writes": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Project-relative paths/globs a write task may change. Overlapping "
            "writers never run together; omitted means exclusive.",
        },
        "command": {
            "type": "array",
            "items": {"type": "string"},
            "description": 'check: program and arguments, e.g. ["uv", "run", "pytest", "-q"].',
        },
        "output_schema": {
            "type": "object",
            "description": "agent: JSON Schema (type, properties, required, items, enum, "
            "min/max bounds) for the child's final ```json block; enables for_each/when.",
        },
        "for_each": {
            "type": "string",
            "description": "'<dependency>.<field>' naming an array result; one child per item "
            "(IDs <id>#1, <id>#2, ...). title and prompt may use {{item}}, {{item.field}} and "
            "{{index}}. Tasks that depend on this task wait for every item and receive all "
            "item results.",
        },
        "where": _condition("for_each item filter; path is relative to the item ('' = item)."),
        "max_items": {"type": "integer", "minimum": 1, "maximum": 16},
        "when": _condition("Run only if this holds for a dependency result; otherwise skip."),
        "repair": {
            "type": "object",
            "description": "check: on failure, rerun this upstream agent task with the failure "
            "output, then re-check.",
            "properties": {
                "task": {"type": "string"},
                "max_rounds": {"type": "integer", "minimum": 1, "maximum": 3},
            },
            "required": ["task"],
            "additionalProperties": False,
        },
        "depends_on": {
            "type": "array",
            "items": {"type": "string"},
            "description": "IDs of tasks whose results this task needs. Name a for_each task "
            "itself, not its items.",
        },
        "phase": {"type": "string"},
        "timeout_ms": {
            "type": "integer",
            "minimum": 1_000,
            "maximum": 1_800_000,
            "description": "Optional. Agents default to their profile's limit (Aelix: 600000); "
            "checks default to 600000.",
        },
    },
    "required": ["id", "title"],
    "additionalProperties": False,
}

PLAN_SCHEMA = {
    "type": "object",
    "properties": {
        "action": {
            "type": "string",
            "enum": ["create", "add", "revise", "retry", "skip", "cancel", "finish"],
        },
        "run_id": {"type": "string"},
        "goal": {"type": "string"},
        "tasks": {"type": "array", "items": TASK_SCHEMA, "minItems": 1, "maxItems": 64},
        "task_ids": {"type": "array", "items": {"type": "string"}},
        "reason": {"type": "string", "description": "Required for add/revise/retry/skip."},
        "summary": {
            "type": "string",
            "description": "Required for finish; describe verified scope.",
        },
        "max_concurrency": {"type": "integer", "minimum": 1, "maximum": 4},
    },
    "required": ["action"],
    "additionalProperties": False,
}

RUN_SCHEMA = {
    "type": "object",
    "properties": {
        "run_id": {"type": "string"},
        "stop_on_failure": {
            "type": "boolean",
            "description": "Start no new task after the first failure (default false).",
        },
    },
    "required": ["run_id"],
    "additionalProperties": False,
}

STATUS_SCHEMA = {
    "type": "object",
    "properties": {
        "run_id": {"type": "string"},
        "task_id": {
            "type": "string",
            "description": "Show one task's attempts, result data and tool trail.",
        },
    },
    "additionalProperties": False,
}
