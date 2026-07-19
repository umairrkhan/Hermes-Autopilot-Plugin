"""Phase 2 Execution Bridge — guarded Discussion→Development handoff adapter.

This adapter enables Autopilot to prepare a structured development execution
brief from project context and session metadata, without performing uncontrolled
autonomous edits. It is fail-closed: the brief is a handoff artifact, not an
execution directive. Actual coding remains gated behind explicit lease and
adapter authorization.

Architecture:
    ExecutionBridge reads the active project registration, validates autonomy
    lease and capabilities, and generates a DevelopmentExecutionBrief — a
    self-contained JSON artifact that packages what needs to be done, where,
    and under what constraints. The brief is persisted to the project-scoped
    autopilot state directory as an audit trail artifact.

    The bridge does NOT:
    - Execute any file edits or terminal commands
    - Make any LLM/model calls
    - Write to session databases or workspace files
    - Perform any git operations

    It DOES:
    - Read project registration and session metadata
    - Validate lease scope and capabilities
    - Generate a structured execution brief
    - Persist the brief as an artifact (JSON file)
    - Audit-log every operation
    - Gate all output behind lease expiry and capability checks

Phase 2 is the bridge between Phase 1 (simulation) and Phase 3 (autonomous
delivery). It prepares the handoff; it does not execute it.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..constants import (
    CAP_WORKSPACE_READ,
    CAP_WORKSPACE_WRITE,
    CAP_GIT_READ,
    CAP_GIT_COMMIT,
    CAP_USER_INTERACTION,
    RISK_LOW,
    RISK_MEDIUM,
    RISK_HIGH,
)
from ..lease import AutonomyLease, validate_lease, validate_lease_expired
from ..policy import check_capability, classify_risk, validate_lease_for_workspace
from ..registration import (
    ProjectRegistration,
    validate_registration,
    validate_workspace_path,
)
from ..audit import log_event

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Brief schema version — incremented when the brief structure changes
# ---------------------------------------------------------------------------
BRIEF_SCHEMA_VERSION = 1

# ---------------------------------------------------------------------------
# Brief artifact paths relative to project state dir
# ---------------------------------------------------------------------------
BRIEF_ARTIFACT_DIR = "briefs"
BRIEF_FILENAME_TEMPLATE = "brief_{brief_id}.json"


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class BriefTask:
    """A single task item within an execution brief."""

    task_id: str
    title: str
    description: str
    priority: str  # "low", "medium", "high", "critical"
    risk_level: str  # "low", "medium", "high"
    acceptance_criteria: tuple[str, ...] = ()
    estimated_files: tuple[str, ...] = ()
    dependencies: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["acceptance_criteria"] = list(self.acceptance_criteria)
        d["estimated_files"] = list(self.estimated_files)
        d["dependencies"] = list(self.dependencies)
        return d

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> BriefTask:
        return cls(
            task_id=str(data["task_id"]),
            title=str(data["title"]),
            description=str(data.get("description", "")),
            priority=str(data.get("priority", "medium")),
            risk_level=str(data.get("risk_level", "medium")),
            acceptance_criteria=tuple(data.get("acceptance_criteria", [])),
            estimated_files=tuple(data.get("estimated_files", [])),
            dependencies=tuple(data.get("dependencies", [])),
        )


@dataclass(frozen=True)
class DevelopmentExecutionBrief:
    """A self-contained execution brief for the Development session.

    This is the primary Phase 2 artifact. It packages:
    - Project identity and workspace scope
    - Session binding (Discussion → Development)
    - Lease authorization snapshot
    - Task list with acceptance criteria
    - Capability constraints for execution
    - Human-in-the-loop gates

    The brief is a READ-ONLY handoff document. It does not authorize
    autonomous execution — that requires an explicit Phase 3 lease and
    adapter authorization.
    """

    brief_id: str
    brief_version: int
    schema_version: int
    project_id: str
    workspace_root: str
    discussion_session_id: str
    development_session_id: str
    display_title: str
    lease_id: str
    lease_expiry: str
    granted_capabilities: tuple[str, ...]
    scope: str
    tasks: tuple[BriefTask, ...]
    created_at: str
    created_by: str  # "execution_bridge"
    human_gate_required: bool = True
    execution_authorized: bool = False
    notes: str = ""

    def to_dict(self) -> dict[str, Any]:
        d = {
            "brief_id": self.brief_id,
            "brief_version": self.brief_version,
            "schema_version": self.schema_version,
            "project_id": self.project_id,
            "workspace_root": self.workspace_root,
            "discussion_session_id": self.discussion_session_id,
            "development_session_id": self.development_session_id,
            "display_title": self.display_title,
            "lease_id": self.lease_id,
            "lease_expiry": self.lease_expiry,
            "granted_capabilities": list(self.granted_capabilities),
            "scope": self.scope,
            "tasks": [t.to_dict() for t in self.tasks],
            "created_at": self.created_at,
            "created_by": self.created_by,
            "human_gate_required": self.human_gate_required,
            "execution_authorized": self.execution_authorized,
            "notes": self.notes,
        }
        return d

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> DevelopmentExecutionBrief:
        tasks = tuple(BriefTask.from_dict(t) for t in data.get("tasks", []))
        return cls(
            brief_id=str(data["brief_id"]),
            brief_version=int(data.get("brief_version", 1)),
            schema_version=int(data.get("schema_version", BRIEF_SCHEMA_VERSION)),
            project_id=str(data["project_id"]),
            workspace_root=str(data["workspace_root"]),
            discussion_session_id=str(data.get("discussion_session_id", "")),
            development_session_id=str(data.get("development_session_id", "")),
            display_title=str(data.get("display_title", "")),
            lease_id=str(data.get("lease_id", "")),
            lease_expiry=str(data.get("lease_expiry", "")),
            granted_capabilities=tuple(data.get("granted_capabilities", [])),
            scope=str(data.get("scope", "")),
            tasks=tasks,
            created_at=str(data.get("created_at", "")),
            created_by=str(data.get("created_by", "execution_bridge")),
            human_gate_required=bool(data.get("human_gate_required", True)),
            execution_authorized=bool(data.get("execution_authorized", False)),
            notes=str(data.get("notes", "")),
        )


# ---------------------------------------------------------------------------
# Brief generation result
# ---------------------------------------------------------------------------

@dataclass
class BriefGenerationResult:
    """Result of attempting to generate an execution brief."""

    success: bool
    brief: DevelopmentExecutionBrief | None = None
    blockers: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    artifact_path: str = ""

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "success": self.success,
            "blockers": self.blockers,
            "warnings": self.warnings,
            "artifact_path": self.artifact_path,
        }
        if self.brief:
            d["brief"] = self.brief.to_dict()
        return d


# ---------------------------------------------------------------------------
# ExecutionBridge adapter
# ---------------------------------------------------------------------------

class ExecutionBridge:
    """Phase 2 adapter that bridges Discussion context to Development execution.

    The bridge generates development execution briefs — structured handoff
    artifacts — from project registration and lease context. It does NOT
    perform actual code execution, file editing, or terminal commands.

    Safety properties:
    - Requires valid non-expired lease with explicit capabilities
    - Requires lease workspace scope to cover the active workspace
    - Requires workspace to exist and pass validation
    - Persists briefs as immutable artifacts (new file per brief)
    - Audit-logs every operation
    - human_gate_required=True by default (cannot be overridden by bridge)
    - execution_authorized=False always (requires external authorization)
    - Never writes to session databases, workspace files, or git repos

    Usage:
        bridge = ExecutionBridge()
        result = bridge.generate_brief(state)
        if result.success:
            # Brief is persisted as artifact — ready for human review
            print(result.brief.to_dict())
    """

    def __init__(self, hermes_home: str | Path | None = None):
        if hermes_home is None:
            raw = os.environ.get("HERMES_HOME", "").strip()
            self._hermes_home = Path(raw).expanduser() if raw else Path.home() / ".hermes"
        else:
            self._hermes_home = Path(hermes_home).expanduser()

    @property
    def _state_dir(self) -> Path:
        return self._hermes_home / "state" / "autopilot"

    def _project_state_dir(self, project_id: str) -> Path:
        return self._state_dir / "projects" / project_id

    def _briefs_dir(self, project_id: str) -> Path:
        return self._project_state_dir(project_id) / BRIEF_ARTIFACT_DIR

    def _persist_brief(self, brief: DevelopmentExecutionBrief) -> str:
        """Persist a brief as an immutable JSON artifact.

        Returns the absolute path to the persisted artifact.
        """
        briefs_dir = self._briefs_dir(brief.project_id)
        briefs_dir.mkdir(parents=True, exist_ok=True)

        filename = BRIEF_FILENAME_TEMPLATE.format(brief_id=brief.brief_id)
        artifact_path = briefs_dir / filename

        payload = json.dumps(brief.to_dict(), indent=2, sort_keys=True) + "\n"
        artifact_path.write_text(payload, encoding="utf-8")

        return str(artifact_path)

    def _validate_lease_for_brief(
        self,
        lease_data: dict[str, Any],
        registration: dict[str, Any],
    ) -> list[str]:
        """Validate lease is suitable for brief generation.

        Brief generation is a READ-ONLY operation — it reads project context
        and generates an artifact. However, it still requires a valid lease
        to ensure authorization is traceable.

        Returns a list of blockers (empty = valid).
        """
        blockers: list[str] = []

        try:
            lease = validate_lease(lease_data)
        except ValueError as exc:
            blockers.append(f"Lease validation failed: {exc}")
            return blockers

        valid, err = validate_lease_expired(lease)
        if not valid:
            blockers.append(err)

        active_project_id = str(registration.get("project_id", ""))
        if lease.project_id != active_project_id:
            blockers.append(
                f"Lease project_id {lease.project_id!r} does not match "
                f"active project {active_project_id!r}."
            )

        # Workspace scope check
        ws_valid, ws_err = validate_lease_for_workspace(
            lease, registration.get("workspace_root", "")
        )
        if not ws_valid:
            blockers.append(ws_err)

        # Brief generation requires the complete read-only handoff capability set.
        for capability in (CAP_WORKSPACE_READ, CAP_GIT_READ):
            granted, msg = check_capability(lease, capability)
            if not granted:
                blockers.append(msg)

        return blockers

    def validate_readiness(
        self,
        state: dict[str, Any],
    ) -> tuple[bool, list[str]]:
        """Validate that the active project is ready for brief generation.

        Checks:
        - Valid project registration exists
        - Workspace exists and passes validation
        - Valid non-expired lease
        - Lease workspace scope covers the workspace
        - workspace.read capability is granted

        Returns (ready, blockers).
        """
        blockers: list[str] = []

        # Check registration
        reg = state.get("registration")
        if not isinstance(reg, dict):
            blockers.append("No project registration. Run /autopilot register first.")
            return False, blockers

        # Validate workspace
        ws_valid, ws_path, ws_err = validate_workspace_path(
            reg.get("workspace_root", "")
        )
        if not ws_valid:
            blockers.append(f"Workspace validation failed: {ws_err}")

        # Check lease
        lease_data = state.get("lease")
        if not isinstance(lease_data, dict):
            blockers.append(
                "No active lease. Run /autopilot lease request to review "
                "the safe Phase 2 read-only preset."
            )
            return False, blockers

        lease_blockers = self._validate_lease_for_brief(lease_data, reg)
        blockers.extend(lease_blockers)

        return not blockers, blockers

    def generate_brief(
        self,
        state: dict[str, Any],
        *,
        tasks: list[dict[str, Any]] | None = None,
        scope: str = "",
        notes: str = "",
    ) -> BriefGenerationResult:
        """Generate a development execution brief from project state.

        This is a READ-ONLY operation that produces a persisted artifact.
        It does NOT perform any execution, file editing, or git operations.

        Args:
            state: The current autopilot state dict (must contain registration + lease)
            tasks: Optional list of task dicts with title, description, priority, etc.
            scope: Optional scope description for the brief
            notes: Optional notes to attach to the brief

        Returns:
            BriefGenerationResult with the generated brief or blockers
        """
        # Validate readiness
        ready, blockers = self.validate_readiness(state)
        if not ready:
            return BriefGenerationResult(
                success=False,
                blockers=blockers,
            )

        reg = state["registration"]
        lease_data = state["lease"]
        lease = validate_lease(lease_data)

        # Build task list
        brief_tasks: list[BriefTask] = []
        if tasks:
            for i, task_data in enumerate(tasks):
                task_id = str(task_data.get("task_id", f"t-{i+1:03d}"))
                title = str(task_data.get("title", f"Task {i+1}"))
                description = str(task_data.get("description", ""))
                priority = str(task_data.get("priority", "medium"))
                risk_level = classify_risk(title + " " + description).level
                acceptance_criteria = tuple(
                    str(c) for c in task_data.get("acceptance_criteria", [])
                )
                estimated_files = tuple(
                    str(f) for f in task_data.get("estimated_files", [])
                )
                dependencies = tuple(
                    str(d) for d in task_data.get("dependencies", [])
                )

                brief_tasks.append(BriefTask(
                    task_id=task_id,
                    title=title,
                    description=description,
                    priority=priority,
                    risk_level=risk_level,
                    acceptance_criteria=acceptance_criteria,
                    estimated_files=estimated_files,
                    dependencies=dependencies,
                ))

        # Generate brief ID (timestamp + microsecond + short uuid for uniqueness)
        now = datetime.now(timezone.utc)
        import uuid as _uuid
        _short_uuid = _uuid.uuid4().hex[:8]
        brief_id = f"brief-{now.strftime('%Y%m%d_%H%M%S')}-{now.microsecond:06d}-{_short_uuid}"

        # Version counter: count existing briefs for this project
        project_id = reg.get("project_id", "unknown")
        existing_briefs = self.list_briefs(project_id)
        brief_version = len(existing_briefs) + 1

        brief = DevelopmentExecutionBrief(
            brief_id=brief_id,
            brief_version=brief_version,
            schema_version=BRIEF_SCHEMA_VERSION,
            project_id=reg.get("project_id", ""),
            workspace_root=reg.get("workspace_root", ""),
            discussion_session_id=reg.get("discussion_session_id", ""),
            development_session_id=reg.get("development_session_id", ""),
            display_title=reg.get("display_title", ""),
            lease_id=lease.lease_id,
            lease_expiry=lease.expiry,
            granted_capabilities=lease.granted_capabilities,
            scope=scope or reg.get("display_title", ""),
            tasks=tuple(brief_tasks),
            created_at=now.isoformat(),
            created_by="execution_bridge",
            human_gate_required=True,
            execution_authorized=False,
            notes=notes,
        )

        # Persist artifact
        warnings: list[str] = []
        artifact_path = ""
        try:
            artifact_path = self._persist_brief(brief)
        except Exception as exc:
            warnings.append(f"Could not persist brief artifact: {exc}")

        # Audit log
        log_event(
            event_type="brief_generated",
            state_from=state.get("state", ""),
            state_to="PREPARING_BRIEF",
            detail=f"brief_id={brief_id}, tasks={len(brief_tasks)}",
            lease_id=lease.lease_id,
        )

        return BriefGenerationResult(
            success=True,
            brief=brief,
            warnings=warnings,
            artifact_path=artifact_path,
        )

    def read_brief(self, project_id: str, brief_id: str) -> DevelopmentExecutionBrief | None:
        """Read a persisted brief artifact.

        Returns the brief if found, None otherwise. Read-only operation.
        """
        briefs_dir = self._briefs_dir(project_id)
        filename = BRIEF_FILENAME_TEMPLATE.format(brief_id=brief_id)
        artifact_path = briefs_dir / filename

        if not artifact_path.exists():
            return None

        try:
            raw = artifact_path.read_text(encoding="utf-8")
            data = json.loads(raw)
            return DevelopmentExecutionBrief.from_dict(data)
        except Exception as exc:
            logger.error("Failed to read brief artifact %s: %s", artifact_path, exc)
            return None

    def set_brief_authorization(
        self,
        project_id: str,
        brief_id: str,
        authorized: bool,
    ) -> DevelopmentExecutionBrief | None:
        """Set human execution authorization on a persisted brief artifact."""
        brief = self.read_brief(project_id, brief_id)
        if brief is None:
            return None

        updated = DevelopmentExecutionBrief(
            brief_id=brief.brief_id,
            brief_version=brief.brief_version,
            schema_version=brief.schema_version,
            project_id=brief.project_id,
            workspace_root=brief.workspace_root,
            discussion_session_id=brief.discussion_session_id,
            development_session_id=brief.development_session_id,
            display_title=brief.display_title,
            lease_id=brief.lease_id,
            lease_expiry=brief.lease_expiry,
            granted_capabilities=brief.granted_capabilities,
            scope=brief.scope,
            tasks=brief.tasks,
            created_at=brief.created_at,
            created_by=brief.created_by,
            human_gate_required=True,
            execution_authorized=authorized,
            notes=brief.notes,
        )
        self._persist_brief(updated)
        log_event(
            event_type="brief_authorization_updated",
            state_from="NEEDS_HUMAN",
            state_to="NEEDS_HUMAN",
            detail=f"brief_id={brief_id}, authorized={authorized}",
        )
        return updated

    def list_briefs(self, project_id: str) -> list[dict[str, Any]]:
        """List all persisted brief artifacts for a project.

        Returns a list of brief metadata dicts (without full task details).
        Read-only operation.
        """
        briefs_dir = self._briefs_dir(project_id)
        if not briefs_dir.exists():
            return []

        briefs: list[dict[str, Any]] = []
        for path in sorted(briefs_dir.glob("brief_*.json")):
            try:
                raw = path.read_text(encoding="utf-8")
                data = json.loads(raw)
                briefs.append({
                    "brief_id": data.get("brief_id", ""),
                    "brief_version": data.get("brief_version", 0),
                    "project_id": data.get("project_id", ""),
                    "scope": data.get("scope", ""),
                    "task_count": len(data.get("tasks", [])),
                    "created_at": data.get("created_at", ""),
                    "human_gate_required": data.get("human_gate_required", True),
                    "execution_authorized": data.get("execution_authorized", False),
                    "artifact_path": str(path),
                })
            except Exception:
                continue

        return briefs

    def validate_brief_for_execution(
        self,
        brief: DevelopmentExecutionBrief,
        state: dict[str, Any],
    ) -> tuple[bool, list[str]]:
        """Validate whether a brief is ready for execution authorization.

        This is a READ-ONLY check. It verifies:
        - Brief project matches active project
        - Brief workspace matches registration workspace
        - Brief lease is still valid (not expired)
        - Required execution capabilities are in the lease
        - human_gate_required is True (must be acknowledged)
        - execution_authorized is False (requires explicit external auth)

        Returns (valid, blockers).
        """
        blockers: list[str] = []

        reg = state.get("registration", {})
        if brief.project_id != reg.get("project_id"):
            blockers.append(
                f"Brief project_id {brief.project_id!r} does not match "
                f"active project {reg.get('project_id')!r}"
            )

        if brief.workspace_root != reg.get("workspace_root"):
            blockers.append(
                f"Brief workspace {brief.workspace_root!r} does not match "
                f"registration workspace {reg.get('workspace_root')!r}"
            )

        # Check lease validity
        lease_data = state.get("lease")
        if not isinstance(lease_data, dict):
            blockers.append("No active lease in state")
        else:
            try:
                lease = validate_lease(lease_data)
                valid, err = validate_lease_expired(lease)
                if not valid:
                    blockers.append(err)
                if lease.lease_id != brief.lease_id:
                    blockers.append(
                        f"Brief lease_id {brief.lease_id!r} does not match "
                        f"active lease {lease.lease_id!r}"
                    )
            except ValueError as exc:
                blockers.append(f"Lease validation failed: {exc}")

        # Check human gate
        if not brief.human_gate_required:
            blockers.append(
                "Brief has human_gate_required=False — "
                "this is not allowed by the execution bridge"
            )

        # Check execution authorization
        if brief.execution_authorized:
            blockers.append(
                "Brief has execution_authorized=True — "
                "this must be False until explicitly authorized"
            )

        return not blockers, blockers


# ---------------------------------------------------------------------------
# Module-level singleton for convenience
# ---------------------------------------------------------------------------

_default_bridge: ExecutionBridge | None = None


def get_execution_bridge(hermes_home: str | Path | None = None) -> ExecutionBridge:
    """Get or create the default execution bridge instance."""
    global _default_bridge
    if _default_bridge is None or hermes_home is not None:
        _default_bridge = ExecutionBridge(hermes_home)
    return _default_bridge
