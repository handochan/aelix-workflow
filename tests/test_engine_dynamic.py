"""Engine behavior: halting, retries, acceptance, dynamic expansion, checks and scopes."""

from __future__ import annotations

import asyncio

import pytest
from aelix_coding_agent.subagent_contract import SubagentProgress, SubagentResult
from test_dynamic import check, review, verify
from test_model import spec

from aelix_workflow.engine import CheckOutcome, CheckRefused, WorkflowEngine
from aelix_workflow.model import MAX_ATTEMPTS, Workflow
from aelix_workflow.prompting import MAX_RENDERED_BYTES
from aelix_workflow.report import run_report


def ok(task, summary="done", **extra):
    return SubagentResult(f"sub-{task.spec.id}", "reviewer", True, "ok", summary, **extra)


def failed(task, status="error", error="boom", **extra):
    return SubagentResult(
        f"sub-{task.spec.id}", "reviewer", False, status, "", error=error, **extra
    )


async def execute(run, delegate, engine=None, **options):
    engine = engine or WorkflowEngine(retry_delays=(0, 0))
    options.setdefault("changed", lambda _: None)
    options.setdefault("admit", lambda: True)
    return await engine.execute(run, delegate, **options)


async def test_an_external_stop_halts_new_work():
    run = Workflow.create("Stop", [spec("a"), spec("b"), spec("c")], 1)
    launched = []

    async def delegate(task, _prompt, _on_event):
        launched.append(task.spec.id)
        return failed(task, "aborted", "stopped")

    execution = await execute(run, delegate)
    assert launched == ["a"]
    assert run.tasks["a"].state == "interrupted"
    assert run.tasks["b"].state == run.tasks["c"].state == "pending"
    assert run.state == "needs_attention"
    assert execution.halted and "stopped from outside" in execution.halted


async def test_a_blocker_opens_the_circuit():
    run = Workflow.create("Credit", [spec(f"t{n}") for n in range(6)], 2)
    launched = []

    async def delegate(task, _prompt, _on_event):
        launched.append(task.spec.id)
        await asyncio.sleep(0)
        return failed(task, error="HTTP 402: insufficient credits")

    execution = await execute(run, delegate)
    assert len(launched) == 2
    assert {run.tasks[t].attempts[0].failure for t in launched} == {"blocker"}
    assert execution.halted and "external blocker" in execution.halted
    assert sum(t.state == "pending" for t in run.tasks.values()) == 4


async def test_side_effect_free_transient_failures_retry_automatically():
    run = Workflow.create("Retry", [spec("a")])
    admissions = 0
    calls = 0

    def admit():
        nonlocal admissions
        admissions += 1
        return True

    async def delegate(task, _prompt, _on_event):
        nonlocal calls
        calls += 1
        return failed(task, error="503 Service Unavailable") if calls == 1 else ok(task)

    await execute(run, delegate, admit=admit)
    task = run.tasks["a"]
    assert task.state == "succeeded"
    assert [(a.status, a.failure, a.trigger) for a in task.attempts] == [
        ("error", "transient", "run"),
        ("ok", None, "auto"),
    ]
    assert admissions == 2


@pytest.mark.parametrize("evidence", ["trail", "progress"])
async def test_transient_failures_after_tool_use_wait_for_the_parent(evidence):
    run = Workflow.create("Retry", [spec("a")])

    async def delegate(task, _prompt, on_event):
        if evidence == "progress":
            on_event(SubagentProgress("sub-a", "reviewer", "running", current_tool="edit"))
            return failed(task, error="503 Service Unavailable")
        return failed(task, error="503 Service Unavailable", tool_trail="edit(a.py) ok")

    await execute(run, delegate)
    task = run.tasks["a"]
    assert task.state == "failed"
    assert len(task.attempts) == 1
    assert task.attempts[0].failure == "transient"


async def test_automatic_retries_are_bounded():
    run = Workflow.create("Retry", [spec("a")])

    async def delegate(task, _prompt, _on_event):
        return failed(task, error="429 rate limit")

    await execute(run, delegate)
    assert len(run.tasks["a"].attempts) == 3
    assert run.tasks["a"].state == "failed"


