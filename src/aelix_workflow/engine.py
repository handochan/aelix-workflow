"""Bounded, work-conserving DAG execution over injected delegation and check runners."""

from __future__ import annotations

import asyncio
import math
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

from aelix_coding_agent.subagent_contract import SubagentProgress, SubagentResult

from aelix_workflow import failures, structured
from aelix_workflow.model import (
    MAX_ATTEMPTS,
    MAX_DATA_BYTES,
    SUMMARY_LIMIT,
    TRAIL_LIMIT,
    Attempt,
    Task,
    Workflow,
    conflicts,
)
from aelix_workflow.prompting import build_prompt
from aelix_workflow.workspace import Snapshot, changed, matches

__all__ = [
    "CheckOutcome",
    "CheckRefused",
    "Delegate",
    "Execution",
    "WorkflowEngine",
    "build_prompt",
    "is_aborted",
]

RETRY_DELAYS = (2.0, 8.0)
_INTERRUPTED = "Execution interrupted; review side effects before explicit retry."


class Delegate(Protocol):
    async def __call__(
        self, task: Task, prompt: str, on_event: Callable[[SubagentProgress], None]
    ) -> SubagentResult: ...


class Workspace(Protocol):
    async def snapshot(self) -> Snapshot | None: ...


@dataclass(frozen=True)
class CheckOutcome:
    exit_code: int
    output: str
    elapsed_ms: int = 0
    timed_out: bool = False


class CheckRefused(Exception):
    """The user did not approve a verification command; treated like a declined spawn."""


@dataclass
class Execution:
    """What one workflow_run call did, so its report can show only new evidence."""

    # The objects themselves: a re-expanded item reuses its ID and attempt numbers.
    finished: list[tuple[Task, Attempt]] = field(default_factory=list)
    events: list[str] = field(default_factory=list)
    halted: str | None = None


@dataclass
class _Run:
    run: Workflow
    delegate: Delegate
    changed: Callable[[bool], None]
    admit: Callable[[], bool]
    check: Callable[[Task], Awaitable[CheckOutcome]] | None
    started: Callable[[Task, Attempt], None] | None
    workspace: Workspace | None
    execution: Execution


def is_aborted(signal: Any) -> bool:
    return signal is not None and bool(getattr(signal, "aborted", False))


def _clip(text: str | None, limit: int) -> str | None:
    return text[:limit] if text else None


