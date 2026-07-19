"""Best-effort Kanban lifecycle hooks for low-latency loop reconciliation."""

from __future__ import annotations

import logging
from typing import Any

from .adapters.autonomous_loop import AutonomousLoopSupervisor
from .adapters.development_executor import CommandRuntime
from .adapters.loop_reconciler import LoopReconciler
from .policy_hook import find_task_binding
from .storage import list_projects, load_project_state
from .verification import load_verification_profile

logger = logging.getLogger(__name__)

_RECOVERABLE_STATUSES = {"QUEUED", "RUNNING", "VERIFYING", "MERGING", "REMEDIATING"}


def recover_active_loops(
    runtime: CommandRuntime,
    *,
    supervisor: AutonomousLoopSupervisor | None = None,
    max_loops: int = 50,
) -> dict[str, int]:
    """Idempotently reconcile bounded active loops after missed lifecycle events."""

    supervisor = supervisor or AutonomousLoopSupervisor()
    summary = {"examined": 0, "reconciled": 0, "errors": 0}
    for project_id in list_projects():
        if summary["examined"] >= max_loops:
            break
        try:
            state = load_project_state(project_id)
            profile = load_verification_profile(project_id)
        except Exception:
            continue
        if profile is None:
            continue
        for loop in supervisor.list_loops(project_id):
            if summary["examined"] >= max_loops:
                break
            if loop.get("status") not in _RECOVERABLE_STATUSES:
                continue
            loop_id = loop.get("loop_id")
            if not isinstance(loop_id, str) or not loop_id:
                continue
            summary["examined"] += 1
            try:
                result = LoopReconciler(runtime, supervisor).sync(
                    loop=loop,
                    state=state,
                    profile=profile,
                )
                error = "; ".join(result.blockers) if result.blockers else ""
                supervisor.record_recovery_result(
                    project_id=project_id,
                    loop_id=loop_id,
                    error=error,
                )
                if error:
                    summary["errors"] += 1
                else:
                    summary["reconciled"] += 1
            except Exception as exc:
                summary["errors"] += 1
                supervisor.record_recovery_result(
                    project_id=project_id,
                    loop_id=loop_id,
                    error=str(exc),
                )
                logger.warning(
                    "Autopilot recovery failed for loop %s: %s",
                    loop_id,
                    exc,
                )
    return summary


def reconcile_task_event(
    runtime: CommandRuntime,
    *,
    task_id: str = "",
    **_: Any,
) -> None:
    """Reconcile an Autopilot-bound task; ordinary Kanban tasks are ignored."""

    binding = find_task_binding(task_id)
    if binding is None:
        return
    project_id = binding.get("project_id")
    if not isinstance(project_id, str) or not project_id:
        return
    supervisor = AutonomousLoopSupervisor()
    try:
        state = load_project_state(project_id)
        profile = load_verification_profile(project_id)
        if profile is None:
            return
        LoopReconciler(runtime, supervisor).sync(
            loop=binding,
            state=state,
            profile=profile,
        )
    except Exception as exc:  # Hooks must never break Kanban terminalization.
        loop_id = binding.get("loop_id")
        if isinstance(loop_id, str) and loop_id:
            supervisor.record_recovery_result(
                project_id=project_id,
                loop_id=loop_id,
                error=str(exc),
            )
        logger.warning(
            "Autopilot lifecycle reconciliation failed for task %s: %s",
            task_id,
            exc,
        )