async def test_a_write_task_that_ran_read_only_fails():
    run = Workflow.create("Edit", [{**spec("impl"), "access": "write"}, spec("review")])

    async def delegate(task, _prompt, _on_event):
        return ok(
            task,
            "I could not edit files in plan mode.",
            permission_mode="plan",
            dropped_tools=("write",),
        )

    await execute(run, delegate)
    impl = run.tasks["impl"]
    assert impl.state == "failed"
    assert impl.attempts[0].failure == "read_only"
    assert impl.attempts[0].dropped_tools == ["write"]
    assert "plan (read-only)" in impl.note
    assert run.tasks["review"].state == "succeeded"


async def test_structured_results_are_validated_and_stored():
    run = Workflow.create(
        "Structured", [review("good"), review("bad"), review("none"), review("long")]
    )
    answers = {
        "good": 'Two issues.\n```json\n{"findings": [{"file": "a.py"}]}\n```',
        "bad": '```json\n{"findings": [{"line": 3}]}\n```',
        "none": "I found nothing worth reporting.",
    }

    async def delegate(task, _prompt, _on_event):
        if task.spec.id == "long":
            full = "x" * 100 + '\n```json\n{"findings": []}\n```'
            return ok(task, "x" * 100 + "[truncated]", truncated=True, details=full)
        return ok(task, answers[task.spec.id])

    await execute(run, delegate)
    good = run.tasks["good"]
    assert good.state == "succeeded"
    assert good.data == {"findings": [{"file": "a.py"}]}
    assert good.output == "Two issues."
    assert run.tasks["bad"].attempts[0].failure == "invalid_output"
    assert "findings[0].file: is required" in run.tasks["bad"].note
    assert run.tasks["none"].attempts[0].failure == "invalid_output"
    assert run.tasks["long"].data == {"findings": []}


async def test_for_each_runs_one_child_per_item_and_aggregates_for_successors():
    item_schema = {"type": "object", "properties": {"confirmed": {"type": "boolean"}}}
    run = Workflow.create(
        "Fan-out",
        [
            review(),
            verify(output_schema=item_schema),
            spec("report", deps=("verify",)),
        ],
        3,
    )
    prompts = {}

    async def delegate(task, prompt, _on_event):
        prompts[task.spec.id] = prompt
        if task.spec.id == "review":
            files = '[{"file": "a.py"}, {"file": "b.py"}, {"file": "c.py"}]'
            return ok(task, f'```json\n{{"findings": {files}}}\n```')
        if task.spec.id.startswith("verify#"):
            confirmed = "true" if task.item["file"] != "b.py" else "false"
            return ok(task, f'Checked.\n```json\n{{"confirmed": {confirmed}}}\n```')
        return ok(task, "report")

    execution = await execute(run, delegate)
    assert "verify expanded into 3 item(s)" in execution.events
    assert "Reproduce b.py (#2)." in prompts["verify#2"]
    assert "[verify#1] Checked." in prompts["report"]
    assert '{"confirmed":false}' in prompts["report"]
    assert run.tasks["verify"].data == [
        {"confirmed": True},
        {"confirmed": False},
        {"confirmed": True},
    ]
    assert run.state == "awaiting_review"


async def test_conditions_skip_branches_without_a_parent_turn():
    run = Workflow.create(
        "Branch",
        [
            review(),
            {
                **spec("fix", deps=("review",)),
                "when": {"path": "review.findings", "op": "nonempty"},
            },
            spec("report", deps=("fix",)),
        ],
    )
    called = []

    async def delegate(task, _prompt, _on_event):
        called.append(task.spec.id)
        return ok(task, '```json\n{"findings": []}\n```' if task.spec.id == "review" else "ok")

    execution = await execute(run, delegate)
    assert called == ["review", "report"]
    assert "fix skipped: condition not met" in execution.events
    assert run.state == "awaiting_review"


def fix_and_test(rounds=2):
    return Workflow.create(
        "Repair",
        [
            {**spec("fix", prompt="Make the tests pass."), "access": "write"},
            check(repair={"task": "fix", "max_rounds": rounds}),
        ],
    )


