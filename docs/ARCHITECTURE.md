# Architecture — Project Autopilot

## Shipped Roadmap

`phases.py` separates shipped implementation status from active-project runtime readiness:

1. Foundation / Offline Simulation — complete.
2. Guarded Read-Only Handoff — complete.
3. Controlled Development Run Packages — complete.
4. Supervised Durable Development Loop — complete.

A complete phase may still be `BLOCKED` for the active project when registration, lease, workspace, capability, adapter, verification-profile, gateway, brief-approval, or human gates are missing.

## Runtime Components

```text
Hermes plugin entrypoint (__init__.py)
├── /autopilot command closure
│   └── PluginToolRuntime(ctx)
├── pre_tool_call hook
│   └── policy_hook.py
└── Kanban lifecycle hooks
    └── lifecycle.py → loop_reconciler.py

commands.py
├── registration.py / storage.py
├── lease.py / policy.py / kill_switch.py / audit.py
├── verification.py
├── checkpoint.py
└── adapters/
    ├── execution_bridge.py      # read-only briefs and handoff
    ├── runner.py                # legacy non-executing run packages
    ├── development_executor.py  # live Kanban pipeline dispatch/remediation
    ├── autonomous_loop.py       # durable loop/result/acceptance state
    ├── loop_reconciler.py       # task/run/evidence/cancellation truth
    └── simulation.py            # deterministic offline flow
```

## Project Scoping

Every registered Project owns isolated plugin state:

```text
$HERMES_HOME/state/autopilot/
├── manifest.json
├── kill_switch.json
└── projects/<project_id>/
    ├── autopilot_state.json
    ├── verification_profile.json
    ├── briefs/
    ├── runs/
    ├── loops/
    ├── results/
    └── checkpoints/
```

The manifest stores only the currently selected project. Durable loop bindings include the project, brief, lease, board, Development/verifier task IDs, starting revision, verification-profile digest, and original-workspace status digest.

## Live Dispatch

`DevelopmentExecutor.dispatch()` performs fail-closed preflight:

1. Global kill switch is clear.
2. Brief is explicitly approved.
3. Registration, brief, loop, lease, verification profile, and workspace identities match.
4. Lease is valid, unexpired, and grants the fixed autonomous-development capabilities.
5. Workspace is a Git repository; `HEAD` and raw source Git status are recorded.
6. Verification prerequisites exist.
7. Hermes gateway is running.
8. The Hermes Project primary folder exactly matches the registration.
9. A project-scoped Kanban board exists.

It then creates the Development card blocked, creates its dependent verifier card, persists all task bindings, and promotes Development. This staging order prevents a partially constructed pipeline from executing.

Development uses Hermes Kanban `worktree` workspace mode. The verifier uses `dir:<worktree>` and depends on Development. Remediation/re-verification pairs follow the same blocked-then-promote protocol.

## Worker Enforcement

`policy_hook.pre_tool_call_guard()` is a no-op for ordinary sessions. For a Kanban task ID found in an active loop it:

- reloads global kill state and project lease before each tool invocation;
- validates loop/task/lease binding and absolute expiry;
- validates current process CWD and file paths against the isolated worktree;
- blocks unknown/external tools, cross-task/board Kanban access, artifact uploads, delegation, cron, local-image access, background terminal commands, shell control operators, writes by verifier role, and Git writes outside the exact post-verification promotion allowlist;
- requires every non-Git terminal invocation to match one configured verification argv and declared cwd exactly;
- rejects Git repository/worktree overrides, output/external-helper/pager options, and path escape;
- rejects completion metadata containing recognizable unredacted secrets or personal identifiers before Kanban persistence;
- allows `kanban_block` after expiry/cancellation only for the worker's own bound card so it can report its stop reason.

Kanban per-attempt max runtime remains the hard timeout if a worker makes no further tool call.

## Durable Lifecycle and Evidence

Kanban task/run state is authoritative. Lifecycle hooks call reconciliation after completion/block/failure, but hooks are best effort. `/autopilot loop sync <loop_id>` can always reconstruct state after a restart or missed hook.

Verifier metadata is validated against:

- project, loop, brief, board, task, and run provenance;
- starting Git revision;
- canonical verification-profile digest;
- exact changed paths;
- exactly one result for every configured check;
- exact argv/cwd, exit code, duration, and required bounded excerpts;
- bounded findings and residual risk.

Malformed metadata moves to `NEEDS_HUMAN`. Valid failed/rejected evidence may queue only the configured number of remediation cycles. Valid passed/approved evidence is stored immutably, enters `MERGING`, and is committed and pushed by the host-controlled reconciler before moving to `AWAITING_HUMAN_ACCEPTANCE`.

`/autopilot loop accept` revalidates the project-scoped result path/provenance and writes a separate immutable acceptance record.

## Verified Worktree Promotion

After accepted verifier evidence, `LoopReconciler`:

1. Resolves `.worktrees/<development_task_id>` beneath the registered workspace and verifies it is the Git worktree root.
2. Requires the autonomous lease's `git.commit` and `git.push` capabilities and `allow-list` Git policy.
3. Stages all verified worktree changes and creates a structured `autopilot(<task_id>): <brief_id>` commit.
4. Records the commit revision in the durable loop before pushing `HEAD` to the bound target branch (default `Development`).
5. Fails closed to `NEEDS_HUMAN` on staging, commit, revision, or push failure. The worktree is retained for diagnostics.

The push is a normal fast-forward Git push; the workflow does not force-push or perform a branch merge.

## Legacy Checkpoint and Commit Separation

After acceptance:

1. `loop checkpoint` compares current changed paths to verifier evidence, confirms the original workspace status digest is unchanged, rejects unsafe paths/symlinks/oversize files/obvious secrets, and creates a mode-0600 ZIP with file hashes plus status and content digests.
2. `loop authorize-commit` revalidates source status, worktree status, live bytes, checkpoint provenance, the exact archive member set, every archived file size/hash, and the recomputed canonical content digest, then writes a one-time 15-minute authorization.
3. `loop commit` repeats the archive and live-worktree validation, stages and commits only in the isolated worktree, marks authorization used, and records the verified commit revision.

These legacy manual checkpoint commands do not push, merge, deploy, migrate, or mutate the original workspace.

## Cancellation

Cancellation is two-phase and truthful:

1. Persist `CANCEL_REQUESTED`; the tool hook stops subsequent worker actions.
2. Inspect every bound task, reclaim a running worker, and block unfinished cards.
3. Persist `CANCELED` only if every control operation succeeds. Otherwise retain `CANCEL_REQUESTED` and report unconfirmed task IDs.

The global stop writes the independent kill-switch file before attempting pipeline cancellation.

## Trust Boundaries

- Public Hermes plugin/tool/hook/Project/Kanban surfaces are used.
- Hermes SQLite/session/Kanban databases are never written directly.
- Background delegation is not treated as durable execution.
- Tool output, worker summaries, and caller-supplied JSON are untrusted until schema/provenance validation.
- Original uncommitted changes remain outside the worker worktree and are checked again before checkpoint/commit.
