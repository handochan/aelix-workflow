"""Engine behaviour for what the host reports: capacity, written files, live state, wrap-ups."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from aelix_coding_agent.subagent_contract import SubagentProgress, SubagentResult, SubagentUsage
from test_engine_dynamic import FakeWorkspace, execute, failed, ok
from test_model import spec

from aelix_workflow.engine import CheckOutcome, WorkflowEngine
from aelix_workflow.model import Attempt, Workflow
from aelix_workflow.panel import render_panel, task_line
from aelix_workflow.report import run_report

FULL = (
    "Delegation refused: 1 delegated agent already runs; this limit is set in settings "
    "backgroundJobs.limits.agents. Stop one or wait; do not retry."
)


def full(task):
    """The host's capacity refusal: an error envelope for a child that never started."""
    return SubagentResult(f"sub-{task.spec.id}", "reviewer", False, "error", FULL, error=FULL)


async def until(predicate):
    async with asyncio.timeout(5):
        while not predicate():
            await asyncio.sleep(0.001)


# --- A. A capacity refusal waits instead of failing ----------------------------------------


async def test_a_refused_task_waits_for_a_sibling_without_spending_an_attempt():
    run = Workflow.create("Slots", [spec("a"), spec("b")], 2)
    calls, holder, admissions, rows = [], [], 0, []

    def admit():
        nonlocal admissions
        admissions += 1
        return True

    async def delegate(task, _prompt, _on_event):
        calls.append(task.spec.id)
        if holder:
            return full(task)
        if task.spec.id == "a":
            holder.append(task.spec.id)
            # Hold the only slot until the other task waits for it.
            await until(lambda: "free agent slot" in run.tasks["b"].note)
            rows.append(task_line(run, run.tasks["b"]))
            holder.clear()
        return ok(task)

    await execute(run, delegate, engine=WorkflowEngine(retry_delays=(60, 60)), admit=admit)
    assert calls == ["a", "b", "b"]
    assert all(t.state == "succeeded" for t in run.tasks.values())
    assert [(a.number, a.trigger) for a in run.tasks["b"].attempts] == [(1, "run")]
    assert admissions == 2 and run.attempt_count == 2
    # It kept its concurrency slot and said why it was waiting.
    assert rows[0].startswith("> b #1 | running: Waiting for a free agent slot")
    assert run.tasks["b"].note == ""


async def test_with_none_of_ours_running_it_falls_back_to_the_delays_then_fails():
    run = Workflow.create("Slots", [spec("a")])
    calls, admissions = 0, 0

    def admit():
        nonlocal admissions
        admissions += 1
        return True

    async def delegate(task, _prompt, _on_event):
        nonlocal calls
        calls += 1
        return full(task)

    await execute(run, delegate, admit=admit)
    task = run.tasks["a"]
    # Tried again after each delay on the same attempt, then failed as transient.
    assert calls == 3 and admissions == 1
    assert [(a.failure, a.trigger) for a in task.attempts] == [("transient", "run")]
    assert task.state == "failed" and task.note.startswith("transient: Delegation refused: 1")


async def test_a_slot_freed_by_something_outside_the_run_ends_the_fallback():
    run = Workflow.create("Slots", [spec("a")])
    answers = iter([full, ok])

    async def delegate(task, _prompt, _on_event):
        return next(answers)(task)

    await execute(run, delegate)
    assert [(a.status, a.trigger) for a in run.tasks["a"].attempts] == [("ok", "run")]


async def test_a_halt_ends_the_wait():
    run = Workflow.create("Slots", [spec("a"), spec("b")], 2)
    calls = []

    async def delegate(task, _prompt, _on_event):
        calls.append(task.spec.id)
        if task.spec.id == "b":
            return full(task)
        await until(lambda: "free agent slot" in run.tasks["b"].note)
        return failed(task, error="HTTP 402: insufficient credits")

    execution = await execute(run, delegate, engine=WorkflowEngine(retry_delays=(60, 60)))
    assert calls == ["a", "b"]
    assert execution.halted and "external blocker" in execution.halted
    assert run.tasks["b"].attempts[0].failure == "transient"
    assert len(run.tasks["b"].attempts) == 1


