"""Deterministic risk classifier and default-deny capability engine.

Risk levels:
- LOW: reversible implementation choices → proceed and log
- MEDIUM: requires explicit lease capability → proceed if granted, else pause
- HIGH: business, security, data, privileged, deployment, personal-account,
        next-phase → always pause for human

Model/file/web/chat content cannot override the lease.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from .constants import (
    RISK_LOW, RISK_MEDIUM, RISK_HIGH,
    LOW_RISK_PATTERNS, MEDIUM_RISK_PATTERNS, HIGH_RISK_PATTERNS,
    DEFAULT_DENIED_CAPABILITIES,
)
from .lease import AutonomyLease


@dataclass(frozen=True)
class RiskDecision:
    """Result of a risk classification."""
    level: str  # RISK_LOW, RISK_MEDIUM, RISK_HIGH
    action: str  # "proceed_and_log", "check_capability", "pause_for_human"
    reason: str
    matched_pattern: str = ""


def classify_risk(action_text: str) -> RiskDecision:
    """Classify an action text into a risk level.

    This is deterministic: same input always produces the same output.
    No LLM or model call is made.
    """
    text = action_text.lower().strip()

    # HIGH risk: checked first (highest precedence)
    for pattern in HIGH_RISK_PATTERNS:
        if pattern in text:
            return RiskDecision(
                level=RISK_HIGH,
                action="pause_for_human",
                reason=f"HIGH risk pattern matched: '{pattern}'",
                matched_pattern=pattern,
            )

    # MEDIUM risk
    for pattern in MEDIUM_RISK_PATTERNS:
        if pattern in text:
            return RiskDecision(
                level=RISK_MEDIUM,
                action="check_capability",
                reason=f"MEDIUM risk pattern matched: '{pattern}'",
                matched_pattern=pattern,
            )

    # LOW risk
    for pattern in LOW_RISK_PATTERNS:
        if pattern in text:
            return RiskDecision(
                level=RISK_LOW,
                action="proceed_and_log",
                reason=f"LOW risk pattern matched: '{pattern}'",
                matched_pattern=pattern,
            )

    # Default: unknown → HIGH (fail-closed)
    return RiskDecision(
        level=RISK_HIGH,
        action="pause_for_human",
        reason="Unknown action — default to HIGH risk (fail-closed)",
    )


def check_capability(
    lease: AutonomyLease | None,
    required_cap: str,
) -> tuple[bool, str]:
    """Check if a capability is granted by the lease.

    Returns (granted, message).
    """
    if lease is None:
        return False, "No active lease — capability denied"
    if lease.is_expired():
        return False, f"Lease expired — capability denied"
    if required_cap in lease.granted_capabilities:
        return True, ""
    return False, f"Capability '{required_cap}' not granted by lease"


def is_denied_by_default(cap: str) -> bool:
    """Check if a capability is denied by default."""
    return cap in DEFAULT_DENIED_CAPABILITIES


def evaluate_action(
    action_text: str,
    lease: AutonomyLease | None = None,
    required_cap: str | None = None,
) -> RiskDecision:
    """Full risk evaluation of an action.

    Combines risk classification with capability checking.
    """
    decision = classify_risk(action_text)

    if decision.level == RISK_LOW:
        return decision

    if decision.level == RISK_MEDIUM and required_cap:
        granted, msg = check_capability(lease, required_cap)
        if granted:
            return RiskDecision(
                level=RISK_LOW,
                action="proceed_and_log",
                reason=f"MEDIUM risk but capability '{required_cap}' is granted",
            )
        else:
            return RiskDecision(
                level=RISK_HIGH,
                action="pause_for_human",
                reason=f"MEDIUM risk but {msg}",
            )

    # HIGH risk or MEDIUM without capability → always pause
    if decision.level == RISK_HIGH:
        return decision

    return decision


def validate_lease_for_workspace(
    lease: AutonomyLease,
    workspace_root: str,
) -> tuple[bool, str]:
    """Validate that a lease's workspace scope covers the given workspace.

    Returns (valid, error_message).
    """
    lease_ws = lease.workspace_root.strip()
    if not lease_ws:
        return True, ""  # No restriction = allowed

    from pathlib import Path
    lease_ws_resolved = Path(lease_ws).expanduser().resolve()
    actual_ws_resolved = Path(workspace_root).expanduser().resolve()

    # The actual workspace must be the same as or a subdirectory of the lease workspace
    try:
        actual_ws_resolved.relative_to(lease_ws_resolved)
        return True, ""
    except ValueError:
        return False, (
            f"Workspace '{workspace_root}' is outside lease scope '{lease_ws}'"
        )
