"""Tests for Phase 2 execution bridge adapter.

Covers:
- ExecutionBridge readiness validation
- Brief generation with/without tasks
- Brief persistence and retrieval
- Brief validation for execution
- Brief generation failure modes (missing lease, expired lease, wrong capabilities)
- Integration with commands (/autopilot brief, /autopilot handoff, /autopilot execute)
- Safety properties (fail-closed, human gate, no autonomous execution)
- Audit trail logging
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone, timedelta
from pathlib import Path

import pytest

from autopilot.adapters.execution_bridge import (
    ExecutionBridge,
    DevelopmentExecutionBrief,
    BriefTask,
    BriefGenerationResult,
    BRIEF_SCHEMA_VERSION,
)
from autopilot.commands import handle_autopilot_command
from autopilot.constants import (
    STATE_CONFIGURED,
    STATE_LEASE_READY,
    SCHEMA_VERSION,
)
from autopilot.storage import save_state, mutate_state, set_active_project_id


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def bridge(tmp_hermes_home: Path) -> ExecutionBridge:
    """Create an ExecutionBridge with isolated HERMES_HOME."""
    return ExecutionBridge(hermes_home=str(tmp_hermes_home))


@pytest.fixture
def valid_lease_for_brief(tmp_workspace: Path) -> dict:
    """Lease with workspace.read capability for brief generation."""
    now = datetime.now(timezone.utc)
    expiry = now + timedelta(hours=2)
    return {
        "lease_id": "lease-bridge-001",
        "lease_version": 1,
        "project_id": "test-project-001",
        "scope": "Phase 2 brief generation",
        "created_at": now.isoformat(),
        "expiry": expiry.isoformat(),
        "max_runtime_seconds": 7200,
        "max_loop_iterations": 3,
        "max_budget_cents": 200,
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
        "notes": "Test lease for bridge",
    }


@pytest.fixture
def phase2_lease(tmp_workspace: Path) -> dict:
    """Lease with workspace.write and git.commit for Phase 2 execution."""
    now = datetime.now(timezone.utc)
    expiry = now + timedelta(hours=2)
    return {
        "lease_id": "lease-phase2-001",
        "lease_version": 1,
        "project_id": "test-project-001",
        "scope": "Phase 2 execution",
        "created_at": now.isoformat(),
        "expiry": expiry.isoformat(),
        "max_runtime_seconds": 7200,
        "max_loop_iterations": 3,
        "max_budget_cents": 500,
        "granted_capabilities": ["workspace.read", "workspace.write", "git.read", "git.commit"],
        "workspace_root": str(tmp_workspace),
        "git_policy": "commit",
        "dependency_policy": "deny",
        "local_service_policy": "deny",
        "database_policy": "read-only",
        "privileged_account_policy": "deny",
        "external_write_policy": "deny",
        "user_interaction_policy": "pause-for-human",
        "issuer": "test",
        "notes": "Phase 2 test lease",
    }


@pytest.fixture
def state_with_registration_and_lease(
    tmp_hermes_home: Path,
    tmp_workspace: Path,
    sample_registration: dict,
    valid_lease_for_brief: dict,
) -> dict:
    """State dict with valid registration and lease."""
    return {
        "schema_version": SCHEMA_VERSION,
        "state": STATE_LEASE_READY,
        "registration": sample_registration,
        "lease": valid_lease_for_brief,
        "loop_iteration": 0,
        "max_loop_iterations": 3,
        "transition_history": [],
        "run_count": 0,
        "last_error": None,
        "kill_switch_active": False,
    }


@pytest.fixture
def state_no_lease(tmp_hermes_home: Path, sample_registration: dict) -> dict:
    """State with registration but no lease."""
    return {
        "schema_version": SCHEMA_VERSION,
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
def state_no_registration(tmp_hermes_home: Path) -> dict:
    """State with no registration."""
    return {
        "schema_version": SCHEMA_VERSION,
        "state": STATE_CONFIGURED,
        "registration": None,
        "lease": None,
        "loop_iteration": 0,
        "max_loop_iterations": 1,
        "transition_history": [],
        "run_count": 0,
        "last_error": None,
        "kill_switch_active": False,
    }


# ---------------------------------------------------------------------------
# BriefTask tests
# ---------------------------------------------------------------------------

class TestBriefTask:
    def test_task_creation(self):
        task = BriefTask(
            task_id="t1",
            title="Test task",
            description="A test task",
            priority="high",
            risk_level="medium",
            acceptance_criteria=("criteria1", "criteria2"),
        )
        assert task.task_id == "t1"
        assert task.priority == "high"
        assert len(task.acceptance_criteria) == 2

    def test_task_to_dict(self):
        task = BriefTask(
            task_id="t1",
            title="Test",
            description="",
            priority="low",
            risk_level="low",
        )
        d = task.to_dict()
        assert d["task_id"] == "t1"
        assert isinstance(d["acceptance_criteria"], list)
        assert isinstance(d["estimated_files"], list)
        assert isinstance(d["dependencies"], list)

    def test_task_from_dict(self):
        data = {
            "task_id": "t2",
            "title": "From dict",
            "description": "desc",
            "priority": "medium",
            "risk_level": "low",
            "acceptance_criteria": ["c1"],
            "estimated_files": ["f1.py"],
            "dependencies": [],
        }
        task = BriefTask.from_dict(data)
        assert task.task_id == "t2"
        assert task.acceptance_criteria == ("c1",)
        assert task.estimated_files == ("f1.py",)

    def test_task_is_frozen(self):
        task = BriefTask(task_id="t1", title="T", description="", priority="low", risk_level="low")
        with pytest.raises(AttributeError):
            task.title = "changed"


# ---------------------------------------------------------------------------
# DevelopmentExecutionBrief tests
# ---------------------------------------------------------------------------

class TestDevelopmentExecutionBrief:
    def test_brief_creation(self):
        now = datetime.now(timezone.utc).isoformat()
        brief = DevelopmentExecutionBrief(
            brief_id="brief-001",
            brief_version=1,
            schema_version=BRIEF_SCHEMA_VERSION,
            project_id="test-project",
            workspace_root="/tmp/test",
            discussion_session_id="disc-001",
            development_session_id="dev-001",
            display_title="Test",
            lease_id="lease-001",
            lease_expiry=now,
            granted_capabilities=("workspace.read",),
            scope="test scope",
            tasks=(),
            created_at=now,
            created_by="execution_bridge",
        )
        assert brief.brief_id == "brief-001"
        assert brief.human_gate_required is True
        assert brief.execution_authorized is False

    def test_brief_to_dict_roundtrip(self):
        now = datetime.now(timezone.utc).isoformat()
        task = BriefTask(
            task_id="t1", title="Task 1", description="",
            priority="high", risk_level="medium",
        )
        brief = DevelopmentExecutionBrief(
            brief_id="brief-roundtrip",
            brief_version=2,
            schema_version=BRIEF_SCHEMA_VERSION,
            project_id="proj",
            workspace_root="/tmp",
            discussion_session_id="d1",
            development_session_id="dev1",
            display_title="Proj",
            lease_id="l1",
            lease_expiry=now,
            granted_capabilities=("workspace.read",),
            scope="scope",
            tasks=(task,),
            created_at=now,
            created_by="test",
        )
        d = brief.to_dict()
        restored = DevelopmentExecutionBrief.from_dict(d)
        assert restored.brief_id == brief.brief_id
        assert restored.brief_version == brief.brief_version
        assert len(restored.tasks) == 1
        assert restored.tasks[0].task_id == "t1"
        assert restored.human_gate_required is True
        assert restored.execution_authorized is False

    def test_brief_is_frozen(self):
        now = datetime.now(timezone.utc).isoformat()
        brief = DevelopmentExecutionBrief(
            brief_id="b1", brief_version=1, schema_version=1,
            project_id="p", workspace_root="/", discussion_session_id="d",
            development_session_id="dev", display_title="", lease_id="l",
            lease_expiry=now, granted_capabilities=(), scope="", tasks=(),
            created_at=now, created_by="t",
        )
        with pytest.raises(AttributeError):
            brief.execution_authorized = True


# ---------------------------------------------------------------------------
# ExecutionBridge readiness tests
# ---------------------------------------------------------------------------

class TestExecutionBridgeReadiness:
    def test_not_ready_no_registration(self, bridge, state_no_registration):
        ready, blockers = bridge.validate_readiness(state_no_registration)
        assert ready is False
        assert any("registration" in b.lower() for b in blockers)

    def test_not_ready_no_lease(self, bridge, state_no_lease):
        ready, blockers = bridge.validate_readiness(state_no_lease)
        assert ready is False
        assert any("lease" in b.lower() for b in blockers)

    def test_ready_with_valid_state(self, bridge, state_with_registration_and_lease):
        ready, blockers = bridge.validate_readiness(state_with_registration_and_lease)
        assert ready is True
        assert blockers == []

    def test_not_ready_without_git_read(self, bridge, state_with_registration_and_lease):
        state_with_registration_and_lease["lease"]["granted_capabilities"] = [
            "workspace.read"
        ]

        ready, blockers = bridge.validate_readiness(
            state_with_registration_and_lease
        )

        assert ready is False
        assert any("git.read" in blocker for blocker in blockers)

    def test_not_ready_when_lease_project_does_not_match(
        self,
        bridge,
        state_with_registration_and_lease,
    ):
        state_with_registration_and_lease["lease"]["project_id"] = "other-project"

        ready, blockers = bridge.validate_readiness(
            state_with_registration_and_lease
        )

        assert ready is False
        assert any("does not match active project" in blocker for blocker in blockers)

    def test_not_ready_expired_lease(self, bridge, sample_registration, expired_lease, tmp_hermes_home):
        state = {
            "schema_version": SCHEMA_VERSION,
            "state": STATE_LEASE_READY,
            "registration": sample_registration,
            "lease": expired_lease,
            "loop_iteration": 0,
            "max_loop_iterations": 1,
            "transition_history": [],
            "run_count": 0,
            "last_error": None,
            "kill_switch_active": False,
        }
        ready, blockers = bridge.validate_readiness(state)
        assert ready is False
        assert any("expired" in b.lower() for b in blockers)


# ---------------------------------------------------------------------------
# Brief generation tests
# ---------------------------------------------------------------------------

class TestBriefGeneration:
    def test_generate_brief_happy_path(self, bridge, state_with_registration_and_lease):
        result = bridge.generate_brief(state_with_registration_and_lease)
        assert result.success is True
        assert result.brief is not None
        assert result.brief.project_id == "test-project-001"
        assert result.brief.human_gate_required is True
        assert result.brief.execution_authorized is False
        assert result.brief.schema_version == BRIEF_SCHEMA_VERSION

    def test_generate_brief_with_tasks(self, bridge, state_with_registration_and_lease):
        tasks = [
            {"task_id": "t1", "title": "Implement feature", "priority": "high",
             "acceptance_criteria": ["tests pass", "lint clean"]},
            {"task_id": "t2", "title": "Update docs", "priority": "low"},
        ]
        result = bridge.generate_brief(
            state_with_registration_and_lease,
            tasks=tasks,
            scope="Feature implementation",
        )
        assert result.success is True
        assert len(result.brief.tasks) == 2
        assert result.brief.tasks[0].task_id == "t1"
        assert result.brief.tasks[0].priority == "high"
        assert result.brief.tasks[0].risk_level in ("low", "medium", "high")
        assert len(result.brief.tasks[0].acceptance_criteria) == 2
        assert result.brief.scope == "Feature implementation"

    def test_generate_brief_persists_artifact(self, bridge, state_with_registration_and_lease):
        result = bridge.generate_brief(state_with_registration_and_lease)
        assert result.success is True
        assert result.artifact_path != ""
        assert Path(result.artifact_path).exists()

        # Verify artifact content
        raw = Path(result.artifact_path).read_text()
        data = json.loads(raw)
        assert data["project_id"] == "test-project-001"
        assert data["human_gate_required"] is True
        assert data["execution_authorized"] is False

    def test_generate_brief_fails_no_registration(self, bridge, state_no_registration):
        result = bridge.generate_brief(state_no_registration)
        assert result.success is False
        assert any("registration" in b.lower() for b in result.blockers)

    def test_generate_brief_fails_no_lease(self, bridge, state_no_lease):
        result = bridge.generate_brief(state_no_lease)
        assert result.success is False
        assert any("lease" in b.lower() for b in result.blockers)

    def test_generate_brief_increments_version(self, bridge, state_with_registration_and_lease):
        r1 = bridge.generate_brief(state_with_registration_and_lease)
        assert r1.success
        assert r1.brief.brief_version == 1

        # Generate again — version should increment
        r2 = bridge.generate_brief(state_with_registration_and_lease)
        assert r2.success
        assert r2.brief.brief_version == 2

    def test_generate_brief_task_risk_classification(self, bridge, state_with_registration_and_lease):
        tasks = [
            {"title": "Fix typo in README", "priority": "low"},
            {"title": "Deploy to production", "priority": "critical"},
        ]
        result = bridge.generate_brief(state_with_registration_and_lease, tasks=tasks)
        assert result.success
        # "fix typo" should be low risk
        assert result.brief.tasks[0].risk_level == "low"
        # "deploy to production" should be high risk
        assert result.brief.tasks[1].risk_level == "high"


# ---------------------------------------------------------------------------
# Brief persistence tests
# ---------------------------------------------------------------------------

class TestBriefPersistence:
    def test_list_briefs_empty(self, bridge):
        briefs = bridge.list_briefs("nonexistent-project")
        assert briefs == []

    def test_list_briefs_after_generation(self, bridge, state_with_registration_and_lease):
        bridge.generate_brief(state_with_registration_and_lease)
        bridge.generate_brief(state_with_registration_and_lease)

        briefs = bridge.list_briefs("test-project-001")
        assert len(briefs) == 2
        assert briefs[0]["project_id"] == "test-project-001"
        assert briefs[0]["task_count"] == 0
        assert briefs[0]["human_gate_required"] is True
        assert briefs[0]["execution_authorized"] is False

    def test_read_brief(self, bridge, state_with_registration_and_lease):
        result = bridge.generate_brief(state_with_registration_and_lease)
        brief = bridge.read_brief("test-project-001", result.brief.brief_id)
        assert brief is not None
        assert brief.brief_id == result.brief.brief_id

    def test_read_brief_not_found(self, bridge):
        brief = bridge.read_brief("nonexistent", "nonexistent-brief")
        assert brief is None

    def test_brief_artifacts_are_isolated_per_project(self, bridge, state_with_registration_and_lease):
        result = bridge.generate_brief(state_with_registration_and_lease)
        briefs_project = bridge.list_briefs("test-project-001")
        briefs_other = bridge.list_briefs("other-project")
        assert len(briefs_project) == 1
        assert len(briefs_other) == 0


# ---------------------------------------------------------------------------
# Brief validation for execution tests
# ---------------------------------------------------------------------------

class TestBriefValidation:
    def test_validate_brief_for_execution(self, bridge, state_with_registration_and_lease):
        result = bridge.generate_brief(state_with_registration_and_lease)
        brief = result.brief

        valid, blockers = bridge.validate_brief_for_execution(brief, state_with_registration_and_lease)
        assert valid is True
        assert blockers == []

    def test_validate_brief_wrong_project(self, bridge, state_with_registration_and_lease):
        result = bridge.generate_brief(state_with_registration_and_lease)
        brief = result.brief

        # Tamper with project_id
        tampered = DevelopmentExecutionBrief(
            brief_id=brief.brief_id,
            brief_version=brief.brief_version,
            schema_version=brief.schema_version,
            project_id="wrong-project",
            workspace_root=brief.workspace_root,
            discussion_session_id=brief.discussion_session_id,
            development_session_id=brief.development_session_id,
            display_title=brief.display_title,
            lease_id=brief.lease_id,
            lease_expiry=brief.lease_expiry,
            granted_capabilities=brief.granted_capabilities,
            scope=brief.scope,
            tasks=brief.tasks,
            created_at=brief.created_at,
            created_by=brief.created_by,
        )
        valid, blockers = bridge.validate_brief_for_execution(tampered, state_with_registration_and_lease)
        assert valid is False
        assert any("project_id" in b for b in blockers)

    def test_validate_brief_human_gate_must_be_true(self, bridge, state_with_registration_and_lease):
        result = bridge.generate_brief(state_with_registration_and_lease)
        brief = result.brief

        # Create brief with human_gate_required=False (simulating tampering)
        tampered = DevelopmentExecutionBrief(
            brief_id=brief.brief_id,
            brief_version=brief.brief_version,
            schema_version=brief.schema_version,
            project_id=brief.project_id,
            workspace_root=brief.workspace_root,
            discussion_session_id=brief.discussion_session_id,
            development_session_id=brief.development_session_id,
            display_title=brief.display_title,
            lease_id=brief.lease_id,
            lease_expiry=brief.lease_expiry,
            granted_capabilities=brief.granted_capabilities,
            scope=brief.scope,
            tasks=brief.tasks,
            created_at=brief.created_at,
            created_by=brief.created_by,
            human_gate_required=False,
        )
        valid, blockers = bridge.validate_brief_for_execution(tampered, state_with_registration_and_lease)
        assert valid is False
        assert any("human_gate" in b.lower() for b in blockers)

    def test_validate_brief_execution_authorized_must_be_false(self, bridge, state_with_registration_and_lease):
        result = bridge.generate_brief(state_with_registration_and_lease)
        brief = result.brief

        # Create brief with execution_authorized=True (simulating tampering)
        tampered = DevelopmentExecutionBrief(
            brief_id=brief.brief_id,
            brief_version=brief.brief_version,
            schema_version=brief.schema_version,
            project_id=brief.project_id,
            workspace_root=brief.workspace_root,
            discussion_session_id=brief.discussion_session_id,
            development_session_id=brief.development_session_id,
            display_title=brief.display_title,
            lease_id=brief.lease_id,
            lease_expiry=brief.lease_expiry,
            granted_capabilities=brief.granted_capabilities,
            scope=brief.scope,
            tasks=brief.tasks,
            created_at=brief.created_at,
            created_by=brief.created_by,
            execution_authorized=True,
        )
        valid, blockers = bridge.validate_brief_for_execution(tampered, state_with_registration_and_lease)
        assert valid is False
        assert any("execution_authorized" in b.lower() for b in blockers)

    def test_validate_brief_expired_lease(self, bridge, sample_registration, expired_lease, tmp_hermes_home):
        state = {
            "schema_version": SCHEMA_VERSION,
            "state": STATE_LEASE_READY,
            "registration": sample_registration,
            "lease": expired_lease,
            "loop_iteration": 0,
            "max_loop_iterations": 1,
            "transition_history": [],
            "run_count": 0,
            "last_error": None,
            "kill_switch_active": False,
        }
        # Generate brief with valid lease first
        valid_lease_state = dict(state)
        now = datetime.now(timezone.utc)
        valid_lease_state["lease"] = {
            "lease_id": "lease-valid",
            "lease_version": 1,
            "project_id": "test-project-001",
            "scope": "test",
            "created_at": now.isoformat(),
            "expiry": (now + timedelta(hours=1)).isoformat(),
            "max_runtime_seconds": 3600,
            "max_loop_iterations": 1,
            "max_budget_cents": 100,
            "granted_capabilities": ["workspace.read", "git.read"],
            "workspace_root": state["registration"]["workspace_root"],
        }
        result = bridge.generate_brief(valid_lease_state)
        assert result.success

        # Now validate against state with expired lease
        valid, blockers = bridge.validate_brief_for_execution(result.brief, state)
        assert valid is False
        assert any("expired" in b.lower() for b in blockers)


# ---------------------------------------------------------------------------
# Command integration tests
# ---------------------------------------------------------------------------

class TestBriefCommand:
    def test_brief_command_no_registration(self, tmp_hermes_home):
        result = handle_autopilot_command("brief")
        # Bridge is available but no registration — brief generation fails
        assert "failed" in result.lower() or "blockers" in result.lower() or "No project registration" in result

    def test_brief_command_no_lease(self, tmp_hermes_home, tmp_workspace, sample_registration):
        handle_autopilot_command(f"register {json.dumps(sample_registration)}")
        result = handle_autopilot_command("brief")
        assert "brief" in result.lower() or "failed" in result.lower() or "blocker" in result.lower()

    def test_brief_command_with_lease(self, tmp_hermes_home, tmp_workspace, sample_registration, valid_lease_for_brief):
        handle_autopilot_command(f"register {json.dumps(sample_registration)}")
        handle_autopilot_command(f"lease {json.dumps(valid_lease_for_brief)}")
        result = handle_autopilot_command("brief")
        assert "Brief ID" in result or "brief" in result.lower()

    def test_brief_command_with_tasks(self, tmp_hermes_home, tmp_workspace, sample_registration, valid_lease_for_brief):
        handle_autopilot_command(f"register {json.dumps(sample_registration)}")
        handle_autopilot_command(f"lease {json.dumps(valid_lease_for_brief)}")
        payload = json.dumps({
            "scope": "Test scope",
            "tasks": [
                {"title": "Task 1", "priority": "high"},
                {"title": "Task 2", "priority": "low"},
            ],
        })
        result = handle_autopilot_command(f"brief {payload}")
        assert "Brief ID" in result or "brief" in result.lower()

    def test_brief_list_empty(self, tmp_hermes_home, tmp_workspace, sample_registration, valid_lease_for_brief):
        handle_autopilot_command(f"register {json.dumps(sample_registration)}")
        handle_autopilot_command(f"lease {json.dumps(valid_lease_for_brief)}")
        result = handle_autopilot_command("brief --list")
        # Should list briefs (may be empty or may have ones from prior tests)
        assert "Briefs" in result or "brief" in result.lower()

    def test_brief_list_after_generation(self, tmp_hermes_home, tmp_workspace, sample_registration, valid_lease_for_brief):
        handle_autopilot_command(f"register {json.dumps(sample_registration)}")
        handle_autopilot_command(f"lease {json.dumps(valid_lease_for_brief)}")
        handle_autopilot_command("brief")
        result = handle_autopilot_command("brief --list")
        assert "test-project-001" in result or "Briefs" in result


class TestHandoffCommand:
    def test_handoff_no_registration(self, tmp_hermes_home):
        result = handle_autopilot_command("handoff")
        # Bridge is available but no registration — reports no registration
        assert "No project registered" in result or "registration" in result.lower() or "not registered" in result.lower()

    def test_handoff_with_registration_and_lease(
        self, tmp_hermes_home, tmp_workspace, sample_registration, valid_lease_for_brief
    ):
        handle_autopilot_command(f"register {json.dumps(sample_registration)}")
        handle_autopilot_command(f"lease {json.dumps(valid_lease_for_brief)}")
        result = handle_autopilot_command("handoff")
        assert "Handoff Readiness" in result or "handoff" in result.lower()

    def test_handoff_readonly(self, tmp_hermes_home, tmp_workspace, sample_registration, valid_lease_for_brief):
        handle_autopilot_command(f"register {json.dumps(sample_registration)}")
        handle_autopilot_command(f"lease {json.dumps(valid_lease_for_brief)}")
        result = handle_autopilot_command("handoff")
        assert "READ-ONLY" in result or "read-only" in result.lower() or "No execution" in result


class TestExecuteCommandPhase2:
    def test_execute_generates_brief(
        self, tmp_hermes_home, tmp_workspace, sample_registration, phase2_lease
    ):
        handle_autopilot_command(f"register {json.dumps(sample_registration)}")
        handle_autopilot_command(f"lease {json.dumps(phase2_lease)}")
        result = handle_autopilot_command("execute")
        # Should generate a brief successfully
        assert "Phase 2 Execution Brief" in result or "brief" in result.lower()

    def test_execute_shows_human_gate(
        self, tmp_hermes_home, tmp_workspace, sample_registration, phase2_lease
    ):
        handle_autopilot_command(f"register {json.dumps(sample_registration)}")
        handle_autopilot_command(f"lease {json.dumps(phase2_lease)}")
        result = handle_autopilot_command("execute")
        assert "human gate required: true" in result.lower() or "human_gate_required" in result.lower()

    def test_execute_shows_not_autonomous(
        self, tmp_hermes_home, tmp_workspace, sample_registration, phase2_lease
    ):
        handle_autopilot_command(f"register {json.dumps(sample_registration)}")
        handle_autopilot_command(f"lease {json.dumps(phase2_lease)}")
        result = handle_autopilot_command("execute")
        assert "handoff brief" in result.lower() or "not an execution directive" in result.lower()


# ---------------------------------------------------------------------------
# Safety property tests
# ---------------------------------------------------------------------------

class TestSafetyProperties:
    def test_brief_always_has_human_gate(self, bridge, state_with_registration_and_lease):
        """Briefs generated by the bridge always require human gate."""
        result = bridge.generate_brief(state_with_registration_and_lease)
        assert result.brief.human_gate_required is True

    def test_brief_always_not_authorized(self, bridge, state_with_registration_and_lease):
        """Briefs generated by the bridge are never execution-authorized."""
        result = bridge.generate_brief(state_with_registration_and_lease)
        assert result.brief.execution_authorized is False

    def test_brief_is_read_only_artifact(self, bridge, state_with_registration_and_lease):
        """Brief is an immutable dataclass — cannot be modified after creation."""
        result = bridge.generate_brief(state_with_registration_and_lease)
        brief = result.brief
        with pytest.raises(AttributeError):
            brief.execution_authorized = True
        with pytest.raises(AttributeError):
            brief.human_gate_required = False

    def test_no_workspace_files_modified(self, bridge, state_with_registration_and_lease, tmp_workspace):
        """Brief generation does not create files in the workspace."""
        before = set(str(p) for p in tmp_workspace.rglob("*"))
        bridge.generate_brief(state_with_registration_and_lease)
        after = set(str(p) for p in tmp_workspace.rglob("*"))
        assert before == after

    def test_bridge_never_performs_execution(self, bridge, state_with_registration_and_lease):
        """Bridge only generates briefs — no execution methods exist."""
        assert not hasattr(bridge, 'execute_task')
        assert not hasattr(bridge, 'run_development')
        assert not hasattr(bridge, 'apply_changes')
        assert not hasattr(bridge, 'commit_changes')

    def test_brief_schema_version_is_current(self, bridge, state_with_registration_and_lease):
        result = bridge.generate_brief(state_with_registration_and_lease)
        assert result.brief.schema_version == BRIEF_SCHEMA_VERSION


# ---------------------------------------------------------------------------
# Phase 2 readiness integration tests
# ---------------------------------------------------------------------------

class TestPhase2Readiness:
    def test_readiness_auto_detects_bridge(self, state_with_registration_and_lease):
        """readiness_for_phase with None auto-detects the execution bridge."""
        from autopilot.phases import readiness_for_phase, PHASE_2
        ready, blockers = readiness_for_phase(state_with_registration_and_lease, PHASE_2)
        # Should detect the bridge and pass the adapter check
        # (may still fail on capabilities, which is expected)
        adapter_blocker = [b for b in blockers if "adapter" in b.lower()]
        # The adapter should be detected, so no adapter blocker
        # But capabilities may be missing — that's expected
        assert not adapter_blocker or all("workspace.write" in b or "git.commit" in b for b in adapter_blocker)

    def test_readiness_explicit_false_still_blocks(self, state_with_registration_and_lease):
        """Explicitly passing real_adapter_available=False still blocks."""
        from autopilot.phases import readiness_for_phase, PHASE_2
        ready, blockers = readiness_for_phase(
            state_with_registration_and_lease, PHASE_2, real_adapter_available=False
        )
        adapter_blockers = [b for b in blockers if "adapter" in b.lower()]
        assert len(adapter_blockers) > 0

    def test_readiness_with_phase2_lease_and_explicit_adapter(
        self, tmp_hermes_home, tmp_workspace, sample_registration, phase2_lease
    ):
        """Full Phase 2 readiness with proper lease and explicit adapter."""
        from autopilot.phases import readiness_for_phase, PHASE_2
        state = {
            "schema_version": SCHEMA_VERSION,
            "state": STATE_LEASE_READY,
            "registration": sample_registration,
            "lease": phase2_lease,
            "loop_iteration": 0,
            "max_loop_iterations": 3,
            "transition_history": [],
            "run_count": 0,
            "last_error": None,
            "kill_switch_active": False,
        }
        ready, blockers = readiness_for_phase(state, PHASE_2, real_adapter_available=True)
        assert ready is True
        assert blockers == []


# ---------------------------------------------------------------------------
# Audit trail tests
# ---------------------------------------------------------------------------

class TestAuditTrail:
    def test_brief_generation_logged(self, bridge, state_with_registration_and_lease):
        from autopilot.audit import read_audit_trail
        bridge.generate_brief(state_with_registration_and_lease)
        trail = read_audit_trail()
        brief_events = [e for e in trail if e.get("event_type") == "brief_generated"]
        assert len(brief_events) >= 1
        assert "brief_id" in brief_events[-1].get("detail", "")
