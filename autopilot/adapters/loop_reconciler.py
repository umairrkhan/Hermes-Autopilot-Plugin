"""Authoritative reconciliation from durable Hermes Kanban state."""

from __future__ import annotations

from dataclasses import dataclass, field
import json
from typing import Any

from ..evidence import validate_verifier_evidence
from ..kill_switch import check_kill_switch
from ..lease import validate_lease, validate_lease_expired
from ..verification import VerificationProfile, verification_profile_digest
from .autonomous_loop import AutonomousLoopSupervisor
from .development_executor import CommandRuntime, DevelopmentExecutor


@dataclass
class SyncResult:
    status: str
    message: str
    evidence_path: str = ""
    blockers: list[str] = field(default_factory=list)


class LoopReconciler:
    """Derive one loop state from its durable Kanban tasks and run metadata."""

    def __init__(self, runtime: CommandRuntime, supervisor: AutonomousLoopSupervisor):
        self._runtime = runtime
        self._supervisor = supervisor

    def _show(self, board: str, task_id: str) -> tuple[dict[str, Any] | None, str]:
        result = self._runtime.run(
            ("hermes", "kanban", "--board", board, "show", task_id, "--json"),
            cwd=None,
            timeout_seconds=60,
        )
        if result.exit_code != 0:
            return None, result.stderr or result.stdout or "Kanban show failed"
        try:
            payload = json.loads(result.stdout)
        except (TypeError, json.JSONDecodeError):
            return None, "Kanban show returned invalid JSON"
        if not isinstance(payload, dict):
            return None, "Kanban show returned an invalid object"
        task = payload.get("task")
        if not isinstance(task, dict) or task.get("id") != task_id:
            return None, "Kanban task identity does not match the loop binding"
        return payload, ""

    def _set_status(self, loop: dict[str, Any], status: str) -> None:
        self._supervisor.mark_status(
            project_id=str(loop["project_id"]),
            loop_id=str(loop["loop_id"]),
            status=status,
        )

    def cancel(self, *, loop: dict[str, Any], reason: str) -> SyncResult:
        """Request worker termination and block every unfinished pipeline card."""

        project_id = loop.get("project_id")
        loop_id = loop.get("loop_id")
        board = loop.get("board_slug")
        if not all(isinstance(value, str) and value for value in (project_id, loop_id, board)):
            return SyncResult("NEEDS_HUMAN", "Loop cancellation binding is invalid.")
        project_id = str(project_id)
        loop_id = str(loop_id)
        board = str(board)
        self._supervisor.mark_status(
            project_id=project_id,
            loop_id=loop_id,
            status="CANCEL_REQUESTED",
        )
        reason = str(reason or "Autonomy canceled")[:300]
        task_ids: list[str] = []
        for key in (
            "development_task_id",
            "current_remediation_task_id",
            "verifier_task_id",
        ):
            task_id = loop.get(key)
            if isinstance(task_id, str) and task_id and task_id not in task_ids:
                task_ids.append(task_id)
        if not task_ids:
            return SyncResult("CANCEL_REQUESTED", "No durable task ids were available to confirm cancellation.")

        blockers: list[str] = []
        for task_id in task_ids:
            payload, error = self._show(board, task_id)
            if payload is None:
                blockers.append(f"{task_id}: {error}")
                continue
            status = payload["task"].get("status")
            if status in {"done", "blocked", "archived"}:
                continue
            if status == "running":
                reclaimed = self._runtime.run(
                    (
                        "hermes", "kanban", "--board", board,
                        "reclaim", task_id, "--reason", reason,
                    ),
                    cwd=None,
                    timeout_seconds=60,
                )
                if reclaimed.exit_code != 0:
                    blockers.append(
                        f"{task_id}: worker reclaim was not confirmed ({reclaimed.stderr or reclaimed.stdout})"
                    )
            blocked = self._runtime.run(
                (
                    "hermes", "kanban", "--board", board,
                    "block", task_id, reason, "--kind", "capability",
                ),
                cwd=None,
                timeout_seconds=60,
            )
            if blocked.exit_code != 0:
                blockers.append(
                    f"{task_id}: task block was not confirmed ({blocked.stderr or blocked.stdout})"
                )

        if blockers:
            return SyncResult(
                "CANCEL_REQUESTED",
                "Cancellation was requested but could not be confirmed for every task.",
                blockers=blockers,
            )
        self._supervisor.mark_status(
            project_id=project_id,
            loop_id=loop_id,
            status="CANCELED",
        )
        return SyncResult("CANCELED", "All unfinished Kanban tasks were reclaimed and blocked.")

    def sync(
        self,
        *,
        loop: dict[str, Any],
        state: dict[str, Any],
        profile: VerificationProfile,
    ) -> SyncResult:
        required_strings = (
            "loop_id",
            "project_id",
            "brief_id",
            "lease_id",
            "board_slug",
            "development_task_id",
            "verifier_task_id",
            "starting_revision",
            "verification_profile_digest",
        )
        if any(not isinstance(loop.get(key), str) or not loop.get(key) for key in required_strings):
            return SyncResult("NEEDS_HUMAN", "Loop artifact is missing its durable dispatch binding.")
        project_id = str(loop["project_id"])
        loop_id = str(loop["loop_id"])
        if profile.project_id != project_id:
            self._set_status(loop, "NEEDS_HUMAN")
            return SyncResult("NEEDS_HUMAN", "Verification profile project changed after dispatch.")
        if verification_profile_digest(profile) != loop["verification_profile_digest"]:
            self._set_status(loop, "NEEDS_HUMAN")
            return SyncResult("NEEDS_HUMAN", "Verification profile changed after dispatch; re-approval is required.")

        kill_reason = check_kill_switch()
        if kill_reason:
            return self.cancel(loop=loop, reason=f"Kill switch active: {kill_reason}")
        lease_data = state.get("lease")
        if not isinstance(lease_data, dict):
            return self.cancel(loop=loop, reason="Active autonomy lease is missing")
        try:
            lease = validate_lease(lease_data)
        except ValueError as exc:
            return self.cancel(loop=loop, reason=f"Active autonomy lease is invalid: {exc}")
        lease_valid, lease_error = validate_lease_expired(lease)
        if not lease_valid or lease.lease_id != loop["lease_id"]:
            return self.cancel(loop=loop, reason=lease_error or "Loop lease was replaced")

        board = str(loop["board_slug"])
        development_task_id = str(loop["development_task_id"])
        verifier_task_id = str(loop["verifier_task_id"])
        development, error = self._show(board, development_task_id)
        if development is None:
            self._set_status(loop, "NEEDS_HUMAN")
            return SyncResult("NEEDS_HUMAN", "Cannot reconcile Development task.", blockers=[error])
        development_status = development["task"].get("status")
        if development_status == "blocked":
            self._set_status(loop, "NEEDS_HUMAN")
            return SyncResult("NEEDS_HUMAN", "Development worker is blocked.")
        if development_status != "done":
            status = "RUNNING" if development_status == "running" else "QUEUED"
            self._set_status(loop, status)
            return SyncResult(status, f"Development task is {development_status}.")

        verifier, error = self._show(board, verifier_task_id)
        if verifier is None:
            self._set_status(loop, "NEEDS_HUMAN")
            return SyncResult("NEEDS_HUMAN", "Cannot reconcile verifier task.", blockers=[error])
        verifier_status = verifier["task"].get("status")
        if verifier_status == "blocked":
            self._set_status(loop, "NEEDS_HUMAN")
            return SyncResult("NEEDS_HUMAN", "Verifier worker is blocked.")
        if verifier_status != "done":
            self._set_status(loop, "VERIFYING")
            return SyncResult("VERIFYING", f"Verifier task is {verifier_status}.")

        runs = verifier.get("runs")
        if not isinstance(runs, list):
            self._set_status(loop, "NEEDS_HUMAN")
            return SyncResult("NEEDS_HUMAN", "Verifier has no durable run history.")
        completed_runs = [
            run for run in runs
            if isinstance(run, dict)
            and run.get("status") == "done"
            and run.get("outcome") == "completed"
            and isinstance(run.get("id"), int)
        ]
        if not completed_runs:
            self._set_status(loop, "NEEDS_HUMAN")
            return SyncResult("NEEDS_HUMAN", "Verifier completed without a completed Kanban run.")
        run = max(completed_runs, key=lambda item: item["id"])
        try:
            evidence = validate_verifier_evidence(
                run.get("metadata"),
                project_id=project_id,
                loop_id=loop_id,
                brief_id=str(loop["brief_id"]),
                board_slug=board,
                task_id=verifier_task_id,
                run_id=run["id"],
                starting_revision=str(loop["starting_revision"]),
                profile=profile,
            )
        except ValueError as exc:
            self._set_status(loop, "NEEDS_HUMAN")
            return SyncResult("NEEDS_HUMAN", "Verifier evidence was rejected.", blockers=[str(exc)])

        if not evidence.accepted:
            remediation_count = loop.get("remediation_count", 0)
            if not isinstance(remediation_count, int) or remediation_count < 0:
                self._set_status(loop, "NEEDS_HUMAN")
                return SyncResult("NEEDS_HUMAN", "Loop remediation counter is invalid.")
            if remediation_count >= profile.max_remediation_cycles:
                self._set_status(loop, "NEEDS_HUMAN")
                return SyncResult(
                    "NEEDS_HUMAN",
                    "Verification failed and the authorized remediation limit is exhausted.",
                    blockers=list(evidence.findings),
                )
            remediation = DevelopmentExecutor(
                self._runtime,
                supervisor=self._supervisor,
            ).queue_remediation(
                loop=loop,
                state=state,
                profile=profile,
                failed_evidence=evidence.to_dict(),
            )
            if not remediation.success:
                self._set_status(loop, "NEEDS_HUMAN")
                return SyncResult(
                    "NEEDS_HUMAN",
                    "Verification failed and remediation could not be queued safely.",
                    blockers=remediation.blockers,
                )
            return SyncResult(
                "REMEDIATING",
                f"Verification failed; remediation cycle {remediation_count + 1}/{profile.max_remediation_cycles} was queued.",
            )

        try:
            evidence_path = self._supervisor.record_verification_evidence(
                project_id=project_id,
                loop_id=loop_id,
                evidence=evidence.to_dict(),
            )
        except (OSError, ValueError) as exc:
            self._set_status(loop, "NEEDS_HUMAN")
            return SyncResult("NEEDS_HUMAN", "Validated evidence could not be persisted.", blockers=[str(exc)])
        return SyncResult(
            "AWAITING_HUMAN_ACCEPTANCE",
            "Verification and independent review passed. Human acceptance is still required.",
            evidence_path=evidence_path,
        )
