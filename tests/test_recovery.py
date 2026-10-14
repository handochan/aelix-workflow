"""Restart recovery against real session files and the host's receipt record format."""

from __future__ import annotations

from types import SimpleNamespace

from aelix_agent_core.session import JsonlSessionStorage, LocalFileSystem, Session
from aelix_ai.messages import AssistantMessage, TextContent, ToolCallContent, ToolResultMessage
from test_dynamic import FINDINGS
from test_model import spec

from aelix_workflow import recovery
from aelix_workflow.model import Workflow


async def write_child(path, messages):
    path.parent.mkdir(parents=True, exist_ok=True)
    storage = await JsonlSessionStorage.create(
        LocalFileSystem(), str(path), cwd=str(path.parent), session_id=path.stem
    )
    session = Session(storage)
    for message in messages:
        await session.append_message(message)


def receipts(run, task_id, number, *, key="sub-000000000001", settle=None, rel=None, path=None):
    start = {
        "v": 1,
        "key": key,
        "phase": "start",
        "tool_call_id": None,
        "profile": "reviewer",
        "task_preview": f"[aelix-workflow run={run.id} task={task_id} attempt={number}]\n\nWorkflow",
        "child": {"session_id": "child", "path": path, "rel": rel} if rel or path else None,
        "permission_mode": "plan",
    }
    entries = [SimpleNamespace(custom_type=recovery.CHILD_SESSION_TYPE, data=start)]
    if settle is not None:
        entries.append(
            SimpleNamespace(
                custom_type=recovery.CHILD_SESSION_TYPE,
                data={**start, "phase": "settle", **settle},
            )
        )
    return recovery.collect_receipts(entries)


def edit_messages(answer="Patched route.py and verified it."):
    call = ToolCallContent("call-1", "edit", {"path": "route.py"})
    return [
        AssistantMessage(content=[call], stop_reason="toolUse"),
        ToolResultMessage(tool_call_id="call-1", content=[TextContent(text="ok")]),
        AssistantMessage(content=[TextContent(text=answer)], stop_reason="end_turn"),
    ]


async def test_a_settled_success_is_recovered_instead_of_rerun(tmp_path):
    parent = tmp_path / "--bucket--" / "parent.jsonl"
    await write_child(
        tmp_path / "--bucket--" / "parent" / "sub-1.jsonl",
        edit_messages('Found one.\n```json\n{"findings": [{"file": "route.py"}]}\n```'),
    )
    run = Workflow.create("Recover", [{**spec("a"), "output_schema": FINDINGS}])
    assert recovery.apply_attempt_record(
        run, {"v": 1, "run": run.id, "task": "a", "attempt": 1, "child_id": "sub-1"}
    )
    assert run.tasks["a"].state == "interrupted"
    found = receipts(
        run,
        "a",
        1,
        key="sub-1",
        rel="parent/sub-1.jsonl",
        settle={
            "status": "ok",
            "usage": {"input": 10, "output": 5, "cost": 0.25},
            "elapsed_ms": 900,
        },
    )
    notes = await recovery.reconcile(run, found, str(parent))
    task = run.tasks["a"]
    assert notes == ["a #1: succeeded (ok)"]
    assert task.state == "succeeded"
    assert task.data == {"findings": [{"file": "route.py"}]}
    assert task.output == "Found one."
    assert task.attempts[0].cost == 0.25
    assert task.attempts[0].tool_trail == 'edit({"path": "route.py"}) ok'
    assert "Recovered after a restart" in task.note
    assert run.state == "awaiting_review"


async def test_the_host_marker_finds_a_child_the_workflow_never_recorded(tmp_path):
    parent = tmp_path / "--bucket--" / "parent.jsonl"
    await write_child(tmp_path / "--bucket--" / "parent" / "sub-9.jsonl", edit_messages()[:1])
    run = Workflow.create("Recover", [spec("a")])
    found = receipts(run, "a", 1, key="sub-9", rel="parent/sub-9.jsonl")
    notes = await recovery.reconcile(run, found, str(parent))
    task = run.tasks["a"]
    assert notes == ["a #1: started before the restart"]
    assert task.state == "interrupted"
    assert task.attempts[0].child_id == "sub-9"
    assert "never settled; it ran edit" in task.note
    assert task.attempts[0].tool_trail == 'edit({"path": "route.py"}) (no result)'


async def test_settled_failures_and_cancellations(tmp_path):
    parent = tmp_path / "--bucket--" / "parent.jsonl"
    run = Workflow.create("Recover", [spec("a"), spec("b")])
    for task_id in ("a", "b"):
        recovery.apply_attempt_record(
            run, {"v": 1, "run": run.id, "task": task_id, "attempt": 1, "child_id": None}
        )
    failed = receipts(
        run,
        "a",
        1,
        key="sub-a",
        settle={"status": "error", "error": "HTTP 402 insufficient credits"},
    )
    cancelled = receipts(run, "b", 1, key="sub-b", settle={"status": "cancelled"})
    await recovery.reconcile(run, {**failed, **cancelled}, str(parent))
    assert run.tasks["a"].state == "failed"
    assert run.tasks["a"].attempts[0].failure == "blocker"
    assert run.tasks["b"].state == "interrupted"
    assert run.tasks["b"].note.startswith("The child was stopped mid-run")


