# Implementation verification

Date: 2026-10-08

Initial interactive verification host: Aelix `0.1.0b2`, sibling source checkout initially inspected at
`402a801325dcc335e2b7e1a7e829e9b5ebed93ca`; interactive verification at
`61f03b6718211c5e3ec81617374ff736881dd999`. The runtime source files used by this extension
were unchanged between these revisions. The host's existing untracked work was preserved.

Environment: macOS arm64, CPython 3.14.5, uv-managed project environment, pytest 9.1.1,
pytest-asyncio 1.4.0, Ruff 0.16.10, Pyright 1.1.414.

## Automated checks

`uv run --no-sync pytest -q`: **70 passed** after the activation-mode implementation.
The initial graph/runtime implementation passed 39 tests.

Coverage includes invalid/cyclic/ambiguous graphs, atomic revisions, transitive result
invalidation, dependency failures, explicit retry after interruption, pipeline scheduling,
concurrency/attempt budgets, shared per-prompt admissions, terminal width/CJK/control-character
handling, built wheel contents, and the actual Aelix harness/hook/delegation/session contracts.

The host integration tests exercise guard denial, spawn-consent decline, active-tool narrowing,
project-identity refusal even in a trusted project, real host abort and joined child cleanup,
late TUI updates, real JSONL file reload, branch-local restoration, and corrupt-latest-snapshot
handling. Their child **transport is simulated**. These tests do not claim live provider coverage.
The slash-command regression verifies that `/workflow <goal>` starts an actual managed host
turn at idle and does not enqueue the same request again for a later turn.

Mode coverage includes default off, positive English/Korean requests, topic questions and
negative requests, blocked model calls without any graph/child creation, request-scoped opt-in,
mode-on ordinary requests, self-opt-in through tool arguments being refused, live footer
updates, late UI binding before the first request, mode-change timing, retained opt-in through
per-run settlement/retry boundaries, and actual JSONL file/branch restoration of mode records.

`uv run --no-sync ruff check .`, `uv run --no-sync ruff format --check .`, and
`uv run --no-sync pyright`: passed, with zero type errors/warnings.

`uv build`: wheel and source distribution built. Installed the wheel into this project's own
environment with `uv pip install --python .venv/bin/python --no-deps --reinstall dist/aelix_workflow-0.1.0-py3-none-any.whl`.
`.venv/bin/aelix extension verify aelix-workflow`: **all 1 endpoint(s) BOUND**.
The global Aelix installation was not changed.

## Python version and OS matrix

