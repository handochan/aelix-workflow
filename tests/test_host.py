"""Real harness, hook bus, delegation runtime and JSONL session; only child transport is fake."""

from __future__ import annotations

import asyncio
import copy
import dataclasses
import json
from types import SimpleNamespace

import pytest
from aelix_agent_core.harness.core import AgentHarness, AgentHarnessOptions
from aelix_agent_core.harness.hooks import ToolCallResult
from aelix_agent_core.session import JsonlSessionStorage, LocalFileSystem, Session
from aelix_agents import AgentsExtension
from aelix_agents.print_channel import narrow_tools
from aelix_ai.messages import AssistantMessage, TextContent, ToolCallContent
from aelix_ai.streaming import AssistantEndEvent, AssistantStartEvent, Model
from aelix_ai.tools import ToolExecutionContext
from aelix_coding_agent import subagent_contract
from aelix_coding_agent.builtin.permission_mode import PermissionMode, PermissionPosture
from aelix_coding_agent.extensions.api import Extension, ExtensionAPI, _ExtensionRuntime
from aelix_coding_agent.subagent_contract import SubagentResult
from test_model import spec

from aelix_workflow import failures
from aelix_workflow.extension import SNAPSHOT_TYPE, WorkflowExtension, setup
from aelix_workflow.panel import WIDGET_KEY, task_line

CONTEXT = ToolExecutionContext()


class RecordingChannel:
    def __init__(self):
        self.plans = []

    async def run(self, plan, *, child=None, on_stream=None):
        self.plans.append(plan)
        if child is not None:
            child.state = "running"
            child.stream.current_tool = "read"
            child.stream.tokens = 42
            if on_stream is not None:
                on_stream(child.stream)
            child.state = "done"
        # As the real channel reports it: what the profile requested but the child cannot have.
        dropped = narrow_tools(plan.resolved.profile, plan.parent_tools).dropped
        return SubagentResult(
            plan.id, plan.resolved.name, True, "ok", "checked route.py:12", dropped_tools=dropped
        )


class RecordingUI:
    def __init__(self, answer=None):
        self.answer = answer
        self.widgets = []
        self.status = []
        self.dialogs = []

    def set_widget(self, key, content, options=None):
        self.widgets.append((key, copy.deepcopy(content)))

    def set_status(self, key, text):
        self.status.append((key, text))

    async def select(self, title, options, opts=None):
        self.dialogs.append((title, options))
        return self.answer


@pytest.fixture
async def jobs(monkeypatch):
    """Background jobs on, the host's default: a real job service, as its CLI builds one.

    Without one, the host leaves every delegation to its per-session live-child count.
    """
    from aelix_coding_agent.jobs import activation
    from aelix_coding_agent.jobs.service import JobService

    # The host's documented test seam: the gate on, and not switched off by a setting.
    monkeypatch.setattr(activation, "ACTIVE", True)
    monkeypatch.setattr(activation, "_switched_off", False)
    settings: dict = {}  # backgroundJobs, read at each reservation
    service = JobService(settings=lambda: settings)
    activation.install_service(service)
    yield SimpleNamespace(service=service, settings=settings)
    await service.shutdown()
    activation.install_service(None)


