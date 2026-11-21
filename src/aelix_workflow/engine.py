"""Bounded, work-conserving DAG execution over injected delegation and check runners."""

from __future__ import annotations

import asyncio
import contextlib
import math
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

from aelix_coding_agent.subagent_contract import SubagentProgress, SubagentResult

from aelix_workflow import failures, structured
from aelix_workflow.model import (
    MAX_ATTEMPTS,
    MAX_DATA_BYTES,
    MAX_FILES,
    PATH_LIMIT,
    REASON_LIMIT,
    SUMMARY_LIMIT,
    TRAIL_LIMIT,
    Attempt,
    Task,
    TaskSpec,
    Workflow,
    conflicts,
)
from aelix_workflow.prompting import build_prompt
from aelix_workflow.workspace import Snapshot, changed, matches, project_path

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
SCOPE_LIMIT = 20
_INTERRUPTED = "Execution interrupted; review side effects before explicit retry."
PAUSED = "Paused by the user (/workflow pause)."
_WAITING = "Waiting for a free agent slot"


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
    paused: bool = False  # The user's /workflow pause ended it; `halted` says why, with any halt.
    interrupted: list[str] = field(default_factory=list)  # tasks a pause stopped mid-run


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
    # How the host says a child's run ended ("wrapped_up", "stopped", ...), when it can.
    ended: Callable[[SubagentResult], str | None] | None = None
    # Gives back the admission of an attempt that never started (beside `admit`).
    refund: Callable[[], None] | None = None
    # Whether the user's own /workflow wrap asked this child (by child ID) to finish early.
    wrap_asked: Callable[[str], bool] | None = None


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
        # Members whose delegation is in flight, and finished members: a task refused for
        # want of an agent slot waits for one of these to finish (_wait_for_slot).
        self._delegating: set[str] = set()
        self._completions = 0
        self._freed = asyncio.Event()
        self._halted = asyncio.Event()  # Ends a retry delay as soon as admission halts.
        self._pause_requested = False
        self._pausing = False  # Set while a pause stops the members (see _wait_for_slot).

    @property
    def active(self) -> bool:
        return self._owner is not None

    def request_pause(self) -> bool:
        """Ask the active execution to pause at its next step; False when none is active.

        Cooperative, unlike Ctrl+C: the owner task is never cancelled from outside the host's
        own abort. The execution stops its members and returns normally (``PAUSED``).
        """
        if self._owner is None:
            return False
        self._pause_requested = True
        return True

    def _halt_with(self, reason: str) -> None:
        """Admit nothing new; the first reason stands, and waiting members wake to see it."""
        if self._halt is None:
            self._halt = reason
            self._halted.set()
            self._wake()

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
        ended: Callable[[SubagentResult], str | None] | None = None,
        refund: Callable[[], None] | None = None,
        wrap_asked: Callable[[str], bool] | None = None,
        repair_approved: Callable[[Task], bool] | None = None,
    ) -> Execution:
        if self._owner is not None:
            raise ValueError("A workflow execution is already active in this session.")
        if run.state in {"completed", "cancelled"}:
            raise ValueError(
                f"Workflow is {run.state}; create a new one or retry interrupted work."
            )
        self._owner = asyncio.current_task()
        self._halt = None
        self._halted = asyncio.Event()
        self._pause_requested = self._pausing = False
        self._cancel_members()  # Nothing from an earlier, interrupted execution may linger.
        execution = Execution()
        ctx = _Run(
            run,
            delegate,
            changed,
            admit,
            check,
            started,
            workspace,
            execution,
            ended,
            refund,
            wrap_asked,
        )
        # A repair a stopped execution still owed (a pause, Ctrl+C or a halt in its round)
        # is the plan's, so it is taken up again (see owes_repair), but only once its check's
        # command is approved for this run: rerunning the target first and then declining the
        # check would spend a child for nothing, without the user ever being asked.
        repairs: set[str] = set()
        for owed in (t for t in run.tasks.values() if owes_repair(t)):
            if repair_approved is None or repair_approved(owed):
                repairs.add(owed.id)
            else:
                owed.note = (
                    f"Its repair, rerunning {(owed.spec.repair or {}).get('task')}, waits for the "
                    f"user to approve its command ({' '.join(owed.spec.command)}), which was not "
                    "approved for this run; the interactive TUI asks before the next run starts."
                )[:500]
        try:
            run.state = "running"
            changed(True)
            while True:
                if is_aborted(signal):
                    raise asyncio.CancelledError
                if self._pause_requested:
                    return await self._pause(run, changed, execution)
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
                    if stop_on_failure and newly_failed:
                        self._halt_with(
                            f"Stopped after {newly_failed[0]} failed (stop_on_failure)."
                        )
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
                    self._completions += 1
                if done:
                    # Settled first, so a task waiting for a slot sees this round's halt.
                    self._wake()
                run.sync()
            run.refresh_state()
            if self._halt is not None:
                run.state = "needs_attention"
            execution.halted = self._halt
            changed(True)
            return execution
        except asyncio.CancelledError:
            await self._stop_members(run, changed)
            raise
        except BaseException:
            await self._stop(
                run, changed, "Execution stopped by an internal error; review side effects."
            )
            raise
        finally:
            self._owner = None
            # One asked for as the execution ended goes with it.
            self._pause_requested = self._pausing = False

    async def _pause(
        self, run: Workflow, changed: Callable[[bool], None], execution: Execution
    ) -> Execution:
        """What Ctrl+C does to the run, then a normal return: the turn itself goes on."""
        earlier = self._halt  # A blocker settled in this same round keeps its reason.
        self._pausing = True
        self._halt_with(PAUSED)
        execution.interrupted = await self._stop_members(run, changed)
        execution.paused = True
        execution.halted = PAUSED if earlier is None else f"{PAUSED} It had also halted: {earlier}"
        return execution

    async def _stop_members(self, run: Workflow, changed: Callable[[bool], None]) -> list[str]:
        """Ctrl+C's and the pause's stop: the tasks interrupted, and a state that fits the rest.

        When it interrupted nothing (only unstarted tasks ran), nothing needs review, so the
        run says what its graph holds instead of needs_attention.
        """
        running = [t for t in run.units() if t.state == "running"]
        await self._stop(run, changed, _INTERRUPTED)
        interrupted = [t.id for t in running if t.state == "interrupted"]
        if not interrupted:
            run.refresh_state()
            changed(True)
        return interrupted

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
                self._halt_with(f"The workflow used all {MAX_ATTEMPTS} attempts.")
                return
            if task.spec.kind == "agent" and not ctx.admit():
                self._halt_with("The per-prompt delegation budget is exhausted.")
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
        task.clear_live()
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
            self._halt_with(f"{task.id}: {failures.describe(kind)}{detail}"[:500])
        elif task.state == "failed" and stop_on_failure and not rounds_left:
            # A failed check with repair rounds left is the plan's own reaction, not a stop.
            self._halt_with(f"Stopped after {task.id} failed (stop_on_failure).")
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
            try:
                before = await self._snapshot(ctx, task)
            except asyncio.CancelledError:
                # Stopped before its delegation was even asked for: no child can exist.
                _unstart(ctx, task, task.attempts[-1], "before its agent was asked for")
                raise
            if task.spec.kind == "check":
                await self._run_check(ctx, task)
            else:
                await self._run_agent(ctx, task)
            if task.state != "pending":  # Unstarted by a pause: there is nothing to observe.
                await self._observe_scope(ctx, task, before)
        finally:
            self._scope_own.pop(task.id, None)
            self._scope_seen.pop(task.id, None)
            task.clear_live()
            ctx.run.sync()
            ctx.changed(True)

    async def _run_agent(self, ctx: _Run, task: Task) -> None:
        automatic = 0  # transient retries, each a new attempt
        waits = 0  # capacity delays, on the same attempt
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
                # Newer hosts only. Child-authored, so shown as the tool name is, never acted on.
                task.approval = _text(getattr(event, "approval", None))
                task.stuck = _text(getattr(event, "stuck", None))
                task.wrapping_up = getattr(event, "wrapping_up", False) is True
                task.elapsed_ms = event.elapsed_ms
                task.context_tokens = event.tokens
                attempt.model = event.model or attempt.model
                ctx.changed(False)

            completions = self._completions
            self._delegating.add(task.id)
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
            finally:
                self._delegating.discard(task.id)
            refused = failures.capacity(result)
            if refused:
                # No child ever ran, so the same attempt is tried again, at no cost. Whoever
                # waited on this delegation re-decides now: it held no slot to free.
                self._wake()
                try:
                    waited = await self._wait_for_slot(ctx, task, completions, waits)
                except asyncio.CancelledError:
                    _unstart(ctx, task, attempt, "while waiting for a free agent slot")
                    raise
                if self._pausing:
                    # Woken by the pause itself: whatever the cancel's timing, no child existed.
                    _unstart(ctx, task, attempt, "while waiting for a free agent slot")
                    return
                if waited is not None:
                    waits = waited
                    continue
            try:
                asked = ctx.wrap_asked is not None and ctx.wrap_asked(result.id)
                record_result(task, attempt, result, end=_end(ctx, result), wrap_asked=asked)
            except Exception as exc:
                attempt.status, attempt.failure = "error", "error"
                attempt.error = f"Could not record the result: {exc}"[:1_000]
                task.state = "failed"
                task.note = attempt.error[:500]
            ctx.execution.finished.append((task, attempt))
            # A capacity refusal has had its waits; a new attempt would only be refused again.
            delay = (
                None if refused else self._retry_delay(ctx, task, attempt, used_tools, automatic)
            )
            if delay is None:
                return
            # The slot stays held during the backoff so the retry cannot be starved.
            failed_note = task.note
            task.state = "running"
            task.note = f"Transient failure; retrying automatically in {delay:g}s."
            ctx.changed(False)
            try:
                await self._delay(delay)
            except asyncio.CancelledError:
                task.state, task.note = "failed", failed_note
                raise
            if self._halt is not None or ctx.run.attempt_count >= MAX_ATTEMPTS or not ctx.admit():
                task.state, task.note = "failed", failed_note
                return
            automatic += 1
            task.note = ""
            task.attempts.append(Attempt(number=len(task.attempts) + 1, trigger="auto"))

    async def _wait_for_slot(
        self, ctx: _Run, task: Task, completions: int, waits: int
    ) -> int | None:
        """After a capacity refusal, wait until a slot may be free; the delays used, or None.

        While another delegation of this execution is in flight, the task waits for it to
        come back, keeping its own concurrency slot. One that ran wakes it once settled; one
        refused too wakes it at once, and both then take the delay path. With none in flight
        (something outside the run holds the slots), it waits out the retry delays, counted
        apart from transient retries. None records the refusal as a transient failure: a
        halt, or no delay left.
        """
        try:
            while self._halt is None:
                if self._completions != completions:
                    return waits  # One of ours finished meanwhile: a slot may be free.
                if not self._delegating and waits >= len(self.retry_delays):
                    return None
                task.clear_live()
                if self._delegating:
                    task.note = f"{_WAITING}; another task of this run holds one."
                    ctx.changed(False)
                    await self._freed.wait()
                    continue
                delay = self.retry_delays[waits]
                task.note = f"{_WAITING}; trying again in {delay:g}s."
                ctx.changed(False)
                await self._delay(delay)
                return waits + 1 if self._halt is None else None
            return None
        finally:
            task.note = ""

    async def _delay(self, seconds: float) -> None:
        """A retry delay that a halt cuts short."""
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self._halted.wait(), seconds)

    def _wake(self) -> None:
        self._freed.set()
        self._freed = asyncio.Event()

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
        # Git also sees bash's changes, but not whose they were: concurrent writers share one
        # allowance. The host's own record of this child (record_result) stays first.
        outside = sorted(path for path in changed(before, after) if not matches(path, allowed))
        merged = dict.fromkeys([*attempt.scope_violations, *map(printable, outside)])
        attempt.scope_violations = list(merged)[:SCOPE_LIMIT]

    # --- Teardown ------------------------------------------------------------------------

    def _cancel_members(self) -> list[asyncio.Task[None]]:
        members = list(self._members)
        self._members.clear()
        self._scope_own.clear()
        self._scope_seen.clear()
        self._delegating.clear()
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


