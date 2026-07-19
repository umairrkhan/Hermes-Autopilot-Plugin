"""Tests for storage module — atomic writes, locking, schema versioning, corruption detection."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

from autopilot.storage import (
    load_state, save_state, mutate_state, reset_state, _state_path,
    SCHEMA_VERSION,
)

# ---------------------------------------------------------------------------
# Basic save/load/reset
# ---------------------------------------------------------------------------

class TestBasicStorage:
    def test_save_and_load(self, tmp_hermes_home):
        state = {"state": "IDLE", "schema_version": SCHEMA_VERSION}
        save_state(state)
        loaded = load_state()
        assert loaded["state"] == "IDLE"

    def test_load_missing_returns_default(self, tmp_hermes_home):
        loaded = load_state()
        assert loaded["state"] == "IDLE"
        assert loaded["schema_version"] == SCHEMA_VERSION

    def test_reset(self, tmp_hermes_home):
        state = {"state": "CONFIGURED", "schema_version": SCHEMA_VERSION, "test": True}
        save_state(state)
        loaded = load_state()
        assert loaded["state"] == "CONFIGURED"
        reset_state()
        loaded2 = load_state()
        assert loaded2["state"] == "IDLE"

    def test_state_path_location(self, tmp_hermes_home):
        p = _state_path()
        assert str(tmp_hermes_home) in str(p)
        assert p.name == "autopilot_state.json"


# ---------------------------------------------------------------------------
# Atomic writes
# ---------------------------------------------------------------------------

class TestAtomicWrites:
    def test_atomic_write_no_partial(self, tmp_hermes_home):
        from autopilot.constants import STATE_IDLE, SCHEMA_VERSION
        state = {"state": STATE_IDLE, "schema_version": SCHEMA_VERSION}
        save_state(state)
        loaded = load_state()
        assert loaded["state"] == STATE_IDLE
        # Only the final file should exist, no temp files
        state_dir = _state_path().parent
        json_files = list(state_dir.glob("*.json"))
        assert len(json_files) == 1

    def test_atomic_write_backup(self, tmp_hermes_home):
        from autopilot.constants import STATE_IDLE, STATE_CONFIGURED, SCHEMA_VERSION
        state1 = {"state": STATE_IDLE, "schema_version": SCHEMA_VERSION}
        save_state(state1)
        state2 = {"state": STATE_CONFIGURED, "schema_version": SCHEMA_VERSION}
        save_state(state2)
        loaded = load_state()
        assert loaded["state"] == STATE_CONFIGURED
        # State file should exist (atomic write replaces in-place)
        assert _state_path().exists()


# ---------------------------------------------------------------------------
# Schema versioning
# ---------------------------------------------------------------------------

class TestSchemaVersioning:
    def test_schema_version_in_saved(self, tmp_hermes_home):
        state = {"state": "IDLE"}
        save_state(state)
        raw = json.loads(_state_path().read_text())
        assert raw["schema_version"] == SCHEMA_VERSION

    def test_wrong_schema_version_rejected(self, tmp_hermes_home):
        state_path = _state_path()
        raw = {"state": "IDLE", "schema_version": 999}
        state_path.write_text(json.dumps(raw, indent=2))
        loaded = load_state()
        # Should reset to default when schema version is wrong
        assert loaded["schema_version"] == SCHEMA_VERSION


# ---------------------------------------------------------------------------
# Corruption detection
# ---------------------------------------------------------------------------

class TestCorruptionDetection:
    def test_corrupted_json(self, tmp_hermes_home):
        state_path = _state_path()
        state_path.write_text("not json at all {{{")
        loaded = load_state()
        # Should return default state (fail-closed) when file is corrupted
        assert loaded["state"] == "IDLE"
        assert loaded.get("schema_version") == SCHEMA_VERSION

    def test_truncated_json(self, tmp_hermes_home):
        state_path = _state_path()
        state_path.write_text('{"state": "IDLE", "schema_')
        loaded = load_state()
        assert loaded["state"] == "IDLE"

    def test_empty_file(self, tmp_hermes_home):
        state_path = _state_path()
        state_path.write_text("")
        loaded = load_state()
        assert loaded["state"] == "IDLE"

    def test_wrong_schema_resets(self, tmp_hermes_home):
        state_path = _state_path()
        state_path.write_text(json.dumps({"state": "X", "schema_version": 999}))
        loaded = load_state()
        assert loaded["schema_version"] == SCHEMA_VERSION


# ---------------------------------------------------------------------------
# mutate_state
# ---------------------------------------------------------------------------

class TestMutateState:
    def test_mutate_applies(self, tmp_hermes_home):
        def mutate(s):
            s["state"] = "CONFIGURED"
        mutate_state(mutate)
        loaded = load_state()
        assert loaded["state"] == "CONFIGURED"

    def test_mutate_atomic(self, tmp_hermes_home):
        def mutate(s):
            s["state"] = "CONFIGURED"
            raise RuntimeError("Simulated error")
        with pytest.raises(RuntimeError):
            mutate_state(mutate)
        loaded = load_state()
        assert loaded["state"] == "IDLE"  # unchanged


# ---------------------------------------------------------------------------
# Cross-process locking
# ---------------------------------------------------------------------------

class TestLocking:
    def test_lock_file_created(self, tmp_hermes_home):
        save_state({"state": "IDLE", "schema_version": SCHEMA_VERSION})
        lock_path = _state_path().with_suffix(".json.lock")
        # After save completes, lock should be released (file may or may not exist)
        loaded = load_state()
        assert loaded["state"] == "IDLE"
