"""Real harness, delegation runtime and JSONL sessions for the dynamic features."""

from __future__ import annotations

import asyncio
import shutil
import sys
from types import SimpleNamespace

import pytest
from aelix_agent_core.session import JsonlSessionStorage, LocalFileSystem, Session
from aelix_ai.tools import ToolExecutionContext
from aelix_coding_agent.extensions.api import Extension, ExtensionAPI, _ExtensionRuntime
from aelix_coding_agent.subagent_contract import SubagentResult
from test_host import RecordingUI, bench
from test_model import spec

from aelix_workflow.activation import requested_workflow
from aelix_workflow.extension import ALLOW_CHECKS, SNAPSHOT_TYPE, WorkflowExtension
from aelix_workflow.model import Workflow

CONTEXT = ToolExecutionContext()


def check_task(task_id, code="print('checked ok')", deps=()):
    return {
        "id": task_id,
        "title": f"Check {task_id}",
        "kind": "check",
        "command": [sys.executable, "-c", code],
        "depends_on": list(deps),
    }


async def create(b, tasks, **extra):
    result = await b.controller.plan(
        {"action": "create", "goal": "Dynamic", "tasks": tasks, **extra}, CONTEXT
    )
    assert not result.is_error, result.content[0].text
    return b.controller.runs[b.controller.selected]


async def test_a_session_stop_halts_the_workflow_and_keeps_the_host_door_shut(tmp_path):
    b = await bench(tmp_path)
    spawned = []
    stopped = asyncio.Event()

    async def channel(plan, *, child=None, on_stream=None):
        spawned.append((plan.task.split("Your task (")[1].split(")")[0], stopped.is_set()))
        if len(spawned) == 1:
            while not child.stopped:
                await asyncio.sleep(0.01)
            child.state = "stopped"
            return SubagentResult(
                plan.id, plan.resolved.name, False, "aborted", "", error="stopped"
            )
        child.state = "done"
        return SubagentResult(plan.id, plan.resolved.name, True, "ok", "done")

    b.channel.run = channel
    run = await create(b, [spec("a"), spec("b"), spec("c")], max_concurrency=1)
    runner = asyncio.create_task(b.controller.run({"run_id": run.id}, CONTEXT))
    while not spawned:
        await asyncio.sleep(0.01)
    await b.runtime.subagents.stop_all()  # What /new, /resume, /fork and quit do first.
    stopped.set()
    result = await runner
    assert spawned == [("a", False)]
    assert b.runtime.subagents._closed
    assert run.tasks["a"].state == "interrupted"
    assert run.tasks["b"].state == run.tasks["c"].state == "pending"
    assert result.is_error and "Halted" in result.content[0].text
    await b.harness.dispose()


async def test_verification_commands_are_approved_once_per_workflow(tmp_path):
    ui = RecordingUI(answer=ALLOW_CHECKS)
    b = await bench(tmp_path, ui=ui)
    run = await create(b, [check_task("passes")])
    result = await b.controller.run({"run_id": run.id}, CONTEXT)
    assert not result.is_error, result.content[0].text
    assert len(ui.dialogs) == 1
    assert "verification command" in ui.dialogs[0][0]
    assert run.tasks["passes"].output == "checked ok"
    failing = check_task("fails", "import sys; print('3 failed'); sys.exit(3)")
    await b.controller.plan(
        {
            "action": "add",
            "run_id": run.id,
            "tasks": [check_task("again"), failing],
            "reason": "More",
        },
        CONTEXT,
    )
    result = await b.controller.run({"run_id": run.id}, CONTEXT)
    assert len(ui.dialogs) == 2
    # Only the new command is asked about; the approved one runs without a second dialog.
    assert "checked ok" not in ui.dialogs[1][0] and "3 failed" in ui.dialogs[1][0]
    assert run.tasks["again"].state == "succeeded"
    fails = run.tasks["fails"]
    assert fails.state == "failed" and fails.attempts[0].failure == "check_failed"
    assert fails.attempts[0].exit_code == 3
    assert "3 failed" in result.content[0].text
    assert not b.channel.plans
    # Approval belongs to one workflow: a new workflow asks again for the same command.
    other = await create(b, [check_task("passes")])
    await b.controller.run({"run_id": other.id}, CONTEXT)
    assert len(ui.dialogs) == 3
    await b.harness.dispose()


