"""Structured, durable decision handling for Autopilot Kanban workers."""

from __future__ import annotations

import json
import os
from typing import Any, Callable

from .adapters.autonomous_loop import AutonomousLoopSupervisor
from .policy_hook import find_task_binding
from .question_policy import (
    HUMAN_REQUIRED_CATEGORIES,
    ROUTINE_LOW_RISK_CATEGORY,
    Question,
    decide_question,
)

DECISION_CATEGORIES = (ROUTINE_LOW_RISK_CATEGORY, *sorted(HUMAN_REQUIRED_CATEGORIES))

AUTOPILOT_DECIDE_SCHEMA = {
    "name": "autopilot_decide",
    "description": (
        "Classify and durably resolve a choice before an Autopilot Development or "
        "remediation worker selects it. You MUST call this tool instead of choosing "
        "silently whenever implementation presents alternatives. Only typed "
        "routine_low_risk choices with an explicit recommendation may auto-resolve; "
        "all other categories are persisted and the task is blocked for human input."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "question_id": {
                "type": "string",
                "description": "Stable identifier for this question (letters, digits, hyphen, underscore).",
            },
            "text": {"type": "string", "description": "The bounded question to resolve."},
            "category": {
                "type": "string",
                "enum": list(DECISION_CATEGORIES),
                "description": "Typed decision category. Use routine_low_risk only for local reversible convention choices.",
            },
            "choices": {
                "type": "array",
                "items": {"type": "string"},
                "maxItems": 8,
                "description": "Presented choices, if any.",
            },
            "recommended_choice": {
                "type": "string",
                "description": "Explicit recommendation/default, or empty when none exists.",
            },
            "context": {
                "type": "string",
                "description": "Short risk and reversibility context; do not include secrets or personal data.",
            },
        },
        "required": ["question_id", "text", "category", "choices", "recommended_choice", "context"],
        "additionalProperties": False,
    },
}


def check_autopilot_worker() -> bool:
    """Expose the tool only inside dispatcher workers for an Autopilot tenant."""

    return bool(os.environ.get("HERMES_KANBAN_TASK")) and os.environ.get(
        "HERMES_TENANT", ""
    ).startswith("autopilot:")


def _as_payload(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


class WorkerDecisionHandler:
    """Resolve worker decisions and enforce a Kanban pause when human input is required."""

    def __init__(
        self,
        dispatch_tool: Callable[..., Any],
        *,
        supervisor: AutonomousLoopSupervisor | None = None,
    ) -> None:
        self._dispatch_tool = dispatch_tool
        self._supervisor = supervisor or AutonomousLoopSupervisor()

    def handle(self, args: dict[str, Any], **_: Any) -> str:
        task_id = os.environ.get("HERMES_KANBAN_TASK", "").strip()
        tenant = os.environ.get("HERMES_TENANT", "").strip()
        if not task_id or not tenant.startswith("autopilot:"):
            return json.dumps({"ok": False, "error": "autopilot_decide is worker-scoped"})

        binding = find_task_binding(task_id)
        if binding is None:
            return json.dumps({
                "ok": False,
                "error": "Autopilot task binding is missing; the worker must stop fail-closed.",
            })
        project_id = binding.get("project_id")
        loop_id = binding.get("loop_id")
        board = binding.get("board_slug")
        if not all(isinstance(value, str) and value for value in (project_id, loop_id, board)):
            return json.dumps({"ok": False, "error": "Autopilot task binding is corrupt"})
        project_id = str(project_id)
        loop_id = str(loop_id)
        board = str(board)
        if tenant != f"autopilot:{project_id}":
            return json.dumps({"ok": False, "error": "Autopilot tenant does not match the loop project"})

        try:
            question = Question(
                question_id=str(args.get("question_id", "")),
                text=str(args.get("text", "")),
                category=str(args.get("category", "")),
                choices=tuple(args.get("choices", [])),
                recommended_choice=str(args.get("recommended_choice", "")),
                context=str(args.get("context", "")),
            )
            decision = decide_question(question)
            artifact_path, artifact = self._supervisor.record_question_decision(
                project_id=project_id,
                loop_id=loop_id,
                task_id=task_id,
                question={
                    "question_id": question.question_id,
                    "text": question.text,
                    "category": question.category,
                    "choices": list(question.choices),
                    "recommended_choice": question.recommended_choice,
                    "context": question.context,
                },
                decision=decision.to_dict(),
            )
        except (TypeError, ValueError) as exc:
            return json.dumps({"ok": False, "error": str(exc)})

        if artifact.get("status") == "answered" and artifact.get("human_answer"):
            return json.dumps({
                "ok": True,
                "action": "human_answer",
                "selected_choice": artifact["human_answer"],
                "artifact_path": artifact_path,
            })

        if decision.action == "auto_answer":
            return json.dumps({
                "ok": True,
                "action": decision.action,
                "selected_choice": decision.selected_choice,
                "reason": decision.reason,
                "artifact_path": artifact_path,
            })

        reason = (
            f"Autopilot decision {question.question_id} requires human input: "
            f"{question.text[:400]} Use /autopilot loop answer {loop_id} "
            f"{question.question_id} <answer>."
        )
        blocked = _as_payload(self._dispatch_tool(
            "kanban_block",
            {
                "task_id": task_id,
                "reason": reason,
                "kind": "needs_input",
                "board": board,
            },
        ))
        if blocked.get("ok") is not True:
            return json.dumps({
                "ok": False,
                "action": "needs_human",
                "error": "Human pause was persisted but Kanban blocking was not confirmed.",
                "artifact_path": artifact_path,
            })
        return json.dumps({
            "ok": True,
            "action": "needs_human",
            "reason": decision.reason,
            "artifact_path": artifact_path,
            "task_status": blocked.get("status", "blocked"),
        })
