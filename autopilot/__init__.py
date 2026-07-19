"""Hermes Project Autopilot — plugin package."""

from __future__ import annotations

from .constants import PLUGIN_VERSION, PLUGIN_NAME
from .commands import handle_autopilot_command
from .schemas import AUTOPILOT_STATUS

__version__ = PLUGIN_VERSION

__plugin__ = {
    "name": PLUGIN_NAME,
    "version": PLUGIN_VERSION,
    "description": (
        "Project-agnostic Hermes Project Autopilot — Phase 1 safe foundation "
        "with offline simulation + Phase 2 execution bridge for guarded "
        "Discussion→Development handoff. No uncontrolled autonomous execution."
    ),
    "commands": [
        {
            "name": "autopilot",
            "description": "Project Autopilot commands (status, register, validate, lease, projects, use, phases, readiness, execute, brief, handoff, simulate, off, stop, help)",
            "handler": handle_autopilot_command,
        }
    ],
    "tools": [AUTOPILOT_STATUS],
    "hooks": [],
}
