# ADR-0004: Following the host's delegation policy and reading what it reports

Status: Accepted

Date: 2026-11-21

## Problem

Version 0.2.0 was built against Aelix `28dbcaf4`. At `83714045` the host had changed beneath it,
and each of the following was reproduced or read from the host's code. This version targets
Aelix at `83714045` or later. On the released beta.2 it is best effort and not covered by the
tests; the host's version string is still `0.1.0b2`, so the dependency floor cannot say so yet.

1. Profiles resolved through `resolve_profile(allow_project=False)`, the human door. The host now
   has a door for delegations a model chose (`ModelDelegation.resolve_for_model`, its ADR-0304),
   which applies the `agents` settings and the session identity's `agents:` list. Under an
   identity that delegates to nobody, `workflow_run` still started a child.
2. The host removed every delegation time limit (its ADR-0276): `spawn(timeout_ms=…)` is
   accepted and never read. The workflow still offered and passed a per-task agent `timeout_ms`.
3. With background jobs on, the host's default, a full agent limit is refused by the job
   service's admission in new words, which classified as `error`. Read as `transient`, the
   refusal was retried after 2 s and 8 s, each retry a new attempt and admission, and then the
   task failed, although children run far longer than ten seconds and the host's sentence says
   "Stop one or wait; do not retry."
4. The host reports what a child did (`session_id`, `changed_files`, `unconfirmed_files`,
   `files_overflow`, `reason`) and what a running child is doing (`approval`, `stuck`,
   `wrapping_up`). The workflow discarded all of it: a child blocked on an approval dialog looked
   hung, and the git observer could not tell concurrent writers apart.
5. A child asked to finish early (a wrap-up) ends `aborted`, which the workflow treated as an
   outside stop: its task became `interrupted` and the whole run halted.

## Decision

### The model's door

The workflow's tasks are delegations the model chose, so every profile resolution, at planning
and again at run, goes through `resolve_for_model` when the runtime has it; its refusal text,
written for the model, reaches the model behind the profile's name. A host whose contract defines
`ModelDelegation` but whose runtime cannot answer refuses, as the contract asks. A host from
before the door, which has no such policy, keeps `resolve_profile(allow_project=False)`.

### No agent time limit

`timeout_ms` belongs to `kind: check` tasks only, whose runner still enforces it. The model picks
no kill timer for a delegated agent on any host, so an agent task that sets one is refused at
plan time. The refusal is true of both hosts: current Aelix has no limit (Ctrl+C or `/workflow pause` stops a child),
and an older one ends a child at its profile's own limit (600 s by default), which only the
profile can raise. A stored snapshot's agent `timeout_ms` is dropped on restore. The `timeout`
kind stays, for checks and for older hosts, and its advice names the profile's limit.

### Capacity is a wait

`failures.capacity` recognises the host's capacity refusal (the job admission's two sentences
and the older live-child cap) only with proof that no child existed, since the attempt is then
tried again for free: `started` false, or, on a host without the field, the silence `classify`
requires (no usage, turns or stop reason) whichever of `error` and `summary` holds the words.
`started` true is never capacity; a child that ran may have quoted the sentence.

Such a refusal costs no further attempt or admission: the same attempt, launched with one
admission, is tried again. While another
delegation of the execution is in flight, the refused task keeps its concurrency slot and waits
for it. One that ran wakes it once its member is settled, so a halt that member causes is seen
first; one that comes back refused too wakes it at once, and both take the delay path together.
With none in flight (something outside the run holds the slots), it waits out the 2 s and 8 s
delays, counted apart from the automatic retries of ordinary transient failures, then records the
refusal as `transient` and is not retried further. A halt ends a wait and a retry delay at once,
this delay and the transient backoff alike. A pause or Ctrl+C while a task waits puts it back to
`pending`, its unstarted attempt removed and its admission refunded (never above the per-prompt
limit), and a removed automatic retry leaves the task's next run an ordinary one: no child
existed, so there is nothing to review. A waiter that wakes while a pause stops the members
unstarts itself whatever reached it first. On Python 3.11 `asyncio.wait_for` returns the inner
result when the delay's halt and the pause's cancel land in the same loop pass, swallowing the
cancel, so the outcome may not depend on that order.

