"""Small live-model check with a registered profile and a read-only tool grant."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--profile", required=True, help="An existing user/global Aelix profile name"
    )
    parser.add_argument(
        "--provider", help="Provider override; otherwise use the user's Aelix default"
    )
    parser.add_argument("--model", help="Model override; otherwise use the user's Aelix default")
    config = parser.parse_args()
    if bool(config.provider) != bool(config.model):
        parser.error("Specify both --provider and --model, or neither")
    evidence = root / "artifacts"
    evidence.mkdir(exist_ok=True)
    job = {
        "action": "create",
        "goal": "Verify live workflow delegation against the extension README",
        "max_concurrency": 1,
        "tasks": [
            {
                "id": "readme",
                "title": "Read the extension README",
                "profile": config.profile,
                "phase": "Smoke",
                "prompt": f"Read {root / 'README.md'} with the read tool. Return WORKFLOW_CHILD_LIVE_OK, its first heading, and the three workflow tool names. Do not modify files.",
                "timeout_ms": 60_000,
            }
        ],
    }
    prompt = (
        "Run exactly this one-task workflow by calling workflow_plan with the supplied JSON, "
        "then workflow_run with the returned run_id. Inspect the actual child result. If it "
        "contains WORKFLOW_CHILD_LIVE_OK and the expected README facts, call workflow_plan "
        "action=finish with an evidence-based summary and return WORKFLOW_PARENT_LIVE_OK. "
        "If it fails, report the failure and do not claim success. Do not add any other tasks "
        f"or run any direct agent calls.\n\n{json.dumps(job)}"
    )
    command = [
        str(Path(sys.executable).with_name("aelix")),
        "--mode",
        "json",
        "--offline",
        "--no-session",
        "--no-context-files",
        "--agents",
        "--trust-extension-path",
        "aelix-workflow",
        "--tools",
        "read,agent,workflow_plan,workflow_run,workflow_status",
        "--permission-mode",
        "yolo",
        "--thinking",
        "low",
    ]
    if config.model:
        command.extend(["--provider", config.provider, "--model", config.model])
    command.append(prompt)
    result = subprocess.run(command, cwd=root, capture_output=True, text=True, timeout=150)
    (evidence / "live-smoke.jsonl").write_text(result.stdout)
    (evidence / "live-smoke.stderr.txt").write_text(result.stderr)
    events = []
    for line in result.stdout.splitlines():
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    tool_events = [event for event in events if event.get("type") == "tool_execution_end"]
    # A sentinel in the echoed USER prompt is not execution evidence.
    messages = [
        event["message"]
        for event in events
        if event.get("type") == "message_end"
        and event.get("message", {}).get("role") == "assistant"
    ]
    assistant_text = "\n".join(
        block.get("text", "")
        for msg in messages
        for block in msg.get("content", [])
        if block.get("type") == "text"
    )
    workflow_results = [
        e.get("result", {}) for e in tool_events if e.get("tool_name") == "workflow_run"
    ]
    result_text = "\n".join(
        block.get("text", "")
        for outcome in workflow_results
        for block in outcome.get("content", [])
    )
    child_ok = "WORKFLOW_CHILD_LIVE_OK" in result_text
    parent_ok = "WORKFLOW_PARENT_LIVE_OK" in assistant_text
    finished = any(
        e.get("result", {}).get("details", {}).get("state") == "completed"
        for e in tool_events
        if e.get("tool_name") == "workflow_plan"
    )
    passed = (
        result.returncode == 0 and child_ok and parent_ok and finished and bool(workflow_results)
    )
    summary = {
        "exit_code": result.returncode,
        "tool_outcomes": [
            {"tool": e.get("tool_name"), "is_error": e.get("is_error")} for e in tool_events
        ],
        "child_sentinel_observed": child_ok,
        "parent_sentinel_observed": parent_ok,
        "passed": passed,
        "workflow_completed": finished,
        "assistant_errors": [
            m.get("error_message") for m in messages if m.get("stop_reason") == "error"
        ],
        "event_count": len(events),
        "stderr_tail": result.stderr[-1_000:],
    }
    (evidence / "live-smoke-summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2)
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
