"""Validated task graphs, dynamic expansion, revisions, and serializable execution evidence."""

from __future__ import annotations

import copy
import dataclasses
import math
import re
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any, Literal
from uuid import uuid4

from aelix_workflow import structured
from aelix_workflow.errors import WorkflowError
from aelix_workflow.failures import DESCRIPTIONS
from aelix_workflow.workspace import overlaps

__all__ = ["Attempt", "Task", "TaskSpec", "Workflow", "WorkflowError", "conflicts"]

TaskState = Literal["pending", "running", "succeeded", "failed", "interrupted", "skipped"]
RunState = Literal[
    "ready", "running", "needs_attention", "awaiting_review", "completed", "cancelled"
]
SCHEMA_VERSION = 2
MAX_TASKS = 64
MAX_ATTEMPTS = 128
MAX_ITEMS = 16
MAX_PROMPT_BYTES = 24_000
MAX_ITEM_PROMPT_BYTES = MAX_PROMPT_BYTES
MAX_TIMEOUT_MS = 1_800_000
CHECK_TIMEOUT_MS = 600_000
SUMMARY_LIMIT = 4_000
TRAIL_LIMIT = 4_096
MAX_DATA_BYTES = 16_000
REPAIR_CONTEXT_LIMIT = 4_000
TASK_STATES = frozenset({"pending", "running", "succeeded", "failed", "interrupted", "skipped"})
RUN_STATES = frozenset(
    {"ready", "running", "needs_attention", "awaiting_review", "completed", "cancelled"}
)
ATTEMPT_STATUSES = frozenset(
    {"running", "ok", "error", "timeout", "aborted", "declined", "interrupted"}
)
FAILURE_KINDS = frozenset(DESCRIPTIONS)
TRIGGERS = frozenset({"run", "retry", "auto", "repair"})
WRITE_TOOLS = frozenset({"edit", "write", "bash"})
_ID = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,47}\Z")
_SPEC_FIELDS = frozenset(
    {
        "id",
        "title",
        "prompt",
        "profile",
        "depends_on",
        "phase",
        "timeout_ms",
        "kind",
        "access",
        "writes",
        "command",
        "output_schema",
        "for_each",
        "where",
        "max_items",
        "when",
        "repair",
    }
)
_RUN_KEYS = frozenset(
    {
        "schema_version",
        "id",
        "goal",
        "max_concurrency",
        "revision",
        "state",
        "summary",
        "revisions",
        "tasks",
    }
)
_TASK_KEYS = frozenset(
    {
        "spec",
        "state",
        "note",
        "attempts",
        "bypassed",
        "rounds",
        "repair_context",
        "next_trigger",
        "retired",
        "retired_cost",
        "generation",
        "items",
    }
)
_ITEM_KEYS = frozenset({"index", "item", "state", "note", "attempts", "next_trigger"})
_V1_ATTEMPT_FIELDS = frozenset(
    {
        "number",
        "status",
        "child_id",
        "summary",
        "error",
        "elapsed_ms",
        "input_tokens",
        "output_tokens",
        "model",
    }
)


def text_field(value: Any, name: str, *, limit: int = MAX_PROMPT_BYTES) -> str:
    if not isinstance(value, str) or not value.strip():
        raise WorkflowError(f"{name} must be a non-empty string.")
    if len(value.encode("utf-8")) > limit:
        raise WorkflowError(f"{name} exceeds {limit} UTF-8 bytes.")
    return value.strip()


