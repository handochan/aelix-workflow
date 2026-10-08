"""Provider-portable schemas with conditional requirements checked in the handler."""

TASK_SCHEMA = {
    "type": "object",
    "properties": {
        "id": {"type": "string", "description": "Stable task ID (letters, digits, _ and -)."},
        "title": {"type": "string"},
        "prompt": {"type": "string", "description": "Self-contained job with acceptance criteria."},
        "profile": {
            "type": "string",
            "description": "An existing user/global Aelix agent profile.",
        },
        "depends_on": {"type": "array", "items": {"type": "string"}},
        "phase": {"type": "string"},
        "timeout_ms": {"type": "integer", "minimum": 1_000, "maximum": 600_000},
    },
    "required": ["id", "title", "prompt", "profile"],
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
    "properties": {"run_id": {"type": "string"}},
    "required": ["run_id"],
    "additionalProperties": False,
}

STATUS_SCHEMA = {
    "type": "object",
    "properties": {"run_id": {"type": "string"}},
    "additionalProperties": False,
}
