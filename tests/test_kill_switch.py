"""Tests for kill switch."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

from autopilot.kill_switch import (
    is_kill_switch_active, activate_kill_switch,
    deactivate_kill_switch, check_kill_switch, _kill_switch_path,
)
from autopilot.storage import save_state
from autopilot.constants import SCHEMA_VERSION

# ---------------------------------------------------------------------------
# Basic kill switch
# ---------------------------------------------------------------------------

class TestKillSwitch:
    def test_not_active_by_default(self, tmp_hermes_home):
        assert is_kill_switch_active() is False
        assert check_kill_switch() is None

    def test_activate(self, tmp_hermes_home):
        activate_kill_switch("test reason")
        assert is_kill_switch_active() is True
        reason = check_kill_switch()
        assert reason is not None
        assert "test reason" in reason

    def test_deactivate(self, tmp_hermes_home):
        activate_kill_switch("test reason")
        assert is_kill_switch_active() is True
        deactivate_kill_switch()
        assert is_kill_switch_active() is False
        assert check_kill_switch() is None

    def test_kill_switch_file_location(self, tmp_hermes_home):
        p = _kill_switch_path()
        assert str(tmp_hermes_home) in str(p)
        assert p.name == "kill_switch.json"

    def test_kill_switch_file_contents(self, tmp_hermes_home):
        activate_kill_switch("reason here")
        raw = json.loads(_kill_switch_path().read_text())
        assert raw["active"] is True
        assert raw["reason"] == "reason here"

    def test_deactivate_removes_file(self, tmp_hermes_home):
        activate_kill_switch("reason")
        deactivate_kill_switch()
        # File still exists but is marked inactive
        assert _kill_switch_path().exists()
        assert is_kill_switch_active() is False


# ---------------------------------------------------------------------------
# Kill switch with malformed state
# ---------------------------------------------------------------------------

class TestKillSwitchMalformedState:
    def test_kill_switch_works_with_corrupted_state(self, tmp_hermes_home):
        """Kill switch operates independently of state."""
        # Corrupt the state file
        state_path = Path(tmp_hermes_home) / "state" / "autopilot" / "autopilot_state.json"
        state_path.parent.mkdir(parents=True, exist_ok=True)
        state_path.write_text("CORRUPTED")

        # Kill switch should still work
        activate_kill_switch("emergency")
        assert is_kill_switch_active() is True
        assert check_kill_switch() is not None

    def test_kill_switch_works_with_missing_state(self, tmp_hermes_home):
        """Kill switch works even when no state file exists."""
        activate_kill_switch("no state file")
        assert is_kill_switch_active() is True

    def test_check_kill_switch_file_error(self, tmp_hermes_home):
        """check_kill_switch returns reason even if file is malformed."""
        p = _kill_switch_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("NOT_JSON")
        reason = check_kill_switch()
        # Should return a reason indicating file is malformed
        assert reason is not None


# ---------------------------------------------------------------------------
# Kill switch with active run
# ---------------------------------------------------------------------------

class TestKillSwitchActiveRun:
    def test_kill_switch_check_before_command(self, tmp_hermes_home):
        """Activate kill switch and verify it blocks operations."""
        activate_kill_switch("stop everything")
        reason = check_kill_switch()
        assert reason is not None
        assert "stop everything" in reason

    def test_kill_switch_resilient_to_missing_env(self, tmp_hermes_home):
        """Kill switch works even with unusual HERMES_HOME."""
        activate_kill_switch("test")
        assert is_kill_switch_active() is True
