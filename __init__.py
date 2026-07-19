"""Hermes plugin registration for Project Autopilot."""

from __future__ import annotations

from pathlib import Path
import os
import time

try:  # Hermes loads plugins as packages.
    from .autopilot.commands import handle_autopilot_command
    from .autopilot.decision_runtime import (
        AUTOPILOT_DECIDE_SCHEMA,
        WorkerDecisionHandler,
        check_autopilot_worker,
    )
    from .autopilot.lifecycle import reconcile_task_event, recover_active_loops
    from .autopilot.policy_hook import pre_tool_call_guard
    from .autopilot.runtime import PluginToolRuntime
except ImportError:  # Pytest may import this file as a standalone module.
    from autopilot.commands import handle_autopilot_command
    from autopilot.decision_runtime import (
        AUTOPILOT_DECIDE_SCHEMA,
        WorkerDecisionHandler,
        check_autopilot_worker,
    )
    from autopilot.lifecycle import reconcile_task_event, recover_active_loops
    from autopilot.policy_hook import pre_tool_call_guard
    from autopilot.runtime import PluginToolRuntime


def register(ctx) -> None:
    """Register Project Autopilot slash command and bundled skill."""
    runtime = PluginToolRuntime(ctx)
    decision_handler = WorkerDecisionHandler(ctx.dispatch_tool)
    recovery_state = {"running": False, "last_monotonic": 0.0}

    def run_recovery(*, force: bool = False) -> None:
        if os.environ.get("HERMES_KANBAN_TASK") or recovery_state["running"]:
            return
        now = time.monotonic()
        if not force and now - recovery_state["last_monotonic"] < 300:
            return
        recovery_state["running"] = True
        try:
            recover_active_loops(runtime)
            recovery_state["last_monotonic"] = now
        finally:
            recovery_state["running"] = False

    def command_handler(raw_args: str) -> str | None:
        run_recovery(force=True)
        return handle_autopilot_command(raw_args, runtime=runtime)

    def lifecycle_handler(**kwargs) -> None:
        reconcile_task_event(runtime, **kwargs)

    def startup_recovery_handler(**_kwargs) -> None:
        run_recovery(force=True)

    def activity_recovery_handler(**_kwargs) -> None:
        run_recovery()

    ctx.register_tool(
        name="autopilot_decide",
        toolset="kanban",
        schema=AUTOPILOT_DECIDE_SCHEMA,
        handler=decision_handler.handle,
        check_fn=check_autopilot_worker,
        description="Resolve or pause a structured Autopilot implementation decision",
        emoji="🧭",
    )
    ctx.register_hook("pre_tool_call", pre_tool_call_guard)
    ctx.register_hook("post_tool_call", activity_recovery_handler)
    ctx.register_hook("on_session_start", startup_recovery_handler)
    ctx.register_hook("kanban_task_claimed", lifecycle_handler)
    ctx.register_hook("kanban_task_completed", lifecycle_handler)
    ctx.register_hook("kanban_task_blocked", lifecycle_handler)
    ctx.register_command(
        "autopilot",
        command_handler,
        description="Project-scoped Autopilot: configure verification, approve a lease/brief, dispatch durable Development and verifier workers, sync evidence, or stop",
        args_hint="<command> [args]",
    )

    skill = Path(__file__).parent / "skills" / "project-autopilot" / "SKILL.md"
    if skill.exists():
        ctx.register_skill("project-autopilot", skill)
