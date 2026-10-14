from __future__ import annotations

import pytest
from aelix_coding_agent.subagent_contract import SubagentResult

from aelix_workflow.failures import HALTING, classify


def result(status="error", *, error=None, summary="", ok=False):
    return SubagentResult("sub-1", "reviewer", ok, status, summary, error=error)


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
    from aelix_coding_agent.subagent_contract import SubagentUsage

    prose = SubagentResult(
        "s",
        "reviewer",
        False,
        "error",
        "Route audit: 401 responses are missing the WWW-Authenticate header.",
        exit_code=1,
        stop_reason="end_turn",
        usage=SubagentUsage(input=1_200, output=300, turns=1),
    )
    assert classify(prose) == "error"
    startup = SubagentResult(
        "s", "reviewer", False, "error", "Error: no API key found for openrouter", exit_code=1
    )
    assert classify(startup) == "blocker"
