# ADR-0002: User-controlled proactive and explicit workflow modes

Status: Accepted

Date: 2026-10-08

## Problem

The initial extension described workflows when requested but exposed plan/run tools without
an activation rule. `/workflow` was a convenient entry rather than an enforced prerequisite.
Users need to choose between active model-driven orchestration and explicit use.

## Decision

Default to **off**, as selected by the user. Off permits `/workflow <goal>` and affirmative
current requests such as "use a workflow" and "워크플로로 진행해주세요". A workflow topic
question, old transcript, task argument, or dependency output does not activate a request.
The detector is a documented English/Korean request-cue convention, not a general natural
language intent classifier. The slash command supplies an explicit `Workflow:` request label.

**On** instructs the parent model to actively assess and prefer workflows for substantive work
and independent verification. It remains a judgment about task shape; a simple question or
one-step edit does not need a workflow. This is a system-prompt strategy, not an automatic
graph template or a separate planning-model call.

Use `/workflow mode on|off` for user control. Mode changes do not themselves start a model
turn. The model has no mode-setting tool/argument and cannot opt in by writing "workflow"
inside its own tool arguments. Check activation in both the hook and the plan/run handlers.
Keep status readable in either mode.

## Request lifetime and host behavior

Snapshot activation at `before_agent_start` for the whole parent prompt. A mode change during
that prompt applies to the next request and does not revoke its admitted work. On the current
host, `settled` is emitted per run, including before retries, rather than at whole-prompt
release. Therefore read the hook context's live `is_idle()` when determining whether the
saved activation still applies; do not drop an explicit opt-in at `agent_end`/`settled`.
The next `before_agent_start` always computes a new activation from the new request.

Do not re-register or re-enable tools when toggling mode: a CLI/user active-tool restriction
must remain authoritative. The native permission/consent paths and budgets remain in force.

## Persistence and visibility

Append `aelix-workflow.mode` records through the host API and restore the latest selected-branch
preference at `session_start`. Resume/reload/fork preserve that branch preference; a new
session defaults to off. No global user settings or project configuration files are written.
The host pins asynchronous appends and awaits them at teardown; this is not a synchronous
storage acknowledgment. A malformed mode record falls back to off and reports the restore gap.

Show the mode in the TUI footer even when there is no graph, and in the expanded progress
panel when work exists. Hiding progress keeps the mode visible. Aelix can emit `session_start`
before binding its real TUI. Observe the public `ui:list-modules` probe that the TUI emits
after binding and render then; unsubscribe at cleanup. This adds no polling/background task
and does not contribute descriptors or replace the host's footer.