async def bench(
    tmp_path,
    *,
    ui=None,
    permission=PermissionMode.DEFAULT,
    deny_run=False,
    approval="inherit",
    project_trusted=False,
    mode="on",
    tools="[read, write]",
    jobs=None,
):
    agent_dir = tmp_path / "agent"
    profiles = agent_dir / "agents"
    profiles.mkdir(parents=True)
    (profiles / "reviewer.md").write_text(
        "---\nname: reviewer\ndescription: Inspect and verify evidence\n"
        + (f"tools: {tools}\n" if tools is not None else "")
        + f"approval_mode: {approval}\n---\nRead the evidence.\n"
    )
    session = Session(
        await JsonlSessionStorage.create(
            LocalFileSystem(),
            str(tmp_path / "parent.jsonl"),
            cwd=str(tmp_path),
            session_id="parent",
        )
    )
    runtime = _ExtensionRuntime()
    if ui is not None:
        runtime.bind_ui(ui)
    agent_record = Extension(name="aelix-agents")
    channel = RecordingChannel()
    agents = AgentsExtension(
        posture=PermissionPosture(mode=permission),
        agent_dir=str(agent_dir),
        cwd=str(tmp_path),
        channel=channel,
        project_trusted=project_trusted,
        # As cli/entry.py wires it, so the host records its spawn receipts (#199).
        session=lambda: session,
    )
    agents(ExtensionAPI(agent_record, runtime))
    record = Extension(name="aelix-workflow")
    controller = WorkflowExtension(ExtensionAPI(record, runtime), mode=mode)
    controller.install()
    records = [agent_record, record]
    if jobs is not None:
        from aelix_coding_agent.jobs.extension import JobsExtension

        # As cli/entry.py wires it: the service's own extension comes first.
        jobs_record = Extension(name=JobsExtension.name)
        JobsExtension(jobs.service)(ExtensionAPI(jobs_record, runtime))
        records.insert(0, jobs_record)
    if deny_run:
        guard = Extension(name="guard")

        def deny(event, _ctx):
            if event.tool_name == "workflow_run":
                return ToolCallResult(block=True, reason="Workflow execution denied by the guard")

        guard.handlers["tool_call"] = [deny]
        records.insert(0, guard)

    step = 0

    async def stream(_model, context, _options):
        nonlocal step
        step += 1
        if step == 1:
            call = ToolCallContent(
                "plan", "workflow_plan", {"action": "create", "goal": "Review", "tasks": [spec()]}
            )
        elif step == 2:
            call = ToolCallContent("run", "workflow_run", {"run_id": controller.selected})
        else:
            yield AssistantStartEvent(partial=AssistantMessage(content=[]))
            yield AssistantEndEvent(
                message=AssistantMessage(
                    content=[TextContent(text="Review complete")], stop_reason="end_turn"
                )
            )
            return
        yield AssistantStartEvent(partial=AssistantMessage(content=[]))
        yield AssistantEndEvent(message=AssistantMessage(content=[call], stop_reason="toolUse"))

    harness = AgentHarness(
        AgentHarnessOptions(
            model=Model(id="fixture", api="fixture"),
            cwd=str(tmp_path),
            runtime=runtime,
            extensions=records,
            session=session,
            stream_fn=stream,
            project_trusted=project_trusted,
            notice_inbox=jobs is not None,
        )
    )
    if jobs is not None:
        await jobs.service.bind(harness, reason="startup")
    return SimpleNamespace(
        controller=controller,
        harness=harness,
        session=session,
        channel=channel,
        ui=ui,
        runtime=runtime,
    )


async def test_real_hook_path_executes_workflow_without_widening_child_permissions(tmp_path):
    b = await bench(tmp_path)
    await b.harness.prompt("Review using a workflow")
    assert len(b.channel.plans) == 1
    child = b.channel.plans[0]
    assert child.permission_mode == PermissionMode.PLAN
    narrowed = narrow_tools(child.resolved.profile, child.parent_tools)
    assert "workflow_run" not in narrowed.profile.tools
    assert "write" not in narrowed.profile.tools
    run = b.controller.runs[b.controller.selected]
    assert run.tasks["inspect"].state == "succeeded"
    assert run.tasks["inspect"].attempts[0].child_id == child.id
    assert b.controller.remaining == 15
    await b.harness.dispose()


async def test_guard_refusal_starts_no_children(tmp_path):
    b = await bench(tmp_path, deny_run=True)
    await b.harness.prompt("Review using a workflow")
    assert not b.channel.plans
    run = b.controller.runs[b.controller.selected]
    assert run.tasks["inspect"].state == "pending"
    assert run.attempt_count == 0
    await b.harness.dispose()


async def test_live_panel_receives_child_tool_progress_and_cleanup_clears_it(tmp_path):
    ui = RecordingUI()
    b = await bench(tmp_path, ui=ui, permission=PermissionMode.PLAN)
    await b.harness.prompt("Review using a workflow")
    renders = [lines for key, lines in ui.widgets if key == WIDGET_KEY and lines]
    assert any("running" in "\n".join(lines) for lines in renders)
    assert any("running: read" in "\n".join(lines) for lines in renders)
    assert any("awaiting_review" in "\n".join(lines) for lines in renders)
    await b.controller.shutdown(None, None)
    assert ui.widgets[-1] == (WIDGET_KEY, None)
    await b.harness.dispose()


async def test_consent_decline_does_not_spawn_or_count_as_success(tmp_path):
    ui = RecordingUI(answer=None)
    b = await bench(tmp_path, ui=ui, approval="auto")
    await b.harness.prompt("Review using a workflow")
    assert ui.dialogs
    assert not b.channel.plans
    run = b.controller.runs[b.controller.selected]
    assert run.tasks["inspect"].state == "failed"
    assert run.tasks["inspect"].attempts[-1].status == "declined"
    await b.harness.dispose()