async def test_the_approval_dialog_names_writers_that_can_change_the_checked_code(tmp_path):
    ui = RecordingUI(answer="Deny")
    b = await bench(tmp_path, ui=ui)
    b.controller._write_capable = lambda _resolved: True
    run = await create(
        b, [{**spec("impl"), "writes": ["src/**"]}, check_task("tests", deps=("impl",))]
    )
    await b.controller.run({"run_id": run.id}, CONTEXT)
    assert "Write tasks in this workflow (impl) can change the code" in ui.dialogs[0][0]
    await b.harness.dispose()


async def test_git_observation_needs_a_trusted_project(tmp_path, monkeypatch):
    import aelix_workflow.extension as extension

    built = []
    monkeypatch.setattr(extension, "GitWorkspace", lambda *args: built.append(args) or None)
    b = await bench(tmp_path)
    run = await create(b, [spec("inspect")])
    await b.controller.run({"run_id": run.id}, CONTEXT)
    assert built == []
    b.controller._capture_cwd(SimpleNamespace(cwd=str(tmp_path), is_project_trusted=lambda: True))
    await b.controller.plan(
        {"action": "add", "run_id": run.id, "tasks": [spec("more")], "reason": "More"}, CONTEXT
    )
    await b.controller.run({"run_id": run.id}, CONTEXT)
    assert len(built) == 1
    await b.harness.dispose()


@pytest.mark.parametrize("headless", [False, True])
async def test_denied_or_unaskable_commands_are_declined(tmp_path, headless):
    ui = None if headless else RecordingUI(answer="Deny")
    b = await bench(tmp_path, ui=ui)
    run = await create(b, [check_task("tests")])
    await b.controller.run({"run_id": run.id}, CONTEXT)
    attempt = run.tasks["tests"].attempts[0]
    assert attempt.failure == "declined"
    assert ("approval" if headless else "declined") in attempt.error
    await b.harness.dispose()


async def test_a_write_task_without_write_tools_is_refused_before_any_spawn(tmp_path):
    b = await bench(tmp_path)
    run = await create(b, [{**spec("impl"), "access": "write"}])
    result = await b.controller.run({"run_id": run.id}, CONTEXT)
    assert result.is_error
    assert "has no edit, write or bash tool" in result.content[0].text
    assert not b.channel.plans
    assert run.attempt_count == 0
    await b.harness.dispose()


async def test_a_read_only_child_cannot_satisfy_a_write_task(tmp_path):
    b = await bench(tmp_path)
    b.controller._write_capable = lambda _resolved: True

    async def channel(plan, *, child=None, on_stream=None):
        child.state = "done"
        return SubagentResult(
            plan.id,
            plan.resolved.name,
            True,
            "ok",
            "I would edit route.py, but I cannot write in plan mode.",
            permission_mode=plan.permission_mode.value,
        )

    b.channel.run = channel
    run = await create(b, [{**spec("impl"), "access": "write"}])
    result = await b.controller.run({"run_id": run.id}, CONTEXT)
    impl = run.tasks["impl"]
    assert impl.state == "failed" and impl.attempts[0].failure == "read_only"
    assert impl.attempts[0].permission_mode == "plan"
    assert "read_only" in result.content[0].text
    await b.harness.dispose()


async def test_status_shows_one_task_and_runs_report_only_new_results(tmp_path):
    b = await bench(tmp_path)
    await b.harness.prompt("Review using a workflow")
    run = b.controller.runs[b.controller.selected]
    detail = await b.controller.status({"run_id": run.id, "task_id": "inspect"}, CONTEXT)
    text = detail.content[0].text
    assert "Task inspect | succeeded" in text
    assert "checked route.py:12" in text
    assert f"child: {run.tasks['inspect'].attempts[0].child_id}" in text
    await b.controller.plan(
        {
            "action": "add",
            "run_id": run.id,
            "tasks": [spec("next", deps=("inspect",))],
            "reason": "More",
        },
        CONTEXT,
    )
    result = await b.controller.run({"run_id": run.id}, CONTEXT)
    report = result.content[0].text
    assert "Finished in this run (1):" in report
    assert "next #1" in report and "inspect #1" not in report
    missing = await b.controller.status({"run_id": run.id, "task_id": "nope"}, CONTEXT)
    assert missing.is_error
    await b.harness.dispose()


