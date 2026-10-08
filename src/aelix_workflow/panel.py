"""Compact, terminal-safe live progress and a complete textual detail view."""

from __future__ import annotations

import unicodedata
from collections import Counter

from wcwidth import wcswidth, wcwidth

from aelix_workflow.model import Task, Workflow

WIDGET_KEY = "aelix-workflow:progress"
STATUS_KEY = "aelix-workflow:status"
_SYMBOLS = {
    "pending": "o",
    "running": ">",
    "succeeded": "+",
    "failed": "!",
    "interrupted": "~",
    "skipped": "-",
}
_ORDER = {"running": 0, "failed": 1, "interrupted": 2, "pending": 3, "succeeded": 4, "skipped": 5}


def clean(text: str) -> str:
    return " ".join(
        "".join(c if not unicodedata.category(c).startswith("C") else " " for c in text).split()
    )


def truncate(text: str, width: int) -> str:
    text = clean(text)
    if width <= 0:
        return ""
    if wcswidth(text) <= width:
        return text
    out: list[str] = []
    size = 0
    for char in text:
        cells = max(0, wcwidth(char))
        if size + cells > width - 1:
            break
        out.append(char)
        size += cells
    return "".join(out) + "…"


def status_line(run: Workflow) -> str:
    counts = Counter(t.state for t in run.tasks.values())
    parts = [f"Workflow {run.id}", run.state, f"{counts['succeeded']}/{len(run.tasks)} succeeded"]
    for state in ("running", "failed", "interrupted"):
        if counts[state]:
            parts.append(f"{counts[state]} {state}")
    return " | ".join(parts)


def task_line(run: Workflow, task: Task) -> str:
    if task.state == "running":
        detail = task.current_tool or "delegating"
        detail += f" | {task.elapsed_ms / 1_000:.1f}s"
        if task.context_tokens:
            detail += f" | {task.context_tokens:,} ctx tokens"
    else:
        detail = run.waiting_reason(task) or task.state
    attempt = f" #{len(task.attempts)}" if task.attempts else ""
    state = task.state if detail == task.state else f"{task.state}: {detail}"
    # The state/current tool must survive narrow terminals and long model-authored titles.
    return f"{_SYMBOLS[task.state]} {task.spec.id}{attempt} | {state} | [{task.spec.phase}] {task.spec.title}"


def render_panel(
    run: Workflow, *, width: int = 100, max_rows: int = 6, mode: str | None = None
) -> list[str]:
    policy = f"mode {mode} | " if mode is not None else ""
    lines = [
        f"Dynamic workflow | {policy}{run.state} | r{run.revision} | {run.goal}",
        status_line(run),
    ]
    tasks = sorted(run.tasks.values(), key=lambda t: _ORDER[t.state])
    lines.extend(task_line(run, task) for task in tasks[:max_rows])
    if len(tasks) > max_rows:
        lines.append(f"… {len(tasks) - max_rows} more tasks | /workflow status")
    if run.revisions:
        lines.append(f"Replan: {run.revisions[-1]}")
    lines.append("/workflow status | /workflow hide | Ctrl+C interrupts execution")
    return [truncate(line, width) for line in lines]


def render_details(run: Workflow) -> str:
    lines = [status_line(run), f"Goal: {clean(run.goal)} | revision {run.revision}"]
    for task in run.tasks.values():
        lines.append(task_line(run, task))
        for attempt in task.attempts:
            line = f"  attempt {attempt.number}: {attempt.status}"
            if attempt.child_id:
                line += f" | child {attempt.child_id}"
            if attempt.model:
                line += f" | {clean(attempt.model)}"
            lines.append(line)
            if attempt.summary:
                lines.append(f"  result: {clean(attempt.summary)}")
            if attempt.error:
                lines.append(f"  error: {clean(attempt.error)}")
    if run.summary:
        lines.append(f"Conclusion: {clean(run.summary)}")
    return "\n".join(lines)
