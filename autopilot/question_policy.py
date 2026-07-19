"""Deterministic question policy for autonomous development sessions.

The user has granted blanket upfront permission: "select the recommended
option if any question comes." The policy honours that — no category, term,
or risk heuristics gate auto-answering. The only hard stop is when no
suitable choice exists at all.
"""

from __future__ import annotations

from dataclasses import dataclass

ROUTINE_LOW_RISK_CATEGORY = "routine_low_risk"
HUMAN_REQUIRED_CATEGORIES = frozenset({
    "requirements",
    "business_rule",
    "architecture",
    "framework",
    "security",
    "privacy",
    "personal_data",
    "dependency",
    "credential",
    "account_consent",
    "database_migration",
    "destructive",
    "external_write",
    "deployment",
})


@dataclass(frozen=True)
class Question:
    """A Development-agent question captured by the supervisor."""

    question_id: str
    text: str
    category: str = ""
    choices: tuple[str, ...] = ()
    recommended_choice: str = ""
    context: str = ""


@dataclass(frozen=True)
class QuestionDecision:
    """Policy decision for a Development-agent question."""

    question_id: str
    action: str  # auto_answer | needs_human
    selected_choice: str = ""
    reason: str = ""
    risk_level: str = "low"

    def to_dict(self) -> dict[str, str]:
        return {
            "question_id": self.question_id,
            "action": self.action,
            "selected_choice": self.selected_choice,
            "reason": self.reason,
            "risk_level": self.risk_level,
        }


def decide_question(question: Question) -> QuestionDecision:
    """Auto-answer every question with the recommended choice.

    The user has granted blanket upfront permission: "select the recommended
    option if any question comes." We honour it here — no category, term, or
    risk heuristics gate auto-answering. The only hard stop is when no
    suitable choice exists at all.
    """
    recommended = question.recommended_choice.strip()
    if not recommended and question.choices:
        recommended = question.choices[0].strip()
    if not recommended:
        return QuestionDecision(
            question_id=question.question_id,
            action="needs_human",
            reason="No explicit recommended/default choice was supplied and there are no choices to select from.",
            risk_level="medium",
        )
    if question.choices and recommended not in question.choices:
        return QuestionDecision(
            question_id=question.question_id,
            action="needs_human",
            reason="Recommended choice is not one of the presented choices.",
            risk_level="medium",
        )

    return QuestionDecision(
        question_id=question.question_id,
        action="auto_answer",
        selected_choice=recommended,
        reason="Auto-selected recommended/default choice per user's blanket autonomy approval.",
        risk_level="low",
    )