def integer(value: Any, name: str, low: int, high: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise WorkflowError(f"{name} must be an integer between {low} and {high}.")
    return value


def _command(raw: Any, where: str) -> tuple[str, ...]:
    if (
        not isinstance(raw, list)
        or not 1 <= len(raw) <= 32
        or any(not isinstance(arg, str) for arg in raw)
        or not raw[0].strip()
    ):
        raise WorkflowError(
            f"{where}: command must be an array of 1-32 strings, program first (no shell)."
        )
    for arg in raw:
        if len(arg.encode("utf-8")) > 2_000 or any(ord(c) < 32 or ord(c) == 127 for c in arg):
            raise WorkflowError(f"{where}: command arguments must be printable, under 2000 bytes.")
    return tuple(raw)


def _writes(raw: Any, where: str) -> tuple[str, ...]:
    if not isinstance(raw, list) or len(raw) > 16 or any(not isinstance(p, str) for p in raw):
        raise WorkflowError(f"{where}: writes must be an array of at most 16 path globs.")
    scope: list[str] = []
    for entry in raw:
        path = entry.strip().replace("\\", "/")
        while path.startswith("./"):
            path = path[2:]
        if (
            not path
            or len(path.encode("utf-8")) > 300
            or path.startswith("/")
            or re.match(r"[A-Za-z]:", path)
            or ".." in path.split("/")
            or any(ord(c) < 32 for c in path)
        ):
            raise WorkflowError(
                f"{where}: writes entries must be relative paths inside the project."
            )
        scope.append(path + "**" if path.endswith("/") else path)
    if len(set(scope)) != len(scope):
        raise WorkflowError(f"{where}: writes contains duplicates.")
    return tuple(scope)


def _repair(raw: Any, where: str) -> dict[str, Any]:
    if not isinstance(raw, dict) or set(raw) - {"task", "max_rounds"} or "task" not in raw:
        raise WorkflowError(f"{where}: repair must be {{'task': <task id>, 'max_rounds': 1-3}}.")
    target = raw["task"]
    if not isinstance(target, str) or not _ID.fullmatch(target):
        raise WorkflowError(f"{where}: repair.task must be a task ID.")
    rounds = integer(raw.get("max_rounds", 2), "repair.max_rounds", 1, 3)
    return {"task": target, "max_rounds": rounds}


@dataclass(frozen=True)
class TaskSpec:
    id: str
    title: str
    prompt: str = ""
    profile: str = ""
    depends_on: tuple[str, ...] = ()
    phase: str = "Work"
    timeout_ms: int | None = None
    kind: str = "agent"
    access: str = "read"
    writes: tuple[str, ...] = ()
    command: tuple[str, ...] = ()
    output_schema: dict[str, Any] | None = None
    for_each: str | None = None
    where: dict[str, Any] | None = None
    max_items: int = 8
    when: dict[str, Any] | None = None
    repair: dict[str, Any] | None = None

    @classmethod
    def parse(cls, raw: Any) -> TaskSpec:
        if not isinstance(raw, dict):
            raise WorkflowError("Each task must be an object.")
        if raw.keys() - _SPEC_FIELDS:
            raise WorkflowError(f"Unknown task fields: {sorted(raw.keys() - _SPEC_FIELDS)}")
        # Providers often send explicit nulls for optional fields.
        raw = {key: value for key, value in raw.items() if value is not None}
        task_id = text_field(raw.get("id"), "task.id", limit=48)
        if not _ID.fullmatch(task_id):
            raise WorkflowError("task.id accepts letters, digits, underscores, and hyphens.")
        where = f"Task {task_id}"
        deps = raw.get("depends_on", [])
        if not isinstance(deps, list) or any(not isinstance(d, str) for d in deps):
            raise WorkflowError("depends_on must be an array of task IDs.")
        if len(deps) != len(set(deps)):
            raise WorkflowError("depends_on contains duplicate task IDs.")
        kind = raw.get("kind", "agent")
        if kind not in {"agent", "check"}:
            raise WorkflowError(f"{where}: kind must be 'agent' or 'check'.")
        common: dict[str, Any] = {
            "id": task_id,
            "title": text_field(raw.get("title"), "task.title", limit=300),
            "depends_on": tuple(deps),
            "phase": text_field(raw.get("phase", "Work"), "task.phase", limit=100),
            # Unset leaves an agent's limit to its profile (Aelix: 600 s), as the agent tool does.
            "timeout_ms": (
                integer(raw["timeout_ms"], "timeout_ms", 1_000, MAX_TIMEOUT_MS)
                if "timeout_ms" in raw
                else None
            ),
            "kind": kind,
        }
        when = raw.get("when")
        if when is not None:
            common["when"] = structured.check_condition(when, f"{where}.when")
        if kind == "check":
            agent_only = raw.keys() & {
                "prompt",
                "profile",
                "access",
                "writes",
                "output_schema",
                "for_each",
                "where",
                "max_items",
            }
            if agent_only:
                raise WorkflowError(
                    f"{where}: a check runs a command and does not accept {sorted(agent_only)}."
                )
            repair = raw.get("repair")
            return cls(
                **common,
                command=_command(raw.get("command"), where),
                repair=_repair(repair, where) if repair is not None else None,
            )
        if raw.keys() & {"command", "repair"}:
            raise WorkflowError(f"{where}: command and repair belong to check tasks.")
        writes = _writes(raw.get("writes", []), where)
        access = raw.get("access", "write" if writes else "read")
        if access not in {"read", "write"}:
            raise WorkflowError(f"{where}: access must be 'read' or 'write'.")
        if writes and access != "write":
            raise WorkflowError(f"{where}: writes requires access 'write'.")
        schema = raw.get("output_schema")
        if schema is not None:
            structured.check_schema(schema, f"{where}.output_schema")
        for_each = raw.get("for_each")
        if for_each is None and raw.keys() & {"where", "max_items"}:
            raise WorkflowError(f"{where}: where and max_items require for_each.")
        if for_each is not None:
            structured.split_path(for_each, f"{where}.for_each")
        filter_ = raw.get("where")
        return cls(
            **common,
            prompt=text_field(raw.get("prompt"), "task.prompt"),
            profile=text_field(raw.get("profile"), "task.profile", limit=200),
            access=access,
            writes=writes,
            output_schema=schema,
            for_each=for_each.strip() if for_each is not None else None,
            where=(
                structured.check_condition(filter_, f"{where}.where", relative=True)
                if filter_ is not None
                else None
            ),
            max_items=integer(raw.get("max_items", 8), "max_items", 1, MAX_ITEMS),
        )

    def to_dict(self) -> dict[str, Any]:
        """Only non-default fields, so ordinary snapshots stay compact."""
        data: dict[str, Any] = {}
        for item in dataclasses.fields(self):
            value = getattr(self, item.name)
            if item.name in {"id", "title"} or value != item.default:
                data[item.name] = list(value) if isinstance(value, tuple) else value
        return data


@dataclass
class Attempt:
    number: int
    status: str = "running"
    child_id: str | None = None
    summary: str = ""
    error: str | None = None
    elapsed_ms: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    model: str | None = None
    failure: str | None = None
    trigger: str = "run"
    cost: float = 0.0
    permission_mode: str | None = None
    dropped_tools: list[str] = field(default_factory=list)
    tool_trail: str | None = None
    truncated: bool = False
    data: Any = None
    exit_code: int | None = None
    scope_violations: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)

    @classmethod
    def restore(cls, raw: Any, number: int) -> Attempt:
        if not isinstance(raw, dict) or set(raw) != _ATTEMPT_FIELDS:
            raise WorkflowError("Invalid attempt record fields.")
        if raw["number"] != number or isinstance(raw["number"], bool):
            raise WorkflowError("Attempt numbers must be sequential.")
        if raw["status"] not in ATTEMPT_STATUSES:
            raise WorkflowError("Invalid attempt status.")
        if raw["failure"] is not None and raw["failure"] not in FAILURE_KINDS:
            raise WorkflowError("Invalid attempt failure kind.")
        if raw["trigger"] not in TRIGGERS:
            raise WorkflowError("Invalid attempt trigger.")
        if not isinstance(raw["summary"], str):
            raise WorkflowError("Attempt summary must be a string.")
        for key, limit in (
            ("summary", SUMMARY_LIMIT),
            ("error", 1_000),
            ("child_id", 200),
            ("model", 300),
            ("permission_mode", 40),
            ("tool_trail", TRAIL_LIMIT),
        ):
            value = raw[key]
            if value is not None and (not isinstance(value, str) or len(value) > limit):
                raise WorkflowError(f"Invalid attempt {key}.")
        for key in ("elapsed_ms", "input_tokens", "output_tokens"):
            integer(raw[key], key, 0, 1_000_000_000_000)
        cost = raw["cost"]
        if isinstance(cost, bool) or not isinstance(cost, int | float) or not math.isfinite(cost):
            raise WorkflowError("Invalid attempt cost.")
        if cost < 0:
            raise WorkflowError("Invalid attempt cost.")
        for key, limit in (("dropped_tools", 32), ("scope_violations", 20)):
            value = raw[key]
            if not isinstance(value, list) or len(value) > limit:
                raise WorkflowError(f"Invalid attempt {key}.")
            if any(not isinstance(v, str) or len(v) > 400 for v in value):
                raise WorkflowError(f"Invalid attempt {key}.")
        if not isinstance(raw["truncated"], bool):
            raise WorkflowError("Invalid attempt truncation flag.")
        if raw["exit_code"] is not None:
            integer(raw["exit_code"], "exit_code", -(2**31), 2**32)
        if raw["data"] is not None and _too_large(raw["data"]):
            raise WorkflowError("Attempt data exceeds its size budget.")
        return cls(**raw)


