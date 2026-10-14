"""Public Aelix integration: tools, slash command, session records, and live TUI."""

from __future__ import annotations

import asyncio
import logging
import os
import shlex
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
    ResolvedProfile,
    SubagentProgress,
    SubagentResult,
    subagent_depth,
)
from aelix_coding_agent.tools import ALL_TOOL_NAMES

from aelix_workflow import recovery, report
from aelix_workflow.activation import (
    DEFAULT_MODE,
    MODE_ENTRY_TYPE,
    WorkflowMode,
    activation_guidance,
    requested_workflow,
)
from aelix_workflow.engine import CheckOutcome, CheckRefused, WorkflowEngine
from aelix_workflow.model import WRITE_TOOLS, Attempt, Task, Workflow, WorkflowError
from aelix_workflow.panel import STATUS_KEY, WIDGET_KEY, render_details, render_panel, status_line
from aelix_workflow.schema import PLAN_SCHEMA, RUN_SCHEMA, STATUS_SCHEMA
from aelix_workflow.workspace import GitWorkspace

SNAPSHOT_TYPE = "aelix-workflow.snapshot"
PROMPT_DELEGATION_LIMIT = 16
ALLOW_CHECKS = "Allow for this workflow"
_LOG = logging.getLogger(__name__)
_GUIDANCE = """
Dynamic workflows are available through workflow_plan, workflow_run and workflow_status.
When workflow activation permits it, decompose the goal into verifiable, self-contained tasks
using existing agent profiles from the agent tool's roster. Create a dependency graph with
workflow_plan(action='create') and execute it with workflow_run(run_id=...). Independent tasks
run in parallel, subject to max_concurrency, declared write scopes and the host's permission
checks.
Let the graph adapt without extra turns where you can: an output_schema gives a task structured
data; for_each runs one child per item of such data (where filters items); when skips a task
whose condition on a dependency result is false; kind='check' runs a command the user approves
and passes on exit code 0; repair on a check reruns an upstream fix with the failure output.
Mark tasks that must change files access='write' and list the files they own in writes.
Overlapping writers never run together; a write task whose child had no write authority fails
as read_only. Keep read-only investigations read.
workflow_run reports only what finished in that run; read details with workflow_status(run_id,
task_id). Use add or revise to adapt the graph, and retry a failed or interrupted task only
after examining its failure and possible side effects.
A declined delegation or command is the user's refusal: stop that branch and do not retry it.
Authentication failures, credit/usage limits, unavailable models and unavailable delegation
halt the run and require a change outside the workflow. Do not retry unchanged calls or create
another workflow to work around the same blocker. Use ordinary available tools when they can do
the work and report the delegated checks as unverified.
Revising a task invalidates dependent results. A task marked succeeded means its agent returned
successfully, not that every acceptance check passed; prefer check tasks for acceptance. Call
finish with an evidence-based summary only after the goal is achieved. Do not silently skip
failed work. No workflow or child may increase the parent's tool grant or approve an action for
the user. Workflows require the host's --agents feature and an active agent tool. Execution is
foreground: the TUI panel updates while agents work, and Ctrl+C pauses the run.
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
        self._cwd: str | None = None
        self._project_trusted = False
        # Verification commands the user answered, per workflow run; never persisted.
        self._approved: set[tuple[str, tuple[str, ...]]] = set()
        self._denied: set[tuple[str, tuple[str, ...]]] = set()

    def install(self) -> None:
        for name, description, schema, execute in (
            (
                "workflow_plan",
                "Create or adapt a workflow task graph. create needs goal/tasks; add/revise need "
                "run_id/tasks/reason; retry/skip need run_id/task_ids/reason; finish needs "
                "run_id/summary. Tasks delegate to agent profiles or run approved check "
                "commands; output_schema, for_each, when and repair let the graph expand from "
                "results. Changes are atomic; cycles and unknown dependencies are rejected. "
                "In workflow mode off, mutations require an explicit current workflow request.",
                PLAN_SCHEMA,
                self.plan,
            ),
            (
                "workflow_run",
                "Execute ready workflow tasks through Aelix's consent-taking delegation runtime. "
                "Independent tasks run concurrently, successors receive dependency results, and "
                "for_each/when/repair steps happen without another turn. The report lists only "
                "what finished in this run. Failed work is not retried automatically, except "
                "transient failures of children that ran no tool. Requires --agents. This "
                "foreground call updates the TUI throughout execution; Ctrl+C pauses it. "
                "In workflow mode off, execution requires an explicit current workflow request.",
                RUN_SCHEMA,
                self.run,
            ),
            (
                "workflow_status",
                "Inspect workflow tasks, dependency waits and revision history, or one task's "
                "attempts, result data and tool trail with task_id. With no run_id, inspect the "
                "most recently selected workflow.",
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
            description="Run/inspect/continue a workflow or set /workflow mode on|off",
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
            self._append(SNAPSHOT_TYPE, run.to_dict())
        self._render(force=persist)

    def _append(self, custom_type: str, data: Any) -> None:
        try:
            self.api.append_entry(custom_type, data)
        except ExtensionError:
            _LOG.debug("Workflow record reached a disposed runtime", exc_info=True)

    def _started(self, run: Workflow, task: Task, attempt: Attempt) -> None:
        # A full snapshot follows at completion; this small record exists so a crash in
        # between still knows which child the attempt belonged to.
        self._append(recovery.ATTEMPT_TYPE, recovery.attempt_record(run, task, attempt))

    def _render(self, *, force: bool = True) -> None:
        now = time.monotonic()
        selected = self.runs.get(self.selected or "")
        signature = (
            (
                self.mode,
                selected.id,
                selected.state,
                [(t.state, t.current_tool) for t in selected.iter_all()],
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

    def _resolve(self, name: str) -> ResolvedProfile:
        runtime = self.api.runtime.subagents
        if runtime is None:
            raise WorkflowError(
                "Delegation is unavailable. Start Aelix with --agents and an active agent tool."
            )
        # Model-originated tasks never use the human command's project-identity override.
        return runtime.resolve_profile(name, allow_project=False)

    def _check_profiles(self, tasks: Any) -> None:
        """Refuse an unknown profile while the plan is written, not after work started."""
        if self.api.runtime.subagents is None or not isinstance(tasks, list):
            return
        for task in tasks:
            if not isinstance(task, dict) or task.get("kind", "agent") not in {"agent", None}:
                continue
            name = task.get("profile")
            if isinstance(name, str) and name.strip():
                try:
                    self._resolve(name.strip())
                except Exception as exc:
                    raise WorkflowError(
                        f"Task {task.get('id')}: profile {name!r} is not available: {exc}"
                    ) from None

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
            if action in {"create", "add", "revise"}:
                self._check_profiles(args.get("tasks"))
            if action == "create":
                if self.engine.active:
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
                    if any(t.state == "running" for t in run.units()):
                        raise WorkflowError(
                            "Interrupt the active execution before cancelling its graph."
                        )
                    run.cancel()
                else:
                    raise WorkflowError(f"Unknown workflow action: {action}")
            self._changed(True)
            return _result(report.overview(run), data=run.to_dict())
        except (WorkflowError, ValueError) as exc:
            return _result(str(exc), error=True)

    def _admit(self) -> bool:
        if self.remaining <= 0:
            return False
        self.remaining -= 1
        return True

    def _write_capable(self, resolved: ResolvedProfile) -> bool:
        grant = set(self.api.get_active_tools())
        tools = resolved.profile.tools
        requested = set(tools) if tools is not None else grant
        return bool(requested & grant & set(ALL_TOOL_NAMES) & WRITE_TOOLS)

    async def _approve_checks(self, run: Workflow) -> None:
        """Ask once per run, before any child starts, so no dialog competes with spawn consent."""
        commands = [
            t.spec.command for t in run.units() if t.spec.kind == "check" and t.state == "pending"
        ]
        unknown = [
            c
            for c in dict.fromkeys(commands)
            if (run.id, c) not in self._approved and (run.id, c) not in self._denied
        ]
        ui = self.api.runtime.ui
        if not unknown or ui is HEADLESS_UI_CONTEXT:
            return
        listing = "\n".join(f"  $ {shlex.join(command)}" for command in unknown)
        writers = sorted(t.id for t in run.units() if t.spec.access == "write")
        warning = (
            f"Write tasks in this workflow ({', '.join(writers[:6])}) can change the code these "
            "commands execute, and later rounds run it without asking again."
            if writers
            else "Later rounds of this workflow run them again without asking."
        )
        title = (
            f"Workflow {run.id} asks to run {len(unknown)} verification command(s) in "
            f"{self._cwd_now()}:\n{listing}\n"
            f"They run without a shell, with your own permissions. {warning}"
        )
        try:
            answer = await ui.select(title, [ALLOW_CHECKS, "Deny"])
        except Exception:
            answer = None
        target = self._approved if answer == ALLOW_CHECKS else self._denied
        target.update((run.id, command) for command in unknown)

    async def _check(self, run: Workflow, task: Task) -> CheckOutcome:
        command = task.spec.command
        if (run.id, command) not in self._approved:
            raise CheckRefused(
                "The user declined this verification command."
                if (run.id, command) in self._denied
                else "Verification commands need the user's approval in the interactive TUI."
            )
        started = time.monotonic()
        result = await self.api.exec(
            command[0], list(command[1:]), cwd=self._cwd_now(), timeout_ms=task.spec.timeout_ms
        )
        output = "\n".join(part for part in (result.stdout, result.stderr) if part.strip())
        return CheckOutcome(
            exit_code=result.code,
            output=output[-20_000:],
            elapsed_ms=int((time.monotonic() - started) * 1_000),
            timed_out=result.killed,
        )

    def _cwd_now(self) -> str:
        return self._cwd or os.getcwd()

    async def run(self, args: dict[str, Any], context: ToolExecutionContext) -> ToolResult:
        try:
            self._require_activation()
            if not isinstance(args.get("run_id"), str) or set(args) - {
                "run_id",
                "stop_on_failure",
            }:
                raise WorkflowError(
                    "workflow_run requires run_id and accepts only stop_on_failure besides it."
                )
            stop_on_failure = args.get("stop_on_failure") or False
            if not isinstance(stop_on_failure, bool):
                raise WorkflowError("stop_on_failure must be true or false.")
            run = self._get(args.get("run_id"))
            if self.engine.active:
                raise WorkflowError("A workflow execution is already active in this session.")
            # Every identity this execution can need is resolved before any work starts,
            # including the targets that a check's repair may rerun.
            upcoming = [t for t in run.units() if t.state == "pending"]
            for check in [t for t in upcoming if t.spec.repair]:
                upcoming.append(run.tasks[check.spec.repair["task"]])  # type: ignore[index]
            agents = [t for t in upcoming if t.spec.kind == "agent"]
            if agents and "agent" not in self.api.get_active_tools():
                raise WorkflowError(
                    "Delegation is unavailable. Start Aelix with --agents and an active agent tool."
                )
            profiles = {name: self._resolve(name) for name in {t.spec.profile for t in agents}}
            for task in agents:
                if task.spec.access == "write" and not self._write_capable(
                    profiles[task.spec.profile]
                ):
                    raise WorkflowError(
                        f"Task {task.id} must change files, but profile {task.spec.profile!r} "
                        "has no edit, write or bash tool under the current tool grant. Revise "
                        "the task's profile or ask the user to grant those tools."
                    )
            await self._approve_checks(run)

            async def delegate(
                task: Task, prompt: str, on_event: Callable[[SubagentProgress], None]
            ) -> SubagentResult:
                # Public spawn takes its OWN consent and narrows permissions/tools.
                # No private spawn_granted API, forged grant, or new process launcher.
                # This public door does not charge the host agent-tool budget, so this
                # extension enforces a separate, shared 16-per-user-prompt admission cap.
                resolved = profiles.get(task.spec.profile) or self._resolve(task.spec.profile)
                return await self.api.runtime.subagents.spawn(  # type: ignore[union-attr]
                    resolved, prompt, timeout_ms=task.spec.timeout_ms, on_event=on_event
                )

            execution = await self.engine.execute(
                run,
                delegate,
                changed=lambda persist: self._changed(persist, run),
                admit=self._admit,
                signal=context.signal,
                check=lambda task: self._check(run, task),
                started=lambda task, attempt: self._started(run, task, attempt),
                # git status can run filters named by the repository's own configuration.
                workspace=GitWorkspace(self.api, self._cwd_now())
                if self._project_trusted
                else None,
                stop_on_failure=stop_on_failure,
            )
            text = f"{report.run_report(run, execution)}\n{report.NEXT_STEPS}"
            if execution.halted and self.remaining <= 0:
                text += (
                    "\nThe per-prompt delegation budget is exhausted. Continue in a new user "
                    "turn; the user can type /workflow continue."
                )
            return _result(text, data=run.to_dict(), error=run.state == "needs_attention")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            return _result(str(exc), error=True)

    async def status(self, args: dict[str, Any], _context: ToolExecutionContext) -> ToolResult:
        try:
            if set(args) - {"run_id", "task_id"}:
                raise WorkflowError("workflow_status accepts only run_id and task_id.")
            if not self.runs and args.get("run_id") is None:
                return _result(f"{self._mode_status()}\nNo workflow yet.", data={"mode": self.mode})
            run = self._get(args.get("run_id"))
            self._render()
            task_id = args.get("task_id")
            body = (
                report.overview(run)
                if task_id is None
                else report.task_detail(run, run.find(task_id))
            )
            text = f"{self._mode_status()}\n{body}"
            if self._restore_warning:
                text = f"{self._restore_warning}\n\n{text}"
            return _result(text, data=run.to_dict())
        except WorkflowError as exc:
            return _result(str(exc), error=True)

    def _send(self, text: str, ctx: ExtensionContext | None) -> None:
        message = UserMessage(content=[TextContent(text=text)])
        if ctx is not None and not ctx.is_idle():
            self.api.send_message(message, deliver_as="follow_up")
        else:
            # send_user_message queues at idle. send_message's explicit trigger
            # starts a managed host turn without also enqueuing it a second time.
            self.api.send_message(message, trigger_turn=True)

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
        if not args or words[0] == "status":
            if not self.runs:
                return (
                    self._restore_warning
                    or f"{self._mode_status()}\nNo workflow yet. Use /workflow <goal>."
                )
            parts = args.split()[1:]
            try:
                run = self._get(parts[0] if parts else None)
                if len(parts) > 1:
                    return report.task_detail(run, run.find(parts[1]))
                return f"{self._mode_status()}\n{render_details(run)}"
            except WorkflowError as exc:
                return str(exc)
        parts = args.split()[1:]
        if words[0] == "continue" and (not parts or (len(parts) == 1 and parts[0] in self.runs)):
            # "/workflow continue the refactor" is a goal, not a run to resume.
            target = parts[0] if parts else self.selected
            if target is None or target not in self.runs:
                return "No workflow to continue. Use /workflow <goal> to start one."
            self._send(
                f"Workflow: continue run {target}.\n\nInspect it with workflow_status, then "
                f"resume it with workflow_run(run_id='{target}'). Retry interrupted or failed work "
                "only after checking its cause and side effects. Replan from the results, verify "
                "the result, then finish the workflow.",
                _ctx,
            )
            return f"Continuing workflow {target}."
        self._send(
            f"Workflow: {args}\n\nCreate and execute an adaptive workflow for this goal. "
            "Use workflow_plan, workflow_run and workflow_status. Replan from actual results, "
            "verify the result, then finish the workflow.",
            _ctx,
        )
        return "Workflow requested. Progress will appear above the editor."

    async def before_agent_start(
        self, event: Any, _ctx: ExtensionContext
    ) -> BeforeAgentStartResult:
        self.remaining = PROMPT_DELEGATION_LIMIT
        active_mode: WorkflowMode = self.mode
        self._turn_mode = active_mode
        self._request_context = _ctx
        self._capture_cwd(_ctx)
        self._turn_allowed = self.mode == "on" or requested_workflow(getattr(event, "prompt", ""))
        self._render()
        if not {"workflow_plan", "workflow_run"}.issubset(self.api.get_active_tools()):
            return BeforeAgentStartResult()
        policy = activation_guidance(active_mode, allowed=self._turn_allowed)
        if "agent" not in self.api.get_active_tools():
            policy += "\nDelegation is unavailable in this session. Do not create or run workflows; handle work directly."
        # A request that may not use workflows does not pay for the usage guide.
        guide = f"\n\n{_GUIDANCE}" if self._turn_allowed else ""
        return BeforeAgentStartResult(system_prompt=f"{event.system_prompt}\n\n{policy}{guide}")

    def _capture_cwd(self, ctx: Any) -> None:
        try:
            cwd = getattr(ctx, "cwd", None) if ctx is not None else None
            trusted = getattr(ctx, "is_project_trusted", None) if ctx is not None else None
            self._project_trusted = bool(trusted()) if callable(trusted) else False
        except ExtensionError:
            cwd = None
            self._project_trusted = False
        if isinstance(cwd, str) and cwd:
            self._cwd = cwd

    async def session_start(self, _event: Any, ctx: ExtensionContext) -> None:
        self.runs.clear()
        self.selected = None
        self._restore_warning = None
        self.mode = self._default_mode
        self._turn_allowed = None
        self._turn_mode = None
        self._request_context = None
        self._approved.clear()
        self._denied.clear()
        self._capture_cwd(ctx)
        try:
            session = ctx.session_manager.get_session()
            entries = await session.get_branch() if session is not None else []
            latest: dict[str, tuple[int, Any]] = {}
            records: list[tuple[int, Any]] = []
            for index, entry in enumerate(entries):
                custom_type = getattr(entry, "custom_type", None)
                if custom_type == MODE_ENTRY_TYPE:
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
                elif custom_type == SNAPSHOT_TYPE:
                    run_id = entry.data.get("id") if isinstance(entry.data, dict) else None
                    if isinstance(run_id, str):
                        latest[run_id] = (index, entry.data)
                    else:
                        self._restore_warning = "Could not restore a workflow snapshot: no id"
                elif custom_type == recovery.ATTEMPT_TYPE:
                    records.append((index, entry.data))
            receipts = recovery.collect_receipts(entries)
            parent_path = getattr(session, "session_file", None)
            # Only each run's latest snapshot counts: an invalid latest snapshot must not
            # resurrect an older, more successful-looking state.
            for index, data in sorted(latest.values(), key=lambda item: item[0]):
                try:
                    run = Workflow.restore(data)
                except (WorkflowError, TypeError, KeyError, ValueError) as exc:
                    self._restore_warning = f"Could not restore a workflow snapshot: {exc}"
                    _LOG.warning(self._restore_warning)
                    continue
                touched = False
                for record_index, record in records:
                    if record_index > index:
                        touched = recovery.apply_attempt_record(run, record) or touched
                if touched:
                    run.sync()
                    run.refresh_state()
                self.runs[run.id] = run
                self.selected = run.id
                notes = await recovery.reconcile(run, receipts, parent_path)
                if touched or notes:
                    self._append(SNAPSHOT_TYPE, run.to_dict())
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