async def test_session_snapshot_survives_actual_file_reload(tmp_path):
    b = await bench(tmp_path)
    await b.harness.prompt("Review using a workflow")
    await b.harness.flush_pending_session_writes()
    saved_id = b.controller.selected
    path = b.session.session_file
    await b.harness.dispose()
    reopened = Session(await JsonlSessionStorage.open(LocalFileSystem(), path))
    entries = await reopened.get_entries()
    assert any(getattr(e, "custom_type", None) == SNAPSHOT_TYPE for e in entries)
    runtime = _ExtensionRuntime()
    controller = WorkflowExtension(ExtensionAPI(Extension(name="restored"), runtime))
    await controller.session_start(
        None, SimpleNamespace(session_manager=SimpleNamespace(get_session=lambda: reopened))
    )
    assert controller.selected == saved_id
    assert controller.runs[saved_id].tasks["inspect"].state == "succeeded"
    assert controller.runs[saved_id].tasks["inspect"].output == "checked route.py:12"
    assert controller.runs[saved_id].state == "awaiting_review"
    # Rewind to the snapshot before delegation: other-branch completion must not leak in.
    snapshot = next(e for e in entries if getattr(e, "custom_type", None) == SNAPSHOT_TYPE)
    await reopened.get_storage().set_leaf_id(snapshot.id)
    await controller.session_start(
        None, SimpleNamespace(session_manager=SimpleNamespace(get_session=lambda: reopened))
    )
    assert controller.runs[saved_id].tasks["inspect"].state == "pending"


async def test_invalid_latest_snapshot_does_not_resurrect_older_success(tmp_path):
    b = await bench(tmp_path)
    await b.harness.prompt("Review using a workflow")
    await b.harness.flush_pending_session_writes()
    run = b.controller.runs[b.controller.selected]
    corrupt = run.to_dict()
    corrupt["tasks"][0]["state"] = "invented-state"
    await b.session.append_custom_entry(SNAPSHOT_TYPE, corrupt)
    await b.controller.session_start(
        None, SimpleNamespace(session_manager=SimpleNamespace(get_session=lambda: b.session))
    )
    assert not b.controller.runs
    assert "Could not restore" in await b.controller.command("status", None)
    await b.harness.dispose()


async def test_missing_identity_preflight_and_missing_run_id_start_no_children(tmp_path):
    b = await bench(tmp_path)
    context = ToolExecutionContext()
    result = await b.controller.plan(
        {"action": "create", "goal": "Review", "tasks": [{**spec(), "profile": "missing"}]}, context
    )
    # An unknown identity is refused while the plan is written.
    assert result.is_error
    assert "missing" in result.content[0].text
    assert not b.controller.runs
    await b.controller.plan({"action": "create", "goal": "Review", "tasks": [spec()]}, context)
    # A profile deleted between planning and execution is still refused before any spawn.
    (tmp_path / "agent" / "agents" / "reviewer.md").unlink()
    result = await b.controller.run({"run_id": b.controller.selected}, context)
    assert result.is_error
    assert not b.channel.plans
    assert b.controller.runs[b.controller.selected].attempt_count == 0
    result = await b.controller.plan({"action": "finish", "summary": "done"}, context)
    assert result.is_error
    assert "run_id" in result.content[0].text
    await b.harness.dispose()


def test_workflow_is_not_registered_inside_a_delegated_child(monkeypatch):
    monkeypatch.setenv("AELIX_SUBAGENT_DEPTH", "1")
    record = Extension(name="nested")
    setup(ExtensionAPI(record, _ExtensionRuntime()))
    assert not record.tools
    assert not record.commands


async def test_trusted_project_identity_is_still_refused_for_model_workflow(tmp_path):
    b = await bench(tmp_path, project_trusted=True)
    project_profiles = tmp_path / ".aelix" / "agents"
    project_profiles.mkdir(parents=True)
    (project_profiles / "project-reviewer.md").write_text(
        "---\nname: project-reviewer\ndescription: Project-owned identity\n---\nReview.\n"
    )
    resolved = b.runtime.subagents.resolve_profile("project-reviewer", allow_project=True)
    assert resolved.scope == "project"
    context = ToolExecutionContext()
    result = await b.controller.plan(
        {
            "action": "create",
            "goal": "Review",
            "tasks": [{**spec(), "profile": "project-reviewer"}],
        },
        context,
    )
    assert result.is_error
    assert not b.controller.runs
    assert not b.channel.plans
    await b.harness.dispose()


async def test_disabled_agent_tool_cannot_be_reintroduced_by_workflow(tmp_path):
    b = await bench(tmp_path)
    b.controller.api.set_active_tools(["workflow_plan", "workflow_run"])
    context = ToolExecutionContext()
    await b.controller.plan({"action": "create", "goal": "Review", "tasks": [spec()]}, context)
    result = await b.controller.run({"run_id": b.controller.selected}, context)
    assert result.is_error
    assert not b.channel.plans
    b.controller.api.set_active_tools([])
    guidance = await b.controller.before_agent_start(SimpleNamespace(system_prompt="base"), None)
    assert guidance.system_prompt is None
    await b.harness.dispose()


