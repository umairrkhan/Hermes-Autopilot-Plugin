"""Tests for the human-friendly, fail-closed lease preset flow."""

from __future__ import annotations

import json
from datetime import datetime, timezone

from autopilot.commands import handle_autopilot_command
from autopilot.storage import load_state


def _register(sample_registration: dict) -> str:
    return str(handle_autopilot_command(
        f"register {json.dumps(sample_registration)}"
    ))


class TestLeaseRequest:
    def test_start_without_lease_shows_permission_checklist(
        self,
        tmp_hermes_home,
        sample_registration,
    ):
        _register(sample_registration)

        result = str(handle_autopilot_command("start"))

        assert "Lease Request: Phase 2 read-only handoff" in result
        assert "[x] workspace.read" in result
        assert "[ ] workspace.write" in result
        assert "/autopilot lease approve phase2-readonly" in result
        assert load_state().get("lease") is None

    def test_start_without_registration_is_actionable(self, tmp_hermes_home):
        result = str(handle_autopilot_command("start"))

        assert "No active project registration" in result
        assert "register" in result.lower()

    def test_request_shows_read_only_permissions_and_approval_command(
        self,
        tmp_hermes_home,
        sample_registration,
    ):
        _register(sample_registration)

        result = str(handle_autopilot_command("lease request"))

        assert "Phase 2 read-only handoff" in result
        assert "[x] workspace.read" in result
        assert "[x] git.read" in result
        assert "[ ] workspace.write" in result
        assert "[ ] git.commit" in result
        assert sample_registration["project_id"] in result
        assert sample_registration["workspace_root"] in result
        assert "/autopilot lease approve phase2-readonly" in result
        assert "No lease has been created" in result
        assert load_state().get("lease") is None

    def test_request_generates_auditable_preview_metadata(
        self,
        tmp_hermes_home,
        sample_registration,
    ):
        _register(sample_registration)

        result = str(handle_autopilot_command("lease wizard phase2-readonly"))

        assert "Created at (UTC):" in result
        assert "Expires (UTC):" in result
        assert "Duration: 2 hours" in result
        assert "Issuer: user" in result

    def test_request_requires_active_registration(self, tmp_hermes_home):
        result = str(handle_autopilot_command("lease request"))

        assert "No active project registration" in result
        assert load_state().get("lease") is None

    def test_unknown_request_preset_is_rejected(self, tmp_hermes_home, sample_registration):
        _register(sample_registration)

        result = str(handle_autopilot_command("lease request broad-write"))

        assert "Unknown lease preset" in result
        assert "phase2-readonly" in result
        assert load_state().get("lease") is None


class TestLeaseApprove:
    def test_approve_creates_only_the_fixed_read_only_lease(
        self,
        tmp_hermes_home,
        sample_registration,
    ):
        _register(sample_registration)

        result = str(handle_autopilot_command("lease approve phase2-readonly"))
        lease = load_state()["lease"]

        assert "Lease approved" in result
        assert lease["project_id"] == sample_registration["project_id"]
        assert lease["workspace_root"] == sample_registration["workspace_root"]
        assert lease["granted_capabilities"] == ["workspace.read", "git.read"]
        assert lease["git_policy"] == "read-only"
        assert lease["dependency_policy"] == "deny"
        assert lease["local_service_policy"] == "deny"
        assert lease["database_policy"] == "read-only"
        assert lease["privileged_account_policy"] == "deny"
        assert lease["external_write_policy"] == "deny"
        assert lease["user_interaction_policy"] == "pause-for-human"
        assert lease["issuer"] == "user"
        created = datetime.fromisoformat(lease["created_at"])
        expiry = datetime.fromisoformat(lease["expiry"])
        assert created.tzinfo is not None
        assert expiry.tzinfo is not None
        assert created <= datetime.now(timezone.utc) < expiry
        assert 7190 <= (expiry - created).total_seconds() <= 7210

    def test_unknown_or_modified_preset_is_rejected_without_creating_lease(
        self,
        tmp_hermes_home,
        sample_registration,
    ):
        _register(sample_registration)

        unknown = str(handle_autopilot_command("lease approve broad-write"))
        modified = str(
            handle_autopilot_command(
                "lease approve phase2-readonly workspace.write"
            )
        )

        assert "Only fixed presets" in unknown
        assert "Only fixed presets" in modified
        assert load_state().get("lease") is None

    def test_approve_requires_active_registration(self, tmp_hermes_home):
        result = str(
            handle_autopilot_command("lease approve phase2-readonly")
        )

        assert "approval blocked" in result.lower()
        assert "no active project registration" in result.lower()
        assert load_state().get("lease") is None

    def test_approved_preset_unlocks_read_only_phase_two_flow(
        self,
        tmp_hermes_home,
        sample_registration,
    ):
        _register(sample_registration)
        handle_autopilot_command("lease approve phase2-readonly")

        handoff = str(handle_autopilot_command("handoff"))
        brief = str(handle_autopilot_command("brief"))
        execute = str(handle_autopilot_command("execute"))

        assert "Status: READY" in handoff
        assert "Development Execution Brief" in brief
        assert "Phase 2 Execution Brief Generated" in execute
        assert "Execution authorized: False" in execute
        assert load_state()["lease"]["granted_capabilities"] == [
            "workspace.read",
            "git.read",
        ]

    def test_start_with_active_lease_guides_next_steps(
        self,
        tmp_hermes_home,
        sample_registration,
    ):
        _register(sample_registration)
        handle_autopilot_command("lease approve phase2-readonly")

        result = str(handle_autopilot_command("start"))

        assert "Autopilot Start" in result
        assert "A lease is already active" in result
        assert "/autopilot brief" in result
        assert "/autopilot handoff" in result
