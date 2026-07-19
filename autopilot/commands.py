"""Command dispatcher for /autopilot slash command.

Commands:
  /autopilot status           — show current state and info
  /autopilot register <json>  — register a project contract
  /autopilot validate          — validate current registration
  /autopilot lease <json>      — inspect or set a lease
  /autopilot simulate          — run a simulation
  /autopilot off              — gracefully turn off
  /autopilot stop             — emergency stop + kill switch
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from .constants import (
    STATE_IDLE, STATE_CONFIGURED, STATE_LEASE_READY, STATE_SIMULATING,
    STATE_PREPARING_BRIEF, STATE_EXECUTING, STATE_VERIFYING,
    STATE_REVIEWING, STATE_REMEDIATING, STATE_PHASE_ACCEPTED,
    STATE_NEEDS_HUMAN, STATE_STOPPED,
    LEASE_PRESET_PHASE2_READONLY, LEASE_PRESET_PHASE3_DEVELOPMENT,
    LEASE_PRESET_AUTONOMOUS_DEVELOPMENT,
    LEASE_PRESET_DURATION_HOURS,
    LEASE_PRESET_ISSUER,
)
from .kill_switch import (
    is_kill_switch_active, activate_kill_switch, check_kill_switch,
)
from .storage import (
    load_state, save_state, mutate_state, reset_state,
    set_active_project_id, get_active_project_id, list_projects,
    mutate_project_state,
)
from .state_machine import (
    current_state_label, can_transition, transition,
    state_summary, increment_loop,
)
from .registration import (
    validate_registration, validate_workspace_path, registration_to_dict,
    ProjectRegistration, apply_registration_defaults,
)
from .lease import (
    build_lease_preset, validate_lease, lease_to_dict, validate_lease_expired,
)
from .policy import classify_risk, evaluate_action, validate_lease_for_workspace
from .audit import log_event
from .adapters.simulation import SimulationAdapter
from .phases import phase_report, readiness_report, readiness_for_phase, PHASE_2
from .verification import (
    load_verification_profile,
    save_verification_profile,
    validate_verification_profile,
    verification_profile_to_dict,
)

logger = logging.getLogger(__name__)

def _get_execution_bridge():
    """Get the execution bridge adapter, or None if Phase 2 is not available."""
    try:
        from .adapters.execution_bridge import ExecutionBridge
        return ExecutionBridge()
    except ImportError:
        return None


def _json_payload(raw: str) -> str:
    """Normalize JSON typed in chat slash commands.

    Hermes slash commands are not a shell, so examples wrapped in single
    quotes arrive with the quote characters intact. Accept both wrapped and
    unwrapped JSON payloads.
    """
    payload = (raw or "").strip().strip("·").strip()
    if len(payload) >= 2 and payload[0] == payload[-1] and payload[0] in {"'", '"'}:
        payload = payload[1:-1].strip()
    return payload


def handle_autopilot_command(raw_args: str, *, runtime: Any | None = None) -> str | None:
    """Handle /autopilot slash commands.

    Signature matches plugin command handler: fn(raw_args: str) -> str | None.
    """
    args = (raw_args or "").strip()

    # Kill switch check — works even if state is malformed
    kill_reason = check_kill_switch()
    if kill_reason and not args.startswith("stop"):
        return f"AUTOPILOT STOPPED: Kill switch active — {kill_reason}\nRun /autopilot stop to reset."

    if not args or args == "status":
        return _cmd_status()
    elif args == "validate":
        return _cmd_validate()
    elif args.startswith("register"):
        return _cmd_register(args[len("register"):].strip())
    elif args.startswith("lease"):
        return _cmd_lease(args[len("lease"):].strip())
    elif args == "verify" or args.startswith("verify "):
        return _cmd_verify(args[len("verify"):].strip())
    elif args == "projects":
        return _cmd_projects()
    elif args.startswith("use "):
        return _cmd_use(args[len("use "):].strip())
    elif args == "phases":
        return _cmd_phases()
    elif args == "readiness":
        return _cmd_readiness()
    elif args == "start":
        return _cmd_start()
    elif args == "execute":
        return _cmd_execute()
    elif args.startswith("approve "):
        return _cmd_authorize_brief(args[len("approve "):].strip(), True)
    elif args.startswith("revoke "):
        return _cmd_authorize_brief(args[len("revoke "):].strip(), False)
    elif args.startswith("run "):
        return _cmd_run(args[len("run "):].strip())
    elif args == "loop" or args.startswith("loop "):
        return _cmd_loop(args[len("loop"):].strip(), runtime=runtime)
    elif args == "simulate":
        return _cmd_simulate()
    elif args.startswith("brief"):
        return _cmd_brief(args[len("brief"):].strip())
    elif args.startswith("handoff"):
        return _cmd_handoff(args[len("handoff"):].strip())
    elif args == "off":
        return _cmd_off()
    elif args == "stop":
        return _cmd_stop(runtime=runtime)
    elif args == "help":
        return _cmd_help()
    else:
        return (
            f"Unknown autopilot command: {args!r}\n"
            "Available: status, register, validate, lease, projects, use, phases, readiness, start, execute, approve, revoke, run, loop, brief, handoff, simulate, off, stop, help"
        )


def _cmd_status() -> str:
    """Show current autopilot status."""
    state = load_state()
    summary = state_summary(state)
    reg = state.get("registration")
    lease = state.get("lease")
    kill_active = is_kill_switch_active()

    active_project = get_active_project_id()

    lines = [
        "=== Project Autopilot Status ===",
        f"Active project: {active_project or 'NONE'}",
        f"State: {summary['state']}",
        f"Loop iteration: {summary['loop_iteration']}/{summary['max_loop']}",
        f"Transitions: {summary['transitions_count']}",
        f"Kill switch: {'ACTIVE' if kill_active else 'clear'}",
        f"Run count: {state.get('run_count', 0)}",
    ]

    if reg:
        lines.append(f"Project: {reg.get('project_id', 'unknown')}")
        lines.append(f"Workspace: {reg.get('workspace_root', 'unknown')}")
    else:
        lines.append("Project: NOT REGISTERED")

    if lease:
        exp = lease.get("expiry", "unknown")
        caps = lease.get("granted_capabilities", [])
        lines.append(f"Lease: {lease.get('lease_id', 'unknown')} (expires: {exp})")
        lines.append(f"Capabilities: {caps if caps else 'none (default-deny)'}")
    else:
        lines.append("Lease: NONE")

    lines.append(
        "\nShipped phases: 4/4. Runtime execution remains project-, lease-, verification-, evidence-, and human-gated."
    )
    return "\n".join(lines)


def _cmd_register(raw: str) -> str:
    """Register a project contract."""
    if not raw:
        return (
            "Usage: /autopilot register {\"project_id\": \"...\", "
            "\"workspace_root\": \"...\", \"discussion_session_id\": \"...\", "
            "\"development_session_id\": \"...\"}"
        )

    try:
        data = json.loads(_json_payload(raw))
    except json.JSONDecodeError as exc:
        return f"Invalid JSON: {exc}"

    try:
        data, resolution_notes = apply_registration_defaults(data)
    except TypeError as exc:
        return f"Registration validation failed: {exc}"

    try:
        reg = validate_registration(data)
    except ValueError as exc:
        return f"Registration validation failed: {exc}"

    # Validate workspace path
    valid, path, err = validate_workspace_path(reg.workspace_root)
    if not valid:
        return f"Workspace validation failed: {err}"

    # Check kill switch
    kill_reason = check_kill_switch()
    if kill_reason:
        return f"Cannot register — kill switch active: {kill_reason}"

    # Persist this registration into its own project-scoped state and make it active.
    # This is the critical multi-project isolation point: Solar360, Fresen, and
    # any future project each get separate registration/lease/history/run counts.
    set_active_project_id(reg.project_id)

    def mutate(state):
        state["project_id"] = reg.project_id
        state["registration"] = registration_to_dict(reg)
        if current_state_label(state) == STATE_IDLE:
            transition(state, STATE_CONFIGURED, "Registration completed")
        state["last_error"] = None

    mutate_project_state(reg.project_id, mutate)
    log_event("registration", STATE_IDLE, STATE_CONFIGURED,
              f"project_id={reg.project_id}")

    lines = [
        f"Registered: project={reg.project_id}\n"
        f"Workspace: {reg.workspace_root}\n"
        f"State: CONFIGURED"
    ]
    if resolution_notes:
        lines.append("\nSession resolution:")
        lines.extend(f"- {note}" for note in resolution_notes)
    return "\n".join(lines)


def _cmd_validate() -> str:
    """Validate current registration and lease."""
    state = load_state()
    reg = state.get("registration")
    if not reg:
        return "No registration found. Use /autopilot register first."

    lines = ["=== Validation ==="]

    # Validate workspace
    valid, path, err = validate_workspace_path(reg.get("workspace_root", ""))
    lines.append(f"Workspace: {'OK' if valid else 'FAIL'} — {path}")
    if not valid:
        lines.append(f"  Error: {err}")

    # Validate lease
    lease = state.get("lease")
    if lease:
        valid_lease, err = validate_lease_expired(validate_lease(lease))
        lines.append(f"Lease: {'OK' if valid_lease else 'EXPIRED/INVALID'}")
        if not valid_lease:
            lines.append(f"  Error: {err}")

        # Validate workspace scope
        ws_valid, ws_err = validate_lease_for_workspace(
            validate_lease(lease), reg.get("workspace_root", "")
        )
        lines.append(f"Lease workspace scope: {'OK' if ws_valid else 'FAIL'}")
        if not ws_valid:
            lines.append(f"  Error: {ws_err}")
    else:
        lines.append("Lease: NOT SET")

    return "\n".join(lines)


def _cmd_projects() -> str:
    """List registered project-scoped autopilot states."""
    projects = list_projects()
    active = get_active_project_id()
    if not projects:
        return "No autopilot projects registered. Use /autopilot register {json} in a Hermes Project."

    lines = ["=== Autopilot Projects ==="]
    for pid in projects:
        marker = "*" if pid == active else " "
        lines.append(f"{marker} {pid}")
    lines.append("\nUse /autopilot use <project_id> to switch the active project.")
    return "\n".join(lines)


def _cmd_use(project_id: str) -> str:
    """Switch the active project-scoped autopilot state."""
    pid = (project_id or "").strip()
    if not pid:
        return "Usage: /autopilot use <project_id>"
    projects = list_projects()
    if pid not in projects:
        return f"Unknown autopilot project: {pid}. Known projects: {projects or 'none'}"
    set_active_project_id(pid)
    state = load_state()
    reg = state.get("registration") or {}
    return (
        f"Active autopilot project set to: {pid}\n"
        f"Workspace: {reg.get('workspace_root', 'unknown')}\n"
        f"State: {current_state_label(state)}"
    )


def _cmd_verify(raw: str) -> str:
    """Configure or inspect the active project's verification profile."""

    state = load_state()
    registration = state.get("registration")
    if not isinstance(registration, dict):
        return "Verification configuration blocked: no active project registration."
    project_id = str(registration.get("project_id", ""))
    workspace_root = str(registration.get("workspace_root", ""))
    action = (raw or "").strip()

    if action in {"", "show"}:
        try:
            profile = load_verification_profile(project_id)
        except ValueError as exc:
            return f"Verification profile is invalid: {exc}"
        if profile is None:
            return (
                f"No verification profile configured for project {project_id}.\n"
                "Use /autopilot verify configure {json}."
            )
        return json.dumps(verification_profile_to_dict(profile), indent=2)

    if action == "validate":
        try:
            profile = load_verification_profile(project_id)
        except ValueError as exc:
            return f"Verification profile validation failed: {exc}"
        if profile is None:
            return f"Verification profile validation failed: none configured for {project_id}."
        return (
            f"Verification profile valid for {project_id}: "
            f"{len(profile.checks)} check(s), workspace={profile.workspace_root}"
        )

    prefix = "configure "
    if not action.startswith(prefix):
        return (
            "Usage: /autopilot verify show | validate | configure {json}"
        )
    try:
        data = json.loads(_json_payload(action[len(prefix):]))
    except json.JSONDecodeError as exc:
        return f"Invalid verification profile JSON: {exc}"
    try:
        profile = validate_verification_profile(
            data,
            registered_project_id=project_id,
            registered_workspace_root=workspace_root,
        )
        save_verification_profile(profile)
    except (TypeError, ValueError) as exc:
        return f"Verification profile validation failed: {exc}"

    log_event(
        "verification_profile_configured",
        current_state_label(state),
        current_state_label(state),
        f"project_id={project_id}, checks={len(profile.checks)}",
        lease_id=str((state.get("lease") or {}).get("lease_id", "")),
    )
    return (
        f"Verification profile configured for {project_id}.\n"
        f"Checks: {', '.join(check.check_id for check in profile.checks)}\n"
        "Commands will run as argv without shell interpretation."
    )