_ATTEMPT_FIELDS = frozenset(item.name for item in dataclasses.fields(Attempt))
_ATTEMPT_DEFAULTS = {
    key: value for key, value in Attempt(0).to_dict().items() if key not in _V1_ATTEMPT_FIELDS
}


def _too_large(value: Any) -> bool:
    try:
        return structured.utf8_len(structured.compact(value)) > MAX_DATA_BYTES
    except (TypeError, ValueError, RecursionError):
        return True


@dataclass
class Task:
    spec: TaskSpec
    state: TaskState = "pending"
    attempts: list[Attempt] = field(default_factory=list)
    note: str = ""
    bypassed: bool = False
    rounds: int = 0
    repair_context: str = ""
    next_trigger: str = "run"
    items: list[Task] | None = None
    item: Any = None
    index: int = 0
    retired: int = 0
    retired_cost: float = 0.0
    # Each for_each expansion gets a new generation, and its items carry it, so a
    # re-expanded "verify#1" is never mistaken for an earlier expansion's item.
    generation: int = 0
    # Live progress only; never persisted.
    current_tool: str | None = None
    elapsed_ms: int = 0
    context_tokens: int = 0

    @property
    def id(self) -> str:
        return self.spec.id

    @property
    def group(self) -> bool:
        """A for_each template; its items are the tasks that actually run."""
        return self.spec.for_each is not None

    @property
    def latest(self) -> Attempt | None:
        return self.attempts[-1] if self.attempts else None

    @property
    def output(self) -> str:
        if self.group:
            if self.state != "succeeded" or not self.items:
                return ""
            return "\n".join(f"[{i.id}] {i.output}" for i in self.items if i.state == "succeeded")
        if self.state == "succeeded" and self.attempts:
            return self.attempts[-1].summary
        return ""

    @property
    def data(self) -> Any:
        if self.state != "succeeded":
            return None
        if self.group:
            return [i.data for i in self.items or [] if i.state == "succeeded"]
        return self.attempts[-1].data if self.attempts else None

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "spec": self.spec.to_dict(),
            "state": self.state,
            "attempts": [a.to_dict() for a in self.attempts],
            "note": self.note,
        }
        if self.bypassed:
            data["bypassed"] = True
        if self.rounds:
            data["rounds"] = self.rounds
        if self.repair_context:
            data["repair_context"] = self.repair_context
        if self.next_trigger != "run":
            data["next_trigger"] = self.next_trigger
        if self.retired:
            data["retired"] = self.retired
            data["retired_cost"] = self.retired_cost
        if self.generation:
            data["generation"] = self.generation
        if self.items is not None:
            data["items"] = [
                {
                    "index": child.index,
                    "item": child.item,
                    "state": child.state,
                    "note": child.note,
                    "attempts": [a.to_dict() for a in child.attempts],
                    "next_trigger": child.next_trigger,
                }
                for child in self.items
            ]
        return data


