"""Proactive/explicit policy at the real hook, handler, session and TUI boundaries."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from aelix_ai.tools import ToolExecutionContext
from aelix_coding_agent.extensions.api import Extension, ExtensionAPI, _ExtensionRuntime
from test_host import RecordingUI, bench
from test_model import spec

from aelix_workflow.activation import MODE_ENTRY_TYPE, requested_workflow
from aelix_workflow.extension import WorkflowExtension
from aelix_workflow.panel import STATUS_KEY, WIDGET_KEY


@pytest.mark.parametrize(
    "text",
    [
        "/workflow inspect the repository",
        "workflow: review and verify the code",
        "Use a workflow to inspect the README",
        "Please run a dynamic workflow for this task",
        "Review this repository using an adaptive workflow",
        "워크플로로 분석하고 검증해주세요",
        "이번 작업은 workflow를 적극적으로 사용해주세요",
        "워크플로우를 활용해서 진행해주세요",
    ],
)
def test_explicit_user_request_cues(text):
    assert requested_workflow(text)


@pytest.mark.parametrize(
    "text",
    [
        "Inspect README.md and report the tool names",
        "What is a workflow?",
        "Explain the workflow module",
        "How do I use a workflow?",
        "워크플로 사용법을 설명해주세요",
        "워크플로가 어떻게 동작하나요?",
        "aelix-workflow/README.md를 읽고 설명해주세요",
        "Do not use a workflow for this request",
        "Don't run a dynamic workflow",
        "워크플로를 사용하지 말고 직접 처리해주세요",
        "워크플로 없이 진행해주세요",
    ],
)
def test_plain_questions_and_refusals_do_not_opt_in(text):
    assert not requested_workflow(text)


def test_default_is_explicit_mode_off():
    controller = WorkflowExtension(ExtensionAPI(Extension(name="default"), _ExtensionRuntime()))
    assert controller.mode == "off"


async def test_off_blocks_forced_model_tool_calls_without_creating_any_work(tmp_path):
    b = await bench(tmp_path, mode="off")
    # The fixture model attempts plan/run regardless of the prompt; the hooks must enforce off.
    await b.harness.prompt("Inspect README.md and report the tool names")
    assert not b.controller.runs
    assert not b.channel.plans
    assert b.controller.remaining == 16
    await b.harness.dispose()


async def test_off_allows_a_current_explicit_request_but_does_not_enable_mode(tmp_path):
    b = await bench(tmp_path, mode="off")
    await b.harness.prompt("워크플로로 검토해주세요")
    assert len(b.channel.plans) == 1
    assert b.controller.mode == "off"
    assert b.controller.runs[b.controller.selected].state == "awaiting_review"
    # The earlier opt-in and graph goal cannot authorize a subsequent plain request.
    await b.harness.prompt("Now explain the README")
    result = await b.controller.plan(
        {"action": "create", "goal": "Use a workflow", "tasks": [spec()]}, ToolExecutionContext()
    )
    assert result.is_error
    assert len(b.channel.plans) == 1
    await b.harness.dispose()


async def test_off_command_triggers_the_real_host_and_opts_in_once(tmp_path):
    b = await bench(tmp_path, mode="off")
    await b.controller.command("Inspect this README", None)
    await asyncio.sleep(0)
    await asyncio.wait_for(b.harness.wait_for_idle(), 2)
    assert len(b.channel.plans) == 1
    assert b.controller.mode == "off"
    assert not b.controller._allowed()
    await b.harness.dispose()


async def test_on_allows_model_choice_for_an_ordinary_request(tmp_path):
    b = await bench(tmp_path, mode="on")
    await b.harness.prompt("Compare two independent documents and verify their consistency")
    assert len(b.channel.plans) == 1
    assert b.controller.runs[b.controller.selected].state == "awaiting_review"
    await b.harness.dispose()


async def test_task_arguments_cannot_self_authorize_or_change_mode(tmp_path):
    b = await bench(tmp_path, mode="off")
    context = ToolExecutionContext()
    result = await b.controller.plan(
        {"action": "create", "goal": "Use a workflow", "tasks": [spec()], "mode": "on"}, context
    )
    assert result.is_error
    result = await b.controller.run({"run_id": "invented"}, context)
    assert result.is_error
    assert b.controller.mode == "off"
    assert not b.controller.runs
    assert not b.channel.plans
    status = await b.controller.status({}, context)
    assert not status.is_error
    assert status.details == {"mode": "off"}
    await b.harness.dispose()


async def test_mode_switch_changes_guidance_and_footer_without_starting_a_turn(tmp_path):
    ui = RecordingUI()
    b = await bench(tmp_path, mode="off", ui=ui)
    before = await b.controller.before_agent_start(
        SimpleNamespace(system_prompt="base", prompt="Inspect a file"), None
    )
    assert "Do not call workflow_plan or workflow_run" in before.system_prompt
    await b.controller.settled(None, None)
    await b.controller.command("mode on", None)
    assert b.controller.mode == "on"
    assert ui.status[-1] == (STATUS_KEY, "Workflow mode: on")
    assert ui.widgets[-1] == (WIDGET_KEY, None)
    after = await b.controller.before_agent_start(
        SimpleNamespace(system_prompt="base", prompt="Inspect a file"), None
    )
    assert "Actively assess" in after.system_prompt
    assert "simple one-step" in after.system_prompt
    assert not b.channel.plans
    assert not b.controller.runs
    await b.controller.settled(None, None)
    await b.controller.command("mode off", None)
    assert ui.status[-1] == (STATUS_KEY, "Workflow mode: off")
    assert "Usage:" in await b.controller.command("mode maybe", None)
    assert b.controller.mode == "off"
    await b.harness.dispose()


async def test_mode_toggle_applies_next_request_and_does_not_cancel_current_work(tmp_path):
    b = await bench(tmp_path, mode="on")
    scope = SimpleNamespace(idle=False)
    ctx = SimpleNamespace(is_idle=lambda: scope.idle)
    await b.controller.before_agent_start(
        SimpleNamespace(system_prompt="base", prompt="Compare documents"), ctx
    )
    await b.controller.command("mode off", ctx)
    assert b.controller._allowed()
    assert "next request" in b.controller._mode_status()
    scope.idle = True
    await b.controller.before_agent_start(
        SimpleNamespace(system_prompt="base", prompt="Compare documents"), None
    )
    assert not b.controller._allowed()
    await b.harness.dispose()


async def test_settled_before_retry_does_not_drop_an_explicit_request(tmp_path):
    b = await bench(tmp_path, mode="off")
    scope = SimpleNamespace(idle=False)
    ctx = SimpleNamespace(is_idle=lambda: scope.idle)
    await b.controller.before_agent_start(
        SimpleNamespace(system_prompt="base", prompt="Use a workflow"), ctx
    )
    await b.controller.settled(None, ctx)
    assert b.controller._allowed()
    scope.idle = True
    assert not b.controller._allowed()
    await b.harness.dispose()


async def test_mode_is_persisted_in_the_actual_session_branch_and_restored(tmp_path):
    b = await bench(tmp_path, mode="off")
    await b.controller.command("mode on", None)

    # append_entry is a host-pinned asynchronous append, not a synchronous write.
    async def written(expected):
        async with asyncio.timeout(2):
            while True:
                entries = [
                    e
                    for e in await b.session.get_entries()
                    if getattr(e, "custom_type", None) == MODE_ENTRY_TYPE
                ]
                if len(entries) >= expected:
                    return entries
                await asyncio.sleep(0)

    on_entry = (await written(1))[0]
    await b.controller.command("mode off", None)
    await written(2)
    from aelix_agent_core.session import JsonlSessionStorage, LocalFileSystem, Session

    reopened = Session(await JsonlSessionStorage.open(LocalFileSystem(), b.session.session_file))
    restored = WorkflowExtension(ExtensionAPI(Extension(name="restored"), _ExtensionRuntime()))
    ctx = SimpleNamespace(session_manager=SimpleNamespace(get_session=lambda: reopened))
    await restored.session_start(None, ctx)
    assert restored.mode == "off"
    await reopened.get_storage().set_leaf_id(on_entry.id)
    await restored.session_start(None, ctx)
    assert restored.mode == "on"
    await restored.session_start(
        None, SimpleNamespace(session_manager=SimpleNamespace(get_session=lambda: None))
    )
    assert restored.mode == "off"
    await b.harness.dispose()


async def test_malformed_mode_record_does_not_restore_an_older_on_setting(tmp_path):
    b = await bench(tmp_path, mode="off")
    await b.controller.command("mode on", None)
    await b.harness.flush_pending_session_writes()
    await b.session.append_custom_entry(MODE_ENTRY_TYPE, {"schema_version": 1, "mode": []})
    ctx = SimpleNamespace(session_manager=SimpleNamespace(get_session=lambda: b.session))
    await b.controller.session_start(None, ctx)
    assert b.controller.mode == "off"
    assert "using off" in b.controller._restore_warning
    await b.harness.dispose()


async def test_late_ui_binding_displays_default_mode_before_any_request(tmp_path):
    b = await bench(tmp_path, mode="off")
    ui = RecordingUI()
    b.runtime.bind_ui(ui)
    b.controller.api.events.emit("ui:list-modules", object())
    assert ui.status[-1] == (STATUS_KEY, "Workflow mode: off")
    assert not b.controller.runs
    assert not b.channel.plans
    await b.harness.dispose()
    writes = len(ui.status)
    b.controller.api.events.emit("ui:list-modules", object())
    assert len(ui.status) == writes


async def test_requests_that_cannot_use_workflows_skip_the_usage_guide(tmp_path):
    b = await bench(tmp_path, mode="off")
    plain = await b.controller.before_agent_start(
        SimpleNamespace(system_prompt="base", prompt="Inspect a file"), None
    )
    explicit = await b.controller.before_agent_start(
        SimpleNamespace(system_prompt="base", prompt="Use a workflow to inspect a file"), None
    )
    assert "for_each" not in plain.system_prompt
    assert "for_each" in explicit.system_prompt
    assert len(plain.system_prompt) < 1_000
    await b.harness.dispose()
