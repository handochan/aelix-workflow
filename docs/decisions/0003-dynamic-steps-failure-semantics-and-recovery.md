# ADR-0003: Declarative dynamic steps, failure semantics and attempt recovery

Status: Accepted

Date: 2026-10-14

## Problem

The first version scheduled a validated graph, but several behaviors were not robust, each
reproduced against the code at `c327f40`:

1. After the host's `stop_all` (session teardown), the engine kept admitting tasks through
   `runtime.spawn`, and that user-typed door reopened the host's closed delegation door.
2. Every failure was the same kind. Identical HTTP 402 credit failures launched every ready task.
3. A write task whose child ran in `plan` posture succeeded; `permission_mode`, `dropped_tools`
   and `tool_trail` from the host envelope were discarded.
4. The attempt-to-child link was persisted only when the attempt finished.
5. Dependency results were cut by characters and checked by bytes, so Korean results made a
   successor fail deterministically, with an error that blamed the task prompt.
6. 64 tasks were allowed but only 32 attempts, so a 40-task graph could never finish.
7. Ctrl+C cancelled the whole run and marked never-started work as needing side-effect review.
8. Every `workflow_run`/`workflow_status` resent all results of all attempts to the parent.
9. Snapshot restore compared exact field sets, so any new attempt field broke old sessions.

Separately, every adaptation needed a parent turn: run, read everything, add, run again.

## Decision

### Failure semantics

Each attempt records a failure kind: `declined`, `aborted`, `timeout`, `blocker`, `transient`,
`invalid_output`, `read_only`, `check_failed`, `error` or `interrupted`. `aborted` (an external
stop or the host's draining refusal) and `blocker` (authentication, credit, model or delegation
availability) halt new admissions for the execution; running members finish. Only a
`transient` failure of a child that ran no tool (empty `tool_trail` and no tool in progress
events) is retried automatically, twice at most, each retry consuming an admission. Anything
that may have touched the workspace waits for the parent.

Ctrl+C pauses: running work becomes `interrupted`; pending work stays pending; the run stays
editable. `workflow_plan(action='cancel')` still ends a run. The pause is recorded before the
engine waits for its children to be torn down, so a second Ctrl+C during that wait cannot
leave the run marked running or leave stale members behind for the next execution. A failure
while recording one child's result fails that task only; it never drains its siblings.

The summary of a failed child is model prose once the child has talked to a model, so it is
classified only when the envelope shows no model reply (no usage, no turns, no stop reason) —
a startup or spawn failure whose summary is stderr. Otherwise only `error` is classified.

### Acceptance

`access: write` declares a task that must change files. Before any spawn, a write task whose
profile has no `edit`, `write` or `bash` tool under the parent's live grant refuses the run.
After the run, a write task whose child ran in `plan` posture fails as `read_only`. The
posture itself is decided by the host's consent, so this check is necessarily after the fact.

### Declarative dynamic steps

Generated-script execution stays deferred (ADR-0001). These declarative fields cover the
common data-driven patterns, are validated before anything runs, and run inside the engine
without a parent turn:

- `output_schema`: a JSON Schema subset. The child's final `json` fence is parsed from the
  uncapped `details` when the summary was truncated, then validated. Only the last fence counts.
- `for_each` + `where` + `max_items` (≤16): one item task (`T#n`) per selected array element of
  a dependency's structured result. Placeholders (`{{item.x}}`, `{{index}}`) are substituted
  once; agent-authored values are never re-expanded. Too many items fails the group with a
  clear note rather than silently dropping work.
- `when`: a false condition skips a task as *bypassed*, which satisfies successors. A manual
  skip still blocks them.
- `kind: check`: an argv command (no shell) judged by its exit code. The user approves each
  distinct command once per workflow run, before any child starts, so the dialog never
  competes with the host's spawn-consent modal. Headless sessions cannot approve, so checks
  decline. The approval is bound to the command line, not to the code it executes: write
  tasks in the same workflow can change that code, and repair rounds run it again without
  asking. The dialog says so and names those writers. This is informed consent, not
  containment; a check runs with the user's own permissions.
- `repair` on a check: a failed check reruns an upstream agent task with the failure output,
  up to `max_rounds` (≤3). Results built on the old target (succeeded or pending work,
  condition skips, failed checks) rerun. Declined, manually skipped, interrupted and failed
  agent work is left for the parent: re-running it would override a decision or repeat side
  effects. Declines never repair.

