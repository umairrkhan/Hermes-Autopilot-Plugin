# Project Autopilot — Phase Roadmap and Completion Status

> **Historical design record.** This document preserves the earlier Phase 2 plan. Claims below that Phases 3–4 or live execution are future/unavailable are superseded by `../README.md` and `ARCHITECTURE.md`; the shipped durable executor remains fail-closed behind project, lease, verification, and human gates.

## Current completion

All four roadmap phases are now implemented. The authoritative current behavior is documented in `../README.md` and `ARCHITECTURE.md`: live execution is available through lease-gated, project-scoped durable Kanban Development/verifier workers with isolated Git worktrees, exact verification profiles, trusted evidence, bounded remediation, human acceptance, immutable checkpoints, and separate one-time local commit authorization.

The remainder of this file is retained as the historical Phase 2 design plan.

## Phase 1 — Foundation / Offline Simulation

**Status:** Complete

Implemented:

- Project-scoped registration
- Discussion/development session binding
- Workspace binding
- Durable project-isolated state
- Active project switching
- Lease model
- State machine
- Kill switch
- Audit trail
- Offline simulation
- Read-only session and Kanban adapters

Commands:

```bash
/autopilot register '{...}'
/autopilot projects
/autopilot use <project_id>
/autopilot status
/autopilot simulate
```

## Phase 2 — Guarded Execution Bridge

**Status:** Implemented (guarded)

Phase 2 introduces the `ExecutionBridge` adapter and `DevelopmentExecutionBrief` model. This enables a structured, auditable handoff from the Discussion session to the Development session, without enabling autonomous file editing or git operations.

### What Phase 2 provides

- **ExecutionBridge adapter** — validates Phase 2 readiness, generates and persists development execution briefs
- **DevelopmentExecutionBrief** — immutable dataclass packaging project context, session metadata, lease authorization, task list, and risk classification into a structured JSON artifact
- **Brief persistence** — briefs are stored as versioned JSON artifacts under the project state directory, with audit trail logging
- **Readiness auto-detection** — `readiness_for_phase()` automatically detects whether the `ExecutionBridge` adapter is installed
- **Handoff validation** — the handoff command verifies brief integrity, project match, lease validity, and human gate status

### What Phase 2 does NOT do

- Does **not** perform any file editing, git operations, or autonomous execution
- Does **not** create files in the workspace (briefs are stored in the state directory)
- Does **not** bypass the human gate — all briefs require explicit human authorization
- Does **not** authorize execution — `execution_authorized` is always `False`

### Safety properties

- Briefs are immutable frozen dataclasses — cannot be modified after creation
- `human_gate_required` is always `True` — cannot be overridden
- `execution_authorized` is always `False` — requires explicit Phase 3 authorization
- Brief validation checks project match, lease validity, and human gate status
- All operations are audit-logged
- No workspace files are modified during brief generation

### Required before Phase 2 readiness

- Valid active project registration
- Valid active workspace
- Non-expired lease
- Lease `project_id` matches active project
- Lease workspace scope covers active workspace
- Capabilities explicitly granted:
  - `workspace.read`
  - `git.read`
- The `ExecutionBridge` adapter installed (auto-detected)

### Commands

```bash
/autopilot brief                       # Generate a development execution brief
/autopilot brief '{"scope":"..."}'     # Generate brief with custom scope and tasks
/autopilot brief --list                # List existing briefs for the active project
/autopilot brief --read <brief_id>     # Read a specific brief
/autopilot handoff                     # Validate Discussion→Development handoff readiness
/autopilot handoff --brief <brief_id>  # Validate a specific brief for execution
/autopilot execute                     # Generate brief (fail-closed until adapter + capabilities)
```

### Brief structure

A `DevelopmentExecutionBrief` contains:

- `brief_id` — unique identifier (timestamp + microsecond + short UUID)
- `brief_version` — sequential version within the project
- `project_id` and `workspace_root` — project context
- `discussion_session_id` and `development_session_id` — session bindings
- `lease_id` and `lease_expiry` — lease authorization traceability
- `granted_capabilities` — capabilities from the lease
- `scope` and `notes` — human-readable context
- `tasks` — list of `BriefTask` objects with risk classification
- `human_gate_required` — always `True`
- `execution_authorized` — always `False`
- `created_at` and `created_by` — audit metadata

### Risk classification

Task risk levels are auto-classified from task title/description:

- **low** — typo fixes, formatting, documentation
- **medium** — commits, pushes, merges, installs
- **high** — security changes, deployments, migrations, privileged operations, unknown actions

## Phase 3 — Autonomous Phase Delivery

**Status:** Gated in code

Additional requirement:

- `next-phase` capability

Intended future behavior:

- Execute one accepted project phase
- Run verifier/reviewer/remediator loop
- Stop at human phase acceptance gate
- Preserve project-scoped state and audit logs

## Phase 4 — Scaled Multi-Project Orchestration

**Status:** Gated in code

Additional capabilities for future scaled orchestration:

- `git.push`
- `external.write`
- `deployment`

Intended future behavior:

- Coordinate many active project autopilots
- Preserve strict project isolation
- Prevent cross-project plan/workspace/session leakage
- Enforce per-project leases and audit trails

## Commands for all phases

```bash
/autopilot phases     # Show roadmap and remaining phases
/autopilot readiness  # Show active project readiness for each phase
/autopilot execute    # Phase 2: generate execution brief (fail-closed until adapter + caps)
/autopilot brief      # Phase 2: generate/list/read development execution briefs
/autopilot handoff    # Phase 2: validate Discussion→Development handoff readiness
```

## Why real execution remains blocked

The user requirement is that Autopilot must be reusable for many projects and must follow each project's own discussion/development plan. That makes safety more important, not less.

Phase 2 provides a structured handoff mechanism (briefs) without enabling autonomous execution. The brief is a READ-ONLY artifact that packages everything an external executor needs — but the actual execution authorization remains gated behind Phase 3 lease and adapter requirements.

This avoids accidentally letting one project's plan, workspace, session, or lease affect another project.
