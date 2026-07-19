"""Authoritative reconciliation from durable Hermes Kanban state."""

from __future__ import annotations

from dataclasses import dataclass, field
import json
from pathlib import Path
import re
from typing import Any

from ..constants import CAP_GIT_COMMIT, CAP_GIT_PUSH
from ..evidence import validate_verifier_evidence
from ..kill_switch import check_kill_switch
from ..lease import AutonomyLease, validate_lease, validate_lease_expired
from ..policy import check_capability, validate_lease_for_workspace
from ..verification import VerificationProfile, verification_profile_digest
from .autonomous_loop import AutonomousLoopSupervisor
from .development_executor import CommandRuntime, DevelopmentExecutor


@dataclass
class SyncResult:
    status: str
    message: str
    evidence_path: str = ""
    commit_revision: str = ""
    blockers: list[str] = field(default_factory=list)


@dataclass
class MergeResult:
    success: bool
    commit_revision: str = ""
    target_branch: str = ""
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

    @staticmethod
    def _target_branch(starting_revision: str) -> str:
        prefix = "ref: refs/heads/"
        branch = (
            starting_revision[len(prefix):]
            if starting_revision.startswith(prefix)
            else "Development"
        )
        if (
            not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,254}", branch)
            or ".." in branch
            or "//" in branch
            or "@{" in branch
            or branch.endswith(("/", ".", ".lock"))
        ):
            return ""
        return branch

    def _merge_worktree(
        self,
        *,
        loop: dict[str, Any],
        lease: AutonomyLease,
    ) -> MergeResult:
        """Commit and push only the verified Development worktree."""

        project_id = loop.get("project_id")
        loop_id = loop.get("loop_id")
        task_id = loop.get("development_task_id")
        brief_id = loop.get("brief_id")
        workspace_raw = loop.get("workspace_root")
        starting_revision = loop.get("starting_revision")
        if not all(
            isinstance(value, str) and value
            for value in (
                project_id,
                loop_id,
                task_id,
                brief_id,
                workspace_raw,
                starting_revision,
            )
        ):
            return MergeResult(False, blockers=["Loop promotion binding is incomplete."])
        if not re.fullmatch(r"[A-Za-z0-9_-]+", str(task_id)):
            return MergeResult(False, blockers=["Development task id is unsafe for promotion."])
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,199}", str(brief_id)):
            return MergeResult(False, blockers=["Brief id is unsafe for the commit message."])

        target_branch = self._target_branch(str(starting_revision))
        if not target_branch:
            return MergeResult(False, blockers=["Loop target branch is invalid."])
        if lease.git_policy != "allow-list":
            return MergeResult(False, target_branch=target_branch, blockers=[
                "Active lease does not authorize post-verification Git promotion."
            ])
        for capability in (CAP_GIT_COMMIT, CAP_GIT_PUSH):
            granted, message = check_capability(lease, capability)
            if not granted:
                return MergeResult(False, target_branch=target_branch, blockers=[message])
        workspace_valid, workspace_error = validate_lease_for_workspace(
            lease,
            str(workspace_raw),
        )
        if not workspace_valid:
            return MergeResult(False, target_branch=target_branch, blockers=[workspace_error])

        try:
            workspace = Path(str(workspace_raw)).expanduser().resolve(strict=True)
            worktrees_root = (workspace / ".worktrees").resolve(strict=True)
            worktree = (worktrees_root / str(task_id)).resolve(strict=True)
            worktree.relative_to(worktrees_root)
        except (OSError, RuntimeError, ValueError):
            return MergeResult(False, target_branch=target_branch, blockers=[
                "Verified Development worktree is missing or unsafe."
            ])

        git_root = self._runtime.run(
            ("git", "rev-parse", "--show-toplevel"),
            cwd=str(worktree),
            timeout_seconds=60,
        )
        try:
            resolved_git_root = Path(git_root.stdout.strip()).resolve(strict=True)
        except (OSError, RuntimeError):
            resolved_git_root = Path()
        if git_root.exit_code != 0 or resolved_git_root != worktree:
            return MergeResult(False, target_branch=target_branch, blockers=[
                "Promotion path is not the bound Git worktree."
            ])

        subject = f"autopilot({task_id}): {brief_id}"
        body = "Automated commit by Autopilot post-verification."
        status = self._runtime.run(
            ("git", "status", "--porcelain=v1", "-z", "--untracked-files=all"),
            cwd=str(worktree),
            timeout_seconds=60,
        )
        if status.exit_code != 0:
            return MergeResult(False, target_branch=target_branch, blockers=[
                "Verified worktree status could not be inspected before promotion."
            ])

        commit_revision = ""
        if status.stdout:
            staged = self._runtime.run(
                ("git", "add", "--all"),
                cwd=str(worktree),
                timeout_seconds=60,
            )
            if staged.exit_code != 0:
                return MergeResult(False, target_branch=target_branch, blockers=[
                    "Git staging failed in the verified worktree."
                ])
            committed = self._runtime.run(
                ("git", "commit", "-m", subject, "-m", body),
                cwd=str(worktree),
                timeout_seconds=120,
            )
            if committed.exit_code != 0:
                return MergeResult(False, target_branch=target_branch, blockers=[
                    "Git commit failed in the verified worktree."
                ])
        else:
            last_subject = self._runtime.run(
                ("git", "log", "-1", "--pretty=%s"),
                cwd=str(worktree),
                timeout_seconds=60,
            )
            bound_revision = loop.get("commit_revision")
            has_bound_revision = isinstance(bound_revision, str) and bool(bound_revision)
            if last_subject.exit_code != 0 or (
                last_subject.stdout.strip() != subject
                and not has_bound_revision
            ):
                return MergeResult(False, target_branch=target_branch, blockers=[
                    "Verified worktree has no changes to promote."
                ])

        revision = self._runtime.run(
            ("git", "rev-parse", "HEAD"),
            cwd=str(worktree),
            timeout_seconds=60,
        )
        commit_revision = revision.stdout.strip()
        if revision.exit_code != 0 or not re.fullmatch(r"[0-9a-fA-F]{7,64}", commit_revision):
            return MergeResult(False, target_branch=target_branch, blockers=[
                "Promoted commit revision could not be verified."
            ])
        bound_revision = loop.get("commit_revision")
        if isinstance(bound_revision, str) and bound_revision and bound_revision != commit_revision:
            return MergeResult(False, commit_revision=commit_revision, target_branch=target_branch, blockers=[
                "Worktree HEAD no longer matches the loop's recorded promotion revision."
            ])

        persisted = self._supervisor.mark_status(
            project_id=str(project_id),
            loop_id=str(loop_id),
            status="MERGING",
            commit_revision=commit_revision,
        )
        if persisted is None:
            return MergeResult(False, commit_revision=commit_revision, target_branch=target_branch, blockers=[
                "Commit revision could not be bound before push."
            ])

        pushed = self._runtime.run(
            ("git", "push", "origin", f"HEAD:refs/heads/{target_branch}"),
            cwd=str(worktree),
            timeout_seconds=180,
        )
        if pushed.exit_code != 0:
            return MergeResult(False, commit_revision=commit_revision, target_branch=target_branch, blockers=[
                f"Verified commit could not be pushed to {target_branch}."
            ])
        return MergeResult(
            True,
            commit_revision=commit_revision,
            target_branch=target_branch,
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

        existing_revision = loop.get("commit_revision")
        if (
            loop.get("status") == "AWAITING_HUMAN_ACCEPTANCE"
            and isinstance(existing_revision, str)
            and re.fullmatch(r"[0-9a-fA-F]{7,64}", existing_revision)
        ):
            return SyncResult(
                "AWAITING_HUMAN_ACCEPTANCE",
                "Verified worktree was already promoted; human acceptance is still required.",
                evidence_path=str(loop.get("result_artifact_path", "")),
                commit_revision=existing_revision,
            )

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

        merging = self._supervisor.mark_status(
            project_id=project_id,
            loop_id=loop_id,
            status="MERGING",
        )
        if merging is None:
            self._set_status(loop, "NEEDS_HUMAN")
            return SyncResult(
                "NEEDS_HUMAN",
                "Verification passed, but the merge phase could not be persisted.",
                evidence_path=evidence_path,
            )
        merged = self._merge_worktree(loop=merging, lease=lease)
        if not merged.success:
            fields: dict[str, Any] = {}
            if merged.commit_revision:
                fields["commit_revision"] = merged.commit_revision
            self._supervisor.mark_status(
                project_id=project_id,
                loop_id=loop_id,
                status="NEEDS_HUMAN",
                **fields,
            )
            return SyncResult(
                "NEEDS_HUMAN",
                "Verification passed, but automatic worktree promotion failed.",
                evidence_path=evidence_path,
                commit_revision=merged.commit_revision,
                blockers=merged.blockers,
            )
        promoted = self._supervisor.mark_status(
            project_id=project_id,
            loop_id=loop_id,
            status="AWAITING_HUMAN_ACCEPTANCE",
            commit_revision=merged.commit_revision,
        )
        if promoted is None:
            return SyncResult(
                "NEEDS_HUMAN",
                "The verified commit was pushed, but its loop binding could not be finalized.",
                evidence_path=evidence_path,
                commit_revision=merged.commit_revision,
                blockers=[f"Pushed revision {merged.commit_revision} requires binding repair."],
            )
        return SyncResult(
            "AWAITING_HUMAN_ACCEPTANCE",
            f"Verification passed and {merged.commit_revision} was pushed to "
            f"{merged.target_branch}. Human acceptance is still required.",
            evidence_path=evidence_path,
            commit_revision=merged.commit_revision,
        )
