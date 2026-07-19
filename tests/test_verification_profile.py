"""Tests for project-scoped verification configuration and execution."""

from __future__ import annotations

from autopilot.storage import save_project_state
from autopilot.verification import (
    load_verification_profile,
    save_verification_profile,
    validate_verification_profile,
)


def test_valid_profile_is_normalized_and_bound_to_registered_workspace(tmp_workspace):
    profile = validate_verification_profile(
        {
            "schema_version": 1,
            "project_id": "test-project-001",
            "workspace_root": str(tmp_workspace),
            "prerequisites": ["python3"],
            "checks": [
                {
                    "check_id": "unit",
                    "argv": ["python3", "-m", "pytest", "tests", "-q"],
                    "cwd": ".",
                    "timeout_seconds": 120,
                    "required_evidence": ["exit_code", "stdout"],
                }
            ],
        },
        registered_project_id="test-project-001",
        registered_workspace_root=str(tmp_workspace),
    )

    assert profile.project_id == "test-project-001"
    assert profile.workspace_root == str(tmp_workspace.resolve())
    assert profile.checks[0].argv == ("python3", "-m", "pytest", "tests", "-q")
    assert profile.checks[0].cwd == "."
    assert profile.checks[0].timeout_seconds == 120


def test_profile_round_trip_is_project_scoped(
    tmp_hermes_home,
    tmp_workspace,
    configured_state,
):
    save_project_state("test-project-001", configured_state)
    profile = validate_verification_profile(
        {
            "schema_version": 1,
            "project_id": "test-project-001",
            "workspace_root": str(tmp_workspace),
            "prerequisites": [],
            "checks": [
                {
                    "check_id": "unit",
                    "argv": ["python3", "-m", "pytest", "-q"],
                    "cwd": ".",
                    "timeout_seconds": 60,
                    "required_evidence": ["exit_code"],
                }
            ],
        },
        registered_project_id="test-project-001",
        registered_workspace_root=str(tmp_workspace),
    )

    save_verification_profile(profile)

    loaded = load_verification_profile("test-project-001")
    assert loaded == profile
    assert load_verification_profile("other-project") is None


def test_verify_configure_command_persists_active_project_profile(
    tmp_hermes_home,
    tmp_workspace,
    sample_registration,
):
    from autopilot.commands import handle_autopilot_command

    assert "Registered" in str(
        handle_autopilot_command(f"register {__import__('json').dumps(sample_registration)}")
    )
    raw_profile = {
        "schema_version": 1,
        "project_id": sample_registration["project_id"],
        "workspace_root": str(tmp_workspace),
        "prerequisites": ["python3"],
        "checks": [
            {
                "check_id": "unit",
                "argv": ["python3", "-m", "pytest", "-q"],
                "cwd": ".",
                "timeout_seconds": 60,
                "required_evidence": ["exit_code", "stdout"],
            }
        ],
    }

    configured = str(
        handle_autopilot_command(
            f"verify configure {__import__('json').dumps(raw_profile)}"
        )
    )
    shown = str(handle_autopilot_command("verify show"))

    assert "Verification profile configured" in configured
    assert '"check_id": "unit"' in shown
    assert load_verification_profile(sample_registration["project_id"]) is not None


def test_profile_rejects_duplicate_check_ids(tmp_workspace):
    import pytest

    raw = {
        "schema_version": 1,
        "project_id": "test-project-001",
        "workspace_root": str(tmp_workspace),
        "prerequisites": [],
        "checks": [
            {
                "check_id": "unit",
                "argv": ["python3", "-m", "pytest"],
                "cwd": ".",
                "timeout_seconds": 30,
                "required_evidence": ["exit_code"],
            },
            {
                "check_id": "unit",
                "argv": ["python3", "-m", "pytest", "tests"],
                "cwd": ".",
                "timeout_seconds": 30,
                "required_evidence": ["exit_code"],
            },
        ],
    }

    with pytest.raises(ValueError, match="duplicate check_id"):
        validate_verification_profile(
            raw,
            registered_project_id="test-project-001",
            registered_workspace_root=str(tmp_workspace),
        )


def test_profile_accepts_bounded_development_commands_and_rejects_shell_wrappers(tmp_workspace):
    import pytest

    base = {
        "schema_version": 1,
        "project_id": "test-project-001",
        "workspace_root": str(tmp_workspace),
        "prerequisites": [],
        "checks": [{
            "check_id": "unit",
            "argv": ["python3", "-m", "pytest"],
            "cwd": ".",
            "timeout_seconds": 30,
            "required_evidence": ["exit_code"],
        }],
        "development_commands": [{
            "command_id": "format",
            "argv": ["ruff", "format", "."],
            "cwd": ".",
            "timeout_seconds": 60,
        }],
    }
    profile = validate_verification_profile(
        base,
        registered_project_id="test-project-001",
        registered_workspace_root=str(tmp_workspace),
    )
    assert profile.development_commands[0].argv == ("ruff", "format", ".")

    unsafe = dict(base)
    unsafe["development_commands"] = [{
        "command_id": "shell",
        "argv": ["bash", "-c", "ruff format ."],
        "cwd": ".",
        "timeout_seconds": 60,
    }]
    with pytest.raises(ValueError, match="shell or command wrapper"):
        validate_verification_profile(
            unsafe,
            registered_project_id="test-project-001",
            registered_workspace_root=str(tmp_workspace),
        )