def _unstart(ctx: _Run, task: Task, attempt: Attempt, when: str) -> None:
    """Stopped before any child of it existed: nothing needs review, so it is pending again.

    The attempt is taken back with its admission. An automatic retry leaves no trace: the
    task's next run is an ordinary one.
    """
    if task.attempts and task.attempts[-1] is attempt:
        task.attempts.pop()
        if task.spec.kind == "agent" and ctx.refund is not None:
            ctx.refund()
    task.next_trigger = "run" if attempt.trigger == "auto" else attempt.trigger
    task.state = "pending"
    task.note = f"Stopped {when}; no agent had started."


def owes_repair(task: Task) -> bool:
    """A failed check whose repair a stopped execution never applied, with rounds left.

    Its own failure is the repairable kind and its state is still that failure, so neither
    the model nor a revision has moved it since: retry and revise reset it to pending.
    """
    plan = task.spec.repair
    last = task.latest
    return (
        plan is not None
        and task.state == "failed"
        and last is not None
        and last.failure in {"check_failed", "timeout"}
        and task.rounds < plan["max_rounds"]
    )


def _interrupt(task: Task, attempt: Attempt) -> None:
    attempt.status = "interrupted"
    attempt.failure = "interrupted"
    task.state = "interrupted"
    task.note = _INTERRUPTED


