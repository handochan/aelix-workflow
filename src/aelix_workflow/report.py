"""Model-facing text: what one run produced, an overview, and one task's full evidence.

A run report repeats nothing the parent has already seen, so replanning does not resend
every earlier result into the parent's context.
"""

from __future__ import annotations

from aelix_workflow.engine import Execution
from aelix_workflow.model import MAX_ATTEMPTS, Task, Workflow, WorkflowError
from aelix_workflow.panel import _SYMBOLS, attempt_line, clean, clean_block, status_line, task_line
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
            symbol = "+" if attempt.status == "ok" and not attempt.failure else "!"
            line = f"{symbol} {attempt_line(task, attempt)}"
            if not _current(run, task):
                line += " (from an expansion that a later step replaced)"
            lines.append(line)
            if attempt.summary:
                lines.append(f"  {_excerpt(attempt.summary, share)}")
            if attempt.data is not None:
                lines.append(f"  data: {_excerpt(compact(attempt.data), share)}")
            if attempt.failure and attempt.error:
                lines.append(f"  error: {_excerpt(attempt.error, 400)}")
            if attempt.dropped_tools:
                lines.append(
                    f"  tools unavailable to the child: {', '.join(attempt.dropped_tools)}"
                )
            if attempt.scope_violations:
                lines.append(
                    "  changed outside its declared writes (concurrent tasks may share blame): "
                    + ", ".join(attempt.scope_violations)
                )
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
        if task.state == "succeeded" and attempt and attempt.summary:
            row += f" | {_excerpt(attempt.summary.strip().splitlines()[0], 160)}"
        lines.append(row)
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
    head += [f"phase {spec.phase}", f"timeout {spec.timeout_ms / 1_000:g}s"]
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
        lines.append(f"{_SYMBOLS.get(task.state, '?')} attempt {attempt_line(task, attempt)}")
        if attempt.child_id:
            lines.append(f"  child: {clean(attempt.child_id)}")
        if attempt.input_tokens or attempt.output_tokens:
            lines.append(f"  tokens: {attempt.input_tokens:,} in / {attempt.output_tokens:,} out")
        if attempt.dropped_tools:
            lines.append(f"  tools unavailable to the child: {', '.join(attempt.dropped_tools)}")
        if attempt.summary:
            lines.append("  result:\n" + _indent(clean_block(attempt.summary)))
        if attempt.data is not None:
            lines.append(f"  data: {_excerpt(compact(attempt.data), 4_000)}")
        if attempt.error:
            lines.append(f"  error: {clean(attempt.error)}")
        if attempt.tool_trail:
            lines.append("  tool trail:\n" + _indent(clean_block(attempt.tool_trail)))
        if attempt.scope_violations:
            lines.append("  changed outside writes: " + ", ".join(attempt.scope_violations))
    return "\n".join(lines)


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
