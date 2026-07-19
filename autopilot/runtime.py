"""Hermes-hosted runtime bridge for Autopilot command execution."""

from __future__ import annotations

import json
import shlex
from typing import Any

from .adapters.development_executor import CommandResult


class PluginToolRuntime:
    """Route argv through Hermes' public plugin tool-dispatch interface."""

    def __init__(self, plugin_context: Any):
        self._context = plugin_context

    def run(
        self,
        argv: tuple[str, ...],
        *,
        cwd: str | None,
        timeout_seconds: int,
    ) -> CommandResult:
        if not argv or not all(
            isinstance(part, str) and part and "\x00" not in part
            for part in argv
        ):
            return CommandResult(-1, "", "invalid argv")
        args: dict[str, Any] = {
            "command": shlex.join(argv),
            "timeout": timeout_seconds,
        }
        if cwd:
            args["workdir"] = cwd
        try:
            raw = self._context.dispatch_tool("terminal", args)
            payload = json.loads(raw)
        except Exception as exc:
            return CommandResult(-1, "", f"terminal dispatch failed: {exc}")
        if not isinstance(payload, dict):
            return CommandResult(-1, "", "terminal dispatch returned invalid payload")
        exit_code = payload.get("exit_code", -1)
        if not isinstance(exit_code, int) or isinstance(exit_code, bool):
            exit_code = -1
        stdout = payload.get("output", "")
        error = payload.get("error", "")
        return CommandResult(
            exit_code=exit_code,
            stdout=str(stdout or ""),
            stderr=str(error or ""),
        )
