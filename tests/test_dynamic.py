"""Graph-level dynamic steps: structured results, for_each, when, checks and repair."""

from __future__ import annotations

import pytest
from test_model import spec

from aelix_workflow.model import MAX_TASKS, Attempt, Workflow, WorkflowError, conflicts

FINDINGS = {
    "type": "object",
    "properties": {
        "findings": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"file": {"type": "string"}, "confirmed": {"type": "boolean"}},
                "required": ["file"],
            },
        },
        "risky": {"type": "boolean"},
    },
    "required": ["findings"],
}


def review(task_id="review", **extra):
    return {**spec(task_id), "output_schema": FINDINGS, **extra}


def verify(**extra):
    return {
        **spec("verify", deps=("review",), prompt="Reproduce {{item.file}} (#{{index}})."),
        "for_each": "review.findings",
        **extra,
    }


def check(task_id="tests", deps=("fix",), **extra):
    return {
        "id": task_id,
        "title": "Run the tests",
        "kind": "check",
        "command": ["python", "-m", "pytest"],
        "depends_on": list(deps),
        **extra,
    }


def succeed(task, data=None, summary="done"):
    task.state = "succeeded"
    task.attempts.append(Attempt(len(task.attempts) + 1, "ok", summary=summary, data=data))


@pytest.mark.parametrize(
    "tasks, match",
    [
        ([spec("review"), verify()], "declares no output_schema"),
        ([review(), {**verify(), "for_each": "other.findings"}], "start with one of"),
        ([review(), {**verify(), "for_each": "review.risky"}], "not an array"),
        (
            [review(), {**verify(), "prompt": "Check {{item.path}}"}],
            "placeholder item.path",
        ),
        ([review(), {**verify(), "where": {"path": "nope", "op": "true"}}], "where.path"),
        (
            [review(), {**spec("x", deps=("review",)), "where": {"path": "", "op": "true"}}],
            "require for_each",
        ),
        (
            [
                review(),
                {**spec("x", deps=("review",)), "when": {"path": "review.nope", "op": "true"}},
            ],
            "when.path is not",
        ),
        ([spec("fix"), check(repair={"task": "other"})], "dependency of the check"),
        (
            [review(), verify(), check(deps=("verify",), repair={"task": "verify"})],
            "single agent task",
        ),
        ([{**check(deps=()), "profile": "reviewer"}], "does not accept"),
        ([{**spec("x"), "command": ["ls"]}], "belong to check tasks"),
        ([{**check(deps=()), "command": ["", "x"]}], "program first"),
        ([{**check(deps=()), "command": ["echo", "a\nb"]}], "printable"),
        ([{**spec("x"), "writes": ["/etc/passwd"]}], "relative paths"),
        ([{**spec("x"), "writes": ["../outside"]}], "relative paths"),
        ([{**spec("x"), "access": "read", "writes": ["a.py"]}], "requires access 'write'"),
        ([{**spec("x"), "kind": "script"}], "kind must be"),
    ],
)
def test_dynamic_plans_are_validated_before_anything_runs(tasks, match):
    with pytest.raises(WorkflowError, match=match):
        Workflow.create("Dynamic", tasks)


def test_writes_imply_write_access_and_are_normalized():
    run = Workflow.create("Edit", [{**spec("x"), "writes": ["./src\\auth/", "README.md"]}])
    assert run.tasks["x"].spec.access == "write"
    assert run.tasks["x"].spec.writes == ("src/auth/**", "README.md")
    restored = Workflow.restore(run.to_dict())
    assert restored.tasks["x"].spec == run.tasks["x"].spec


def test_for_each_expands_filtered_items_into_runnable_tasks():
    run = Workflow.create(
        "Dynamic",
        [
            review(),
            verify(where={"path": "confirmed", "op": "not_equals", "value": "false"}),
            spec("report", deps=("verify",)),
        ],
    )
    succeed(
        run.tasks["review"],
        {"findings": [{"file": "a.py"}, {"file": "b.py", "confirmed": False}, {"file": "c.py"}]},
    )
    assert [t.id for t in run.ready()] == []
    events = run.advance()
    assert events == ["verify expanded into 2 item(s)"]
    items = run.tasks["verify"].items or []
    assert [i.id for i in items] == ["verify#1", "verify#2"]
    assert items[1].spec.prompt == "Reproduce c.py (#2)."
    assert [t.id for t in run.ready()] == ["verify#1", "verify#2"]
    assert run.tasks["verify"].state == "pending"
    assert run.find("verify#2") is items[1]
    for item in items:
        succeed(item, {"ok": True}, f"verified {item.item['file']}")
    run.sync()
    assert run.tasks["verify"].state == "succeeded"
    assert run.tasks["verify"].data == [{"ok": True}, {"ok": True}]
    assert [t.id for t in run.ready()] == ["report"]
    restored = Workflow.restore(run.to_dict())
    assert restored.tasks["verify"].items is not None
    assert restored.tasks["verify"].items[1].spec.prompt == "Reproduce c.py (#2)."
    assert restored.to_dict() == run.to_dict()


