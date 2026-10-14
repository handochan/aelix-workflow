"""Restart recovery from the workflow's own attempt records and the host's spawn receipts.

The workflow appends a small record as soon as a child has an ID, and every child prompt
starts with a marker line. Aelix itself writes an ``aelix.child_session`` start record
(whose task preview keeps that marker) before the child process exists, then a settle
record and the child's own session file. A crash can lose the workflow's queued writes but
not the host's start record, so an interrupted attempt is matched by child ID or marker:
a settled success is recovered instead of re-run, and an unsettled child is reported with
the tool calls its session file shows, for an informed retry decision.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from typing import Any

from aelix_agent_core.session import JsonlSessionStorage, LocalFileSystem, Session
from aelix_ai.messages import AssistantMessage, TextContent, ToolCallContent, ToolResultMessage
from aelix_coding_agent.subagent_contract import SubagentResult, SubagentUsage

from aelix_workflow.engine import record_result
from aelix_workflow.model import TRAIL_LIMIT, Attempt, Task, Workflow, WorkflowError
from aelix_workflow.structured import clip_bytes

ATTEMPT_TYPE = "aelix-workflow.attempt"
CHILD_SESSION_TYPE = "aelix.child_session"
_MARKER = re.compile(
    r"\[aelix-workflow run=([A-Za-z0-9]+) task=([A-Za-z0-9_#-]+) attempt=(\d+)"
    r"(?: expansion=(\d+))?\]"
)
_SETTLE_STATUSES = {"ok", "error", "timeout", "aborted"}


def attempt_record(run: Workflow, task: Task, attempt: Attempt) -> dict[str, Any]:
    record = {
        "v": 1,
        "run": run.id,
        "task": task.id,
        "attempt": attempt.number,
        "child_id": attempt.child_id,
    }
    if task.index:
        record["expansion"] = task.generation
    return record


def apply_attempt_record(run: Workflow, data: Any) -> bool:
    """Mark work that started after the run's latest snapshot as interrupted."""
    if not isinstance(data, dict) or data.get("v") != 1 or data.get("run") != run.id:
        return False
    try:
        task = run.find(data.get("task"))
    except WorkflowError:
        return False
    number, child = data.get("attempt"), data.get("child_id")
    if isinstance(number, bool) or not isinstance(number, int):
        return False
    if child is not None and (not isinstance(child, str) or len(child) > 200):
        return False
    if task.items is not None:
        return False
    # A re-expanded item reuses its ID and attempt numbers; only its own expansion counts.
    if data.get("expansion", 0) != task.generation:
        return False
    if number == len(task.attempts) + 1 and task.state in {"pending", "failed", "interrupted"}:
        task.attempts.append(
            Attempt(
                number=number,
                status="interrupted",
                failure="interrupted",
                trigger=task.next_trigger,
                child_id=child,
            )
        )
        task.next_trigger = "run"
        task.state = "interrupted"
        task.note = "Execution interrupted; review side effects before explicit retry."
        return True
    if number == len(task.attempts) and child and task.attempts[-1].child_id is None:
        task.attempts[-1].child_id = child
        return True
    return False


@dataclass
class Receipt:
    key: str
    start: dict[str, Any] | None = None
    settle: dict[str, Any] | None = None
    marker: tuple[str, str, int, int] | None = None


def collect_receipts(entries: list[Any]) -> dict[str, Receipt]:
    receipts: dict[str, Receipt] = {}
    for entry in entries:
        if getattr(entry, "custom_type", None) != CHILD_SESSION_TYPE:
            continue
        data = getattr(entry, "data", None)
        if not isinstance(data, dict) or data.get("v") != 1:
            continue
        key = data.get("key")
        if not isinstance(key, str):
            continue
        receipt = receipts.setdefault(key, Receipt(key))
        if data.get("phase") == "start":
            receipt.start = data
        elif data.get("phase") == "settle":
            receipt.settle = data
        preview = data.get("task_preview")
        match = _MARKER.match(preview) if isinstance(preview, str) else None
        if match:
            receipt.marker = (
                match.group(1),
                match.group(2),
                int(match.group(3)),
                int(match.group(4) or 0),
            )
    return receipts


@dataclass
class Transcript:
    answer: str
    trail: str | None


async def read_transcript(receipt: Receipt, parent_path: str | None) -> Transcript | None:
    """Read the child's own session file; only files under the parent's directory."""
    if not parent_path:
        return None
    child = next(
        (
            record["child"]
            for record in (receipt.start, receipt.settle)
            if record and isinstance(record.get("child"), dict)
        ),
        None,
    )
    if child is None:
        return None
    base = os.path.realpath(os.path.dirname(os.path.abspath(parent_path)))
    candidates = []
    if isinstance(child.get("rel"), str):
        candidates.append(os.path.join(base, *child["rel"].split("/")))
    if isinstance(child.get("path"), str):
        candidates.append(child["path"])
    for candidate in candidates:
        resolved = os.path.realpath(candidate)
        if not resolved.startswith(base + os.sep) or not os.path.isfile(resolved):
            continue
        try:
            storage = await JsonlSessionStorage.open(LocalFileSystem(), resolved)
            return _transcript(await Session(storage).get_branch())
        except Exception:
            return None
    return None