class WorkflowEngine:
    def __init__(self, *, retry_delays: tuple[float, ...] = RETRY_DELAYS) -> None:
        self.retry_delays = retry_delays
        self._members: dict[asyncio.Task[None], Task] = {}
        self._owner: asyncio.Task[Any] | None = None
        self._halt: str | None = None
        # Declared write scopes of running writers, and the union each one ran alongside.
        self._scope_own: dict[str, frozenset[str] | None] = {}
        self._scope_seen: dict[str, set[str] | None] = {}

    @property
    def active(self) -> bool:
        return self._owner is not None

    async def execute(
        self,
        run: Workflow,
        delegate: Delegate,
        *,
        changed: Callable[[bool], None],
        admit: Callable[[], bool],
        signal: Any = None,
        check: Callable[[Task], Awaitable[CheckOutcome]] | None = None,
        started: Callable[[Task, Attempt], None] | None = None,
        workspace: Workspace | None = None,
        stop_on_failure: bool = False,
    ) -> Execution:
        if self._owner is not None:
            raise ValueError("A workflow execution is already active in this session.")
        if run.state in {"completed", "cancelled"}:
            raise ValueError(
                f"Workflow is {run.state}; create a new one or retry interrupted work."
            )
        self._owner = asyncio.current_task()
        self._halt = None
        self._cancel_members()  # Nothing from an earlier, interrupted execution may linger.
        execution = Execution()
        ctx = _Run(run, delegate, changed, admit, check, started, workspace, execution)
        repairs: set[str] = set()
        try:
            run.state = "running"
            changed(True)
            while True:
                if is_aborted(signal):
                    raise asyncio.CancelledError
                failed_before = {t.id for t in run.tasks.values() if t.state == "failed"}
                events = run.advance()
                if events:
                    execution.events.extend(events)
                    changed(True)
                    newly_failed = [
                        t.id
                        for t in run.tasks.values()
                        if t.state == "failed" and t.id not in failed_before
                    ]
                    if stop_on_failure and newly_failed and self._halt is None:
                        self._halt = f"Stopped after {newly_failed[0]} failed (stop_on_failure)."
                if repairs:
                    self._repair(run, repairs, execution, changed)
                if self._halt is None:
                    self._admit_ready(ctx)
                if not self._members:
                    break
                done, _ = await asyncio.wait(
                    self._members, timeout=0.1, return_when=asyncio.FIRST_COMPLETED
                )
                for member in done:
                    task = self._members.pop(member)
                    member.result()
                    self._settle(task, repairs, stop_on_failure)
                run.sync()
            run.refresh_state()
            if self._halt is not None:
                run.state = "needs_attention"
            execution.halted = self._halt
            changed(True)
            return execution
        except asyncio.CancelledError:
            await self._stop(run, changed, _INTERRUPTED)
            raise
        except BaseException:
            await self._stop(
                run, changed, "Execution stopped by an internal error; review side effects."
            )
            raise
        finally:
            self._owner = None

    async def _stop(self, run: Workflow, changed: Callable[[bool], None], note: str) -> None:
        """Record the pause before waiting, so a second Ctrl+C cannot skip the bookkeeping."""
        members = self._cancel_members()
        run.pause(note)
        changed(True)
        if members:
            try:
                await asyncio.gather(*members, return_exceptions=True)
            finally:
                run.pause(note)
                changed(True)

    # --- Scheduling ----------------------------------------------------------------------

    def _admit_ready(self, ctx: _Run) -> None:
        run = ctx.run
        running = [t for t in run.units() if t.state == "running"]
        for task in run.ready():
            if len(self._members) >= run.max_concurrency:
                return
            if any(conflicts(task, other) for other in running):
                continue
            if run.attempt_count >= MAX_ATTEMPTS:
                self._halt = f"The workflow used all {MAX_ATTEMPTS} attempts."
                return
            if task.spec.kind == "agent" and not ctx.admit():
                self._halt = "The per-prompt delegation budget is exhausted."
                task.note = (
                    "Execution budget exhausted; continue in a new turn or create a new run."
                )
                ctx.changed(True)
                return
            self._launch(ctx, task)
            running.append(task)

    def _launch(self, ctx: _Run, task: Task) -> None:
        task.state = "running"
        task.note = ""
        task.current_tool = None
        task.elapsed_ms = 0
        task.context_tokens = 0
        task.attempts.append(Attempt(number=len(task.attempts) + 1, trigger=task.next_trigger))
        task.next_trigger = "run"
        if task.spec.kind == "agent" and task.spec.access == "write":
            own = frozenset(task.spec.writes) or None
            seen: set[str] | None = None if own is None else set(own)
            for other, other_own in self._scope_own.items():
                if seen is not None:
                    seen = None if other_own is None else seen | other_own
                other_seen = self._scope_seen[other]
                if other_seen is not None:
                    self._scope_seen[other] = None if own is None else other_seen | own
            self._scope_own[task.id] = own
            self._scope_seen[task.id] = seen
        member = asyncio.create_task(self._run_member(ctx, task))
        self._members[member] = task
        ctx.run.sync()  # A group shows running as soon as one of its items does.
        ctx.changed(False)

    def _settle(self, task: Task, repairs: set[str], stop_on_failure: bool) -> None:
        attempt = task.latest
        kind = attempt.failure if attempt else None
        # Only a verification that ran and failed can be repaired; a declined command cannot.
        repairable = task.spec.repair is not None and kind in {"check_failed", "timeout"}
        rounds_left = repairable and task.rounds < (task.spec.repair or {}).get("max_rounds", 0)
        if kind in failures.HALTING and self._halt is None:
            detail = f" ({attempt.error})" if attempt and attempt.error else ""
            self._halt = f"{task.id}: {failures.describe(kind)}{detail}"[:500]
        elif task.state == "failed" and stop_on_failure and not rounds_left and self._halt is None:
            # A failed check with repair rounds left is the plan's own reaction, not a stop.
            self._halt = f"Stopped after {task.id} failed (stop_on_failure)."
        if repairable:
            repairs.add(task.id)

    def _repair(
        self,
        run: Workflow,
        repairs: set[str],
        execution: Execution,
        changed: Callable[[bool], None],
    ) -> None:
        for check_id in sorted(repairs):
            if self._halt is not None:
                repairs.clear()
                return
            check = run.tasks[check_id]
            verdict = run.repair(check)
            if verdict == "deferred":
                continue
            repairs.discard(check_id)
            plan = check.spec.repair or {}
            if verdict == "applied":
                execution.events.append(
                    f"repair round {check.rounds}/{plan.get('max_rounds')}: {check_id} failed, "
                    f"rerunning {plan.get('task')}"
                )
                changed(True)
            elif verdict == "exhausted":
                execution.events.append(
                    f"{check_id} still fails after {plan.get('max_rounds')} repair round(s)"
                )

    # --- Members -------------------------------------------------------------------------

    async def _run_member(self, ctx: _Run, task: Task) -> None:
        try:
            before = await self._snapshot(ctx, task)
            if task.spec.kind == "check":
                await self._run_check(ctx, task)
            else:
                await self._run_agent(ctx, task)
            await self._observe_scope(ctx, task, before)
        finally:
            self._scope_own.pop(task.id, None)
            self._scope_seen.pop(task.id, None)
            task.current_tool = None
            ctx.run.sync()
            ctx.changed(True)

    async def _run_agent(self, ctx: _Run, task: Task) -> None:
        automatic = 0
        while True:
            attempt = task.attempts[-1]
            used_tools = False

            def progress(event: SubagentProgress, attempt: Attempt = attempt) -> None:
                nonlocal used_tools
                if event.id and attempt.child_id is None:
                    attempt.child_id = event.id
                    if ctx.started is not None:
                        ctx.started(task, attempt)
                used_tools = used_tools or bool(event.current_tool)
                task.current_tool = event.current_tool
                task.elapsed_ms = event.elapsed_ms
                task.context_tokens = event.tokens
                attempt.model = event.model or attempt.model
                ctx.changed(False)

            try:
                result = await ctx.delegate(task, build_prompt(ctx.run, task), progress)
            except asyncio.CancelledError:
                _interrupt(task, attempt)
                raise
            except Exception as exc:
                attempt.status = "error"
                attempt.failure = "error"
                attempt.error = str(exc)[:1_000]
                task.state = "failed"
                task.note = str(exc)[:500]
                ctx.execution.finished.append((task, attempt))
                return
            try:
                record_result(task, attempt, result)
            except Exception as exc:
                attempt.status, attempt.failure = "error", "error"
                attempt.error = f"Could not record the result: {exc}"[:1_000]
                task.state = "failed"
                task.note = attempt.error[:500]
            ctx.execution.finished.append((task, attempt))
            delay = self._retry_delay(ctx, task, attempt, used_tools, automatic)
            if delay is None:
                return
            # The slot stays held during the backoff so the retry cannot be starved.
            failed_note = task.note
            task.state = "running"
            task.note = f"Transient failure; retrying automatically in {delay:g}s."
            ctx.changed(False)
            try:
                await asyncio.sleep(delay)
            except asyncio.CancelledError:
                task.state, task.note = "failed", failed_note
                raise
            if self._halt is not None or ctx.run.attempt_count >= MAX_ATTEMPTS or not ctx.admit():
                task.state, task.note = "failed", failed_note
                return
            automatic += 1
            task.note = ""
            task.attempts.append(Attempt(number=len(task.attempts) + 1, trigger="auto"))

    def _retry_delay(
        self, ctx: _Run, task: Task, attempt: Attempt, used_tools: bool, automatic: int
    ) -> float | None:
        """Only a transient failure of a child that ran no tool is retried without asking."""
        if attempt.failure != "transient" or attempt.tool_trail or used_tools:
            return None
        if automatic >= len(self.retry_delays) or self._halt is not None:
            return None
        if ctx.run.attempt_count >= MAX_ATTEMPTS:
            return None
        return self.retry_delays[automatic]

    async def _run_check(self, ctx: _Run, task: Task) -> None:
        attempt = task.attempts[-1]
        if ctx.started is not None:
            ctx.started(task, attempt)
        task.current_tool = "check"
        ctx.changed(False)
        try:
            if ctx.check is None:
                raise CheckRefused("Verification commands are unavailable in this session.")
            outcome = await ctx.check(task)
        except asyncio.CancelledError:
            _interrupt(task, attempt)
            raise
        except CheckRefused as exc:
            attempt.status, attempt.failure = "declined", "declined"
            attempt.error = str(exc)[:1_000]
            task.state = "failed"
            task.note = f"declined: {exc}"[:500]
            ctx.execution.finished.append((task, attempt))
            return
        except Exception as exc:
            attempt.status, attempt.failure = "error", "error"
            attempt.error = str(exc)[:1_000]
            task.state = "failed"
            task.note = str(exc)[:500]
            ctx.execution.finished.append((task, attempt))
            return
        passed = outcome.exit_code == 0 and not outcome.timed_out
        attempt.exit_code = outcome.exit_code
        attempt.elapsed_ms = task.elapsed_ms = max(0, outcome.elapsed_ms)
        output = outcome.output.strip()
        attempt.summary = (
            ("…" + output[-SUMMARY_LIMIT + 1 :]) if len(output) > SUMMARY_LIMIT else output
        )
        attempt.data = {"passed": passed, "exit_code": outcome.exit_code}
        if passed:
            attempt.status, attempt.failure = "ok", None
            task.state, task.note = "succeeded", ""
        else:
            attempt.status = "timeout" if outcome.timed_out else "error"
            attempt.failure = "timeout" if outcome.timed_out else "check_failed"
            last = output.splitlines()[-1] if output else ""
            task.state = "failed"
            task.note = f"{attempt.failure}: exit {outcome.exit_code} {last}"[:500]
        ctx.execution.finished.append((task, attempt))

    # --- Write scopes --------------------------------------------------------------------

    @staticmethod
    async def _snapshot(ctx: _Run, task: Task) -> Snapshot | None:
        spec = task.spec
        if ctx.workspace is None or spec.kind != "agent" or spec.access != "write":
            return None
        if not spec.writes:
            return None
        return await ctx.workspace.snapshot()

    async def _observe_scope(self, ctx: _Run, task: Task, before: Snapshot | None) -> None:
        allowed = self._scope_seen.get(task.id)
        attempt = task.latest
        if before is None or allowed is None or ctx.workspace is None or attempt is None:
            return
        after = await ctx.workspace.snapshot()
        if after is None:
            return
        outside = sorted(path for path in changed(before, after) if not matches(path, allowed))
        attempt.scope_violations = outside[:20]

    # --- Teardown ------------------------------------------------------------------------

    def _cancel_members(self) -> list[asyncio.Task[None]]:
        members = list(self._members)
        self._members.clear()
        self._scope_own.clear()
        self._scope_seen.clear()
        for member in members:
            member.cancel()
        return members

    async def _drain(self) -> None:
        members = self._cancel_members()
        if members:
            await asyncio.gather(*members, return_exceptions=True)

    async def close(self) -> None:
        owner = self._owner
        if owner is not None and owner is not asyncio.current_task():
            owner.cancel()
            await asyncio.gather(owner, return_exceptions=True)
        await self._drain()