def _text(value: Any, limit: int = 200) -> str | None:
    return value[:limit] if isinstance(value, str) and value else None


def _end(ctx: _Run, result: SubagentResult) -> str | None:
    """Only an aborted run can have been a wrap-up; ask the host how it ended."""
    if ctx.ended is None or result.status != "aborted":
        return None
    try:
        return ctx.ended(result)
    except Exception:
        return None


def _paths(value: Any) -> list[str]:
    if not isinstance(value, tuple | list):
        return []
    return [path[:PATH_LIMIT] for path in value if isinstance(path, str) and path][:MAX_FILES]


def reported_violations(spec: TaskSpec, attempt: Attempt) -> list[str]:
    """Files this child itself wrote outside its task's own writes, by the host's record.

    Its edit/write calls only (a change made through bash is not in the record), and so
    attributable whatever ran alongside. A read task owns no files; a writer that declared
    no writes owns every file, as scheduling treats it.
    """
    if spec.access == "write" and not spec.writes:
        return []
    found: list[str] = []
    for path in [*attempt.changed_files, *attempt.unconfirmed_files]:
        local = project_path(path)
        if spec.access == "read" or local is None or not matches(local, spec.writes):
            found.append(printable(local or path))
    return list(dict.fromkeys(found))[:SCOPE_LIMIT]


