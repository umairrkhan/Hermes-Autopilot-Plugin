"""Immutable project registration contract.

The registration binds the autopilot to a specific Hermes Project with:
- project_id: immutable canonical ID
- workspace_root: canonical workspace root (must be validated)
- discussion_session_id: provenance ID of the Discussion authority session (not a routing target)
- development_session_id: provenance ID of the Development boundary session (not a routing target)
- display_title / discussion_title / development_title: display-only
"""

from __future__ import annotations

import os
import re
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


_PROJECT_ID_PATTERN = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]{2,63}$")


def _hermes_home() -> Path:
    """Return the active Hermes home directory."""
    raw = os.environ.get("HERMES_HOME", "").strip()
    return Path(raw).expanduser() if raw else Path.home() / ".hermes"


def _session_exists(session_id: str) -> bool:
    """Return True if a Hermes session id exists in state.db."""
    sid = (session_id or "").strip()
    if not sid:
        return False
    db_path = _hermes_home() / "state.db"
    if not db_path.exists():
        return False
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        try:
            row = conn.execute("SELECT 1 FROM sessions WHERE id = ? LIMIT 1", (sid,)).fetchone()
            return row is not None
        finally:
            conn.close()
    except sqlite3.Error:
        return False


def current_hermes_session_id() -> str:
    """Best-effort current Hermes conversation id."""
    candidates = (
        os.environ.get("HERMES_SESSION_KEY", ""),
        os.environ.get("HERMES_SESSION_ID", ""),
    )
    for candidate in candidates:
        if _session_exists(candidate):
            return candidate.strip()
    for candidate in candidates:
        if candidate and candidate.strip():
            return candidate.strip()
    return ""


def find_session_id_by_title(title: str) -> str:
    """Find the newest Hermes session with an exact title match."""
    wanted = (title or "").strip()
    if not wanted:
        return ""
    db_path = _hermes_home() / "state.db"
    if not db_path.exists():
        return ""
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        try:
            row = conn.execute(
                """
                SELECT id FROM sessions
                WHERE title = ?
                ORDER BY started_at DESC
                LIMIT 1
                """,
                (wanted,),
            ).fetchone()
            return str(row[0]) if row else ""
        finally:
            conn.close()
    except sqlite3.Error:
        return ""


def apply_registration_defaults(data: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """Fill missing registration session ids from the Hermes runtime."""
    if not isinstance(data, dict):
        raise TypeError("Registration must be a dict")

    resolved = dict(data)
    notes: list[str] = []

    if not str(resolved.get("discussion_session_id", "")).strip():
        discussion_title = str(resolved.get("discussion_title", "")).strip()
        sid = find_session_id_by_title(discussion_title)
        if sid:
            resolved["discussion_session_id"] = sid
            notes.append(f"discussion_session_id auto-filled from session title {discussion_title!r}: {sid}")
        else:
            sid = current_hermes_session_id()
            if sid and not sid.startswith("smart-router-"):
                resolved["discussion_session_id"] = sid
                notes.append(f"discussion_session_id auto-filled from current Hermes session: {sid}")
            else:
                notes.append(
                    "discussion_session_id could not be auto-detected; provide "
                    "discussion_title matching an existing session or pass discussion_session_id"
                )

    if not str(resolved.get("development_session_id", "")).strip():
        dev_title = str(resolved.get("development_title", "")).strip()
        sid = find_session_id_by_title(dev_title)
        if sid:
            resolved["development_session_id"] = sid
            notes.append(f"development_session_id auto-filled from session title {dev_title!r}: {sid}")
        else:
            notes.append(
                "development_session_id could not be auto-detected; provide "
                "development_title matching an existing session or pass development_session_id"
            )

    return resolved, notes


@dataclass(frozen=True)
class ProjectRegistration:
    """Immutable project registration."""
    project_id: str
    workspace_root: str
    discussion_session_id: str
    development_session_id: str
    display_title: str = ""
    discussion_title: str = ""
    development_title: str = ""


def validate_registration(data: dict[str, Any]) -> ProjectRegistration:
    """Validate and create a ProjectRegistration from a dict."""
    if not isinstance(data, dict):
        raise TypeError("Registration must be a dict")

    required_keys = ("project_id", "workspace_root")
    missing = [k for k in required_keys if k not in data]
    if missing:
        raise TypeError(f"Missing required keys: {missing}")

    pid = data["project_id"]
    if not pid:
        raise ValueError("project_id must be non-empty")
    if not _PROJECT_ID_PATTERN.match(pid):
        raise ValueError(
            f"project_id '{pid}' invalid format: must be 3-64 chars, "
            "alphanumeric/dash/dot/underscore"
        )

    ws = data["workspace_root"]
    if not ws:
        raise ValueError("workspace_root must be non-empty")

    discussion_session_id = str(data.get("discussion_session_id", "")).strip()
    development_session_id = str(data.get("development_session_id", "")).strip()
    if not discussion_session_id:
        raise ValueError(
            "discussion_session_id is required; use apply_registration_defaults() for auto-detection"
        )
    if not development_session_id:
        raise ValueError(
            "development_session_id is required; pass development_title for auto-detection or provide the id"
        )

    return ProjectRegistration(
        project_id=pid,
        workspace_root=str(ws),
        discussion_session_id=discussion_session_id,
        development_session_id=development_session_id,
        display_title=str(data.get("display_title", "")),
        discussion_title=str(data.get("discussion_title", "")),
        development_title=str(data.get("development_title", "")),
    )


def registration_to_dict(reg: ProjectRegistration) -> dict[str, Any]:
    """Convert registration to a plain dict."""
    return {
        "project_id": reg.project_id,
        "workspace_root": reg.workspace_root,
        "discussion_session_id": reg.discussion_session_id,
        "development_session_id": reg.development_session_id,
        "display_title": reg.display_title,
        "discussion_title": reg.discussion_title,
        "development_title": reg.development_title,
    }


def validate_workspace_path(workspace_root: str) -> tuple[bool, str, str]:
    """Validate a workspace root path.

    Returns (valid, resolved_path, error_message).
    """
    hermes_home = os.environ.get("HERMES_HOME", "").strip()
    if hermes_home:
        hermes_home_resolved = Path(hermes_home).expanduser().resolve()
    else:
        hermes_home_resolved = (Path.home() / ".hermes").resolve()

    ws = Path(workspace_root).expanduser().resolve()

    if not ws.exists():
        return False, str(ws), f"Workspace does not exist: {ws}"

    if not ws.is_dir():
        return False, str(ws), f"Workspace is not a directory: {ws}"

    # Reject protected roots
    home_resolved = Path.home().resolve()
    if ws == home_resolved:
        return False, str(ws), "Cannot use home directory as workspace (protected root)"

    if ws == hermes_home_resolved:
        return False, str(ws), "Cannot use HERMES_HOME as workspace (protected root)"

    return True, str(ws), ""