async def test_prompt_admission_limit_is_shared_across_workflows(tmp_path):
    b = await bench(tmp_path)
    context = ToolExecutionContext()
    await b.controller.plan(
        {"action": "create", "goal": "Review", "tasks": [spec(f"t{n}") for n in range(16)]}, context
    )
    await b.controller.run({"run_id": b.controller.selected}, context)
    assert len(b.channel.plans) == 16
    assert b.controller.remaining == 0
    await b.controller.plan({"action": "create", "goal": "New review", "tasks": [spec()]}, context)
    result = await b.controller.run({"run_id": b.controller.selected}, context)
    assert result.is_error
    assert len(b.channel.plans) == 16
    assert b.controller.runs[b.controller.selected].attempt_count == 0
    await b.controller.before_agent_start(SimpleNamespace(system_prompt="base"), None)
    await b.controller.run({"run_id": b.controller.selected}, context)
    assert len(b.channel.plans) == 17
    await b.harness.dispose()


async def test_a_delegated_agent_gets_no_time_limit(tmp_path):
    # The host ignores spawn's timeout_ms (its ADR-0276), so the workflow neither offers nor
    # passes one; checks keep theirs.
    b = await bench(tmp_path)
    runtime = b.runtime.subagents
    spawn, calls = runtime.spawn, []

    async def recording(resolved, task, **options):
        calls.append(options)
        return await spawn(resolved, task, **options)

    runtime.spawn = recording
    refused = await b.controller.plan(
        {"action": "create", "goal": "Review", "tasks": [{**spec(), "timeout_ms": 60_000}]},
        CONTEXT,
    )
    assert refused.is_error and "no time limit" in refused.content[0].text
    assert not b.controller.runs
    await b.harness.prompt("Review using a workflow")
    assert len(b.channel.plans) == 1
    assert calls and all("timeout_ms" not in options for options in calls)
    await b.harness.dispose()


def no_delegation(b, agents=()):
    """The session's identity (--agent, /agents use) with this `agents:` list."""
    impl = b.controller.api.runtime.subagents
    impl.host = dataclasses.replace(
        impl.host, active_profile=lambda: SimpleNamespace(name="lead", agents=agents)
    )


async def test_an_identity_that_delegates_to_nobody_refuses_the_plan(tmp_path):
    b = await bench(tmp_path)
    no_delegation(b)
    with pytest.raises(subagent_contract.DelegationNotAllowed):
        b.runtime.subagents.resolve_for_model("reviewer")
    result = await b.controller.plan(
        {"action": "create", "goal": "Review", "tasks": [spec()]}, CONTEXT
    )
    text = result.content[0].text
    assert result.is_error
    # The host's sentence, written for the model, reaches it whole.
    assert "profile 'reviewer' is not available" in text
    assert "'lead', whose agents: list is empty, so it delegates to no profile" in text
    assert not b.controller.runs
    await b.harness.prompt("Review using a workflow")
    assert not b.channel.plans
    await b.harness.dispose()


async def test_a_delegation_policy_changed_after_planning_is_applied_at_run(tmp_path):
    b = await bench(tmp_path)
    no_delegation(b, agents=("reviewer",))
    planned = await b.controller.plan(
        {"action": "create", "goal": "Review", "tasks": [spec()]}, CONTEXT
    )
    assert not planned.is_error
    no_delegation(b, agents=("implementer",))
    result = await b.controller.run({"run_id": b.controller.selected}, CONTEXT)
    assert result.is_error
    assert "Profile 'reviewer' is not available" in result.content[0].text
    assert "is not one this session's profile 'lead' may delegate to" in result.content[0].text
    assert not b.channel.plans
    assert b.controller.runs[b.controller.selected].attempt_count == 0
    await b.harness.dispose()


class OlderRuntime:
    """A delegation runtime with only the SubagentRuntime protocol's resolve_profile."""

    def __init__(self):
        self.calls = []

    def resolve_profile(self, name, *, allow_project=False):
        self.calls.append((name, allow_project))
        return SimpleNamespace(name=name)


def offline_controller(runtime):
    return WorkflowExtension(SimpleNamespace(runtime=SimpleNamespace(subagents=runtime)))


async def test_a_runtime_without_the_model_door_cannot_delegate_for_a_workflow():
    # The host's contract has the model door, so its policy applies; this runtime cannot
    # answer for it, and the contract says the tool then refuses.
    runtime = OlderRuntime()
    controller = offline_controller(runtime)
    controller.mode = "on"
    result = await controller.plan(
        {"action": "create", "goal": "Review", "tasks": [spec()]}, CONTEXT
    )
    assert result.is_error
    assert "cannot tell which agent profiles the model may use" in result.content[0].text
    assert not runtime.calls
    assert not controller.runs


