"""Tests for per-tool Autopilot lease and workspace enforcement."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

from autopilot.adapters.autonomous_loop import AutonomousLoopSupervisor
from autopilot.lease import build_lease_preset, lease_to_dict
from autopilot.policy_hook import pre_tool_call_guard
from autopilot.storage import save_project_state
from autopilot.verification import save_verification_profile, validate_verification_profile


def _active_task(tmp_hermes_home: Path, tmp_workspace: Path, *, expired: bool = False):
    now = datetime.now(timezone.utc)
    lease = build_lease_preset(
        "autonomous-development",
        project_id="test-project-001",
        workspace_root=str(tmp_workspace),
        now=now - (timedelta(hours=3) if expired else timedelta(minutes=1)),
    )
    if expired:
        lease = replace(lease, expiry=(now - timedelta(minutes=1)).isoformat())
    state = {
        "state": "LEASE_READY",
        "registration": {
            "project_id": "test-project-001",
            "display_name": "Test Project",
            "workspace_root": str(tmp_workspace),
            "development_session_id": "dev-session",
            "discussion_session_id": "discussion-session",
        },
        "lease": lease_to_dict(lease),
    }
    save_project_state("test-project-001", state)
    loops = tmp_hermes_home / "state" / "autopilot" / "projects" / "test-project-001" / "loops"
    loops.mkdir(parents=True, exist_ok=True)
    payload = {
        "loop_id": "loop-test",
        "project_id": "test-project-001",
        "lease_id": lease.lease_id,
        "status": "QUEUED",
        "brief_id": "brief-1",
        "development_task_id": "task-dev",
        "verifier_task_id": "task-verify",
        "starting_revision": "ref: refs/heads/Development",
        "workspace_root": str(tmp_workspace),
    }
    (loops / "loop_loop-test.json").write_text(json.dumps(payload), encoding="utf-8")
    return state


def _configure_python_check(tmp_workspace: Path) -> None:
    profile = validate_verification_profile(
        {
            "schema_version": 1,
            "project_id": "test-project-001",
            "workspace_root": str(tmp_workspace),
            "prerequisites": ["python3"],
            "checks": [
                {
                    "check_id": "pytest",
                    "argv": ["python3", "-m", "pytest", "-q"],
                    "cwd": ".",
                    "timeout_seconds": 60,
                    "required_evidence": ["exit_code", "duration_seconds"],
                }
            ],
            "max_remediation_cycles": 1,
        },
        registered_project_id="test-project-001",
        registered_workspace_root=str(tmp_workspace),
    )
    save_verification_profile(profile)


def test_development_exact_command_is_allowed_but_verifier_cannot_use_it(
    tmp_hermes_home,
    tmp_workspace,
    monkeypatch,
):
    _active_task(tmp_hermes_home, tmp_workspace)
    profile = validate_verification_profile(
        {
            "schema_version": 1,
            "project_id": "test-project-001",
            "workspace_root": str(tmp_workspace),
            "prerequisites": ["python3"],
            "checks": [{
                "check_id": "pytest",
                "argv": ["python3", "-m", "pytest", "-q"],
                "cwd": ".",
                "timeout_seconds": 60,
                "required_evidence": ["exit_code"],
            }],
            "development_commands": [{
                "command_id": "format",
                "argv": ["ruff", "format", "."],
                "cwd": ".",
                "timeout_seconds": 60,
            }],
        },
        registered_project_id="test-project-001",
        registered_workspace_root=str(tmp_workspace),
    )
    save_verification_profile(profile)
    worktree = tmp_workspace / ".worktrees" / "task-dev"
    worktree.mkdir(parents=True)
    monkeypatch.chdir(worktree)

    allowed = pre_tool_call_guard(
        tool_name="terminal",
        args={"command": "ruff format .", "workdir": str(worktree)},
        task_id="task-dev",
    )
    denied = pre_tool_call_guard(
        tool_name="terminal",
        args={"command": "ruff format .", "workdir": str(worktree)},
        task_id="task-verify",
    )

    assert allowed is None
    assert denied is not None
    assert "worker role" in denied["message"].lower()


def test_unbound_autopilot_tenant_worker_fails_closed(tmp_hermes_home, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_TASK", "task-missing")
    monkeypatch.setenv("HERMES_TENANT", "autopilot:test-project-001")

    decision = pre_tool_call_guard(tool_name="read_file", args={"path": "README.md"})

    assert decision is not None
    assert "no valid durable loop binding" in decision["message"].lower()


def test_needs_human_loop_allows_work_and_kanban_block(
    tmp_hermes_home,
    tmp_workspace,
    monkeypatch,
):
    _active_task(tmp_hermes_home, tmp_workspace)
    loop_path = (
        tmp_hermes_home / "state" / "autopilot" / "projects" /
        "test-project-001" / "loops" / "loop_loop-test.json"
    )
    loop = json.loads(loop_path.read_text(encoding="utf-8"))
    loop["status"] = "NEEDS_HUMAN"
    loop_path.write_text(json.dumps(loop), encoding="utf-8")
    worktree = tmp_workspace / ".worktrees" / "task-dev"
    worktree.mkdir(parents=True)
    monkeypatch.chdir(worktree)

    denied = pre_tool_call_guard(
        tool_name="write_file",
        args={"path": "change.py", "content": "x"},
        task_id="task-dev",
    )
    report = pre_tool_call_guard(
        tool_name="kanban_block",
        args={"reason": "waiting for answer"},
        task_id="task-dev",
    )

    assert denied is None, "NEEDS_HUMAN is now an active status — work tools should pass"
    assert report is None


def test_expired_task_is_blocked_but_may_report_kanban_block(
    tmp_hermes_home,
    tmp_workspace,
    monkeypatch,
):
    _active_task(tmp_hermes_home, tmp_workspace, expired=True)
    worktree = tmp_workspace / ".worktrees" / "task-dev"
    worktree.mkdir(parents=True)
    monkeypatch.chdir(worktree)

    decision = pre_tool_call_guard(
        tool_name="read_file",
        args={"path": "README.md"},
        task_id="task-dev",
    )
    report = pre_tool_call_guard(
        tool_name="kanban_block",
        args={"reason": "lease expired"},
        task_id="task-dev",
    )

    assert decision == {
        "action": "block",
        "message": "Project Autopilot blocked this tool call: autonomy lease expired.",
    }
    assert report is None


def test_active_task_blocks_git_commit(tmp_hermes_home, tmp_workspace, monkeypatch):
    _active_task(tmp_hermes_home, tmp_workspace)
    worktree = tmp_workspace / ".worktrees" / "task-dev"
    worktree.mkdir(parents=True)
    monkeypatch.chdir(worktree)

    decision = pre_tool_call_guard(
        tool_name="terminal",
        args={"command": "git commit -am 'not authorized'", "workdir": str(worktree)},
        task_id="task-dev",
    )

    assert decision is not None
    assert decision["action"] == "block"
    assert "git commit" in decision["message"].lower()


def test_merge_phase_allows_only_exact_promotion_git_commands(
    tmp_hermes_home,
    tmp_workspace,
    monkeypatch,
):
    _active_task(tmp_hermes_home, tmp_workspace)
    loop_path = (
        tmp_hermes_home / "state" / "autopilot" / "projects" /
        "test-project-001" / "loops" / "loop_loop-test.json"
    )
    loop = json.loads(loop_path.read_text(encoding="utf-8"))
    loop["status"] = "MERGING"
    loop_path.write_text(json.dumps(loop), encoding="utf-8")
    worktree = tmp_workspace / ".worktrees" / "task-dev"
    worktree.mkdir(parents=True)
    monkeypatch.chdir(worktree)

    commands = (
        "git add --all",
        "git commit -m 'autopilot(task-dev): brief-1' "
        "-m 'Automated commit by Autopilot post-verification.'",
        "git push origin HEAD:refs/heads/Development",
    )
    decisions = [
        pre_tool_call_guard(
            tool_name="terminal",
            args={"command": command, "workdir": str(worktree)},
            task_id="task-dev",
        )
        for command in commands
    ]
    wrong_target = pre_tool_call_guard(
        tool_name="terminal",
        args={
            "command": "git push origin HEAD:refs/heads/main",
            "workdir": str(worktree),
        },
        task_id="task-dev",
    )
    verifier_push = pre_tool_call_guard(
        tool_name="terminal",
        args={"command": commands[-1], "workdir": str(worktree)},
        task_id="task-verify",
    )

    assert decisions == [None, None, None]
    assert wrong_target is not None
    assert "exact post-verification" in wrong_target["message"].lower()
    assert verifier_push is not None
    assert "development during the merge phase" in verifier_push["message"].lower()


def test_verifier_cannot_write_files(tmp_hermes_home, tmp_workspace, monkeypatch):
    _active_task(tmp_hermes_home, tmp_workspace)
    worktree = tmp_workspace / ".worktrees" / "task-dev"
    worktree.mkdir(parents=True)
    monkeypatch.chdir(worktree)

    decision = pre_tool_call_guard(
        tool_name="write_file",
        args={"path": "lib/app.py", "content": "changed"},
        task_id="task-verify",
    )

    assert decision is not None
    assert "verifier is read-only" in decision["message"].lower()


def test_active_task_cannot_address_path_outside_worktree(
    tmp_hermes_home,
    tmp_workspace,
    monkeypatch,
):
    _active_task(tmp_hermes_home, tmp_workspace)
    worktree = tmp_workspace / ".worktrees" / "task-dev"
    worktree.mkdir(parents=True)
    monkeypatch.chdir(worktree)

    decision = pre_tool_call_guard(
        tool_name="read_file",
        args={"path": str(tmp_workspace / "secret.txt")},
        task_id="task-dev",
    )

    assert decision is not None
    assert "outside the isolated worktree" in decision["message"].lower()


def test_verification_executable_cannot_run_unapproved_arguments(
    tmp_hermes_home,
    tmp_workspace,
    monkeypatch,
):
    _active_task(tmp_hermes_home, tmp_workspace)
    _configure_python_check(tmp_workspace)
    worktree = tmp_workspace / ".worktrees" / "task-dev"
    worktree.mkdir(parents=True)
    monkeypatch.chdir(worktree)

    decision = pre_tool_call_guard(
        tool_name="terminal",
        args={
            "command": "python3 -c 'open(\"../../escaped\", \"w\").write(\"x\")'",
            "workdir": str(worktree),
        },
        task_id="task-dev",
    )

    assert decision is not None
    assert "exact configured verification command" in decision["message"].lower()


def test_exact_verification_command_is_allowed(
    tmp_hermes_home,
    tmp_workspace,
    monkeypatch,
):
    _active_task(tmp_hermes_home, tmp_workspace)
    _configure_python_check(tmp_workspace)
    worktree = tmp_workspace / ".worktrees" / "task-dev"
    worktree.mkdir(parents=True)
    monkeypatch.chdir(worktree)

    decision = pre_tool_call_guard(
        tool_name="terminal",
        args={"command": "python3 -m pytest -q", "workdir": str(worktree)},
        task_id="task-dev",
    )

    assert decision is None


def test_exact_verification_command_requires_configured_workdir(
    tmp_hermes_home,
    tmp_workspace,
    monkeypatch,
):
    _active_task(tmp_hermes_home, tmp_workspace)
    _configure_python_check(tmp_workspace)
    worktree = tmp_workspace / ".worktrees" / "task-dev"
    wrong_workdir = worktree / "nested"
    wrong_workdir.mkdir(parents=True)
    monkeypatch.chdir(worktree)

    decision = pre_tool_call_guard(
        tool_name="terminal",
        args={"command": "python3 -m pytest -q", "workdir": str(wrong_workdir)},
        task_id="task-dev",
    )

    assert decision is not None
    assert "configured verification working directory" in decision["message"].lower()


def test_git_read_cannot_select_alternate_repository(
    tmp_hermes_home,
    tmp_workspace,
    monkeypatch,
):
    _active_task(tmp_hermes_home, tmp_workspace)
    worktree = tmp_workspace / ".worktrees" / "task-dev"
    worktree.mkdir(parents=True)
    monkeypatch.chdir(worktree)

    decision = pre_tool_call_guard(
        tool_name="terminal",
        args={
            "command": "git --git-dir=/tmp/other/.git status",
            "workdir": str(worktree),
        },
        task_id="task-dev",
    )

    assert decision is not None
    assert "repository or worktree override" in decision["message"].lower()


def test_git_read_cannot_write_output_file(
    tmp_hermes_home,
    tmp_workspace,
    monkeypatch,
):
    _active_task(tmp_hermes_home, tmp_workspace)
    worktree = tmp_workspace / ".worktrees" / "task-dev"
    worktree.mkdir(parents=True)
    monkeypatch.chdir(worktree)

    decision = pre_tool_call_guard(
        tool_name="terminal",
        args={
            "command": "git diff --output=/tmp/escaped.diff",
            "workdir": str(worktree),
        },
        task_id="task-dev",
    )

    assert decision is not None
    assert "output or external helper" in decision["message"].lower()


def test_expired_task_cannot_block_a_different_task(
    tmp_hermes_home,
    tmp_workspace,
    monkeypatch,
):
    _active_task(tmp_hermes_home, tmp_workspace, expired=True)
    worktree = tmp_workspace / ".worktrees" / "task-dev"
    worktree.mkdir(parents=True)
    monkeypatch.chdir(worktree)

    decision = pre_tool_call_guard(
        tool_name="kanban_block",
        args={"task_id": "other-task", "reason": "lease expired"},
        task_id="task-dev",
    )

    assert decision is not None
    assert "bound autopilot task" in decision["message"].lower()


def test_task_cannot_address_a_different_board(
    tmp_hermes_home,
    tmp_workspace,
    monkeypatch,
):
    _active_task(tmp_hermes_home, tmp_workspace)
    worktree = tmp_workspace / ".worktrees" / "task-dev"
    worktree.mkdir(parents=True)
    monkeypatch.chdir(worktree)

    decision = pre_tool_call_guard(
        tool_name="kanban_show",
        args={"board": "other-board"},
        task_id="task-dev",
    )

    assert decision is not None
    assert "bound autopilot board" in decision["message"].lower()


def test_task_cannot_upload_completion_artifacts(
    tmp_hermes_home,
    tmp_workspace,
    monkeypatch,
):
    _active_task(tmp_hermes_home, tmp_workspace)
    worktree = tmp_workspace / ".worktrees" / "task-dev"
    worktree.mkdir(parents=True)
    monkeypatch.chdir(worktree)

    decision = pre_tool_call_guard(
        tool_name="kanban_complete",
        args={"summary": "done", "artifacts": ["/etc/hosts"]},
        task_id="task-dev",
    )

    assert decision is not None
    assert "artifact uploads" in decision["message"].lower()


def test_completion_blocks_raw_sensitive_metadata(
    tmp_hermes_home,
    tmp_workspace,
    monkeypatch,
):
    _active_task(tmp_hermes_home, tmp_workspace)
    worktree = tmp_workspace / ".worktrees" / "task-dev"
    worktree.mkdir(parents=True)
    monkeypatch.chdir(worktree)

    decision = pre_tool_call_guard(
        tool_name="kanban_complete",
        task_id="task-dev",
        args={
            "metadata": {
                "autopilot_contract_version": 1,
                "role": "development",
                "residual_risk": "Bear" + "er sensitive-value",
            }
        },
    )

    assert decision is not None
    assert "sensitive" in decision["message"].lower()


def test_task_cannot_use_vision_to_read_local_files(
    tmp_hermes_home,
    tmp_workspace,
    monkeypatch,
):
    _active_task(tmp_hermes_home, tmp_workspace)
    worktree = tmp_workspace / ".worktrees" / "task-dev"
    worktree.mkdir(parents=True)
    monkeypatch.chdir(worktree)

    decision = pre_tool_call_guard(
        tool_name="vision_analyze",
        args={"image_url": "/etc/hosts", "question": "read it"},
        task_id="task-dev",
    )

    assert decision is not None
    assert "outside the autopilot worker allowlist" in decision["message"].lower()