def _cmd_lease(raw: str) -> str:
    """Inspect, request, approve, or load a lease."""
    if raw.startswith("approve"):
        parts = raw.split()
        if len(parts) != 2 or parts[0] != "approve" or parts[1] not in {
            LEASE_PRESET_PHASE2_READONLY,
            LEASE_PRESET_PHASE3_DEVELOPMENT,
            LEASE_PRESET_AUTONOMOUS_DEVELOPMENT,
        }:
            return (
                "Usage: /autopilot lease approve <preset>\n"
                f"Available presets: {LEASE_PRESET_PHASE2_READONLY}, {LEASE_PRESET_PHASE3_DEVELOPMENT}, {LEASE_PRESET_AUTONOMOUS_DEVELOPMENT}.\n"
                "Only fixed presets can be approved by name; capability overrides are rejected."
            )
        return _cmd_lease_approve(parts[1])

    if raw in {"request", "wizard"}:
        return _cmd_lease_request(LEASE_PRESET_PHASE2_READONLY)
    for verb in ("request", "wizard"):
        prefix = f"{verb} "
        if raw.startswith(prefix):
            preset = raw[len(prefix):].strip()
            if preset in {
                LEASE_PRESET_PHASE2_READONLY,
                LEASE_PRESET_PHASE3_DEVELOPMENT,
                LEASE_PRESET_AUTONOMOUS_DEVELOPMENT,
            }:
                return _cmd_lease_request(preset)
            return (
                f"Unknown lease preset. Available presets: "
                f"{LEASE_PRESET_PHASE2_READONLY}, {LEASE_PRESET_PHASE3_DEVELOPMENT}, {LEASE_PRESET_AUTONOMOUS_DEVELOPMENT}"
            )

    if not raw:
        state = load_state()
        lease = state.get("lease")
        if not lease:
            return "No active lease. Use /autopilot lease {json} to set one."
        return json.dumps(lease, indent=2)

    try:
        data = json.loads(_json_payload(raw))
    except json.JSONDecodeError as exc:
        return f"Invalid JSON: {exc}"

    try:
        lev = validate_lease(data)
    except ValueError as exc:
        return f"Lease validation failed: {exc}"

    if lev.is_expired():
        return f"Lease {lev.lease_id} is already expired at {lev.expiry}"

    def mutate(state):
        state["lease"] = lease_to_dict(lev)
        state["max_loop_iterations"] = lev.max_loop_iterations
        state["loop_iteration"] = 0
        if current_state_label(state) == STATE_CONFIGURED:
            transition(state, STATE_LEASE_READY, f"Lease {lev.lease_id} loaded")
        state["last_error"] = None

    mutate_state(mutate)
    log_event("lease_loaded", STATE_CONFIGURED, STATE_LEASE_READY,
              f"lease_id={lev.lease_id}")

    return (
        f"Lease loaded: {lev.lease_id} v{lev.lease_version}\n"
        f"Scope: {lev.scope}\n"
        f"Expiry: {lev.expiry}\n"
        f"Capabilities: {list(lev.granted_capabilities)}\n"
        f"State: LEASE_READY"
    )


