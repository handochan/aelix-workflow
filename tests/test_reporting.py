"""Child prompts, model-facing reports and the TUI panel for dynamic graphs."""

from __future__ import annotations

from test_dynamic import check, review, succeed, verify
from test_model import spec
from wcwidth import wcswidth

from aelix_workflow.engine import Execution
from aelix_workflow.model import Attempt, Workflow
from aelix_workflow.panel import render_details, render_panel
from aelix_workflow.prompting import allocate, build_prompt
from aelix_workflow.report import overview, run_report, task_detail


def test_water_fill_keeps_short_sections_whole():
    assert allocate([10, 1_000, 50], 300) == [10, 240, 50]
    assert allocate([10, 20], 1_000) == [10, 20]
    assert allocate([], 100) == []


def test_prompts_state_the_task_contract():
    run = Workflow.create(
        "Contract",
        [
            {**spec("edit"), "writes": ["src/auth/**"]},
            review(),
            {
                **spec("skipped"),
                "when": {"path": "review.findings", "op": "nonempty"},
                "depends_on": ["review"],
            },
            spec("after", deps=("skipped",)),
        ],
    )
    run.tasks["edit"].attempts.append(Attempt(1))
    run.tasks["edit"].repair_context = "Verification tests failed with exit code 1"
    edit = build_prompt(run, run.tasks["edit"])
    assert edit.splitlines()[0] == f"[aelix-workflow run={run.id} task=edit attempt=1]"
    assert "Change only files matching: src/auth/**" in edit
    assert "do not weaken, skip or delete" in edit
    reviewing = build_prompt(run, run.tasks["review"])
    assert "This is a read-only task" in reviewing
    assert '"required":["findings"]' in reviewing
    succeed(run.tasks["review"], {"findings": []})
    run.advance()
    after = build_prompt(run, run.tasks["after"])
    assert "[skipped]\n(skipped: Condition not met: review.findings nonempty)" in after


def finished_run():
    run = Workflow.create("Report", [spec("old"), spec("new"), {**spec("bad"), "access": "write"}])
    run.tasks["old"].state = "succeeded"
    run.tasks["old"].attempts = [Attempt(1, "ok", summary="OLD RESULT")]
    run.tasks["new"].state = "succeeded"
    run.tasks["new"].attempts = [Attempt(1, "ok", summary="NEW RESULT", data={"n": 1}, cost=0.01)]
    run.tasks["bad"].state = "failed"
    run.tasks["bad"].note = "read_only: ran in plan (read-only) mode"
    run.tasks["bad"].attempts = [
        Attempt(1, "ok", summary="could not edit", failure="read_only", permission_mode="plan")
    ]
    return run


def test_a_run_report_shows_only_what_finished_in_that_run():
    run = finished_run()
    execution = Execution(
        finished=[(run.tasks[t], run.tasks[t].attempts[0]) for t in ("new", "bad")],
        events=["verify expanded into 2 item(s)"],
        halted="bad: an external blocker",
    )
    text = run_report(run, execution)
    assert "Finished in this run (2):" in text
    assert "NEW RESULT" in text and 'data: {"n":1}' in text
    assert "OLD RESULT" not in text
    assert "bad #1 · ok[read_only]" in text and "mode plan" in text
    assert "Graph steps: verify expanded into 2 item(s)" in text
    assert "Halted: bad: an external blocker" in text
    assert "Needs attention: bad (read_only: ran in plan" in text
    many = Workflow.create("Many", [spec(f"t{n}") for n in range(30)])
    for task in many.tasks.values():
        task.state = "succeeded"
        task.attempts = [Attempt(1, "ok", summary="x" * 4_000)]
    big = run_report(many, Execution(finished=[(t, t.attempts[0]) for t in many.tasks.values()]))
    assert len(big) < 20_000


def test_overview_and_task_detail():
    run = Workflow.create("Detail", [review(), verify(), check(deps=("verify",))])
    succeed(run.tasks["review"], {"findings": [{"file": "a.py"}]}, "Found a.py\nmore lines")
    run.advance()
    item = (run.tasks["verify"].items or [])[0]
    item.state = "failed"
    item.attempts = [
        Attempt(
            1,
            "error",
            child_id="sub-7",
            failure="transient",
            error="503",
            tool_trail="read(a.py) ok\nedit(a.py) FAILED",
            scope_violations=["README.md"],
        )
    ]
    run.sync()
    text = overview(run)
    assert "review #1 | succeeded" in text and "| Found a.py" in text
    assert "  ! verify#1 #1 | failed[transient]" in text
    detail = task_detail(run, item)
    assert 'Item 1: {"file":"a.py"}' in detail
    assert "    edit(a.py) FAILED" in detail
    assert "changed outside writes: README.md" in detail
    assert "child: sub-7" in detail
    assert "Command: python -m pytest" in task_detail(run, run.tasks["tests"])


