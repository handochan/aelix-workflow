"""The slash command must actually trigger a turn, not only enqueue a request."""

from __future__ import annotations

import asyncio

from aelix_ai.messages import UserMessage
from test_host import bench


async def test_idle_workflow_command_starts_exactly_one_real_host_turn(tmp_path):
    b = await bench(tmp_path)
    await b.controller.command("Inspect this README", None)
    # Let the host's managed trigger-turn task claim its phase.
    await asyncio.sleep(0)
    await asyncio.wait_for(b.harness.wait_for_idle(), 2)
    assert len(b.channel.plans) == 1
    assert b.controller.runs[b.controller.selected].state == "awaiting_review"
    messages = await b.session.get_entries()
    requests = [
        entry
        for entry in messages
        if isinstance(getattr(entry, "message", None), UserMessage)
        and "Inspect this README" in str(entry.message.content)
    ]
    assert len(requests) == 1
    # A later turn must not drain a duplicate of the triggered request.
    await b.harness.prompt("A separate later user turn")
    messages = await b.session.get_entries()
    assert (
        sum(
            isinstance(getattr(entry, "message", None), UserMessage)
            and "Inspect this README" in str(entry.message.content)
            for entry in messages
        )
        == 1
    )
    await b.harness.dispose()