def _cmd_lease_approve(preset_name: str) -> str:
    """Create and activate one fixed preset after explicit command approval."""
    state = load_state()
    registration_data = state.get("registration")
    if not isinstance(registration_data, dict):
        return (
            "Lease approval blocked: no active project registration.\n"
            "Register or select the intended project first, then run "
            "/autopilot lease request."
        )

    try:
        registration = validate_registration(registration_data)
    except (TypeError, ValueError) as exc:
        return f"Lease approval blocked: registration validation failed: {exc}"

    valid_workspace, resolved_workspace, workspace_error = validate_workspace_path(
        registration.workspace_root
    )
    if not valid_workspace:
        return f"Lease approval blocked: {workspace_error}"
    if resolved_workspace != registration.workspace_root:
        return (
            "Lease approval blocked: registered workspace is not canonical. "
            "Re-register the resolved workspace before approving a lease."
        )

    try:
        lease = build_lease_preset(
            preset_name,
            project_id=registration.project_id,
            workspace_root=registration.workspace_root,
        )
    except ValueError as exc:
        return f"Lease approval blocked: {exc}"

    state_from = current_state_label(state)

    def mutate(current):
        current["lease"] = lease_to_dict(lease)
        current["max_loop_iterations"] = lease.max_loop_iterations
        current["loop_iteration"] = 0
        if current_state_label(current) == STATE_CONFIGURED:
            transition(
                current,
                STATE_LEASE_READY,
                f"Read-only lease preset {preset_name} approved",
            )
        current["last_error"] = None

    mutate_state(mutate)
    state_to = current_state_label(load_state())
    log_event(
        "lease_preset_approved",
        state_from,
        state_to,
        f"lease_id={lease.lease_id}, preset={preset_name}",
        lease_id=lease.lease_id,
    )

    if preset_name == LEASE_PRESET_PHASE3_DEVELOPMENT:
        title = "Phase 3 controlled development"
        summary = "Workspace writes and local commits may be prepared only for explicitly approved briefs."
    elif preset_name == LEASE_PRESET_AUTONOMOUS_DEVELOPMENT:
        title = "Autonomous development session"
        summary = "Workspace edits and low-risk recommended choices are allowed; commits/push/deploy remain denied."
    else:
        title = "Phase 2 read-only handoff"
        summary = "This lease authorizes read-only brief/handoff preparation only; it does not authorize execution."

    return "\n".join([
        f"=== Lease approved: {title} ===",
        f"Lease ID: {lease.lease_id}",
        f"Project: {lease.project_id}",
        f"Workspace: {lease.workspace_root}",
        f"Created at (UTC): {lease.created_at}",
        f"Expires (UTC): {lease.expiry}",
        f"Capabilities: {list(lease.granted_capabilities)}",
        "Push, external-write, database-write, and deployment permissions remain denied.",
        summary,
    ])


def _cmd_lease_request(preset_name: str = LEASE_PRESET_PHASE2_READONLY) -> str:
    """Preview a fixed lease preset without creating it."""
    state = load_state()
    registration = state.get("registration")
    if not isinstance(registration, dict):
        return (
            "No active project registration. Register or select a project before "
            "requesting a lease."
        )

    now = datetime.now(timezone.utc)
    expiry = now + timedelta(hours=LEASE_PRESET_DURATION_HOURS)

    if preset_name == LEASE_PRESET_PHASE3_DEVELOPMENT:
        title = "Phase 3 controlled development"
        permissions = [
            "[x] workspace.read — read registered workspace context",
            "[x] git.read — inspect repository state and history",
            "[x] workspace.write — prepare approved workspace changes only",
            "[x] git.commit — prepare local commits only when explicitly approved",
            "[x] next-phase — execute one approved phase handoff package",
            "[ ] git.push / merge / release — NOT granted",
            "[ ] deployment / dependencies / database writes — NOT granted",
        ]
    elif preset_name == LEASE_PRESET_AUTONOMOUS_DEVELOPMENT:
        title = "Autonomous development session"
        permissions = [
            "[x] workspace.read — read registered workspace context",
            "[x] git.read — inspect repository state and history",
            "[x] workspace.write — implement approved phase changes in workspace",
            "[x] next-phase — continue approved Discussion→Development loop",
            "[x] user.interaction — auto-select recommended low-risk choices",
            "[x] auto-select recommended low-risk choices — routine only",
            "[ ] git.commit — NOT granted",
            "[ ] git.push / merge / release — NOT granted",
            "[ ] deployment / dependencies / database writes — NOT granted",
        ]
    else:
        title = "Phase 2 read-only handoff"
        permissions = [
            "[x] workspace.read — read registered workspace context",
            "[x] git.read — inspect repository state and history",
            "[ ] workspace.write — NOT granted",
            "[ ] git.commit — NOT granted",
            "[ ] git.push / merge / release — NOT granted",
            "[ ] dependencies / database writes / deployment — NOT granted",
        ]

    return "\n".join([
        f"=== Lease Request: {title} ===",
        f"Preset: {preset_name}",
        f"Project: {registration.get('project_id', '')}",
        f"Workspace: {registration.get('workspace_root', '')}",
        f"Created at (UTC): {now.isoformat()}",
        f"Expires (UTC): {expiry.isoformat()}",
        f"Duration: {LEASE_PRESET_DURATION_HOURS} hours",
        f"Issuer: {LEASE_PRESET_ISSUER}",
        "",
        "Permissions:",
        *permissions,
        "",
        "Hermes plugin slash commands accept text only; real UI checkboxes are not available.",
        "No lease has been created and no capability has been granted.",
        "To approve this exact preset, copy and paste:",
        f"/autopilot lease approve {preset_name}",
    ])