def test_an_older_host_without_the_model_door_keeps_the_project_refusal(monkeypatch):
    # The released beta.2 contract has no ModelDelegation and no delegation policy.
    monkeypatch.delattr(subagent_contract, "ModelDelegation")
    runtime = OlderRuntime()
    resolved = offline_controller(runtime)._resolve("reviewer")
    assert resolved.name == "reviewer"
    assert runtime.calls == [("reviewer", False)]


async def test_with_background_jobs_on_a_workflow_child_holds_a_job_slot(tmp_path, jobs):
    b = await bench(tmp_path, jobs=jobs)
    # The delegation kind is registered, so every spawn reserves from the job service.
    assert b.runtime.subagents.host.reserve is not None
    record, held = b.channel.run, []

    async def channel(plan, *, child=None, on_stream=None):
        held.append(jobs.service.admission.count("agent"))
        return await record(plan, child=child, on_stream=on_stream)

    b.channel.run = channel
    await b.harness.prompt("Review using a workflow")
    assert held == [1]
    assert jobs.service.admission.count("agent") == 0
    run = b.controller.runs[b.controller.selected]
    assert run.tasks["inspect"].state == "succeeded"
    assert run.tasks["inspect"].attempts[0].child_id == b.channel.plans[0].id
    await b.harness.dispose()


def recording_spawns(b):
    """Every envelope the host's spawn returns, refusals included."""
    runtime, results = b.runtime.subagents, []
    spawn = runtime.spawn

    async def recording(resolved, task, **options):
        results.append(await spawn(resolved, task, **options))
        return results[-1]

    runtime.spawn = recording
    return results


FULL = (
    "Delegation refused: 1 delegated agent already runs; this limit is set in settings "
    "backgroundJobs.limits.agents. Stop one or wait; do not retry."
)


async def test_with_background_jobs_on_a_slot_held_outside_the_run_is_waited_out(tmp_path, jobs):
    jobs.settings["limits"] = {"agents": 1}
    b = await bench(tmp_path, jobs=jobs)
    b.controller.engine.retry_delays = (0.05, 0.05)
    results = recording_spawns(b)
    # Another delegation holds the only slot, as an agent call reserves before its dialog.
    other = jobs.service.reserve("agent", 1)
    await b.controller.plan({"action": "create", "goal": "Review", "tasks": [spec()]}, CONTEXT)
    task = b.controller.runs[b.controller.selected].tasks["inspect"]

    async def free_after_the_refusal():
        while not results:
            await asyncio.sleep(0.005)
        other.release()

    freeing = asyncio.create_task(free_after_the_refusal())
    result = await b.controller.run({"run_id": b.controller.selected}, CONTEXT)
    await freeing
    refused = results[0]
    # The host's real sentence, and the same attempt tried again once the slot is free.
    assert refused.error == FULL and refused.started is False
    assert failures.capacity(refused) and failures.classify(refused) == "transient"
    assert [(a.status, a.trigger) for a in task.attempts] == [("ok", "run")]
    assert not result.is_error and task.state == "succeeded"
    assert len(b.channel.plans) == 1
    assert b.controller.remaining == 15
    assert jobs.service.admission.count("agent") == 0
    await b.harness.dispose()


async def test_with_background_jobs_on_a_task_waits_for_its_sibling_to_free_the_slot(
    tmp_path, jobs
):
    jobs.settings["limits"] = {"agents": 1}
    b = await bench(tmp_path, jobs=jobs)
    results = recording_spawns(b)
    await b.controller.plan(
        {"action": "create", "goal": "Review", "tasks": [spec("a"), spec("b")]}, CONTEXT
    )
    run = b.controller.runs[b.controller.selected]
    record, rows = b.channel.run, []

    async def channel(plan, *, child=None, on_stream=None):
        # The first child holds the only slot until the other task waits for it.
        while len(b.channel.plans) == 0 and not any(
            "free agent slot" in t.note for t in run.tasks.values()
        ):
            await asyncio.sleep(0.005)
        rows.extend(task_line(run, t) for t in run.tasks.values())
        return await record(plan, child=child, on_stream=on_stream)

    b.channel.run = channel
    async with asyncio.timeout(10):
        result = await b.controller.run({"run_id": run.id}, CONTEXT)
    assert not result.is_error
    assert [len(t.attempts) for t in run.tasks.values()] == [1, 1]
    assert all(t.state == "succeeded" for t in run.tasks.values())
    assert sorted(r.error or "" for r in results) == ["", "", FULL]
    assert any("Waiting for a free agent slot" in row for row in rows)
    # Two children, two admissions: the wait cost neither an attempt nor budget.
    assert len(b.channel.plans) == 2 and b.controller.remaining == 14
    assert jobs.service.admission.count("agent") == 0
    await b.harness.dispose()


