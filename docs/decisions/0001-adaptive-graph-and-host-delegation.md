# ADR-0001: Mutable task graph over Aelix's delegation contract

Status: Accepted for the initial implementation

Date: 2026-10-08

## Problem

Aelix needs dynamic task decomposition, delegation and replanning, with progress visible in
the TUI. Its bundled agents already own child spawning, live permission/tool narrowing,
consent, timeouts, child sessions and cleanup. Building a second process launcher would create
two execution registries and duplicate these policies.

## Decision

Implement an independent `aelix-workflow` extension, discovered by the `aelix.extensions`
entry point and a packaged `aelix-plugin.toml`. Keep these responsibilities separate:

1. The parent agent creates and amends a validated graph with `workflow_plan`.
2. `WorkflowEngine` admits ready tasks under concurrency and attempt limits. It starts a
   successor when its own dependencies succeed, without a barrier for unrelated work.
3. `workflow_run` uses the public `runtime.subagents` contract. Resolve by registered name
   with `allow_project=False`, require the active `agent` capability, and take spawn consent
   through the host. Never fabricate a grant or call a private `spawn_granted` API.
4. Store graph/attempt snapshots as namespaced session custom entries, including child IDs.
   Read only the current session branch during startup/resume/fork.
5. Render the selected run above the editor and summarize it in the footer. Read the UI
   binding on each update and contain UI errors independently of execution outcomes.

The host's public spawn seam was originally intended for `/agents run`. It takes its own
consent and narrows authority, but does not charge the built-in model-agent counter or accept
tool-call receipt metadata. Therefore, this extension enforces an additional 16-admission
limit per user prompt, shared across its runs; its snapshots associate workflow tasks and child
IDs. A future host API may provide richer orchestration admission/receipt semantics. The first
version must not claim its receipts are indistinguishable from a native `agent` tool call.

## State and outcomes

- Tasks are `pending`, `running`, `succeeded`, `failed`, `interrupted`, or `skipped`.
- A successful agent return is not acceptance verification. Exhausted graphs move to
  `awaiting_review`. Only an explicit `finish` with a conclusion marks the workflow completed.
- Failed or skipped dependencies do not satisfy a successor. No automatic retry occurs.
- A revision invalidates transitive dependent results and retains old attempts as evidence.
- Restore converts running work to interruption. Explicit retry follows review of any side
  effects. Existing completed attempts are not replayed.
- An abort cancels and joins this engine's admitted children. It does not stop unrelated
  delegations in the shared host runtime.

## Initial limits

Foreground execution, 1–4 concurrent tasks, 64 task definitions, 32 attempts per workflow,
16 admissions per user prompt, 1–600-second child timeout, and bounded result summaries.
The host may apply stricter limits. Parallel children share the host cwd; independent write
ownership is the planner's responsibility. Use concurrency 1 for overlapping edits.

## Alternatives and follow-ups

A generated Python/JavaScript script would provide closer Claude Code workflow parity. It
also requires an execution boundary, deterministic replay/caching rules and a durable
authority-aware recovery contract. Defer it until the graph/runtime/UI behavior is verified.

Background execution would keep the conversation free, but requires session-owned task
cleanup, completion delivery, command availability during execution and receipt routing
through rebuilds. It is a separate follow-up, not a flag enabled on an unmanaged asyncio task.

Richer progress navigation, peer messaging, dynamically authored profiles, isolated worktrees,
durable child resurrection and external-side-effect idempotency remain outside this version.
