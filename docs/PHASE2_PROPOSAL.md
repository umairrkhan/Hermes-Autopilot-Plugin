# Phase 2 Implementation — Guarded Discussion→Development Bridge

> **Historical Phase 2 record.** The bridge behavior below remains accurate for `/autopilot execute`, brief, handoff, and legacy run-package commands. Statements that a future runner is still required are superseded by `../README.md` and `ARCHITECTURE.md`; live execution uses `/autopilot loop start` and durable Kanban workers.

> **Status:** Implemented as a guarded handoff bridge.  
> **Scope:** Generate and validate Development execution briefs from project context.  
> **Non-scope:** Autonomous coding, workspace edits, git operations, or live cross-chat message injection.

## What Phase 2 now provides

Phase 2 adds `autopilot/adapters/execution_bridge.py` and related `/autopilot` commands.

The bridge can:

1. Validate the active project registration and workspace.
2. Validate a non-expired, workspace-scoped autonomy lease.
3. Require explicit capabilities before producing a handoff artifact.
4. Generate immutable `DevelopmentExecutionBrief` JSON artifacts.
5. Persist those artifacts under the project-scoped Autopilot state directory.
6. Audit-log brief generation.
7. Validate existing briefs for human review.

The bridge does **not**:

- edit project files
- run terminal commands
- commit or push git changes
- write directly into Hermes session databases
- inject live messages into the Solar360 Discussion or Development chat
- authorize autonomous execution

Every generated brief has:

```text
human_gate_required = true
execution_authorized = false
```

That means the brief is ready for human review or manual Development-chat handoff, but it is not permission for the plugin to start coding by itself.

## Commands

```bash
/autopilot brief
/autopilot brief '{"scope":"...","notes":"...","tasks":[...]}'
/autopilot brief --list
/autopilot brief --read <brief_id>
/autopilot handoff
/autopilot handoff --brief <brief_id>
/autopilot execute
```

### `/autopilot brief`

Generates a read-only Development execution brief. Minimum required lease capability:

```text
workspace.read
```

### `/autopilot handoff`

Validates current bridge readiness and existing brief integrity. It does not send messages or execute code.

### `/autopilot execute`

In Phase 2, this command still does **not** perform autonomous coding. It runs the stricter Phase 2 readiness gate and, if permitted, generates a handoff brief.

Current Phase 2 readiness requires:

```text
workspace.write
git.commit
```

Those capabilities are intentionally stricter than `/autopilot brief` because `/autopilot execute` is the future real-execution entry point. Today, it remains a guarded brief-generation path with `execution_authorized=false`.

## Solar360 usage

From Solar360 Project Discussion, after registration:

```bash
/autopilot status
/autopilot readiness
/autopilot brief '{"scope":"Next accepted Solar360 development phase","notes":"Prepare a Development-chat execution handoff from the accepted Discussion plan."}'
/autopilot brief --list
/autopilot handoff
```

Then copy/review the generated brief into Solar360 Development manually, or use it as the source for the next implementation prompt.

## What remains for real autonomous coding

To actually let Autopilot communicate into Solar360 Development and perform coding, a future Phase 3 runner is still required. That runner must be explicitly authorized and should provide:

1. a safe way to create/send a message into the target Development session or spawn a controlled Hermes Development worker;
2. workspace editing through Hermes file tools scoped to the registered workspace only;
3. terminal/test execution under command-approval policy;
4. result reporting back to Discussion through a supported Hermes messaging/session API;
5. strict human gates before commits, pushes, migrations, deployments, or role/security-sensitive changes.

Until that exists, Phase 2 is the safe bridge: it packages Discussion→Development intent into auditable handoff artifacts without starting uncontrolled work.
