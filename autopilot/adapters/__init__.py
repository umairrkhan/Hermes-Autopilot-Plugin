"""Adapter interfaces and simulation adapters.

Phase 1: read-only session/kanban adapters + fake simulation adapters
that simulate Planner -> Development -> Verifier -> Reviewer -> one bounded
remediation loop -> phase acceptance.

Phase 2: execution bridge adapter that generates development execution briefs
for Discussion→Development handoff. Fail-closed: produces handoff artifacts
but does not perform autonomous execution.
"""
from __future__ import annotations

from .base import BaseSessionAdapter, BaseKanbanAdapter
from .session import ReadOnlySessionAdapter
from .kanban import ReadOnlyKanbanAdapter
from .simulation import SimulationAdapter
from .execution_bridge import (
    ExecutionBridge,
    DevelopmentExecutionBrief,
    BriefTask,
    BriefGenerationResult,
)
from .runner import DevelopmentRunner, DevelopmentRunPackage, DevelopmentRunResult
from .autonomous_loop import AutonomousLoopSupervisor, AutonomousLoop, LoopStartResult

__all__ = [
    "BaseSessionAdapter",
    "BaseKanbanAdapter",
    "ReadOnlySessionAdapter",
    "ReadOnlyKanbanAdapter",
    "SimulationAdapter",
    "ExecutionBridge",
    "DevelopmentExecutionBrief",
    "BriefTask",
    "BriefGenerationResult",
    "DevelopmentRunner",
    "DevelopmentRunPackage",
    "DevelopmentRunResult",
    "AutonomousLoopSupervisor",
    "AutonomousLoop",
    "LoopStartResult",
]