async def test_ctrl_c_ends_the_wait():
    run = Workflow.create("Slots", [spec("a"), spec("b")], 2)
    forever = asyncio.Event()

    async def delegate(task, _prompt, _on_event):
        if task.spec.id == "b":
            return full(task)
        await forever.wait()

    engine = WorkflowEngine(retry_delays=(60, 60))
    running = asyncio.create_task(execute(run, delegate, engine=engine))
    await until(lambda: "free agent slot" in run.tasks["b"].note)
    running.cancel()
    with pytest.raises(asyncio.CancelledError):
        await running
    # No child of b ever existed: it is pending again, with nothing to review.
    b = run.tasks["b"]
    assert (b.state, b.attempts, b.next_trigger) == ("pending", [], "run")
    assert "no agent had started" in b.note
    assert run.tasks["a"].state == "interrupted"
    assert run.ready() == [b]
    assert not engine.active


async def test_two_refused_tasks_do_not_wait_on_each_other():
    run = Workflow.create("Slots", [spec("a"), spec("b")], 2)

    async def delegate(task, _prompt, _on_event):
        return full(task)

    async with asyncio.timeout(5):
        await execute(run, delegate)
    assert [t.attempts[0].failure for t in run.tasks.values()] == ["transient", "transient"]


async def test_a_started_child_that_quotes_the_limit_is_not_a_capacity_refusal():
    run = Workflow.create("Slots", [spec("a")])

    async def delegate(task, _prompt, _on_event):
        return SubagentResult("sub-a", "reviewer", False, "error", FULL, error=FULL, started=True)

    await execute(run, delegate)
    # An ordinary transient: the existing automatic retries, each a new attempt.
    assert [a.trigger for a in run.tasks["a"].attempts] == ["run", "auto", "auto"]


# --- B. What the host reports a child did ---------------------------------------------------


async def test_the_hosts_record_of_a_child_is_kept_with_its_attempt():
    run = Workflow.create("Files", [{**spec("edit"), "writes": ["src/**"]}])
    changed = tuple(f"src/m{n}.py" for n in range(64))

    async def delegate(task, _prompt, _on_event):
        return failed(
            task,
            "error",
            "boom",
            session_id="0193-child",
            changed_files=changed,
            unconfirmed_files=("src/half.py",),
            files_overflow=3,
            reason="its run failed without an error message",
        )

    await execute(run, delegate)
    attempt = run.tasks["edit"].attempts[0]
    assert attempt.session_id == "0193-child"
    assert attempt.changed_files == list(changed)
    assert attempt.unconfirmed_files == ["src/half.py"]
    assert attempt.files_overflow == 3
    assert attempt.reason == "its run failed without an error message"
    again = Workflow.restore(run.to_dict()).tasks["edit"].attempts[0]
    assert again == attempt


# --- C. Writes are attributed to the child that made them -----------------------------------


async def test_a_childs_own_writes_outside_its_scope_are_its_violations_without_git():
    run = Workflow.create(
        "Scope",
        [
            {**spec("auth"), "writes": ["src/auth/**"]},
            {**spec("docs"), "writes": ["docs/**"]},
            spec("review"),
            {**spec("any"), "access": "write"},
        ],
        4,
    )
    reported = {
        # As runtime._display_path shows them: "/" inside the project on every platform, a
        # backslash only as part of a POSIX name, "../" for a path that climbed out.
        "auth": (("src/auth/a\\b.py", "./docs/guide.md"), ("../elsewhere.py", "/tmp/x.py")),
        "docs": (("docs/guide.md",), ()),
        "review": (("notes.md",), ()),
        "any": (("anything.py",), ()),
    }

    async def delegate(task, _prompt, _on_event):
        changed, unconfirmed = reported[task.spec.id]
        return ok(task, changed_files=changed, unconfirmed_files=unconfirmed)

    # No workspace: an untrusted project, where git is never asked.
    await execute(run, delegate)
    violations = {t.id: t.attempts[0].scope_violations for t in run.tasks.values()}
    # docs/** ran alongside auth, yet auth's own write there is still auth's.
    assert violations == {
        "auth": ["docs/guide.md", "../elsewhere.py", "/tmp/x.py"],
        "docs": [],
        "review": ["notes.md"],
        "any": [],
    }


