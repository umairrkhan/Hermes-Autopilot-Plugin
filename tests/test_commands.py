"""Tests for command dispatcher."""

from __future__ import annotations

import json
import os
import sqlite3
import sys
from pathlib import Path

import pytest

from autopilot.commands import handle_autopilot_command
from autopilot.kill_switch import activate_kill_switch
from autopilot.storage import save_state
from autopilot.constants import SCHEMA_VERSION

# ---------------------------------------------------------------------------
# Basic command dispatch
# ---------------------------------------------------------------------------

class TestCommandDispatcher:
    def test_empty_args_returns_status(self, tmp_hermes_home):
        result = handle_autopilot_command("")
        assert "Status" in result or "status" in result.lower() or "IDLE" in result

    def test_help_command(self, tmp_hermes_home):
        result = handle_autopilot_command("help")
        assert "Commands" in result or "commands" in result.lower() or "Phase 1" in result

    def test_unknown_command(self, tmp_hermes_home):
        result = handle_autopilot_command("foobar")
        assert "Unknown" in result or "unknown" in result.lower()

    def test_status_command(self, tmp_hermes_home):
        result = handle_autopilot_command("status")
        assert "IDLE" in result or "IDLE" in result

    def test_off_when_idle(self, tmp_hermes_home):
        result = handle_autopilot_command("off")
        assert "IDLE" in result or "already" in result.lower()

    def test_stop_always_works(self, tmp_hermes_home):
        result = handle_autopilot_command("stop")
        assert "STOPPED" in result or "stop" in result.lower() or "kill" in result.lower()

    def test_stop_activates_kill_switch(self, tmp_hermes_home):
        handle_autopilot_command("stop")
        from autopilot.kill_switch import is_kill_switch_active
        assert is_kill_switch_active() is True

    def test_lease_inspect_empty(self, tmp_hermes_home):
        result = handle_autopilot_command("lease")
        assert "No active lease" in result or "no lease" in result.lower()

    def test_validate_no_registration(self, tmp_hermes_home):
        result = handle_autopilot_command("validate")
        assert "No registration" in result or "not registered" in result.lower()


# ---------------------------------------------------------------------------
# Registration command
# ---------------------------------------------------------------------------

class TestRegistrationCommand:
    def test_register_no_args(self, tmp_hermes_home):
        result = handle_autopilot_command("register")
        assert "Usage" in result

    def test_register_invalid_json(self, tmp_hermes_home):
        result = handle_autopilot_command("register {bad json}")
        assert "Invalid JSON" in result

    def test_register_valid(self, tmp_hermes_home, tmp_workspace, sample_registration):
        result = handle_autopilot_command(
            f"register {json.dumps(sample_registration)}"
        )
        assert "Registered" in result or "project" in result.lower()

    def test_register_accepts_shell_quoted_json(self, tmp_hermes_home, tmp_workspace, sample_registration):
        payload = json.dumps(sample_registration)
        result = handle_autopilot_command(f"register '{payload}'")
        assert "Registered" in result

    def test_register_accepts_trailing_middle_dot_marker(self, tmp_hermes_home, tmp_workspace, sample_registration):
        payload = json.dumps(sample_registration)
        result = handle_autopilot_command(f"register '{payload}'·")
        assert "Registered" in result

    def test_register_auto_resolves_session_ids(self, tmp_hermes_home, tmp_workspace, monkeypatch):
        conn = sqlite3.connect(tmp_hermes_home / "state.db")
        try:
            conn.execute("CREATE TABLE sessions (id TEXT PRIMARY KEY, title TEXT, started_at REAL)")
            conn.execute(
                "INSERT INTO sessions (id, title, started_at) VALUES (?, ?, ?)",
                ("sess-disc", "Solar360 Project Discussion", 1.0),
            )
            conn.execute(
                "INSERT INTO sessions (id, title, started_at) VALUES (?, ?, ?)",
                ("sess-dev", "Solar360 Development", 1.0),
            )
            conn.commit()
        finally:
            conn.close()
        monkeypatch.setenv("HERMES_SESSION_KEY", "sess-disc")

        result = handle_autopilot_command(
            "register " + json.dumps({
                "project_id": "solar360",
                "workspace_root": str(tmp_workspace),
                "display_title": "Solar360",
                "discussion_title": "Solar360 Project Discussion",
                "development_title": "Solar360 Development",
            })
        )

        assert "Registered" in result
        assert "discussion_session_id auto-filled" in result
        assert "development_session_id auto-filled" in result

    def test_register_bad_project_id(self, tmp_hermes_home, tmp_workspace, sample_registration):
        sample_registration["project_id"] = "x"
        result = handle_autopilot_command(
            f"register {json.dumps(sample_registration)}"
        )
        assert "validation failed" in result.lower() or "invalid" in result.lower()

    def test_register_bad_workspace(self, tmp_hermes_home, sample_registration):
        sample_registration["workspace_root"] = "/nonexistent/path/xyz"
        result = handle_autopilot_command(
            f"register {json.dumps(sample_registration)}"
        )
        assert "validation failed" in result.lower()


