from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from aelix_coding_agent.subagent_contract import SubagentProgress, SubagentResult
from test_model import spec

from aelix_workflow.engine import WorkflowEngine
from aelix_workflow.model import MAX_ATTEMPTS, Attempt, Workflow


async def test_pipeline_starts_successors_without_waiting_for_unrelated_slow_work():
    run = Workflow.create("Review", [spec("fast"), spec("slow"), spec("verify", deps=("fast",))])
    slow_started = asyncio.Event()
    verify_started = asyncio.Event()
    peak = 0
    live = 0
    seen = []

    async def delegate(task, prompt, on_event):
        nonlocal live, peak
        live += 1
        peak = max(peak, live)
        seen.append(task.spec.id)
        on_event(SubagentProgress(task.spec.id, "reviewer", "running", current_tool="read"))
        if task.spec.id == "fast":
            await slow_started.wait()
        elif task.spec.id == "slow":
            slow_started.set()
            await asyncio.wait_for(verify_started.wait(), 1)
        else:
            assert "fast evidence" in prompt
            assert run.tasks["slow"].state != "succeeded"
            verify_started.set()
        live -= 1
        return SubagentResult(task.spec.id, "reviewer", True, "ok", f"{task.spec.id} evidence")

    await WorkflowEngine().execute(run, delegate, changed=lambda _: None, admit=lambda: True)
    assert seen == ["fast", "slow", "verify"]
    assert peak == 2
    assert run.state == "awaiting_review"


async def test_failure_stops_dependent_work_but_keeps_independent_evidence():
    run = Workflow.create("Review", [spec("bad"), spec("good"), spec("blocked", deps=("bad",))])
    called = []

    async def delegate(task, _prompt, _on_event):
        called.append(task.spec.id)
        ok = task.spec.id != "bad"
        return SubagentResult(task.spec.id, "reviewer", ok, "ok" if ok else "error", "evidence")

    await WorkflowEngine().execute(run, delegate, changed=lambda _: None, admit=lambda: True)
    assert called == ["bad", "good"]
    assert run.tasks["good"].output == "evidence"
    assert run.tasks["blocked"].state == "pending"
    assert run.state == "needs_attention"


async def test_dynamic_replan_adds_new_work_from_prior_evidence():
    run = Workflow.create("Review", [spec("discover")])

    async def delegate(task, prompt, _on_event):
        if task.spec.id == "verify":
            assert "missing auth check in route.py" in prompt
        return SubagentResult(
            task.spec.id, "reviewer", True, "ok", "missing auth check in route.py"
        )

    engine = WorkflowEngine()
    await engine.execute(run, delegate, changed=lambda _: None, admit=lambda: True)
    run.amend(
        [spec("verify", deps=("discover",))], replace=False, reason="Verify discovered route defect"
    )
    await engine.execute(run, delegate, changed=lambda _: None, admit=lambda: True)
    assert run.tasks["discover"].attempts[0].number == 1
    assert len(run.tasks["discover"].attempts) == 1
    assert run.tasks["verify"].state == "succeeded"


async def test_abort_joins_every_child_and_preserves_finished_results():
    run = Workflow.create("Review", [spec("a"), spec("b"), spec("c")])
    started = asyncio.Event()
    drained = []
    signal = SimpleNamespace(aborted=False)

    async def delegate(task, _prompt, _on_event):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            drained.append(task.spec.id)

    engine = WorkflowEngine()
    owner = asyncio.create_task(
        engine.execute(run, delegate, changed=lambda _: None, admit=lambda: True, signal=signal)
    )
    await started.wait()
    signal.aborted = True
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(owner, 1)
    assert set(drained) == {"a", "b"}
    assert all(t.state == "interrupted" for t in run.tasks.values())
    assert run.state == "cancelled"
    assert not engine._members


async def test_prompt_budget_and_run_budget_refuse_new_children():
    run = Workflow.create("Review", [spec("a"), spec("b")])
    count = 0
    called = []

    def admit():
        nonlocal count
        count += 1
        return count <= 1

    async def delegate(task, _prompt, _on_event):
        called.append(task.spec.id)
        return SubagentResult(task.spec.id, "reviewer", True, "ok", "done")

    engine = WorkflowEngine()
    await engine.execute(run, delegate, changed=lambda _: None, admit=admit)
    assert called == ["a"]
    assert run.tasks["b"].state == "pending"
    assert run.state == "needs_attention"
    run.tasks["a"].attempts.extend(Attempt(n) for n in range(2, MAX_ATTEMPTS + 1))
    await engine.execute(run, delegate, changed=lambda _: None, admit=lambda: True)
    assert called == ["a"]


async def test_delegate_exception_is_a_task_failure():
    run = Workflow.create("Review", [spec()])

    async def delegate(*_args):
        raise OSError("transport failure")

    await WorkflowEngine().execute(run, delegate, changed=lambda _: None, admit=lambda: True)
    assert run.tasks["inspect"].state == "failed"
    assert "transport failure" in run.tasks["inspect"].attempts[-1].error
