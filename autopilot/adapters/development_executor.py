"""Durable Development worker dispatch through Hermes Kanban.

The executor is deliberately host-mediated: every command is routed through a
runtime supplied by the plugin context. It never shells out directly and never
falls back to a shared workspace when worktree preflight fails.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
from typing import Protocol

from ..kill_switch import check_kill_switch
from ..lease import validate_lease, validate_lease_expired
from ..policy import check_capability, validate_lease_for_workspace
from ..constants import (
    CAP_GIT_COMMIT,
    CAP_GIT_PUSH,
    CAP_GIT_READ,
    CAP_NEXT_PHASE,
    CAP_USER_INTERACTION,
    CAP_WORKSPACE_READ,
    CAP_WORKSPACE_WRITE,
)
from ..verification import (
    VerificationProfile,
    verification_profile_digest,
    verification_profile_to_dict,
)
from .autonomous_loop import AutonomousLoop, AutonomousLoopSupervisor
from .execution_bridge import DevelopmentExecutionBrief


REQUIRED_EXECUTION_CAPABILITIES = (
    CAP_WORKSPACE_READ,
    CAP_GIT_READ,
    CAP_WORKSPACE_WRITE,
    CAP_GIT_COMMIT,
    CAP_GIT_PUSH,
    CAP_NEXT_PHASE,
    CAP_USER_INTERACTION,
)


@dataclass(frozen=True)
class CommandResult:
    exit_code: int
    stdout: str
    stderr: str


class CommandRuntime(Protocol):
    def run(
        self,
        argv: tuple[str, ...],
        *,
        cwd: str | None,
        timeout_seconds: int,
    ) -> CommandResult: ...


@dataclass
class DispatchResult:
    success: bool
    blockers: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    board_slug: str = ""
    development_task_id: str = ""
    verifier_task_id: str = ""
    starting_revision: str = ""
    verification_profile_digest: str = ""
    source_status_digest: str = ""
    dirty_workspace: bool = False


@dataclass
class RemediationDispatchResult:
    success: bool
    blockers: list[str] = field(default_factory=list)
    remediation_task_id: str = ""
    verifier_task_id: str = ""


class DevelopmentExecutor:
    """Preflight and enqueue one approved brief as a durable Kanban pipeline."""

    def __init__(
        self,
        runtime: CommandRuntime,
        *,
        supervisor: AutonomousLoopSupervisor | None = None,
    ):
        self._runtime = runtime
        self._supervisor = supervisor or AutonomousLoopSupervisor()

    def _run(
        self,
        argv: tuple[str, ...],
        *,
        cwd: str | None = None,
        timeout_seconds: int = 30,
    ) -> CommandResult:
        return self._runtime.run(argv, cwd=cwd, timeout_seconds=timeout_seconds)

    def _validate_contract(
        self,
        *,
        loop: AutonomousLoop,
        brief: DevelopmentExecutionBrief,
        state: dict,
        profile: VerificationProfile,
    ) -> list[str]:
        blockers: list[str] = []
        kill_reason = check_kill_switch()
        if kill_reason:
            blockers.append(f"Kill switch is active: {kill_reason}")
        if not brief.execution_authorized:
            blockers.append("Brief is not approved for execution.")
        registration = state.get("registration") or {}
        project_id = str(registration.get("project_id", ""))
        workspace_root = str(registration.get("workspace_root", ""))
        if not project_id or project_id != brief.project_id or project_id != loop.project_id:
            blockers.append("Project identity does not match registration, brief, and loop.")
        try:
            registered_root = str(Path(workspace_root).resolve(strict=True))
        except (OSError, RuntimeError):
            registered_root = ""
        if (
            not registered_root
            or registered_root != str(Path(brief.workspace_root).resolve(strict=False))
            or registered_root != str(Path(loop.workspace_root).resolve(strict=False))
            or registered_root != profile.workspace_root
        ):
            blockers.append("Workspace does not match the registered project scope.")
        lease_data = state.get("lease")
        if not isinstance(lease_data, dict):
            blockers.append("No active autonomy lease.")
            return blockers
        try:
            lease = validate_lease(lease_data)
        except ValueError as exc:
            blockers.append(f"Lease validation failed: {exc}")
            return blockers
        valid, reason = validate_lease_expired(lease)
        if not valid:
            blockers.append(reason)
        if lease.lease_id != loop.lease_id:
            blockers.append("Loop lease does not match the active lease.")
        if lease.project_id != project_id:
            blockers.append("Lease project does not match the active project.")
        workspace_valid, workspace_reason = validate_lease_for_workspace(lease, workspace_root)
        if not workspace_valid:
            blockers.append(workspace_reason)
        for capability in REQUIRED_EXECUTION_CAPABILITIES:
            granted, message = check_capability(lease, capability)
            if not granted:
                blockers.append(message)
        return blockers

    @staticmethod
    def _json_payload(output: str):
        text = (output or "").strip()
        if not text:
            raise ValueError("command returned no JSON")
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            for line in reversed(text.splitlines()):
                candidate = line.strip()
                if candidate.startswith(("{", "[")):
                    try:
                        return json.loads(candidate)
                    except json.JSONDecodeError:
                        continue
        raise ValueError("command returned invalid JSON")

    @staticmethod
    def _task_id(result: CommandResult) -> str:
        if result.exit_code != 0:
            return ""
        try:
            payload = DevelopmentExecutor._json_payload(result.stdout)
        except ValueError:
            return ""
        if not isinstance(payload, dict):
            return ""
        task_id = payload.get("id") or payload.get("task_id")
        return str(task_id).strip() if task_id else ""

    @staticmethod
    def _kill_switch_path() -> str:
        raw = os.environ.get("HERMES_HOME", "").strip()
        home = Path(raw).expanduser() if raw else Path.home() / ".hermes"
        return str(home / "state" / "autopilot" / "kill_switch.json")

    @staticmethod
    def _development_body(
        *,
        loop: AutonomousLoop,
        brief: DevelopmentExecutionBrief,
        profile: VerificationProfile,
        lease_expiry: str,
        starting_revision: str,
        dirty_workspace: bool,
    ) -> str:
        contract = {
            "contract_version": 1,
            "loop_id": loop.loop_id,
            "brief": brief.to_dict(),
            "verification_profile": verification_profile_to_dict(profile),
            "starting_revision": starting_revision,
            "main_workspace_was_dirty": dirty_workspace,
        }
        return "\n".join([
            "PROJECT AUTOPILOT — APPROVED DEVELOPMENT TASK",
            "",
            "Authority and scope:",
            f"- Work only in the dispatcher-provided worktree for project {brief.project_id}.",
            f"- Absolute lease expiry (UTC): {lease_expiry}",
            f"- Before every write or command, stop and call kanban_block if that time has passed or the kill switch is active at {DevelopmentExecutor._kill_switch_path()}.",
            "- The original registered workspace may contain uncommitted user work. Never edit, clean, reset, stash, checkout, or otherwise mutate it.",
            "- Do not run git commit, git push, merge, release, deploy, database migrations/writes, dependency installs, privileged-account actions, or personal-account flows.",
            "- Do not read or reproduce credentials. Redact secrets and personal identifiers from summaries and metadata.",
            "- Use existing project conventions. Before selecting any implementation alternative, call autopilot_decide with a stable question_id. The question policy auto-answers with the recommended/default choice per the user's blanket autonomy approval. You will never receive needs_human; proceed immediately on auto_answer.",
            "",
            "Execution protocol:",
            "1. Call kanban_show and inspect the full approved contract below.",
            "2. Confirm the worktree starts from starting_revision; do not modify the main workspace.",
            "3. Implement only the approved brief, using tests-first where behavior changes.",
            "4. Run every verification check exactly as argv (no shell composition), respecting cwd and timeout.",
            "5. Review the diff for scope, security, regressions, and accidental secret exposure.",
            "6. Complete with concise evidence metadata. Required keys: autopilot_contract_version=1, role='development', brief_id, changed_files, commands_run, verification_attempts, decisions, blocked_reason, residual_risk, starting_revision. Do not include raw logs or secrets.",
            "7. If any required step cannot be completed safely, call kanban_block instead of claiming success.",
            "",
            "Approved machine-readable contract:",
            json.dumps(contract, indent=2, sort_keys=True),
        ])

    @staticmethod
    def _verifier_body(
        *,
        loop: AutonomousLoop,
        brief: DevelopmentExecutionBrief,
        profile: VerificationProfile,
        lease_expiry: str,
        starting_revision: str,
    ) -> str:
        contract = {
            "contract_version": 1,
            "loop_id": loop.loop_id,
            "brief_id": brief.brief_id,
            "project_id": brief.project_id,
            "workspace_root": profile.workspace_root,
            "verification_profile": verification_profile_to_dict(profile),
            "starting_revision": starting_revision,
        }
        return "\n".join([
            "PROJECT AUTOPILOT — INDEPENDENT VERIFICATION TASK",
            "",
            f"Inspect only the completed Development worktree. Absolute lease expiry (UTC): {lease_expiry}.",
            f"Before every command, stop and call kanban_block if the lease expired or the kill switch is active at {DevelopmentExecutor._kill_switch_path()}.",
            "Do not edit files, install dependencies, start services, commit, push, deploy, migrate databases, or use privileged/personal accounts.",
            "Run every configured check exactly as argv without shell composition, from its declared relative cwd and within its timeout.",
            "Independently inspect the diff from starting_revision for scope, correctness, security, regressions, and secret exposure.",
            "Complete only after producing metadata with: autopilot_contract_version=1, role='verifier', brief_id, verification_status ('passed' or 'failed'), review_status ('approved' or 'rejected'), changed_files, checks (check_id, argv, cwd, exit_code, duration_seconds, redacted stdout_excerpt and stderr_excerpt), findings, residual_risk, starting_revision.",
            "A missing check, missing evidence, non-zero exit, out-of-scope change, or unverifiable claim means verification_status='failed' and review_status='rejected'. Never repair code in this task.",
            "Do not include raw logs, credentials, tokens, or personal identifiers in metadata.",
            "",
            "Machine-readable verification contract:",
            json.dumps(contract, indent=2, sort_keys=True),
        ])

    def dispatch(
        self,
        *,
        loop: AutonomousLoop,
        brief: DevelopmentExecutionBrief,
        state: dict,
        profile: VerificationProfile,
    ) -> DispatchResult:
        blockers = self._validate_contract(
            loop=loop,
            brief=brief,
            state=state,
            profile=profile,
        )
        if blockers:
            return DispatchResult(success=False, blockers=blockers)

        workspace = profile.workspace_root
        git_root = self._run(
            ("git", "rev-parse", "--show-toplevel"),
            cwd=workspace,
        )
        if git_root.exit_code != 0 or git_root.stdout.strip() != workspace:
            blockers.append("Registered workspace must be the root of an initialized Git repository.")

        revision = self._run(("git", "rev-parse", "HEAD"), cwd=workspace)
        if revision.exit_code != 0 or not revision.stdout.strip():
            blockers.append("Git repository has no HEAD revision; an isolated worktree cannot be created.")

        status = self._run(
            ("git", "status", "--porcelain=v1", "-z"),
            cwd=workspace,
        )
        if status.exit_code != 0:
            blockers.append("Unable to inspect Git workspace status.")

        for prerequisite in profile.prerequisites:
            available = self._run(("which", prerequisite), cwd=workspace)
            if available.exit_code != 0:
                blockers.append(f"Verification prerequisite is unavailable: {prerequisite}")

        gateway = self._run(("hermes", "gateway", "status"), cwd=None)
        gateway_output = f"{gateway.stdout}\n{gateway.stderr}".lower()
        if (
            gateway.exit_code != 0
            or "not running" in gateway_output
            or "stopped" in gateway_output
            or "running" not in gateway_output
        ):
            blockers.append(
                "Hermes gateway dispatcher is not running. Start it with "
                "`hermes gateway run` or install the user service before launch."
            )

        if blockers:
            return DispatchResult(
                success=False,
                blockers=blockers,
                starting_revision=revision.stdout.strip() if revision.exit_code == 0 else "",
                dirty_workspace=bool(status.stdout) if status.exit_code == 0 else False,
            )

        project_id = brief.project_id
        if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,63}", project_id):
            return DispatchResult(
                success=False,
                blockers=["Project id is not a valid isolated Kanban board slug."],
                starting_revision=revision.stdout.strip(),
                dirty_workspace=bool(status.stdout),
            )

        project = self._run(("hermes", "project", "show", project_id), cwd=None)
        primary = ""
        if project.exit_code == 0:
            for line in project.stdout.splitlines():
                if line.strip().startswith("primary:"):
                    primary = line.split(":", 1)[1].strip()
                    break
        try:
            primary_root = str(Path(primary).expanduser().resolve(strict=True)) if primary else ""
        except (OSError, RuntimeError):
            primary_root = ""
        if project.exit_code != 0 or primary_root != workspace:
            return DispatchResult(
                success=False,
                blockers=[
                    "Registered Autopilot project is not bound to a matching Hermes Project primary workspace."
                ],
                starting_revision=revision.stdout.strip(),
                dirty_workspace=bool(status.stdout),
            )

        boards_result = self._run(
            ("hermes", "kanban", "boards", "list", "--json"),
            cwd=None,
        )
        try:
            boards_payload = self._json_payload(boards_result.stdout)
        except ValueError:
            boards_payload = []
        if isinstance(boards_payload, dict):
            boards = boards_payload.get("boards", [])
        else:
            boards = boards_payload
        board_exists = isinstance(boards, list) and any(
            isinstance(item, dict) and item.get("slug") == project_id
            for item in boards
        )
        if boards_result.exit_code != 0:
            return DispatchResult(
                success=False,
                blockers=["Unable to inspect project-scoped Kanban boards."],
                starting_revision=revision.stdout.strip(),
                dirty_workspace=bool(status.stdout),
            )
        if not board_exists:
            create_board = self._run(
                (
                    "hermes",
                    "kanban",
                    "boards",
                    "create",
                    project_id,
                    "--name",
                    brief.display_title or project_id,
                    "--description",
                    f"Project Autopilot queue for {project_id}",
                    "--default-workdir",
                    workspace,
                ),
                cwd=None,
            )
            if create_board.exit_code != 0:
                return DispatchResult(
                    success=False,
                    blockers=[f"Unable to create isolated Kanban board: {create_board.stderr or create_board.stdout}"],
                    starting_revision=revision.stdout.strip(),
                    dirty_workspace=bool(status.stdout),
                )

        lease = validate_lease(state["lease"])
        remaining = int(min(lease.remaining_seconds(), float(lease.max_runtime_seconds)))
        if remaining < 60:
            return DispatchResult(
                success=False,
                blockers=["Lease has less than 60 seconds remaining; approve a new lease before launch."],
                board_slug=project_id,
                starting_revision=revision.stdout.strip(),
                dirty_workspace=bool(status.stdout),
            )
        runtime_arg = f"{remaining}s"
        starting_revision = revision.stdout.strip()
        dirty_workspace = bool(status.stdout)
        source_status_digest = hashlib.sha256(
            status.stdout.encode("utf-8")
        ).hexdigest()
        warnings: list[str] = []
        if dirty_workspace:
            warnings.append(
                "The main workspace is dirty. It will remain untouched; the worker starts from HEAD in an isolated worktree and will not see uncommitted main-workspace changes."
            )

        development = self._run(
            (
                "hermes",
                "kanban",
                "--board",
                project_id,
                "create",
                f"Autopilot development: {brief.brief_id}",
                "--assignee",
                "default",
                "--project",
                project_id,
                "--workspace",
                "worktree",
                "--tenant",
                f"autopilot:{project_id}",
                "--priority",
                "2",
                "--idempotency-key",
                f"autopilot:{loop.loop_id}:development",
                "--max-runtime",
                runtime_arg,
                "--max-retries",
                "1",
                "--created-by",
                "project-autopilot",
                "--skill",
                "test-driven-development",
                "--skill",
                "systematic-debugging",
                "--initial-status",
                "blocked",
                "--body",
                self._development_body(
                    loop=loop,
                    brief=brief,
                    profile=profile,
                    lease_expiry=lease.expiry,
                    starting_revision=starting_revision,
                    dirty_workspace=dirty_workspace,
                ),
                "--json",
            ),
            cwd=None,
            timeout_seconds=60,
        )
        development_task_id = self._task_id(development)
        if not re.fullmatch(r"[A-Za-z0-9_-]+", development_task_id):
            return DispatchResult(
                success=False,
                blockers=[f"Development task creation failed: {development.stderr or development.stdout}"],
                warnings=warnings,
                board_slug=project_id,
                starting_revision=starting_revision,
                dirty_workspace=dirty_workspace,
            )

        worktree_path = str(Path(workspace) / ".worktrees" / development_task_id)
        verifier = self._run(
            (
                "hermes",
                "kanban",
                "--board",
                project_id,
                "create",
                f"Autopilot verification: {brief.brief_id}",
                "--assignee",
                "default",
                "--parent",
                development_task_id,
                "--workspace",
                f"dir:{worktree_path}",
                "--tenant",
                f"autopilot:{project_id}",
                "--priority",
                "2",
                "--idempotency-key",
                f"autopilot:{loop.loop_id}:verification:0",
                "--max-runtime",
                runtime_arg,
                "--max-retries",
                "1",
                "--created-by",
                "project-autopilot",
                "--skill",
                "requesting-code-review",
                "--body",
                self._verifier_body(
                    loop=loop,
                    brief=brief,
                    profile=profile,
                    lease_expiry=lease.expiry,
                    starting_revision=starting_revision,
                ),
                "--json",
            ),
            cwd=None,
            timeout_seconds=60,
        )
        verifier_task_id = self._task_id(verifier)
        if not re.fullmatch(r"[A-Za-z0-9_-]+", verifier_task_id):
            return DispatchResult(
                success=False,
                blockers=[
                    "Verifier task creation failed; the Development card remains blocked and was not dispatched. "
                    f"Details: {verifier.stderr or verifier.stdout}"
                ],
                warnings=warnings,
                board_slug=project_id,
                development_task_id=development_task_id,
                starting_revision=starting_revision,
                dirty_workspace=dirty_workspace,
            )

        profile_digest = verification_profile_digest(profile)
        bound = self._supervisor.mark_dispatched(
            project_id=project_id,
            loop_id=loop.loop_id,
            board_slug=project_id,
            development_task_id=development_task_id,
            verifier_task_id=verifier_task_id,
            starting_revision=starting_revision,
            verification_profile_digest=profile_digest,
            source_status_digest=source_status_digest,
            dirty_workspace=dirty_workspace,
        )
        if bound is None:
            return DispatchResult(
                success=False,
                blockers=[
                    "Kanban cards remain blocked because their durable policy binding could not be persisted."
                ],
                warnings=warnings,
                board_slug=project_id,
                development_task_id=development_task_id,
                verifier_task_id=verifier_task_id,
                starting_revision=starting_revision,
                dirty_workspace=dirty_workspace,
            )

        promoted = self._run(
            ("hermes", "kanban", "--board", project_id, "promote", development_task_id),
            cwd=None,
        )
        if promoted.exit_code != 0:
            return DispatchResult(
                success=False,
                blockers=[
                    "Kanban pipeline was created but the Development card remains blocked because promotion failed."
                ],
                warnings=warnings,
                board_slug=project_id,
                development_task_id=development_task_id,
                verifier_task_id=verifier_task_id,
                starting_revision=starting_revision,
                dirty_workspace=dirty_workspace,
            )

        return DispatchResult(
            success=True,
            warnings=warnings,
            board_slug=project_id,
            development_task_id=development_task_id,
            verifier_task_id=verifier_task_id,
            starting_revision=starting_revision,
            verification_profile_digest=profile_digest,
            source_status_digest=source_status_digest,
            dirty_workspace=dirty_workspace,
        )

    def queue_remediation(
        self,
        *,
        loop: dict,
        state: dict,
        profile: VerificationProfile,
        failed_evidence: dict,
    ) -> RemediationDispatchResult:
        """Atomically stage one remediation + re-verifier pair, then release it."""

        kill_reason = check_kill_switch()
        if kill_reason:
            return RemediationDispatchResult(False, [f"Kill switch active: {kill_reason}"])
        lease_data = state.get("lease")
        if not isinstance(lease_data, dict):
            return RemediationDispatchResult(False, ["Active lease is missing."])
        try:
            lease = validate_lease(lease_data)
        except ValueError as exc:
            return RemediationDispatchResult(False, [f"Lease is invalid: {exc}"])
        valid, error = validate_lease_expired(lease)
        if not valid or lease.lease_id != loop.get("lease_id"):
            return RemediationDispatchResult(False, [error or "Loop lease was replaced."])

        board = loop.get("board_slug")
        loop_id = loop.get("loop_id")
        parent_verifier = loop.get("verifier_task_id")
        development_task_id = loop.get("development_task_id")
        cycle = loop.get("remediation_count", 0) + 1
        if (
            not isinstance(board, str)
            or not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,63}", board)
            or not isinstance(loop_id, str)
            or not re.fullmatch(r"[A-Za-z0-9_-]+", loop_id)
            or not isinstance(parent_verifier, str)
            or not re.fullmatch(r"[A-Za-z0-9_-]+", parent_verifier)
            or not isinstance(development_task_id, str)
            or not re.fullmatch(r"[A-Za-z0-9_-]+", development_task_id)
            or not isinstance(cycle, int)
            or cycle < 1
            or cycle > profile.max_remediation_cycles
        ):
            return RemediationDispatchResult(False, ["Remediation binding or cycle limit is invalid."])

        worktree = Path(profile.workspace_root) / ".worktrees" / development_task_id
        try:
            worktree = worktree.resolve(strict=True)
            worktree.relative_to(Path(profile.workspace_root).resolve(strict=True))
        except (OSError, RuntimeError, ValueError):
            return RemediationDispatchResult(False, ["Development worktree is missing or unsafe."])

        remaining = int(min(lease.remaining_seconds(), float(lease.max_runtime_seconds)))
        if remaining < 60:
            return RemediationDispatchResult(False, ["Lease has less than 60 seconds remaining."])
        runtime_arg = f"{remaining}s"
        evidence_json = json.dumps(failed_evidence, indent=2, sort_keys=True)
        remediation_body = "\n".join([
            "PROJECT AUTOPILOT — BOUNDED REMEDIATION",
            f"Brief ID: {loop.get('brief_id', '')}",
            f"Cycle: {cycle}/{profile.max_remediation_cycles}",
            f"Absolute lease expiry (UTC): {lease.expiry}",
            f"Before every tool call, stop and call kanban_block if the lease expired or the kill switch is active at {self._kill_switch_path()}.",
            "Work only in the existing isolated Development worktree. Repair only the verified failures/findings below; do not expand the approved brief.",
            "Do not commit, push, merge, deploy, migrate databases, install dependencies, access credentials/personal accounts, or mutate the original workspace.",
            "Use tests first, run the exact verification profile, review the diff, then complete with the development metadata contract. Before selecting any implementation alternative, call autopilot_decide with a stable question_id. The question policy auto-answers with the recommended/default choice per the user's blanket autonomy approval — proceed immediately on auto_answer.",
            "Failed verifier evidence (already bounded and redacted):",
            evidence_json,
        ])
        remediation = self._run(
            (
                "hermes", "kanban", "--board", board, "create",
                f"Autopilot remediation: {loop.get('brief_id', '')} cycle {cycle}",
                "--assignee", "default",
                "--parent", parent_verifier,
                "--workspace", f"dir:{worktree}",
                "--tenant", f"autopilot:{profile.project_id}",
                "--priority", "2",
                "--idempotency-key", f"autopilot:{loop_id}:remediation:{cycle}",
                "--max-runtime", runtime_arg,
                "--max-retries", "1",
                "--created-by", "project-autopilot",
                "--skill", "test-driven-development",
                "--skill", "systematic-debugging",
                "--initial-status", "blocked",
                "--body", remediation_body,
                "--json",
            ),
            cwd=None,
            timeout_seconds=60,
        )
        remediation_task_id = self._task_id(remediation)
        if not re.fullmatch(r"[A-Za-z0-9_-]+", remediation_task_id):
            return RemediationDispatchResult(
                False,
                [f"Remediation task creation failed: {remediation.stderr or remediation.stdout}"],
            )

        verifier_body = "\n".join([
            "PROJECT AUTOPILOT — INDEPENDENT RE-VERIFICATION",
            f"Brief ID: {loop.get('brief_id', '')}",
            f"Cycle: {cycle}/{profile.max_remediation_cycles}",
            f"Absolute lease expiry (UTC): {lease.expiry}",
            f"Before every tool call, stop and call kanban_block if the lease expired or the kill switch is active at {self._kill_switch_path()}.",
            "Remain read-only. Run every verification command exactly as argv, independently review the full diff from the starting revision, and return the same verifier metadata contract.",
            "Do not repair, commit, push, deploy, migrate, install, or use credentials/personal accounts. Any missing evidence or scope violation is a failed/rejected result.",
            "Verification profile:",
            json.dumps(verification_profile_to_dict(profile), indent=2, sort_keys=True),
            f"Starting revision: {loop.get('starting_revision', '')}",
        ])
        verifier = self._run(
            (
                "hermes", "kanban", "--board", board, "create",
                f"Autopilot re-verification: {loop.get('brief_id', '')} cycle {cycle}",
                "--assignee", "default",
                "--parent", remediation_task_id,
                "--workspace", f"dir:{worktree}",
                "--tenant", f"autopilot:{profile.project_id}",
                "--priority", "2",
                "--idempotency-key", f"autopilot:{loop_id}:verification:{cycle}",
                "--max-runtime", runtime_arg,
                "--max-retries", "1",
                "--created-by", "project-autopilot",
                "--skill", "requesting-code-review",
                "--body", verifier_body,
                "--json",
            ),
            cwd=None,
            timeout_seconds=60,
        )
        verifier_task_id = self._task_id(verifier)
        if not re.fullmatch(r"[A-Za-z0-9_-]+", verifier_task_id):
            return RemediationDispatchResult(
                False,
                ["Re-verifier creation failed; remediation remains blocked."],
                remediation_task_id=remediation_task_id,
            )

        bound = self._supervisor.mark_status(
            project_id=profile.project_id,
            loop_id=loop_id,
            status="REMEDIATING",
            remediation_count=cycle,
            current_remediation_task_id=remediation_task_id,
            verifier_task_id=verifier_task_id,
        )
        if bound is None:
            return RemediationDispatchResult(
                False,
                ["Remediation cards remain blocked because their durable policy binding could not be persisted."],
                remediation_task_id=remediation_task_id,
                verifier_task_id=verifier_task_id,
            )

        promoted = self._run(
            ("hermes", "kanban", "--board", board, "promote", remediation_task_id),
            cwd=None,
        )
        if promoted.exit_code != 0:
            return RemediationDispatchResult(
                False,
                ["Remediation pipeline exists but remains blocked because promotion failed."],
                remediation_task_id=remediation_task_id,
                verifier_task_id=verifier_task_id,
            )
        return RemediationDispatchResult(
            True,
            remediation_task_id=remediation_task_id,
            verifier_task_id=verifier_task_id,
        )