def _cmd_phases() -> str:
    """Show the full phase roadmap and remaining phases."""
    return phase_report()


def _cmd_readiness() -> str:
    """Show active project readiness for each phase."""
    return readiness_report(load_state())


def _cmd_start() -> str:
    """Begin the guided Autopilot flow for the active project."""
    state = load_state()
    registration = state.get("registration")
    if not isinstance(registration, dict):
        return (
            "No active project registration.\n"
            "First register the project, then run /autopilot start again."
        )

    lease = state.get("lease")
    if not isinstance(lease, dict):
        return _cmd_lease_request()

    return "\n".join([
        f"=== Autopilot Start: {registration.get('project_id', 'unknown')} ===",
        "A lease is already active.",
        "Next safe Phase 2 commands:",
        "1. /autopilot readiness",
        "2. /autopilot brief",
        "3. /autopilot handoff",
        "",
        "Reminder: Phase 2 creates a read-only Development handoff brief.",
        "It does not edit files, commit code, push, deploy, or message another chat by itself.",
    ])


def _cmd_execute() -> str:
    """Attempt to enter real execution.

    Phase 2: uses the execution bridge to check readiness and generate
    a development execution brief. The brief is a handoff artifact — it
    does NOT perform autonomous execution. Actual coding remains gated
    behind explicit Phase 3 lease and adapter authorization.

    The command:
    1. Checks Phase 2 readiness (lease, capabilities, adapter)
    2. Generates a development execution brief
    3. Persists the brief as an audit artifact
    4. Reports the handoff status

    Fail-closed: if the bridge is unavailable, capabilities are missing,
    or the lease is invalid, execution is refused.
    """
    state = load_state()

    bridge = _get_execution_bridge()
    adapter_available = bridge is not None

    ready, blockers = readiness_for_phase(state, PHASE_2, real_adapter_available=adapter_available)
    if not ready:
        lines = [
            "Real execution is not enabled.",
            "Phase 2 readiness blockers:",
        ]
        lines.extend(f"- {b}" for b in blockers)
        lines.append("Use /autopilot simulate for Phase 1 offline operation.")
        return "\n".join(lines)

    if bridge is None:
        return "Real execution adapter not installed. Refusing to execute."

    # Generate a development execution brief
    result = bridge.generate_brief(state)

    if not result.success:
        lines = [
            "Execution brief generation failed.",
            "Blockers:",
        ]
        lines.extend(f"- {b}" for b in result.blockers)
        return "\n".join(lines)

    brief = result.brief
    if brief is None:
        return "Execution brief generation succeeded but no brief was produced."

    lines = [
        "=== Phase 2 Execution Brief Generated ===",
        f"Brief ID: {brief.brief_id}",
        f"Project: {brief.project_id}",
        f"Workspace: {brief.workspace_root}",
        f"Lease: {brief.lease_id} (expires: {brief.lease_expiry})",
        f"Tasks: {len(brief.tasks)}",
        f"Human gate required: {brief.human_gate_required}",
        f"Execution authorized: {brief.execution_authorized}",
        "",
    ]

    if brief.tasks:
        lines.append("Tasks:")
        for task in brief.tasks:
            lines.append(f"  [{task.priority.upper()}] {task.title} (risk: {task.risk_level})")
            if task.acceptance_criteria:
                for criterion in task.acceptance_criteria:
                    lines.append(f"    ✓ {criterion}")
        lines.append("")

    if result.artifact_path:
        lines.append(f"Brief artifact: {result.artifact_path}")

    if result.warnings:
        lines.append("\nWarnings:")
        lines.extend(f"  ⚠ {w}" for w in result.warnings)

    lines.extend([
        "",
        "NOTE: This is a handoff brief, not an execution directive.",
        "Actual coding requires explicit Phase 3 lease authorization.",
        "The brief is persisted as an audit artifact for human review.",
    ])

    return "\n".join(lines)


def _cmd_brief(raw: str) -> str:
    """Generate a development execution brief from project context.

    Usage:
        /autopilot brief                    — generate brief with default tasks
        /autopilot brief '{"scope":"..."}'  — generate brief with custom scope
        /autopilot brief --list             — list existing briefs
        /autopilot brief --read <brief_id>  — read a specific brief

    The brief is a READ-ONLY handoff artifact. It packages project context,
    session metadata, lease authorization, and task list into a structured
    JSON document persisted under the project state directory.

    This does NOT perform any execution, file editing, or git operations.
    """
    bridge = _get_execution_bridge()
    if bridge is None:
        return (
            "Phase 2 execution bridge not available.\n"
            "Use /autopilot simulate for Phase 1 offline operation."
        )

    # Handle sub-commands
    if raw == "--list" or raw == "list":
        state = load_state()
        reg = state.get("registration")
        if not reg:
            return "No project registered. Use /autopilot register first."
        project_id = reg.get("project_id", "")
        briefs = bridge.list_briefs(project_id)
        if not briefs:
            return f"No briefs found for project {project_id}."
        lines = [f"=== Briefs for {project_id} ==="]
        for b in briefs:
            lines.append(
                f"  {b['brief_id']} v{b['brief_version']} "
                f"({b['task_count']} tasks, {b['created_at']})"
            )
        return "\n".join(lines)

    if raw.startswith("--read ") or raw.startswith("read "):
        brief_id = raw.split(None, 1)[1].strip() if raw.startswith("--read ") else raw.split(None, 1)[1].strip() if " " in raw else ""
        if not brief_id:
            return "Usage: /autopilot brief --read <brief_id>"
        state = load_state()
        reg = state.get("registration")
        if not reg:
            return "No project registered."
        project_id = reg.get("project_id", "")
        brief = bridge.read_brief(project_id, brief_id)
        if brief is None:
            return f"Brief {brief_id} not found for project {project_id}."
        return json.dumps(brief.to_dict(), indent=2)

    # Generate a new brief
    state = load_state()

    # Parse optional scope/notes from JSON payload
    scope = ""
    notes = ""
    tasks = None
    if raw:
        try:
            data = json.loads(_json_payload(raw))
            scope = str(data.get("scope", ""))
            notes = str(data.get("notes", ""))
            tasks = data.get("tasks")
            if tasks and not isinstance(tasks, list):
                return "Invalid 'tasks' field — must be a list of task objects."
        except json.JSONDecodeError:
            # Treat raw text as scope
            scope = raw

    result = bridge.generate_brief(state, tasks=tasks, scope=scope, notes=notes)

    if not result.success:
        lines = ["Brief generation failed.", "Blockers:"]
        lines.extend(f"- {b}" for b in result.blockers)
        return "\n".join(lines)

    brief = result.brief
    if brief is None:
        return "Brief generation succeeded but no brief was produced."

    lines = [
        f"=== Development Execution Brief ===",
        f"Brief ID: {brief.brief_id}",
        f"Project: {brief.project_id}",
        f"Workspace: {brief.workspace_root}",
        f"Discussion session: {brief.discussion_session_id}",
        f"Development session: {brief.development_session_id}",
        f"Lease: {brief.lease_id} (expires: {brief.lease_expiry})",
        f"Capabilities: {list(brief.granted_capabilities)}",
        f"Tasks: {len(brief.tasks)}",
        f"Human gate required: {brief.human_gate_required}",
        f"Execution authorized: {brief.execution_authorized}",
    ]

    if brief.tasks:
        lines.append("\nTasks:")
        for task in brief.tasks:
            lines.append(f"  [{task.priority.upper()}] {task.title} (risk: {task.risk_level})")
            if task.acceptance_criteria:
                for criterion in task.acceptance_criteria:
                    lines.append(f"    ✓ {criterion}")

    if result.artifact_path:
        lines.append(f"\nBrief artifact: {result.artifact_path}")

    if result.warnings:
        lines.append("\nWarnings:")
        lines.extend(f"  ⚠ {w}" for w in result.warnings)

    lines.extend([
        "",
        "The brief is a READ-ONLY handoff artifact.",
        "It does NOT authorize autonomous execution.",
    ])

    return "\n".join(lines)


