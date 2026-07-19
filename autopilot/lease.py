"""Strict versioned AutonomyLease.

An AutonomyLease is an immutable, time-bound, capability-scoped authorization
that constrains what the autopilot may do.

Key properties:
- Versioned: lease_version must be >= 1
- Expiry: must have a future expiry
- Capabilities: explicit grant list (default-deny for anything not listed)
- Self-expansion is denied at the object level
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

from .constants import (
    LEASE_PRESET_DURATION_HOURS,
    LEASE_PRESET_ISSUER,
    LEASE_PRESET_AUTONOMOUS_DEVELOPMENT,
    LEASE_PRESET_PHASE2_READONLY,
    LEASE_PRESET_PHASE3_DEVELOPMENT,
    AUTONOMOUS_DEVELOPMENT_CAPABILITIES,
    PHASE2_READONLY_CAPABILITIES,
    PHASE3_DEVELOPMENT_CAPABILITIES,
    VALID_CAPABILITIES,
)


@dataclass(frozen=True)
class AutonomyLease:
    """Immutable autonomy lease."""
    lease_id: str
    lease_version: int
    project_id: str
    scope: str
    created_at: str
    expiry: str
    max_runtime_seconds: int
    max_loop_iterations: int
    granted_capabilities: tuple[str, ...]
    workspace_root: str = ""
    git_policy: str = "read-only"
    dependency_policy: str = "deny"
    local_service_policy: str = "deny"
    database_policy: str = "read-only"
    privileged_account_policy: str = "deny"
    external_write_policy: str = "deny"
    user_interaction_policy: str = "pause-for-human"
    issuer: str = ""
    notes: str = ""

    def is_expired(self) -> bool:
        """Check if the lease has expired."""
        try:
            exp = datetime.fromisoformat(self.expiry.replace("Z", "+00:00"))
            now = datetime.now(timezone.utc)
            return now >= exp
        except Exception:
            return True  # If we can't parse, treat as expired

    def has_capability(self, cap: str) -> bool:
        """Check if a specific capability is granted."""
        return cap in self.granted_capabilities

    def self_expand(self, new_caps: tuple[str, ...]) -> None:
        """Attempt to self-expand capabilities — always denied."""
        raise PermissionError(
            "Self-expansion is denied. Leases are immutable and "
            "must be replaced via /autopilot lease with a new lease."
        )

    def remaining_seconds(self) -> float:
        """Return remaining seconds until expiry (negative if expired)."""
        try:
            exp = datetime.fromisoformat(self.expiry.replace("Z", "+00:00"))
            now = datetime.now(timezone.utc)
            return (exp - now).total_seconds()
        except Exception:
            return -1.0


def build_lease_preset(
    preset_name: str,
    *,
    project_id: str,
    workspace_root: str,
    now: datetime | None = None,
) -> AutonomyLease:
    """Build one immutable, audited lease from a fixed safe preset.

    The command surface deliberately exposes no capability overrides. Adding a
    capability requires a reviewed code change to the named preset.
    """
    if preset_name not in {
        LEASE_PRESET_PHASE2_READONLY,
        LEASE_PRESET_PHASE3_DEVELOPMENT,
        LEASE_PRESET_AUTONOMOUS_DEVELOPMENT,
    }:
        raise ValueError(f"Unknown lease preset: {preset_name!r}")
    if not project_id.strip():
        raise ValueError("An active project_id is required for a lease preset")
    if not workspace_root.strip():
        raise ValueError("An active workspace_root is required for a lease preset")

    created = now or datetime.now(timezone.utc)
    if created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)
    created = created.astimezone(timezone.utc)
    expiry = created + timedelta(hours=LEASE_PRESET_DURATION_HOURS)
    if preset_name == LEASE_PRESET_PHASE2_READONLY:
        lease_prefix = "lease-p2ro"
        scope = "Phase 2 read-only Discussion-to-Development handoff"
        capabilities = PHASE2_READONLY_CAPABILITIES
        git_policy = "read-only"
        notes = (
            "Fixed safe preset: read-only Phase 2 handoff. "
            "No workspace writes, commits, pushes, external writes, or deployment."
        )
    elif preset_name == LEASE_PRESET_PHASE3_DEVELOPMENT:
        lease_prefix = "lease-p3dev"
        scope = "Phase 3 controlled development package preparation"
        capabilities = PHASE3_DEVELOPMENT_CAPABILITIES
        git_policy = "commit"
        notes = (
            "Fixed guarded preset: Phase 3 development preparation. "
            "Workspace writes and local commits may be prepared only for an explicitly approved brief. "
            "Pushes, external writes, database writes, and deployment remain denied."
        )
    else:
        lease_prefix = "lease-auto"
        scope = "Autonomous development session with recommended-choice policy"
        capabilities = AUTONOMOUS_DEVELOPMENT_CAPABILITIES
        git_policy = "allow-list"
        notes = (
            "Fixed guarded preset: autonomous development session. "
            "Allows workspace edits and low-risk recommended-choice handling. "
            "Git add, commit, and push are allowed only during host-controlled "
            "post-verification promotion. Other Git writes, external writes, "
            "database writes, and deployment remain denied."
        )

    lease_id = f"{lease_prefix}-{created.strftime('%Y%m%dT%H%M%S')}-{uuid4().hex[:8]}"

    return AutonomyLease(
        lease_id=lease_id,
        lease_version=1,
        project_id=project_id,
        scope=scope,
        created_at=created.isoformat(),
        expiry=expiry.isoformat(),
        max_runtime_seconds=LEASE_PRESET_DURATION_HOURS * 60 * 60,
        max_loop_iterations=1,
        granted_capabilities=capabilities,
        workspace_root=workspace_root,
        git_policy=git_policy,
        dependency_policy="deny",
        local_service_policy="deny",
        database_policy="read-only",
        privileged_account_policy="deny",
        external_write_policy="deny",
        user_interaction_policy="pause-for-human",
        issuer=LEASE_PRESET_ISSUER,
        notes=notes,
    )


def validate_lease(data: dict[str, Any]) -> AutonomyLease:
    """Validate and create an AutonomyLease from a dict."""
    if not isinstance(data, dict):
        raise ValueError("Lease must be a dict")

    vid = data.get("lease_version", 0)
    if not isinstance(vid, int) or vid < 1:
        raise ValueError("lease_version must be >= 1")

    runtime = data.get("max_runtime_seconds", 0)
    if not isinstance(runtime, int) or runtime < 0:
        raise ValueError("max_runtime_seconds must be >= 0")

    loop = data.get("max_loop_iterations", 0)
    if not isinstance(loop, int) or loop < 0:
        raise ValueError("max_loop_iterations must be >= 0")

    caps = data.get("granted_capabilities", [])
    if not isinstance(caps, list):
        raise ValueError("granted_capabilities must be a list")
    for c in caps:
        if not isinstance(c, str) or not c.strip():
            raise ValueError(f"Invalid capability entry: {c!r}")
    # Validate each cap is in the valid set
    for c in caps:
        if c not in VALID_CAPABILITIES:
            raise ValueError(f"Unknown capability: {c!r}")

    return AutonomyLease(
        lease_id=str(data.get("lease_id", "")),
        lease_version=vid,
        project_id=str(data.get("project_id", "")),
        scope=str(data.get("scope", "")),
        created_at=str(data.get("created_at", "")),
        expiry=str(data.get("expiry", "")),
        max_runtime_seconds=runtime,
        max_loop_iterations=loop,
        granted_capabilities=tuple(caps),
        workspace_root=str(data.get("workspace_root", "")),
        git_policy=str(data.get("git_policy", "read-only")),
        dependency_policy=str(data.get("dependency_policy", "deny")),
        local_service_policy=str(data.get("local_service_policy", "deny")),
        database_policy=str(data.get("database_policy", "read-only")),
        privileged_account_policy=str(data.get("privileged_account_policy", "deny")),
        external_write_policy=str(data.get("external_write_policy", "deny")),
        user_interaction_policy=str(data.get("user_interaction_policy", "pause-for-human")),
        issuer=str(data.get("issuer", "")),
        notes=str(data.get("notes", "")),
    )


def lease_to_dict(lev: AutonomyLease) -> dict[str, Any]:
    """Convert a lease to a plain dict."""
    from dataclasses import asdict
    d = asdict(lev)
    d["granted_capabilities"] = list(d["granted_capabilities"])
    return d


def validate_lease_expired(lev: AutonomyLease) -> tuple[bool, str]:
    """Validate that a lease is not expired.

    Returns (valid, error_message).
    """
    if lev.is_expired():
        return False, f"Lease {lev.lease_id} expired at {lev.expiry}"
    return True, ""
