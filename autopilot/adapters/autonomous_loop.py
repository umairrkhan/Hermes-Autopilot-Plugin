"""Autonomous development loop supervisor artifacts.

This supervisor is the plugin-side control plane for the user's Discussion ↔
Development workflow. It records a durable loop contract, enforces the active
lease/brief boundaries, stores status, and captures Development results for
Discussion review. It deliberately does not bypass Hermes approvals, push code,
deploy, or write directly to Hermes session databases.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import re
import tempfile
import threading
from typing import Any
from uuid import uuid4

from ..audit import log_event, redact_text
from ..constants import (
    CAP_GIT_READ,
    CAP_NEXT_PHASE,
    CAP_USER_INTERACTION,
    CAP_WORKSPACE_READ,
    CAP_WORKSPACE_WRITE,
)
from ..lease import validate_lease, validate_lease_expired
from ..policy import check_capability, validate_lease_for_workspace
from .execution_bridge import DevelopmentExecutionBrief

LOOP_ARTIFACT_DIR = "loops"
RESULT_ARTIFACT_DIR = "results"
DECISION_ARTIFACT_DIR = "decisions"
LOOP_FILENAME_TEMPLATE = "loop_{loop_id}.json"
RESULT_FILENAME_TEMPLATE = "result_{loop_id}.json"
_LOOP_LOCK = threading.RLock()
REQUIRED_AUTONOMOUS_CAPABILITIES = (
    CAP_WORKSPACE_READ,
    CAP_GIT_READ,
    CAP_WORKSPACE_WRITE,
    CAP_NEXT_PHASE,
    CAP_USER_INTERACTION,
)


@dataclass(frozen=True)
class AutonomousLoop:
    """Durable contract for one supervised autonomous development loop."""

    loop_id: str
    project_id: str
    brief_id: str
    workspace_root: str
    discussion_session_id: str
    development_session_id: str
    lease_id: str
    status: str
    mode: str
    auto_answer_policy: str
    created_at: str
    artifact_path: str = ""
    board_slug: str = ""
    development_task_id: str = ""
    verifier_task_id: str = ""
    starting_revision: str = ""
    verification_profile_digest: str = ""
    source_status_digest: str = ""
    dirty_workspace: bool = False
    remediation_count: int = 0
    current_remediation_task_id: str = ""
    result_artifact_path: str = ""
    result_task_id: str = ""
    result_run_id: int = 0
    acceptance_artifact_path: str = ""
    checkpoint_artifact_path: str = ""
    checkpoint_status_digest: str = ""
    checkpoint_content_digest: str = ""
    commit_authorization_path: str = ""
    commit_revision: str = ""
    decision_count: int = 0
    pending_decision_artifact_path: str = ""
    recovery_attempts: int = 0
    last_recovery_at: str = ""
    last_recovery_error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "loop_id": self.loop_id,
            "project_id": self.project_id,
            "brief_id": self.brief_id,
            "workspace_root": self.workspace_root,
            "discussion_session_id": self.discussion_session_id,
            "development_session_id": self.development_session_id,
            "lease_id": self.lease_id,
            "status": self.status,
            "mode": self.mode,
            "auto_answer_policy": self.auto_answer_policy,
            "created_at": self.created_at,
            "artifact_path": self.artifact_path,
            "board_slug": self.board_slug,
            "development_task_id": self.development_task_id,
            "verifier_task_id": self.verifier_task_id,
            "starting_revision": self.starting_revision,
            "verification_profile_digest": self.verification_profile_digest,
            "source_status_digest": self.source_status_digest,
            "dirty_workspace": self.dirty_workspace,
            "remediation_count": self.remediation_count,
            "current_remediation_task_id": self.current_remediation_task_id,
            "result_artifact_path": self.result_artifact_path,
            "result_task_id": self.result_task_id,
            "result_run_id": self.result_run_id,
            "acceptance_artifact_path": self.acceptance_artifact_path,
            "checkpoint_artifact_path": self.checkpoint_artifact_path,
            "checkpoint_status_digest": self.checkpoint_status_digest,
            "checkpoint_content_digest": self.checkpoint_content_digest,
            "commit_authorization_path": self.commit_authorization_path,
            "commit_revision": self.commit_revision,
            "decision_count": self.decision_count,
            "pending_decision_artifact_path": self.pending_decision_artifact_path,
            "recovery_attempts": self.recovery_attempts,
            "last_recovery_at": self.last_recovery_at,
            "last_recovery_error": self.last_recovery_error,
        }


@dataclass
class LoopStartResult:
    success: bool
    loop: AutonomousLoop | None = None
    blockers: list[str] = field(default_factory=list)
    artifact_path: str = ""


@dataclass
class DevelopmentResultReport:
    success: bool
    artifact_path: str = ""
    blockers: list[str] = field(default_factory=list)


class AutonomousLoopSupervisor:
    """Persist and supervise project-scoped autonomous loop contracts."""

    def __init__(self, hermes_home: str | Path | None = None):
        if hermes_home is None:
            raw = os.environ.get("HERMES_HOME", "").strip()
            self._hermes_home = Path(raw).expanduser() if raw else Path.home() / ".hermes"
        else:
            self._hermes_home = Path(hermes_home).expanduser()

    @property
    def _state_dir(self) -> Path:
        return self._hermes_home / "state" / "autopilot"

    def _project_dir(self, project_id: str) -> Path:
        return self._state_dir / "projects" / project_id

    def _loops_dir(self, project_id: str) -> Path:
        return self._project_dir(project_id) / LOOP_ARTIFACT_DIR

    def _results_dir(self, project_id: str) -> Path:
        return self._project_dir(project_id) / RESULT_ARTIFACT_DIR

    def _decisions_dir(self, project_id: str) -> Path:
        return self._project_dir(project_id) / DECISION_ARTIFACT_DIR

    @contextmanager
    def _lease_guard(self, project_id: str, lease_id: str):
        """Serialize live-loop consumption for one immutable lease."""

        safe_lease_id = lease_id if re.fullmatch(r"[A-Za-z0-9_-]+", lease_id) else "invalid"
        lock_path = self._loops_dir(project_id) / f".lease-{safe_lease_id}.lock"
        with _LOOP_LOCK:
            lock_path.parent.mkdir(parents=True, exist_ok=True)
            with lock_path.open("a+", encoding="utf-8") as handle:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
                try:
                    yield
                finally:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    @contextmanager
    def _loop_guard(self, project_id: str, loop_id: str):
        """Serialize one loop's read-modify-write mutations across workers."""

        lock_path = self._loops_dir(project_id) / f".{loop_id}.lock"
        with _LOOP_LOCK:
            lock_path.parent.mkdir(parents=True, exist_ok=True)
            with lock_path.open("a+", encoding="utf-8") as handle:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
                try:
                    yield
                finally:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def _validate_start(self, brief: DevelopmentExecutionBrief | None, state: dict[str, Any]) -> list[str]:
        blockers: list[str] = []
        if brief is None:
            return ["Approved brief not found."]
        if not brief.execution_authorized:
            blockers.append("Brief is not approved for autonomous execution.")

        registration = state.get("registration") or {}
        if brief.project_id != registration.get("project_id"):
            blockers.append("Brief project does not match active project.")
        if brief.workspace_root != registration.get("workspace_root"):
            blockers.append("Brief workspace does not match active workspace.")

        lease_data = state.get("lease")
        if not isinstance(lease_data, dict):
            blockers.append("No active autonomous-development lease.")
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
        valid_ws, ws_err = validate_lease_for_workspace(lease, brief.workspace_root)
        if not valid_ws:
            blockers.append(ws_err)
        for capability in REQUIRED_AUTONOMOUS_CAPABILITIES:
            granted, message = check_capability(lease, capability)
            if not granted:
                blockers.append(message)
        used_loops = sum(
            1 for loop in self.list_loops(brief.project_id)
            if loop.get("lease_id") == lease.lease_id
        )
        if used_loops >= lease.max_loop_iterations:
            blockers.append(
                f"Lease loop limit reached ({used_loops}/{lease.max_loop_iterations}); approve a new lease."
            )
        return blockers

    def start_loop(
        self,
        brief: DevelopmentExecutionBrief | None,
        state: dict[str, Any],
    ) -> LoopStartResult:
        raw_lease = state.get("lease")
        lease_id = str(raw_lease.get("lease_id", "invalid")) if isinstance(raw_lease, dict) else "invalid"
        project_id = brief.project_id if brief is not None else "invalid"
        if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,63}", project_id):
            project_id = "invalid"
        with self._lease_guard(project_id, lease_id):
            blockers = self._validate_start(brief, state)
            if blockers:
                return LoopStartResult(success=False, blockers=blockers)
            assert brief is not None
            lease = validate_lease(state["lease"])
            now = datetime.now(timezone.utc)
            loop_id = f"loop-{now.strftime('%Y%m%dT%H%M%S')}-{uuid4().hex[:8]}"
            loop = AutonomousLoop(
                loop_id=loop_id,
                project_id=brief.project_id,
                brief_id=brief.brief_id,
                workspace_root=brief.workspace_root,
                discussion_session_id=brief.discussion_session_id,
                development_session_id=brief.development_session_id,
                lease_id=lease.lease_id,
                status="WAITING_FOR_DEVELOPMENT_EXECUTOR",
                mode="supervised-development",
                auto_answer_policy="recommended_low_risk_only",
                created_at=now.isoformat(),
            )
            artifact_path = self._write_loop(loop)
            loop = replace(loop, artifact_path=artifact_path)
        log_event(
            event_type="autonomous_loop_started",
            state_from=state.get("state", ""),
            state_to="EXECUTING",
            detail=f"loop_id={loop_id}, brief_id={brief.brief_id}",
            lease_id=lease.lease_id,
        )
        return LoopStartResult(success=True, loop=loop, artifact_path=artifact_path)

    @staticmethod
    def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        content = json.dumps(payload, indent=2, sort_keys=True) + "\n"
        fd, tmp_name = tempfile.mkstemp(
            prefix=f".{path.name}.",
            suffix=".tmp",
            dir=str(path.parent),
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_name, path)
        finally:
            try:
                if os.path.exists(tmp_name):
                    os.unlink(tmp_name)
            except OSError:
                pass

    def _write_loop(self, loop: AutonomousLoop) -> str:
        loops_dir = self._loops_dir(loop.project_id)
        loops_dir.mkdir(parents=True, exist_ok=True)
        path = loops_dir / LOOP_FILENAME_TEMPLATE.format(loop_id=loop.loop_id)
        payload = loop.to_dict() | {"artifact_path": str(path)}
        self._atomic_write_json(path, payload)
        return str(path)

    def list_loops(self, project_id: str) -> list[dict[str, Any]]:
        loops_dir = self._loops_dir(project_id)
        if not loops_dir.exists():
            return []
        loops = []
        for path in sorted(loops_dir.glob("loop_*.json")):
            try:
                loops.append(json.loads(path.read_text(encoding="utf-8")))
            except Exception:
                continue
        return loops

    def _update_loop(self, project_id: str, loop_id: str, **changes: Any) -> dict[str, Any] | None:
        if not re.fullmatch(r"[A-Za-z0-9_-]+", loop_id):
            return None
        allowed = {
            "status",
            "board_slug",
            "development_task_id",
            "verifier_task_id",
            "starting_revision",
            "verification_profile_digest",
            "source_status_digest",
            "dirty_workspace",
            "remediation_count",
            "current_remediation_task_id",
            "result_artifact_path",
            "result_task_id",
            "result_run_id",
            "acceptance_artifact_path",
            "checkpoint_artifact_path",
            "checkpoint_status_digest",
            "checkpoint_content_digest",
            "commit_authorization_path",
            "commit_revision",
            "decision_count",
            "pending_decision_artifact_path",
            "recovery_attempts",
            "last_recovery_at",
            "last_recovery_error",
        }
        if any(key not in allowed for key in changes):
            raise ValueError("unsupported loop update field")
        path = self._loops_dir(project_id) / LOOP_FILENAME_TEMPLATE.format(loop_id=loop_id)
        with self._loop_guard(project_id, loop_id):
            if not path.exists():
                return None
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                return None
            if payload.get("project_id") != project_id or payload.get("loop_id") != loop_id:
                return None
            payload.update(changes)
            payload["artifact_path"] = str(path)
            self._atomic_write_json(path, payload)
            return payload

    def mark_dispatched(
        self,
        *,
        project_id: str,
        loop_id: str,
        board_slug: str,
        development_task_id: str,
        verifier_task_id: str,
        starting_revision: str,
        verification_profile_digest: str,
        source_status_digest: str,
        dirty_workspace: bool,
    ) -> dict[str, Any] | None:
        """Persist the durable Kanban task bindings for a launched loop."""

        return self._update_loop(
            project_id,
            loop_id,
            status="QUEUED",
            board_slug=board_slug,
            development_task_id=development_task_id,
            verifier_task_id=verifier_task_id,
            starting_revision=starting_revision,
            verification_profile_digest=verification_profile_digest,
            source_status_digest=source_status_digest,
            dirty_workspace=dirty_workspace,
        )

    def mark_status(
        self,
        *,
        project_id: str,
        loop_id: str,
        status: str,
        **fields: Any,
    ) -> dict[str, Any] | None:
        allowed_statuses = {
            "QUEUED",
            "RUNNING",
            "VERIFYING",
            "REMEDIATING",
            "NEEDS_HUMAN",
            "CANCEL_REQUESTED",
            "CANCELED",
            "TIMED_OUT",
            "AWAITING_HUMAN_ACCEPTANCE",
            "ACCEPTED",
            "DISPATCH_BLOCKED",
            "STOPPED",
        }
        if status not in allowed_statuses:
            raise ValueError("unsupported loop status")
        return self._update_loop(project_id, loop_id, status=status, **fields)

    def record_question_decision(
        self,
        *,
        project_id: str,
        loop_id: str,
        task_id: str,
        question: dict[str, Any],
        decision: dict[str, Any],
    ) -> tuple[str, dict[str, Any]]:
        """Persist one idempotent structured worker decision and bind it to the loop."""

        question_id = question.get("question_id")
        if not isinstance(question_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", question_id):
            raise ValueError("question_id is invalid")
        if not re.fullmatch(r"[A-Za-z0-9_-]+", loop_id):
            raise ValueError("loop_id is invalid")
        loop_path = self._loops_dir(project_id) / LOOP_FILENAME_TEMPLATE.format(loop_id=loop_id)
        decision_path = self._decisions_dir(project_id) / f"decision_{loop_id}_{question_id}.json"
        with self._loop_guard(project_id, loop_id):
            try:
                loop = json.loads(loop_path.read_text(encoding="utf-8"))
            except (OSError, ValueError, TypeError) as exc:
                raise ValueError("loop artifact could not be read") from exc
            if loop.get("project_id") != project_id or loop.get("loop_id") != loop_id:
                raise ValueError("loop binding is invalid")
            authorized_tasks = {
                loop.get("development_task_id"),
                loop.get("current_remediation_task_id"),
            }
            if task_id not in authorized_tasks or not task_id:
                raise ValueError("only the bound Development or remediation task may ask decisions")

            choices = question.get("choices", [])
            if not isinstance(choices, list) or len(choices) > 8 or not all(
                isinstance(choice, str) and 0 < len(choice) <= 300 for choice in choices
            ):
                raise ValueError("choices must contain at most eight bounded strings")
            fields = {
                "text": question.get("text"),
                "category": question.get("category"),
                "recommended_choice": question.get("recommended_choice", ""),
                "context": question.get("context", ""),
            }
            if not isinstance(fields["text"], str) or not fields["text"].strip():
                raise ValueError("question text is required")
            if not isinstance(fields["category"], str) or not fields["category"].strip():
                raise ValueError("question category is required")
            if any(not isinstance(value, str) or len(value) > 2000 for value in fields.values()):
                raise ValueError("question fields must be bounded strings")

            payload = {
                "schema_version": 1,
                "project_id": project_id,
                "loop_id": loop_id,
                "task_id": task_id,
                "question_id": question_id,
                "text": redact_text(fields["text"], max_length=2000),
                "category": redact_text(fields["category"], max_length=100),
                "choices": [redact_text(choice, max_length=300) for choice in choices],
                "recommended_choice": redact_text(fields["recommended_choice"], max_length=300),
                "context": redact_text(fields["context"], max_length=2000),
                "decision": decision,
                "status": "awaiting_human" if decision.get("action") == "needs_human" else "auto_answered",
                "human_answer": "",
                "created_at": datetime.now(timezone.utc).isoformat(),
                "artifact_path": str(decision_path),
            }
            if decision_path.exists():
                existing = json.loads(decision_path.read_text(encoding="utf-8"))
                stable_keys = (
                    "project_id", "loop_id", "task_id", "question_id", "text",
                    "category", "choices", "recommended_choice", "context", "decision",
                )
                if any(existing.get(key) != payload.get(key) for key in stable_keys):
                    raise ValueError("question replay conflicts with the immutable decision artifact")
                return str(decision_path), existing

            self._atomic_write_json(decision_path, payload)
            loop["decision_count"] = int(loop.get("decision_count", 0)) + 1
            if decision.get("action") == "needs_human":
                loop["status"] = "NEEDS_HUMAN"
                loop["pending_decision_artifact_path"] = str(decision_path)
            loop["artifact_path"] = str(loop_path)
            self._atomic_write_json(loop_path, loop)
            return str(decision_path), payload

    def stage_question_answer(
        self,
        *,
        project_id: str,
        loop_id: str,
        question_id: str,
        answer: str,
    ) -> dict[str, Any]:
        """Durably stage a human answer before attempting to resume its task."""

        if not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", question_id):
            raise ValueError("question_id is invalid")
        answer = answer.strip()
        if not answer or len(answer) > 2000:
            raise ValueError("answer must contain 1 to 2000 characters")
        if redact_text(answer, max_length=len(answer)) != answer:
            raise ValueError("answer contains sensitive or personal data; redact it before resuming")
        loop_path = self._loops_dir(project_id) / LOOP_FILENAME_TEMPLATE.format(loop_id=loop_id)
        decision_path = self._decisions_dir(project_id) / f"decision_{loop_id}_{question_id}.json"
        with self._loop_guard(project_id, loop_id):
            try:
                loop = json.loads(loop_path.read_text(encoding="utf-8"))
                decision = json.loads(decision_path.read_text(encoding="utf-8"))
            except (OSError, ValueError, TypeError) as exc:
                raise ValueError("pending decision artifact could not be read") from exc
            if loop.get("pending_decision_artifact_path") != str(decision_path):
                raise ValueError("question is not the loop's pending decision")
            if decision.get("status") == "answer_staged":
                if decision.get("human_answer") != answer:
                    raise ValueError("a different answer is already staged")
                return decision
            if decision.get("status") != "awaiting_human":
                raise ValueError("question is not awaiting a human answer")
            decision["human_answer"] = answer
            decision["status"] = "answer_staged"
            decision["answer_staged_at"] = datetime.now(timezone.utc).isoformat()
            self._atomic_write_json(decision_path, decision)
            return decision

    def finalize_question_answer(
        self,
        *,
        project_id: str,
        loop_id: str,
        question_id: str,
    ) -> dict[str, Any]:
        """Finalize a staged answer only after Kanban confirms task unblocking."""

        if not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", question_id):
            raise ValueError("question_id is invalid")
        loop_path = self._loops_dir(project_id) / LOOP_FILENAME_TEMPLATE.format(loop_id=loop_id)
        decision_path = self._decisions_dir(project_id) / f"decision_{loop_id}_{question_id}.json"
        with self._loop_guard(project_id, loop_id):
            try:
                loop = json.loads(loop_path.read_text(encoding="utf-8"))
                decision = json.loads(decision_path.read_text(encoding="utf-8"))
            except (OSError, ValueError, TypeError) as exc:
                raise ValueError("staged decision artifact could not be read") from exc
            if loop.get("pending_decision_artifact_path") != str(decision_path):
                raise ValueError("question is not the loop's pending decision")
            if decision.get("status") != "answer_staged" or not decision.get("human_answer"):
                raise ValueError("human answer was not staged")
            now = datetime.now(timezone.utc).isoformat()
            decision["status"] = "answered"
            decision["answered_at"] = now
            self._atomic_write_json(decision_path, decision)
            loop["status"] = "QUEUED"
            loop["pending_decision_artifact_path"] = ""
            loop["artifact_path"] = str(loop_path)
            self._atomic_write_json(loop_path, loop)
            return decision

    def list_question_decisions(self, project_id: str, loop_id: str) -> list[dict[str, Any]]:
        if not re.fullmatch(r"[A-Za-z0-9_-]+", loop_id):
            return []
        decisions = []
        for path in sorted(self._decisions_dir(project_id).glob(f"decision_{loop_id}_*.json")):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError, TypeError):
                continue
            if payload.get("project_id") == project_id and payload.get("loop_id") == loop_id:
                decisions.append(payload)
        return decisions

    def record_recovery_result(
        self,
        *,
        project_id: str,
        loop_id: str,
        error: str = "",
    ) -> None:
        """Persist bounded recovery telemetry without changing the loop workflow status."""

        path = self._loops_dir(project_id) / LOOP_FILENAME_TEMPLATE.format(loop_id=loop_id)
        with self._loop_guard(project_id, loop_id):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError, TypeError):
                return
            if payload.get("project_id") != project_id or payload.get("loop_id") != loop_id:
                return
            payload["recovery_attempts"] = int(payload.get("recovery_attempts", 0)) + 1
            payload["last_recovery_at"] = datetime.now(timezone.utc).isoformat()
            payload["last_recovery_error"] = redact_text(str(error), max_length=500) if error else ""
            payload["artifact_path"] = str(path)
            self._atomic_write_json(path, payload)

    def mark_dispatch_blocked(
        self,
        *,
        project_id: str,
        loop_id: str,
    ) -> dict[str, Any] | None:
        return self._update_loop(project_id, loop_id, status="DISPATCH_BLOCKED")

    def record_verification_evidence(
        self,
        *,
        project_id: str,
        loop_id: str,
        evidence: dict[str, Any],
    ) -> str:
        """Persist validated evidence once and bind it to the loop."""

        provenance = evidence.get("provenance") if isinstance(evidence, dict) else None
        if not re.fullmatch(r"[A-Za-z0-9_-]+", loop_id):
            raise ValueError("loop_id is invalid")
        if not isinstance(provenance, dict):
            raise ValueError("validated evidence is missing provenance")
        task_id = provenance.get("task_id")
        run_id = provenance.get("run_id")
        if (
            provenance.get("project_id") != project_id
            or provenance.get("loop_id") != loop_id
            or not isinstance(task_id, str)
            or not re.fullmatch(r"[A-Za-z0-9_-]+", task_id)
            or not isinstance(run_id, int)
            or isinstance(run_id, bool)
            or run_id < 1
        ):
            raise ValueError("validated evidence provenance does not match the loop")
        if not evidence.get("accepted"):
            raise ValueError("only accepted verifier evidence can await human acceptance")

        results_dir = self._results_dir(project_id)
        filename = f"result_{loop_id}_{task_id}_{run_id}.json"
        path = results_dir / filename
        canonical = json.dumps(evidence, indent=2, sort_keys=True) + "\n"
        if path.exists():
            existing = path.read_text(encoding="utf-8")
            if existing != canonical:
                raise ValueError("evidence replay conflicts with the immutable result artifact")
        else:
            self._atomic_write_json(path, evidence)

        updated = self._update_loop(
            project_id,
            loop_id,
            status="AWAITING_HUMAN_ACCEPTANCE",
            result_artifact_path=str(path),
            result_task_id=task_id,
            result_run_id=run_id,
        )
        if updated is None:
            raise ValueError("loop binding could not be updated")
        return str(path)

    def accept_loop(
        self,
        *,
        project_id: str,
        loop_id: str,
        accepted_by: str,
    ) -> str:
        """Write a separate immutable human acceptance record."""

        if not re.fullmatch(r"[A-Za-z0-9_-]+", loop_id):
            raise ValueError("loop_id is invalid")
        loop_path = self._loops_dir(project_id) / LOOP_FILENAME_TEMPLATE.format(loop_id=loop_id)
        try:
            loop = json.loads(loop_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError) as exc:
            raise ValueError("loop artifact could not be read") from exc
        if loop.get("status") == "ACCEPTED" and loop.get("acceptance_artifact_path"):
            return str(loop["acceptance_artifact_path"])
        if loop.get("status") != "AWAITING_HUMAN_ACCEPTANCE":
            raise ValueError("loop is not awaiting human acceptance")

        task_id = loop.get("result_task_id")
        run_id = loop.get("result_run_id")
        if (
            not isinstance(task_id, str)
            or not re.fullmatch(r"[A-Za-z0-9_-]+", task_id)
            or not isinstance(run_id, int)
            or isinstance(run_id, bool)
            or run_id < 1
        ):
            raise ValueError("loop result binding is invalid")
        expected_result = self._results_dir(project_id) / f"result_{loop_id}_{task_id}_{run_id}.json"
        if loop.get("result_artifact_path") != str(expected_result):
            raise ValueError("loop result path is not project-scoped")
        try:
            if expected_result.stat().st_size > 1_000_000:
                raise ValueError("result artifact exceeds the evidence size limit")
            evidence = json.loads(expected_result.read_text(encoding="utf-8"))
        except (OSError, TypeError, json.JSONDecodeError) as exc:
            raise ValueError("validated result artifact could not be read") from exc
        if not isinstance(evidence, dict):
            raise ValueError("validated result artifact is not a JSON object")
        provenance = evidence.get("provenance")
        if (
            evidence.get("accepted") is not True
            or not isinstance(provenance, dict)
            or provenance.get("project_id") != project_id
            or provenance.get("loop_id") != loop_id
            or provenance.get("task_id") != task_id
            or provenance.get("run_id") != run_id
        ):
            raise ValueError("validated result artifact provenance is invalid")

        acceptance_path = self._results_dir(project_id) / f"acceptance_{loop_id}.json"
        acceptance = {
            "schema_version": 1,
            "project_id": project_id,
            "loop_id": loop_id,
            "brief_id": loop.get("brief_id", ""),
            "result_artifact_path": str(expected_result),
            "result_task_id": task_id,
            "result_run_id": run_id,
            "accepted_by": str(accepted_by)[:200],
            "accepted_at": datetime.now(timezone.utc).isoformat(),
        }
        if acceptance_path.exists():
            try:
                existing = json.loads(acceptance_path.read_text(encoding="utf-8"))
            except (OSError, ValueError, TypeError) as exc:
                raise ValueError("acceptance artifact is corrupted") from exc
            if (
                existing.get("project_id") != project_id
                or existing.get("loop_id") != loop_id
                or existing.get("result_task_id") != task_id
                or existing.get("result_run_id") != run_id
            ):
                raise ValueError("acceptance artifact conflicts with this result")
        else:
            self._atomic_write_json(acceptance_path, acceptance)
        updated = self._update_loop(
            project_id,
            loop_id,
            status="ACCEPTED",
            acceptance_artifact_path=str(acceptance_path),
        )
        if updated is None:
            raise ValueError("accepted loop could not be persisted")
        return str(acceptance_path)

    def stop_loop(self, project_id: str, loop_id: str) -> bool:
        return self._update_loop(project_id, loop_id, status="STOPPED") is not None

    def record_development_result(
        self,
        *,
        project_id: str,
        loop_id: str,
        summary: str,
        evidence: dict[str, Any],
    ) -> DevelopmentResultReport:
        loop = next((item for item in self.list_loops(project_id) if item.get("loop_id") == loop_id), None)
        if not loop:
            return DevelopmentResultReport(success=False, blockers=["Loop not found."])
        results_dir = self._results_dir(project_id)
        results_dir.mkdir(parents=True, exist_ok=True)
        path = results_dir / RESULT_FILENAME_TEMPLATE.format(loop_id=loop_id)
        payload = {
            "loop_id": loop_id,
            "project_id": project_id,
            "brief_id": loop.get("brief_id", ""),
            "status": "READY_FOR_DISCUSSION_REVIEW",
            "summary": summary,
            "evidence": evidence,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        return DevelopmentResultReport(success=True, artifact_path=str(path))
