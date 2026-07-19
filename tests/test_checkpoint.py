"""Tests for checkpoint and separate commit authorization."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import zipfile

from autopilot.adapters.autonomous_loop import AutonomousLoopSupervisor
from autopilot.adapters.development_executor import CommandResult
from autopilot.checkpoint import CheckpointManager


class FakeRuntime:
    def __init__(self, workspace: Path, worktree: Path, source_status: str, worktree_status: str):
        self.workspace = workspace
        self.worktree = worktree
        self.source_status = source_status
        self.worktree_status = worktree_status
        self.calls = []

    def run(self, argv, *, cwd, timeout_seconds):
        self.calls.append((argv, cwd, timeout_seconds))
        if argv == ("git", "status", "--porcelain=v1", "-z", "--untracked-files=all"):
            output = self.source_status if cwd == str(self.workspace) else self.worktree_status
            return CommandResult(0, output, "")
        if argv == ("git", "add", "--all"):
            return CommandResult(0, "", "")
        if argv[:3] == ("git", "commit", "-m"):
            return CommandResult(0, "created commit", "")
        if argv == ("git", "rev-parse", "HEAD"):
            return CommandResult(0, "abcdef1234567890\n", "")
        return CommandResult(127, "", f"unexpected: {argv}")


def _accepted_loop(tmp_hermes_home, tmp_workspace):
    project_id = "test-project-001"
    loop_id = "loop-1"
    worktree = tmp_workspace / ".worktrees" / "task-dev"
    (worktree / "lib").mkdir(parents=True)
    (worktree / "lib" / "app.py").write_text("value = 2\n", encoding="utf-8")
    (tmp_workspace / "local.txt").write_text("uncommitted source edit\n", encoding="utf-8")
    source_status = " M local.txt\x00"
    worktree_status = " M lib/app.py\x00"

    project_dir = (
        tmp_hermes_home / "state" / "autopilot" / "projects" / project_id
    )
    result_path = project_dir / "results" / "result_loop-1_task-verify_9.json"
    result_path.parent.mkdir(parents=True)
    result_path.write_text(
        json.dumps(
            {
                "accepted": True,
                "changed_files": ["lib/app.py"],
                "provenance": {
                    "project_id": project_id,
                    "loop_id": loop_id,
                    "task_id": "task-verify",
                    "run_id": 9,
                },
            }
        ),
        encoding="utf-8",
    )
    loop = {
        "loop_id": loop_id,
        "project_id": project_id,
        "brief_id": "brief-1",
        "workspace_root": str(tmp_workspace),
        "status": "ACCEPTED",
        "development_task_id": "task-dev",
        "verifier_task_id": "task-verify",
        "starting_revision": "abc123",
        "source_status_digest": hashlib.sha256(source_status.encode()).hexdigest(),
        "result_artifact_path": str(result_path),
        "result_task_id": "task-verify",
        "result_run_id": 9,
        "acceptance_artifact_path": str(project_dir / "results" / "acceptance_loop-1.json"),
    }
    loops = project_dir / "loops"
    loops.mkdir(parents=True)
    (loops / "loop_loop-1.json").write_text(json.dumps(loop), encoding="utf-8")
    return loop, worktree, source_status, worktree_status


def test_checkpoint_authorization_and_commit_are_separate_and_worktree_only(
    tmp_hermes_home,
    tmp_workspace,
):
    loop, worktree, source_status, worktree_status = _accepted_loop(
        tmp_hermes_home, tmp_workspace
    )
    runtime = FakeRuntime(tmp_workspace, worktree, source_status, worktree_status)
    supervisor = AutonomousLoopSupervisor(hermes_home=tmp_hermes_home)
    manager = CheckpointManager(
        runtime,
        hermes_home=tmp_hermes_home,
        supervisor=supervisor,
    )

    checkpoint = manager.create(loop=loop)
    assert checkpoint.success is True
    with zipfile.ZipFile(checkpoint.artifact_path) as archive:
        manifest = json.loads(archive.read("manifest.json"))
        assert manifest["files"][0]["path"] == "lib/app.py"
        assert archive.read("files/lib/app.py") == b"value = 2\n"

    persisted = supervisor.list_loops("test-project-001")[0]
    authorization = manager.authorize_commit(loop=persisted)
    assert authorization.success is True
    assert persisted.get("commit_authorization_path") in {None, ""}

    authorized = supervisor.list_loops("test-project-001")[0]
    committed = manager.commit(loop=authorized)
    assert committed.success is True
    assert committed.revision == "abcdef1234567890"
    assert (tmp_workspace / "local.txt").read_text(encoding="utf-8") == "uncommitted source edit\n"
    commit_calls = [call for call in runtime.calls if call[0][:2] == ("git", "commit")]
    assert len(commit_calls) == 1
    assert commit_calls[0][1] == str(worktree)
    assert not any("push" in call[0] for call in runtime.calls)

    auth_payload = json.loads(Path(authorization.artifact_path).read_text(encoding="utf-8"))
    assert auth_payload["used"] is True


def test_commit_blocks_byte_changes_even_when_git_status_codes_are_unchanged(
    tmp_hermes_home,
    tmp_workspace,
):
    loop, worktree, source_status, worktree_status = _accepted_loop(
        tmp_hermes_home, tmp_workspace
    )
    runtime = FakeRuntime(tmp_workspace, worktree, source_status, worktree_status)
    supervisor = AutonomousLoopSupervisor(hermes_home=tmp_hermes_home)
    manager = CheckpointManager(
        runtime,
        hermes_home=tmp_hermes_home,
        supervisor=supervisor,
    )
    assert manager.create(loop=loop).success
    persisted = supervisor.list_loops("test-project-001")[0]
    assert manager.authorize_commit(loop=persisted).success
    authorized = supervisor.list_loops("test-project-001")[0]

    (worktree / "lib" / "app.py").write_text("value = 3\n", encoding="utf-8")
    blocked = manager.commit(loop=authorized)

    assert blocked.success is False
    assert any("contents changed" in blocker for blocker in blocked.blockers)
    assert not any(call[0][:2] == ("git", "commit") for call in runtime.calls)


def test_authorization_rejects_tampered_checkpoint_payload(
    tmp_hermes_home,
    tmp_workspace,
):
    loop, worktree, source_status, worktree_status = _accepted_loop(
        tmp_hermes_home, tmp_workspace
    )
    runtime = FakeRuntime(tmp_workspace, worktree, source_status, worktree_status)
    supervisor = AutonomousLoopSupervisor(hermes_home=tmp_hermes_home)
    manager = CheckpointManager(
        runtime,
        hermes_home=tmp_hermes_home,
        supervisor=supervisor,
    )
    checkpoint = manager.create(loop=loop)
    assert checkpoint.success
    artifact = Path(checkpoint.artifact_path)
    with zipfile.ZipFile(artifact, "r") as archive:
        manifest_bytes = archive.read("manifest.json")
    with zipfile.ZipFile(artifact, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("manifest.json", manifest_bytes)
        archive.writestr("files/lib/app.py", b"tampered\n")

    persisted = supervisor.list_loops("test-project-001")[0]
    authorization = manager.authorize_commit(loop=persisted)

    assert authorization.success is False
    assert any("checkpoint payload" in blocker.lower() for blocker in authorization.blockers)
