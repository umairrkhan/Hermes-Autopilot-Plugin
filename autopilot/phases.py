"""Project Autopilot shipped-phase roadmap and per-project readiness gates.

Implementation status describes what the plugin ships. Runtime readiness is
still evaluated fail-closed for the active project's registration, lease,
workspace, capabilities, verification profile, and adapters.
"""

from __future__ import annotations

from dataclasses import dataclass, asdict
from typing import Any

from .constants import (
    CAP_WORKSPACE_READ, CAP_GIT_READ,
    CAP_WORKSPACE_WRITE, CAP_GIT_COMMIT,
)
from .lease import validate_lease, validate_lease_expired
from .policy import check_capability, validate_lease_for_workspace
from .registration import validate_workspace_path


def _execution_adapter_available(phase_number: int) -> bool:
    """Check whether the shipped adapter for a phase is importable."""

    try:
        if phase_number <= PHASE_3:
            from .adapters.execution_bridge import ExecutionBridge  # noqa: F401
        else:
            from .adapters.development_executor import DevelopmentExecutor  # noqa: F401
            from .adapters.loop_reconciler import LoopReconciler  # noqa: F401
        return True
    except ImportError:
        return False


PHASE_1 = 1
PHASE_2 = 2
PHASE_3 = 3
PHASE_4 = 4


@dataclass(frozen=True)
class PhaseDefinition:
    """A single roadmap phase definition."""

    number: int
    name: str
    status: str
    description: str
    required_capabilities: tuple[str, ...]
    real_side_effects_allowed: bool

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["required_capabilities"] = list(self.required_capabilities)
        return data


PHASES: tuple[PhaseDefinition, ...] = (
    PhaseDefinition(
        number=PHASE_1,
        name="Foundation / Offline Simulation",
        status="complete",
        description="Project-scoped registration, leases, state machine, kill switch, audit trail, and offline simulation.",
        required_capabilities=(),
        real_side_effects_allowed=False,
    ),
    PhaseDefinition(
        number=PHASE_2,
        name="Guarded Read-Only Handoff",
        status="complete",
        description="Generate and validate Discussion-to-Development handoff briefs without workspace or Git writes.",
        required_capabilities=(CAP_WORKSPACE_READ, CAP_GIT_READ),
        real_side_effects_allowed=False,
    ),
    PhaseDefinition(
        number=PHASE_3,
        name="Controlled Development Run Packages",
        status="complete",
        description="Human-approve a brief and prepare a project-scoped Development-session run package.",
        required_capabilities=(CAP_WORKSPACE_READ, CAP_WORKSPACE_WRITE, CAP_GIT_READ, CAP_GIT_COMMIT),
        real_side_effects_allowed=True,
    ),
    PhaseDefinition(
        number=PHASE_4,
        name="Supervised Durable Development Loop",
        status="complete",
        description="Dispatch lease-gated Kanban workers in isolated worktrees, independently verify, remediate within bounds, require human acceptance, checkpoint, and separately authorize a local commit.",
        required_capabilities=(CAP_WORKSPACE_READ, CAP_GIT_READ, CAP_WORKSPACE_WRITE),
        real_side_effects_allowed=True,
    ),
)


def phase_by_number(number: int) -> PhaseDefinition:
    """Return a phase definition by number."""
    for phase in PHASES:
        if phase.number == number:
            return phase
    raise ValueError(f"Unknown autopilot phase: {number}")


def remaining_phases() -> list[PhaseDefinition]:
    """Return roadmap phases whose implementation is not complete."""
    return [phase for phase in PHASES if phase.status != "complete"]


def phase_report() -> str:
    """Return a human-readable phase roadmap."""
    lines = ["=== Project Autopilot Phase Roadmap ==="]
    for phase in PHASES:
        marker = "✅" if phase.status == "complete" else "🔒"
        caps = ", ".join(phase.required_capabilities) or "none"
        lines.extend([
            f"{marker} Phase {phase.number}: {phase.name} [{phase.status}]",
            f"   {phase.description}",
            f"   Required capabilities: {caps}",
            f"   Real side effects allowed: {phase.real_side_effects_allowed}",
        ])
    lines.append("")
    remaining = remaining_phases()
    if remaining:
        labels = ", ".join(f"Phase {phase.number}" for phase in remaining)
        lines.append(f"Unshipped phases: {len(remaining)} ({labels})")
    else:
        lines.append("Shipped roadmap phases: 4/4. Runtime actions remain lease- and gate-controlled per project.")
    return "\n".join(lines)


def _missing_registration(state: dict[str, Any]) -> str | None:
    reg = state.get("registration")
    if not isinstance(reg, dict):
        return "No project registration. Run /autopilot register first."
    valid, _, err = validate_workspace_path(reg.get("workspace_root", ""))
    if not valid:
        return f"Registered workspace is invalid: {err}"
    return None


def readiness_for_phase(state: dict[str, Any], phase_number: int, *, real_adapter_available: bool | None = None) -> tuple[bool, list[str]]:
    """Evaluate whether the active project can enter a phase.

    Phase 1 is complete once registration exists. Phases 2+ require:
    - valid registration/workspace
    - valid non-expired lease
    - lease project/workspace match
    - all phase capabilities explicitly granted
    - the shipped adapter for the requested phase is installed

    ``real_adapter_available`` may override adapter detection in tests.
    """
    phase = phase_by_number(phase_number)
    blockers: list[str] = []

    reg_error = _missing_registration(state)
    if reg_error:
        blockers.append(reg_error)
        return False, blockers

    if phase.number == PHASE_1:
        return True, blockers

    lease_data = state.get("lease")
    if not isinstance(lease_data, dict):
        blockers.append(
            "No active autonomy lease. Run /autopilot lease request "
            "to review the safe Phase 2 read-only preset."
        )
        return False, blockers

    try:
        lease = validate_lease(lease_data)
    except ValueError as exc:
        blockers.append(f"Lease validation failed: {exc}")
        return False, blockers

    valid, err = validate_lease_expired(lease)
    if not valid:
        blockers.append(err)

    reg = state.get("registration") or {}
    if lease.project_id != reg.get("project_id"):
        blockers.append(f"Lease project_id {lease.project_id!r} does not match active project {reg.get('project_id')!r}.")

    ws_valid, ws_err = validate_lease_for_workspace(lease, reg.get("workspace_root", ""))
    if not ws_valid:
        blockers.append(ws_err)

    for cap in phase.required_capabilities:
        granted, msg = check_capability(lease, cap)
        if not granted:
            blockers.append(msg)

    if real_adapter_available is None:
        real_adapter_available = _execution_adapter_available(phase.number)

    if phase.number >= PHASE_2 and not real_adapter_available:
        blockers.append(f"The shipped Phase {phase.number} adapter is unavailable.")

    return not blockers, blockers


def readiness_report(state: dict[str, Any]) -> str:
    """Return a readiness report for all phases for the active project."""
    project_id = (state.get("registration") or {}).get("project_id", state.get("project_id", "unknown"))
    lines = [f"=== Autopilot Readiness: {project_id} ==="]
    for phase in PHASES:
        ready, blockers = readiness_for_phase(state, phase.number)
        marker = "READY" if ready else "BLOCKED"
        lines.append(f"Phase {phase.number} ({phase.name}): {marker}")
        for blocker in blockers:
            lines.append(f"  - {blocker}")
    return "\n".join(lines)