def conflicts(left: Task, right: Task) -> bool:
    """Whether two tasks must not run at the same time in the shared working tree."""
    a, b = left.spec, right.spec
    if a.kind == "check" or b.kind == "check":
        # A verification must observe a tree that no writer is changing.
        other = b if a.kind == "check" else a
        return other.kind == "check" or other.access == "write"
    return a.access == "write" and b.access == "write" and overlaps(a.writes, b.writes)


def _stale(task: Task) -> bool:
    if task.state in {"succeeded", "pending"}:
        return True
    if task.state == "skipped":
        return task.bypassed
    last = task.latest
    return (
        task.spec.kind == "check"
        and task.state == "failed"
        and last is not None
        and last.failure in {"check_failed", "timeout"}
    )


def _reset(task: Task, note: str, *, keep_rounds: bool = False) -> None:
    if task.items is not None:
        task.retired += sum(len(i.attempts) for i in task.items)
        task.retired_cost += sum(a.cost for i in task.items for a in i.attempts)
        task.items = None
    task.state = "pending"
    task.note = note
    task.bypassed = False
    task.next_trigger = "run"
    if not keep_rounds:
        task.rounds = 0
        task.repair_context = ""


@dataclass
class Workflow:
    id: str
    goal: str
    tasks: dict[str, Task]
    max_concurrency: int = 2
    revision: int = 1
    state: RunState = "ready"
    summary: str = ""
    revisions: list[str] = field(default_factory=list)

    @classmethod
    def create(cls, goal: Any, tasks: Any, max_concurrency: Any = 2) -> Workflow:
        run = cls(
            id=uuid4().hex[:12],
            goal=text_field(goal, "goal", limit=4_000),
            tasks=cls._parse_tasks(tasks),
            max_concurrency=integer(max_concurrency, "max_concurrency", 1, 4),
        )
        run.validate()
        return run

    @staticmethod
    def _parse_tasks(raw: Any) -> dict[str, Task]:
        if not isinstance(raw, list) or not raw or len(raw) > MAX_TASKS:
            raise WorkflowError(f"tasks must contain between 1 and {MAX_TASKS} task objects.")
        tasks: dict[str, Task] = {}
        for item in raw:
            spec = TaskSpec.parse(item)
            if spec.id in tasks:
                raise WorkflowError(f"Duplicate task ID: {spec.id}")
            tasks[spec.id] = Task(spec=spec)
        return tasks

    # --- Structure ---------------------------------------------------------------------

    def iter_all(self) -> Iterator[Task]:
        for task in self.tasks.values():
            yield task
            if task.items:
                yield from task.items

    def units(self) -> Iterator[Task]:
        """Tasks that hold their own state: everything except expanded groups."""
        for task in self.iter_all():
            if task.items is None:
                yield task

    @property
    def node_count(self) -> int:
        return sum(1 for _ in self.iter_all())

    def find(self, task_id: Any) -> Task:
        if isinstance(task_id, str):
            if task_id in self.tasks:
                return self.tasks[task_id]
            base, mark, number = task_id.rpartition("#")
            if mark and base in self.tasks and number.isdigit():
                items = self.tasks[base].items or []
                if 1 <= int(number) <= len(items):
                    return items[int(number) - 1]
        raise WorkflowError(f"Unknown task {task_id}.")

    def ancestors(self, task_id: str) -> set[str]:
        seen: set[str] = set()
        stack = list(self.tasks[task_id].spec.depends_on)
        while stack:
            current = stack.pop()
            if current not in seen:
                seen.add(current)
                stack.extend(self.tasks[current].spec.depends_on)
        return seen

    @staticmethod
    def _dependents(tasks: dict[str, Task], roots: set[str]) -> set[str]:
        found = set(roots)
        while True:
            more = {
                tid for tid, task in tasks.items() if found.intersection(task.spec.depends_on)
            } - found
            if not more:
                return found - roots
            found |= more

    def validate(self) -> None:
        if not 1 <= len(self.tasks) <= MAX_TASKS:
            raise WorkflowError(f"A workflow may contain at most {MAX_TASKS} tasks.")
        visiting: set[str] = set()
        visited: set[str] = set()

        def visit(task_id: str) -> None:
            if task_id in visiting:
                raise WorkflowError(f"Dependency cycle at task {task_id}.")
            if task_id in visited:
                return
            visiting.add(task_id)
            for dependency in self.tasks[task_id].spec.depends_on:
                if dependency not in self.tasks:
                    raise WorkflowError(f"Task {task_id} depends on unknown task {dependency}.")
                visit(dependency)
            visiting.remove(task_id)
            visited.add(task_id)

        for task_id in self.tasks:
            visit(task_id)
        for task in self.tasks.values():
            self._validate_dynamic(task.spec)

    def result_schema(self, task_id: str) -> dict[str, Any] | None:
        """The shape of a task's structured result, as successors see it."""
        spec = self.tasks[task_id].spec
        if spec.kind == "check":
            return structured.CHECK_SCHEMA
        if spec.for_each is not None:
            return {"type": "array", "items": spec.output_schema or {"type": "null"}}
        return spec.output_schema

    def _validate_dynamic(self, spec: TaskSpec) -> None:
        where = f"Task {spec.id}"
        if spec.for_each is not None:
            source, *rest = spec.for_each.split(".")
            if source not in spec.depends_on:
                raise WorkflowError(f"{where}: for_each must start with one of its dependencies.")
            schema = self.result_schema(source)
            if schema is None:
                raise WorkflowError(
                    f"{where}: {source} declares no output_schema, so it has nothing to iterate."
                )
            target = structured.schema_at(schema, rest)
            if target is None or target["type"] != "array":
                raise WorkflowError(
                    f"{where}: {spec.for_each} is not an array in {source}'s output_schema."
                )
            item_schema = target.get("items")
            for segments in [
                *structured.placeholders(spec.prompt),
                *structured.placeholders(spec.title),
            ]:
                if segments and structured.schema_at(item_schema, segments) is None:
                    raise WorkflowError(
                        f"{where}: placeholder item.{'.'.join(segments)} is not in the item schema."
                    )
            if spec.where is not None and spec.where["path"]:
                segments = spec.where["path"].split(".")
                if structured.schema_at(item_schema, segments) is None:
                    raise WorkflowError(f"{where}: where.path is not in the item schema.")
        if spec.when is not None:
            source, *rest = spec.when["path"].split(".")
            if source not in spec.depends_on:
                raise WorkflowError(f"{where}: when.path must start with one of its dependencies.")
            schema = self.result_schema(source)
            if schema is None:
                raise WorkflowError(
                    f"{where}: {source} declares no output_schema, so when has nothing to test."
                )
            if rest and structured.schema_at(schema, rest) is None:
                raise WorkflowError(f"{where}: when.path is not in {source}'s output_schema.")
        if spec.repair is not None:
            target_id = spec.repair["task"]
            if target_id not in self.ancestors(spec.id):
                raise WorkflowError(
                    f"{where}: repair.task must be a direct or indirect dependency of the check."
                )
            target = self.tasks[target_id].spec
            if target.kind != "agent" or target.for_each is not None:
                raise WorkflowError(f"{where}: repair.task must be a single agent task.")

    # --- Readiness and dynamic steps ---------------------------------------------------

    def satisfied(self, task_id: str) -> bool:
        dependency = self.tasks[task_id]
        return dependency.state == "succeeded" or (
            dependency.state == "skipped" and dependency.bypassed
        )

    def deps_met(self, task: Task) -> bool:
        return all(self.satisfied(d) for d in task.spec.depends_on)

    def _when(self, task: Task) -> bool:
        condition = task.spec.when
        if condition is None:
            return True
        source = condition["path"].split(".")[0]
        dependency = self.tasks[source]
        root = {source: dependency.data} if dependency.state == "succeeded" else {}
        return structured.holds(condition, root)

    def ready(self) -> list[Task]:
        ready: list[Task] = []
        for task in self.tasks.values():
            if task.group:
                ready.extend(i for i in task.items or [] if i.state == "pending")
            elif task.state == "pending" and self.deps_met(task) and self._when(task):
                ready.append(task)
        return ready

    def waiting_reason(self, task: Task) -> str:
        if task.state != "pending":
            return task.note
        if task.index:
            return task.note or "ready"
        waiting = [d for d in task.spec.depends_on if not self.satisfied(d)]
        if waiting:
            blocked = [d for d in waiting if self.tasks[d].state in {"failed", "skipped"}]
            if blocked:
                return f"blocked by {', '.join(blocked)}"
            return f"waiting for {', '.join(waiting)}"
        holders = [
            other.id
            for other in self.units()
            if other.state == "running" and conflicts(task, other)
        ]
        if holders:
            return f"waiting for {', '.join(holders)} (shared files)"
        return task.note or "ready"

    def advance(self) -> list[str]:
        """Apply zero-cost graph steps: false conditions skip, satisfied for_each expand."""
        events: list[str] = []
        progressed = True
        while progressed:
            progressed = False
            for task in self.tasks.values():
                if task.state != "pending" or task.items is not None or not self.deps_met(task):
                    continue
                if not self._when(task):
                    task.state = "skipped"
                    task.bypassed = True
                    task.note = f"Condition not met: {structured.describe(task.spec.when or {})}"
                    events.append(f"{task.id} skipped: condition not met")
                    progressed = True
                elif task.group:
                    events.append(self._expand(task))
                    progressed = True
            self.sync()
        return events

    def _expand(self, group: Task) -> str:
        path = group.spec.for_each or ""
        source, *rest = path.split(".")
        dependency = self.tasks[source]
        if dependency.state != "succeeded":
            group.state = "skipped"
            group.bypassed = True
            group.note = f"{source} was skipped, so there is nothing to iterate."
            return f"{group.id} skipped: {source} was skipped"
        value = structured.resolve(dependency.data, rest)
        if not isinstance(value, list):
            group.state = "failed"
            group.note = f"for_each {path} did not produce an array."
            return f"{group.id} failed: {group.note}"
        where = group.spec.where
        selected = [v for v in value if where is None or structured.holds(where, v)]
        if len(selected) > group.spec.max_items:
            group.state = "failed"
            group.note = (
                f"for_each produced {len(selected)} items but max_items is "
                f"{group.spec.max_items}. Narrow it with where or raise max_items (up to "
                f"{MAX_ITEMS})."
            )
            return f"{group.id} failed: too many items"
        if self.node_count + len(selected) > MAX_TASKS:
            group.state = "failed"
            group.note = f"Expanding {len(selected)} items would exceed {MAX_TASKS} tasks."
            return f"{group.id} failed: task limit"
        group.generation += 1
        group.items = [self._item(group, v, n) for n, v in enumerate(selected, 1)]
        filtered = len(value) - len(selected)
        group.note = f"{len(selected)} item(s) from {path}" + (
            f", {filtered} filtered out by where" if filtered else ""
        )
        return f"{group.id} expanded into {len(selected)} item(s)"

    @staticmethod
    def _item(group: Task, value: Any, number: int) -> Task:
        title = structured.render(group.spec.title, value, number)
        if title == group.spec.title and (name := structured.label(value)):
            title = f"{title} · {name}"
        spec = dataclasses.replace(
            group.spec,
            id=f"{group.id}#{number}",
            title=structured.clip_bytes(title, 300),
            prompt=structured.clip_bytes(
                structured.render(group.spec.prompt, value, number), MAX_ITEM_PROMPT_BYTES
            ),
            for_each=None,
            where=None,
            max_items=8,
            when=None,
        )
        return Task(spec=spec, item=value, index=number, generation=group.generation)

    def sync(self) -> None:
        """Derive each expanded group's state from its items."""
        for task in self.tasks.values():
            if task.items is None:
                continue
            states = {i.state for i in task.items}
            for state in ("running", "pending", "failed", "interrupted", "succeeded"):
                if state in states:
                    task.state = state  # type: ignore[assignment]
                    break
            else:
                task.state = "skipped" if task.items else "succeeded"

    def refresh_state(self) -> None:
        if self.state in {"completed", "cancelled"}:
            return
        self.sync()
        states = {task.state for task in self.tasks.values()}
        if "running" in states:
            self.state = "running"
        elif any(t.state == "pending" and self.deps_met(t) for t in self.tasks.values()):
            self.state = "ready"
        elif states & {"pending", "failed", "interrupted"}:
            self.state = "needs_attention"
        else:
            self.state = "awaiting_review"

    # --- Accounting --------------------------------------------------------------------

    @property
    def attempt_count(self) -> int:
        return sum(len(t.attempts) + t.retired for t in self.iter_all())

    @property
    def cost(self) -> float:
        return sum(sum(a.cost for a in t.attempts) + t.retired_cost for t in self.iter_all())

    # --- Mutations ---------------------------------------------------------------------

    def amend(self, tasks: Any, *, replace: bool, reason: Any) -> None:
        self._editable()
        note = text_field(reason, "reason", limit=1_000)
        changes = self._parse_tasks(tasks)
        candidate = copy.deepcopy(self.tasks)
        affected: set[str] = set()
        for task_id, task in changes.items():
            if replace:
                if task_id not in candidate:
                    raise WorkflowError(f"Cannot revise unknown task {task_id}; use add.")
                candidate[task_id].spec = task.spec
                affected.add(task_id)
            else:
                if task_id in candidate:
                    raise WorkflowError(f"Task {task_id} already exists; use revise.")
                candidate[task_id] = task
        # Revisions invalidate every dependent result, including completed work.
        affected |= self._dependents(candidate, affected)
        for task_id in affected:
            _reset(candidate[task_id], note)
        proposed = copy.copy(self)
        proposed.tasks = candidate
        proposed.validate()
        if proposed.node_count > MAX_TASKS:
            raise WorkflowError(f"A workflow may contain at most {MAX_TASKS} tasks and items.")
        self.tasks = candidate
        self._record_revision(note)

    def retry(self, task_ids: Any, reason: Any) -> None:
        self._editable(allow_cancelled=True)
        note = text_field(reason, "reason", limit=1_000)
        targets: list[Task] = []
        for task in self._select(task_ids):
            if task.items is not None:
                failed = [i for i in task.items if i.state in {"failed", "interrupted"}]
                if not failed:
                    raise WorkflowError(f"{task.id} has no failed or interrupted items.")
                targets.extend(failed)
            elif task.state in {"failed", "interrupted"}:
                targets.append(task)
            else:
                raise WorkflowError("Only failed or interrupted tasks can be retried.")
        for task in targets:
            task.state = "pending"
            task.note = note
            task.next_trigger = "retry"
            if task.spec.kind == "check":
                task.rounds = 0
        self._record_revision(note)

    def skip(self, task_ids: Any, reason: Any) -> None:
        self._editable()
        note = text_field(reason, "reason", limit=1_000)
        targets: list[Task] = []
        for task in self._select(task_ids):
            units = task.items if task.items is not None else [task]
            open_units = [u for u in units if u.state in {"pending", "failed", "interrupted"}]
            if not open_units:
                raise WorkflowError("Only pending, failed, or interrupted tasks can be skipped.")
            targets.extend(open_units)
        for task in targets:
            task.state = "skipped"
            task.bypassed = False
            task.note = note
        self._record_revision(note)

    def finish(self, summary: Any) -> None:
        self._editable()
        if any(t.state not in {"succeeded", "skipped"} for t in self.tasks.values()):
            raise WorkflowError(
                "Unfinished tasks remain. Resolve, retry, revise, or skip them first."
            )
        if not any(t.state == "succeeded" for t in self.tasks.values()):
            raise WorkflowError("A workflow with no successful work cannot be completed.")
        self.summary = text_field(summary, "summary", limit=SUMMARY_LIMIT)
        self.state = "completed"

    def cancel(self) -> None:
        if self.state == "completed":
            raise WorkflowError("A completed workflow cannot be cancelled.")
        for task in self.units():
            if task.state in {"pending", "running"}:
                task.state = "interrupted"
                task.note = "Workflow cancelled; review side effects before retrying."
                if task.attempts and task.attempts[-1].status == "running":
                    task.attempts[-1].status = "interrupted"
                    task.attempts[-1].failure = "interrupted"
        self.sync()
        self.state = "cancelled"

    def pause(self, note: str) -> None:
        """Stop in place: only work that was running becomes interrupted."""
        for task in self.units():
            if task.state == "running":
                task.state = "interrupted"
                task.note = note
                task.current_tool = None
                if task.attempts and task.attempts[-1].status == "running":
                    task.attempts[-1].status = "interrupted"
                    task.attempts[-1].failure = "interrupted"
        self.sync()
        if self.state not in {"completed", "cancelled"}:
            self.state = "needs_attention"

    def repair(self, check: Task) -> str:
        """Rerun a failed check's repair target with the failure output, then re-check."""
        plan = check.spec.repair
        if plan is None or check.state != "failed":
            return "none"
        if check.rounds >= plan["max_rounds"]:
            return "exhausted"
        target = self.tasks[plan["task"]]
        affected = {target.id} | self._dependents(self.tasks, {target.id})
        if any(t.state == "running" for tid in affected for t in self._family(tid)):
            return "deferred"
        last = check.attempts[-1]
        rounds = check.rounds + 1
        command = " ".join(check.spec.command)
        # Results built on the old target rerun. Declined, manually skipped, interrupted or
        # failed agent work is a decision or a side-effect risk, so it waits for the parent.
        stale = {task_id for task_id in affected if _stale(self.tasks[task_id])} | {check.id}
        context = (
            f"Verification {check.id} (`{command}`) failed with exit code {last.exit_code} "
            f"(repair round {rounds}/{plan['max_rounds']}). Output tail:\n{last.summary}"
        )
        note = f"Repair round {rounds}/{plan['max_rounds']} after {check.id} failed"
        for task_id in stale:
            _reset(self.tasks[task_id], note, keep_rounds=True)
        check.rounds = rounds
        target.repair_context = structured.clip_bytes(context, REPAIR_CONTEXT_LIMIT)
        target.next_trigger = "repair"
        self.sync()
        return "applied"

    def _family(self, task_id: str) -> list[Task]:
        task = self.tasks[task_id]
        return [task, *(task.items or [])]

    def _select(self, ids: Any) -> list[Task]:
        if not isinstance(ids, list) or not ids or any(not isinstance(t, str) for t in ids):
            raise WorkflowError("task_ids must be a non-empty array of IDs.")
        if len(set(ids)) != len(ids):
            raise WorkflowError("task_ids contains duplicate or unknown IDs.")
        try:
            return [self.find(t) for t in ids]
        except WorkflowError:
            raise WorkflowError("task_ids contains duplicate or unknown IDs.") from None

    def _editable(self, *, allow_cancelled: bool = False) -> None:
        if any(t.state == "running" for t in self.units()):
            raise WorkflowError("Wait for the current execution before changing its graph.")
        if self.state == "completed" or (self.state == "cancelled" and not allow_cancelled):
            raise WorkflowError(
                f"Workflow is {self.state}; create a new workflow or retry interruption."
            )

    def _record_revision(self, note: str) -> None:
        self.revision += 1
        self.revisions.append(note)
        self.revisions = self.revisions[-32:]
        self.summary = ""
        self.state = "ready"
        self.refresh_state()

    # --- Persistence -------------------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "id": self.id,
            "goal": self.goal,
            "max_concurrency": self.max_concurrency,
            "revision": self.revision,
            "state": self.state,
            "summary": self.summary,
            "revisions": list(self.revisions),
            "tasks": [task.to_dict() for task in self.tasks.values()],
        }

    @classmethod
    def restore(cls, raw: Any) -> Workflow:
        if isinstance(raw, dict) and raw.get("schema_version") == 1:
            raw = _migrate_v1(raw)
        if not isinstance(raw, dict) or raw.get("schema_version") != SCHEMA_VERSION:
            raise WorkflowError("Unsupported workflow snapshot version.")
        if set(raw) - _RUN_KEYS:
            raise WorkflowError("Unknown workflow snapshot fields.")
        items = raw.get("tasks")
        if not isinstance(items, list) or any(not isinstance(t, dict) for t in items):
            raise WorkflowError("Invalid workflow snapshot tasks.")
        run = cls.create(
            raw.get("goal"), [t.get("spec") for t in items], raw.get("max_concurrency")
        )
        run.id = text_field(raw.get("id"), "run_id", limit=48)
        if not _ID.fullmatch(run.id):
            raise WorkflowError("Invalid workflow ID.")
        run.revision = integer(raw.get("revision"), "revision", 1, 1_000_000)
        if raw.get("state") not in RUN_STATES:
            raise WorkflowError("Invalid workflow state.")
        run.state = raw["state"]
        run.summary = str(raw.get("summary", ""))[:SUMMARY_LIMIT]
        revisions = raw.get("revisions", [])
        if not isinstance(revisions, list) or any(not isinstance(n, str) for n in revisions):
            raise WorkflowError("Invalid revision history.")
        run.revisions = revisions[-32:]
        for item in items:
            if set(item) - _TASK_KEYS:
                raise WorkflowError("Unknown workflow task fields.")
            task = run.tasks[item["spec"]["id"]]
            _restore_state(task, item)
            _restore_extras(task, item)
            if task.group and item.get("items") is None and task.state == "succeeded":
                raise WorkflowError("A succeeded for_each task must keep its items.")
            raw_items = item.get("items")
            if raw_items is not None:
                if not task.group or not isinstance(raw_items, list):
                    raise WorkflowError("Only for_each tasks have items.")
                if len(raw_items) > task.spec.max_items:
                    raise WorkflowError("A for_each task has more items than max_items.")
                task.items = []
                for number, entry in enumerate(raw_items, 1):
                    if not isinstance(entry, dict) or set(entry) - _ITEM_KEYS:
                        raise WorkflowError("Invalid for_each item record.")
                    if entry.get("index") != number or _too_large(entry.get("item")):
                        raise WorkflowError("Invalid for_each item record.")
                    child = run._item(task, entry.get("item"), number)
                    _restore_state(child, entry)
                    task.items.append(child)
        if run.node_count > MAX_TASKS:
            raise WorkflowError("Snapshot exceeds the workflow task limit.")
        if run.attempt_count > MAX_ATTEMPTS:
            raise WorkflowError("Snapshot exceeds the workflow attempt budget.")
        run.sync()
        if run.state == "completed" and (
            not run.summary
            or not any(t.state == "succeeded" for t in run.tasks.values())
            or any(t.state not in {"succeeded", "skipped"} for t in run.tasks.values())
        ):
            raise WorkflowError("Completed snapshot has unresolved work or no conclusion.")
        if run.state == "running":
            run.state = "needs_attention"
        run.refresh_state()
        return run