def _cmd_handoff(raw: str) -> str:
    """Validate readiness for a Discussion→Development handoff.

    Usage:
        /autopilot handoff                — validate handoff readiness
        /autopilot handoff --validate     — same as above
        /autopilot handoff --brief <id>   — validate a specific brief for execution

    This is a READ-ONLY validation command. It checks:
    - Phase 2 readiness (lease, capabilities, adapter)
    - Brief integrity and project match
    - Human gate status
    - Execution authorization status

    It does NOT perform any execution.
    """
    bridge = _get_execution_bridge()
    if bridge is None:
        return (
            "Phase 2 execution bridge not available.\n"
            "Use /autopilot simulate for Phase 1 offline operation."
        )

    state = load_state()
    reg = state.get("registration")
    if not reg:
        return "No project registered. Use /autopilot register first."

    # Default: validate overall handoff readiness
    ready, blockers = bridge.validate_readiness(state)

    lines = [
        f"=== Handoff Readiness: {reg.get('project_id', 'unknown')} ===",
        f"Status: {'READY' if ready else 'BLOCKED'}",
    ]

    if blockers:
        lines.append("\nBlockers:")
        lines.extend(f"  - {b}" for b in blockers)

    # If a specific brief ID is provided, validate it too
    brief_id = ""
    if raw.startswith("--brief "):
        brief_id = raw.split(None, 1)[1].strip() if " " in raw else ""
    elif raw.startswith("brief "):
        brief_id = raw.split(None, 1)[1].strip()

    if brief_id:
        project_id = reg.get("project_id", "")
        brief = bridge.read_brief(project_id, brief_id)
        if brief is None:
            lines.append(f"\nBrief {brief_id} not found for project {project_id}.")
        else:
            valid, brief_blockers = bridge.validate_brief_for_execution(brief, state)
            lines.append(f"\nBrief {brief_id}: {'VALID' if valid else 'INVALID'}")
            if brief_blockers:
                lines.append("Brief blockers:")
                lines.extend(f"  - {b}" for b in brief_blockers)
            lines.append(f"  Human gate required: {brief.human_gate_required}")
            lines.append(f"  Execution authorized: {brief.execution_authorized}")

    # Show existing briefs
    project_id = reg.get("project_id", "")
    briefs = bridge.list_briefs(project_id)
    if briefs:
        lines.append(f"\nExisting briefs ({len(briefs)}):")
        for b in briefs:
            lines.append(f"  {b['brief_id']} v{b['brief_version']} ({b['task_count']} tasks)")

    lines.extend([
        "",
        "NOTE: This is a READ-ONLY validation. No execution is performed.",
        "A valid brief with human_gate_required=True requires explicit",
        "human authorization before any coding begins.",
    ])

    return "\n".join(lines)


def _cmd_authorize_brief(brief_id: str, authorized: bool) -> str:
    """Approve or revoke a persisted brief for Phase 3 run preparation."""
    bid = (brief_id or "").strip()
    if not bid:
        verb = "approve" if authorized else "revoke"
        return f"Usage: /autopilot {verb} <brief_id>"

    state = load_state()
    reg = state.get("registration") or {}
    project_id = reg.get("project_id", "")
    if not project_id:
        return "No project registered. Use /autopilot register first."

    bridge = _get_execution_bridge()
    if bridge is None:
        return "Phase 2/3 execution bridge not available."

    updated = bridge.set_brief_authorization(project_id, bid, authorized)
    if updated is None:
        return f"Brief {bid} not found for project {project_id}."

    action = "approved" if authorized else "revoked"
    return "\n".join([
        f"Brief {bid} {action} for project {project_id}.",
        f"Execution authorized: {updated.execution_authorized}",
        "Next step: /autopilot run <brief_id>" if authorized else "The brief cannot be run until approved again.",
    ])


def _cmd_run(brief_id: str) -> str:
    """Prepare a controlled Development-session run package for an approved brief."""
    bid = (brief_id or "").strip()
    if not bid:
        return "Usage: /autopilot run <brief_id>"

    state = load_state()
    reg = state.get("registration") or {}
    project_id = reg.get("project_id", "")
    if not project_id:
        return "No project registered. Use /autopilot register first."

    bridge = _get_execution_bridge()
    if bridge is None:
        return "Phase 3 runner blocked: execution bridge not available."

    brief = bridge.read_brief(project_id, bid)
    try:
        from .adapters.runner import DevelopmentRunner
    except ImportError:
        return "Phase 3 runner not installed."

    result = DevelopmentRunner().prepare_run(brief, state)
    if not result.success:
        lines = ["Phase 3 run preparation blocked.", "Blockers:"]
        lines.extend(f"- {blocker}" for blocker in result.blockers)
        return "\n".join(lines)

    package = result.package
    if package is None:
        return "Phase 3 run preparation failed: no package produced."

    return "\n".join([
        "=== Phase 3 Development Run Prepared ===",
        f"Run ID: {package.run_id}",
        f"Brief ID: {package.brief_id}",
        f"Project: {package.project_id}",
        f"Workspace: {package.workspace_root}",
        f"Execution mode: {package.execution_mode}",
        f"Status: {package.status}",
        "Autonomous file editing has NOT started.",
        "Use the run artifact prompt in the Development session for controlled implementation.",
        f"Run artifact: {result.artifact_path}",
    ])