def test_empty_and_oversized_fan_outs():
    run = Workflow.create(
        "Dynamic", [review(), verify(max_items=2), spec("report", deps=("verify",))]
    )
    succeed(run.tasks["review"], {"findings": []})
    run.advance()
    assert run.tasks["verify"].state == "succeeded"
    assert [t.id for t in run.ready()] == ["report"]
    big = Workflow.create("Dynamic", [review(), verify(max_items=2)])
    succeed(big.tasks["review"], {"findings": [{"file": "a"}, {"file": "b"}, {"file": "c"}]})
    big.advance()
    assert big.tasks["verify"].state == "failed"
    assert "max_items is 2" in big.tasks["verify"].note
    big.amend([verify(max_items=3)], replace=True, reason="Allow all three findings")
    big.advance()
    assert len(big.tasks["verify"].items or []) == 3


def test_fan_out_respects_the_task_limit():
    run = Workflow.create(
        "Dynamic", [review(), verify(max_items=16)] + [spec(f"t{n}") for n in range(MAX_TASKS - 10)]
    )
    succeed(run.tasks["review"], {"findings": [{"file": str(n)} for n in range(16)]})
    run.advance()
    assert run.tasks["verify"].state == "failed"
    assert "would exceed" in run.tasks["verify"].note


def test_false_condition_skips_but_satisfies_successors():
    run = Workflow.create(
        "Dynamic",
        [
            review(),
            {
                **spec("fix", deps=("review",)),
                "when": {"path": "review.findings", "op": "nonempty"},
            },
            spec("report", deps=("fix",)),
        ],
    )
    succeed(run.tasks["review"], {"findings": []})
    assert run.ready() == []
    assert run.advance() == ["fix skipped: condition not met"]
    fix = run.tasks["fix"]
    assert fix.state == "skipped" and fix.bypassed
    assert [t.id for t in run.ready()] == ["report"]
    manual = Workflow.create("Manual", [spec("a"), spec("b", deps=("a",))])
    manual.skip(["a"], "Out of scope")
    assert manual.ready() == []


def test_revising_the_source_retires_a_fan_out_without_resetting_the_budget():
    run = Workflow.create("Dynamic", [review(), verify()])
    succeed(run.tasks["review"], {"findings": [{"file": "a"}, {"file": "b"}]})
    run.advance()
    for item in run.tasks["verify"].items or []:
        item.state = "failed"
        item.attempts.append(Attempt(1, "error", failure="error", cost=0.5))
    run.sync()
    assert run.tasks["verify"].state == "failed"
    before = run.attempt_count
    run.amend([review(prompt="Look again.")], replace=True, reason="Narrower review")
    assert run.tasks["verify"].items is None
    assert run.tasks["verify"].state == "pending"
    assert run.attempt_count == before
    assert run.cost == pytest.approx(1.0)
    assert Workflow.restore(run.to_dict()).attempt_count == before


def test_items_are_retried_and_skipped_by_id():
    run = Workflow.create("Dynamic", [review(), verify(), spec("report", deps=("verify",))])
    succeed(run.tasks["review"], {"findings": [{"file": "a"}, {"file": "b"}]})
    run.advance()
    first, second = run.tasks["verify"].items or []
    succeed(first)
    second.state = "failed"
    second.attempts.append(Attempt(1, "error", failure="error"))
    run.sync()
    with pytest.raises(WorkflowError, match="Unfinished"):
        run.finish("done")
    run.retry(["verify"], "Retry the failed item")
    assert second.state == "pending" and second.next_trigger == "retry"
    second.state = "failed"
    run.sync()
    run.skip(["verify#2"], "Not reproducible in this environment")
    assert run.tasks["verify"].state == "succeeded"
    assert [t.id for t in run.ready()] == ["report"]


def test_repair_reruns_the_target_with_the_failure_and_counts_rounds():
    run = Workflow.create(
        "Fix",
        [
            {**spec("fix"), "access": "write"},
            spec("docs", deps=("fix",)),
            check(repair={"task": "fix", "max_rounds": 2}),
        ],
    )
    succeed(run.tasks["fix"])
    succeed(run.tasks["docs"])
    tests = run.tasks["tests"]
    tests.state = "failed"
    tests.attempts.append(
        Attempt(1, "error", failure="check_failed", summary="1 failed", exit_code=1)
    )
    assert run.repair(tests) == "applied"
    assert run.tasks["fix"].state == "pending"
    assert run.tasks["fix"].next_trigger == "repair"
    assert "exit code 1" in run.tasks["fix"].repair_context
    assert "1 failed" in run.tasks["fix"].repair_context
    # Every result built on the old fix is stale, not only the check.
    assert run.tasks["docs"].state == "pending"
    assert tests.state == "pending" and tests.rounds == 1
    restored = Workflow.restore(run.to_dict())
    assert restored.tasks["tests"].rounds == 1
    assert restored.tasks["fix"].repair_context == run.tasks["fix"].repair_context
    tests.state = "failed"
    run.tasks["docs"].state = "running"
    assert run.repair(tests) == "deferred"
    run.tasks["docs"].state = "succeeded"
    assert run.repair(tests) == "applied"
    tests.state = "failed"
    assert run.repair(tests) == "exhausted"
    run.retry(["tests"], "Try again after a manual fix")
    assert tests.rounds == 0