async def test_an_inheriting_profile_reports_no_withheld_tools(tmp_path):
    # Live run: the host lists every extension tool of an inheriting profile as withheld.
    b = await bench(tmp_path, tools=None)
    await b.harness.prompt("Review using a workflow")
    assert narrow_tools(
        b.channel.plans[0].resolved.profile, b.channel.plans[0].parent_tools
    ).dropped
    run = b.controller.runs[b.controller.selected]
    assert run.tasks["inspect"].attempts[0].dropped_tools == []
    await b.harness.dispose()


async def test_a_profile_learns_which_requested_tools_its_child_lacked(tmp_path):
    b = await bench(tmp_path, tools="[read, web_fetch]")
    await b.harness.prompt("Review using a workflow")
    child = b.channel.plans[0]
    expected = list(narrow_tools(child.resolved.profile, child.parent_tools).dropped)
    run = b.controller.runs[b.controller.selected]
    # A profile that names its tools asked for each one, so the host's list is kept whole.
    assert run.tasks["inspect"].attempts[0].dropped_tools == expected
    assert "web_fetch" in expected
    await b.harness.dispose()


async def test_a_change_in_what_a_running_child_waits_on_is_not_throttled_away(tmp_path):
    ui = RecordingUI()
    b = await bench(tmp_path, ui=ui)
    await b.controller.plan({"action": "create", "goal": "Review", "tasks": [spec()]}, CONTEXT)
    task = b.controller.runs[b.controller.selected].tasks["inspect"]
    task.state, task.current_tool = "running", "edit"
    b.controller._render()
    task.approval = "awaiting approval: edit"
    b.controller._render(force=False)  # A progress tick: skipped only if nothing shown changed.
    assert "awaiting approval: edit" in "\n".join(ui.widgets[-1][1])
    await b.harness.dispose()


def wrapped_up(plan, child, answer):
    """The envelope and row a consumed wrap-up leaves, through the host's own envelope code."""
    from aelix_agents.command_wire import new_wrap_up
    from aelix_agents.envelope import build_result

    # As runtime.wrap_up records the request, and as the child's stream reads it and ends.
    child.wrap_up = new_wrap_up("user")
    child.stream.wrap_up_head, child.stream.wrap_up_read = child.wrap_up.head, True
    child.stream.summary, child.stream.saw_agent_end = answer, True
    child.state = "done"
    return build_result(
        id=plan.id,
        profile=plan.resolved.name,
        state=child.stream,
        wrap_up=child.wrap_up,
        wrap_up_consumed=True,
    )


async def test_a_child_wrapped_up_by_the_user_fails_its_task_and_the_run_goes_on(tmp_path):
    b = await bench(tmp_path)
    record = b.channel.run

    async def channel(plan, *, child=None, on_stream=None):
        if "task=a " not in plan.task:
            return await record(plan, child=child, on_stream=on_stream)
        b.channel.plans.append(plan)
        return wrapped_up(plan, child, "Partial: route.py checked, session.py not reached.")

    b.channel.run = channel
    await b.controller.plan(
        {
            "action": "create",
            "goal": "Review",
            "tasks": [spec("a"), spec("b")],
            "max_concurrency": 1,
        },
        CONTEXT,
    )
    result = await b.controller.run({"run_id": b.controller.selected}, CONTEXT)
    run = b.controller.runs[b.controller.selected]
    child = run.tasks["a"].attempts[0].child_id
    # The host's envelope says aborted; its row says how: the wrap-up, not a stop.
    row = next(r for r in b.runtime.subagents.controls.list() if r["id"] == child)
    assert (row["end"], run.tasks["a"].attempts[0].status) == ("wrapped_up", "aborted")
    assert run.tasks["a"].state == "failed"
    assert run.tasks["a"].attempts[0].failure == "wrapped_up"
    assert run.tasks["a"].output == ""  # A partial answer is no dependency result.
    assert "session.py not reached" in run.tasks["a"].attempts[0].summary
    assert run.tasks["b"].state == "succeeded"
    assert "Halted" not in result.content[0].text
    await b.harness.dispose()


class StandInControls:
    def __init__(self, reply):
        self.reply, self.asked = reply, []

    def list(self):
        return []

    async def wrap_up(self, child_id):
        self.asked.append(child_id)
        return self.reply


async def running_workflow(b, tasks):
    """A workflow whose children hold until released; the run task and the release."""
    release = asyncio.Event()
    record = b.channel.run

    async def channel(plan, *, child=None, on_stream=None):
        if on_stream is not None:
            on_stream(child.stream)
        await release.wait()
        return await record(plan, child=child, on_stream=on_stream)

    b.channel.run = channel
    await b.controller.plan(
        {"action": "create", "goal": "Review", "tasks": tasks, "max_concurrency": 2}, CONTEXT
    )
    run = b.controller.runs[b.controller.selected]
    runner = asyncio.create_task(b.controller.run({"run_id": run.id}, CONTEXT))
    async with asyncio.timeout(5):
        while not all(t.latest and t.latest.child_id for t in run.tasks.values()):
            await asyncio.sleep(0.005)
    return run, runner, release


