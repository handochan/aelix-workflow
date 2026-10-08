"""Validated task graphs, revisions, and serializable execution evidence."""

from __future__ import annotations

import copy
import re
from dataclasses import asdict, dataclass, field
from typing import Any, Literal
from uuid import uuid4

TaskState = Literal["pending", "running", "succeeded", "failed", "interrupted", "skipped"]
RunState = Literal[
    "ready", "running", "needs_attention", "awaiting_review", "completed", "cancelled"
]
MAX_TASKS = 64
MAX_ATTEMPTS = 32
MAX_PROMPT_BYTES = 24_000
SUMMARY_LIMIT = 4_000
TASK_STATES = {"pending", "running", "succeeded", "failed", "interrupted", "skipped"}
RUN_STATES = {"ready", "running", "needs_attention", "awaiting_review", "completed", "cancelled"}
_ID = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,47}\Z")


class WorkflowError(ValueError):
    """A rejected workflow operation; no partial graph mutation is committed."""


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


@dataclass(frozen=True)
class TaskSpec:
    id: str
    title: str
    prompt: str
    profile: str
    depends_on: tuple[str, ...] = ()
    phase: str = "Work"
    timeout_ms: int = 120_000

    @classmethod
    def parse(cls, raw: Any) -> TaskSpec:
        if not isinstance(raw, dict):
            raise WorkflowError("Each task must be an object.")
        allowed = {"id", "title", "prompt", "profile", "depends_on", "phase", "timeout_ms"}
        if raw.keys() - allowed:
            raise WorkflowError(f"Unknown task fields: {sorted(raw.keys() - allowed)}")
        task_id = text_field(raw.get("id"), "task.id", limit=48)
        if not _ID.fullmatch(task_id):
            raise WorkflowError("task.id accepts letters, digits, underscores, and hyphens.")
        deps = raw.get("depends_on", [])
        if not isinstance(deps, list) or any(not isinstance(d, str) for d in deps):
            raise WorkflowError("depends_on must be an array of task IDs.")
        if len(deps) != len(set(deps)):
            raise WorkflowError("depends_on contains duplicate task IDs.")
        return cls(
            id=task_id,
            title=text_field(raw.get("title"), "task.title", limit=300),
            prompt=text_field(raw.get("prompt"), "task.prompt"),
            profile=text_field(raw.get("profile"), "task.profile", limit=200),
            depends_on=tuple(deps),
            phase=text_field(raw.get("phase", "Work"), "task.phase", limit=100),
            timeout_ms=integer(raw.get("timeout_ms", 120_000), "timeout_ms", 1_000, 600_000),
        )

    def to_dict(self) -> dict[str, Any]:
        return {**asdict(self), "depends_on": list(self.depends_on)}


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

    @classmethod
    def restore(cls, raw: Any, number: int) -> Attempt:
        if not isinstance(raw, dict) or set(raw) != set(cls.__dataclass_fields__):
            raise WorkflowError("Invalid attempt record fields.")
        if raw.get("number") != number or isinstance(raw.get("number"), bool):
            raise WorkflowError("Attempt numbers must be sequential.")
        if raw.get("status") not in {
            "running",
            "ok",
            "error",
            "timeout",
            "aborted",
            "declined",
            "interrupted",
        }:
            raise WorkflowError("Invalid attempt status.")
        for key, limit in (
            ("summary", SUMMARY_LIMIT),
            ("error", 1_000),
            ("child_id", 200),
            ("model", 300),
        ):
            value = raw[key]
            if key == "summary" and value is None:
                raise WorkflowError("Attempt summary must be a string.")
            if value is not None and (not isinstance(value, str) or len(value) > limit):
                raise WorkflowError(f"Invalid attempt {key}.")
        for key in ("elapsed_ms", "input_tokens", "output_tokens"):
            integer(raw[key], key, 0, 1_000_000_000_000)
        return cls(**raw)