def _transcript(entries: list[Any]) -> Transcript:
    answer = ""
    calls: dict[str, str] = {}
    order: list[str] = []
    verdicts: dict[str, str] = {}
    for entry in entries:
        message = getattr(entry, "message", None)
        if isinstance(message, AssistantMessage):
            texts = [
                b.text for b in message.content if isinstance(b, TextContent) and b.text.strip()
            ]
            if texts:
                answer = "\n".join(texts)
            for block in message.content:
                if isinstance(block, ToolCallContent):
                    arguments = json.dumps(block.input, ensure_ascii=False, default=str)
                    arguments = "".join(c if c.isprintable() else " " for c in arguments)
                    calls[block.tool_call_id] = f"{block.tool_name}({arguments[:120]})"
                    order.append(block.tool_call_id)
        elif isinstance(message, ToolResultMessage):
            verdicts[message.tool_call_id] = "FAILED" if message.is_error else "ok"
    lines = [f"{calls[call]} {verdicts.get(call, '(no result)')}" for call in order]
    trail = clip_bytes("\n".join(lines), TRAIL_LIMIT) if lines else None
    return Transcript(answer, trail)


def _result(receipt: Receipt, transcript: Transcript | None) -> SubagentResult:
    settle = receipt.settle or {}
    raw_usage = settle.get("usage")
    usage: dict[str, Any] = raw_usage if isinstance(raw_usage, dict) else {}
    answer = transcript.answer if transcript else ""

    def number(value: Any) -> int:
        return value if isinstance(value, int) and not isinstance(value, bool) else 0

    status = settle.get("status")
    error = settle.get("error") if isinstance(settle.get("error"), str) else None
    cost = usage.get("cost")
    return SubagentResult(
        id=receipt.key,
        profile=str(settle.get("profile", "")),
        ok=status == "ok",
        status=status,  # type: ignore[arg-type]
        summary=answer or error or "(no recorded answer)",
        usage=SubagentUsage(
            input=number(usage.get("input")),
            output=number(usage.get("output")),
            cost=float(cost) if isinstance(cost, int | float) else 0.0,
        ),
        error=error,
        elapsed_ms=number(settle.get("elapsed_ms")),
        model=settle.get("model") if isinstance(settle.get("model"), str) else None,
        permission_mode=(
            settle.get("permission_mode")
            if isinstance(settle.get("permission_mode"), str)
            else None
        ),
        tool_trail=transcript.trail if transcript else None,
    )


def _note_orphan(run: Workflow, task_id: str, expansion: int, key: str) -> None:
    """A child from an expansion the restored snapshot never saw: report it, never adopt it."""
    group_id = task_id.rpartition("#")[0]
    group = run.tasks.get(group_id)
    if not expansion or group is None or expansion <= group.generation:
        return
    group.note = (
        f"Child {key} of {task_id} from an expansion that was not saved before the restart "
        "may have run; review its work before running this step again."
    )[:500]


async def reconcile(
    run: Workflow, receipts: dict[str, Receipt], parent_path: str | None
) -> list[str]:
    """Settle interrupted attempts from receipts; returns one note per changed attempt."""
    notes: list[str] = []
    by_marker = {r.marker: r for r in receipts.values() if r.marker and r.marker[0] == run.id}
    # The host's start record survives a crash that lost the workflow's own queued record.
    for marker in sorted(by_marker, key=lambda m: (m[3], m[2])):
        _, task_id, number, expansion = marker
        record = {"v": 1, "run": run.id, "task": task_id, "attempt": number}
        if expansion:
            record["expansion"] = expansion
        if apply_attempt_record(run, {**record, "child_id": by_marker[marker].key}):
            notes.append(f"{task_id} #{number}: started before the restart")
        else:
            _note_orphan(run, task_id, expansion, by_marker[marker].key)
    for task in list(run.units()):
        for attempt in task.attempts:
            if attempt.status != "interrupted":
                continue
            receipt = receipts.get(attempt.child_id or "") or by_marker.get(
                (run.id, task.id, attempt.number, task.generation if task.index else 0)
            )
            if receipt is None:
                continue
            attempt.child_id = receipt.key
            transcript = await read_transcript(receipt, parent_path)
            if transcript and transcript.trail and not attempt.tool_trail:
                attempt.tool_trail = transcript.trail
            latest = attempt is task.attempts[-1]
            open_task = latest and task.state == "interrupted"
            settle = receipt.settle
            status = settle.get("status") if settle else None
            if status not in _SETTLE_STATUSES:
                if open_task:
                    ran = "no tool call was recorded"
                    if attempt.tool_trail:
                        names = [line.split("(", 1)[0] for line in attempt.tool_trail.splitlines()]
                        ran = "it ran " + ", ".join(dict.fromkeys(names))
                    lead = (
                        "The child was stopped mid-run"
                        if status == "cancelled"
                        else "Outcome unknown: the child started but never settled"
                    )
                    task.note = f"{lead}; {ran}. Review side effects before retrying."[:500]
                continue
            if not open_task or run.state in {"completed", "cancelled"}:
                continue
            if status == "ok" and not (transcript and transcript.answer.strip()):
                # Success without the answer would hand successors nothing to build on.
                task.note = (
                    "The child finished (ok) before the restart, but its answer could not be "
                    "read from its session file. Review its work before retrying."
                )
                continue
            record_result(task, attempt, _result(receipt, transcript))
            if task.state == "succeeded":
                task.note = "Recovered after a restart from the child's own session record."
            notes.append(f"{task.id} #{attempt.number}: {task.state} ({status})")
    run.sync()
    run.refresh_state()
    return notes
