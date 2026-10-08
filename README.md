# Aelix Workflow

An Aelix extension for adaptive task decomposition, agent delegation, result-based replanning,
and live progress in the TUI. The parent agent builds a task graph, executes it, examines the
results, and changes the plan when new evidence calls for it.

The first version uses a validated task graph instead of executing generated Python or
JavaScript. Independent ready tasks run concurrently. A successor starts as soon as its own
dependencies succeed; it does not wait for unrelated work. After execution, the parent can add
new investigations, revise invalid assumptions, retry an interrupted task, and add verification.

## Develop and run

This checkout uses the sibling `../../aelix-ai` source for development. Build a wheel for use
in another environment; the published dependency metadata contains no local paths.

Use this layout when cloning into a new workspace (the extension repository is private):

```text
workspace/
  aelix-ai/
  extensions/
    aelix-workflow/
```

```sh
uv sync --extra tui
uv run aelix --agents --trust-extension-path aelix-workflow
```

The development command explicitly trusts this editable extension's path. A wheel installed
into Aelix's environment is discovered through its installed entry point. Existing user/global Aelix agent
profiles are required for delegation. In the TUI:

```text
/workflow inspect this repository, identify the highest-impact defect, fix it, and verify the fix
/workflow status
/workflow hide
/workflow show
/workflow mode
/workflow mode on
/workflow mode off
```

## Workflow activation

The default is **mode off**. Off controls automatic workflow selection; explicit workflows remain
available through `/workflow <goal>` or a current request such as "use a workflow" or
"워크플로로 진행해주세요". The model may use a workflow only for that request. A question
about workflows, a filename containing `workflow`, earlier conversation text, a dependency
result or the workflow tool's own arguments do not opt in. The request detector recognizes
English/Korean execution cues; `/workflow` is the unambiguous entry for other phrasing.

**Mode on** asks the model to actively judge when orchestration benefits the task and prefer it
for substantive work, independent investigations, multi-file implementation and independent
verification. It still handles straightforward questions and one-step work directly.

| Command | Behavior |
| --- | --- |
| `/workflow mode` | Show the current mode and its meaning |
| `/workflow mode on` | Enable proactive workflow selection |
| `/workflow mode off` | Require an explicit current workflow request |

The footer displays `Workflow mode: on/off` even before a run exists. Running panels also show
the configured mode. Changing mode does not launch a model turn or cancel an in-flight
workflow; it applies to the next request. Off is enforced by the tool-call hook and the actual
handlers as well as the system prompt. It does not change the host's permission mode, tool
grant, agent availability or execution limits.

Mode changes are recorded in the **current session** using the host's asynchronous session
append API. `/resume`, `/reload` and `/fork` restore the preference on the selected branch.
`/new` starts with the default off; `--no-session` keeps the preference only in memory.

The panel above the editor shows the goal, revision, workflow state, task states, dependency
waits, current child tool, elapsed time and context tokens. Running and failed tasks appear first.
`/workflow status` includes all tasks and their attempt history. Ctrl+C interrupts execution.

The host's permission mode and each profile's `approval_mode` determine write authority.
A default-mode parent with an inherited profile produces read-only children. For implementation
tasks, use a profile that declares the required write authority and approve it through Aelix's
existing spawn-consent dialog.

## Install the wheel

For an installed Aelix environment, install the wheel into **the same environment as Aelix**:

```sh
uv build
uv pip install --python /path/to/aelix-environment/bin/python dist/aelix_workflow-0.1.0-py3-none-any.whl
aelix extension verify aelix-workflow
```

After installing a wheel into a uv project's environment, use `--no-sync` to keep that
installation when launching from the project:

```sh
uv run --no-sync aelix --agents
```

## Tools

| Tool | Purpose |
| --- | --- |
| `workflow_plan` | `create`, `add`, `revise`, `retry`, `skip`, `cancel`, `finish` |
| `workflow_run` | Execute the ready graph and return bounded results for the next decision |
| `workflow_status` | Inspect state, waiting reasons, outputs and prior attempts |