@dataclass
class Task:
    spec: TaskSpec
    state: TaskState = "pending"
    attempts: list[Attempt] = field(default_factory=list)
    note: str = ""
    current_tool: str | None = None
    elapsed_ms: int = 0
    context_tokens: int = 0

    @property
    def output(self) -> str:
        if self.state == "succeeded" and self.attempts:
            return self.attempts[-1].summary
        return ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "spec": self.spec.to_dict(),
            "state": self.state,
            "attempts": [asdict(a) for a in self.attempts],
            "note": self.note,
        }


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

    def ready(self) -> list[Task]:
        return [
            task
            for task in self.tasks.values()
            if task.state == "pending"
            and all(self.tasks[d].state == "succeeded" for d in task.spec.depends_on)
        ]

    def waiting_reason(self, task: Task) -> str:
        if task.state != "pending":
            return task.note
        waiting = [d for d in task.spec.depends_on if self.tasks[d].state != "succeeded"]
        return f"waiting for {', '.join(waiting)}" if waiting else "ready"

    @property
    def attempt_count(self) -> int:
        return sum(len(t.attempts) for t in self.tasks.values())

    def refresh_state(self) -> None:
        if self.state in {"completed", "cancelled"}:
            return
        states = {task.state for task in self.tasks.values()}
        if "running" in states:
            self.state = "running"
        elif self.ready():
            self.state = "ready"
        elif states & {"pending", "failed", "interrupted"}:
            self.state = "needs_attention"
        else:
            self.state = "awaiting_review"

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
                previous = candidate[task_id]
                previous.spec = task.spec
                previous.state = "pending"
                previous.note = note
                affected.add(task_id)
            else:
                if task_id in candidate:
                    raise WorkflowError(f"Task {task_id} already exists; use revise.")
                candidate[task_id] = task
        # Revisions invalidate every dependent result, including completed work.
        while True:
            more = {
                tid
                for tid, task in candidate.items()
                if affected.intersection(task.spec.depends_on)
            } - affected
            if not more:
                break
            affected.update(more)
        for task_id in affected:
            candidate[task_id].state = "pending"
            candidate[task_id].note = note
        proposed = copy.copy(self)
        proposed.tasks = candidate
        proposed.validate()
        self.tasks = candidate
        self._record_revision(note)

    def retry(self, task_ids: Any, reason: Any) -> None:
        self._editable(allow_cancelled=True)
        note = text_field(reason, "reason", limit=1_000)
        selected = self._select(task_ids)
        if any(t.state not in {"failed", "interrupted"} for t in selected):
            raise WorkflowError("Only failed or interrupted tasks can be retried.")
        for task in selected:
            task.state = "pending"
            task.note = note
        self._record_revision(note)

    def skip(self, task_ids: Any, reason: Any) -> None:
        self._editable()
        note = text_field(reason, "reason", limit=1_000)
        selected = self._select(task_ids)
        if any(t.state not in {"pending", "failed", "interrupted"} for t in selected):
            raise WorkflowError("Only pending, failed, or interrupted tasks can be skipped.")
        for task in selected:
            task.state = "skipped"
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
        for task in self.tasks.values():
            if task.state in {"pending", "running"}:
                task.state = "interrupted"
                task.note = "Workflow cancelled; review side effects before retrying."
                if task.attempts and task.attempts[-1].status == "running":
                    task.attempts[-1].status = "interrupted"
        self.state = "cancelled"

    def _select(self, ids: Any) -> list[Task]:
        if not isinstance(ids, list) or not ids or any(not isinstance(t, str) for t in ids):
            raise WorkflowError("task_ids must be a non-empty array of IDs.")
        if len(set(ids)) != len(ids) or any(t not in self.tasks for t in ids):
            raise WorkflowError("task_ids contains duplicate or unknown IDs.")
        return [self.tasks[t] for t in ids]

    def _editable(self, *, allow_cancelled: bool = False) -> None:
        if any(t.state == "running" for t in self.tasks.values()):
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

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
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
        if not isinstance(raw, dict) or raw.get("schema_version") != 1:
            raise WorkflowError("Unsupported workflow snapshot version.")
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
            task = run.tasks[item["spec"]["id"]]
            if item.get("state") not in TASK_STATES:
                raise WorkflowError("Invalid task state.")
            task.state = item["state"]
            task.note = str(item.get("note", ""))[:1_000]
            attempts = item.get("attempts", [])
            if not isinstance(attempts, list) or len(attempts) > MAX_ATTEMPTS:
                raise WorkflowError("Invalid attempt history.")
            task.attempts = [Attempt.restore(a, n) for n, a in enumerate(attempts, 1)]
            if task.state == "succeeded" and (
                not task.attempts or task.attempts[-1].status != "ok"
            ):
                raise WorkflowError("Successful task is missing its successful attempt evidence.")
            if task.state == "running":
                task.state = "interrupted"
                task.note = "Execution interrupted; review side effects before explicit retry."
                if task.attempts:
                    task.attempts[-1].status = "interrupted"
        if run.attempt_count > MAX_ATTEMPTS:
            raise WorkflowError("Snapshot exceeds the workflow attempt budget.")
        if run.state == "completed" and (
            not run.summary
            or not any(t.state == "succeeded" for t in run.tasks.values())
            or any(t.state not in {"succeeded", "skipped"} for t in run.tasks.values())
        ):
            raise WorkflowError("Completed snapshot has unresolved work or no conclusion.")
        if run.state == "running":
            run.state = "needs_attention"
        run.validate()
        run.refresh_state()
        return run
