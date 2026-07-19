"""Tests for state machine."""

from __future__ import annotations

import sys

import pytest

from autopilot.state_machine import (
    current_state_label, can_transition, transition,
    increment_loop, is_terminal, is_error, state_summary,
)
from autopilot.constants import (
    STATE_IDLE, STATE_CONFIGURED, STATE_LEASE_READY, STATE_SIMULATING,
    STATE_PREPARING_BRIEF, STATE_EXECUTING, STATE_VERIFYING,
    STATE_REVIEWING, STATE_REMEDIATING, STATE_PHASE_ACCEPTED,
    STATE_NEEDS_HUMAN, STATE_PAUSED, STATE_FAILED, STATE_LEASE_EXPIRED,
    STATE_STOPPED,
)

# ---------------------------------------------------------------------------
# Valid transitions
# ---------------------------------------------------------------------------

class TestTransitions:
    def test_idle_to_configured(self):
        state = {"state": STATE_IDLE, "loop_iteration": 0, "transition_history": []}
        result = transition(state, STATE_CONFIGURED, "test")
        assert result["state"] == STATE_CONFIGURED

    def test_configured_to_lease_ready(self):
        state = {"state": STATE_CONFIGURED, "loop_iteration": 0, "transition_history": []}
        transition(state, STATE_LEASE_READY, "test")
        assert state["state"] == STATE_LEASE_READY

    def test_lease_ready_to_simulating(self):
        state = {"state": STATE_LEASE_READY, "loop_iteration": 0, "transition_history": []}
        transition(state, STATE_SIMULATING, "test")
        assert state["state"] == STATE_SIMULATING

    def test_simulating_to_preparing_brief(self):
        state = {"state": STATE_SIMULATING, "loop_iteration": 0, "transition_history": []}
        transition(state, STATE_PREPARING_BRIEF, "test")
        assert state["state"] == STATE_PREPARING_BRIEF

    def test_preparing_brief_to_executing(self):
        state = {"state": STATE_PREPARING_BRIEF, "loop_iteration": 0, "transition_history": []}
        transition(state, STATE_EXECUTING, "test")
        assert state["state"] == STATE_EXECUTING

    def test_executing_to_verifying(self):
        state = {"state": STATE_EXECUTING, "loop_iteration": 0, "transition_history": []}
        transition(state, STATE_VERIFYING, "test")
        assert state["state"] == STATE_VERIFYING

    def test_verifying_to_reviewing(self):
        state = {"state": STATE_VERIFYING, "loop_iteration": 0, "transition_history": []}
        transition(state, STATE_REVIEWING, "test")
        assert state["state"] == STATE_REVIEWING

    def test_reviewing_to_phase_accepted(self):
        state = {"state": STATE_REVIEWING, "loop_iteration": 0, "transition_history": []}
        transition(state, STATE_PHASE_ACCEPTED, "test")
        assert state["state"] == STATE_PHASE_ACCEPTED

    def test_reviewing_to_remediating(self):
        state = {"state": STATE_REVIEWING, "loop_iteration": 0, "transition_history": []}
        transition(state, STATE_REMEDIATING, "test")
        assert state["state"] == STATE_REMEDIATING

    def test_remediating_to_verifying(self):
        state = {"state": STATE_REMEDIATING, "loop_iteration": 0, "transition_history": []}
        transition(state, STATE_VERIFYING, "test")
        assert state["state"] == STATE_VERIFYING

    def test_any_to_needs_human(self):
        """Most states can transition to NEEDS_HUMAN."""
        for s in [STATE_SIMULATING, STATE_PREPARING_BRIEF, STATE_EXECUTING,
                   STATE_VERIFYING, STATE_REVIEWING, STATE_REMEDIATING]:
            state = {"state": s, "loop_iteration": 0, "transition_history": []}
            transition(state, STATE_NEEDS_HUMAN, "need human")
            assert state["state"] == STATE_NEEDS_HUMAN

    def test_any_to_stopped(self):
        """Most states can transition to STOPPED."""
        for s in [STATE_IDLE, STATE_CONFIGURED, STATE_LEASE_READY,
                   STATE_SIMULATING, STATE_EXECUTING, STATE_VERIFYING,
                   STATE_REVIEWING, STATE_PAUSED, STATE_FAILED]:
            state = {"state": s, "loop_iteration": 0, "transition_history": []}
            transition(state, STATE_STOPPED, "stop")
            assert state["state"] == STATE_STOPPED

    def test_any_to_lease_expired(self):
        """Most operational states can transition to LEASE_EXPIRED."""
        for s in [STATE_LEASE_READY, STATE_SIMULATING, STATE_EXECUTING,
                   STATE_VERIFYING, STATE_REVIEWING, STATE_REMEDIATING]:
            state = {"state": s, "loop_iteration": 0, "transition_history": []}
            transition(state, STATE_LEASE_EXPIRED, "lease expired")
            assert state["state"] == STATE_LEASE_EXPIRED


