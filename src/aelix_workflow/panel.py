"""Compact, terminal-safe live progress and a complete textual detail view."""

from __future__ import annotations

import unicodedata
from collections import Counter

from wcwidth import wcswidth, wcwidth

from aelix_workflow.model import Attempt, Task, Workflow
from aelix_workflow.structured import compact

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


def clean_block(text: str) -> str:
    """Like clean, but keeps line breaks for multi-line evidence."""
    return "\n".join(clean(line) for line in text.splitlines())


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
    if run.cost:
        parts.append(f"${run.cost:.4f}")
    return " | ".join(parts)


def state_label(task: Task) -> str:
    attempt = task.latest
    failure = attempt.failure if attempt else None
    if task.state in {"failed", "interrupted"} and failure and failure != task.state:
        return f"{task.state}[{failure}]"
    return task.state


def task_line(run: Workflow, task: Task) -> str:
    if task.items is not None:
        counts = Counter(i.state for i in task.items)
        detail = f"{counts['succeeded']}/{len(task.items)} items"
        for state in ("running", "failed", "interrupted", "skipped"):
            if counts[state]:
                detail += f", {counts[state]} {state}"
    elif task.state == "running":
        detail = task.current_tool or ("checking" if task.spec.kind == "check" else "delegating")
        detail += f" | {task.elapsed_ms / 1_000:.1f}s"
        if task.context_tokens:
            detail += f" | {task.context_tokens:,} ctx tokens"
    else:
        detail = run.waiting_reason(task)
    label = state_label(task)
    state = f"{label}: {detail}" if detail and detail != task.state else label
    if task.spec.repair and task.rounds:
        state += f" | repair {task.rounds}/{task.spec.repair['max_rounds']}"
    attempt = f" #{len(task.attempts)}" if task.attempts else ""
    # The state/current tool must survive narrow terminals and long model-authored titles.
    return f"{_SYMBOLS[task.state]} {task.id}{attempt} | {state} | [{task.spec.phase}] {task.spec.title}"


def render_panel(
    run: Workflow, *, width: int = 100, max_rows: int = 6, mode: str | None = None
) -> list[str]:
    policy = f"mode {mode} | " if mode is not None else ""
    lines = [
        f"Dynamic workflow | {policy}{run.state} | r{run.revision} | {run.goal}",
        status_line(run),
    ]
    rows: list[tuple[int, str]] = []
    for task in run.tasks.values():
        rows.append((_ORDER[task.state], task_line(run, task)))
        # Running items are where the live tool activity is; show them under the group.
        rows.extend(
            (_ORDER["running"], "↳ " + task_line(run, item))
            for item in task.items or []
            if item.state == "running"
        )
    rows.sort(key=lambda row: row[0])
    lines.extend(text for _, text in rows[:max_rows])
    if len(rows) > max_rows:
        lines.append(f"… {len(rows) - max_rows} more rows | /workflow status")
    if run.revisions:
        lines.append(f"Replan: {run.revisions[-1]}")
    lines.append("/workflow status | /workflow hide | Ctrl+C pauses execution")
    return [truncate(line, width) for line in lines]


def attempt_line(task: Task, attempt: Attempt) -> str:
    status = attempt.status
    if attempt.failure and attempt.failure != attempt.status:
        status += f"[{attempt.failure}]"
    bits = [f"{task.id} #{attempt.number}", status]
    if attempt.trigger != "run":
        bits.append(attempt.trigger)
    if attempt.elapsed_ms:
        bits.append(f"{attempt.elapsed_ms / 1_000:.1f}s")
    if attempt.cost:
        bits.append(f"${attempt.cost:.4f}")
    if attempt.model:
        bits.append(clean(attempt.model))
    if attempt.permission_mode:
        bits.append(f"mode {clean(attempt.permission_mode)}")
    if attempt.exit_code is not None:
        bits.append(f"exit {attempt.exit_code}")
    return " · ".join(bits)


def render_details(run: Workflow) -> str:
    lines = [status_line(run), f"Goal: {clean(run.goal)} | revision {run.revision}"]
    for task in run.iter_all():
        indent = "  " if task.index else ""
        lines.append(indent + task_line(run, task))
        for attempt in task.attempts:
            line = f"{indent}  attempt {attempt_line(task, attempt)}"
            if attempt.child_id:
                line += f" | child {clean(attempt.child_id)}"
            lines.append(line)
            if attempt.summary:
                lines.append(f"{indent}  result: {clean(attempt.summary)}")
            if attempt.data is not None:
                lines.append(f"{indent}  data: {clean(compact(attempt.data))[:2_000]}")
            if attempt.error:
                lines.append(f"{indent}  error: {clean(attempt.error)}")
            if attempt.scope_violations:
                lines.append(
                    f"{indent}  changed outside writes: {clean(', '.join(attempt.scope_violations))}"
                )
    if run.summary:
        lines.append(f"Conclusion: {clean(run.summary)}")
    return "\n".join(lines)