def _cmd_loop(raw: str, *, runtime: Any | None = None) -> str:
    """Manage supervised autonomous development loops."""
    args = (raw or "").strip()
    state = load_state()
    registration = state.get("registration") or {}
    project_id = registration.get("project_id", "")
    if not project_id:
        return "No project registered. Use /autopilot register first."

    try:
        from .adapters.autonomous_loop import AutonomousLoopSupervisor
    except ImportError:
        return "Autonomous loop supervisor not installed."

    supervisor = AutonomousLoopSupervisor()
    if not args or args == "status":
        loops = supervisor.list_loops(project_id)
        if not loops:
            return f"=== Autonomous Loops: {project_id} ===\nNo loops found."
        lines = [f"=== Autonomous Loops: {project_id} ==="]
        for item in loops:
            lines.append(
                f"- {item.get('loop_id', '')}: {item.get('status', '')} "
                f"brief={item.get('brief_id', '')} mode={item.get('mode', '')}"
            )
        return "\n".join(lines)

    if args.startswith("answer "):
        parts = args.split(maxsplit=3)
        if len(parts) != 4:
            return "Usage: /autopilot loop answer <loop_id> <question_id> <answer>"
        _, loop_id, question_id, answer = parts
        if runtime is None:
            return "Loop answer requires the Hermes plugin runtime."
        loop = next(
            (
                item for item in supervisor.list_loops(project_id)
                if item.get("loop_id") == loop_id
            ),
            None,
        )
        if loop is None:
            return f"Autonomous loop {loop_id} not found."
        try:
            staged = supervisor.stage_question_answer(
                project_id=project_id,
                loop_id=loop_id,
                question_id=question_id,
                answer=answer,
            )
        except ValueError as exc:
            return f"Loop answer blocked: {exc}"
        task_id = staged.get("task_id")
        board = loop.get("board_slug")
        if not all(isinstance(value, str) and value for value in (task_id, board)):
            return "Loop answer blocked: pending decision task binding is invalid."
        reason = f"Human answer to {question_id}: {staged['human_answer']}"
        resumed = runtime.run(
            (
                "hermes", "kanban", "--board", str(board),
                "unblock", "--reason", reason, str(task_id),
            ),
            cwd=None,
            timeout_seconds=60,
        )
        if resumed.exit_code != 0:
            detail = resumed.stderr or resumed.stdout or "unblock failed"
            return (
                "Human answer was staged, but the task was not resumed. "
                f"Retry the same command after checking Kanban: {detail}"
            )
        try:
            finalized = supervisor.finalize_question_answer(
                project_id=project_id,
                loop_id=loop_id,
                question_id=question_id,
            )
        except ValueError as exc:
            return f"Task was unblocked, but decision finalization needs recovery: {exc}"
        return "\n".join([
            "=== Autopilot Decision Answered ===",
            "Human answer recorded; the blocked task was resumed.",
            f"Loop ID: {loop_id}",
            f"Question: {question_id}",
            f"Task resumed: {task_id}",
            f"Decision artifact: {finalized.get('artifact_path', '')}",
        ])

    if args.startswith("report "):
        loop_id = args[len("report "):].strip()
        if not loop_id:
            return "Usage: /autopilot loop report <loop_id>"
        loop = next(
            (
                item for item in supervisor.list_loops(project_id)
                if item.get("loop_id") == loop_id
            ),
            None,
        )
        if loop is None:
            return f"Autonomous loop {loop_id} not found."
        lines = [
            "=== Autonomous Loop Report ===",
            f"Loop ID: {loop_id}",
            f"Project: {project_id}",
            f"Brief: {loop.get('brief_id', '')}",
            f"Status: {loop.get('status', '')}",
            f"Starting revision: {loop.get('starting_revision', '') or 'not dispatched'}",
            f"Source workspace was dirty at dispatch: {'yes' if loop.get('dirty_workspace') else 'no'}",
            f"Development task: {loop.get('development_task_id', '') or 'not dispatched'}",
            f"Current remediation task: {loop.get('current_remediation_task_id', '') or 'none'}",
            f"Verifier task: {loop.get('verifier_task_id', '') or 'not dispatched'}",
            f"Remediation cycles used: {loop.get('remediation_count', 0)}",
            f"Structured decisions: {loop.get('decision_count', 0)}",
            f"Pending human decision: {loop.get('pending_decision_artifact_path', '') or 'none'}",
            f"Validated evidence: {loop.get('result_artifact_path', '') or 'not available'}",
            f"Human acceptance: {loop.get('acceptance_artifact_path', '') or 'not accepted'}",
            f"Checkpoint: {loop.get('checkpoint_artifact_path', '') or 'not created'}",
            f"Commit authorization: {loop.get('commit_authorization_path', '') or 'not authorized'}",
            f"Isolated commit revision: {loop.get('commit_revision', '') or 'not committed'}",
        ]
        if loop.get("status") == "AWAITING_HUMAN_ACCEPTANCE":
            lines.append(f"Next action: /autopilot loop accept {loop_id}")
        return "\n".join(lines)

    for operation, prefix in (
        ("checkpoint", "checkpoint "),
        ("authorize-commit", "authorize-commit "),
        ("commit", "commit "),
    ):
        if not args.startswith(prefix):
            continue
        loop_id = args[len(prefix):].strip()
        if not loop_id:
            return f"Usage: /autopilot loop {operation} <loop_id>"
        if runtime is None:
            return f"Loop {operation} requires the Hermes plugin runtime."
        loop = next(
            (
                item for item in supervisor.list_loops(project_id)
                if item.get("loop_id") == loop_id
            ),
            None,
        )
        if loop is None:
            return f"Autonomous loop {loop_id} not found."
        from .checkpoint import CheckpointManager

        manager = CheckpointManager(runtime, supervisor=supervisor)
        if operation == "checkpoint":
            result = manager.create(loop=loop)
            heading = "Immutable checkpoint"
        elif operation == "authorize-commit":
            result = manager.authorize_commit(loop=loop)
            heading = "One-time commit authorization"
        else:
            result = manager.commit(loop=loop)
            heading = "Isolated worktree commit"
        if not result.success:
            lines = [f"{heading} blocked:"]
            lines.extend(f"- {blocker}" for blocker in result.blockers)
            return "\n".join(lines)
        lines = [f"=== {heading} created ===", f"Loop ID: {loop_id}"]
        if result.artifact_path:
            lines.append(f"Artifact: {result.artifact_path}")
        if result.revision:
            lines.append(f"Commit revision: {result.revision}")
        if operation == "checkpoint":
            lines.append(
                f"Next action, if a local commit is desired: /autopilot loop authorize-commit {loop_id}"
            )
        elif operation == "authorize-commit":
            lines.extend([
                "Authorization expires in 15 minutes and may be used once.",
                f"A second explicit action is required: /autopilot loop commit {loop_id}",
            ])
        else:
            lines.append("No push, merge, deployment, migration, or source-workspace mutation was performed.")
        return "\n".join(lines)

    if args.startswith("sync "):
        loop_id = args[len("sync "):].strip()
        if not loop_id:
            return "Usage: /autopilot loop sync <loop_id>"
        if runtime is None:
            return "Loop sync requires the Hermes plugin runtime."
        loop = next(
            (
                item for item in supervisor.list_loops(project_id)
                if item.get("loop_id") == loop_id
            ),
            None,
        )
        if loop is None:
            return f"Autonomous loop {loop_id} not found."
        try:
            profile = load_verification_profile(project_id)
        except ValueError as exc:
            return f"Loop sync blocked: verification profile is invalid: {exc}"
        if profile is None:
            return "Loop sync blocked: no project verification profile is configured."
        from .adapters.loop_reconciler import LoopReconciler

        synced = LoopReconciler(runtime, supervisor).sync(
            loop=loop,
            state=state,
            profile=profile,
        )
        lines = [
            "=== Autonomous Loop Reconciled ===",
            f"Loop ID: {loop_id}",
            f"Status: {synced.status}",
            synced.message,
        ]
        if synced.evidence_path:
            lines.append(f"Evidence: {synced.evidence_path}")
        if synced.blockers:
            lines.append("Blockers:")
            lines.extend(f"- {blocker}" for blocker in synced.blockers)
        return "\n".join(lines)

    if args.startswith("accept "):
        loop_id = args[len("accept "):].strip()
        if not loop_id:
            return "Usage: /autopilot loop accept <loop_id>"
        try:
            acceptance_path = supervisor.accept_loop(
                project_id=project_id,
                loop_id=loop_id,
                accepted_by="human:/autopilot loop accept",
            )
        except ValueError as exc:
            return f"Loop acceptance blocked: {exc}"
        return "\n".join([
            "=== Autonomous Loop Accepted by Human ===",
            f"Loop ID: {loop_id}",
            "Status: ACCEPTED",
            f"Acceptance record: {acceptance_path}",
            "No commit, merge, push, deployment, or migration was performed by acceptance.",
        ])

    if args.startswith("start "):
        brief_id = args[len("start "):].strip()
        if not brief_id:
            return "Usage: /autopilot loop start <brief_id>"
        bridge = _get_execution_bridge()
        if bridge is None:
            return "Autonomous loop blocked: execution bridge not available."
        brief = bridge.read_brief(project_id, brief_id)
        profile = None
        if runtime is not None:
            try:
                profile = load_verification_profile(project_id)
            except ValueError as exc:
                return f"Autonomous loop blocked: verification profile is invalid: {exc}"
            if profile is None:
                return (
                    "Autonomous loop blocked: no project verification profile is configured.\n"
                    "Run /autopilot verify configure {json} first."
                )
        result = supervisor.start_loop(brief, state)
        if not result.success:
            lines = ["Autonomous loop start blocked.", "Blockers:"]
            lines.extend(f"- {blocker}" for blocker in result.blockers)
            return "\n".join(lines)
        loop = result.loop
        if loop is None:
            return "Autonomous loop start failed: no loop artifact produced."
        if runtime is None:
            return "\n".join([
                "=== Autonomous Development Loop Started ===",
                f"Loop ID: {loop.loop_id}",
                f"Brief ID: {loop.brief_id}",
                f"Project: {loop.project_id}",
                f"Workspace: {loop.workspace_root}",
                f"Mode: {loop.mode}",
                f"Status: {loop.status}",
                f"Auto-answer policy: {loop.auto_answer_policy}",
                "Development executor runtime is unavailable in this direct invocation.",
                f"Loop artifact: {result.artifact_path}",
            ])

        from .adapters.development_executor import DevelopmentExecutor

        assert profile is not None
        assert brief is not None
        dispatch = DevelopmentExecutor(runtime, supervisor=supervisor).dispatch(
            loop=loop,
            brief=brief,
            state=state,
            profile=profile,
        )
        if not dispatch.success:
            supervisor.mark_dispatch_blocked(project_id=project_id, loop_id=loop.loop_id)
            lines = [
                "Autonomous loop dispatch blocked.",
                f"Loop ID: {loop.loop_id}",
                "No Development worker was released unless a task id is shown below.",
                "Blockers:",
            ]
            lines.extend(f"- {blocker}" for blocker in dispatch.blockers)
            if dispatch.development_task_id:
                lines.append(f"Development task (blocked): {dispatch.development_task_id}")
            if dispatch.verifier_task_id:
                lines.append(f"Verifier task: {dispatch.verifier_task_id}")
            return "\n".join(lines)

        updated = next(
            (
                item for item in supervisor.list_loops(project_id)
                if item.get("loop_id") == loop.loop_id
            ),
            None,
        )
        expected_binding = {
            "board_slug": dispatch.board_slug,
            "development_task_id": dispatch.development_task_id,
            "verifier_task_id": dispatch.verifier_task_id,
            "starting_revision": dispatch.starting_revision,
            "verification_profile_digest": dispatch.verification_profile_digest,
            "source_status_digest": dispatch.source_status_digest,
            "dirty_workspace": dispatch.dirty_workspace,
        }
        if updated is None or any(
            updated.get(key) != value for key, value in expected_binding.items()
        ):
            return (
                "Autonomous loop dispatch failed closed: Kanban tasks were created but "
                "their durable policy binding could not be verified. Stop the tasks from the Kanban board."
            )
        current_status = str(updated.get("status", "QUEUED"))
        lines = [
            "=== Autonomous Development Loop Dispatched ===",
            f"Loop ID: {loop.loop_id}",
            f"Brief ID: {loop.brief_id}",
            f"Project: {loop.project_id}",
            f"Workspace: {loop.workspace_root}",
            f"Status: {current_status}",
            f"Kanban board: {dispatch.board_slug}",
            f"Development task: {dispatch.development_task_id}",
            f"Verifier task: {dispatch.verifier_task_id}",
            f"Starting revision: {dispatch.starting_revision}",
            "The main workspace is preserved; work runs in a project-linked Git worktree.",
            "Use /autopilot loop sync <loop_id> to import verifier evidence.",
            f"Loop artifact: {result.artifact_path}",
        ]
        if dispatch.warnings:
            lines.append("Warnings:")
            lines.extend(f"- {warning}" for warning in dispatch.warnings)
        return "\n".join(lines)

    if args.startswith("stop "):
        loop_id = args[len("stop "):].strip()
        if not loop_id:
            return "Usage: /autopilot loop stop <loop_id>"
        if runtime is None:
            if supervisor.stop_loop(project_id, loop_id):
                return f"Autonomous loop {loop_id} stopped."
            return f"Autonomous loop {loop_id} not found."
        loop = next(
            (
                item for item in supervisor.list_loops(project_id)
                if item.get("loop_id") == loop_id
            ),
            None,
        )
        if loop is None:
            return f"Autonomous loop {loop_id} not found."
        current_status = str(loop.get("status", ""))
        terminal_statuses = {
            "CANCELED",
            "TIMED_OUT",
            "ACCEPTED",
            "DISPATCH_BLOCKED",
            "STOPPED",
        }
        if current_status in terminal_statuses:
            return (
                f"Autonomous loop {loop_id} is already terminal: "
                f"{current_status}. No Kanban cancellation was required."
            )
        from .adapters.loop_reconciler import LoopReconciler

        canceled = LoopReconciler(runtime, supervisor).cancel(
            loop=loop,
            reason="Human requested Autopilot loop stop",
        )
        lines = [
            f"Autonomous loop {loop_id} cancellation status: {canceled.status}.",
            canceled.message,
        ]
        if canceled.blockers:
            lines.append("Unconfirmed tasks:")
            lines.extend(f"- {blocker}" for blocker in canceled.blockers)
        return "\n".join(lines)

    return "Usage: /autopilot loop status | start <brief_id> | sync/report/accept/checkpoint/authorize-commit/commit/stop <loop_id>"