async def test_workflow_wrap_asks_the_running_children_through_the_hosts_controls(tmp_path):
    b = await bench(tmp_path)
    run, runner, release = await running_workflow(b, [spec("a"), spec("b")])
    # The host's own door: this fake transport's child has no process to write to.
    text = await b.controller.command("wrap a", None)
    assert text == "a: not reached (the agent has not started yet; retry)"
    controls = StandInControls({"outcome": "queued", "phase": "streaming"})
    b.runtime.subagents.controls = controls
    text = await b.controller.command("wrap", None)
    assert controls.asked == [run.tasks["a"].latest.child_id, run.tasks["b"].latest.child_id]
    assert text.splitlines() == [
        "a: asked to wrap up; its next reply ends it",
        "b: asked to wrap up; its next reply ends it",
    ]
    controls.reply = {
        "outcome": "refused",
        "reason": 'the agent has finished or was stopped; to go on, use action="resume"',
    }
    text = await b.controller.command("wrap b", None)
    # A workflow task has no resume door, so the host's advice to resume is not passed on.
    assert text == "b: not reached (the agent has finished or was stopped)"
    release.set()
    await runner
    assert await b.controller.command("wrap", None) == (
        "No workflow is executing, so no agent is running to wrap up."
    )
    await b.harness.dispose()


async def test_workflow_wrap_targets_the_executing_run_and_never_becomes_a_goal(tmp_path):
    b = await bench(tmp_path)
    sent = []
    b.controller._send = lambda text, ctx: sent.append(text)
    await b.controller.plan({"action": "create", "goal": "Other", "tasks": [spec("x")]}, CONTEXT)
    other = b.controller.selected
    run, runner, release = await running_workflow(b, [spec("a"), spec("b")])
    controls = StandInControls({"outcome": "queued"})
    b.runtime.subagents.controls = controls
    await b.controller.command(f"status {other}", None)
    assert b.controller.selected == other
    # The executing run, whatever /workflow status selected since.
    assert (await b.controller.command("wrap", None)).splitlines()[0].startswith("a: asked")
    assert len(controls.asked) == 2
    for word in ("x", "missing", "docs"):
        assert await b.controller.command(f"wrap {word}", None) == (
            f"Workflow {run.id} has no running agent task {word}. Running agent tasks: a, b."
        )
    assert not sent
    release.set()
    await runner
    await b.harness.dispose()


async def test_workflow_wrap_followed_by_words_is_still_a_goal(tmp_path):
    b = await bench(tmp_path)
    sent = []
    b.controller._send = lambda text, ctx: sent.append(text)
    await b.controller.command("wrap up the release notes", None)
    assert sent and sent[0].startswith("Workflow: wrap up the release notes")
    # With nothing executing, one word after wrap is a goal too.
    await b.controller.command("wrap docs", None)
    assert sent[1].startswith("Workflow: wrap docs")
    await b.harness.dispose()


@pytest.mark.parametrize("action", ["create", "add", "revise"])
async def test_a_plan_change_without_tasks_says_so_and_shows_one(tmp_path, action):
    # A weak live model sent create with no tasks 16 times, each told only the bounds.
    b = await bench(tmp_path)
    await b.controller.plan({"action": "create", "goal": "Review", "tasks": [spec()]}, CONTEXT)
    args = {"action": action, "goal": "Review"}
    if action != "create":
        args = {"action": action, "run_id": b.controller.selected, "reason": "more"}
    result = await b.controller.plan(args, CONTEXT)
    text = result.content[0].text
    assert result.is_error
    assert text.startswith(f"workflow_plan(action='{action}') needs tasks, an array of task")
    example = json.loads(text.split("A minimal one: tasks=", 1)[1])
    assert [sorted(t) for t in example] == [["id", "profile", "prompt", "title"]]
    await b.harness.dispose()


