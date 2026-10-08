# Implementation verification

Date: 2026-10-08

Host: Aelix `0.1.0b2`, sibling source checkout initially inspected at
`402a801325dcc335e2b7e1a7e829e9b5ebed93ca`; final verification at
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

- The default `z-ai/glm-5.3-flash` delegated child returned a provider 400 saying reasoning was
  mandatory and could not be disabled. The current host does not carry the parent's thinking
  setting through this public spawn path. This provider/profile combination is **not validated**.
- The model catalog listed `gpt-5.4` and `gpt-5.4-mini`, but this account's Codex OAuth endpoint
  rejected both as unsupported. Neither is claimed as a passing live provider check.
- A file-path profile was correctly refused: delegation resolves registered profile names only.
- No GitHub CI, remote repository publication, release upload, real model parallel benchmark,
  write-conflict isolation, background execution, generated scripts, peer mailboxes, or automatic
  recovery of external side effects was verified or implemented.

The live model and transport-simulation checks establish distinct, bounded evidence. They do
not establish full feature parity with Claude Code or Deep Agents' interpreter orchestration.
