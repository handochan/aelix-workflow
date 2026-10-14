"""Deterministic transport simulation inside the REAL Aelix TUI. No model calls, no commands."""

from __future__ import annotations

import asyncio
import json

from aelix_coding_agent.subagent_contract import SubagentProgress, SubagentResult

from aelix_workflow.engine import CheckOutcome
from aelix_workflow.extension import WorkflowExtension
from aelix_workflow.model import Workflow

FINDINGS = {
    "type": "object",
    "properties": {
        "findings": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"file": {"type": "string"}, "claim": {"type": "string"}},
                "required": ["file", "claim"],
            },
        }
    },
    "required": ["findings"],
}


def setup(aelix):
    controller = WorkflowExtension(aelix)
    aelix.add_cleanup(controller.engine.close)

    async def demo(_args, _ctx):
        run = Workflow.create(
            "SIMULATION: review → verify each finding → fix → test, with one repair round",
            [
                {
                    "id": "review",
                    "title": "Review the session guard",
                    "prompt": "Review",
                    "profile": "demo",
                    "phase": "Review",
                    "output_schema": FINDINGS,
                },
                {
                    "id": "verify",
                    "title": "Verify {{item.file}}",
                    "prompt": "Reproduce {{item.claim}} in {{item.file}}",
                    "profile": "demo",
                    "phase": "Verify",
                    "depends_on": ["review"],
                    "for_each": "review.findings",
                    "max_items": 4,
                },
                {
                    "id": "fix",
                    "title": "Fix the confirmed defects",
                    "prompt": "Fix",
                    "profile": "demo",
                    "phase": "Fix",
                    "depends_on": ["verify"],
                    "writes": ["src/auth/**"],
                },
                {
                    "id": "tests",
                    "title": "Run the auth tests",
                    "kind": "check",
                    "command": ["pytest", "-q", "tests/auth"],
                    "phase": "Check",
                    "depends_on": ["fix"],
                    "repair": {"task": "fix", "max_rounds": 2},
                },
            ],
            3,
        )
        controller.runs[run.id] = run
        controller.selected = run.id
        findings = [
            {"file": "src/auth/session.py", "claim": "expired sessions are accepted"},
            {"file": "src/auth/routes.py", "claim": "admin route skips the guard"},
            {"file": "src/auth/tokens.py", "claim": "refresh token reuse"},
        ]

        async def transport(task, _prompt, on_event):
            ticks = {"review": 4, "fix": 5}.get(task.spec.id, 3 + task.index)
            for tick in range(ticks):
                on_event(
                    SubagentProgress(
                        f"demo-{task.spec.id}-{len(task.attempts)}",
                        "demo",
                        "running",
                        current_tool=("read", "grep", "edit")[min(tick, 2)]
                        if task.spec.id == "fix"
                        else ("read" if tick < 2 else "grep"),
                        elapsed_ms=tick * 700,
                        tokens=(tick + 1) * 380,
                        model="simulation",
                    )
                )
                await asyncio.sleep(0.7)
            if task.spec.id == "review":
                text = "SIMULATED: three suspicious paths.\n```json\n"
                text += json.dumps({"findings": findings}) + "\n```"
            elif task.spec.id == "fix":
                text = f"SIMULATED: patched src/auth (attempt {len(task.attempts)})"
            else:
                text = f"SIMULATED: reproduced {task.item['claim']}"
            return SubagentResult(
                f"demo-{task.spec.id}", "demo", True, "ok", text, elapsed_ms=ticks * 700
            )

        outcomes = iter(
            [
                CheckOutcome(
                    1, "FAILED tests/auth/test_session.py::test_expired - SIMULATED", 2_000
                ),
                CheckOutcome(0, "SIMULATED: 42 passed", 2_000),
            ]
        )

        async def checker(_task):
            await asyncio.sleep(2)
            return next(outcomes)

        await controller.engine.execute(
            run, transport, changed=controller._changed, admit=lambda: True, check=checker
        )
        run.finish(
            "SIMULATION complete. Real child transport, model and commands were not invoked."
        )
        controller._changed(True)
        return "Simulation complete. The panel retains the final workflow state."

    aelix.register_command(
        "workflow-demo", handler=demo, description="Simulate a dynamic workflow in the real TUI"
    )