async def test_host_reported_and_git_observed_violations_are_merged():
    run = Workflow.create("Scope", [{**spec("auth"), "writes": ["src/auth/**"]}])
    workspace = FakeWorkspace(["src/auth/login.py", "README.md", "setup.cfg"])

    async def delegate(task, _prompt, _on_event):
        return ok(task, changed_files=("setup.cfg", "src/auth/login.py"))

    await execute(run, delegate, workspace=workspace)
    assert run.tasks["auth"].attempts[0].scope_violations == ["setup.cfg", "README.md"]


# --- D. A child blocked on a human, stuck, or wrapping up -----------------------------------


async def test_a_childs_approval_wait_and_marks_show_on_its_row_while_it_runs():
    run = Workflow.create("Live", [spec("a")])
    seen = []

    async def delegate(task, _prompt, on_event):
        on_event(
            SubagentProgress(
                "sub-a",
                "reviewer",
                "running",
                current_tool="edit",
                approval="awaiting approval: edit",
                stuck="error loop",
                wrapping_up=True,
            )
        )
        seen.append(task_line(run, task))
        on_event(SubagentProgress("sub-a", "reviewer", "running", current_tool="read"))
        seen.append(task_line(run, task))
        return ok(task)

    await execute(run, delegate)
    # The dialog names the child, so the row does too.
    assert seen[0].startswith("> a #1 | running: awaiting approval: edit · sub-a | ")
    assert "| stuck: error loop | wrapping up |" in seen[0]
    assert seen[1].startswith("> a #1 | running: read | ")
    assert "stuck" not in seen[1] and "wrapping" not in seen[1]
    task = run.tasks["a"]
    assert (task.approval, task.stuck, task.wrapping_up) == (None, None, False)
    assert "approval" not in str(run.to_dict())


def test_child_authored_live_text_is_flattened_on_the_panel():
    run = Workflow.create("Live", [spec("a")])
    task = run.tasks["a"]
    task.state = "running"
    task.approval = "awaiting approval:\x1b[2J edit\nnow"
    assert all("\x1b" not in line and "\n" not in line for line in render_panel(run))


# --- E. A wrapped-up child concluded; it was not stopped ------------------------------------


async def test_a_wrapped_up_child_fails_its_task_without_halting_the_run():
    run = Workflow.create("Wrap", [spec("a"), spec("b")], 1)
    calls = []

    async def delegate(task, _prompt, _on_event):
        calls.append(task.spec.id)
        if task.spec.id == "a":
            return SubagentResult(
                "sub-a",
                "reviewer",
                False,
                "aborted",
                "Partial: route.py checked, session.py not reached.",
                stop_reason="stop",
                started=True,
            )
        return ok(task)

    execution = await execute(run, delegate, ended=lambda result: "wrapped_up")
    a = run.tasks["a"]
    assert calls == ["a", "b"] and execution.halted is None
    assert a.state == "failed" and len(a.attempts) == 1
    assert a.attempts[0].failure == "wrapped_up"
    assert a.attempts[0].summary == "Partial: route.py checked, session.py not reached."
    # Not asked by this extension's /workflow wrap: someone at the host's controls. A live
    # model blamed a time limit, then retried at once.
    assert a.note == (
        "wrapped_up: someone using the host's controls asked the child to finish early (no "
        "time limit ended it); its reply is partial. Do not retry it unless the user asks: "
        "report the partial result."
    )
    assert run.tasks["b"].state == "succeeded"
    assert Workflow.restore(run.to_dict()).tasks["a"].attempts[0].failure == "wrapped_up"


