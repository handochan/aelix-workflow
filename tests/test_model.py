from __future__ import annotations

import copy

import pytest

from aelix_workflow.model import Attempt, Workflow, WorkflowError


def spec(task_id="inspect", *, deps=(), prompt="Inspect and report evidence."):
    return {
        "id": task_id,
        "title": task_id,
        "prompt": prompt,
        "profile": "reviewer",
        "depends_on": list(deps),
    }


@pytest.mark.parametrize(
    "tasks, match",
    [
        ([spec("a", deps=("missing",))], "unknown"),
        ([spec("a", deps=("b",)), spec("b", deps=("a",))], "cycle"),
        ([spec("a"), spec("a")], "Duplicate"),
        ([spec("../a")], "letters"),
        ([{**spec(), "background": True}], "Unknown"),
    ],
)
def test_invalid_graphs_are_refused(tasks, match):
    with pytest.raises(WorkflowError, match=match):
        Workflow.create("Review", tasks)


def test_graph_revision_is_atomic_and_invalidates_transitive_results():
    run = Workflow.create("Review", [spec("a"), spec("b", deps=("a",)), spec("c", deps=("b",))])
    for task in run.tasks.values():
        task.state = "succeeded"
        task.attempts = [Attempt(1, status="ok", summary="old evidence")]
    before = copy.deepcopy(run.to_dict())
    with pytest.raises(WorkflowError, match="cycle"):
        run.amend([spec("a", deps=("c",))], replace=True, reason="new requirement")
    assert run.to_dict() == before
    run.amend(
        [spec("a", prompt="Inspect the newly discovered boundary.")],
        replace=True,
        reason="new finding",
    )
    assert all(task.state == "pending" for task in run.tasks.values())
    assert all(task.output == "" for task in run.tasks.values())
    assert run.tasks["c"].attempts[0].summary == "old evidence"
    assert run.revision == 2


def test_failed_or_skipped_dependencies_do_not_unblock_successors():
    run = Workflow.create("Review", [spec("a"), spec("b", deps=("a",))])
    run.tasks["a"].state = "failed"
    assert run.ready() == []
    run.skip(["a"], "No longer in scope")
    assert run.ready() == []
    with pytest.raises(WorkflowError, match="Unfinished"):
        run.finish("verified")
    run.skip(["b"], "Depends on work removed from scope")
    with pytest.raises(WorkflowError, match="no successful"):
        run.finish("verified")


def test_restore_turns_inflight_work_into_interruption_without_replaying_it():
    run = Workflow.create("Review", [spec("a"), spec("b", deps=("a",))])
    run.tasks["a"].state = "running"
    run.tasks["a"].attempts.append(Attempt(1, child_id="child-1"))
    run.state = "running"
    restored = Workflow.restore(run.to_dict())
    assert restored.tasks["a"].state == "interrupted"
    assert restored.tasks["a"].attempts[0].status == "interrupted"
    assert restored.ready() == []
    restored.retry(["a"], "Confirmed no write effects to reconcile")
    assert [task.spec.id for task in restored.ready()] == ["a"]
    assert restored.attempt_count == 1


def test_successful_graph_requires_explicit_conclusion():
    run = Workflow.create("Review", [spec()])
    run.tasks["inspect"].state = "succeeded"
    run.tasks["inspect"].attempts = [Attempt(1, status="ok", summary="verified")]
    run.refresh_state()
    assert run.state == "awaiting_review"
    run.finish("Verified the route guard through a reproduction test.")
    assert Workflow.restore(run.to_dict()).state == "completed"


@pytest.mark.parametrize("value", [True, 0, 5, "2"])
def test_concurrency_is_bounded_and_typed(value):
    with pytest.raises(WorkflowError):
        Workflow.create("Review", [spec()], value)


def v1_snapshot():
    """A snapshot exactly as version 1 of this extension wrote it."""
    return {
        "schema_version": 1,
        "id": "0123456789ab",
        "goal": "Review",
        "max_concurrency": 2,
        "revision": 2,
        "state": "needs_attention",
        "summary": "",
        "revisions": ["Verify discovered route defect"],
        "tasks": [
            {
                "spec": {**spec("a"), "phase": "Work", "timeout_ms": 120_000},
                "state": "succeeded",
                "attempts": [
                    {
                        "number": 1,
                        "status": "ok",
                        "child_id": "sub-1",
                        "summary": "evidence",
                        "error": None,
                        "elapsed_ms": 10,
                        "input_tokens": 1,
                        "output_tokens": 2,
                        "model": "m",
                    }
                ],
                "note": "",
            },
            {
                "spec": {**spec("b", deps=("a",)), "phase": "Work", "timeout_ms": 120_000},
                "state": "failed",
                "attempts": [
                    {
                        "number": 1,
                        "status": "declined",
                        "child_id": None,
                        "summary": "",
                        "error": "declined",
                        "elapsed_ms": 0,
                        "input_tokens": 0,
                        "output_tokens": 0,
                        "model": None,
                    }
                ],
                "note": "declined",
            },
        ],
    }


def test_version_1_snapshots_migrate_with_failure_kinds():
    run = Workflow.restore(v1_snapshot())
    assert run.tasks["a"].output == "evidence"
    assert run.tasks["a"].attempts[0].failure is None
    assert run.tasks["b"].attempts[0].failure == "declined"
    assert run.tasks["b"].attempts[0].trigger == "run"
    assert run.to_dict()["schema_version"] == 2
    assert Workflow.restore(run.to_dict()).to_dict() == run.to_dict()


@pytest.mark.parametrize(
    "mutate, match",
    [
        (lambda raw: raw.update(schema_version=3), "Unsupported"),
        (lambda raw: raw.update(owner="x"), "Unknown workflow snapshot"),
        (lambda raw: raw["tasks"][0].update(extra=1), "Unknown workflow task"),
        (lambda raw: raw["tasks"][0]["attempts"][0].update(cost=-1), "cost"),
        (lambda raw: raw["tasks"][0]["attempts"][0].update(failure="weird"), "failure kind"),
        (lambda raw: raw["tasks"][0]["attempts"][0].update(extra=1), "fields"),
        (lambda raw: raw["tasks"][1].update(bypassed=True), "condition skip"),
        (lambda raw: raw["tasks"][0].update(items=[]), "Only for_each"),
    ],
)
def test_version_2_snapshots_are_strict(mutate, match):
    raw = Workflow.restore(v1_snapshot()).to_dict()
    mutate(raw)
    with pytest.raises(WorkflowError, match=match):
        Workflow.restore(raw)


def test_a_graph_at_the_task_limit_fits_the_attempt_budget():
    run = Workflow.create("Big", [spec(f"t{n}") for n in range(64)])
    for task in run.tasks.values():
        task.state = "succeeded"
        task.attempts = [Attempt(1, status="ok", summary="done")]
    run.refresh_state()
    run.finish("All 64 tasks verified.")
    assert Workflow.restore(run.to_dict()).state == "completed"
