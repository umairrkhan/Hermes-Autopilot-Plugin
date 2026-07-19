---
name: project-autopilot
description: "Operate project-scoped, lease-gated durable Development and verifier workers with human acceptance and separately authorized local commits."
version: 0.5.0
author: Hermes Agent + Nous Research
metadata:
  hermes:
    tags: [autopilot, kanban, development, verification, safety]
---

# Project Autopilot

Use this skill when operating the `project-autopilot` plugin for a registered Hermes Project.

## Core Model

- **Shipped is not ready:** `/autopilot phases` shows implementation status; `/autopilot readiness` shows the active project's current blockers.
- **Kanban is the durable executor:** live loops create a Development task and dependent independent-verifier task on the project board.
- **Worktree isolation:** workers edit `.worktrees/<development_task_id>`, never the original workspace.
- **Lease and kill checks:** a `pre_tool_call` hook rechecks bound workers; Kanban max runtime is a second hard limit.
- **Session IDs are provenance:** registered Discussion/Development IDs identify the product-authority boundary; live work runs in dedicated Kanban worker sessions because public plugin APIs do not inject into arbitrary GUI sessions.
- **Hooks plus recovery:** lifecycle hooks reconcile quickly; session-start and activity-throttled recovery scan bounded active loops, while `/autopilot loop sync <loop_id>` is the explicit recovery path.
- **Evidence is not acceptance:** passing verifier evidence yields `AWAITING_HUMAN_ACCEPTANCE`; only a human command creates `ACCEPTED`.
- **Acceptance is not commit:** checkpoint, commit authorization, and local worktree commit are separate explicit commands.

## Required Live Workflow

1. Confirm the active Project:
   - `/autopilot status`
   - `/autopilot projects`
   - `/autopilot use <project_id>` when necessary.
2. Configure exact verification:
   - `/autopilot verify configure <json>`
   - `/autopilot verify validate`
3. Generate/read a brief and obtain explicit approval:
   - `/autopilot brief`
   - `/autopilot approve <brief_id>`
4. Review and approve the fixed live lease:
   - `/autopilot lease request autonomous-development`
   - `/autopilot lease approve autonomous-development`
5. Dispatch:
   - `/autopilot loop start <brief_id>`
6. Observe/reconcile:
   - `/autopilot loop status`
   - `/autopilot loop sync <loop_id>`
   - `/autopilot loop report <loop_id>`
   - If a structured question is pending, `/autopilot loop answer <loop_id> <question_id> <answer>`.
7. If status is `AWAITING_HUMAN_ACCEPTANCE`, review evidence and explicitly accept:
   - `/autopilot loop accept <loop_id>`
8. Optional local commit, only after acceptance:
   - `/autopilot loop checkpoint <loop_id>`
   - `/autopilot loop authorize-commit <loop_id>`
   - `/autopilot loop commit <loop_id>`

Never collapse steps 7–8 into one action.

## Verification Profile Shape

```json
{
  "schema_version": 1,
  "project_id": "my-project",
  "workspace_root": "/absolute/project",
  "prerequisites": ["python3", "ruff"],
  "max_remediation_cycles": 1,
  "development_commands": [
    {
      "command_id": "format",
      "argv": ["ruff", "format", "."],
      "cwd": ".",
      "timeout_seconds": 120
    }
  ],
  "checks": [
    {
      "check_id": "unit",
      "argv": ["python3", "-m", "pytest", "-q"],
      "cwd": ".",
      "timeout_seconds": 300,
      "required_evidence": [
        "exit_code",
        "duration_seconds",
        "stdout_excerpt",
        "stderr_excerpt"
      ]
    }
  ]
}
```

Commands are argv arrays, not shell strings. CWD must remain relative and inside the project. Profiles are digested at dispatch; changing one during a run requires human review.

## Safety Rules

- Do not bypass registration, brief approval, lease approval, verification, or acceptance gates.
- Do not automate personal account selection, consent, credentials, or privileged identity decisions.
- Do not commit, push, merge, deploy, migrate, install dependencies, or perform external writes from a Development/remediation/verifier worker.
- Worker terminal calls must be foreground and one command at a time. Verifier commands must exactly match configured checks; Development/remediation workers may additionally use exact `development_commands`. Git is restricted to guarded read-only forms, and shell wrappers/inline evaluation are rejected.
- Autopilot is not an OS/container sandbox: approved verification commands execute project code with Hermes process permissions. Use an external sandbox for hostile or untrusted repositories.
- A dirty original workspace is allowed but must remain untouched. The loop binds its Git-status digest and fails checkpoint/commit authorization if it changes.
- Verifier evidence must exactly match all configured checks and complete changed paths. Caller-supplied summaries are not proof.
- Remediation is bounded by `max_remediation_cycles`; exhaustion or malformed evidence becomes `NEEDS_HUMAN`.
- A checkpoint archives only verifier-approved changed paths, rejects symlinks/oversize payloads/obvious credentials, and binds status plus byte-level content digests.
- Commit authorization expires after 15 minutes, is one-time, and targets one checkpoint. The commit remains local to the isolated worktree; there is no push command.

## Stop and Recovery

- `/autopilot loop stop <loop_id>` persists `CANCEL_REQUESTED`, reclaims a running worker, blocks unfinished cards, and reports `CANCELED` only after confirmation.
- `/autopilot stop` activates the global kill switch first, then attempts to cancel all active loops.
- Startup and activity-throttled recovery automatically reconcile a bounded set of active loops; failures are recorded on the loop.
- Use `/autopilot loop sync <loop_id>` for immediate explicit reconciliation after an outage or when recovery reports an error.
- Keep the kill switch active when cancellation is unconfirmed and inspect the reported Kanban task IDs.

## Legacy Commands

`/autopilot execute`, `/autopilot handoff`, and `/autopilot run <brief_id>` remain useful for read-only handoff and non-executing run-package workflows. They do not dispatch live workers. Use `/autopilot loop start` for durable execution.