def test_panel_summarizes_groups_and_shows_running_items():
    run = Workflow.create("한국어 패널", [review(), verify()])
    succeed(run.tasks["review"], {"findings": [{"file": "a"}, {"file": "b"}, {"file": "c"}]})
    run.advance()
    first, second, _third = run.tasks["verify"].items or []
    succeed(first)
    second.state = "running"
    second.current_tool = "grep"
    run.sync()
    rows = render_panel(run, width=60, max_rows=6)
    assert all(wcswidth(row) <= 60 for row in rows)
    assert any("verify | running: 1/3 items, 1 running" in row for row in rows)
    assert any(row.startswith("↳ > verify#2 | running: grep") for row in rows)
    assert "verify#3" in render_details(run)


def test_the_worst_case_child_prompt_stays_under_the_ceiling():
    import json

    from aelix_workflow.prompting import MAX_RENDERED_BYTES

    wide: dict = {"type": "object", "properties": {}}
    while len(json.dumps(wide, ensure_ascii=False).encode()) < 3_700:
        wide["properties"][f"p{len(wide['properties'])}"] = {
            "type": "string",
            "description": "설명" * 30,
        }
    run = Workflow.create(
        "목" * 1_300,
        [
            {
                **spec("a"),
                "output_schema": {"type": "object", "properties": {"x": {"type": "string"}}},
            },
            {
                **spec("v", deps=("a",), prompt="가" * 7_990),
                "writes": [f"{'경' * 99}/{n}" for n in range(16)],
                "output_schema": wide,
            },
        ],
    )
    succeed(run.tasks["a"], {"x": "나" * 5_000}, "다" * 4_000)
    run.tasks["v"].attempts.append(Attempt(1))
    run.tasks["v"].repair_context = "라" * 1_300
    prompt = build_prompt(run, run.tasks["v"])
    assert len(prompt.encode("utf-8")) <= MAX_RENDERED_BYTES


def test_a_timed_out_attempt_has_no_empty_result_and_keeps_its_own_symbol():
    run = Workflow.create("Timeout", [spec("a")])
    task = run.tasks["a"]
    task.state = "interrupted"
    task.attempts = [
        Attempt(1, "timeout", summary="\n", failure="timeout", elapsed_ms=120_200),
        Attempt(2, "interrupted", failure="interrupted"),
    ]
    detail = task_detail(run, task)
    assert "result:" not in detail
    assert "! attempt a #1 · timeout" in detail
    assert "~ attempt a #2" in detail
    assert "timeout profile default" in detail
    report = run_report(run, Execution(finished=[(task, task.attempts[0])]))
    assert "! a #1 · timeout" in report
    assert "\n  \n" not in report + "\n"


def test_a_fan_out_title_is_shown_without_raw_placeholders():
    run = Workflow.create(
        "Titles",
        [review(), {**verify(), "title": "Verify {{index}}: {{item.file}}"}],
    )
    group = next(line for line in render_panel(run, width=200) if " verify " in f" {line} ")
    assert "{{" not in group and "Verify …: …" in group
    succeed(run.tasks["review"], {"findings": [{"file": "a.py", "severity": "high"}]})
    run.advance()
    assert "Verify 1: a.py" in render_details(run)


def test_items_of_a_fixed_title_are_told_apart_by_their_item():
    # Live run: the model gave a fan-out a fixed title, so its three rows read identically.
    run = Workflow.create("Same", [review(), {**verify(), "title": "Explain the module"}])
    succeed(run.tasks["review"], {"findings": [{"file": "a.py"}, {"file": "b.py"}]})
    run.advance()
    titles = [item.spec.title for item in run.tasks["verify"].items or []]
    assert titles == ["Explain the module · a.py", "Explain the module · b.py"]


def test_a_blank_summary_never_breaks_the_overview():
    # Review: a succeeded task whose child answered only whitespace made overview() raise.
    run = Workflow.create("Blank", [spec("a"), spec("b")])
    run.tasks["a"].state = "succeeded"
    run.tasks["a"].attempts = [Attempt(1, "ok", summary="\n")]
    text = overview(run)
    assert "+ a #1 | succeeded | [Work] a\n" in text + "\n"
