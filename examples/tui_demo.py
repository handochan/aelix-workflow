"""Deterministic transport simulation inside the REAL Aelix TUI. No model calls."""

from __future__ import annotations

import asyncio

from aelix_coding_agent.subagent_contract import SubagentProgress, SubagentResult

from aelix_workflow.extension import WorkflowExtension
from aelix_workflow.model import Workflow


def setup(aelix):
    controller = WorkflowExtension(aelix)
    aelix.add_cleanup(controller.engine.close)

    async def demo(_args, _ctx):
        run = Workflow.create(
            "SIMULATION: inspect → verify → replan from a failed check",
            [
                {
                    "id": "routes",
                    "title": "Inspect route guards",
                    "prompt": "Inspect routes",
                    "profile": "demo",
                    "phase": "Inspect",
                },
                {
                    "id": "sessions",
                    "title": "Inspect session checks",
                    "prompt": "Inspect sessions",
                    "profile": "demo",
                    "phase": "Inspect",
                },
                {
                    "id": "verify",
                    "title": "Verify the finding",
                    "prompt": "Verify evidence",
                    "profile": "demo",
                    "phase": "Verify",
                    "depends_on": ["routes"],
                },
            ],
        )
        controller.runs[run.id] = run
        controller.selected = run.id

        async def transport(task, _prompt, on_event):
            ticks = 8 if task.spec.id == "sessions" else 4
            for tick in range(ticks):
                on_event(
                    SubagentProgress(
                        f"demo-{task.spec.id}",
                        "demo",
                        "running",
                        current_tool="read" if tick < 2 else "grep",
                        elapsed_ms=tick * 750,
                        tokens=(tick + 1) * 420,
                        model="simulation",
                    )
                )
                await asyncio.sleep(0.75)
            return SubagentResult(
                f"demo-{task.spec.id}",
                "demo",
                True,
                "ok",
                "SIMULATED: verification found a missing negative test"
                if task.spec.id == "verify"
                else "SIMULATED: inspected source paths",
                elapsed_ms=ticks * 750,
                model="simulation",
            )

        await controller.engine.execute(
            run, transport, changed=controller._changed, admit=lambda: True
        )
        run.amend(
            [
                {
                    "id": "regression",
                    "title": "Check negative-case regression",
                    "prompt": "Check regression",
                    "profile": "demo",
                    "phase": "Replan",
                    "depends_on": ["verify"],
                }
            ],
            replace=False,
            reason="Verification reported a missing negative-case check",
        )
        controller._changed(True)
        await controller.engine.execute(
            run, transport, changed=controller._changed, admit=lambda: True
        )
        run.finish("SIMULATION complete. Real child transport and model were not invoked.")
        controller._changed(True)
        return "Simulation complete. The panel retains the final workflow state."

    aelix.register_command(
        "workflow-demo", handler=demo, description="Simulate workflow progress in the real TUI"
    )