@pytest.mark.parametrize("lose_workflow_record", [False, True])
async def test_a_crash_mid_run_restores_the_child_and_an_unknown_outcome(
    tmp_path, lose_workflow_record
):
    b = await bench(tmp_path)
    running = asyncio.Event()

    async def channel(plan, *, child=None, on_stream=None):
        running.set()
        await asyncio.Event().wait()

    b.channel.run = channel
    run = await create(b, [spec("inspect")])
    runner = asyncio.create_task(b.controller.run({"run_id": run.id}, CONTEXT))
    await asyncio.wait_for(running.wait(), 2)
    child_id = run.tasks["inspect"].attempts[0].child_id
    assert child_id
    await b.harness.flush_pending_session_writes()
    crash = tmp_path / "crash.jsonl"
    shutil.copy(b.session.session_file, crash)
    if lose_workflow_record:
        # Keep the entry tree intact; only hide the workflow's own attempt record.
        text = crash.read_text().replace('"aelix-workflow.attempt"', '"x-lost"')
        crash.write_text(text)
    runner.cancel()
    await asyncio.gather(runner, return_exceptions=True)
    await b.harness.dispose()

    reopened = Session(await JsonlSessionStorage.open(LocalFileSystem(), str(crash)))
    controller = WorkflowExtension(ExtensionAPI(Extension(name="restored"), _ExtensionRuntime()))
    await controller.session_start(
        None, SimpleNamespace(session_manager=SimpleNamespace(get_session=lambda: reopened))
    )
    restored = controller.runs[run.id]
    task = restored.tasks["inspect"]
    assert task.state == "interrupted"
    assert task.attempts[0].child_id == child_id
    assert "Aelix stopped before this child settled" in task.note
    assert restored.state == "needs_attention"


async def test_continue_resumes_with_an_explicit_request(tmp_path):
    b = await bench(tmp_path, mode="off")
    run = Workflow.create("Paused", [spec("a")])
    b.controller.runs[run.id] = run
    b.controller.selected = run.id
    sent = []
    b.controller.api.send_message = lambda message, **options: sent.append(message)  # type: ignore[method-assign]
    assert await b.controller.command("continue", None) == f"Continuing workflow {run.id}."
    text = sent[-1].content[0].text
    assert text.startswith(f"Workflow: continue run {run.id}.")
    assert requested_workflow(text)
    await b.controller.command("continue the refactor", None)
    assert sent[-1].content[0].text.startswith("Workflow: continue the refactor")
    await b.harness.dispose()


async def owed_after_restart(b):
    """A run left as a stopped execution leaves it (fix done, its check failed with a repair
    owed), saved, then a new session start: approvals are not persisted, so none is left."""
    from aelix_workflow.model import Attempt

    repaired = {**check_task("chk", deps=["fix"]), "repair": {"task": "fix", "max_rounds": 2}}
    run = await create(b, [spec("fix"), repaired])
    fix, chk = run.tasks["fix"], run.tasks["chk"]
    fix.state, fix.attempts = "succeeded", [Attempt(1, status="ok", child_id="sub-old")]
    chk.state = "failed"
    chk.attempts = [Attempt(1, "error", summary="1 failed", failure="check_failed", exit_code=1)]
    # Saved as the snapshot written when that execution ended.
    await b.session.append_custom_entry(SNAPSHOT_TYPE, run.to_dict())
    await b.controller.session_start(
        None, SimpleNamespace(session_manager=SimpleNamespace(get_session=lambda: b.session))
    )
    return b.controller.runs[run.id]


class CountingUI(RecordingUI):
    """Records how many children existed when each dialog was shown."""

    def __init__(self, answer, plans):
        super().__init__(answer=answer)
        self.plans, self.children_at_dialog = plans, []

    async def select(self, title, options, opts=None):
        self.children_at_dialog.append(len(self.plans))
        return await super().select(title, options, opts)


async def test_an_owed_repair_after_a_restart_is_approved_before_any_child(tmp_path):
    ui = CountingUI(ALLOW_CHECKS, [])
    b = await bench(tmp_path, ui=ui)
    ui.plans = b.channel.plans
    run = await owed_after_restart(b)
    result = await b.controller.run({"run_id": run.id}, CONTEXT)
    assert not result.is_error, result.content[0].text
    # The check's command was asked about before the repair's child was started.
    assert ui.children_at_dialog == [0] and "checked ok" in ui.dialogs[0][0]
    assert len(b.channel.plans) == 1 and "1 failed" in b.channel.plans[0].task
    chk = run.tasks["chk"]
    assert (chk.state, chk.rounds, chk.output) == ("succeeded", 1, "checked ok")
    await b.harness.dispose()