# ---------------------------------------------------------------------------
# Invalid transitions
# ---------------------------------------------------------------------------

class TestInvalidTransitions:
    def test_idle_to_simulating_invalid(self):
        state = {"state": STATE_IDLE, "loop_iteration": 0, "transition_history": []}
        with pytest.raises(ValueError, match="Invalid transition"):
            transition(state, STATE_SIMULATING, "skip ahead")

    def test_configured_to_executing_invalid(self):
        state = {"state": STATE_CONFIGURED, "loop_iteration": 0, "transition_history": []}
        with pytest.raises(ValueError, match="Invalid transition"):
            transition(state, STATE_EXECUTING, "skip ahead")

    def test_phase_accepted_to_executing_invalid(self):
        state = {"state": STATE_PHASE_ACCEPTED, "loop_iteration": 0, "transition_history": []}
        with pytest.raises(ValueError, match="Invalid transition"):
            transition(state, STATE_EXECUTING, "backwards")

    def test_stopped_to_configured_invalid(self):
        state = {"state": STATE_STOPPED, "loop_iteration": 0, "transition_history": []}
        with pytest.raises(ValueError, match="Invalid transition"):
            transition(state, STATE_CONFIGURED, "reconfigure")

    def test_failed_to_simulating_invalid(self):
        state = {"state": STATE_FAILED, "loop_iteration": 0, "transition_history": []}
        with pytest.raises(ValueError, match="Invalid transition"):
            transition(state, STATE_SIMULATING, "retry")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class TestHelpers:
    def test_current_state_label(self):
        assert current_state_label({"state": STATE_IDLE}) == STATE_IDLE

    def test_current_state_label_invalid(self):
        assert current_state_label({"state": "INVALID"}) == STATE_IDLE

    def test_current_state_label_missing(self):
        assert current_state_label({}) == STATE_IDLE

    def test_can_transition_valid(self):
        assert can_transition(STATE_IDLE, STATE_CONFIGURED)

    def test_can_transition_invalid(self):
        assert not can_transition(STATE_IDLE, STATE_SIMULATING)

    def test_can_transition_invalid_state(self):
        assert not can_transition("INVALID", STATE_CONFIGURED)

    def test_increment_loop(self):
        state = {"loop_iteration": 0}
        n = increment_loop(state)
        assert n == 1
        assert state["loop_iteration"] == 1
        n = increment_loop(state)
        assert n == 2

    def test_is_terminal(self):
        assert is_terminal(STATE_IDLE)
        assert is_terminal(STATE_STOPPED)
        assert not is_terminal(STATE_EXECUTING)

    def test_is_error(self):
        assert is_error(STATE_FAILED)
        assert is_error(STATE_LEASE_EXPIRED)
        assert not is_error(STATE_IDLE)

    def test_state_summary(self):
        state = {"state": STATE_IDLE, "loop_iteration": 2, "max_loop_iterations": 5,
                 "transition_history": [{"from": "A", "to": "B"}]}
        s = state_summary(state)
        assert s["state"] == STATE_IDLE
        assert s["loop_iteration"] == 2
        assert s["max_loop"] == 5
        assert s["is_terminal"] is True
        assert s["transitions_count"] == 1

    def test_transition_history_recorded(self):
        state = {"state": STATE_IDLE, "loop_iteration": 0, "transition_history": []}
        transition(state, STATE_CONFIGURED, "register")
        transition(state, STATE_LEASE_READY, "lease")
        assert len(state["transition_history"]) == 2
        assert state["transition_history"][0]["from"] == STATE_IDLE
        assert state["transition_history"][0]["to"] == STATE_CONFIGURED

    def test_transition_history_capped(self):
        state = {"state": STATE_IDLE, "loop_iteration": 0,
                 "transition_history": [{"from": "X", "to": "Y"}] * 150}
        transition(state, STATE_CONFIGURED, "overflow")
        assert len(state["transition_history"]) <= 100

    def test_full_simulation_cycle(self):
        """Walk the complete happy path: IDLE -> ... -> PHASE_ACCEPTED -> IDLE."""
        state = {"state": STATE_IDLE, "loop_iteration": 0, "transition_history": []}
        path = [
            STATE_CONFIGURED, STATE_LEASE_READY, STATE_SIMULATING,
            STATE_PREPARING_BRIEF, STATE_EXECUTING, STATE_VERIFYING,
            STATE_REVIEWING, STATE_PHASE_ACCEPTED, STATE_IDLE,
        ]
        for target in path:
            transition(state, target, f"step to {target}")
        assert state["state"] == STATE_IDLE
        assert len(state["transition_history"]) == 9
