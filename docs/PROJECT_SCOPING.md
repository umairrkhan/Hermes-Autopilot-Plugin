# Project Autopilot — Project-Scoped Architecture

## Principle

Project Autopilot is **not one global bot**. It is a reusable plugin that creates an isolated autopilot instance per Hermes Project.

If you have:

- Solar360 Project
  - Solar360 Discussion chat
  - Solar360 Development chat
  - Solar360 phased plan
- Another Product Project
  - Its own Discussion chat
  - Its own Development chat
  - Its own phased plan

then Autopilot must treat those as separate worlds.

## State Layout

Each registered project gets its own state directory:

```text
$HERMES_HOME/state/autopilot/
├── manifest.json                         # active_project_id pointer
└── projects/
    ├── solar360/
    │   └── autopilot_state.json          # Solar360-only registration, lease, history
    └── other-product/
        └── autopilot_state.json          # Other project-only registration, lease, history
```

This prevents one project from leaking into another project's plan, lease, workspace, run count, or transition history.

## Registration Contract

Every project registration binds Autopilot to:

```json
{
  "project_id": "solar360",
  "workspace_root": "/path/to/solar360/repo",
  "discussion_session_id": "solar360-discussion-session",
  "development_session_id": "solar360-development-session",
  "display_title": "Solar360",
  "discussion_title": "Solar360 Project Discussion",
  "development_title": "Solar360 Development"
}
```

The `discussion_session_id` is where plans and phase decisions come from.
The `development_session_id` is where implementation happens.
The `workspace_root` is the only repo/folder the project autopilot may target.

For normal use, users do not need to manually find the session IDs. Run
`/autopilot register` from the project Discussion chat and provide the
Development chat title. Autopilot auto-fills:

- `discussion_session_id` from the current Hermes session
- `development_session_id` from the newest session whose title equals `development_title`

## Commands

```bash
/autopilot register '{...}'     # registers/activates the current project
/autopilot projects             # lists known project autopilots
/autopilot use <project_id>     # switches active project autopilot
/autopilot status               # status for active project only
/autopilot lease '{...}'        # lease for active project only
/autopilot simulate             # simulation for active project only
```

## Example Flow

### Solar360

```bash
/autopilot register '{"project_id":"solar360", "workspace_root":"/repos/solar360", "discussion_title":"Solar360 Project Discussion", "development_title":"Solar360 Development"}'
/autopilot simulate
```

Autopilot uses only Solar360's discussion/development pairing and Solar360's workspace.

### Another Project

```bash
/autopilot register '{"project_id":"inventory-app", "workspace_root":"/repos/inventory", "discussion_title":"Inventory Discussion", "development_title":"Inventory Development"}'
/autopilot simulate
```

Autopilot now uses only Inventory App's project plan and workspace.

### Switching Back

```bash
/autopilot use solar360
/autopilot status
```

The Solar360 state, run count, lease, and history are restored without touching Inventory App.

## Safety Invariants

1. Project id is the state boundary.
2. Workspace root is immutable after registration for that project.
3. Discussion/development session pairing is part of the registration contract.
4. Active project selection controls which state file is read/written.
5. Cross-project contamination is a bug.
6. Emergency kill switch remains global in Phase 1 for safety.

## Future Phase Behavior

In real execution phases, the autopilot must:

- Read the active project's discussion plan only
- Execute in the active project's development session only
- Use the active project's workspace only
- Require a lease matching the active project id and workspace root
- Never borrow another project's plan, phase status, business rules, or credentials
