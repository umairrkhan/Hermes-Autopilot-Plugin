"""Phase 3 controlled Development runner package preparation.

This module deliberately does not edit files, run terminal commands, spawn agents,
or commit git changes. It converts an explicitly approved Phase 2 brief plus a
Phase 3 lease into a project-scoped run artifact that can be handed to the
Solar360 Development session for controlled implementation.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from ..audit import log_event
from ..constants import (
    CAP_GIT_COMMIT,
    CAP_GIT_READ,
    CAP_NEXT_PHASE,
    CAP_WORKSPACE_READ,
    CAP_WORKSPACE_WRITE,
)
from ..lease import validate_lease, validate_lease_expired
from ..policy import check_capability, validate_lease_for_workspace
from .execution_bridge import DevelopmentExecutionBrief

RUN_ARTIFACT_DIR = "runs"
RUN_FILENAME_TEMPLATE = "run_{run_id}.json"
REQUIRED_PHASE3_CAPABILITIES = (
    CAP_WORKSPACE_READ,
    CAP_GIT_READ,
    CAP_WORKSPACE_WRITE,
    CAP_GIT_COMMIT,
    CAP_NEXT_PHASE,
)


@dataclass(frozen=True)
class DevelopmentRunPackage:
    """A controlled run package for a Development session."""

    run_id: str
    project_id: str
    brief_id: str
    workspace_root: str
    development_session_id: str
    discussion_session_id: str
    lease_id: str
    status: str
    execution_mode: str
    execution_authorized: bool
    created_at: str
    prompt: str
    artifact_path: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "project_id": self.project_id,
            "brief_id": self.brief_id,
            "workspace_root": self.workspace_root,
            "development_session_id": self.development_session_id,
            "discussion_session_id": self.discussion_session_id,
            "lease_id": self.lease_id,
            "status": self.status,
            "execution_mode": self.execution_mode,
            "execution_authorized": self.execution_authorized,
            "created_at": self.created_at,
            "prompt": self.prompt,
            "artifact_path": self.artifact_path,
        }


@dataclass
class DevelopmentRunResult:
    """Result of preparing a controlled development run package."""

    success: bool
    package: DevelopmentRunPackage | None = None
    blockers: list[str] = field(default_factory=list)
    artifact_path: str = ""


class DevelopmentRunner:
    """Prepare approved briefs for controlled Development-session execution."""

    def __init__(self, hermes_home: str | Path | None = None):
        if hermes_home is None:
            raw = os.environ.get("HERMES_HOME", "").strip()
            self._hermes_home = Path(raw).expanduser() if raw else Path.home() / ".hermes"
        else:
            self._hermes_home = Path(hermes_home).expanduser()

    @property
    def _state_dir(self) -> Path:
        return self._hermes_home / "state" / "autopilot"

    def _runs_dir(self, project_id: str) -> Path:
        return self._state_dir / "projects" / project_id / RUN_ARTIFACT_DIR

    def _persist_package(self, package: DevelopmentRunPackage) -> str:
        runs_dir = self._runs_dir(package.project_id)
        runs_dir.mkdir(parents=True, exist_ok=True)
        path = runs_dir / RUN_FILENAME_TEMPLATE.format(run_id=package.run_id)
        payload = package.to_dict() | {"artifact_path": str(path)}
        path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        return str(path)

    def _validate(self, brief: DevelopmentExecutionBrief | None, state: dict[str, Any]) -> list[str]:
        blockers: list[str] = []
        if brief is None:
            return ["Brief not found."]
        if not brief.execution_authorized:
            blockers.append("Brief is not approved for execution.")
        if not brief.human_gate_required:
            blockers.append("Brief human gate is missing.")

        reg = state.get("registration") or {}
        if brief.project_id != reg.get("project_id"):
            blockers.append("Brief project does not match active project.")
        if brief.workspace_root != reg.get("workspace_root"):
            blockers.append("Brief workspace does not match active project workspace.")

        lease_data = state.get("lease")
        if not isinstance(lease_data, dict):
            blockers.append("No active Phase 3 lease.")
            return blockers

        try:
            lease = validate_lease(lease_data)
        except ValueError as exc:
            blockers.append(f"Lease validation failed: {exc}")
            return blockers

        valid, err = validate_lease_expired(lease)
        if not valid:
            blockers.append(err)
        if lease.project_id != brief.project_id:
            blockers.append("Lease project does not match brief project.")
        ws_valid, ws_err = validate_lease_for_workspace(lease, brief.workspace_root)
        if not ws_valid:
            blockers.append(ws_err)

        for capability in REQUIRED_PHASE3_CAPABILITIES:
            granted, message = check_capability(lease, capability)
            if not granted:
                blockers.append(message)

        return blockers

    def _prompt(self, brief: DevelopmentExecutionBrief) -> str:
        tasks = "\n".join(
            f"- {task.task_id}: {task.title} ({task.priority}, risk={task.risk_level})"
            for task in brief.tasks
        ) or "- No explicit tasks were supplied; derive the smallest safe next task from the brief scope."
        return "\n".join([
            f"Project: {brief.project_id}",
            f"Workspace: {brief.workspace_root}",
            f"Brief: {brief.brief_id}",
            "",
            "Implement only the approved brief scope in the registered workspace.",
            "Do not push, deploy, run migrations, or access privileged accounts.",
            "Run the project verification commands before reporting success.",
            "Stop and ask for human review on security, auth, payment, database, or production-risk changes.",
            "",
            "Tasks:",
            tasks,
        ])

    def prepare_run(
        self,
        brief: DevelopmentExecutionBrief | None,
        state: dict[str, Any],
    ) -> DevelopmentRunResult:
        blockers = self._validate(brief, state)
        if blockers:
            return DevelopmentRunResult(success=False, blockers=blockers)
        assert brief is not None
        lease = validate_lease(state["lease"])
        now = datetime.now(timezone.utc)
        run_id = f"run-{now.strftime('%Y%m%dT%H%M%S')}-{uuid4().hex[:8]}"
        package = DevelopmentRunPackage(
            run_id=run_id,
            project_id=brief.project_id,
            brief_id=brief.brief_id,
            workspace_root=brief.workspace_root,
            development_session_id=brief.development_session_id,
            discussion_session_id=brief.discussion_session_id,
            lease_id=lease.lease_id,
            status="READY_FOR_DEVELOPMENT_SESSION",
            execution_mode="controlled-package",
            execution_authorized=True,
            created_at=now.isoformat(),
            prompt=self._prompt(brief),
        )
        artifact_path = self._persist_package(package)
        package = replace(package, artifact_path=artifact_path)
        log_event(
            event_type="development_run_prepared",
            state_from=state.get("state", ""),
            state_to="NEEDS_HUMAN",
            detail=f"run_id={run_id}, brief_id={brief.brief_id}",
            lease_id=lease.lease_id,
        )
        return DevelopmentRunResult(success=True, package=package, artifact_path=artifact_path)
