"""Why an attempt failed, and what the scheduler may do about it.

The kinds decide control flow: a halting kind stops new admissions for the current
execution, and only a transient failure of a child that ran no tool may be retried
without the parent's decision, because nothing it did can have changed the workspace.
"""

from __future__ import annotations

import re

from aelix_coding_agent.subagent_contract import SubagentResult

DESCRIPTIONS = {
    "declined": "the user declined it; do not retry unchanged",
    "aborted": "stopped from outside the workflow; review side effects before retrying",
    "timeout": "the child ran out of time",
    "blocker": "an external blocker (authentication, credit, model or delegation availability)",
    "transient": "a transient provider or capacity failure",
    "invalid_output": "the result did not satisfy the task's output_schema",
    "read_only": "a write task ran without write authority",
    "check_failed": "the verification command failed",
    "error": "the child reported a failure",
    "interrupted": "execution was interrupted",
}
HALTING = frozenset({"aborted", "blocker"})

# Host refusals are plain error envelopes, so they are matched by their stable wording.
_DRAINING = re.compile(r"delegations were stopped", re.I)
_LIVE_CAP = re.compile(r"maximum number of live delegated agents", re.I)
_BLOCKER = re.compile(
    r"\b40[123]\b|unauthori[sz]ed|authenticat|invalid[ _-]?api[ _-]?key|api key|credential"
    r"|insufficient[ _-]?(?:credits?|funds|quota|balance)|out of credits|credit balance"
    r"|billing|payment required|quota exceeded|exceeded your (?:current )?quota"
    r"|model\b.{0,60}\b(?:not found|not supported|unsupported|does not exist|unavailable)"
    r"|no such model|unknown model|not logged in|login required|delegation is unavailable",
    re.I,
)
_TRANSIENT = re.compile(
    r"\b(?:429|500|502|503|504|529)\b|rate[ _-]?limit|overloaded|temporar(?:il)?y unavailable"
    r"|try again|connection (?:reset|refused|error|aborted)|econnreset|network error"
    r"|server error|bad gateway|service unavailable|timed out",
    re.I,
)


def classify(result: SubagentResult) -> str | None:
    """None for a successful envelope; otherwise the failure kind."""
    if result.ok:
        return None
    if result.status in {"declined", "aborted", "timeout"}:
        return result.status
    # Once the child has talked to a model, its summary is model prose that may quote the
    # very status codes it reviewed. Only a child that never got a model reply (a startup or
    # spawn failure, whose summary is stderr) is classified by its summary.
    usage = result.usage
    silent = not (usage.input or usage.output or usage.turns) and result.stop_reason is None
    text = result.error or ((result.summary or "")[:500] if silent else "")
    if _DRAINING.search(text):
        return "aborted"
    if _LIVE_CAP.search(text):
        return "transient"
    if _BLOCKER.search(text):
        return "blocker"
    if _TRANSIENT.search(text):
        return "transient"
    return "error"


def describe(kind: str | None) -> str:
    return DESCRIPTIONS.get(kind or "", "")