### What the host reports

An attempt keeps `session_id`, `changed_files` and `unconfirmed_files` (64 paths each, as the
host keeps them), `files_overflow` and `reason`. They are written only when set. Snapshots move
to schema 3, which adds these fields, `own_violations` and the `wrapped_up` kind; a version 2
snapshot migrates with no change. Version 0.2.0 refuses a version 3 snapshot ("Unsupported
workflow snapshot version."), which its session start reports as "Could not restore a workflow
snapshot", instead of misreading it. Task detail shows the fields compactly, with "+N more" for
what the host only counted. Nothing tells the model to resume a child: an extension has no
resume door, and a resume outside the graph would leave the task's state behind. The session ID
is marked as a record for people and hosts, after a live model tried to resume it.

A path in a child's own written files that falls outside its task's `writes` is that child's
scope violation, whatever ran alongside and in any project; a read task owns no files. The host
shows a path inside the parent's directory with `/` on every platform, one that climbed out as
`../…`, and an absolute one as written, so only `./` is normalised: a backslash is part of a
POSIX name, and an outside path never matches. The trusted-project git observation stays for
what the host's record cannot see (bash), with its union rule. Both merge into `scope_violations`,
the child's own first (`own_violations` counts them), and reports keep them apart: only git's
entries may be shared with concurrent writers. Every recorded path is cleaned of control
characters before the model can read it.

### A running child's state

`approval`, `stuck` and `wrapping_up` are live task state, never persisted. The panel shows the
approval wait in place of the tool, with the child's ID that the host's approval dialog names
(shortened on a narrow row), and marks stuck and wrapping up; the text is child-authored and
flattened like the tool name. The render throttle's signature includes them.

### A wrap-up is a conclusion

A new kind, `wrapped_up`, comes from the host's own typed verdict: the child-events row that the
optional `controls` member exposes carries `end`, and `"wrapped_up"` there is a consumed wrap-up
on a run that ended by itself (`envelope.child_end`). The envelope alone cannot tell it from a
stop; both are `aborted` with prose reasons. The row is read as `spawn` returns, with no await
between the host's terminal publish and the read, so the host's window of kept ended rows cannot
have dropped it. The kind does not halt and is never retried automatically; the task fails,
keeps the child's last reply as its summary, and its note says the reply is partial and that the
task is not to be retried unless the user asks. It also says who asked: this extension remembers
the children its own `/workflow wrap` asked and then names the user; any other wrap-up came
through the host's controls and is described so, never as a time limit. A live model blamed a
timeout, and later, with the neutral wording, doubted who asked and retried at once.

`/workflow wrap [task_id]` asks the running children of the executing run, never merely the
selected one, to finish through `controls.wrap_up`; Aelix runs an extension's command during a
turn (its ADR-0292), which is when a workflow's children run. While a run executes, `wrap` and
`wrap <one word>` are always this command, and an unknown or idle task ID is answered with the
running ones; with none executing, bare `wrap` says so and longer text stays a goal.

### Pausing a run Ctrl+C cannot reach

Without a time limit, a run in a turn the TUI does not track (see below) could not be stopped
short of quitting. An extension's command does run mid-turn, so `/workflow pause` asks the
executing run to pause. The engine pauses cooperatively: a flag its loop checks where it checks
the abort signal. It then does to the run what Ctrl+C does (members cancelled and awaited, tasks
whose delegation had been asked for interrupted, others back to `pending`, as below) but
returns normally: the owner task, the turn's own, is never cancelled from outside the host's
abort. Only a task whose delegation had been asked for is interrupted: the extension cannot
tell whether its child existed (the consent dialog's window), so it takes the safe side. A task
stopped before that, during the git snapshot at its start, goes back to `pending` like a
slot-waiter, under Ctrl+C as under a pause. A pause that interrupts nothing leaves the run in the
state its graph gives it, and a pause asked for as an execution ends is dropped with it. A halt
settled in the same round, a blocker for instance, keeps its reason beside the pause.

A check that failed with repair rounds left loses its repair when the execution stops in that
round, by a pause, Ctrl+C or a halt. The next execution derives the owed repairs from the run
itself: a check still `failed` with a repairable failure and rounds left. That is safe because
everything that should cancel the repair moves the check: retrying it or revising it or its
target resets it to `pending`, an exhausted check has no rounds left, a declined one has no
repairable failure, and nothing runs when an execution starts, so no repair is deferred. It is
safe only with the approval condition too, since check approvals are per run and never
persisted: an owed check counts as a pending one for everything decided before work starts, so
the approval dialog asks about its command and the preflight resolves its target's profile and
write authority. The repair is applied only when that command is approved for this run (the
extension passes the engine a predicate over its approvals); denied, or with no interactive UI
to ask, it is not applied, no child is spent, and the check's note says the repair waits for
approval. A repair also resets its target's stale dependents inside the execution, after the
dialog is decided, so a check never asked about could run declined though the user never
refused it. When any check is pending or owes a repair, the dialog therefore asks about every
command a check of the run could still need (all but checks skipped by hand); with none, there is
no dialog, as before. Without this, after a restart the repair reran its target and the check was then
declined unasked. Wherever a failed check is reported to the model, the report says a repair is
owed, what the next `workflow_run` will rerun, and that retrying, revising or skipping the check
cancels it.

The `workflow_run` result is not an error; it says the user paused the run with `/workflow
pause`, how many tasks were interrupted, and that the agent should report and wait. Until the
user's next prompt, every `workflow_run` is refused with how the user continues the paused run,
so the agent cannot rebuild the remaining tasks as a new run; planning stays allowed. A turn
that a background job's result starts (`source="notice"`) is not a prompt: like the host's own
agent budget, the latch and the per-prompt budget outlast it. `/workflow continue` also lifts the
latch, since typed mid-turn it arrives as a follow-up rather than a prompt.

## Left to the host

- No public resume door for an extension, so a wrapped-up or interrupted task is retried as a
  new child, never continued.
- Refusals are still prose: capacity is matched by the host's words, and only `started` is typed.
  A typed refusal kind on the envelope would retire `failures`' patterns.
- A wrap-up's typed verdict lives on the host's live row, not in its session records, so a
  wrap-up recovered after a restart reads as `aborted`.
- A turn an extension starts cannot be interrupted from the TUI. `/workflow <goal>` and
  `/workflow continue` start theirs with `send_message(trigger_turn=True)`, which at idle runs
  `loop.create_task(self.prompt(text))` (`aelix_agent_core/harness/core.py:5278`). The TUI marks
  a turn running only around its input loop's own `harness.prompt` (`tui/shell.py:5611`; also
  the notice turn, `:5237`, and commands, `:2788`); after an extension command the loop just
  returns to reading input (`:5419`). Ctrl+C reaches a turn only `if self._running or
  self.interrupt_armed` (`tui/chrome.py:1564`), Esc has the same gate (`:1610`), and the working
  row the same condition (`:1025`, `:1187`). The host does count an extension's turn as busy
  for its `!` lane (`busy_provider`, `chrome.py:836`; `shell_lane.py:329`), not for these.
  `28dbcaf4` is the same (`core.py:4398`, `shell.py:4053`, `chrome.py:945`, `shell.py:3907`).
  No public API starts a turn the way a typed one starts: `send_user_message` only queues one for
  the next turn (`core.py:5294`), and `chrome.submit_line` is not on the extension UI context.
  `/workflow pause` covers the gap for workflows; the fix belongs in the host.
- The earlier host-side follow-ups of ADR-0003 (a receipt-carrying extension door, one consent
  per plan, a shared per-prompt budget) remain open.

The host bench now runs with background jobs both off and on (a real job service), so the
admission path the host ships by default is exercised alongside the older live-child count.
