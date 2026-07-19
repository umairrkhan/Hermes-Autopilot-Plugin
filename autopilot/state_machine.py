"""Fail-closed state machine.

States:
  IDLE, CONFIGURED, LEASE_READY, SIMULATING, PREPARING_BRIEF,
  EXECUTING, VERIFYING, REVIEWING, REMEDIATING, PHASE_ACCEPTED,
  NEEDS_HUMAN, PAUSED, FAILED, LEASE_EXPIRED, STOPPED

Rules:
- Transitions are validated against a fixed graph
- Invalid transitions raise ValueError
- History is recorded and capped at MAX_TRANSITION_HISTORY
- Most states can transition to STOPPED (emergency) or NEEDS_HUMAN
- Terminal states (IDLE, STOPPED, FAILED, LEASE_EXPIRED) have limited exits
"""

from __future__ import annotations

from typing import Any

from .constants import (
    STATE_IDLE, STATE_CONFIGURED, STATE_LEASE_READY, STATE_SIMULATING,
    STATE_PREPARING_BRIEF, STATE_EXECUTING, STATE_VERIFYING,
    STATE_REVIEWING, STATE_REMEDIATING, STATE_PHASE_ACCEPTED,
    STATE_NEEDS_HUMAN, STATE_PAUSED, STATE_FAILED, STATE_LEASE_EXPIRED,
    STATE_STOPPED, TERMINAL_STATES, ERROR_STATES, MAX_TRANSITION_HISTORY,
)

# Allowed transitions: from_state -> set of valid target states
# This is the complete state machine graph.
ALLOWED_TRANSITIONS: dict[str, set[str]] = {
    STATE_IDLE: {STATE_CONFIGURED, STATE_STOPPED},
    STATE_CONFIGURED: {STATE_LEASE_READY, STATE_SIMULATING, STATE_IDLE, STATE_STOPPED},
    STATE_LEASE_READY: {
        STATE_SIMULATING, STATE_LEASE_EXPIRED, STATE_NEEDS_HUMAN, STATE_PAUSED,
        STATE_IDLE, STATE_STOPPED,
    },
    STATE_SIMULATING: {
        STATE_PREPARING_BRIEF, STATE_NEEDS_HUMAN, STATE_PAUSED,
        STATE_FAILED, STATE_LEASE_EXPIRED, STATE_STOPPED,
    },
    STATE_PREPARING_BRIEF: {
        STATE_EXECUTING, STATE_NEEDS_HUMAN, STATE_PAUSED,
        STATE_FAILED, STATE_LEASE_EXPIRED, STATE_STOPPED,
    },
    STATE_EXECUTING: {
        STATE_VERIFYING, STATE_NEEDS_HUMAN, STATE_PAUSED,
        STATE_FAILED, STATE_LEASE_EXPIRED, STATE_STOPPED,
    },
    STATE_VERIFYING: {
        STATE_REVIEWING, STATE_NEEDS_HUMAN, STATE_PAUSED,
        STATE_FAILED, STATE_LEASE_EXPIRED, STATE_STOPPED,
    },
    STATE_REVIEWING: {
        STATE_PHASE_ACCEPTED, STATE_REMEDIATING, STATE_NEEDS_HUMAN,
        STATE_PAUSED, STATE_FAILED, STATE_LEASE_EXPIRED, STATE_STOPPED,
    },
    STATE_REMEDIATING: {
        STATE_VERIFYING, STATE_NEEDS_HUMAN, STATE_PAUSED,
        STATE_FAILED, STATE_LEASE_EXPIRED, STATE_STOPPED,
    },
    STATE_PHASE_ACCEPTED: {STATE_IDLE, STATE_SIMULATING, STATE_STOPPED},
    STATE_NEEDS_HUMAN: {
        STATE_SIMULATING, STATE_EXECUTING, STATE_VERIFYING,
        STATE_REVIEWING, STATE_REMEDIATING, STATE_CONFIGURED,
        STATE_IDLE, STATE_STOPPED, STATE_FAILED,
    },
    STATE_PAUSED: {
        STATE_SIMULATING, STATE_EXECUTING, STATE_VERIFYING,
        STATE_REVIEWING, STATE_REMEDIATING, STATE_CONFIGURED,
        STATE_IDLE, STATE_STOPPED, STATE_FAILED,
    },
    STATE_FAILED: {STATE_IDLE, STATE_STOPPED},
    STATE_LEASE_EXPIRED: {STATE_IDLE, STATE_STOPPED},
    STATE_STOPPED: set(),  # Terminal — no exits except manual reset
}


def can_transition(from_state: str, to_state: str) -> bool:
    """Check if a transition is allowed."""
    if from_state not in ALLOWED_TRANSITIONS:
        return False
    return to_state in ALLOWED_TRANSITIONS[from_state]


def transition(
    state: dict[str, Any],
    target: str,
    reason: str = "",
) -> dict[str, Any]:
    """Apply a state transition.

    Modifies state in-place and returns it.
    Raises ValueError for invalid transitions.
    """
    current = current_state_label(state)

    if not can_transition(current, target):
        raise ValueError(
            f"Invalid transition: {current} -> {target}. "
            f"Allowed targets: {sorted(ALLOWED_TRANSITIONS.get(current, set()))}"
        )

    history = state.get("transition_history", [])
    history.append({
        "from": current,
        "to": target,
        "reason": reason,
    })
    # Cap history
    if len(history) > MAX_TRANSITION_HISTORY:
        history = history[-MAX_TRANSITION_HISTORY:]

    state["state"] = target
    state["transition_history"] = history
    return state


def current_state_label(state: dict[str, Any]) -> str:
    """Get the current state label, defaulting to IDLE for invalid states."""
    s = state.get("state", STATE_IDLE)
    if s not in {
        STATE_IDLE, STATE_CONFIGURED, STATE_LEASE_READY, STATE_SIMULATING,
        STATE_PREPARING_BRIEF, STATE_EXECUTING, STATE_VERIFYING,
        STATE_REVIEWING, STATE_REMEDIATING, STATE_PHASE_ACCEPTED,
        STATE_NEEDS_HUMAN, STATE_PAUSED, STATE_FAILED, STATE_LEASE_EXPIRED,
        STATE_STOPPED,
    }:
        return STATE_IDLE
    return s


def increment_loop(state: dict[str, Any]) -> int:
    """Increment the loop iteration counter."""
    n = state.get("loop_iteration", 0) + 1
    state["loop_iteration"] = n
    return n


def is_terminal(state_label: str) -> bool:
    """Check if a state is terminal."""
    return state_label in TERMINAL_STATES


def is_error(state_label: str) -> bool:
    """Check if a state is an error state."""
    return state_label in ERROR_STATES


def state_summary(state: dict[str, Any]) -> dict[str, Any]:
    """Return a summary of the current state."""
    label = current_state_label(state)
    return {
        "state": label,
        "loop_iteration": state.get("loop_iteration", 0),
        "max_loop": state.get("max_loop_iterations", 1),
        "is_terminal": is_terminal(label),
        "is_error": is_error(label),
        "transitions_count": len(state.get("transition_history", [])),
        "run_count": state.get("run_count", 0),
    }