async def test_a_failed_check_repairs_its_target_and_rechecks():
    run = fix_and_test()
    prompts = []
    outcomes = iter([CheckOutcome(1, "FAILED test_login - KeyError"), CheckOutcome(0, "1 passed")])

    async def delegate(task, prompt, _on_event):
        prompts.append(prompt)
        return ok(task, "edited")

    async def checker(_task):
        return next(outcomes)

    execution = await execute(run, delegate, check=checker)
    assert len(prompts) == 2
    assert "FAILED test_login - KeyError" in prompts[1]
    assert "do not weaken" in prompts[1]
    fix, tests = run.tasks["fix"], run.tasks["tests"]
    assert [a.trigger for a in fix.attempts] == ["run", "repair"]
    assert tests.state == "succeeded" and tests.rounds == 1
    assert tests.data == {"passed": True, "exit_code": 0}
    assert any(event.startswith("repair round 1/2") for event in execution.events)
    assert run.state == "awaiting_review"


async def test_repair_rounds_are_bounded_and_declines_are_final():
    run = fix_and_test(rounds=2)
    fixes = 0

    async def delegate(task, _prompt, _on_event):
        nonlocal fixes
        fixes += 1
        return ok(task, "edited")

    async def always_fails(_task):
        return CheckOutcome(2, "still failing")

    execution = await execute(run, delegate, check=always_fails)
    assert fixes == 3
    assert run.tasks["tests"].attempts[-1].failure == "check_failed"
    assert any("still fails after 2" in e for e in execution.events)
    declined = fix_and_test()

    async def refused(_task):
        raise CheckRefused("The user declined this verification command.")

    await execute(declined, delegate, check=refused)
    assert declined.tasks["tests"].attempts[-1].failure == "declined"
    assert len(declined.tasks["fix"].attempts) == 1


async def test_overlapping_writers_never_run_together():
    run = Workflow.create(
        "Writers",
        [
            {**spec("auth"), "writes": ["src/auth/**"]},
            {**spec("session"), "writes": ["src/auth/session.py"]},
            {**spec("billing"), "writes": ["src/billing/**"]},
            spec("read"),
        ],
        4,
    )
    live: set[str] = set()
    seen_together: list[set[str]] = []

    async def delegate(task, _prompt, _on_event):
        live.add(task.spec.id)
        seen_together.append(set(live))
        await asyncio.sleep(0.02)
        live.discard(task.spec.id)
        return ok(task)

    await execute(run, delegate)
    assert all(not {"auth", "session"} <= group for group in seen_together)
    assert any({"auth", "billing", "read"} <= group for group in seen_together)
    assert run.state == "awaiting_review"


@pytest.mark.parametrize("width", [2, 6])
async def test_korean_dependency_evidence_never_fails_a_successor(width):
    deps = [f"d{n}" for n in range(width)]
    run = Workflow.create(
        "목표", [spec(d) for d in deps] + [spec("v", deps=deps, prompt="검증 " * 2_000)]
    )
    prompts = {}

    async def delegate(task, prompt, _on_event):
        prompts[task.spec.id] = prompt
        return ok(task, "한국어결과보고" * 700)

    await execute(run, delegate)
    assert run.tasks["v"].state == "succeeded"
    assert len(prompts["v"].encode("utf-8")) <= MAX_RENDERED_BYTES
    assert all(f"[{d}]" in prompts["v"] for d in deps)
    # Six full Korean results cannot all fit; each is cut at a character boundary.
    assert ("[… truncated" in prompts["v"]) is (width == 6)


async def test_the_child_link_is_reported_as_soon_as_the_child_starts():
    run = Workflow.create("Link", [spec("a")])
    gate = asyncio.Event()
    links = []

    async def delegate(task, _prompt, on_event):
        on_event(SubagentProgress("sub-live", "reviewer", "running", current_tool="bash"))
        await gate.wait()
        return ok(task)

    owner = asyncio.create_task(
        execute(
            run, delegate, started=lambda task, attempt: links.append((task.id, attempt.child_id))
        )
    )
    await asyncio.sleep(0.05)
    assert links == [("a", "sub-live")]
    assert run.tasks["a"].state == "running"
    gate.set()
    await owner