async def test_a_stopped_child_is_still_an_outside_stop():
    run = Workflow.create("Stop", [spec("a"), spec("b")], 1)

    async def delegate(task, _prompt, _on_event):
        return failed(task, "aborted", "stopped")

    execution = await execute(run, delegate, ended=lambda result: "stopped")
    assert run.tasks["a"].state == "interrupted"
    assert execution.halted and "stopped from outside" in execution.halted


# --- Fix round: proof of no child, counters, wakes, halts, paths ----------------------------


def older_host(**fields):
    """An envelope as a host without `started` returns it (beta.2)."""
    base = {
        "id": "sub-a",
        "profile": "reviewer",
        "ok": False,
        "status": "error",
        "summary": "",
        "truncated": False,
        "usage": SubagentUsage(),
        "error": None,
        "exit_code": None,
        "stop_reason": None,
        "elapsed_ms": 0,
        "dropped_tools": (),
        "details": None,
        "tool_trail": None,
        "permission_mode": None,
        "model": None,
    }
    return SimpleNamespace(**{**base, **fields})


async def test_on_an_older_host_a_child_that_ran_and_quotes_the_limit_is_not_run_again():
    # Live-cap wording in a stopped child's answer; usage says it ran. The review's probe.
    run = Workflow.create("Old", [spec("a"), spec("b")], 1)
    calls = []
    quote = "It refuses: 'maximum number of live delegated agents (4).'"

    async def delegate(task, _prompt, _on_event):
        calls.append(task.spec.id)
        return older_host(
            status="aborted",
            summary=quote,
            usage=SubagentUsage(input=900, output=300, turns=3),
            stop_reason="aborted",
        )

    execution = await execute(run, delegate)
    assert calls == ["a"]
    assert run.tasks["a"].attempts[0].failure == "aborted"
    assert execution.halted
    # Its real refusal, silent, is still waited out on the same attempt.
    refusal = (
        "Delegation refused: this session already has the maximum number of live delegated "
        "agents (4). Wait for one to finish, or stop one, before delegating again."
    )
    answers = iter([older_host(summary=refusal, error=refusal), ok(run.tasks["b"])])
    run = Workflow.create("Old", [spec("a")])

    async def refused_once(task, _prompt, _on_event):
        return next(answers)

    await execute(run, refused_once)
    assert [(a.status, a.trigger) for a in run.tasks["a"].attempts] == [("ok", "run")]


async def test_capacity_waits_do_not_use_up_the_transient_retries():
    run = Workflow.create("Slots", [spec("a")])

    def unavailable(task):
        return failed(task, error="503 Service Unavailable", started=True)

    answers = iter([full, full, unavailable, unavailable, ok])

    async def delegate(task, _prompt, _on_event):
        return next(answers)(task)

    await execute(run, delegate)
    assert [(a.trigger, a.failure) for a in run.tasks["a"].attempts] == [
        ("run", "transient"),
        ("auto", "transient"),
        ("auto", None),
    ]


async def test_a_refusal_wakes_whoever_waited_on_it():
    # Both in flight, both refused: neither waits through the other's delays.
    run = Workflow.create("Slots", [spec("a"), spec("b")], 2)

    async def delegate(task, _prompt, _on_event):
        await asyncio.sleep(0)
        return full(task)

    engine = WorkflowEngine(retry_delays=(60, 60))
    running = asyncio.create_task(execute(run, delegate, engine=engine))
    await until(lambda: all("trying again" in t.note for t in run.tasks.values()))
    running.cancel()
    with pytest.raises(asyncio.CancelledError):
        await running


def stop_after(run, task_id, phrase, *, sleep=0.0):
    """A check that fails once task_id's note shows phrase: a stop_on_failure halt."""

    async def check(_task):
        await until(lambda: phrase in run.tasks[task_id].note)
        await asyncio.sleep(sleep)
        return CheckOutcome(1, "failed")

    return check


