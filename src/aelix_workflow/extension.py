"""Public Aelix integration: tools, slash command, session records, and live TUI."""

from __future__ import annotations

import asyncio
import logging
import shutil
import time
from collections.abc import Callable
from typing import Any

from aelix_agent_core.harness.hooks import BeforeAgentStartResult, ToolCallResult
from aelix_agent_core.types import AgentTool
from aelix_ai.messages import TextContent, UserMessage
from aelix_ai.tools import ToolExecutionContext, ToolResult
from aelix_coding_agent.extensions.api import ExtensionAPI, ExtensionContext, ExtensionError
from aelix_coding_agent.extensions.headless_ui import HEADLESS_UI_CONTEXT
from aelix_coding_agent.subagent_contract import (
    MAX_SUBAGENT_DEPTH,
    SubagentProgress,
    SubagentResult,
    subagent_depth,
)

from aelix_workflow.activation import (
    DEFAULT_MODE,
    MODE_ENTRY_TYPE,
    WorkflowMode,
    activation_guidance,
    requested_workflow,
)
from aelix_workflow.engine import WorkflowEngine
from aelix_workflow.model import Task, Workflow, WorkflowError
from aelix_workflow.panel import STATUS_KEY, WIDGET_KEY, render_details, render_panel, status_line
from aelix_workflow.schema import PLAN_SCHEMA, RUN_SCHEMA, STATUS_SCHEMA

SNAPSHOT_TYPE = "aelix-workflow.snapshot"
PROMPT_DELEGATION_LIMIT = 16
_LOG = logging.getLogger(__name__)
_GUIDANCE = """
Dynamic workflows are available through workflow_plan, workflow_run and workflow_status.
When workflow activation permits it, decompose the goal into verifiable, self-contained tasks
using existing agent profiles from the agent tool's roster. Create a dependency graph with
workflow_plan(action='create'). Execute it with workflow_run(run_id=...). Independent tasks
run in parallel, subject to the graph's max_concurrency and the host's permission checks.
Read the actual results before deciding what to do next. Use add or revise to adapt the graph,
and retry for a failed/interrupted task only after examining its failure and possible side effects.
A declined delegation is the user's refusal: stop that branch and do not retry it.
Authentication failures, credit/usage limits, unavailable models and unavailable delegation
require a change outside the workflow. Do not retry unchanged calls or create another workflow
to work around the same blocker. Use ordinary available tools when they can do the work and
report the delegated checks as unverified.
Revising a task invalidates dependent results. Include a verification task when the goal needs it.
A task marked succeeded means its agent returned successfully, not that every acceptance check
passed. Inspect failed checks reported in its summary; add corrective work when needed.
Call finish with an evidence-based summary only after the goal is achieved. Do not silently skip
failed work. No workflow or child may increase the parent's tool grant or approve an action for
the user. Workflows require the host's --agents feature and an active agent tool. Execution is
foreground in this version; the TUI progress panel remains visible and updates as agents work.
""".strip()


def _result(text: str, *, data: Any = None, error: bool = False) -> ToolResult:
    return ToolResult(content=[TextContent(text=text)], details=data, is_error=error)