async def test_stop_on_failure_starts_nothing_after_a_failure():
    run = Workflow.create("Strict", [spec("a"), spec("b"), spec("c")], 1)
    launched = []

    async def delegate(task, _prompt, _on_event):
        launched.append(task.spec.id)
        return failed(task, error="tests failed")

    execution = await execute(run, delegate, stop_on_failure=True)
    assert launched == ["a"]
    assert execution.halted == "Stopped after a failed (stop_on_failure)."


async def test_each_execution_reports_only_its_own_attempts():
    run = Workflow.create("Delta", [spec("a")])

    async def delegate(task, _prompt, _on_event):
        return ok(task)

    engine = WorkflowEngine()
    first = await execute(run, delegate, engine)
    run.amend([spec("b", deps=("a",))], replace=False, reason="More work")
    second = await execute(run, delegate, engine)
    assert [(t.id, a.number) for t, a in first.finished] == [("a", 1)]
    assert [(t.id, a.number) for t, a in second.finished] == [("b", 1)]


async def test_the_attempt_budget_halts_admission():
    run = Workflow.create("Budget", [spec("a"), spec("b")])
    run.tasks["a"].retired = MAX_ATTEMPTS

    async def delegate(task, _prompt, _on_event):
        raise AssertionError("no child may start")

    execution = await execute(run, delegate)
    assert execution.halted and str(MAX_ATTEMPTS) in execution.halted


class FakeWorkspace:
    def __init__(self, changes):
        self.changes = changes
        self.calls = 0

    async def snapshot(self):
        self.calls += 1
        return {} if self.calls == 1 else {path: ("M", 1, 1) for path in self.changes}


async def test_changes_outside_a_declared_scope_are_recorded():
    run = Workflow.create("Scope", [{**spec("auth"), "writes": ["src/auth/**"]}])
    workspace = FakeWorkspace(["src/auth/login.py", "README.md"])

    async def delegate(task, _prompt, _on_event):
        return ok(task)

    await execute(run, delegate, workspace=workspace)
    attempt = run.tasks["auth"].attempts[0]
    assert attempt.scope_violations == ["README.md"]
    assert run.tasks["auth"].state == "succeeded"


async def test_undeclared_writers_and_readers_are_not_observed():
    run = Workflow.create("Scope", [{**spec("any"), "access": "write"}, spec("read")])
    workspace = FakeWorkspace(["anything.py"])

    async def delegate(task, _prompt, _on_event):
        return ok(task)

    await execute(run, delegate, workspace=workspace)
    assert workspace.calls == 0
    assert all(not t.attempts[0].scope_violations for t in run.tasks.values())


async def test_restart_safe_prompt_marker_and_dependency_data():
    run = Workflow.create("Marker", [review(), spec("next", deps=("review",))])
    prompts = {}

    async def delegate(task, prompt, _on_event):
        prompts[task.spec.id] = prompt
        return ok(
            task,
            '```json\n{"findings": [{"file": "a.py"}]}\n```' if task.spec.id == "review" else "ok",
        )

    await execute(run, delegate)
    assert prompts["review"].startswith(f"[aelix-workflow run={run.id} task=review attempt=1]")
    assert "End your reply with exactly one ```json fenced block" in prompts["review"]
    assert 'Result data: {"findings":[{"file":"a.py"}]}' in prompts["next"]


async def test_stop_on_failure_still_lets_a_planned_repair_run():
    run = fix_and_test(rounds=1)
    outcomes = iter([CheckOutcome(1, "1 failed"), CheckOutcome(1, "still 1 failed")])

    async def delegate(task, _prompt, _on_event):
        return ok(task, "edited")

    async def checker(_task):
        return next(outcomes)

    execution = await execute(run, delegate, check=checker, stop_on_failure=True)
    assert len(run.tasks["fix"].attempts) == 2
    assert run.tasks["tests"].rounds == 1
    assert execution.halted == "Stopped after tests failed (stop_on_failure)."


