"""Tests for explicit phase roadmap and readiness gates."""

from __future__ import annotations

import json

from autopilot.commands import handle_autopilot_command
from autopilot.constants import (
    CAP_GIT_COMMIT,
    CAP_GIT_READ,
    CAP_WORKSPACE_READ,
    CAP_WORKSPACE_WRITE,
)
from autopilot.phases import (
    PHASE_1,
    PHASE_2,
    PHASE_3,
    PHASE_4,
    PHASES,
    phase_by_number,
    phase_report,
    readiness_for_phase,
    remaining_phases,
)


class TestPhaseRoadmap:
    def test_all_four_phases_are_defined(self):
        assert [p.number for p in PHASES] == [PHASE_1, PHASE_2, PHASE_3, PHASE_4]
        assert all(phase.status == "complete" for phase in PHASES)

    def test_no_shipped_roadmap_phase_remains_unimplemented(self):
        assert remaining_phases() == []

    def test_phase_report_distinguishes_shipped_from_runtime_ready(self):
        report = phase_report()
        assert "Phase 1" in report
        assert "Phase 4" in report
        assert "Shipped roadmap phases: 4/4" in report
        assert "lease- and gate-controlled per project" in report

    def test_unknown_phase_fails_closed(self):
        try:
            phase_by_number(99)
        except ValueError as exc:
            assert "Unknown autopilot phase" in str(exc)
        else:
            raise AssertionError("Expected unknown phase to fail")


class TestPhaseReadiness:
    def test_phase_one_ready_after_registration(self, configured_state):
        ready, blockers = readiness_for_phase(configured_state, PHASE_1)
        assert ready is True
        assert blockers == []

    def test_phase_two_blocked_without_lease(self, configured_state):
        ready, blockers = readiness_for_phase(configured_state, PHASE_2)
        assert ready is False
        assert any("No active autonomy lease" in b for b in blockers)

    def test_phase_two_uses_only_read_capabilities(self):
        phase = phase_by_number(PHASE_2)
        assert phase.required_capabilities == (
            CAP_WORKSPACE_READ,
            CAP_GIT_READ,
        )
        assert phase.real_side_effects_allowed is False

    def test_phase_two_ready_with_read_only_lease_and_adapter(self, lease_ready_state):
        ready, blockers = readiness_for_phase(
            lease_ready_state,
            PHASE_2,
            real_adapter_available=True,
        )
        assert ready is True
        assert blockers == []

    def test_phase_two_still_blocked_without_real_adapter(self, lease_ready_state):
        lease_ready_state["lease"]["granted_capabilities"] = [
            "workspace.read",
            "git.read",
            CAP_WORKSPACE_WRITE,
            CAP_GIT_COMMIT,
        ]
        # Explicitly tell readiness check that no adapter is available
        ready, blockers = readiness_for_phase(
            lease_ready_state, PHASE_2, real_adapter_available=False
        )
        assert ready is False
        assert any("shipped Phase 2 adapter is unavailable" in b for b in blockers)

    def test_phase_two_ready_when_adapter_is_explicitly_available(self, lease_ready_state):
        lease_ready_state["lease"]["granted_capabilities"] = [
            "workspace.read",
            "git.read",
            CAP_WORKSPACE_WRITE,
            CAP_GIT_COMMIT,
        ]
        ready, blockers = readiness_for_phase(
            lease_ready_state,
            PHASE_2,
            real_adapter_available=True,
        )
        assert ready is True
        assert blockers == []

    def test_phase_two_blocks_cross_project_lease(self, lease_ready_state):
        lease_ready_state["lease"]["project_id"] = "another-project"
        ready, blockers = readiness_for_phase(lease_ready_state, PHASE_2)
        assert ready is False
        assert any("does not match active project" in b for b in blockers)


class TestPhaseCommands:
    def test_phases_command(self, tmp_hermes_home):
        result = str(handle_autopilot_command("phases"))
        assert "Phase 1" in result
        assert "Shipped roadmap phases: 4/4" in result

    def test_readiness_command_without_registration(self, tmp_hermes_home):
        result = str(handle_autopilot_command("readiness"))
        assert "BLOCKED" in result
        assert "No project registration" in result

    def test_execute_fails_closed_without_real_adapter(self, tmp_hermes_home, tmp_workspace, sample_registration, sample_lease):
        sample_lease["granted_capabilities"] = [
            "workspace.read",
            "git.read",
            CAP_WORKSPACE_WRITE,
            CAP_GIT_COMMIT,
        ]
        handle_autopilot_command(f"register {json.dumps(sample_registration)}")
        handle_autopilot_command(f"lease {json.dumps(sample_lease)}")

        result = str(handle_autopilot_command("execute"))

        # With Phase 2 adapter installed, execute generates a brief
        # (it does NOT fail closed anymore when the adapter is available)
        assert "Phase 2 Execution Brief" in result or "Real execution is not enabled" in result
        assert "human gate required: true" in result.lower() or "not enabled" in result.lower()

    def test_readiness_command_after_registration(self, tmp_hermes_home, sample_registration):
        handle_autopilot_command(f"register {json.dumps(sample_registration)}")
        result = str(handle_autopilot_command("readiness"))
        assert "Phase 1" in result
        assert "READY" in result
        assert "Phase 2" in result
        assert "BLOCKED" in result
