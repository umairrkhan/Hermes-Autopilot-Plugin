"""Tests for authoritative Kanban loop reconciliation."""

from __future__ import annotations

from datetime import datetime, timezone
import json

from autopilot.adapters.autonomous_loop import AutonomousLoopSupervisor
from autopilot.adapters.development_executor import CommandResult
from autopilot.adapters.loop_reconciler import LoopReconciler
from autopilot.lease import build_lease_preset, lease_to_dict
from autopilot.lifecycle import recover_active_loops
from autopilot.storage import save_project_state
from autopilot.verification import (
    save_verification_profile,
    validate_verification_profile,
    verification_profile_digest,
)


class FakeRuntime:
    def __init__(self, responses):
        self.responses = responses
        self.calls = []

    def run(self, argv, *, cwd, timeout_seconds):
        self.calls.append((argv, cwd, timeout_seconds))
        return self.responses.get(argv, CommandResult(127, "", "unexpected command"))


def _profile(tmp_workspace):
    return validate_verification_profile(
        {
            "schema_version": 1,
            "project_id": "test-project-001",
            "workspace_root": str(tmp_workspace),
            "prerequisites": ["python3"],
            "max_remediation_cycles": 1,
            "checks": [
                {
                    "check_id": "unit",
                    "argv": ["python3", "-m", "pytest", "-q"],
                    "cwd": ".",
                    "timeout_seconds": 120,
                    "required_evidence": ["exit_code", "duration_seconds", "stdout_excerpt"],
                }
            ],
        },
        registered_project_id="test-project-001",
        registered_workspace_root=str(tmp_workspace),
    )


def _state(tmp_workspace):
    lease = build_lease_preset(
        "autonomous-development",
        project_id="test-project-001",
        workspace_root=str(tmp_workspace),
        now=datetime.now(timezone.utc),
    )
    return {
        "state": "LEASE_READY",
        "registration": {
            "project_id": "test-project-001",
            "workspace_root": str(tmp_workspace),
        },
        "lease": lease_to_dict(lease),
    }


