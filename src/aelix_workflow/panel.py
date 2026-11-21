"""Compact, terminal-safe live progress and a complete textual detail view."""

from __future__ import annotations

import unicodedata
from collections import Counter

from wcwidth import wcswidth, wcwidth

from aelix_workflow.model import Attempt, Task, Workflow
from aelix_workflow.structured import compact, mask

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


def _short(child_id: str) -> str:
    """Enough of an ID to tell children apart on a narrow row: 'sub-4389…'."""
    return child_id if len(child_id) <= 9 else child_id[:8] + "…"


def state_label(task: Task) -> str:
    attempt = task.latest
    failure = attempt.failure if attempt else None
    if task.state in {"failed", "interrupted"} and failure and failure != task.state:
        return f"{task.state}[{failure}]"
    return task.state


def task_line(run: Workflow, task: Task, *, short_ids: bool = False) -> str:
    if task.items is not None:
        counts = Counter(i.state for i in task.items)
        detail = f"{counts['succeeded']}/{len(task.items)} items"
        for state in ("running", "failed", "interrupted", "skipped"):
            if counts[state]:
                detail += f", {counts[state]} {state}"
    elif task.state == "running":
        # A child waiting on a human's answer is blocked there, not hung in its last tool.
        live = task.approval or task.current_tool or task.note
        detail = clean(live) if live else "checking" if task.spec.kind == "check" else "delegating"
        child = task.latest.child_id if task.latest else None
        if task.approval and child:
            # The approval dialog names the child, not the task; this is how to match them.
            detail += f" · {_short(clean(child)) if short_ids else clean(child)}"
        detail += f" | {task.elapsed_ms / 1_000:.1f}s"
        if task.context_tokens:
            detail += f" | {task.context_tokens:,} ctx tokens"
        if task.stuck:
            detail += f" | stuck: {clean(task.stuck)}"
        if task.wrapping_up:
            detail += " | wrapping up"
    else:
        detail = run.waiting_reason(task)
    label = state_label(task)
    state = f"{label}: {detail}" if detail and detail != task.state else label
    if task.spec.repair and task.rounds:
        state += f" | repair {task.rounds}/{task.spec.repair['max_rounds']}"
    attempt = f" #{len(task.attempts)}" if task.attempts else ""
    # A fan-out's own title is a template; its items carry the rendered titles.
    title = mask(task.spec.title) if task.spec.for_each else task.spec.title
    # The state/current tool must survive narrow terminals and long model-authored titles.
    return f"{_SYMBOLS[task.state]} {task.id}{attempt} | {state} | [{task.spec.phase}] {title}"


def attempt_symbol(attempt: Attempt) -> str:
    """One attempt's own outcome, which can differ from its task's current state."""
    if attempt.status == "running":
        return _SYMBOLS["running"]
    if attempt.failure == "wrapped_up":  # aborted by the host's word, but it concluded
        return _SYMBOLS["failed"]
    if {attempt.status, attempt.failure} & {"interrupted", "aborted"}:
        return _SYMBOLS["interrupted"]
    return _SYMBOLS["failed"] if attempt.failure else _SYMBOLS["succeeded"]


def render_panel(
    run: Workflow, *, width: int = 100, max_rows: int = 6, mode: str | None = None
) -> list[str]:
    policy = f"mode {mode} | " if mode is not None else ""
    lines = [
        f"Dynamic workflow | {policy}{run.state} | r{run.revision} | {run.goal}",
        status_line(run),
    ]
    rows: list[tuple[int, str]] = []

    def row(task: Task, prefix: str = "") -> str:
        line = prefix + task_line(run, task)
        if task.approval and wcswidth(clean(line)) > width:
            line = prefix + task_line(run, task, short_ids=True)
        return line

    for task in run.tasks.values():
        rows.append((_ORDER[task.state], row(task)))
        # Running items are where the live tool activity is; show them under the group.
        rows.extend(
            (_ORDER["running"], row(item, "↳ "))
            for item in task.items or []
            if item.state == "running"
        )
    rows.sort(key=lambda row: row[0])
    lines.extend(text for _, text in rows[:max_rows])
    if len(rows) > max_rows:
        lines.append(f"… {len(rows) - max_rows} more rows | /workflow status")
    if run.revisions:
        lines.append(f"Replan: {run.revisions[-1]}")
    lines.append("/workflow status | /workflow hide | /workflow pause (Ctrl+C for a typed request)")
    return [truncate(line, width) for line in lines]


def scope_lines(attempt: Attempt) -> list[str]:
    """Writes outside the task's scope: the child's own, then what git saw in the shared tree."""
    lines = []
    if own := attempt.own_scope:
        lines.append(
            "wrote outside its declared writes (its own edit/write calls): "
            + ", ".join(clean(path) for path in own)
        )
    if seen := attempt.observed_scope:
        lines.append(
            "changed outside its declared writes, seen by git (concurrent tasks may share "
            "blame): " + ", ".join(clean(path) for path in seen)
        )
    return lines


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
            if attempt.summary.strip():
                lines.append(f"{indent}  result: {clean(attempt.summary)}")
            if attempt.data is not None:
                lines.append(f"{indent}  data: {clean(compact(attempt.data))[:2_000]}")
            if attempt.error:
                lines.append(f"{indent}  error: {clean(attempt.error)}")
            lines.extend(f"{indent}  {line}" for line in scope_lines(attempt))
    if run.summary:
        lines.append(f"Conclusion: {clean(run.summary)}")
    return "\n".join(lines)
