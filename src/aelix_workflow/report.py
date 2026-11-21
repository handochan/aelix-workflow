"""Model-facing text: what one run produced, an overview, and one task's full evidence.

A run report repeats nothing the parent has already seen, so replanning does not resend
every earlier result into the parent's context.
"""

from __future__ import annotations

from aelix_workflow.engine import Execution, owes_repair
from aelix_workflow.model import (
    CHECK_TIMEOUT_MS,
    MAX_ATTEMPTS,
    Attempt,
    Task,
    Workflow,
    WorkflowError,
)
from aelix_workflow.panel import (
    attempt_line,
    attempt_symbol,
    clean,
    clean_block,
    scope_lines,
    status_line,
    task_line,
)
from aelix_workflow.structured import compact, describe

REPORT_BUDGET = 12_000
NEXT_STEPS = (
    "Next: inspect a task with workflow_status(run_id, task_id); add or revise tasks for new "
    "findings, retry a failure only after checking its cause and side effects, or finish with "
    "a verified summary. Agent success alone is not acceptance verification."
)


def headline(run: Workflow) -> str:
    return f"{status_line(run)} | attempts {run.attempt_count}/{MAX_ATTEMPTS}"


def run_report(run: Workflow, execution: Execution) -> str:
    lines = [headline(run), f"Goal: {clean(run.goal)} | revision {run.revision}"]
    finished = execution.finished
    if finished:
        lines.append(f"Finished in this run ({len(finished)}):")
        share = max(300, min(1_500, REPORT_BUDGET // len(finished)))
        for task, attempt in finished:
            line = f"{attempt_symbol(attempt)} {attempt_line(task, attempt)}"
            if not _current(run, task):
                line += " (from an expansion that a later step replaced)"
            lines.append(line)
            if attempt.summary.strip():
                lines.append(f"  {_excerpt(attempt.summary, share)}")
            if attempt.data is not None:
                lines.append(f"  data: {_excerpt(compact(attempt.data), share)}")
            if attempt.failure and attempt.error:
                lines.append(f"  error: {_excerpt(attempt.error, 400)}")
            if dropped := _dropped(attempt):
                lines.append(f"  tools unavailable to the child: {dropped}")
            lines.extend(f"  {line}" for line in scope_lines(attempt))
    else:
        lines.append("No task finished in this run.")
    if execution.events:
        lines.append("Graph steps: " + "; ".join(clean(e) for e in execution.events[:12]))
    if execution.halted:
        lines.append(f"Halted: {clean(execution.halted)}")
    attention = [t for t in run.units() if t.state in {"failed", "interrupted"}]
    if attention:
        lines.append(
            "Needs attention: "
            + "; ".join(f"{t.id} ({clean(t.note)[:200] or t.state})" for t in attention[:12])
        )
    lines.extend(_owed(t) for t in run.tasks.values() if owes_repair(t))
    pending = [t for t in run.units() if t.state == "pending"]
    if pending:
        lines.append(
            "Pending: "
            + "; ".join(f"{t.id} ({clean(run.waiting_reason(t))[:120]})" for t in pending[:12])
        )
    if run.summary:
        lines.append(f"Conclusion: {clean(run.summary)}")
    return "\n".join(lines)


def overview(run: Workflow) -> str:
    lines = [headline(run), f"Goal: {clean(run.goal)} | revision {run.revision}"]
    for task in run.iter_all():
        row = ("  " if task.index else "") + task_line(run, task)
        attempt = task.latest
        if task.state == "succeeded" and attempt:
            first = next(iter(attempt.summary.strip().splitlines()), "")
            if first:
                row += f" | {_excerpt(first, 160)}"
        lines.append(row)
        if owes_repair(task):
            lines.append(f"  {_owed(task)}")
    if run.revisions:
        lines.append("Recent replans: " + " | ".join(clean(r)[:120] for r in run.revisions[-3:]))
    if run.summary:
        lines.append(f"Conclusion: {clean(run.summary)}")
    lines.append("workflow_status(run_id, task_id) shows a task's attempts, data and tool trail.")
    return "\n".join(lines)


def task_detail(run: Workflow, task: Task) -> str:
    spec = task.spec
    head = [f"Task {task.id}", task.state, f"kind {spec.kind}"]
    if spec.kind == "agent":
        head += [f"access {spec.access}", f"profile {spec.profile}"]
    head.append(f"phase {spec.phase}")
    if spec.kind == "check":
        head.append(f"timeout {(spec.timeout_ms or CHECK_TIMEOUT_MS) / 1_000:g}s")
    lines = [" | ".join(head), f"Title: {clean(spec.title)}"]
    if task.note:
        lines.append(f"Note: {clean(task.note)}")
    if spec.depends_on:
        lines.append(
            "Depends on: " + ", ".join(f"{d} ({run.tasks[d].state})" for d in spec.depends_on)
        )
    if spec.kind == "check":
        lines.append("Command: " + " ".join(spec.command))
    else:
        lines.append(f"Prompt: {_excerpt(spec.prompt, 1_200)}")
    if spec.writes:
        lines.append("Writes: " + ", ".join(spec.writes))
    if spec.output_schema is not None:
        lines.append(f"Output schema: {_excerpt(compact(spec.output_schema), 800)}")
    if spec.for_each:
        clause = f" where {describe(spec.where)}" if spec.where else ""
        lines.append(f"For each: {spec.for_each}{clause} (max {spec.max_items})")
    if spec.when:
        lines.append(f"When: {describe(spec.when)}")
    if spec.repair:
        lines.append(
            f"Repair: reruns {spec.repair['task']} on failure "
            f"(round {task.rounds}/{spec.repair['max_rounds']})"
        )
    if owes_repair(task):
        lines.append(_owed(task))
    if task.repair_context:
        lines.append(f"Repair context: {_excerpt(task.repair_context, 800)}")
    if task.index:
        lines.append(f"Item {task.index}: {_excerpt(compact(task.item), 800)}")
    if task.items is not None:
        lines.append(f"Items ({len(task.items)}):")
        lines.extend("  " + task_line(run, item) for item in task.items)
    if task.retired:
        lines.append(f"Earlier expansions: {task.retired} attempt(s) retired")
    for attempt in task.attempts:
        lines.append(f"{attempt_symbol(attempt)} attempt {attempt_line(task, attempt)}")
        if attempt.child_id:
            # The session is a record for people and hosts. A live model took it for an agent
            # it could resume; the host refuses, since the workflow, not the model, ran it.
            session = (
                f" | session record {clean(attempt.session_id)} (not resumable with the agent tool)"
                if attempt.session_id
                else ""
            )
            lines.append(f"  child: {clean(attempt.child_id)}{session}")
        if attempt.reason:
            lines.append(f"  ended: {clean(attempt.reason)}")
        if files := _files(attempt.changed_files, attempt.files_overflow):
            lines.append(f"  changed: {files}")
        if files := _files(attempt.unconfirmed_files, 0):
            lines.append(f"  write unconfirmed (stopped mid-write): {files}")
        if attempt.input_tokens or attempt.output_tokens:
            lines.append(f"  tokens: {attempt.input_tokens:,} in / {attempt.output_tokens:,} out")
        if dropped := _dropped(attempt):
            lines.append(f"  tools unavailable to the child: {dropped}")
        if attempt.summary.strip():
            lines.append("  result:\n" + _indent(clean_block(attempt.summary)))
        if attempt.data is not None:
            lines.append(f"  data: {_excerpt(compact(attempt.data), 4_000)}")
        if attempt.error:
            lines.append(f"  error: {clean(attempt.error)}")
        if attempt.tool_trail:
            lines.append("  tool trail:\n" + _indent(clean_block(attempt.tool_trail)))
        lines.extend(f"  {line}" for line in scope_lines(attempt))
    return "\n".join(lines)


def _owed(task: Task) -> str:
    """A repair the next workflow_run takes up, said where the model reads the failed check."""
    target = (task.spec.repair or {}).get("task")
    return (
        f"Repair owed: once its command is approved, the next workflow_run reruns {target} and "
        f"what was built on it for {task.id}; retrying, revising or skipping {task.id} cancels "
        "that."
    )


def _dropped(attempt: Attempt) -> str:
    return ", ".join(attempt.dropped_tools)


def _files(paths: list[str], overflow: int, shown: int = 8) -> str:
    """The first paths and how many more; the host counts past its own cap (overflow)."""
    more = max(0, len(paths) - shown) + overflow
    text = ", ".join(clean(path) for path in paths[:shown])
    if more:
        text += f"{', ' if text else ''}+{more} more"
    return text


def _current(run: Workflow, task: Task) -> bool:
    try:
        return run.find(task.id) is task
    except WorkflowError:
        return False


def _excerpt(text: str, limit: int) -> str:
    text = clean(text)
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _indent(text: str) -> str:
    return "\n".join(f"    {line}" for line in text.splitlines())