def _restore_state(task: Task, raw: dict[str, Any]) -> None:
    if raw.get("state") not in TASK_STATES:
        raise WorkflowError("Invalid task state.")
    task.state = raw["state"]
    task.note = str(raw.get("note", ""))[:1_000]
    attempts = raw.get("attempts", [])
    if not isinstance(attempts, list) or len(attempts) > MAX_ATTEMPTS:
        raise WorkflowError("Invalid attempt history.")
    task.attempts = [Attempt.restore(a, n) for n, a in enumerate(attempts, 1)]
    task.next_trigger = raw.get("next_trigger", "run")
    if task.next_trigger not in TRIGGERS:
        raise WorkflowError("Invalid task trigger.")
    if (
        task.state == "succeeded"
        and not task.group
        and (not task.attempts or task.attempts[-1].status != "ok")
    ):
        raise WorkflowError("Successful task is missing its successful attempt evidence.")
    for attempt in task.attempts:
        if attempt.status == "running":
            attempt.status = "interrupted"
            attempt.failure = "interrupted"
            if task.state in {"running", "pending"} and attempt is task.attempts[-1]:
                task.state = "running"
    if task.state == "running":
        task.state = "interrupted"
        task.note = "Execution interrupted; review side effects before explicit retry."


def _restore_extras(task: Task, raw: dict[str, Any]) -> None:
    bypassed = raw.get("bypassed", False)
    if not isinstance(bypassed, bool) or (bypassed and task.state != "skipped"):
        raise WorkflowError("Invalid condition skip record.")
    task.bypassed = bypassed
    limit = task.spec.repair["max_rounds"] if task.spec.repair else 0
    task.rounds = integer(raw.get("rounds", 0), "rounds", 0, limit)
    context = raw.get("repair_context", "")
    if not isinstance(context, str) or len(context.encode("utf-8")) > REPAIR_CONTEXT_LIMIT:
        raise WorkflowError("Invalid repair context.")
    task.repair_context = context
    task.retired = integer(raw.get("retired", 0), "retired", 0, MAX_ATTEMPTS)
    cost = raw.get("retired_cost", 0.0)
    if isinstance(cost, bool) or not isinstance(cost, int | float) or not math.isfinite(cost):
        raise WorkflowError("Invalid retired cost.")
    task.retired_cost = max(0.0, float(cost))
    task.generation = integer(raw.get("generation", 0), "generation", 0, 1_000_000)
    if task.group and raw.get("items") is not None and task.generation < 1:
        raise WorkflowError("An expanded for_each task needs its expansion generation.")