@pytest.mark.parametrize(
    "answer, phrase",
    [
        (full, "trying again"),
        (lambda t: failed(t, error="503 Service Unavailable", started=True), "retrying"),
    ],
)
async def test_a_halt_cuts_a_retry_delay_short(answer, phrase):
    run = Workflow.create(
        "Halt", [spec("a"), {"id": "c", "title": "c", "kind": "check", "command": ["x"]}], 2
    )

    async def delegate(task, _prompt, _on_event):
        return answer(task)

    engine = WorkflowEngine(retry_delays=(60, 60))
    check = stop_after(run, "a", phrase)
    async with asyncio.timeout(5):
        execution = await execute(run, delegate, engine=engine, check=check, stop_on_failure=True)
    assert execution.halted == "Stopped after c failed (stop_on_failure)."
    assert [a.failure for a in run.tasks["a"].attempts] == ["transient"]


async def test_reported_paths_are_cleaned_and_told_apart_from_git_observations():
    run = Workflow.create("Scope", [{**spec("auth"), "writes": ["src/auth/**"]}])
    workspace = FakeWorkspace(["src/auth/x.py", "notes\x1b[2J.md"])

    async def delegate(task, _prompt, _on_event):
        return ok(task, changed_files=("src/\x1b]0;pwned\x07evil.py",))

    execution = await execute(run, delegate, workspace=workspace)
    attempt = run.tasks["auth"].attempts[0]
    assert attempt.scope_violations == ["src/?]0;pwned?evil.py", "notes?[2J.md"]
    assert (attempt.own_scope, attempt.observed_scope) == (
        ["src/?]0;pwned?evil.py"],
        ["notes?[2J.md"],
    )
    text = run_report(run, execution)
    assert "\x1b" not in text and "\x07" not in text
    own = "  wrote outside its declared writes (its own edit/write calls): src/?]0;pwned?evil.py"
    assert own in text.splitlines()
    assert (
        "  changed outside its declared writes, seen by git (concurrent tasks may share blame): "
        "notes?[2J.md"
    ) in text.splitlines()


def test_a_narrow_row_still_names_the_child_awaiting_approval():
    run = Workflow.create("Live", [spec("a")])
    task = run.tasks["a"]
    task.state, task.approval = "running", "awaiting approval: edit"
    task.attempts = [Attempt(1, child_id="sub-4389265a540f")]
    wide = "\n".join(render_panel(run, width=200))
    narrow = "\n".join(render_panel(run, width=60))
    assert "awaiting approval: edit · sub-4389265a540f" in wide
    assert "awaiting approval: edit · sub-4389… |" in narrow


def test_a_backslash_is_part_of_a_posix_name_not_a_separator():
    # The review's probe: "src/a\\b.py" is one file in src/, inside "src/*.py".
    from aelix_workflow.engine import reported_violations
    from aelix_workflow.model import TaskSpec
    from aelix_workflow.workspace import project_path

    owner = TaskSpec.parse({**spec("t"), "writes": ["src/*.py"]})
    attempt = Attempt(1, changed_files=["src/a\\b.py", "./src/c.py", "../up.py"])
    assert reported_violations(owner, attempt) == ["../up.py"]
    assert project_path("src/a\\b.py") == "src/a\\b.py"
    assert project_path("~/x.py") is None and project_path("/abs/x.py") is None


def test_a_rooted_path_is_outside_even_where_the_platform_calls_it_relative(monkeypatch):
    # CPython 3.13 on Windows: os.path.isabs("/abs/x.py") is False (rooted, no drive). The
    # Windows 3.13 job of CI found it; this holds the same on every platform.
    from aelix_workflow import workspace
    from aelix_workflow.engine import reported_violations
    from aelix_workflow.model import TaskSpec

    monkeypatch.setattr(workspace.os.path, "isabs", lambda _path: False)
    assert workspace.project_path("/abs/x.py") is None
    assert workspace.project_path("src/x.py") == "src/x.py"
    owner = TaskSpec.parse({**spec("t"), "writes": ["**/*.py"]})
    assert reported_violations(owner, Attempt(1, changed_files=["/abs/x.py"])) == ["/abs/x.py"]


# --- /workflow pause: a cooperative pause that returns normally ------------------------------


