"""Verify the host's actual abort signal, rather than only a synthetic signal object."""

from __future__ import annotations

import asyncio

from test_host import bench


async def test_harness_abort_reaches_workflow_and_joins_child(tmp_path):
    b = await bench(tmp_path)
    started = asyncio.Event()
    drained = asyncio.Event()

    async def blocking_channel(_plan, *, child=None, on_stream=None):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            drained.set()

    b.channel.run = blocking_channel
    owner = asyncio.create_task(b.harness.prompt("Review using a workflow"))
    await asyncio.wait_for(started.wait(), 1)
    await b.harness.abort()
    await asyncio.wait_for(owner, 2)
    assert drained.is_set()
    run = b.controller.runs[b.controller.selected]
    assert run.state == "cancelled"
    assert run.tasks["inspect"].state == "interrupted"
    assert not b.controller.engine._members
    assert not b.runtime.subagents.list()
    await b.harness.dispose()