async def test_a_second_cancel_during_teardown_leaves_a_consistent_engine():
    run = Workflow.create("Abort", [spec("a"), spec("b"), spec("c")], 2)
    started = asyncio.Event()

    async def slow_teardown(task, _prompt, _on_event):
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            await asyncio.sleep(0.2)  # The host reaps the child for a while.
            raise

    engine = WorkflowEngine()
    owner = asyncio.create_task(execute(run, slow_teardown, engine))
    await started.wait()
    owner.cancel()
    await asyncio.sleep(0.05)
    owner.cancel()  # Ctrl+C again while the children are still being torn down.
    with pytest.raises(asyncio.CancelledError):
        await owner
    assert run.state == "needs_attention"
    assert not engine._members and not engine.active
    assert {t.state for t in run.tasks.values()} == {"interrupted", "pending"}

    async def quick(task, _prompt, _on_event):
        return ok(task)

    await execute(run, quick, engine)
    assert run.tasks["c"].state == "succeeded"


async def test_a_deeply_nested_result_fails_only_its_own_task():
    run = Workflow.create("Deep", [{**spec("a"), "output_schema": {"type": "array"}}, spec("b")], 2)

    async def delegate(task, _prompt, _on_event):
        if task.spec.id == "a":
            return ok(task, "```json\n" + "[" * 100_000 + "]" * 100_000 + "\n```")
        await asyncio.sleep(0.05)
        return ok(task)

    await execute(run, delegate)
    a = run.tasks["a"]
    assert a.state == "failed" and a.attempts[0].failure == "invalid_output"
    assert "nested too deeply" in a.note
    assert run.tasks["b"].state == "succeeded"


async def test_stop_on_failure_also_stops_after_a_failed_graph_step():
    run = Workflow.create(
        "Strict", [review(), verify(max_items=1), spec("deploy", deps=("review",))], 1
    )
    launched = []

    async def delegate(task, _prompt, _on_event):
        launched.append(task.spec.id)
        return ok(task, '```json\n{"findings": [{"file": "a"}, {"file": "b"}]}\n```')

    execution = await execute(run, delegate, stop_on_failure=True)
    assert launched == ["review"]
    assert execution.halted == "Stopped after verify failed (stop_on_failure)."


async def test_checks_never_run_while_a_writer_changes_files():
    run = Workflow.create(
        "Tree", [{**spec("edit"), "writes": ["src/**"]}, check(deps=()), spec("read")], 3
    )
    live: set[str] = set()
    overlaps = []

    async def delegate(task, _prompt, _on_event):
        live.add(task.spec.id)
        overlaps.append(set(live))
        await asyncio.sleep(0.05)
        live.discard(task.spec.id)
        return ok(task)

    async def checker(task):
        live.add(task.spec.id)
        overlaps.append(set(live))
        await asyncio.sleep(0.05)
        live.discard(task.spec.id)
        return CheckOutcome(0, "ok")

    await execute(run, delegate, check=checker)
    assert all(not {"edit", "tests"} <= group for group in overlaps)
    assert any({"tests", "read"} <= group or {"edit", "read"} <= group for group in overlaps)


async def test_a_report_keeps_results_of_a_replaced_expansion_apart():
    run = Workflow.create(
        "Re-expand",
        [review(), verify(), check(deps=("verify",), repair={"task": "review", "max_rounds": 1})],
        2,
    )
    findings = iter(['[{"file": "a.py"}, {"file": "b.py"}]', '[{"file": "c.py"}]'])
    outcomes = iter([CheckOutcome(1, "1 failed"), CheckOutcome(0, "passed")])

    async def delegate(task, _prompt, _on_event):
        if task.spec.id == "review":
            return ok(task, f'```json\n{{"findings": {next(findings)}}}\n```')
        return ok(task, f"verified {task.item['file']}")

    async def checker(_task):
        return next(outcomes)

    execution = await execute(run, delegate, check=checker)
    text = run_report(run, execution)
    assert "Finished in this run (7):" in text
    assert text.count("(from an expansion that a later step replaced)") == 2
    assert "verified a.py" in text and "verified b.py" in text and "verified c.py" in text


async def test_a_timeout_without_output_explains_itself():
    # Live run: a timed-out child left no summary or error, and the note read "timeout: ".
    run = Workflow.create("Slow", [spec("a")])

    async def delegate(task, _prompt, _on_event):
        return failed(task, "timeout", None, elapsed_ms=120_200)

    await execute(run, delegate)
    task = run.tasks["a"]
    assert task.attempts[0].failure == "timeout"
    assert task.note == (
        "timeout: the child ran out of time after 120s; raise timeout_ms or narrow the task "
        "before retrying"
    )