Example plan (use profile names that actually exist in your Aelix installation):

```json
{
  "action": "create",
  "goal": "Review the authentication boundary and verify the findings",
  "max_concurrency": 2,
  "tasks": [
    {"id": "routes", "title": "Review route guards", "profile": "reviewer", "phase": "Review", "prompt": "Inspect route authorization checks. Return paths and concrete evidence."},
    {"id": "sessions", "title": "Review session checks", "profile": "reviewer", "phase": "Review", "prompt": "Inspect session validation. Return paths and concrete evidence."},
    {"id": "verify", "title": "Verify reported defects", "profile": "reviewer", "phase": "Verify", "depends_on": ["routes", "sessions"], "prompt": "Independently reproduce each alleged defect in the dependency summaries. Separate verified findings from unverified claims."}
  ]
}
```

Pass the returned `run_id` to `workflow_run`. Once it returns, inspect its results before adding
or revising tasks. `finish` requires a conclusion and refuses graphs with unresolved work.
An agent's successful return is execution evidence; it does not prove all acceptance checks passed.

## Execution boundaries

- 1–4 concurrent tasks, at most 64 graph nodes, 32 attempts per run, and 16 admissions per user
  prompt shared across all workflows. Timeouts are 1–600 seconds per child.
- Every child uses the host's public consent-taking `SubagentRuntime.spawn` contract. Profiles
  resolve with `allow_project=False`; no private grants, subprocess launcher, or permission
  widening is introduced. The parent must have an active `agent` tool.
- The public spawn contract currently has no model-tool receipt argument and does not charge
  the built-in `agent` tool's per-prompt counter. This extension applies its own 16-admission
  counter and stores the task-to-child association in its workflow snapshots. Consent is taken
  per child by the host, rather than once for a whole workflow.
- Failed tasks do not unblock their successors or retry themselves. Revisions invalidate
  every dependent result while retaining prior attempt evidence.
- Graph/attempt snapshots use `aelix-workflow.snapshot` session entries. Aelix queues these
  writes; persistence follows the host's session flush behavior. `--no-session` is volatile.
  Resume and fork use only the current session branch. Restored running tasks become interrupted
  and need an explicit retry after checking possible side effects.
- Execution is foreground; live TUI updates continue while the tool is awaited. Background
  runs, generated-script execution, peer messaging, process resurrection, and isolated write
  worktrees remain later work. Parallel children share the host working directory; assign
  disjoint write ownership or use concurrency 1 for overlapping edits.

## Verification

```sh
uv run pytest
uv run ruff check .
uv run ruff format --check .
uv run pyright
uv build
```

[GitHub Actions](https://github.com/handochan/aelix-workflow/actions/workflows/ci.yml) runs the
same checks across **Python 3.11, 3.12 and 3.13 on Linux, macOS and Windows** (nine jobs).
Every job runs the tests with TUI dependencies, lint, formatting and type checks, builds both
distributions, then replaces the editable extension with its wheel and verifies the installed
entry point through `aelix extension verify`. JUnit results are retained as run artifacts.
CI pins the Aelix host revision; it does not test every host version in the dependency range.
Provider calls and interactive terminal sessions are separate, explicitly run checks.

For the real-TUI transport simulation, start Aelix with `-e examples/tui_demo.py` and invoke
`/workflow-demo`. This makes no model calls. For a small live check using a registered profile:

```sh
uv run python examples/live_smoke.py --profile general
```

Replace `general` with an existing user/global profile name. Optional `--provider` and `--model`
arguments select a provider explicitly; otherwise the check uses your current Aelix default.

See [the reference investigation](docs/references.md), [the design decision](docs/decisions/0001-adaptive-graph-and-host-delegation.md),
and [verification evidence](docs/verification.md).
