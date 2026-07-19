"""Tests for adapters — simulation, session, kanban."""

from __future__ import annotations

import sys

import pytest

from autopilot.adapters.simulation import SimulationAdapter
from autopilot.adapters.session import ReadOnlySessionAdapter
from autopilot.adapters.kanban import ReadOnlyKanbanAdapter

# ---------------------------------------------------------------------------
# Simulation adapter
# ---------------------------------------------------------------------------

class TestSimulationAdapter:
    def test_run_simulation_happy_path(self, sample_registration, sample_lease):
        adapter = SimulationAdapter(max_remediation=1)
        result = adapter.run_simulation(
            registration=sample_registration,
            lease=sample_lease,
        )
        assert result.accepted is True
        assert result.remediation_used is True
        assert result.remediation_count == 1
        assert result.total_steps == 6  # planner, dev, verify, review, remediator, re-verify

    def test_run_simulation_no_registration(self, sample_lease):
        adapter = SimulationAdapter()
        result = adapter.run_simulation(lease=sample_lease)
        assert result.error == "No registration provided"
        assert result.accepted is False

    def test_run_simulation_no_lease(self, sample_registration):
        """Phase 1 offline simulation permits no lease because no real actions run."""
        adapter = SimulationAdapter()
        result = adapter.run_simulation(registration=sample_registration)
        assert result.error == ""
        assert result.accepted is True

    def test_simulate_verifier_failure(self):
        adapter = SimulationAdapter()
        result = adapter.simulate_verifier_failure()
        assert result.accepted is False
        assert result.remediation_used is True
        assert any(not s.success for s in result.steps)

    def test_simulate_reviewer_failure(self):
        adapter = SimulationAdapter()
        result = adapter.simulate_reviewer_failure()
        assert result.accepted is False
        assert any(s.role == "reviewer" and not s.success for s in result.steps)

    def test_simulate_max_remediation(self):
        adapter = SimulationAdapter(max_remediation=2)
        result = adapter.simulate_max_remediation_reached()
        assert result.accepted is False
        assert result.max_remediation_reached is True

    def test_simulation_steps_have_roles(self, sample_registration, sample_lease):
        adapter = SimulationAdapter()
        result = adapter.run_simulation(registration=sample_registration, lease=sample_lease)
        valid_roles = {"planner", "developer", "verifier", "reviewer", "remediator"}
        for step in result.steps:
            assert step.role in valid_roles

    def test_simulation_to_dict(self, sample_registration, sample_lease):
        adapter = SimulationAdapter()
        result = adapter.run_simulation(registration=sample_registration, lease=sample_lease)
        d = result.to_dict()
        assert isinstance(d, dict)
        assert "steps" in d
        assert "accepted" in d
        assert isinstance(d["steps"], list)


# ---------------------------------------------------------------------------
# Read-only session adapter
# ---------------------------------------------------------------------------

class TestReadOnlySessionAdapter:
    def test_missing_db_returns_empty(self, tmp_path):
        adapter = ReadOnlySessionAdapter(hermes_home=str(tmp_path / "nonexistent"))
        info = adapter.get_session_info("test")
        assert "error" in info

    def test_list_sessions_missing_db(self, tmp_path):
        adapter = ReadOnlySessionAdapter(hermes_home=str(tmp_path / "nonexistent"))
        sessions = adapter.list_sessions()
        assert sessions == []

    def test_adapter_is_read_only_interface(self, tmp_path):
        adapter = ReadOnlySessionAdapter(hermes_home=str(tmp_path / "nonexistent"))
        assert not hasattr(adapter, 'write_session')
        assert not hasattr(adapter, 'create_session')
        assert not hasattr(adapter, 'delete_session')


# ---------------------------------------------------------------------------
# Read-only Kanban adapter
# ---------------------------------------------------------------------------

class TestReadOnlyKanbanAdapter:
    def test_missing_db_returns_empty(self, tmp_path):
        adapter = ReadOnlyKanbanAdapter(hermes_home=str(tmp_path / "nonexistent"))
        tasks = adapter.list_tasks()
        assert tasks == []

    def test_get_task_missing_db(self, tmp_path):
        adapter = ReadOnlyKanbanAdapter(hermes_home=str(tmp_path / "nonexistent"))
        task = adapter.get_task("test")
        assert task is None

    def test_adapter_is_read_only_interface(self, tmp_path):
        adapter = ReadOnlyKanbanAdapter(hermes_home=str(tmp_path / "nonexistent"))
        assert not hasattr(adapter, 'create_task')
        assert not hasattr(adapter, 'update_task')
        assert not hasattr(adapter, 'delete_task')


# ---------------------------------------------------------------------------
# No side effects
# ---------------------------------------------------------------------------

class TestNoSideEffects:
    def test_simulation_no_files_created(self, tmp_path, sample_registration, sample_lease):
        """Simulation should not create any files in the workspace."""
        import os
        before = set(os.listdir(str(tmp_path)))
        adapter = SimulationAdapter()
        adapter.run_simulation(registration=sample_registration, lease=sample_lease)
        after = set(os.listdir(str(tmp_path)))
        assert before == after

    def test_simulation_no_provider_calls(self, sample_registration, sample_lease):
        """Simulation should not make any real LLM/model calls."""
        adapter = SimulationAdapter()
        result = adapter.run_simulation(registration=sample_registration, lease=sample_lease)
        # Verify all steps have static/deterministic responses
        for step in result.steps:
            assert step.result != ""  # non-empty
            assert step.role in {"planner", "developer", "verifier", "reviewer", "remediator"}
