from __future__ import annotations

import dataclasses
from types import SimpleNamespace

import pytest
from aelix_coding_agent.subagent_contract import SubagentResult, SubagentUsage

from aelix_workflow.failures import HALTING, capacity, classify

FULL = (
    "Delegation refused: 1 delegated agent already runs; this limit is set in settings "
    "backgroundJobs.limits.agents. Stop one or wait; do not retry."
)


def result(status="error", *, error=None, summary="", ok=False):
    return SubagentResult("sub-1", "reviewer", ok, status, summary, error=error)


def older(envelope):
    """The envelope as a host without the `started` field returns it."""
    fields = {f.name: getattr(envelope, f.name) for f in dataclasses.fields(envelope)}
    del fields["started"]
    return SimpleNamespace(**fields)


@pytest.mark.parametrize(
    "envelope, kind",
    [
        (result("ok", ok=True), None),
        (result("declined"), "declined"),
        (result("aborted"), "aborted"),
        (result("timeout"), "timeout"),
        (
            result(error="Delegation refused: this session's delegations were stopped (stop_all)."),
            "aborted",
        ),
        (
            result(
                error="Delegation refused: this session already has the maximum number of "
                "live delegated agents (4)."
            ),
            "transient",
        ),
        # With background jobs on, the job service's admission refuses the slot instead.
        (
            result(
                error="Delegation refused: 4 delegated agents already run; this limit is set "
                "in settings backgroundJobs.limits.agents. Stop one or wait; do not retry."
            ),
            "transient",
        ),
        (
            result(
                error="Delegation refused: 1 delegated agent already runs; this limit is set "
                "in settings backgroundJobs.limits.agents. Stop one or wait; do not retry."
            ),
            "transient",
        ),
        (
            result(
                error="Delegation refused: 6 run across every kind, and settings "
                "backgroundJobs.maxTotal allows 6. Stop one or wait; do not retry."
            ),
            "transient",
        ),
        (result(error="HTTP 402: insufficient credits for this request"), "blocker"),
        (result(error="429 You exceeded your current quota, check billing"), "blocker"),
        (result(error="401 Unauthorized"), "blocker"),
        (result(error="model 'gpt-9' is not supported by this endpoint"), "blocker"),
        (result(error="503 Service Unavailable"), "transient"),
        (result(error="Rate limit reached, try again in 20s"), "transient"),
        (result(error="Provider is overloaded"), "transient"),
        (result(error="The test suite failed with 3 errors"), "error"),
        (result(summary="401 responses are missing in route.py"), "blocker"),
        (result(error="child exited 1", summary="the endpoint returns 403"), "error"),
    ],
)
def test_classification(envelope, kind):
    assert classify(envelope) == kind


def test_only_external_stops_and_blockers_halt_the_run():
    assert {"aborted", "blocker"} == HALTING


def test_model_prose_is_never_mistaken_for_an_external_blocker():
    prose = SubagentResult(
        "s",
        "reviewer",
        False,
        "error",
        "Route audit: 401 responses are missing the WWW-Authenticate header.",
        exit_code=1,
        stop_reason="end_turn",
        usage=SubagentUsage(input=1_200, output=300, turns=1),
        started=True,
    )
    assert classify(prose) == "error"
    # An older host's envelope has no started field; its usage alone says the child ran.
    assert classify(older(prose)) == "error"  # type: ignore[arg-type]
    startup = SubagentResult(
        "s", "reviewer", False, "error", "Error: no API key found for openrouter", exit_code=1
    )
    assert classify(startup) == "blocker"


def test_an_envelope_without_a_child_process_is_classified_by_its_own_text():
    # started=False: no child ever ran, so the summary is the host's or the spawn's own
    # sentence whatever the usage numbers say, not model prose.
    refused = SubagentResult(
        "s",
        "reviewer",
        False,
        "error",
        "Delegation refused: 1 delegated agent already runs; this limit is set in settings "
        "backgroundJobs.limits.agents. Stop one or wait; do not retry.",
        stop_reason="end_turn",
        usage=SubagentUsage(input=1, turns=1),
        started=False,
    )
    assert classify(refused) == "transient"
    assert classify(dataclasses.replace(refused, started=True)) == "error"


def test_a_wrap_up_the_host_reports_is_its_own_kind():
    wrapped = SubagentResult(
        "s", "reviewer", False, "aborted", "Partial answer.", stop_reason="stop", started=True
    )
    assert classify(wrapped) == "aborted"
    assert classify(wrapped, end="wrapped_up") == "wrapped_up"
    assert classify(wrapped, end="stopped") == "aborted"
    assert "wrapped_up" not in HALTING


@pytest.mark.parametrize(
    "envelope, refused",
    [
        (result(error=FULL), True),
        (older(result(error=FULL)), True),
        (dataclasses.replace(result(error=FULL), started=True), False),
        (result(error="503 Service Unavailable"), False),
        (result("ok", ok=True), False),
    ],
)
def test_only_a_refusal_before_any_child_is_a_capacity_wait(envelope, refused):
    assert capacity(envelope) is refused