def _cmd_simulate() -> str:
    """Run a simulation — Phase 1 only execution path."""
    state = load_state()
    label = current_state_label(state)

    # Must be in LEASE_READY, CONFIGURED, or PHASE_ACCEPTED state for the active project
    if label not in (STATE_LEASE_READY, STATE_CONFIGURED, STATE_PHASE_ACCEPTED):
        return (
            f"Cannot simulate in state {label}. "
            "Must be in LEASE_READY, CONFIGURED, or PHASE_ACCEPTED."
        )

    # Check kill switch
    kill_reason = check_kill_switch()
    if kill_reason:
        return f"Cannot simulate — kill switch active: {kill_reason}"

    # Transition to SIMULATING
    if label == STATE_LEASE_READY:
        transition(state, STATE_SIMULATING, "Simulation started")
    else:
        transition(state, STATE_SIMULATING, "Simulation started (no lease)")

    save_state(state)
    log_event("simulation_start", label, STATE_SIMULATING)

    # Run simulation
    adapter = SimulationAdapter(max_remediation=state.get("max_loop_iterations", 1))
    result = adapter.run_simulation(
        registration=state.get("registration"),
        lease=state.get("lease"),
    )

    # Walk through the simulated state transitions
    simulation_transitions = [
        (STATE_PREPARING_BRIEF, "Planner completed"),
        (STATE_EXECUTING, "Developer completed"),
        (STATE_VERIFYING, "Verifier completed"),
        (STATE_REVIEWING, "Reviewer completed"),
    ]

    for target, reason in simulation_transitions:
        try:
            transition(state, target, reason)
            save_state(state)
        except ValueError:
            pass

    if result.remediation_used:
        try:
            transition(state, STATE_REMEDIATING, "Remediation applied")
            save_state(state)
            transition(state, STATE_VERIFYING, "Re-verification after remediation")
            save_state(state)
            transition(state, STATE_REVIEWING, "Re-review after remediation")
            save_state(state)
        except ValueError:
            pass

    if result.accepted:
        try:
            transition(state, STATE_PHASE_ACCEPTED, "Simulation accepted")
            increment_loop(state)
            state["run_count"] = state.get("run_count", 0) + 1
            save_state(state)
        except ValueError:
            pass

    log_event("simulation_complete", STATE_SIMULATING,
              current_state_label(state),
              f"accepted={result.accepted}, steps={result.total_steps}")

    # Build response
    lines = [
        "=== Simulation Result (Phase 1) ===",
        f"Steps: {result.total_steps}",
        f"Accepted: {result.accepted}",
        f"Remediation used: {result.remediation_used} ({result.remediation_count} cycles)",
    ]
    for i, step in enumerate(result.steps, 1):
        status = "PASS" if step.success else "FAIL"
        lines.append(f"  {i}. [{status}] {step.role}: {step.action} -> {step.result}")

    lines.append(f"\nFinal state: {current_state_label(state)}")
    return "\n".join(lines)


