"""Tests for Phase 3 controlled development runner foundation."""

from __future__ import annotations

import json
from pathlib import Path

from autopilot.adapters.execution_bridge import ExecutionBridge
from autopilot.adapters.runner import DevelopmentRunner
from autopilot.commands import handle_autopilot_command
from autopilot.storage import load_state


def _register(sample_registration: dict) -> None:
    result = str(handle_autopilot_command(f"register {json.dumps(sample_registration)}"))
    assert "Registered" in result


def _generate_brief(sample_registration: dict) -> str:
    _register(sample_registration)
    assert "Lease approved" in str(handle_autopilot_command("lease approve phase2-readonly"))
    result = str(handle_autopilot_command("brief"))
    assert "Brief ID:" in result
    for line in result.splitlines():
        if line.startswith("Brief ID:"):
            return line.split(":", 1)[1].strip()
    raise AssertionError("brief id not found")


class TestPhase3LeasePreset:
    def test_phase3_development_request_is_explicit_about_write_scope(self, tmp_hermes_home, sample_registration):
        _register(sample_registration)

        result = str(handle_autopilot_command("lease request phase3-development"))

        assert "Phase 3 controlled development" in result
        assert "[x] workspace.write" in result
        assert "[x] git.commit" in result
        assert "[ ] git.push" in result
        assert "[ ] deployment" in result
        assert "No lease has been created" in result
        assert load_state().get("lease") is None

    def test_phase3_development_approval_creates_fixed_write_lease(self, tmp_hermes_home, sample_registration):
        _register(sample_registration)

        result = str(handle_autopilot_command("lease approve phase3-development"))
        lease = load_state()["lease"]

        assert "Phase 3 controlled development" in result
        assert lease["project_id"] == sample_registration["project_id"]
        assert lease["workspace_root"] == sample_registration["workspace_root"]
        assert lease["granted_capabilities"] == [
            "workspace.read",
            "git.read",
            "workspace.write",
            "git.commit",
            "next-phase",
        ]
        assert lease["git_policy"] == "commit"
        assert lease["external_write_policy"] == "deny"

    def test_phase3_approval_rejects_capability_overrides(self, tmp_hermes_home, sample_registration):
        _register(sample_registration)

        result = str(handle_autopilot_command("lease approve phase3-development git.push"))

        assert "fixed preset" in result.lower()
        assert load_state().get("lease") is None


class TestBriefApprovalCommands:
    def test_approve_requires_existing_brief(self, tmp_hermes_home, sample_registration):
        _register(sample_registration)
        handle_autopilot_command("lease approve phase3-development")

        result = str(handle_autopilot_command("approve missing-brief"))

        assert "not found" in result.lower()

    def test_approve_and_revoke_flip_authorization(self, tmp_hermes_home, sample_registration):
        brief_id = _generate_brief(sample_registration)
        handle_autopilot_command("lease approve phase3-development")

        approved = str(handle_autopilot_command(f"approve {brief_id}"))
        bridge = ExecutionBridge(hermes_home=tmp_hermes_home)
        brief = bridge.read_brief(sample_registration["project_id"], brief_id)

        assert "approved" in approved.lower()
        assert brief is not None
        assert brief.execution_authorized is True

        revoked = str(handle_autopilot_command(f"revoke {brief_id}"))
        brief = bridge.read_brief(sample_registration["project_id"], brief_id)

        assert "revoked" in revoked.lower()
        assert brief is not None
        assert brief.execution_authorized is False


class TestDevelopmentRunner:
    def test_runner_refuses_unapproved_brief(self, tmp_hermes_home, sample_registration):
        brief_id = _generate_brief(sample_registration)
        handle_autopilot_command("lease approve phase3-development")
        bridge = ExecutionBridge(hermes_home=tmp_hermes_home)
        brief = bridge.read_brief(sample_registration["project_id"], brief_id)

        result = DevelopmentRunner(hermes_home=tmp_hermes_home).prepare_run(brief, load_state())

        assert result.success is False
        assert any("approved" in b.lower() for b in result.blockers)

    def test_run_command_creates_controlled_execution_package(self, tmp_hermes_home, sample_registration):
        brief_id = _generate_brief(sample_registration)
        handle_autopilot_command("lease approve phase3-development")
        handle_autopilot_command(f"approve {brief_id}")

        result = str(handle_autopilot_command(f"run {brief_id}"))

        assert "Phase 3 Development Run Prepared" in result
        assert "Execution mode: controlled-package" in result
        assert "Autonomous file editing has NOT started" in result
        assert "Run artifact:" in result

    def test_runner_artifacts_are_project_scoped(self, tmp_hermes_home, sample_registration):
        brief_id = _generate_brief(sample_registration)
        handle_autopilot_command("lease approve phase3-development")
        handle_autopilot_command(f"approve {brief_id}")
        handle_autopilot_command(f"run {brief_id}")

        root = Path(tmp_hermes_home) / "state" / "autopilot" / "projects" / sample_registration["project_id"]
        run_files = list((root / "runs").glob("run_*.json"))

        assert len(run_files) == 1
        payload = json.loads(run_files[0].read_text())
        assert payload["project_id"] == sample_registration["project_id"]
        assert payload["brief_id"] == brief_id
        assert payload["status"] == "READY_FOR_DEVELOPMENT_SESSION"
        assert payload["execution_authorized"] is True
