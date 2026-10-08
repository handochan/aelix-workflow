# Dynamic workflow references

Investigated on 2026-10-08. Scope: adaptive decomposition, delegated execution, result-based
replanning, progress visibility, and Aelix extension integration. This is an implementation
comparison, not a performance benchmark.

| Reference | Useful behavior | Application to Aelix Workflow |
| --- | --- | --- |
| [Claude Code dynamic workflows](https://code.claude.com/docs/en/workflows) | A runtime executes an authored script. `agent`, `parallel`, `pipeline`, `phase`, and `log` coordinate work outside the main conversation; a progress view exposes the run. | Keep execution state outside parent conversation text; expose phase/task state in the TUI; run independent work concurrently and pass bounded dependency results forward. |
| [Claude Code agent teams](https://code.claude.com/docs/en/agent-teams) | A lead coordinates a dependency-aware task list; blocked work cannot be claimed, and tasks/agents are visible in the terminal. | Stable task IDs, explicit dependencies and waiting reasons, with failures blocking successors. Peer mailboxes are deferred. |
| [Deep Agents subagents](https://docs.langchain.com/oss/python/deepagents/subagents) | A supervisor delegates self-contained jobs to agents with isolated contexts and receives concise results. | Give each child its own scoped prompt and existing host profile. Feed summaries, checks and remaining gaps back into the next planning decision. |
| [Deep Agents overview](https://docs.langchain.com/oss/python/deepagents/overview) | Built on LangChain/LangGraph with context management, streaming and delegation. Planning is an optional middleware capability in the current docs. | Planning/execution/UI are separate components. Do not describe planning middleware as a durable execution ledger or replace Aelix's kernel merely to obtain a task list. |
| [Deep Agents dynamic subagents](https://docs.langchain.com/oss/python/deepagents/dynamic-subagents) | Interpreter code dispatches configured subagents with JavaScript loops, branches and parallel batches. The interpreter API is currently beta. | A direct reference for a future code-based orchestration layer; keep child execution behind the same delegation contract used by the initial task-graph scheduler. |
| [Pi's official subagent example](https://github.com/earendil-works/pi/tree/main/packages/coding-agent/examples/extensions/subagent) | Single delegation, parallel work, and sequential chains are implemented as an extension with isolated child processes. | Reuse Aelix's existing delegation extension and public runtime seam instead of adding another process launcher. |
| [milanglacier/pi-dynamic-workflow](https://github.com/milanglacier/pi-dynamic-workflow) | Generated JS orchestration, concurrency limits, phase/log progress, saved workflows and a replay journal. | Bound concurrency and attempts; preserve work evidence; explicitly distinguish result replay from resuming a live child process. |
| [Michaelliv/pi-dynamic-workflows](https://github.com/Michaelliv/pi-dynamic-workflows) | Pi-oriented workflow orchestration with a live workflow surface. | A close ecosystem reference for future script authoring and richer progress navigation. |

## Important distinction

Claude Code's current dynamic workflow is a script runtime, not simply a fixed sequence such
as plan → implement → verify. Its script decides what runs next and holds intermediate results.
The initial Aelix implementation deliberately starts with a mutable task graph: the scheduler
handles dependencies and parallel execution, and the parent agent revises the graph from the
returned evidence. It provides adaptive orchestration without executing generated code.
Generated-script execution and background runs are not implemented yet.

Deep Agents provides useful supervisor/subagent and context-management patterns. It does not
require making LangGraph a dependency of this extension. A saved transcript/checkpoint does
not by itself make interrupted external side effects safe to replay.
Its current dynamic-subagents feature also coordinates from interpreter code, so this version's
mutable graph is an initial subset of the code-orchestration behavior in both references.

## Current Aelix integration evidence

Host inspected: `/Users/handochan/dev/aelix-ai`, commit
`402a801325dcc335e2b7e1a7e829e9b5ebed93ca`, package version `0.1.0b2`.
At final verification the concurrently maintained checkout was at
`61f03b6718211c5e3ec81617374ff736881dd999`. The intervening changes were CI/release workflows,
private-catalog documentation, changelog and related tests; the delegation/extension/TUI runtime
sources used here were unchanged.

- `extensions/api.py`: `register_tool`, `register_command`, lifecycle hooks, `append_entry`,
  and the late-bound `runtime.ui` / `runtime.subagents` surfaces.
- `extensions/ext_ui.py`, `tui/context.py`: `set_widget` above the editor and `set_status`
  in the footer. The headless UI requires guarding these calls.
- `subagent_contract.py`, `aelix_agents/runtime.py`: public `spawn` takes its own consent.
  Profile resolution is by registered name; model callers cannot opt into project identities.
- `aelix_agents/runtime.py`: the public command-oriented spawn path does not charge the
  built-in agent-tool budget and cannot receive model-tool receipt metadata. Workflow applies
  its own admission limit and records task-to-child links in workflow snapshots.
- Session `get_branch()` and `append_entry`: restore current-branch snapshots, rather than
  interpreting `ToolResult.details` as persisted execution state.

The extension introduces no core imports into Aelix and changes no host repository files.