def _interrupt(task: Task, attempt: Attempt) -> None:
    attempt.status = "interrupted"
    attempt.failure = "interrupted"
    task.state = "interrupted"
    task.note = _INTERRUPTED


def record_result(task: Task, attempt: Attempt, result: SubagentResult) -> None:
    attempt.child_id = result.id or attempt.child_id
    attempt.status = result.status
    attempt.error = _clip(result.error, 1_000)
    attempt.elapsed_ms = max(0, int(result.elapsed_ms))
    attempt.input_tokens = max(0, int(result.usage.input))
    attempt.output_tokens = max(0, int(result.usage.output))
    cost = result.usage.cost
    attempt.cost = float(cost) if isinstance(cost, int | float) and math.isfinite(cost) else 0.0
    attempt.cost = max(0.0, attempt.cost)
    attempt.model = result.model or attempt.model
    attempt.permission_mode = result.permission_mode
    attempt.dropped_tools = [str(t)[:400] for t in result.dropped_tools][:32]
    attempt.tool_trail = _clip(result.tool_trail, TRAIL_LIMIT)
    attempt.truncated = bool(result.truncated)
    task.elapsed_ms = attempt.elapsed_ms
    kind = failures.classify(result)
    summary, detail = result.summary, result.error or result.summary
    if kind is None:
        kind, summary, detail = accept(task, attempt, result)
    attempt.summary = (summary or "")[:SUMMARY_LIMIT]
    attempt.failure = kind
    if kind is None:
        task.state = "succeeded"
        task.note = ""
    elif kind == "aborted":
        task.state = "interrupted"
        task.note = "Stopped from outside the workflow; review side effects before retrying."
    else:
        task.state = "failed"
        task.note = f"{kind}: {detail}"[:500]


def accept(task: Task, attempt: Attempt, result: SubagentResult) -> tuple[str | None, str, str]:
    """Acceptance beyond the child's own exit: write authority and structured results."""
    if task.spec.access == "write" and attempt.permission_mode == "plan":
        return (
            "read_only",
            result.summary,
            "ran in plan (read-only) mode, so it could not change files. Use a profile "
            "whose approval_mode grants write authority, or raise the session's mode.",
        )
    schema = task.spec.output_schema
    if schema is None:
        return None, result.summary, ""
    source = result.details if result.truncated and result.details else result.summary
    try:
        data, remainder = structured.extract_json(source)
        problem = structured.validate(data, schema)
        if problem is None and structured.utf8_len(structured.compact(data)) > MAX_DATA_BYTES:
            problem = f"the JSON result exceeds {MAX_DATA_BYTES} bytes"
    except (ValueError, RecursionError) as exc:
        return "invalid_output", result.summary, str(exc) or "the JSON result is nested too deeply"
    if problem is not None:
        return "invalid_output", result.summary, f"output_schema violation: {problem}"
    attempt.data = data
    return None, remainder or "(structured result only)", ""
