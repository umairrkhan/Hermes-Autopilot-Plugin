# Hermes Project Autopilot

A project-scoped Hermes plugin for lease-gated, human-supervised software delivery through durable Kanban workers and isolated Git worktrees.

## Status

**Shipped roadmap: 4/4 phases implemented.** Runtime readiness remains fail-closed per project: a shipped feature does not imply that a project has a valid registration, lease, verification profile, gateway, approved brief, or human acceptance.

1. **Foundation / Offline Simulation** — registration, isolated state, leases, policy, state machine, kill switch, audit, simulation.
2. **Guarded Read-Only Handoff** — structured Discussion→Development briefs and handoff validation.
3. **Controlled Development Run Packages** — explicit brief approval and non-executing Development-session packages.
4. **Supervised Durable Development Loop** — real Kanban dispatch, isolated worktree edits, independent verification, bounded remediation, durable evidence, human acceptance, checkpointing, and separately authorized local commits.

## How This Replaces the Manual Two-Chat Cycle

| Manual step | Autopilot equivalent |
|---|---|
| Discuss purpose, requirements, and phases in the Discussion chat | Keep using the registered Discussion session as the product/requirements authority. |
| Copy an approved phase prompt into the Development chat | `/autopilot brief` creates the immutable handoff; explicit approval plus `loop start` dispatches a durable Development card. |
| Repeatedly allow routine implementation commands | Approve the fixed `autonomous-development` lease once for the bounded session; workers remain constrained by exact tool and verification policy. |
| Answer routine multiple-choice questions | The worker must call the structured `autopilot_decide` tool. Only typed `routine_low_risk` questions with an explicit safe recommendation auto-select; high-risk or ambiguous questions durably block until `/autopilot loop answer …`. |
| Copy the Development response back to Discussion | The dependent independent verifier produces provenance-bound evidence; `loop report` presents the durable result without trusting a pasted summary. |
| Review the phase and prepare the next phase | Human acceptance closes the verified phase. The next phase begins from a new Discussion-authorized brief, so Autopilot cannot silently invent or approve product scope. |

The registered Discussion and Development session IDs are **provenance and boundary metadata**. Public Hermes plugin APIs do not support injecting messages into an arbitrary GUI session, so Autopilot never writes the session database or claims that a Kanban worker ran inside either registered chat. Live implementation and verification run in dedicated Hermes Kanban worker sessions; the Discussion session remains the human authority where commands, decisions, evidence review, and acceptance occur.

A Development/remediation/verifier worker never commits or pushes. The optional local worktree commit sequence is separate and requires three explicit commands after human acceptance. There is no push command.

## Live Execution Architecture

```text
/autopilot loop start <brief_id>
        │
        ├─ validates registration, approved brief, autonomy lease,
        │  verification profile, Git workspace, Hermes Project, gateway
        │
        ├─ creates blocked Development task in project Kanban board
        ├─ creates dependent independent-verifier task
        ├─ binds both IDs, HEAD, profile digest, and source-status digest
        └─ promotes Development only after the complete pipeline exists

Hermes gateway dispatcher
        │
        ├─ Development worker: isolated Git worktree, no commit/push/deploy
        ├─ Verifier worker: read-only review + exact configured checks
        └─ Kanban run metadata: provenance-bound bounded evidence

Lifecycle reconciliation
        │
        ├─ Kanban completion/block/failure hooks: low-latency best effort
        ├─ session-start and activity-throttled recovery: bounded active-loop reconciliation
        ├─ /autopilot loop sync: explicit authoritative recovery path
        ├─ failed verification: bounded remediation + re-verification
        └─ passed verification: AWAITING_HUMAN_ACCEPTANCE (never auto-accepted)
```

The plugin uses public Hermes surfaces: `PluginContext.dispatch_tool("terminal", …)`, plugin hooks, Hermes Projects, and the `hermes kanban` CLI. It does not write Hermes session/Kanban SQLite files or use background delegation as a durability mechanism.

## Safety Invariants

- **Project isolation:** every registration, lease, profile, loop, result, checkpoint, and authorization is stored under one `project_id`.
- **Original workspace preservation:** Development runs in `.worktrees/<task_id>` from the recorded `HEAD`; pre-existing source-workspace changes are not copied or modified.
- **Concurrent-edit detection:** the source workspace Git-status digest is bound at dispatch and checked before checkpoint/commit authorization.
- **Per-tool enforcement:** Autopilot-tenant workers fail closed without a durable binding and are blocked after lease expiry, kill activation, human-decision pause, loop cancellation, workspace escape, disallowed tools, background terminal use, or commit/push/destructive Git attempts.
- **Exact command profiles:** verifier checks and separately listed Development routine commands use structured argv/cwd/timeout configuration; shell wrappers, inline evaluation, composition, and interpolation are rejected.
- **Trusted evidence:** verifier metadata must match project, loop, brief, task, run, starting revision, verification profile, changed paths, and every configured check.
- **Bounded remediation:** the project profile sets a small maximum; exhausted or malformed results go to `NEEDS_HUMAN`.
- **Human acceptance:** passing evidence only reaches `AWAITING_HUMAN_ACCEPTANCE`; `/autopilot loop accept` writes a separate immutable acceptance record.
- **Separate commit gate:** acceptance does not commit. Checkpoint, commit authorization, and commit are three explicit actions. Authorization lasts 15 minutes and is one-time.
- **No push or deployment:** the commit command creates a local commit only in the isolated worktree. Push, merge, deployment, migrations, external writes, credentials, and personal-account decisions remain outside this workflow.
- **Truthful cancellation:** stop first records `CANCEL_REQUESTED`, reclaims running tasks, blocks unfinished cards, and reports `CANCELED` only after confirmation.