def printable(path: str) -> str:
    """A recorded path the model reads: no control character survives into its context."""
    return "".join(c if c.isprintable() else "?" for c in path)


def record_result(
    task: Task,
    attempt: Attempt,
    result: SubagentResult,
    *,
    end: str | None = None,
    wrap_asked: bool = False,
) -> None:
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
    # What the host reports the child did; absent on older hosts.
    attempt.session_id = _text(getattr(result, "session_id", None))
    attempt.changed_files = _paths(getattr(result, "changed_files", ()))
    attempt.unconfirmed_files = _paths(getattr(result, "unconfirmed_files", ()))
    overflow = getattr(result, "files_overflow", 0)
    attempt.files_overflow = (
        min(overflow, 1_000_000) if type(overflow) is int and overflow > 0 else 0
    )
    attempt.reason = _text(getattr(result, "reason", None), REASON_LIMIT)
    if task.spec.kind == "agent":
        attempt.scope_violations = reported_violations(task.spec, attempt)
        attempt.own_violations = len(attempt.scope_violations)
    task.elapsed_ms = attempt.elapsed_ms
    kind = failures.classify(result, end=end)
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
    elif kind == "wrapped_up":
        task.state = "failed"
        # A live model doubted who asked, then retried at once. This extension knows when
        # its own /workflow wrap asked; anything else came through the host's controls.
        task.note = (
            "wrapped_up: the user asked this task to finish early with /workflow wrap; its "
            "reply is partial."
            if wrap_asked
            else f"wrapped_up: {failures.describe(kind)}."
        ) + " Do not retry it unless the user asks: report the partial result."
    else:
        task.state = "failed"
        task.note = f"{kind}: {_failure_detail(kind, detail, attempt)}"[:500]


def _failure_detail(kind: str, detail: str | None, attempt: Attempt) -> str:
    """The child's own words when it left any; a timed-out child usually leaves none."""
    text = (detail or "").strip()
    if text:
        return text
    if kind == "timeout":
        # Only a host older than the removal of delegation time limits ends an agent this way.
        return (
            f"the child ran out of time after {attempt.elapsed_ms / 1_000:.0f}s at its "
            "profile's time limit, which only the profile can raise; narrow or split the task "
            "before retrying"
        )
    return failures.describe(kind) or "no detail was reported"


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
