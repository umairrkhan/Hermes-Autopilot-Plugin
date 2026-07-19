"""Shared test fixtures for Project Autopilot tests.

Uses temporary directories and isolated HERMES_HOME for all tests.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Generator

import pytest

# Add the project root to sys.path so we can import autopilot
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))


@pytest.fixture
def tmp_hermes_home(tmp_path: Path) -> Path:
    """Provide an isolated HERMES_HOME directory."""
    home = tmp_path / ".hermes"
    home.mkdir()
    state_dir = home / "state" / "autopilot"
    state_dir.mkdir(parents=True)
    old = os.environ.get("HERMES_HOME")
    os.environ["HERMES_HOME"] = str(home)
    yield home
    if old is not None:
        os.environ["HERMES_HOME"] = old
    else:
        os.environ.pop("HERMES_HOME", None)


@pytest.fixture
def tmp_workspace(tmp_path: Path) -> Path:
    """Provide a temporary workspace directory that passes validation."""
    ws = tmp_path / "project-workspace"
    ws.mkdir()
    return ws


@pytest.fixture
def sample_registration(tmp_workspace: Path) -> dict[str, Any]:
    """Return a valid registration dict."""
    return {
        "project_id": "test-project-001",
        "workspace_root": str(tmp_workspace),
        "discussion_session_id": "sess-disc-abc12345",
        "development_session_id": "sess-dev-def67890",
        "display_title": "Test Project",
        "discussion_title": "Test Discussion",
        "development_title": "Test Development",
    }


@pytest.fixture
def sample_lease(tmp_workspace: Path) -> dict[str, Any]:
    """Return a valid lease dict (not expired)."""
    now = datetime.now(timezone.utc)
    expiry = now + timedelta(hours=1)
    return {
        "lease_id": "lease-test-001",
        "lease_version": 1,
        "project_id": "test-project-001",
        "scope": "Test simulation scope",
        "created_at": now.isoformat(),
        "expiry": expiry.isoformat(),
        "max_runtime_seconds": 3600,
        "max_loop_iterations": 3,
        "max_budget_cents": 100,
        "granted_capabilities": ["workspace.read", "git.read"],
        "workspace_root": str(tmp_workspace),
        "git_policy": "read-only",
        "dependency_policy": "deny",
        "local_service_policy": "deny",
        "database_policy": "read-only",
        "privileged_account_policy": "deny",
        "external_write_policy": "deny",
        "user_interaction_policy": "pause-for-human",
        "issuer": "test",
        "notes": "Test lease",
    }


@pytest.fixture
def expired_lease(tmp_workspace: Path) -> dict[str, Any]:
    """Return a valid but expired lease dict."""
    now = datetime.now(timezone.utc)
    past = now - timedelta(hours=1)
    created = now - timedelta(hours=2)
    return {
        "lease_id": "lease-expired-001",
        "lease_version": 1,
        "project_id": "test-project-001",
        "scope": "Expired test lease",
        "created_at": created.isoformat(),
        "expiry": past.isoformat(),
        "max_runtime_seconds": 3600,
        "max_loop_iterations": 1,
        "max_budget_cents": 0,
        "granted_capabilities": [],
        "workspace_root": str(tmp_workspace),
    }


@pytest.fixture
def configured_state(tmp_hermes_home: Path, sample_registration: dict) -> dict[str, Any]:
    """Return a state dict in CONFIGURED state."""
    from autopilot.constants import STATE_CONFIGURED
    return {
        "schema_version": 1,
        "state": STATE_CONFIGURED,
        "registration": sample_registration,
        "lease": None,
        "loop_iteration": 0,
        "max_loop_iterations": 1,
        "transition_history": [],
        "run_count": 0,
        "last_error": None,
        "kill_switch_active": False,
    }


@pytest.fixture
def lease_ready_state(
    tmp_hermes_home: Path,
    sample_registration: dict,
    sample_lease: dict,
) -> dict[str, Any]:
    """Return a state dict in LEASE_READY state."""
    from autopilot.constants import STATE_LEASE_READY
    return {
        "schema_version": 1,
        "state": STATE_LEASE_READY,
        "registration": sample_registration,
        "lease": sample_lease,
        "loop_iteration": 0,
        "max_loop_iterations": 1,
        "transition_history": [],
        "run_count": 0,
        "last_error": None,
        "kill_switch_active": False,
    }