# ---------------------------------------------------------------------------
# Lease command
# ---------------------------------------------------------------------------

class TestLeaseCommand:
    def test_lease_invalid_json(self, tmp_hermes_home):
        result = handle_autopilot_command("lease {bad}")
        assert "Invalid JSON" in result

    def test_lease_valid(self, tmp_hermes_home, sample_lease):
        result = handle_autopilot_command(f"lease {json.dumps(sample_lease)}")
        assert "Lease loaded" in result or "lease" in result.lower()

    def test_lease_expired(self, tmp_hermes_home, expired_lease):
        result = handle_autopilot_command(f"lease {json.dumps(expired_lease)}")
        assert "expired" in result.lower()

    def test_lease_inspect_after_load(self, tmp_hermes_home, sample_lease):
        handle_autopilot_command(f"lease {json.dumps(sample_lease)}")
        result = handle_autopilot_command("lease")
        assert "lease-test-001" in result


# ---------------------------------------------------------------------------
# Simulate command
# ---------------------------------------------------------------------------

class TestSimulateCommand:
    def test_simulate_no_lease(self, tmp_hermes_home, sample_registration):
        handle_autopilot_command(
            f"register {json.dumps(sample_registration)}"
        )
        result = handle_autopilot_command("simulate")
        # Should work in CONFIGURED state too
        assert "simulation" in result.lower() or "simulat" in result.lower()

    def test_simulate_with_lease(self, tmp_hermes_home, sample_registration, sample_lease):
        handle_autopilot_command(
            f"register {json.dumps(sample_registration)}"
        )
        handle_autopilot_command(f"lease {json.dumps(sample_lease)}")
        result = handle_autopilot_command("simulate")
        assert "Simulation" in result or "simulat" in result.lower()
        assert "PASS" in result or "accepted" in result.lower()

    def test_simulate_wrong_state(self, tmp_hermes_home):
        # FAILED state should reject simulation
        from autopilot.constants import STATE_FAILED, SCHEMA_VERSION
        from autopilot.storage import save_state
        save_state({"state": STATE_FAILED, "schema_version": SCHEMA_VERSION})
        result = handle_autopilot_command("simulate")
        assert "Cannot simulate" in result


# ---------------------------------------------------------------------------
# Kill switch interaction
# ---------------------------------------------------------------------------

class TestCommandKillSwitch:
    def test_commands_blocked_by_kill_switch(self, tmp_hermes_home, tmp_workspace, sample_registration):
        activate_kill_switch("test block")
        result = handle_autopilot_command("status")
        assert "Kill switch" in result or "STOPPED" in result or "kill" in result.lower()

    def test_stop_always_works_with_kill_switch(self, tmp_hermes_home):
        activate_kill_switch("test")
        result = handle_autopilot_command("stop")
        assert "kill" in result.lower() or "stop" in result.lower()

    def test_validate_blocked_by_kill_switch(self, tmp_hermes_home):
        activate_kill_switch("test")
        result = handle_autopilot_command("validate")
        assert "Kill switch" in result or "kill" in result.lower()

    def test_help_blocked_by_kill_switch(self, tmp_hermes_home):
        activate_kill_switch("test")
        result = handle_autopilot_command("help")
        assert "Kill switch" in result or "kill" in result.lower()


# ---------------------------------------------------------------------------
# Command shape validation
# ---------------------------------------------------------------------------

class TestCommandShape:
    def test_all_commands_return_string(self, tmp_hermes_home):
        commands = ["status", "help", "validate", "lease", "off", "stop"]
        for cmd in commands:
            result = handle_autopilot_command(cmd)
            assert isinstance(result, str), f"Command {cmd} did not return str"

    def test_register_returns_string(self, tmp_hermes_home, sample_registration):
        result = handle_autopilot_command(f"register {json.dumps(sample_registration)}")
        assert isinstance(result, str)

    def test_simulate_returns_string(self, tmp_hermes_home, sample_registration, sample_lease):
        handle_autopilot_command(f"register {json.dumps(sample_registration)}")
        handle_autopilot_command(f"lease {json.dumps(sample_lease)}")
        result = handle_autopilot_command("simulate")
        assert isinstance(result, str)

    def test_on_not_available_phase1(self, tmp_hermes_home):
        """Phase 1: /autopilot on must reject safely."""
        result = handle_autopilot_command("on")
        assert "Unknown" in result or "not available" in result.lower() or "Phase 1" in result