async def test_success_without_a_readable_answer_stays_interrupted(tmp_path):
    parent = tmp_path / "--bucket--" / "parent.jsonl"
    outside = tmp_path / "elsewhere" / "sub-1.jsonl"
    await write_child(outside, edit_messages())
    run = Workflow.create("Recover", [spec("a")])
    recovery.apply_attempt_record(
        run, {"v": 1, "run": run.id, "task": "a", "attempt": 1, "child_id": "sub-1"}
    )
    # A record naming a file outside the parent's own directory is never read.
    found = receipts(run, "a", 1, key="sub-1", path=str(outside), settle={"status": "ok"})
    await recovery.reconcile(run, found, str(parent))
    assert run.tasks["a"].state == "interrupted"
    assert "could not be read" in run.tasks["a"].note


def test_attempt_records_only_extend_the_matching_history():
    run = Workflow.create("Records", [spec("a")])
    other = {"v": 1, "run": "another", "task": "a", "attempt": 1, "child_id": "x"}
    assert not recovery.apply_attempt_record(run, other)
    gap = {"v": 1, "run": run.id, "task": "a", "attempt": 2, "child_id": "x"}
    assert not recovery.apply_attempt_record(run, gap)
    assert not recovery.apply_attempt_record(run, {**gap, "task": "missing", "attempt": 1})
    assert recovery.apply_attempt_record(run, {**gap, "attempt": 1, "child_id": None})
    assert recovery.apply_attempt_record(run, {**gap, "attempt": 1})
    assert run.tasks["a"].attempts[0].child_id == "x"
    assert Workflow.restore(run.to_dict()).tasks["a"].state == "interrupted"


def expanded(run_id=None):
    from test_dynamic import review, succeed, verify

    run = Workflow.create("Generations", [review(), verify()])
    if run_id:
        run.id = run_id
    succeed(run.tasks["review"], {"findings": [{"file": "a.py"}]})
    run.advance()
    return run, succeed, review


async def test_a_receipt_from_an_earlier_expansion_is_never_adopted(tmp_path):
    parent = tmp_path / "--bucket--" / "parent.jsonl"
    await write_child(
        tmp_path / "--bucket--" / "parent" / "sub-old.jsonl", edit_messages("verified a.py")
    )
    run, succeed, review = expanded()
    old_item = (run.tasks["verify"].items or [])[0]
    assert old_item.generation == 1
    # A repair or revision re-expands the group: verify#1 is now a different item.
    run.amend([review(prompt="Again.")], replace=True, reason="Re-review")
    succeed(run.tasks["review"], {"findings": [{"file": "b.py"}]})
    run.advance()
    stale = receipts(
        run, "verify#1", 1, key="sub-old", rel="parent/sub-old.jsonl", settle={"status": "ok"}
    )
    # The old receipt's marker names expansion 1.
    for receipt in stale.values():
        receipt.marker = (run.id, "verify#1", 1, 1)
    notes = await recovery.reconcile(run, stale, str(parent))
    item = (run.tasks["verify"].items or [])[0]
    assert notes == [] and item.state == "pending" and not item.attempts
    # The same receipt for the current expansion is adopted.
    for receipt in stale.values():
        receipt.marker = (run.id, "verify#1", 1, 2)
    await recovery.reconcile(run, stale, str(parent))
    assert item.state == "succeeded"


async def test_a_child_of_an_unsaved_expansion_is_reported_on_its_group(tmp_path):
    run, _succeed, _review = expanded()
    found = receipts(run, "verify#1", 1, key="sub-future")
    for receipt in found.values():
        receipt.marker = (run.id, "verify#1", 1, 5)
    await recovery.reconcile(run, found, str(tmp_path / "parent.jsonl"))
    assert "sub-future" in run.tasks["verify"].note
    assert not (run.tasks["verify"].items or [])[0].attempts


def test_item_markers_and_records_name_their_expansion():
    from aelix_workflow.prompting import marker

    run, _succeed, _review = expanded()
    item = (run.tasks["verify"].items or [])[0]
    item.attempts.append(recovery.Attempt(1))
    assert marker(run, item, 1).endswith("task=verify#1 attempt=1 expansion=1]")
    assert recovery.attempt_record(run, item, item.attempts[0])["expansion"] == 1
    assert recovery.collect_receipts(
        [
            SimpleNamespace(
                custom_type=recovery.CHILD_SESSION_TYPE,
                data={"v": 1, "key": "k", "phase": "start", "task_preview": marker(run, item, 1)},
            )
        ]
    )["k"].marker == (run.id, "verify#1", 1, 1)