class WorkflowExtension:
    """One controller per harness build; only this controller's children are joined."""

    def __init__(self, api: ExtensionAPI, *, mode: WorkflowMode = DEFAULT_MODE) -> None:
        self.api = api
        self.engine = WorkflowEngine()
        self.runs: dict[str, Workflow] = {}
        self.selected: str | None = None
        self.visible = True
        self.mode: WorkflowMode = mode
        self._default_mode: WorkflowMode = mode
        self._turn_allowed: bool | None = None
        self._turn_mode: WorkflowMode | None = None
        self._request_context: ExtensionContext | None = None
        self.remaining = PROMPT_DELEGATION_LIMIT
        self._last_render = 0.0
        self._last_signature: Any = None
        self._restore_warning: str | None = None

    def install(self) -> None:
        for name, description, schema, execute in (
            (
                "workflow_plan",
                "Create or adapt a workflow task graph. create needs goal/tasks; add/revise need "
                "run_id/tasks/reason; retry/skip need run_id/task_ids/reason; finish needs "
                "run_id/summary. Changes are atomic; cycles and unknown dependencies are rejected. "
                "In workflow mode off, mutations require an explicit current workflow request.",
                PLAN_SCHEMA,
                self.plan,
            ),
            (
                "workflow_run",
                "Execute ready workflow tasks through Aelix's consent-taking delegation runtime. "
                "Independent tasks run concurrently and successors receive dependency summaries. "
                "Returns execution evidence for replanning; failed work is never retried automatically. "
                "Requires --agents. This foreground call updates the TUI throughout execution. "
                "In workflow mode off, execution requires an explicit current workflow request.",
                RUN_SCHEMA,
                self.run,
            ),
            (
                "workflow_status",
                "Inspect workflow tasks, attempts, outputs, dependency waits and revision history. "
                "With no run_id, inspect the most recently selected workflow.",
                STATUS_SCHEMA,
                self.status,
            ),
        ):
            self.api.register_tool(
                AgentTool(
                    name=name,
                    description=description,
                    parameters=schema,
                    execute=execute,
                    prompt_snippet=description.split(".")[0],
                    # One tool call at a time avoids concurrent graph mutations and consent modals.
                    execution_mode="sequential",
                )
            )
        self.api.register_command(
            "workflow",
            handler=self.command,
            description="Run/inspect a workflow or set /workflow mode on|off",
        )
        self.api.on("session_start", self.session_start)
        self.api.on("before_agent_start", self.before_agent_start)
        self.api.on("tool_call", self.tool_call)
        self.api.on("settled", self.settled)
        self.api.on("session_shutdown", self.shutdown)
        # The TUI emits this public UI probe after binding its real context,
        # including startup/rebuilds. session_start can run before that bind.
        unsubscribe = self.api.events.on("ui:list-modules", self.ui_ready)
        self.api.add_cleanup(unsubscribe)
        self.api.add_cleanup(self.engine.close)

    def ui_ready(self, _probe: Any) -> None:
        self._render()

    def _mode_status(self) -> str:
        text = f"Workflow mode: {self.mode}"
        if self._request_active() and self._turn_mode != self.mode:
            text += " (next request)"
        return text

    def _request_active(self) -> bool:
        if self._turn_allowed is None:
            return False
        if self._request_context is None:
            return True
        try:
            return not self._request_context.is_idle()
        except ExtensionError:
            return False

    def _allowed(self) -> bool:
        if self._request_active():
            return bool(self._turn_allowed)
        return self.mode == "on"

    def _require_activation(self) -> None:
        if not self._allowed():
            raise WorkflowError(
                "Workflow mode off applies to this request. It has not asked to use a workflow. "
                "Do the work with the ordinary tools; do not retry this workflow call. "
                "The user can invoke /workflow <goal>, explicitly ask to use a workflow, "
                "or switch to /workflow mode on."
            )

    async def tool_call(self, event: Any, _ctx: ExtensionContext) -> ToolCallResult | None:
        if event.tool_name not in {"workflow_plan", "workflow_run"}:
            return None
        try:
            self._require_activation()
        except WorkflowError as exc:
            return ToolCallResult(block=True, reason=str(exc))
        return None

    async def settled(self, _event: Any, _ctx: ExtensionContext) -> None:
        # Aelix emits settled per run, including before a retry. The live
        # context's is_idle() marks the end of the whole prompt instead.
        self._render()

    def _get(self, run_id: Any = None) -> Workflow:
        target = run_id if run_id is not None else self.selected
        if not isinstance(target, str) or target not in self.runs:
            raise WorkflowError("Unknown workflow. Create one with workflow_plan(action='create').")
        self.selected = target
        return self.runs[target]

    def _changed(self, persist: bool, run: Workflow | None = None) -> None:
        if run is None and self.selected is None:
            return
        run = run or self.runs[self.selected]  # type: ignore[index]
        if persist:
            # The host queues these records and drains them with its session lifecycle.
            # ToolResult.details alone is deliberately not used as durable storage.
            try:
                self.api.append_entry(SNAPSHOT_TYPE, run.to_dict())
            except ExtensionError:
                _LOG.debug("Workflow snapshot reached a disposed runtime", exc_info=True)
        self._render(force=persist)

    def _render(self, *, force: bool = True) -> None:
        now = time.monotonic()
        selected = self.runs.get(self.selected or "")
        signature = (
            (
                self.mode,
                selected.id,
                selected.state,
                [(t.state, t.current_tool) for t in selected.tasks.values()],
            )
            if selected is not None
            else self.mode
        )
        if not force and signature == self._last_signature and now - self._last_render < 0.1:
            return
        self._last_render = now
        self._last_signature = signature
        ui = self.api.runtime.ui  # Read live: the host replaces UI bindings on rebuild.
        if ui is HEADLESS_UI_CONTEXT:
            return
        try:
            if self.selected is None or not self.visible:
                ui.set_widget(WIDGET_KEY, None)
                ui.set_status(STATUS_KEY, self._mode_status())
            else:
                run = self.runs[self.selected]
                terminal = shutil.get_terminal_size((100, 24))
                ui.set_widget(
                    WIDGET_KEY,
                    render_panel(
                        run,
                        width=max(1, terminal.columns - 2),
                        max_rows=min(6, max(1, terminal.lines - 16)),
                        mode=self.mode,
                    ),
                )
                ui.set_status(STATUS_KEY, f"{self._mode_status()} | {status_line(run)}")
        except Exception:
            # A disappearing/broken TUI must never change the outcome of delegated work.
            _LOG.debug("Workflow UI update failed", exc_info=True)

    async def plan(self, args: dict[str, Any], _context: ToolExecutionContext) -> ToolResult:
        try:
            self._require_activation()
            action = args.get("action")
            fields = {
                "create": {"action", "goal", "tasks", "max_concurrency"},
                "add": {"action", "run_id", "tasks", "reason"},
                "revise": {"action", "run_id", "tasks", "reason"},
                "retry": {"action", "run_id", "task_ids", "reason"},
                "skip": {"action", "run_id", "task_ids", "reason"},
                "finish": {"action", "run_id", "summary"},
                "cancel": {"action", "run_id"},
            }
            if not isinstance(action, str) or action not in fields:
                raise WorkflowError(f"Unknown workflow action: {action}")
            extra = {k for k, v in args.items() if v is not None} - fields[action]
            if extra:
                raise WorkflowError(f"Fields not supported by {action}: {sorted(extra)}")
            if action != "create" and not isinstance(args.get("run_id"), str):
                raise WorkflowError(f"{action} requires an explicit run_id.")
            if action == "create":
                if any(r.state == "running" for r in self.runs.values()):
                    raise WorkflowError("Wait for the active workflow before creating another.")
                run = Workflow.create(
                    args.get("goal"), args.get("tasks"), args.get("max_concurrency", 2)
                )
                self.runs[run.id] = run
                self.selected = run.id
            else:
                run = self._get(args.get("run_id"))
                if action in {"add", "revise"}:
                    run.amend(
                        args.get("tasks"), replace=action == "revise", reason=args.get("reason")
                    )
                elif action == "retry":
                    run.retry(args.get("task_ids"), args.get("reason"))
                elif action == "skip":
                    run.skip(args.get("task_ids"), args.get("reason"))
                elif action == "finish":
                    run.finish(args.get("summary"))
                elif action == "cancel":
                    if any(t.state == "running" for t in run.tasks.values()):
                        raise WorkflowError(
                            "Interrupt the active execution before cancelling its graph."
                        )
                    run.cancel()
                else:
                    raise WorkflowError(f"Unknown workflow action: {action}")
            self._changed(True)
            return _result(render_details(run), data=run.to_dict())
        except (WorkflowError, ValueError) as exc:
            return _result(str(exc), error=True)

    def _admit(self) -> bool:
        if self.remaining <= 0:
            return False
        self.remaining -= 1
        return True

    async def run(self, args: dict[str, Any], context: ToolExecutionContext) -> ToolResult:
        try:
            self._require_activation()
            if not isinstance(args.get("run_id"), str) or set(args) - {"run_id"}:
                raise WorkflowError("workflow_run requires run_id and accepts no other fields.")
            run = self._get(args.get("run_id"))
            runtime = self.api.runtime.subagents
            if runtime is None or "agent" not in self.api.get_active_tools():
                raise WorkflowError(
                    "Delegation is unavailable. Start Aelix with --agents and an active agent tool."
                )
            # Resolve every pending identity before starting any work. Model-originated
            # tasks never use the human command's project-identity override.
            profiles = {
                task.spec.id: runtime.resolve_profile(task.spec.profile, allow_project=False)
                for task in run.tasks.values()
                if task.state == "pending"
            }

            async def delegate(
                task: Task, prompt: str, on_event: Callable[[SubagentProgress], None]
            ) -> SubagentResult:
                # Public spawn takes its OWN consent and narrows permissions/tools.
                # No private spawn_granted API, forged grant, or new process launcher.
                # This public door does not charge the host agent-tool budget, so this
                # extension enforces a separate, shared 16-per-user-prompt admission cap.
                return await runtime.spawn(
                    profiles[task.spec.id],
                    prompt,
                    timeout_ms=task.spec.timeout_ms,
                    on_event=on_event,
                )

            await self.engine.execute(
                run,
                delegate,
                changed=lambda persist: self._changed(persist, run),
                admit=self._admit,
                signal=context.signal,
            )
            text = render_details(run)
            text += (
                "\n\nInspect the results. Add/revise tasks for new findings, explicitly retry failures, "
                "or finish with a verified summary. Agent success alone is not acceptance verification."
            )
            if run.state == "needs_attention" and self.remaining <= 0:
                text += (
                    "\nThe per-prompt delegation budget is exhausted. Continue in a new user turn."
                )
            return _result(text, data=run.to_dict(), error=run.state == "needs_attention")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            return _result(str(exc), error=True)

    async def status(self, args: dict[str, Any], _context: ToolExecutionContext) -> ToolResult:
        try:
            if set(args) - {"run_id"}:
                raise WorkflowError("workflow_status accepts only run_id.")
            if not self.runs and args.get("run_id") is None:
                return _result(f"{self._mode_status()}\nNo workflow yet.", data={"mode": self.mode})
            run = self._get(args.get("run_id"))
            self._render()
            text = f"{self._mode_status()}\n{render_details(run)}"
            if self._restore_warning:
                text = f"{self._restore_warning}\n\n{text}"
            return _result(text, data=run.to_dict())
        except WorkflowError as exc:
            return _result(str(exc), error=True)

    async def command(self, args: str, _ctx: ExtensionContext) -> str:
        args = args.strip()
        words = args.lower().split()
        if words == ["mode"]:
            return (
                f"{self._mode_status()}\n"
                "on: proactively use workflows when useful. off: explicit workflow requests only.\n"
                "Set with /workflow mode on or /workflow mode off."
            )
        if words and words[0] == "mode":
            if len(words) != 2 or words[1] not in {"on", "off"}:
                return "Usage: /workflow mode on|off"
            self.mode = "on" if words[1] == "on" else "off"
            if _ctx is None or _ctx.is_idle():
                self._turn_allowed = None
                self._turn_mode = None
                self._request_context = None
            self.api.append_entry(MODE_ENTRY_TYPE, {"schema_version": 1, "mode": self.mode})
            self._render()
            return f"{self._mode_status()}. Updated for this session; applies to the next request."
        if args in {"hide", "show"}:
            self.visible = args == "show"
            self._render()
            return f"Workflow panel {args}."
        if not args or args == "status" or args.startswith("status "):
            if not self.runs:
                return (
                    self._restore_warning
                    or f"{self._mode_status()}\nNo workflow yet. Use /workflow <goal>."
                )
            target = args.removeprefix("status ") if args.startswith("status ") else None
            try:
                return f"{self._mode_status()}\n{render_details(self._get(target))}"
            except WorkflowError as exc:
                return str(exc)
        message = UserMessage(
            content=[
                TextContent(
                    text=f"Workflow: {args}\n\nCreate and execute an adaptive workflow for this goal. "
                    "Use workflow_plan, workflow_run and workflow_status. Replan from actual results, "
                    "verify the result, then finish the workflow."
                )
            ]
        )
        if _ctx is not None and not _ctx.is_idle():
            self.api.send_message(message, deliver_as="follow_up")
        else:
            # send_user_message queues at idle. send_message's explicit trigger
            # starts a managed host turn without also enqueuing it a second time.
            self.api.send_message(message, trigger_turn=True)
        return "Workflow requested. Progress will appear above the editor."

    async def before_agent_start(
        self, event: Any, _ctx: ExtensionContext
    ) -> BeforeAgentStartResult:
        self.remaining = PROMPT_DELEGATION_LIMIT
        active_mode: WorkflowMode = self.mode
        self._turn_mode = active_mode
        self._request_context = _ctx
        self._turn_allowed = self.mode == "on" or requested_workflow(getattr(event, "prompt", ""))
        self._render()
        if not {"workflow_plan", "workflow_run"}.issubset(self.api.get_active_tools()):
            return BeforeAgentStartResult()
        policy = activation_guidance(active_mode, allowed=self._turn_allowed)
        if "agent" not in self.api.get_active_tools():
            policy += "\nDelegation is unavailable in this session. Do not create or run workflows; handle work directly."
        return BeforeAgentStartResult(
            system_prompt=f"{event.system_prompt}\n\n{policy}\n\n{_GUIDANCE}"
        )

    async def session_start(self, _event: Any, ctx: ExtensionContext) -> None:
        self.runs.clear()
        self.selected = None
        self._restore_warning = None
        self.mode = self._default_mode
        self._turn_allowed = None
        self._turn_mode = None
        self._request_context = None
        try:
            session = ctx.session_manager.get_session()
            entries = await session.get_branch() if session is not None else []
            for entry in entries:
                if getattr(entry, "custom_type", None) == MODE_ENTRY_TYPE:
                    data = entry.data
                    if (
                        isinstance(data, dict)
                        and data.get("schema_version") == 1
                        and isinstance(data.get("mode"), str)
                        and data["mode"] in {"on", "off"}
                    ):
                        self.mode = "on" if data["mode"] == "on" else "off"
                        if self._restore_warning == "Could not restore workflow mode; using off.":
                            self._restore_warning = None
                    else:
                        self.mode = "off"
                        self._restore_warning = "Could not restore workflow mode; using off."
                    continue
                if getattr(entry, "custom_type", None) != SNAPSHOT_TYPE:
                    continue
                try:
                    run = Workflow.restore(entry.data)
                except (WorkflowError, TypeError, KeyError) as exc:
                    self._restore_warning = f"Could not restore a workflow snapshot: {exc}"
                    _LOG.warning(self._restore_warning)
                    # Do not present an older successful snapshot as the latest state.
                    if isinstance(entry.data, dict):
                        bad_id = entry.data.get("id")
                        if isinstance(bad_id, str):
                            self.runs.pop(bad_id, None)
                            if self.selected == bad_id:
                                self.selected = next(reversed(self.runs), None)
                    continue
                self.runs[run.id] = run
                self.selected = run.id
        except Exception:
            _LOG.debug("Workflow session records unavailable", exc_info=True)
        self._render()

    async def shutdown(self, _event: Any, _ctx: ExtensionContext) -> None:
        await self.engine.close()
        ui = self.api.runtime.ui
        if ui is not HEADLESS_UI_CONTEXT:
            try:
                ui.set_widget(WIDGET_KEY, None)
                ui.set_status(STATUS_KEY, None)
            except Exception:
                _LOG.debug("Workflow UI cleanup failed", exc_info=True)


def setup(aelix: ExtensionAPI) -> None:
    if subagent_depth() >= MAX_SUBAGENT_DEPTH:
        return
    WorkflowExtension(aelix).install()