async def test_workflow_pause_stops_a_run_mid_turn_and_the_agent_is_told_to_wait(tmp_path):
    b = await bench(tmp_path)
    sent = []
    b.controller._send = lambda text, ctx: sent.append(text)
    assert await b.controller.command("pause", None) == (
        "No workflow is executing, so there is nothing to pause."
    )
    run, runner, _release = await running_workflow(b, [spec("a"), spec("b")])
    text = await b.controller.command("pause", None)
    assert text.startswith(f"Pausing workflow {run.id}: ")
    async with asyncio.timeout(5):
        result = await runner  # The tool call returns; the turn's own task is not cancelled.
    body = result.content[0].text
    assert not result.is_error
    assert "The user paused this workflow with /workflow pause; 2 running task(s) were " in body
    assert "interrupted (a, b)" in body and "wait for the user" in body
    assert [t.state for t in run.tasks.values()] == ["interrupted", "interrupted"]
    # Until the user's next prompt the agent runs no workflow at all: not this one, and not
    # the rest rebuilt as a new run. Planning stays allowed.
    refused = await b.controller.run({"run_id": run.id}, CONTEXT)
    assert refused.is_error
    assert refused.content[0].text.startswith(f"The user paused workflow {run.id}")
    planned = await b.controller.plan(
        {"action": "create", "goal": "Rest", "tasks": [spec("x")]}, CONTEXT
    )
    assert not planned.is_error
    b.channel.run = RecordingChannel().run
    rebuilt = await b.controller.run({"run_id": b.controller.selected}, CONTEXT)
    assert rebuilt.is_error and "Do not run any workflow now" in rebuilt.content[0].text
    # A background job's result wakes the model: a notice turn, not the user's prompt.
    b.controller.remaining = 3
    notice = SimpleNamespace(system_prompt="base", prompt="", source="notice")
    await b.controller.before_agent_start(notice, None)
    assert b.controller.remaining == 3
    assert (await b.controller.run({"run_id": run.id}, CONTEXT)).is_error
    await b.controller.before_agent_start(SimpleNamespace(system_prompt="base"), None)
    assert b.controller.remaining == 16
    again = await b.controller.run({"run_id": run.id}, CONTEXT)
    assert "The user paused" not in again.content[0].text
    # With more words it is still a goal.
    await b.controller.command("pause the deploy rollout", None)
    assert sent and sent[0].startswith("Workflow: pause the deploy rollout")
    await b.harness.dispose()


async def test_workflow_continue_lifts_a_pause_typed_mid_turn(tmp_path):
    b = await bench(tmp_path)
    b.controller._send = lambda text, ctx: None
    run, runner, _release = await running_workflow(b, [spec("a")])
    await b.controller.command("pause", None)
    await runner
    assert (await b.controller.run({"run_id": run.id}, CONTEXT)).is_error
    # Typed mid-turn it is a follow-up, not a prompt, so the command itself lifts the pause.
    await b.controller.command(f"continue {run.id}", None)
    b.channel.run = RecordingChannel().run
    assert (
        "The user paused"
        not in (await b.controller.run({"run_id": run.id}, CONTEXT)).content[0].text
    )
    await b.harness.dispose()


class AnsweringControls:
    """The host's controls, except that the wrap-up door answers queued without a process."""

    def __init__(self, real):
        self.real = real

    def list(self):
        return self.real.list()

    async def wrap_up(self, child_id):
        return {"outcome": "queued"}


async def test_a_wrap_up_the_user_asked_for_says_so_and_not_to_retry(tmp_path):
    # Live: the model doubted who asked and retried at once.
    b = await bench(tmp_path)
    release = asyncio.Event()
    record = b.channel.run

    async def channel(plan, *, child=None, on_stream=None):
        if on_stream is not None:
            on_stream(child.stream)
        await release.wait()
        if "task=a " not in plan.task:
            return await record(plan, child=child, on_stream=on_stream)
        b.channel.plans.append(plan)
        return wrapped_up(plan, child, "Partial: route.py checked.")

    b.channel.run = channel
    await b.controller.plan(
        {"action": "create", "goal": "Review", "tasks": [spec("a"), spec("b")]}, CONTEXT
    )
    run = b.controller.runs[b.controller.selected]
    runner = asyncio.create_task(b.controller.run({"run_id": run.id}, CONTEXT))
    async with asyncio.timeout(5):
        while not all(t.latest and t.latest.child_id for t in run.tasks.values()):
            await asyncio.sleep(0.005)
    b.runtime.subagents.controls = AnsweringControls(b.runtime.subagents.controls)
    assert (await b.controller.command("wrap a", None)).startswith("a: asked to wrap up")
    release.set()
    result = await runner
    a = run.tasks["a"]
    assert a.attempts[0].failure == "wrapped_up"
    assert a.note == (
        "wrapped_up: the user asked this task to finish early with /workflow wrap; its reply is "
        "partial. Do not retry it unless the user asks: report the partial result."
    )
    assert "the user asked this task to finish early with /workflow wrap" in result.content[0].text
    await b.harness.dispose()


def test_a_refund_never_raises_the_budget_above_its_limit(tmp_path):
    controller = WorkflowExtension(SimpleNamespace(runtime=SimpleNamespace(subagents=None)))
    controller.remaining = 15
    controller._refund()
    controller._refund()
    assert controller.remaining == 16