def test_pause_keeps_unstarted_work_and_cancel_ends_the_run():
    run = Workflow.create("Pause", [spec("a"), spec("b"), spec("c", deps=("a",))])
    run.tasks["a"].state = "running"
    run.tasks["a"].attempts.append(Attempt(1))
    run.pause("Paused by the user")
    assert run.tasks["a"].state == "interrupted"
    assert run.tasks["a"].attempts[0].failure == "interrupted"
    assert run.tasks["b"].state == "pending" and run.tasks["c"].state == "pending"
    assert run.state == "needs_attention"
    run.amend([spec("d")], replace=False, reason="Paused runs stay editable")
    run.cancel()
    assert run.state == "cancelled"
    assert {t.state for t in run.tasks.values()} == {"interrupted"}
    with pytest.raises(WorkflowError, match="cancelled"):
        run.amend([spec("e")], replace=False, reason="Cancelled runs are closed")


def test_checks_and_writers_never_share_the_tree():
    run = Workflow.create(
        "Scopes",
        [
            {**spec("auth"), "writes": ["src/auth/**"]},
            {**spec("billing"), "writes": ["src/billing/**"]},
            {**spec("session"), "writes": ["src/auth/session.py"]},
            {**spec("anywhere"), "access": "write"},
            spec("read"),
            check(deps=()),
        ],
    )
    t = run.tasks
    assert not conflicts(t["auth"], t["billing"])
    assert conflicts(t["auth"], t["session"])
    assert conflicts(t["anywhere"], t["billing"])
    assert not conflicts(t["read"], t["anywhere"])
    assert conflicts(t["tests"], t["auth"])
    assert not conflicts(t["tests"], t["read"])
    t["auth"].state = "running"
    assert run.waiting_reason(t["session"]) == "waiting for auth (shared files)"


def test_repair_leaves_decisions_and_risky_failures_to_the_parent():
    run = Workflow.create(
        "Decisions",
        [
            {**spec("fix"), "access": "write"},
            check(repair={"task": "fix", "max_rounds": 2}),
            spec("deploy", deps=("fix",)),
            spec("risky", deps=("fix",)),
            spec("migrate", deps=("fix",)),
            spec("flaky", deps=("fix",)),
            spec("docs", deps=("fix",)),
            check("lint", deps=("fix",)),
        ],
    )
    succeed(run.tasks["fix"])
    states = {
        "deploy": ("failed", "declined"),
        "risky": ("skipped", None),
        "migrate": ("interrupted", "interrupted"),
        "flaky": ("failed", "error"),
        "lint": ("failed", "check_failed"),
    }
    for task_id, (state, failure) in states.items():
        task = run.tasks[task_id]
        task.state = state
        if failure:
            task.attempts.append(Attempt(1, "error", failure=failure))
    succeed(run.tasks["docs"])
    tests = run.tasks["tests"]
    tests.state = "failed"
    tests.attempts.append(Attempt(1, "error", failure="check_failed", exit_code=1))
    assert run.repair(tests) == "applied"
    after = {t: run.tasks[t].state for t in ("fix", "tests", "docs", "lint")}
    assert after == {"fix": "pending", "tests": "pending", "docs": "pending", "lint": "pending"}
    for task_id, (state, _failure) in states.items():
        if task_id != "lint":
            assert run.tasks[task_id].state == state


def test_each_expansion_has_its_own_generation():
    run = Workflow.create("Generations", [review(), verify()])
    succeed(run.tasks["review"], {"findings": [{"file": "a"}]})
    run.advance()
    assert run.tasks["verify"].generation == 1
    assert (run.tasks["verify"].items or [])[0].generation == 1
    run.amend([review(prompt="Again.")], replace=True, reason="Re-review")
    succeed(run.tasks["review"], {"findings": [{"file": "b"}]})
    run.advance()
    item = (run.tasks["verify"].items or [])[0]
    assert item.id == "verify#1" and item.generation == 2
    restored = Workflow.restore(run.to_dict())
    assert (restored.tasks["verify"].items or [])[0].generation == 2


def test_a_succeeded_group_must_keep_its_items():
    run = Workflow.create("Tamper", [review(), verify(), spec("after", deps=("verify",))])
    raw = run.to_dict()
    raw["tasks"][1]["state"] = "succeeded"
    with pytest.raises(WorkflowError, match="keep its items"):
        Workflow.restore(raw)


def test_write_scopes_are_bounded_in_bytes():
    with pytest.raises(WorkflowError, match="relative paths"):
        Workflow.create("Bytes", [{**spec("x"), "writes": ["𝔸" * 80]}])
