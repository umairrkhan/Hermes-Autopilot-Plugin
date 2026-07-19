"""Tests for the real Hermes plugin entrypoint."""

from __future__ import annotations

import importlib.util
from pathlib import Path


class FakeContext:
    def __init__(self):
        self.hooks = {}
        self.commands = {}
        self.skills = {}
        self.tools = {}

    def dispatch_tool(self, name, args):
        return {"ok": True, "status": "blocked"}

    def register_tool(self, **kwargs):
        self.tools[kwargs["name"]] = kwargs

    def register_hook(self, name, callback):
        self.hooks[name] = callback

    def register_command(self, name, callback, **metadata):
        self.commands[name] = (callback, metadata)

    def register_skill(self, name, path):
        self.skills[name] = path


def test_register_exposes_runtime_command_and_lifecycle_hooks():
    root = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location(
        "project_autopilot_plugin_entrypoint",
        root / "__init__.py",
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    ctx = FakeContext()

    module.register(ctx)

    assert set(ctx.hooks) == {
        "pre_tool_call",
        "post_tool_call",
        "on_session_start",
        "kanban_task_claimed",
        "kanban_task_completed",
        "kanban_task_blocked",
    }
    assert "autopilot" in ctx.commands
    assert "autopilot_decide" in ctx.tools
    assert ctx.tools["autopilot_decide"]["toolset"] == "kanban"

    response = ctx.commands["autopilot"][0]("help")
    assert "Project Autopilot Commands" in response
    assert "project-autopilot" in ctx.skills