async def test_a_pause_stops_the_members_and_returns_normally():
    run = Workflow.create("Pause", [spec("a"), spec("b"), spec("c")], 2)
    stopped = []

    async def delegate(task, _prompt, _on_event):
        if task.spec.id == "b":
            return full(task)
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            stopped.append(task.spec.id)
            raise

    engine = WorkflowEngine(retry_delays=(60, 60))
    running = asyncio.create_task(execute(run, delegate, engine=engine))
    await until(lambda: "free agent slot" in run.tasks["b"].note)
    assert engine.request_pause()
    async with asyncio.timeout(5):
        execution = await running  # A normal return: no CancelledError reaches the caller.
    assert execution.paused and execution.halted == "Paused by the user (/workflow pause)."
    assert execution.interrupted == ["a"] and stopped == ["a"]
    a, b, c = run.tasks.values()
    assert a.state == "interrupted" and a.attempts[0].status == "interrupted"
    assert (b.state, b.attempts) == ("pending", [])  # It was only waiting for a slot.
    assert (c.state, c.attempts) == ("pending", [])
    assert run.state == "needs_attention" and not engine.active


async def test_a_pause_asked_as_an_execution_ends_does_not_reach_the_next():
    engine = WorkflowEngine(retry_delays=(0, 0))
    assert not engine.request_pause()  # Nothing executing.
    run = Workflow.create("Ending", [spec("a")])
    asked = []

    def changed(_persist):
        # The final save: the loop has already ended.
        if run.state == "awaiting_review" and not asked:
            asked.append(engine.request_pause())

    async def delegate(task, _prompt, _on_event):
        return ok(task)

    first = await execute(run, delegate, engine=engine, changed=changed)
    assert asked == [True] and not first.paused
    nxt = Workflow.create("Next", [spec("b")])
    second = await execute(nxt, delegate, engine=engine)
    assert not second.paused and nxt.tasks["b"].state == "succeeded"


async def test_a_pause_that_stops_nothing_leaves_the_run_as_its_graph_stands():
    engine = WorkflowEngine(retry_delays=(0, 0))
    run = Workflow.create("Done", [spec("a")])

    async def delegate(task, _prompt, _on_event):
        engine.request_pause()  # Seen only after this, the last task, has settled.
        return ok(task)

    execution = await execute(run, delegate, engine=engine)
    assert execution.paused and execution.interrupted == []
    assert run.tasks["a"].state == "succeeded" and run.state == "awaiting_review"


# --- Pause, second review --------------------------------------------------------------------


def long_check(task_id="c"):
    return {"id": task_id, "title": task_id, "kind": "check", "command": ["x"]}


async def forever(_task):
    await asyncio.Event().wait()


async def test_a_pause_during_the_capacity_delay_puts_the_task_back_to_pending():
    # The fallback DELAY, not the wait on a sibling: a running check is no delegation, so the
    # refused task waits out the delay. On 3.11 the halt ending that delay used to swallow the
    # pause's cancel, and the task ended failed[transient].
    run = Workflow.create("Pause", [spec("b"), long_check()], 2)
    refunds = []

    async def delegate(task, _prompt, _on_event):
        return full(task)

    engine = WorkflowEngine(retry_delays=(60, 60))
    running = asyncio.create_task(
        execute(run, delegate, engine=engine, check=forever, refund=lambda: refunds.append(1))
    )
    await until(lambda: "trying again" in run.tasks["b"].note)
    engine.request_pause()
    async with asyncio.timeout(5):
        execution = await running
    b = run.tasks["b"]
    assert (b.state, b.attempts, b.next_trigger) == ("pending", [], "run")
    assert all(task.id != "b" for task, _ in execution.finished)
    assert refunds == [1]  # Its admission is given back.


