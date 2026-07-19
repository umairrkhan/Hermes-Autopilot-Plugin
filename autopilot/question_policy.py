"""Deterministic question policy for autonomous development sessions.

The policy intentionally auto-answers only narrow, low-risk questions with an
explicit recommended/default choice. Anything that changes requirements,
security posture, permissions, accounts, data, deployment, or project scope is
escalated to the user.
"""

from __future__ import annotations

from dataclasses import dataclass

_HIGH_RISK_TERMS = (
    "auth", "authentication", "authorization", "security", "secret", "token",
    "credential", "password", "pricing", "payment", "billing", "business",
    "database", "migration", "deploy", "production", "account", "oauth",
    "consent", "permission", "push", "merge", "release", "delete", "drop",
    "external", "api key", "admin", "privileged", "architecture", "framework",
    "requirement", "privacy", "personal data", "dependency", "package manager",
)

_LOW_RISK_HINTS = (
    "recommended", "default", "existing", "style", "format", "lint", "test",
    "local", "reversible", "small", "convention", "project style",
)

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


def _joined(question: Question) -> str:
    return " ".join([question.text, question.context, " ".join(question.choices)]).lower()


def decide_question(question: Question) -> QuestionDecision:
    """Decide whether to auto-answer a question or pause for the user."""
    category = question.category.strip().lower()
    if category in HUMAN_REQUIRED_CATEGORIES:
        return QuestionDecision(
            question_id=question.question_id,
            action="needs_human",
            reason=f"High-risk typed category {category!r} requires human review.",
            risk_level="high",
        )
    if category != ROUTINE_LOW_RISK_CATEGORY:
        return QuestionDecision(
            question_id=question.question_id,
            action="needs_human",
            reason="Question category is missing or is not an approved low-risk category.",
            risk_level="medium",
        )
    text = _joined(question)
    if any(term in text for term in _HIGH_RISK_TERMS):
        return QuestionDecision(
            question_id=question.question_id,
            action="needs_human",
            reason="High-risk or requirement-changing question requires human review.",
            risk_level="high",
        )

    recommended = question.recommended_choice.strip()
    if not recommended:
        return QuestionDecision(
            question_id=question.question_id,
            action="needs_human",
            reason="No explicit recommended/default choice was supplied.",
            risk_level="medium",
        )

    if question.choices and recommended not in question.choices:
        return QuestionDecision(
            question_id=question.question_id,
            action="needs_human",
            reason="Recommended choice is not one of the presented choices.",
            risk_level="medium",
        )

    if not any(hint in text for hint in _LOW_RISK_HINTS):
        return QuestionDecision(
            question_id=question.question_id,
            action="needs_human",
            reason="Question is not clearly low-risk and reversible.",
            risk_level="medium",
        )

    return QuestionDecision(
        question_id=question.question_id,
        action="auto_answer",
        selected_choice=recommended,
        reason="Selected explicit recommended low-risk choice within session policy.",
        risk_level="low",
    )