## State Layout

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
    ├── decisions/
    ├── results/
    └── checkpoints/
```

## Live Workflow

```text
/autopilot register {"project_id":"my-project","workspace_root":"/absolute/project","discussion_title":"My Project Discussion","development_title":"My Project Development"}

# Configure exact, project-owned verification commands.
/autopilot verify configure {"schema_version":1,"project_id":"my-project","workspace_root":"/absolute/project","prerequisites":["python3","ruff"],"max_remediation_cycles":1,"development_commands":[{"command_id":"format","argv":["ruff","format","."],"cwd":".","timeout_seconds":120}],"checks":[{"check_id":"unit","argv":["python3","-m","pytest","-q"],"cwd":".","timeout_seconds":300,"required_evidence":["exit_code","duration_seconds","stdout_excerpt","stderr_excerpt"]}]}
/autopilot verify validate

# Create/read a brief, then explicitly approve it.
/autopilot brief
/autopilot approve <brief_id>

# Review and approve the fixed live-execution lease.
/autopilot lease request autonomous-development
/autopilot lease approve autonomous-development

# Dispatch and observe the durable pipeline.
/autopilot loop start <brief_id>
/autopilot loop status
/autopilot loop sync <loop_id>
/autopilot loop report <loop_id>
# If a structured question paused the worker:
/autopilot loop answer <loop_id> <question_id> <answer>

# Human gate after trusted verification evidence.
/autopilot loop accept <loop_id>

# Optional local commit sequence—three separate explicit actions.
/autopilot loop checkpoint <loop_id>
/autopilot loop authorize-commit <loop_id>
/autopilot loop commit <loop_id>
```

Interactive account-selection or consent flows are not automated. Any such requirement blocks for the user.

## Commands

| Command | Purpose |
|---|---|
| `/autopilot status` | Active project state, registration, lease, and kill status |
| `/autopilot register <json>` | Register a project-scoped immutable contract |
| `/autopilot projects` / `use <project_id>` | List or switch project contexts |
| `/autopilot phases` / `readiness` | Shipped roadmap and active-project runtime blockers |
| `/autopilot lease request|approve …` | Review or activate a fixed permission preset |
| `/autopilot verify configure|show|validate` | Manage exact project verification configuration |
| `/autopilot brief [json]` / `handoff` | Generate/read briefs and validate handoff |
| `/autopilot approve|revoke <brief_id>` | Control brief execution authorization |
| `/autopilot run <brief_id>` | Prepare a legacy non-executing Development run package |
| `/autopilot loop start <brief_id>` | Dispatch live Development + independent verifier tasks |
| `/autopilot loop sync <loop_id>` | Reconcile authoritative Kanban task/run state |
| `/autopilot loop report <loop_id>` | Show task IDs, pending decisions, evidence, acceptance, checkpoint, and commit state |
| `/autopilot loop answer <loop_id> <question_id> <answer>` | Persist a human answer, comment it on the blocked task, and resume that task |
| `/autopilot loop accept <loop_id>` | Write explicit human acceptance after passing evidence |
| `/autopilot loop checkpoint <loop_id>` | Create immutable changed-file checkpoint after acceptance |
| `/autopilot loop authorize-commit <loop_id>` | Create one-time 15-minute local commit authorization |
| `/autopilot loop commit <loop_id>` | Commit locally in the isolated worktree; never push |
| `/autopilot loop stop <loop_id>` | Reclaim and block one durable pipeline |
| `/autopilot stop` | Activate global kill switch and cancel active pipelines |
| `/autopilot simulate` | Run deterministic offline simulation |

## Verification Evidence Contract

Verifier completion metadata must include:

- `autopilot_contract_version: 1`
- `role: "verifier"`
- exact `brief_id` and `starting_revision`
- `verification_status` and `review_status`
- complete relative `changed_files`
- one result for every configured check with exact `check_id`, argv, cwd, exit code, duration, and bounded stdout/stderr excerpts
- bounded findings and residual risk

Evidence is redacted and size-limited before durable persistence. Missing, duplicate, extra, malformed, or mismatched evidence is rejected.

## Development and Testing

```bash
python -m pytest -q
python scripts/runtime_e2e.py
```

The isolated runtime E2E uses a temporary Hermes home and Git repository, exercises real Hermes Project/Kanban persistence through the complete accepted-checkpoint-authorized-local-commit path, and removes temporary state on exit. It never contacts a model provider or mutates a registered real project.

The suite covers state/lease/policy security, plugin discovery and registration, verification profiles, live dispatch, worktree preservation, lifecycle reconciliation, trusted evidence, remediation limits, cancellation, checkpointing, commit authorization, and prohibited-pattern scans.

No commit is made by the implementation or test workflow unless a user explicitly invokes the separate Autopilot commit commands for an accepted live loop.