def _loop(tmp_hermes_home, tmp_workspace, state, profile):
    payload = {
        "loop_id": "loop-1",
        "project_id": "test-project-001",
        "brief_id": "brief-1",
        "lease_id": state["lease"]["lease_id"],
        "workspace_root": str(tmp_workspace),
        "status": "QUEUED",
        "board_slug": "test-project-001",
        "development_task_id": "task-dev",
        "verifier_task_id": "task-verify",
        "starting_revision": "abc123",
        "verification_profile_digest": verification_profile_digest(profile),
        "remediation_count": 0,
    }
    path = (
        tmp_hermes_home
        / "state"
        / "autopilot"
        / "projects"
        / "test-project-001"
        / "loops"
        / "loop_loop-1.json"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")
    return payload


def _show(task_id, status, *, runs=None):
    return json.dumps(
        {
            "task": {"id": task_id, "status": status},
            "latest_summary": "done",
            "parents": [],
            "children": [],
            "comments": [],
            "events": [],
            "runs": runs or [],
        }
    )


def test_sync_accepts_only_valid_completed_verifier_evidence(
    tmp_hermes_home,
    tmp_workspace,
):
    profile = _profile(tmp_workspace)
    state = _state(tmp_workspace)
    loop = _loop(tmp_hermes_home, tmp_workspace, state, profile)
    metadata = {
        "autopilot_contract_version": 1,
        "role": "verifier",
        "brief_id": "brief-1",
        "verification_status": "passed",
        "review_status": "approved",
        "starting_revision": "abc123",
        "changed_files": ["lib/app.py"],
        "checks": [
            {
                "check_id": "unit",
                "argv": ["python3", "-m", "pytest", "-q"],
                "cwd": ".",
                "exit_code": 0,
                "duration_seconds": 1.2,
                "stdout_excerpt": "1 passed",
                "stderr_excerpt": "",
            }
        ],
        "findings": [],
        "residual_risk": "none",
    }
    runtime = FakeRuntime(
        {
            (
                "hermes",
                "kanban",
                "--board",
                "test-project-001",
                "show",
                "task-dev",
                "--json",
            ): CommandResult(0, _show("task-dev", "done"), ""),
            (
                "hermes",
                "kanban",
                "--board",
                "test-project-001",
                "show",
                "task-verify",
                "--json",
            ): CommandResult(
                0,
                _show(
                    "task-verify",
                    "done",
                    runs=[
                        {
                            "id": 9,
                            "status": "done",
                            "outcome": "completed",
                            "summary": "verified",
                            "error": None,
                            "metadata": metadata,
                            "started_at": 1,
                            "ended_at": 2,
                        }
                    ],
                ),
                "",
            ),
        }
    )
    supervisor = AutonomousLoopSupervisor(hermes_home=tmp_hermes_home)

    result = LoopReconciler(runtime, supervisor).sync(
        loop=loop,
        state=state,
        profile=profile,
    )

    assert result.status == "AWAITING_HUMAN_ACCEPTANCE"
    assert result.evidence_path
    evidence = json.loads(open(result.evidence_path, encoding="utf-8").read())
    assert evidence["accepted"] is True
    persisted = supervisor.list_loops("test-project-001")[0]
    assert persisted["status"] == "AWAITING_HUMAN_ACCEPTANCE"
    assert persisted["result_task_id"] == "task-verify"
    assert persisted["result_run_id"] == 9

    acceptance = supervisor.accept_loop(
        project_id="test-project-001",
        loop_id="loop-1",
        accepted_by="human-command",
    )
    assert acceptance
    accepted = supervisor.list_loops("test-project-001")[0]
    assert accepted["status"] == "ACCEPTED"
    assert accepted["acceptance_artifact_path"]


def test_failed_valid_evidence_queues_one_bounded_remediation_cycle(
    tmp_hermes_home,
    tmp_workspace,
):
    profile = _profile(tmp_workspace)
    state = _state(tmp_workspace)
    loop = _loop(tmp_hermes_home, tmp_workspace, state, profile)
    (tmp_workspace / ".worktrees" / "task-dev").mkdir(parents=True)
    failed_metadata = {
        "autopilot_contract_version": 1,
        "role": "verifier",
        "brief_id": "brief-1",
        "verification_status": "failed",
        "review_status": "rejected",
        "starting_revision": "abc123",
        "changed_files": ["lib/app.py"],
        "checks": [
            {
                "check_id": "unit",
                "argv": ["python3", "-m", "pytest", "-q"],
                "cwd": ".",
                "exit_code": 1,
                "duration_seconds": 1.2,
                "stdout_excerpt": "1 failed",
                "stderr_excerpt": "assertion failed",
            }
        ],
        "findings": ["Fix the failing unit assertion without expanding scope."],
        "residual_risk": "unit failure",
    }

    class RemediationRuntime(FakeRuntime):
        def run(self, argv, *, cwd, timeout_seconds):
            self.calls.append((argv, cwd, timeout_seconds))
            if argv[-1:] == ("--json",) and "show" in argv:
                task_id = argv[-2]
                if task_id == "task-dev":
                    return CommandResult(0, _show(task_id, "done"), "")
                if task_id == "task-verify":
                    return CommandResult(
                        0,
                        _show(
                            task_id,
                            "done",
                            runs=[
                                {
                                    "id": 10,
                                    "status": "done",
                                    "outcome": "completed",
                                    "metadata": failed_metadata,
                                }
                            ],
                        ),
                        "",
                    )
            if argv[:5] == (
                "hermes",
                "kanban",
                "--board",
                "test-project-001",
                "create",
            ):
                if "Autopilot remediation:" in argv[5]:
                    return CommandResult(0, '{"id":"task-remediate"}', "")
                if "Autopilot re-verification:" in argv[5]:
                    return CommandResult(0, '{"id":"task-reverify"}', "")
            if argv[:5] == (
                "hermes",
                "kanban",
                "--board",
                "test-project-001",
                "promote",
            ):
                bound = supervisor.list_loops("test-project-001")[0]
                assert bound["status"] == "REMEDIATING"
                assert bound["current_remediation_task_id"] == "task-remediate"
                assert bound["verifier_task_id"] == "task-reverify"
                assert bound["remediation_count"] == 1
                supervisor.mark_status(
                    project_id="test-project-001",
                    loop_id=bound["loop_id"],
                    status="RUNNING",
                )
                return CommandResult(0, "Promoted task-remediate", "")
            return CommandResult(127, "", f"unexpected command: {argv}")

    runtime = RemediationRuntime({})
    supervisor = AutonomousLoopSupervisor(hermes_home=tmp_hermes_home)
    result = LoopReconciler(runtime, supervisor).sync(
        loop=loop,
        state=state,
        profile=profile,
    )

    assert result.status == "REMEDIATING"
    persisted = supervisor.list_loops("test-project-001")[0]
    assert persisted["status"] == "RUNNING"
    assert persisted["remediation_count"] == 1
    assert persisted["current_remediation_task_id"] == "task-remediate"
    assert persisted["verifier_task_id"] == "task-reverify"


def test_cancel_reclaims_running_worker_then_blocks_unfinished_pipeline(
    tmp_hermes_home,
    tmp_workspace,
):
    profile = _profile(tmp_workspace)
    state = _state(tmp_workspace)
    loop = _loop(tmp_hermes_home, tmp_workspace, state, profile)

    class CancelRuntime(FakeRuntime):
        def run(self, argv, *, cwd, timeout_seconds):
            self.calls.append((argv, cwd, timeout_seconds))
            if "show" in argv:
                task_id = argv[-2]
                status = "running" if task_id == "task-dev" else "todo"
                return CommandResult(0, _show(task_id, status), "")
            if "reclaim" in argv:
                return CommandResult(0, "Reclaimed task-dev", "")
            if "block" in argv:
                return CommandResult(0, "Blocked", "")
            return CommandResult(127, "", "unexpected")

    runtime = CancelRuntime({})
    supervisor = AutonomousLoopSupervisor(hermes_home=tmp_hermes_home)
    result = LoopReconciler(runtime, supervisor).cancel(
        loop=loop,
        reason="Autonomy lease revoked",
    )

    assert result.status == "CANCELED"
    commands = [call[0] for call in runtime.calls]
    assert any("reclaim" in command and "task-dev" in command for command in commands)
    assert any("block" in command and "task-dev" in command for command in commands)
    assert any("block" in command and "task-verify" in command for command in commands)
    persisted = supervisor.list_loops("test-project-001")[0]
    assert persisted["status"] == "CANCELED"


def test_recovery_reconciles_active_loop_idempotently_and_records_telemetry(
    tmp_hermes_home,
    tmp_workspace,
):
    profile = _profile(tmp_workspace)
    state = _state(tmp_workspace)
    loop = _loop(tmp_hermes_home, tmp_workspace, state, profile)
    save_project_state("test-project-001", state)
    save_verification_profile(profile)
    runtime = FakeRuntime({
        (
            "hermes", "kanban", "--board", "test-project-001",
            "show", "task-dev", "--json",
        ): CommandResult(0, _show("task-dev", "ready"), ""),
    })

    first = recover_active_loops(runtime)
    second = recover_active_loops(runtime)

    assert first == {"examined": 1, "reconciled": 1, "errors": 0}
    assert second == {"examined": 1, "reconciled": 1, "errors": 0}
    stored = AutonomousLoopSupervisor().list_loops("test-project-001")[0]
    assert stored is not None
    assert stored["status"] == "QUEUED"
    assert stored["recovery_attempts"] == 2
    assert stored["last_recovery_at"]
    assert stored["last_recovery_error"] == ""


def test_recovery_records_bounded_failure_and_moves_unreconciled_loop_to_human(
    tmp_hermes_home,
    tmp_workspace,
):
    profile = _profile(tmp_workspace)
    state = _state(tmp_workspace)
    loop = _loop(tmp_hermes_home, tmp_workspace, state, profile)
    save_project_state("test-project-001", state)
    save_verification_profile(profile)
    runtime = FakeRuntime({})

    summary = recover_active_loops(runtime)

    assert summary == {"examined": 1, "reconciled": 0, "errors": 1}
    stored = AutonomousLoopSupervisor().list_loops("test-project-001")[0]
    assert stored is not None
    assert stored["status"] == "NEEDS_HUMAN"
    assert stored["recovery_attempts"] == 1
    assert "unexpected command" in stored["last_recovery_error"]