@pytest.mark.parametrize("answer", ["Deny", "headless"])
async def test_an_owed_repair_whose_command_is_not_approved_spends_no_child(tmp_path, answer):
    ui = None if answer == "headless" else RecordingUI(answer=answer)
    b = await bench(tmp_path, ui=ui)
    run = await owed_after_restart(b)
    await b.controller.run({"run_id": run.id}, CONTEXT)
    chk = run.tasks["chk"]
    assert not b.channel.plans
    assert chk.state == "failed" and [a.failure for a in chk.attempts] == ["check_failed"]
    assert chk.note.startswith(
        "Its repair, rerunning fix, waits for the user to approve its command"
    )
    assert run.tasks["fix"].attempts[-1].child_id == "sub-old"
    await b.harness.dispose()


async def test_an_owed_repairs_target_is_checked_before_any_child(tmp_path):
    from test_host import no_delegation

    ui = RecordingUI(answer=ALLOW_CHECKS)
    b = await bench(tmp_path, ui=ui)
    run = await owed_after_restart(b)
    no_delegation(b, agents=("implementer",))  # fix's profile is no longer one it may use
    result = await b.controller.run({"run_id": run.id}, CONTEXT)
    # Refused as the call itself, before the dialog: no attempt, no child.
    assert result.is_error
    assert result.content[0].text.startswith("Profile 'reviewer' is not available")
    assert not b.channel.plans and not ui.dialogs
    assert len(run.tasks["fix"].attempts) == 1
    await b.harness.dispose()


async def test_a_new_session_asks_about_every_check_a_repair_could_reset(tmp_path):
    # The final review's probe: an owed repair of chkA resets chkB, another failed check on
    # the same target that is neither pending nor owed; it used to run declined, unasked.
    from aelix_workflow.model import Attempt

    ui = CountingUI(ALLOW_CHECKS, [])
    b = await bench(tmp_path, ui=ui)
    ui.plans = b.channel.plans
    owed = {**check_task("chkA", "print('A ok')", ["fix"]), "repair": {"task": "fix"}}
    run = await create(b, [spec("fix"), owed, check_task("chkB", "print('B ok')", ["fix"])])
    run.tasks["fix"].state = "succeeded"
    run.tasks["fix"].attempts = [Attempt(1, status="ok", child_id="sub-old")]
    for task_id in ("chkA", "chkB"):
        task = run.tasks[task_id]
        task.state = "failed"
        task.attempts = [Attempt(1, "error", summary="failed", failure="check_failed", exit_code=1)]
    await b.session.append_custom_entry(SNAPSHOT_TYPE, run.to_dict())
    await b.controller.session_start(
        None, SimpleNamespace(session_manager=SimpleNamespace(get_session=lambda: b.session))
    )
    run = b.controller.runs[run.id]
    await b.controller.run({"run_id": run.id}, CONTEXT)
    # One dialog, before any child, listing both commands.
    assert ui.children_at_dialog == [0]
    assert "A ok" in ui.dialogs[0][0] and "B ok" in ui.dialogs[0][0]
    assert len(b.channel.plans) == 1
    for task_id, output in (("chkA", "A ok"), ("chkB", "B ok")):
        task = run.tasks[task_id]
        assert (task.state, task.output) == ("succeeded", output)
        assert all(a.failure != "declined" for a in task.attempts)
    await b.harness.dispose()


async def test_no_dialog_when_no_check_is_pending_or_owes_a_repair(tmp_path):
    from aelix_workflow.model import Attempt

    ui = RecordingUI(answer=ALLOW_CHECKS)
    b = await bench(tmp_path, ui=ui)
    run = await create(b, [spec("a"), check_task("done")])
    done = run.tasks["done"]
    done.state, done.attempts = "succeeded", [Attempt(1, "ok", summary="checked ok", exit_code=0)]
    await b.controller.run({"run_id": run.id}, CONTEXT)
    assert not ui.dialogs
    assert run.tasks["a"].state == "succeeded" and len(b.channel.plans) == 1
    await b.harness.dispose()
