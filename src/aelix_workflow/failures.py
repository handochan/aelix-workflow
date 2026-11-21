"""Why an attempt failed, and what the scheduler may do about it.

The kinds decide control flow: a halting kind stops new admissions for the current
execution, and only a transient failure of a child that ran no tool may be retried
without the parent's decision, because nothing it did can have changed the workspace.
A capacity refusal is a transient the scheduler waits out instead (`capacity`).
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
    "wrapped_up": (
        "someone using the host's controls asked the child to finish early (no time limit "
        "ended it); its reply is partial"
    ),
}
HALTING = frozenset({"aborted", "blocker"})

# Host refusals are plain error envelopes, so they are matched by their stable wording.
_DRAINING = re.compile(r"delegations were stopped", re.I)
# A slot frees when another child ends. Older hosts count live children per session; with
# background jobs on, the job service's admission counts them per kind and in total.
_CAPACITY = re.compile(
    r"maximum number of live delegated agents"
    r"|already runs?; this limit is set in settings backgroundJobs\.limits\."
    r"|run across every kind, and settings backgroundJobs\.maxTotal allows",
    re.I,
)
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


def classify(result: SubagentResult, *, end: str | None = None) -> str | None:
    """None for a successful envelope; otherwise the failure kind.

    ``end`` is how the host says the child's run ended (its ``ChildEnd``), when it says so:
    a consumed wrap-up is an ``aborted`` envelope that no one stopped.
    """
    if result.ok:
        return None
    if end == "wrapped_up":
        return "wrapped_up"
    if result.status in {"declined", "aborted", "timeout"}:
        return result.status
    # Once the child has talked to a model, its summary is model prose that may quote the
    # very status codes it reviewed. Only a child that never got a model reply (a startup or
    # spawn failure, whose summary is stderr) is classified by its summary. A host that says
    # no child process existed (`started`; older hosts lack the field) settles that by itself.
    silent = getattr(result, "started", None) is False or _no_model_reply(result)
    text = result.error or ((result.summary or "")[:500] if silent else "")
    if _DRAINING.search(text):
        return "aborted"
    if _CAPACITY.search(text):
        return "transient"
    if _BLOCKER.search(text):
        return "blocker"
    if _TRANSIENT.search(text):
        return "transient"
    return "error"


def _no_model_reply(result: SubagentResult) -> bool:
    usage = result.usage
    return not (usage.input or usage.output or usage.turns) and result.stop_reason is None


def capacity(result: SubagentResult) -> bool:
    """Whether the host refused for want of a free agent slot, with proof no child existed.

    The scheduler tries such an attempt again at no cost, so the wording alone is not
    enough: a child that ran may have quoted it. ``started`` is that proof; a host without
    the field gives it only by the silence of an envelope no model reply ever reached.
    """
    if result.ok:
        return False
    started = getattr(result, "started", None)
    no_child = started is False or (started is None and _no_model_reply(result))
    return no_child and bool(_CAPACITY.search(result.error or (result.summary or "")[:500]))


def describe(kind: str | None) -> str:
    return DESCRIPTIONS.get(kind or "", "")