async def test_a_blocker_in_the_round_of_a_pause_keeps_its_reason():
    run = Workflow.create("Pause", [spec("a"), spec("b")], 2)
    engine = WorkflowEngine(retry_delays=(0, 0))
    release = asyncio.Event()

    async def delegate(task, _prompt, _on_event):
        if task.spec.id == "a":
            await release.wait()
            engine.request_pause()
            return failed(task, error="HTTP 402: insufficient credits")
        await asyncio.Event().wait()

    running = asyncio.create_task(execute(run, delegate, engine=engine))
    await until(lambda: run.tasks["b"].state == "running")
    release.set()
    execution = await running
    assert execution.paused
    assert execution.halted.startswith("Paused by the user (/workflow pause). It had also halted:")
    assert "a: an external blocker" in execution.halted


class GatedWorkspace:
    """A git snapshot that takes its time: the window before the delegation is asked for."""

    async def snapshot(self):
        await asyncio.Event().wait()


@pytest.mark.parametrize("how", ["pause", "ctrl-c"])
async def test_a_task_stopped_before_its_delegation_was_asked_for_is_pending(how):
    run = Workflow.create("Early", [{**spec("w"), "writes": ["src/**"]}])
    engine = WorkflowEngine(retry_delays=(0, 0))
    delegated, refunds = [], []

    async def delegate(task, _prompt, _on_event):
        delegated.append(task.id)
        return ok(task)

    running = asyncio.create_task(
        execute(
            run,
            delegate,
            engine=engine,
            workspace=GatedWorkspace(),
            refund=lambda: refunds.append(1),
        )
    )
    await until(lambda: run.tasks["w"].state == "running")
    if how == "pause":
        engine.request_pause()
        execution = await running
        assert execution.interrupted == []
    else:
        running.cancel()
        with pytest.raises(asyncio.CancelledError):
            await running
    w = run.tasks["w"]
    assert (w.state, w.attempts, delegated, refunds) == ("pending", [], [], [1])
    assert "before its agent was asked for" in w.note


async def test_a_pause_that_leaves_only_pending_work_does_not_need_attention():
    run = Workflow.create("Waiters", [spec("a")])
    engine = WorkflowEngine(retry_delays=(60, 60))

    async def delegate(task, _prompt, _on_event):
        return full(task)

    running = asyncio.create_task(execute(run, delegate, engine=engine))
    await until(lambda: "trying again" in run.tasks["a"].note)
    engine.request_pause()
    await running
    assert run.tasks["a"].state == "pending" and run.state == "ready"


def fix_and_check(max_rounds=2):
    return Workflow.create(
        "Repair",
        [
            {**spec("fix"), "access": "write"},
            {
                **long_check("chk"),
                "depends_on": ["fix"],
                "repair": {"task": "fix", "max_rounds": max_rounds},
            },
        ],
    )


async def test_a_repair_owed_when_an_execution_stopped_is_taken_up_by_the_next():
    run = fix_and_check()
    engine = WorkflowEngine(retry_delays=(0, 0))
    prompts = []

    async def delegate(task, prompt, _on_event):
        prompts.append(prompt)
        return ok(task)

    async def fails_as_paused(_task):
        engine.request_pause()  # The user pauses as the check fails.
        return CheckOutcome(1, "1 failed: test_route")

    first = await execute(run, delegate, engine=engine, check=fails_as_paused)
    chk = run.tasks["chk"]
    assert first.paused and chk.state == "failed" and chk.rounds == 0

    async def passes(_task):
        return CheckOutcome(0, "ok")

    second = await execute(run, delegate, engine=engine, check=passes)
    assert any("repair round 1/2" in event for event in second.events)
    assert "1 failed: test_route" in prompts[-1]  # The fix reran with the failure output.
    assert (chk.state, chk.rounds, run.state) == ("succeeded", 1, "awaiting_review")


