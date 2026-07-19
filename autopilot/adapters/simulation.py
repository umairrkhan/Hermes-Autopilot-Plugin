"""Simulation adapter — fake adapters that simulate the full lifecycle.

Phase 1 only. Simulates:
  Planner -> Development -> Verifier -> Reviewer -> one bounded remediation
  loop -> phase acceptance

No real LLM calls, no real file operations, no real side effects.
The simulation is deterministic and inspectable.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class SimulationStep:
    """A single step in the simulation."""
    role: str  # "planner", "developer", "verifier", "reviewer", "remediator"
    action: str
    result: str
    success: bool
    details: dict[str, Any] = field(default_factory=dict)


@dataclass
class SimulationResult:
    """Complete simulation result."""
    steps: list[SimulationStep] = field(default_factory=list)
    accepted: bool = False
    remediation_used: bool = False
    remediation_count: int = 0
    max_remediation_reached: bool = False
    error: str = ""
    total_steps: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "steps": [
                {
                    "role": s.role,
                    "action": s.action,
                    "result": s.result,
                    "success": s.success,
                    "details": s.details,
                }
                for s in self.steps
            ],
            "accepted": self.accepted,
            "remediation_used": self.remediation_used,
            "remediation_count": self.remediation_count,
            "max_remediation_reached": self.max_remediation_reached,
            "error": self.error,
            "total_steps": self.total_steps,
        }


class SimulationAdapter:
    """Fake adapter that simulates the full autopilot lifecycle.

    Phase 1: this is the only execution path. No real development work.
    """

    def __init__(self, max_remediation: int = 1):
        self._max_remediation = max_remediation

    def run_simulation(
        self,
        registration: dict[str, Any] | None = None,
        lease: dict[str, Any] | None = None,
    ) -> SimulationResult:
        """Run a complete simulated lifecycle.

        Returns a SimulationResult with all steps recorded.
        """
        result = SimulationResult()

        # Phase 1 guard: must have a project registration. A lease is optional in
        # offline simulation because no real side effects can occur. Real
        # execution phases must require a valid lease before adapter calls.
        if not registration:
            result.error = "No registration provided"
            return result

        # Step 1: Planner
        planner_step = SimulationStep(
            role="planner",
            action="analyze_requirements",
            result="Plan generated with 3 tasks",
            success=True,
            details={"tasks": ["task_a", "task_b", "task_c"]},
        )
        result.steps.append(planner_step)

        # Step 2: Developer
        dev_step = SimulationStep(
            role="developer",
            action="implement_changes",
            result="Changes implemented in 2 files",
            success=True,
            details={"files_modified": ["src/main.py", "tests/test_main.py"]},
        )
        result.steps.append(dev_step)

        # Step 3: Verifier
        verify_step = SimulationStep(
            role="verifier",
            action="run_tests",
            result="All 5 tests pass",
            success=True,
            details={"tests_passed": 5, "tests_failed": 0},
        )
        result.steps.append(verify_step)

        # Step 4: Reviewer
        review_step = SimulationStep(
            role="reviewer",
            action="code_review",
            result="Review passed with 1 suggestion",
            success=True,
            details={"suggestions": 1, "blocking_issues": 0},
        )
        result.steps.append(review_step)

        # Step 5: One bounded remediation (always run to demonstrate the cycle)
        remediator_step = SimulationStep(
            role="remediator",
            action="apply_suggestion",
            result="Applied review suggestion",
            success=True,
            details={"applied": ["style_suggestion_1"]},
        )
        result.steps.append(remediator_step)
        result.remediation_used = True
        result.remediation_count = 1

        # Re-verify after remediation
        reverify_step = SimulationStep(
            role="verifier",
            action="reverify_after_remediation",
            result="All 5 tests still pass",
            success=True,
            details={"tests_passed": 5, "tests_failed": 0},
        )
        result.steps.append(reverify_step)

        # Phase acceptance
        result.accepted = True
        result.total_steps = len(result.steps)

        logger.info(
            "Simulation complete: %d steps, accepted=%s, remediation_count=%d",
            result.total_steps, result.accepted, result.remediation_count,
        )
        return result

    def simulate_verifier_failure(self) -> SimulationResult:
        """Simulate a verifier failure scenario."""
        result = SimulationResult()

        result.steps.append(SimulationStep(
            role="planner",
            action="analyze_requirements",
            result="Plan generated",
            success=True,
        ))
        result.steps.append(SimulationStep(
            role="developer",
            action="implement_changes",
            result="Changes implemented",
            success=True,
        ))
        result.steps.append(SimulationStep(
            role="verifier",
            action="run_tests",
            result="2 tests failed",
            success=False,
            details={"tests_passed": 3, "tests_failed": 2},
        ))

        # Remediation attempt
        result.steps.append(SimulationStep(
            role="remediator",
            action="fix_failing_tests",
            result="Attempted fix",
            success=True,
        ))
        result.remediation_used = True
        result.remediation_count = 1

        # Re-verify still fails
        result.steps.append(SimulationStep(
            role="verifier",
            action="reverify",
            result="Tests still failing",
            success=False,
        ))

        result.accepted = False
        result.total_steps = len(result.steps)
        return result

    def simulate_reviewer_failure(self) -> SimulationResult:
        """Simulate a reviewer blocking failure."""
        result = SimulationResult()

        result.steps.append(SimulationStep(
            role="planner",
            action="analyze_requirements",
            result="Plan generated",
            success=True,
        ))
        result.steps.append(SimulationStep(
            role="developer",
            action="implement_changes",
            result="Changes implemented",
            success=True,
        ))
        result.steps.append(SimulationStep(
            role="verifier",
            action="run_tests",
            result="All tests pass",
            success=True,
        ))
        result.steps.append(SimulationStep(
            role="reviewer",
            action="code_review",
            result="Blocking issue found",
            success=False,
            details={"blocking_issues": 1},
        ))

        result.accepted = False
        result.total_steps = len(result.steps)
        return result

    def simulate_max_remediation_reached(self) -> SimulationResult:
        """Simulate hitting the maximum remediation loop limit."""
        result = SimulationResult()

        for i in range(self._max_remediation + 1):
            result.steps.append(SimulationStep(
                role="developer",
                action=f"implement_attempt_{i}",
                result=f"Implementation attempt {i}",
                success=True,
            ))
            result.steps.append(SimulationStep(
                role="verifier",
                action=f"verify_attempt_{i}",
                result=f"Tests failing on attempt {i}",
                success=False,
            ))
            if i < self._max_remediation:
                result.steps.append(SimulationStep(
                    role="remediator",
                    action=f"remediate_attempt_{i}",
                    result=f"Remediation attempt {i}",
                    success=True,
                ))
                result.remediation_count += 1

        result.remediation_used = result.remediation_count > 0
        result.max_remediation_reached = True
        result.accepted = False
        result.total_steps = len(result.steps)
        return result
