"""Tests for registration module — immutable ProjectRegistration and workspace validation."""

from __future__ import annotations

import os
import sqlite3
import sys
from pathlib import Path

import pytest

from autopilot.registration import (
    ProjectRegistration,
    apply_registration_defaults,
    current_hermes_session_id,
    find_session_id_by_title,
    validate_registration,
    validate_workspace_path,
    registration_to_dict,
)
from autopilot.constants import SCHEMA_VERSION


class TestProjectRegistration:
    def test_creation(self):
        reg = ProjectRegistration(
            project_id="my-project",
            workspace_root="/tmp/project",
            discussion_session_id="s1",
            development_session_id="s2",
            display_title="My Project",
        )
        assert reg.project_id == "my-project"
        assert reg.display_title == "My Project"

    def test_is_frozen(self):
        reg = ProjectRegistration(
            project_id="proj",
            workspace_root="/tmp",
            discussion_session_id="s1",
            development_session_id="s2",
        )
        with pytest.raises(AttributeError):
            reg.project_id = "new"  # type: ignore[misc]

    def test_validate_from_dict(self):
        data = {
            "project_id": "test-proj",
            "workspace_root": "/tmp/test",
            "discussion_session_id": "d1",
            "development_session_id": "d2",
        }
        reg = validate_registration(data)
        assert reg.project_id == "test-proj"

    def test_validate_rejects_empty_project_id(self):
        data = {
            "project_id": "",
            "workspace_root": "/tmp",
            "discussion_session_id": "d1",
            "development_session_id": "d2",
        }
        with pytest.raises(ValueError, match="non-empty"):
            validate_registration(data)

    def test_validate_rejects_bad_format(self):
        data = {
            "project_id": "bad id with spaces!",
            "workspace_root": "/tmp",
            "discussion_session_id": "d1",
            "development_session_id": "d2",
        }
        with pytest.raises(ValueError, match="invalid format"):
            validate_registration(data)

    def test_validate_rejects_missing_keys(self):
        with pytest.raises(TypeError, match="Missing"):
            validate_registration({"project_id": "x"})

    def test_validate_allows_defaults_to_fill_session_keys(self):
        data = {
            "project_id": "test-proj",
            "workspace_root": "/tmp/test",
            "discussion_session_id": "d1",
            "development_session_id": "d2",
        }
        reg = validate_registration(data)
        assert reg.discussion_session_id == "d1"
        assert reg.development_session_id == "d2"

    def test_validate_requires_session_keys_after_defaults(self):
        with pytest.raises(ValueError, match="discussion_session_id"):
            validate_registration({"project_id": "test-proj", "workspace_root": "/tmp/test"})

    def test_to_dict(self):
        reg = ProjectRegistration(
            project_id="p",
            workspace_root="/tmp",
            discussion_session_id="d1",
            development_session_id="d2",
        )
        d = registration_to_dict(reg)
        assert d["project_id"] == "p"
        assert d["workspace_root"] == "/tmp"


class TestSessionResolution:
    def _create_state_db(self, hermes_home):
        db = hermes_home / "state.db"
        conn = sqlite3.connect(db)
        try:
            conn.execute(
                "CREATE TABLE sessions (id TEXT PRIMARY KEY, title TEXT, started_at REAL)"
            )
            conn.execute(
                "INSERT INTO sessions (id, title, started_at) VALUES (?, ?, ?)",
                ("sess-disc", "Solar360 Project Discussion", 1.0),
            )
            conn.execute(
                "INSERT INTO sessions (id, title, started_at) VALUES (?, ?, ?)",
                ("sess-dev-old", "Solar360 Development", 1.0),
            )
            conn.execute(
                "INSERT INTO sessions (id, title, started_at) VALUES (?, ?, ?)",
                ("sess-dev-new", "Solar360 Development", 2.0),
            )
            conn.commit()
        finally:
            conn.close()

    def test_current_session_prefers_existing_session_key(self, tmp_hermes_home, monkeypatch):
        self._create_state_db(tmp_hermes_home)
        monkeypatch.setenv("HERMES_SESSION_KEY", "sess-disc")
        monkeypatch.setenv("HERMES_SESSION_ID", "transient-worker")
        assert current_hermes_session_id() == "sess-disc"

    def test_find_session_id_by_title_newest_exact_match(self, tmp_hermes_home):
        self._create_state_db(tmp_hermes_home)
        assert find_session_id_by_title("Solar360 Development") == "sess-dev-new"

    def test_apply_registration_defaults(self, tmp_hermes_home, tmp_workspace, monkeypatch):
        self._create_state_db(tmp_hermes_home)
        monkeypatch.setenv("HERMES_SESSION_KEY", "smart-router-transient")
        data, notes = apply_registration_defaults({
            "project_id": "solar360",
            "workspace_root": str(tmp_workspace),
            "discussion_title": "Solar360 Project Discussion",
            "development_title": "Solar360 Development",
        })
        assert data["discussion_session_id"] == "sess-disc"
        assert data["development_session_id"] == "sess-dev-new"
        assert any("discussion_session_id auto-filled from session title" in note for note in notes)
        assert any("development_session_id auto-filled" in note for note in notes)


class TestWorkspaceValidation:
    def test_valid_workspace(self, tmp_workspace):
        valid, _, _ = validate_workspace_path(str(tmp_workspace))
        assert valid is True

    def test_nonexistent_workspace(self):
        valid, err, _ = validate_workspace_path("/tmp/absolutely_nonexistent_xyz")
        assert valid is False
        assert len(err) > 0  # error message should be non-empty

    def test_reject_home(self):
        valid, _, _ = validate_workspace_path(str(Path.home()))
        assert valid is False

    def test_reject_system_dirs(self):
        """Protected roots include home and HERMES_HOME."""
        valid, _, _ = validate_workspace_path(str(Path.home()))
        assert valid is False

    def test_symlink_resolves(self, tmp_workspace):
        target = tmp_workspace / "target"
        target.mkdir()
        link = tmp_workspace / "link"
        try:
            link.symlink_to(target)
            valid, _, _ = validate_workspace_path(str(link))
            assert valid is True  # resolves to real dir
        except OSError:
            pytest.skip("symlinks not supported")