The extension is now published to the **private** repository
[`handochan/aelix-workflow`](https://github.com/handochan/aelix-workflow).

Local macOS arm64 checks against host revision
`28dbcaf4a1a9a97e46d88cec4b90c1ddeb486321` passed **70 tests each** on CPython
3.11.15, 3.12.13 and 3.13.13, using separate environments and `uv run --locked --extra tui`.
The built wheel also installed, imported from site-packages and bound its one extension
endpoint successfully in all three local environments.

The [CI workflow](../.github/workflows/ci.yml) defines all nine combinations of Python
3.11/3.12/3.13 and `ubuntu-latest`/`macos-latest`/`windows-latest`. Every job runs the
automated checks, builds the wheel and source distribution, installs the wheel, confirms an
import from the installed environment, and invokes the host's extension-binding verifier.
The host is pinned to the revision above and dependencies use `uv sync --locked --extra tui`.
GitHub job results are the evidence for OS coverage; merely defining the matrix is not a pass.

GitHub Actions run [37714200372](https://github.com/handochan/aelix-workflow/actions/runs/37714200372)
at extension commit `84d98fe702914e39ab08b5fea662b2bf258c2e94` completed with **all nine jobs
successful**. Downloaded and inspected every job's JUnit report: each contains 70 tests with
zero failures, errors or skips.

| Python | Linux | macOS | Windows |
| --- | --- | --- | --- |
| 3.11 | 70 passed | 70 passed | 70 passed |
| 3.12 | 70 passed | 70 passed | 70 passed |
| 3.13 | 70 passed | 70 passed | 70 passed |

Lint, formatting, Pyright, wheel/source-distribution builds, isolated installed-wheel imports
and `aelix extension verify aelix-workflow` also passed in every job. Local copies of the
reports and run metadata are retained under ignored `artifacts/ci-37714200372*`; the run link
above is the shared verification record. Documentation-only pushes do not rerun this matrix;
source, tests, scripts, dependencies and workflow changes do.

These are model-free checks. Interactive/live-provider evidence below was obtained on macOS;
it does not establish live model or actual terminal coverage on Windows/Linux.

## Real terminal verification

Started the actual Aelix TUI in an isolated tmux socket at 100 columns × 32 rows, then repeated
the final panel check at 80 columns × 32 rows. Loaded
`examples/tui_demo.py` and invoked `/workflow-demo`. **Only the child transport was simulated**;
the extension controller, scheduler, UI bindings, terminal rendering and command dispatch were real.

Captured these states in `artifacts/` (the retained simulation captures use the final 80-column layout):

- `tui-running.txt`: two independent tasks executing; successor waiting for `routes`.
- `tui-pipeline.txt`: verification already executing while unrelated session inspection continues.
- `tui-replan.txt`: revision 2 and a newly added regression-check task executing.
- `tui-completed.txt`: four successful tasks and a retained completed-state panel.

Observed panel excerpts:

```text
Dynamic workflow | running | r1 | SIMULATION: inspect → verify → replan from …
> routes #1 | running: read | 0.8s | 840 ctx tokens | [Inspect] Inspect route…
> sessions #1 | running: read | 0.0s | 420 ctx tokens | [Inspect] Inspect ses…
o verify | pending: waiting for routes | [Verify] Verify the finding

Dynamic workflow | running | r2 | SIMULATION: inspect → verify → replan from …
> regression #1 | running: read | 0.8s | 840 ctx tokens | [Replan] Check nega…
Replan: Verification reported a missing negative-case check
```

After installing the wheel, launched a second actual TUI at 80 columns × 32 rows with
OpenRouter `anthropic/claude-haiku-4.5`. Invoked `/workflow` with a single read-only README
task using the existing `general` profile. **Both parent and child used the live model**.
Observed `running → awaiting_review → completed` and the returned tool names. Captures are
`artifacts/tui-live-0.txt` through `tui-live-5.txt`. `/workflow hide` removed the panel;
`/workflow show` restored its retained completed state; `/workflow status` displayed the result
and actual child ID. Captures are `tui-live-hide.txt`, `tui-live-show.txt`, and `tui-live-status.txt`.

The final panel places state/current tool before long titles, so a task's current operation is
visible at 80 columns. A regression covers the long-title case.

## Activation-mode terminal verification

With the new implementation, a cold TUI at 100 columns × 32 rows showed `Workflow mode: off`
before any command or model request. The `ui:list-modules` probe rendered the initial footer
after the host bound its real TUI. `/workflow mode on`, `/workflow mode off`, and
`/workflow mode` updated/displayed the mode without a model turn. The retained cold-start
capture is `artifacts/mode-tui-default-final.txt`.

On was then enabled through `/workflow mode on`. A subsequent plain request to inspect two
documents with independent readers and verification contained no workflow command or execution
cue. The live Haiku parent selected `workflow_plan`/`workflow_run` itself, created the graph,
and started two independent readers. The TUI showed `mode on | running`, then
`mode on | needs_attention`. Captures are `artifacts/mode-on-payload-cap-*.txt`.

The child provider requests returned HTTP 402 because the model's catalog output limit was
64,000 tokens and the account could reserve fewer tokens. This confirms proactive selection,
dispatch and failure visibility, **not successful live child completion for this mode check**.
`examples/bounded_request.py` caps only parent request payloads at 2,048 output tokens, without
changing user settings; delegated profiles with extension inheritance disabled do not load it.
An earlier `before_provider_request` option patch did not carry `max_tokens` to this host's
provider options, so the helper uses the public `before_provider_payload` patch instead.

After switching off, a plain README question was answered without creating a new workflow;
the selected failed graph's ID/state remained unchanged. Captures are
`artifacts/mode-off-plain-*.txt`. A separate bounded `openai/gpt-4o-mini` read-only check answered
the document comparison directly with mode on, consistent with model judgment rather than
forcing a workflow on every request (`artifacts/mode-on-mini-*.txt`).

The final guidance treats auth/credit/model-availability refusals as unchanged external
blockers: use ordinary allowed tools where possible and report delegated checks as unverified,
instead of launching another unchanged workflow. No global model, credential, or permission
settings were changed.

## Successful retry after the user recharged credits

After the user confirmed the OpenRouter recharge, repeated the live TUI check with
`anthropic/claude-haiku-4.5`, the existing `general` profile, read-only tool grants and the
parent response-cap helper. No recharge/payment action was performed by the agent.

A small document comparison was handled directly plus a native `agent` verification call.
The subsequent substantive request asked for three independent code-review specialists and
a separate verifier, without an explicit workflow invocation or execution cue. With mode on,
the parent selected `workflow_plan` and `workflow_run` itself.

Run `487a20532890` completed:

- `activation_lifetime`: specialist review of activation and request lifetime.
- `scheduling_cancellation`: specialist review of scheduling and cancellation.
- `tui_persistence`: specialist review of UI and session persistence.
- `verify_claims`: independent verifier, dependent on all three specialist results.

The first three live children ran concurrently. All four returned `ok`; the run progressed
through `ready → running → awaiting_review → completed`. The parent inspected the results
through `workflow_status` and explicitly called `workflow_plan(action=finish)`. The TUI retained
`mode on | completed` and `4/4 succeeded`. There was no HTTP 402 in this successful run.

Evidence: `artifacts/recharged-specialists-*.txt`, `artifacts/recharged-completion-0.txt`, and
`artifacts/recharged-workflow-full-status.txt`. The child summaries were inspected through the
real status command. These are live model/transport evidence; the reviewers' prose is not a
replacement for the 70 automated tests or a claim of exhaustive correctness.

This successful retry supersedes the earlier lack of live child-completion evidence for mode on.
The earlier credit-limit failures above are retained as historical verification findings.

## Live model verification

Command:

```sh
.venv/bin/python examples/live_smoke.py --profile general \
  --provider openrouter --model anthropic/claude-haiku-4.5
```

Passed through the actual CLI, parent provider, workflow tools, delegated child process,
child provider and returned result. `workflow_plan`, `workflow_run`, and the final
`workflow_plan(action=finish)` returned without errors; the workflow reached `completed`.
The actual child result contained `WORKFLOW_CHILD_LIVE_OK`; the parent's final assistant
message contained `WORKFLOW_PARENT_LIVE_OK`. The parent also read the README to check the facts.

The check gives the parent only `read`, `agent`, and the three workflow tools. The child
receives only the intersection with built-in read tools. The model task made no project/user
file changes; the local test runner writes the evidence artifacts.
The parent runs in `--no-session` mode; workflow persistence is tested separately with real
JSONL storage. Detailed live output is in `artifacts/live-smoke.jsonl` and
`artifacts/live-smoke-summary.json`.

The smoke verifier reads assistant/tool-result events, rather than treating a sentinel echoed
in the user's prompt or CLI exit code 0 as success.

## Bounded findings and remaining scope

- The initial `z-ai/glm-5.3-flash` delegated child returned a provider 400 saying reasoning was
  mandatory and could not be disabled. The host at that check did not carry the parent's thinking
  setting through this public spawn path. Retested on 2026-10-14 against host `64b82af7`:
  `glm-5.3-flash` children with high thinking ran normally (see the live model check below).
- The model catalog listed `gpt-5.4` and `gpt-5.4-mini`, but this account's Codex OAuth endpoint
  rejected both as unsupported. Neither is claimed as a passing live provider check.
- A file-path profile was correctly refused: delegation resolves registered profile names only.
- No release upload, real model parallel benchmark, write-conflict isolation, background
  execution, generated scripts, peer mailboxes, or automatic
  recovery of external side effects was verified or implemented.

The live model and transport-simulation checks establish distinct, bounded evidence. They do
not establish full feature parity with Claude Code or Deep Agents' interpreter orchestration.

## Dynamic steps, failure semantics and recovery (2026-10-14)

Scope: [ADR-0003](decisions/0003-dynamic-steps-failure-semantics-and-recovery.md). Host source
checkout at `64b82af7`; CI still pins `28dbcaf4`, and every host API used here exists there.

### Defects reproduced before the change

Scratch probes ran against `c327f40` (extension engine/model with fake delegates; the stop probe
used the real host runtime through the test bench, with only the child transport faked):

| # | Before | After |
| --- | --- | --- |
| 1 | After `stop_all`, tasks `b`, `c` still spawned and the host door went from closed to open | Only `a` spawned; the door stays closed; `a` interrupted, `b`/`c` pending |
| 2 | Identical HTTP 402 on every child launched 6/6 tasks | 2/6 (the first concurrent wave); the run halts as a blocker |
| 3 | Write task run in `plan` mode: `succeeded`, no posture recorded | `failed[read_only]`; posture, dropped tools and tool trail recorded |
| 4 | Last persisted attempt while a child ran: `child_id: None` | An attempt record with the child ID is written as soon as the child starts |
| 5 | Two Korean dependency results: successor failed on the byte budget, deterministically | Succeeds; six such results are cut at character boundaries within 60,000 bytes |
| 6 | A 40-task graph stopped at 32 attempts and could not finish | All 40 succeed; a 64-task graph fits the 128-attempt budget |
| 7 | Ctrl+C: whole run `cancelled`, unstarted tasks flagged for side-effect review, add/revise refused | Running task interrupted, others pending, run `needs_attention` and editable |
| 8 | 10 tasks × 3 attempts: 121,200 characters resent by run/status | Overview 2,117; a run report with two new results 3,146 |
| 9 | One added attempt field made restore fail | Schema 2 restores, schema 1 migrates; within a version unknown fields are refused |

### Automated checks

`uv run --no-sync pytest -q`: **250 passed**: 239 at `a0ded91`, plus eleven regression tests
for the live check's findings and their review below. There were 70 before this change; three of those were
updated for the deliberate changes above (Ctrl+C pauses, and unknown or project-scoped profiles
are now refused while the plan is written). Ruff lint/format and Pyright: clean.

At `a0ded91` the suite also passed (**238**, all but the packaging test) on **CPython 3.11.15
against the CI-pinned host `28dbcaf4`**, installed from a `git archive` of that revision into a scratch
environment. The packaging test builds with the project's own uv environment and ran in the
main environment. `uv build` then `scripts/verify_wheel.py`: the wheel contains every new
module, imports from site-packages, and `aelix extension verify` reports the endpoint BOUND.

New coverage includes the JSON Schema subset, conditions, templates and result extraction;
failure classification; scope overlap and git observation in a real repository; for_each,
when, repair, pause and cancel at the graph level; engine halting, bounded side-effect-free
retries, read-only and structured-output acceptance, fan-out aggregation, write-scope
serialization, the byte budget, delta reports; schema 1 migration and schema 2 strictness;
recovery from real session files (settled success, marker-only receipts, failures,
cancellations, unreadable answers, files outside the parent directory). Host-level tests use
the real harness, delegation runtime and JSONL sessions with the host's receipt wiring:
`stop_all` halting, one-time command approval, denied and headless checks, the write-tool
preflight, read-only children, task detail and delta reports, crash-time restoration with and
without the workflow's own attempt record, and `/workflow continue`.

### Independent review

A separate reviewer read the whole change against the host source and reported twelve defects,
each with a probe that reproduced it. Every probe was re-run here before and after the fix:

| Finding | Before | After |
| --- | --- | --- |
| Restart adopted an earlier expansion's receipt for a re-expanded item | new `verify#1` became `succeeded` with the old child's answer | item stays pending; expansions are numbered and matched |
| Second Ctrl+C during child teardown | run stayed `running`, stale members made the next run cancel itself | run `needs_attention`, no members left, next run completes |
| Repair re-ran declined, skipped and interrupted work | `deploy` declined → spawned again; `risky`, `migrate` ran | only stale results and failed checks rerun |
| `git status` before consent ran a repository `core.fsmonitor` | hook executed | `core.fsmonitor=false`, lock-free, trusted projects only |
| Check approval bound to the command line only | edited code ran with no prompt | approval per workflow; the dialog names the writers that can change the code (residual risk, documented) |
| 100,000-deep JSON from one child | `RecursionError` drained the run, sibling interrupted | that task fails as `invalid_output`, sibling succeeds |
| Failed child's prose about HTTP 401 | halted the run as a blocker | prose after a model reply is `error`; stderr-only startup failures are still blockers |
| Session in a repository subdirectory | in-scope edit reported, dirty out-of-scope edit missed | paths mapped from the repository root |
| Re-expanded group in one run's report | one result shown twice, two lost | all attempts shown; replaced ones labeled |
| `İ` before the result block | remainder corrupted or valid result refused | located without case folding |
| `stop_on_failure` and a failed fan-out | the next task still launched | the run halts |
| Worst-case child prompt | 65,764 bytes | ≤ 60,000 bytes; `writes` bounded in bytes |

The review's test-quality notes were addressed too: the approval test now asserts that the
approved command is absent from the second dialog, an engine-level test proves a check never
overlaps a writer, and the failure tests assert the corrected classification. A tampered
snapshot whose fan-out claimed success without items is now refused.

### Real terminal simulation

Started the actual Aelix TUI (`aelix --offline --no-session -e examples/tui_demo.py`) in an
isolated tmux server at 100 × 32 and invoked `/workflow-demo`. Only child transport and the
check result were simulated. Captures `artifacts/tui-dynamic-*.txt` show:

```text
> verify | running: 0/3 items, 3 running | [Verify] Verify {{item.file}}
↳ > verify#1 #1 | running: read | 0.0s | 380 ctx tokens | [Verify] Verify src/auth/session.py
o fix | pending: waiting for verify | [Fix] Fix the confirmed defects

> tests #1 | running: check | 0.0s | [Check] Run the auth tests

> fix #2 | running: edit | 2.1s | 1,520 ctx tokens | [Fix] Fix the confirmed defects
o tests #1 | pending: waiting for fix | repair 1/2 | [Check] Run the auth tests

Workflow d2bfa3c0a0ce | completed | 4/4 succeeded
+ tests #2 | succeeded | repair 1/2 | [Check] Run the auth tests
```

These captures predate the live model check below. A fan-out's own row now masks its
placeholders, so the first line reads `[Verify] Verify …`.

The first capture also exposed two display defects, both fixed before the captures above: a
group showed `pending` for up to one progress interval after its items started, and a repaired
check rendered `succeeded: succeeded`.

### GitHub Actions

Commit `a0ded91` on `feat/dynamic-steps-and-recovery` (run `38491347031`, started by
`workflow_dispatch`): **9/9 jobs passed**, Ubuntu, macOS and Windows on Python 3.11, 3.12 and
3.13, against the pinned host `28dbcaf4`.

### Live model check

The real TUI in tmux (130 × 48) ran
`aelix --agents --trust-extension-path aelix-workflow --approve --permission-mode auto-accept-edits`
in a scratch git repository: a `calc` package with three bugs and `unittest` tests. Both parent
and children used the configured default, `z-ai/glm-5.3-flash` on OpenRouter with high
thinking, on host `64b82af7`. Goals were given in Korean through `/workflow`. Every spawn
consent and check approval was answered in the TUI.

| Run | Exercised | Result |
| --- | --- | --- |
| 1 | The parent wrote a plan from a prose description: a survey with `output_schema`, then a `for_each` review (3 items, 2 in parallel), then one `fix` (`access: write`, `writes: calc/**`), then a `kind: check` (`python3 -m unittest -q`) with `repair` | The plan was valid on its first call. 6 attempts, 4/4 succeeded; all three bugs were fixed, only `calc/` changed, and the tests pass. Workflow spend was $0.0094. Prompt markers, expansion numbers and attempt records appeared as designed. The `workflow_run` report was 7,811 characters. |
| 2 | A check that fails first, then a repair round | `release-check` failed (`codename must be 'aurora'`). The failure output reached `bump #2` in its prompt, and the check then passed. 4 attempts, $0.0045. The parent itself pointed out that the repair changed a field the original prompt had said to keep. |
| 3 | A timeout, a parent replan, and a crash | `proofread` hit the old 120 s default. The parent revised it to 600 s and retried. During that retry the Aelix process was killed with `kill -9`. After `aelix --continue`, the attempt was `interrupted` with "Outcome unknown … it ran read, bash", and the child's tool trail was read from its own session file. Then `/workflow continue` led the parent to review, revise and finish, with a third attempt. |
| 4 | Re-check after the fixes below, in a new session | `for_each` over a structured inventory, 3/3 items. No timeout was set, so the host default applied, and the report showed no dropped-tool noise. $0.0025. |

Findings fixed in this change. Each has a regression test, except the schema wording:

- The 120 s default task timeout overrode the profile's limit (Aelix's default is 600 s) and
  timed out a routine proofreading child. An unset `timeout_ms` now leaves an agent's limit to
  its profile, as the `agent` tool does. Checks default to 600 s.
- A child that timed out without output left the note `timeout: ` with nothing after it. The note
  now explains the timeout and suggests raising `timeout_ms` or narrowing the task.
- Every attempt listed the parent's extension tools (`workflow_*`, `agent`, `aelix_status`) as
  unavailable to the child. A profile that inherits the parent's tools "requests" all of them,
  and no child can load extension or MCP tools, so that list is now cleared. A profile that
  names its tools still sees each requested tool its child did not get.
- A fan-out's own row showed raw `{{…}}` placeholders, and items under a fixed title could not
  be told apart. Group titles now mask placeholders, and items under a fixed title get a short
  label from their item.
- The parent model wondered in its reasoning how to depend on a fan-out. The schema now states
  that depending on a `for_each` task waits for all of its items, and that titles accept
  placeholders.
- After the crash, the parent blamed the unsettled attempt on a blocked `bash` call. The note now
  says that Aelix stopped before the child settled. Recovered tool trails use the host's
  argument format, each attempt in `workflow_status` shows its own outcome symbol, and an empty
  result is no longer printed.

A second independent review of these fixes found three more defects, each reproduced and fixed
with a test:

- `overview()` raised `IndexError` for a succeeded task whose answer was only whitespace,
  breaking `workflow_status` and every `workflow_plan` action. The bug predates this change.
- Version 1 snapshots wrote the old 120 s default into every task, so restored runs kept it.
  Migration now drops that value, and the limit defers to the profile.
- The first fix for the dropped-tool noise also hid tools that a profile had named explicitly.

Host observations that this extension cannot fix (ADR-0003, follow-ups):

- Under an `auto-accept-edits` parent, read-only tasks also run with write authority, and each
  one shows the write-consent dialog. The public spawn door has no posture ceiling.
- Every child takes its own consent, and the dialog accepts Enter only after the whole task has
  been scrolled. A 3-item fan-out meant three dialogs.
- The panel showed `delegating` for almost the whole run. The host sets a child's current tool
  only between a tool start and the next turn, so a short tool is rarely visible.

Total model spend for the check was under $0.08, as reported by the host. Panel logs, dialogs
and scrollback are in `artifacts/live-workflow-2026-10-14/` (local, not committed).

### Not verified here

Background execution, worktree isolation, run-level cost budgets and the host-side delegation
door remain open (ADR-0003, follow-ups). The live check used a single provider and model.