def test_only_a_still_failed_check_with_rounds_left_owes_a_repair():
    from aelix_workflow.engine import owes_repair

    run = fix_and_check(max_rounds=1)
    chk = run.tasks["chk"]
    chk.state = "failed"
    chk.attempts = [Attempt(1, "error", failure="check_failed", exit_code=1)]
    assert owes_repair(chk)
    chk.rounds = 1  # Exhausted.
    assert not owes_repair(chk)
    chk.rounds = 0
    chk.attempts[0].failure = "declined"  # A refused command is no failure to repair.
    assert not owes_repair(chk)
    chk.attempts[0].failure = "check_failed"
    run.retry(["chk"], "rerun the check")  # The model retried it: pending, rounds reset.
    assert not owes_repair(chk)
    chk.state = "failed"
    run.tasks["fix"].state = "succeeded"
    run.tasks["fix"].attempts = [Attempt(1, "ok")]
    run.amend(
        [{**spec("fix"), "access": "write", "prompt": "Fix it another way."}],
        replace=True,
        reason="new approach",
    )
    chk = run.tasks["chk"]  # amend replaces the task objects
    assert chk.state == "pending" and not owes_repair(chk)  # A revised target resets it.


async def test_a_popped_automatic_attempt_leaves_no_trace_and_gives_its_admission_back():
    run = Workflow.create("Auto", [spec("a")])
    engine = WorkflowEngine(retry_delays=(0, 60))
    admitted, refunds = [], []
    answers = iter([lambda t: failed(t, error="503 Service Unavailable", started=True), full, full])

    async def delegate(task, _prompt, _on_event):
        return next(answers)(task)

    running = asyncio.create_task(
        execute(
            run,
            delegate,
            engine=engine,
            admit=lambda: admitted.append(1) or True,
            refund=lambda: refunds.append(1),
        )
    )
    await until(lambda: "trying again" in run.tasks["a"].note)
    running.cancel()
    with pytest.raises(asyncio.CancelledError):
        await running
    a = run.tasks["a"]
    assert (a.state, a.next_trigger) == ("pending", "run")
    assert [(x.number, x.trigger) for x in a.attempts] == [(1, "run")]
    assert (len(admitted), len(refunds)) == (2, 1)


async def test_a_wrap_up_this_extension_asked_for_names_the_user():
    run = Workflow.create("Wrap", [spec("a")])

    async def delegate(task, _prompt, _on_event):
        return SubagentResult(
            "sub-a", "reviewer", False, "aborted", "Partial.", stop_reason="stop", started=True
        )

    await execute(
        run, delegate, ended=lambda result: "wrapped_up", wrap_asked=lambda child: child == "sub-a"
    )
    assert run.tasks["a"].note.startswith(
        "wrapped_up: the user asked this task to finish early with /workflow wrap;"
    )
    assert run.tasks["a"].note.endswith(
        "Do not retry it unless the user asks: report the partial result."
    )


async def test_what_the_model_reads_about_a_failed_check_says_its_repair_is_owed():
    from aelix_workflow.report import overview, task_detail

    run = fix_and_check()
    engine = WorkflowEngine(retry_delays=(0, 0))

    async def delegate(task, _prompt, _on_event):
        return ok(task)

    async def fails_as_paused(_task):
        engine.request_pause()
        return CheckOutcome(1, "1 failed")

    execution = await execute(run, delegate, engine=engine, check=fails_as_paused)
    owed = (
        "Repair owed: once its command is approved, the next workflow_run reruns fix and what was "
        "built on it for chk; retrying, revising or skipping chk cancels that."
    )
    assert owed in run_report(run, execution).splitlines()
    assert f"  {owed}" in overview(run).splitlines()
    assert owed in task_detail(run, run.tasks["chk"]).splitlines()
    run.skip(["chk"], "not needed")
    assert "Repair owed" not in overview(run)


async def test_ctrl_c_that_stops_only_unstarted_work_leaves_the_run_as_its_graph_stands():
    run = Workflow.create("Waiters", [spec("a")])
    engine = WorkflowEngine(retry_delays=(60, 60))

    async def delegate(task, _prompt, _on_event):
        return full(task)

    running = asyncio.create_task(execute(run, delegate, engine=engine))
    await until(lambda: "trying again" in run.tasks["a"].note)
    running.cancel()
    with pytest.raises(asyncio.CancelledError):
        await running
    a = run.tasks["a"]
    # "Stopped", true of Ctrl+C as of a pause.
    assert a.note == "Stopped while waiting for a free agent slot; no agent had started."
    assert (a.state, run.state) == ("pending", "ready")
