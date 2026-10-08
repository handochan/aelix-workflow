from __future__ import annotations

import pytest
from test_model import spec
from wcwidth import wcswidth

from aelix_workflow.model import Attempt, Workflow
from aelix_workflow.panel import clean, render_details, render_panel


@pytest.mark.parametrize("width", [12, 40, 80, 100])
def test_panel_fits_cjk_terminals_and_prioritizes_running_failed_tasks(width):
    run = Workflow.create("한국어 목표와 Unicode 경로 검토", [spec(f"t{n}") for n in range(9)])
    run.tasks["t8"].state = "running"
    run.tasks["t8"].current_tool = "read\x1b[2J\nunsafe"
    run.tasks["t7"].state = "failed"
    run.tasks["t7"].note = "failure evidence"
    rows = render_panel(run, width=width, max_rows=3)
    assert all(wcswidth(line) <= width for line in rows)
    assert "t8" in rows[2]
    assert "t7" in rows[3]
    assert not any("\x1b" in line or "\n" in line for line in rows)
    assert len(rows) <= 7


def test_detail_view_keeps_evidence_and_attempt_history_for_all_tasks():
    run = Workflow.create("Review", [spec(f"t{n}") for n in range(9)])
    run.tasks["t8"].attempts = [Attempt(1, "error", "child-1", error="failed check")]
    text = render_details(run)
    assert all(f"t{n}" in text for n in range(9))
    assert "child-1" in text
    assert "failed check" in text
    assert clean("a\r\n\x1b[2Jb\x00") == "a [2Jb"


def test_running_tool_remains_visible_with_a_long_title_at_80_columns():
    run = Workflow.create(
        "Review",
        [
            {
                **spec("read-readme"),
                "title": "Read README.md and report all three workflow tool names",
            }
        ],
    )
    task = run.tasks["read-readme"]
    task.state = "running"
    task.current_tool = "read"
    task.elapsed_ms = 1_500
    rows = render_panel(run, width=80)
    assert "running: read" in rows[2]
    assert "1.5s" in rows[2]