def _cmd_off() -> str:
    """Gracefully turn off the autopilot."""
    state = load_state()
    label = current_state_label(state)

    # Phase 1: /autopilot on is not available, so off is mostly informational
    if label in (STATE_IDLE, STATE_STOPPED):
        return f"Autopilot is already {label}."

    try:
        transition(state, STATE_IDLE, "Graceful shutdown via /autopilot off")
        save_state(state)
    except ValueError:
        # Force to IDLE if in a stuck state
        state["state"] = STATE_IDLE
        state["transition_history"] = state.get("transition_history", [])
        save_state(state)

    log_event("off", label, STATE_IDLE, "Graceful shutdown")
    return "Autopilot turned off. State reset to IDLE."


def _cmd_stop(*, runtime: Any | None = None) -> str:
    """Emergency stop — persist kill state, then cancel bound workers."""
    activate_kill_switch("Emergency stop via /autopilot stop")

    try:
        state = load_state()
        label = current_state_label(state)
        state["state"] = STATE_STOPPED
        state["kill_switch_active"] = True
        save_state(state)
        log_event("stop", label, STATE_STOPPED, "Emergency stop + kill switch")
    except Exception:
        # Even if state is corrupted, the independent kill file is authoritative.
        pass

    lines = [
        "EMERGENCY STOP: Kill switch activated.",
        "New Autopilot tool calls and dispatches are blocked.",
    ]
    if runtime is None:
        lines.append(
            "Running-worker cancellation is unconfirmed because the Hermes plugin runtime is unavailable."
        )
        return "\n".join(lines)

    from .adapters.autonomous_loop import AutonomousLoopSupervisor
    from .adapters.loop_reconciler import LoopReconciler

    cancellations = []
    active_statuses = {"QUEUED", "RUNNING", "VERIFYING", "REMEDIATING", "CANCEL_REQUESTED"}
    supervisor = AutonomousLoopSupervisor()
    for project_id in list_projects():
        for loop in supervisor.list_loops(project_id):
            if loop.get("status") not in active_statuses:
                continue
            result = LoopReconciler(runtime, supervisor).cancel(
                loop=loop,
                reason="Emergency Autopilot kill switch",
            )
            cancellations.append((str(loop.get("loop_id", "")), result))

    if not cancellations:
        lines.append("No active durable worker pipeline was found.")
    else:
        for loop_id, result in cancellations:
            lines.append(f"Loop {loop_id}: {result.status}")
            for blocker in result.blockers:
                lines.append(f"  - {blocker}")
        if all(result.status == "CANCELED" for _, result in cancellations):
            lines.append("All active durable worker pipelines were reclaimed and blocked.")
        else:
            lines.append(
                "One or more cancellations remain unconfirmed; keep the kill switch active and inspect Kanban."
            )
    lines.append("Reset requires an explicit kill-switch reset after reviewing all task states.")
    return "\n".join(lines)


def _cmd_help() -> str:
    """Show help for autopilot commands."""
    return (
        "=== Project Autopilot Commands — Shipped Phases 1–4 ===\n"
        "/autopilot status | projects | use <project_id>\n"
        "/autopilot register {json} | validate | phases | readiness\n"
        "/autopilot lease request [phase2-readonly|phase3-development|autonomous-development]\n"
        "/autopilot lease approve <preset> | lease [json]\n"
        "/autopilot verify configure {json} | verify show | verify validate\n"
        "/autopilot brief [json] | handoff [--brief <id>]\n"
        "/autopilot approve <brief_id> | revoke <brief_id>\n"
        "/autopilot execute | run <brief_id> — read-only/legacy package flows; no live worker\n"
        "/autopilot loop start <brief_id> — dispatch durable Development + verifier workers\n"
        "/autopilot loop status | sync/report/accept/checkpoint/authorize-commit/commit/stop <loop_id>\n"
        "/autopilot loop answer <loop_id> <question_id> <answer> — resume a structured human decision\n"
        "/autopilot simulate | off | stop | help\n"
        "\n"
        "Live loops require project registration, an approved brief, exact verification profile, "
        "autonomous-development lease, gateway, and human acceptance.\n"
        "Workers cannot commit/push/deploy. Optional commit requires checkpoint, separate one-time "
        "authorization, and a second explicit commit command; it remains local to the isolated worktree."
    )
