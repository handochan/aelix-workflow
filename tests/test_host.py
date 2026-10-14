"""Real harness, hook bus, delegation runtime and JSONL session; only child transport is fake."""

from __future__ import annotations

import copy
from types import SimpleNamespace

from aelix_agent_core.harness.core import AgentHarness, AgentHarnessOptions
from aelix_agent_core.harness.hooks import ToolCallResult
from aelix_agent_core.session import JsonlSessionStorage, LocalFileSystem, Session
from aelix_agents import AgentsExtension
from aelix_agents.print_channel import narrow_tools
from aelix_ai.messages import AssistantMessage, TextContent, ToolCallContent
from aelix_ai.streaming import AssistantEndEvent, AssistantStartEvent, Model
from aelix_ai.tools import ToolExecutionContext
from aelix_coding_agent.builtin.permission_mode import PermissionMode, PermissionPosture
from aelix_coding_agent.extensions.api import Extension, ExtensionAPI, _ExtensionRuntime
from aelix_coding_agent.subagent_contract import SubagentResult
from test_model import spec

from aelix_workflow.extension import SNAPSHOT_TYPE, WorkflowExtension, setup
from aelix_workflow.panel import WIDGET_KEY


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
        )
    )
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


async def test_an_unset_timeout_reaches_the_host_as_its_own_default(tmp_path):
    # Live run: the workflow's former 120 s default overrode the profile's (Aelix: 600 s).
    b = await bench(tmp_path)
    await b.harness.prompt("Review using a workflow")
    assert len(b.channel.plans) == 1
    assert b.channel.plans[0].timeout_ms is None
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
