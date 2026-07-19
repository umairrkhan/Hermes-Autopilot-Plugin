"""Trusted, bounded verifier evidence for live Autopilot runs."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any

from .audit import redact_text
from .verification import VerificationProfile, verification_profile_digest

_MAX_FILES = 500
_MAX_FINDINGS = 100
_MAX_TEXT = 4000
_ALLOWED_EVIDENCE_FIELDS = {
    "exit_code",
    "duration_seconds",
    "stdout_excerpt",
    "stderr_excerpt",
}


@dataclass(frozen=True)
class VerificationEvidence:
    project_id: str
    loop_id: str
    brief_id: str
    board_slug: str
    task_id: str
    run_id: int
    starting_revision: str
    profile_digest: str
    verification_status: str
    review_status: str
    changed_files: tuple[str, ...]
    checks: tuple[dict[str, Any], ...]
    findings: tuple[str, ...]
    residual_risk: str

    @property
    def accepted(self) -> bool:
        return (
            self.verification_status == "passed"
            and self.review_status == "approved"
            and all(check["exit_code"] == 0 for check in self.checks)
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "provenance": {
                "project_id": self.project_id,
                "loop_id": self.loop_id,
                "brief_id": self.brief_id,
                "board_slug": self.board_slug,
                "task_id": self.task_id,
                "run_id": self.run_id,
                "starting_revision": self.starting_revision,
                "verification_profile_digest": self.profile_digest,
            },
            "verification_status": self.verification_status,
            "review_status": self.review_status,
            "accepted": self.accepted,
            "changed_files": list(self.changed_files),
            "checks": [dict(check) for check in self.checks],
            "findings": list(self.findings),
            "residual_risk": self.residual_risk,
        }


def _safe_relative_path(value: Any) -> str:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise ValueError("changed file path must be a non-empty string")
    path = PurePosixPath(value.replace("\\", "/"))
    if path.is_absolute() or ".." in path.parts:
        raise ValueError("changed file path escapes the project worktree")
    return str(path)


def _bounded_string(value: Any, field: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a string")
    return redact_text(value, max_length=_MAX_TEXT)


def validate_verifier_evidence(
    metadata: Any,
    *,
    project_id: str,
    loop_id: str,
    brief_id: str,
    board_slug: str,
    task_id: str,
    run_id: int,
    starting_revision: str,
    profile: VerificationProfile,
) -> VerificationEvidence:
    """Validate one Kanban verifier run against the dispatched contract."""

    if not isinstance(metadata, dict):
        raise ValueError("verifier metadata must be an object")
    if metadata.get("autopilot_contract_version") != 1:
        raise ValueError("unsupported verifier contract version")
    if metadata.get("role") != "verifier":
        raise ValueError("evidence role must be verifier")
    if metadata.get("brief_id") != brief_id:
        raise ValueError("evidence brief_id does not match the dispatched brief")
    if metadata.get("starting_revision") != starting_revision:
        raise ValueError("evidence starting revision does not match the dispatch baseline")
    if profile.project_id != project_id:
        raise ValueError("verification profile project does not match result project")

    verification_status = metadata.get("verification_status")
    review_status = metadata.get("review_status")
    if verification_status not in {"passed", "failed"}:
        raise ValueError("verification_status must be passed or failed")
    if review_status not in {"approved", "rejected"}:
        raise ValueError("review_status must be approved or rejected")

    raw_files = metadata.get("changed_files")
    if not isinstance(raw_files, list) or len(raw_files) > _MAX_FILES:
        raise ValueError("changed_files must be a bounded list")
    changed_files = tuple(_safe_relative_path(item) for item in raw_files)
    if len(changed_files) != len(set(changed_files)):
        raise ValueError("changed_files contains duplicates")

    raw_checks = metadata.get("checks")
    if not isinstance(raw_checks, list):
        raise ValueError("checks must be a list")
    check_results: dict[str, dict[str, Any]] = {}
    for raw in raw_checks:
        if not isinstance(raw, dict):
            raise ValueError("each check result must be an object")
        check_id = raw.get("check_id")
        if not isinstance(check_id, str) or check_id in check_results:
            raise ValueError("check results must have unique string check_id values")
        check_results[check_id] = raw

    expected_ids = {check.check_id for check in profile.checks}
    if set(check_results) != expected_ids:
        raise ValueError("every configured check must have exactly one result")

    normalized_checks: list[dict[str, Any]] = []
    for configured in profile.checks:
        raw = check_results[configured.check_id]
        if raw.get("argv") != list(configured.argv):
            raise ValueError(f"check {configured.check_id!r} argv does not match the approved profile")
        if raw.get("cwd") != configured.cwd:
            raise ValueError(f"check {configured.check_id!r} cwd does not match the approved profile")
        exit_code = raw.get("exit_code")
        if not isinstance(exit_code, int) or isinstance(exit_code, bool):
            raise ValueError(f"check {configured.check_id!r} exit_code must be an integer")
        duration = raw.get("duration_seconds")
        if not isinstance(duration, (int, float)) or isinstance(duration, bool) or duration < 0:
            raise ValueError(f"check {configured.check_id!r} duration_seconds must be non-negative")
        unknown_requirements = set(configured.required_evidence) - _ALLOWED_EVIDENCE_FIELDS
        if unknown_requirements:
            raise ValueError(f"check {configured.check_id!r} requests unsupported evidence")
        for field in configured.required_evidence:
            if field not in raw:
                raise ValueError(f"check {configured.check_id!r} is missing required evidence {field!r}")
        normalized_checks.append({
            "check_id": configured.check_id,
            "argv": list(configured.argv),
            "cwd": configured.cwd,
            "exit_code": exit_code,
            "duration_seconds": float(duration),
            "stdout_excerpt": _bounded_string(raw.get("stdout_excerpt", ""), "stdout_excerpt"),
            "stderr_excerpt": _bounded_string(raw.get("stderr_excerpt", ""), "stderr_excerpt"),
        })

    if verification_status == "passed" and any(
        check["exit_code"] != 0 for check in normalized_checks
    ):
        raise ValueError("passed verification cannot contain a failed check")

    raw_findings = metadata.get("findings", [])
    if not isinstance(raw_findings, list) or len(raw_findings) > _MAX_FINDINGS:
        raise ValueError("findings must be a bounded list")
    findings = tuple(_bounded_string(item, "finding") for item in raw_findings)
    residual_risk = _bounded_string(metadata.get("residual_risk", ""), "residual_risk")

    if not isinstance(run_id, int) or isinstance(run_id, bool) or run_id < 1:
        raise ValueError("run_id must identify a durable Kanban run")

    return VerificationEvidence(
        project_id=project_id,
        loop_id=loop_id,
        brief_id=brief_id,
        board_slug=board_slug,
        task_id=task_id,
        run_id=run_id,
        starting_revision=starting_revision,
        profile_digest=verification_profile_digest(profile),
        verification_status=verification_status,
        review_status=review_status,
        changed_files=changed_files,
        checks=tuple(normalized_checks),
        findings=findings,
        residual_risk=residual_risk,
    )