Paths in `for_each`, `where`, `when` and placeholders are checked against the declared schemas
at plan time, so a typo is a refusal rather than a silently false condition.

### Write scopes

`writes` lists project-relative globs. Overlapping writers, writers without a declared scope,
and checks never run concurrently. For declared scopes in a trusted project, `git status`
snapshots before and after the task record changed paths outside the union of the scopes that
ran alongside it. This is evidence, not a failure, because concurrent read tasks can share
blame. Git runs with `core.fsmonitor=false`, `--no-optional-locks` and without submodules, so
it never takes `index.lock` under running children or runs a repository-configured monitor.
In an untrusted project it does not run at all, because a repository's own configuration can
still name filters that `git status` executes. Porcelain paths are mapped from the repository
root to the session directory; changes outside it appear as `:/path` and never match a scope.

### Prompts and reports

A child prompt is at most 60,000 UTF-8 bytes (the host's ceiling is 65,536, and its public
spawn path does not check it). The task's own text, item prompts and `writes` entries are
bounded in bytes at plan time; dependency evidence shares the remaining bytes (water-filled)
and is cut at character boundaries, and the assembled prompt is clipped to the ceiling as a
final guarantee. `workflow_run` reports only attempts that finished in that call;
`workflow_status(run_id, task_id)` shows one task in full.

### Persistence and recovery

Snapshots are schema version 2 and version 1 snapshots migrate. Within a version the field set
is strict; any future field bumps the version with a migration. Full snapshots are written at
plan changes, execution start and end, dynamic steps and attempt completion — not at admission.
When a child ID is first known, a small `aelix-workflow.attempt` record is appended, and every
child prompt starts with `[aelix-workflow run=… task=… attempt=…]`.

Each for_each expansion has a generation number. Re-expanded items reuse their IDs (`verify#1`)
and start their attempts at 1 again, so item markers and attempt records also name the
expansion, and only the current expansion's records apply. A receipt from a newer expansion
than the restored snapshot knows is reported on its group, never adopted. Run reports refer to
attempt objects, not IDs, and label results from a replaced expansion.

On session start, attempt records after a run's latest snapshot mark started work as
interrupted. The host writes an `aelix.child_session` start record whose task preview keeps the
marker before the child process exists, so a crash that loses the workflow's queued record
still finds the child. A settled success whose answer can be read from the child's own session
file (only within the parent's directory) is recovered instead of re-run; an unsettled child is
reported with the tool calls its file shows. Live children are not reconnected: there is no
channel to reattach to, and `pdeathsig` is Linux-only.

### Limits

64 tasks including items, 128 attempts per run (retired fan-out attempts included), 16 items per
fan-out, 16 admissions per user prompt, 1–1,800 seconds per task (the `agent` tool's ceiling).

## Review

An independent review of this change reported twelve defects, each reproduced by a probe:
stale adoption of a re-expanded item's receipt, a second Ctrl+C corrupting the engine, repair
resurrecting declined/skipped/interrupted work, `git status` running repository hooks before
consent and taking `index.lock`, command approval bound only to the command line, deep JSON
aborting the run, model prose classified as a blocker, repository-root paths in a subdirectory
session, run reports mixing expansions, a case-folding offset in result extraction,
`stop_on_failure` ignoring failed graph steps, and an overrun of the prompt ceiling. All are
fixed with regression tests; the command-approval finding is addressed by per-run, informed
consent as described above rather than by containment.

## Consequences and follow-ups

- The extension still uses the host's user-typed `runtime.spawn` door. It now halts on
  `aborted` and the draining refusal, but a proper fix belongs in Aelix: an extension delegation
  door that records the tool-call ID and workflow metadata in the spawn receipt, shares the
  model-driven per-prompt budget (the workflow's 16 are separate from the `agent` tool's 12),
  respects a closed door instead of reopening it, and can take one consent for a whole plan
  (`request_spawn_consent_batch` exists privately). This repository does not change the host.
- Background execution needs a session-owned host primitive (ADR-0197 forbids tasks that
  outlive the session) and depends on the pause/halt semantics above.
- Worktree isolation remains open; declared scopes plus serialization cover overlapping edits.
- Run-level cost budgets can build on the recorded per-attempt cost.