_V1_DEFAULT_TIMEOUT_MS = 120_000


def _migrate_v1(raw: dict[str, Any]) -> dict[str, Any]:
    """Version 1 attempts lack failure kinds and the evidence fields added in version 2."""
    tasks = raw.get("tasks")
    if not isinstance(tasks, list) or any(not isinstance(t, dict) for t in tasks):
        raise WorkflowError("Invalid workflow snapshot tasks.")
    migrated = []
    for task in tasks:
        if set(task) - {"spec", "state", "attempts", "note"}:
            raise WorkflowError("Unknown workflow task fields.")
        attempts = task.get("attempts", [])
        if not isinstance(attempts, list):
            raise WorkflowError("Invalid attempt history.")
        upgraded = []
        for attempt in attempts:
            if not isinstance(attempt, dict) or set(attempt) != _V1_ATTEMPT_FIELDS:
                raise WorkflowError("Invalid attempt record fields.")
            status = attempt.get("status")
            failure = None
            if status not in {"ok", "running"}:
                failure = status if status in FAILURE_KINDS else "error"
            upgraded.append({**copy.deepcopy(_ATTEMPT_DEFAULTS), **attempt, "failure": failure})
        spec = task.get("spec")
        timeout = spec.get("timeout_ms") if isinstance(spec, dict) else None
        if type(timeout) is int and timeout == _V1_DEFAULT_TIMEOUT_MS:
            # Version 1 wrote every field, so its default is indistinguishable from a choice;
            # like an unset limit now, it defers to the profile.
            spec = {key: value for key, value in spec.items() if key != "timeout_ms"}
        migrated.append({**task, "spec": spec, "attempts": upgraded})
    return {**raw, "schema_version": SCHEMA_VERSION, "tasks": migrated}
