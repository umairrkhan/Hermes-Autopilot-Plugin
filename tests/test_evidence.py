"""Tests for trusted verifier evidence."""

from __future__ import annotations

import pytest

from autopilot.evidence import validate_verifier_evidence
from autopilot.verification import validate_verification_profile


def _profile(tmp_workspace):
    return validate_verification_profile(
        {
            "schema_version": 1,
            "project_id": "test-project-001",
            "workspace_root": str(tmp_workspace),
            "prerequisites": ["python3"],
            "checks": [
                {
                    "check_id": "unit",
                    "argv": ["python3", "-m", "pytest", "-q"],
                    "cwd": ".",
                    "timeout_seconds": 120,
                    "required_evidence": ["exit_code", "duration_seconds", "stdout_excerpt"],
                }
            ],
        },
        registered_project_id="test-project-001",
        registered_workspace_root=str(tmp_workspace),
    )


def _metadata():
    return {
        "autopilot_contract_version": 1,
        "role": "verifier",
        "brief_id": "brief-1",
        "verification_status": "passed",
        "review_status": "approved",
        "starting_revision": "abc123",
        "changed_files": ["lib/app.py", "tests/test_app.py"],
        "checks": [
            {
                "check_id": "unit",
                "argv": ["python3", "-m", "pytest", "-q"],
                "cwd": ".",
                "exit_code": 0,
                "duration_seconds": 2.5,
                "stdout_excerpt": "1 passed; token=tok_supersecret123",
                "stderr_excerpt": "",
            }
        ],
        "findings": [],
        "residual_risk": "none",
    }


def test_valid_verifier_evidence_is_provenanced_and_redacted(tmp_workspace):
    evidence = validate_verifier_evidence(
        _metadata(),
        project_id="test-project-001",
        loop_id="loop-1",
        brief_id="brief-1",
        board_slug="test-project-001",
        task_id="task-verify",
        run_id=9,
        starting_revision="abc123",
        profile=_profile(tmp_workspace),
    )

    payload = evidence.to_dict()
    assert evidence.accepted is True
    assert payload["provenance"]["task_id"] == "task-verify"
    assert payload["provenance"]["run_id"] == 9
    assert payload["checks"][0]["stdout_excerpt"] == "1 passed; token=[REDACTED]"


def test_missing_required_check_is_rejected(tmp_workspace):
    metadata = _metadata()
    metadata["checks"] = []

    with pytest.raises(ValueError, match="exactly one result"):
        validate_verifier_evidence(
            metadata,
            project_id="test-project-001",
            loop_id="loop-1",
            brief_id="brief-1",
            board_slug="test-project-001",
            task_id="task-verify",
            run_id=9,
            starting_revision="abc123",
            profile=_profile(tmp_workspace),
        )
