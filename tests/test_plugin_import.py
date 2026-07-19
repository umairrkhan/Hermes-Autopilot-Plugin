"""Tests for plugin import, discovery, and registration."""

from __future__ import annotations

import sys
import importlib
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Plugin package import
# ---------------------------------------------------------------------------

class TestPluginImport:
    def test_import_autopilot_package(self):
        """The autopilot package must be importable."""
        import autopilot
        assert hasattr(autopilot, '__plugin__')

    def test_import_constants(self):
        from autopilot import constants
        assert hasattr(constants, 'SCHEMA_VERSION')
        assert hasattr(constants, 'PLUGIN_VERSION')
        assert hasattr(constants, 'PLUGIN_NAME')

    def test_import_registration(self):
        from autopilot import registration
        assert hasattr(registration, 'validate_registration')

    def test_import_lease(self):
        from autopilot import lease
        assert hasattr(lease, 'validate_lease')
        assert hasattr(lease, 'AutonomyLease')

    def test_import_policy(self):
        from autopilot import policy
        assert hasattr(policy, 'classify_risk')
        assert hasattr(policy, 'check_capability')

    def test_import_state_machine(self):
        from autopilot import state_machine
        assert hasattr(state_machine, 'transition')
        assert hasattr(state_machine, 'can_transition')

    def test_import_storage(self):
        from autopilot import storage
        assert hasattr(storage, 'load_state')
        assert hasattr(storage, 'save_state')

    def test_import_kill_switch(self):
        from autopilot import kill_switch
        assert hasattr(kill_switch, 'is_kill_switch_active')
        assert hasattr(kill_switch, 'activate_kill_switch')

    def test_import_commands(self):
        from autopilot import commands
        assert hasattr(commands, 'handle_autopilot_command')

    def test_import_adapters(self):
        from autopilot.adapters import (
            SimulationAdapter, ReadOnlySessionAdapter, ReadOnlyKanbanAdapter,
        )

    def test_import_audit(self):
        from autopilot import audit
        assert hasattr(audit, 'log_event')
        assert hasattr(audit, '_redact_value')

    def test_import_schemas(self):
        from autopilot import schemas
        assert hasattr(schemas, 'AUTOPILOT_STATUS')


# ---------------------------------------------------------------------------
# Plugin dict shape
# ---------------------------------------------------------------------------

class TestPluginShape:
    def test_plugin_has_required_keys(self):
        import autopilot
        p = autopilot.__plugin__
        assert "name" in p
        assert "version" in p
        assert "commands" in p
        assert "tools" in p
        assert "hooks" in p

    def test_plugin_name(self):
        import autopilot
        assert autopilot.__plugin__["name"] == "project-autopilot"

    def test_plugin_version(self):
        import autopilot
        assert autopilot.__plugin__["version"] == "0.1.0"

    def test_plugin_has_command(self):
        import autopilot
        cmds = autopilot.__plugin__["commands"]
        assert len(cmds) >= 1
        cmd_names = [c["name"] for c in cmds]
        assert "autopilot" in cmd_names

    def test_plugin_has_tool(self):
        import autopilot
        tools = autopilot.__plugin__["tools"]
        assert len(tools) >= 1
        tool_names = [t["name"] for t in tools]
        assert "autopilot_status" in tool_names

    def test_command_handler_callable(self):
        import autopilot
        cmd = autopilot.__plugin__["commands"][0]
        assert callable(cmd["handler"])

    def test_plugin_no_hooks(self):
        import autopilot
        assert autopilot.__plugin__["hooks"] == []
