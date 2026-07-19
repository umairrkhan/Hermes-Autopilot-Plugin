"""Read-only session adapter — never writes Hermes SQLite databases."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from .base import BaseSessionAdapter


class ReadOnlySessionAdapter(BaseSessionAdapter):
    """Read adapter for Hermes session data.

    Uses SQLite read_only mode. Never writes to the database.
    """

    def __init__(self, hermes_home: str | Path | None = None):
        if hermes_home is None:
            import os
            raw = os.environ.get("HERMES_HOME", "").strip()
            self._hermes_home = Path(raw).expanduser() if raw else Path.home() / ".hermes"
        else:
            self._hermes_home = Path(hermes_home).expanduser()

    @property
    def _db_path(self) -> Path:
        return self._hermes_home / "state.db"

    def get_session_info(self, session_id: str) -> dict[str, Any]:
        """Return read-only info about a session."""
        if not self._db_path.exists():
            return {"error": "Session database not found"}
        try:
            conn = sqlite3.connect(f"file:{self._db_path}?mode=ro", uri=True)
            conn.row_factory = sqlite3.Row
            try:
                cursor = conn.execute(
                    "SELECT * FROM sessions WHERE id = ? LIMIT 1",
                    (session_id,),
                )
                row = cursor.fetchone()
                if row:
                    return dict(row)
                return {"error": f"Session {session_id} not found"}
            finally:
                conn.close()
        except Exception as exc:
            return {"error": str(exc)}

    def list_sessions(self, limit: int = 10) -> list[dict[str, Any]]:
        """List recent sessions (read-only)."""
        if not self._db_path.exists():
            return []
        try:
            conn = sqlite3.connect(f"file:{self._db_path}?mode=ro", uri=True)
            conn.row_factory = sqlite3.Row
            try:
                cursor = conn.execute(
                    "SELECT id, title, created_at FROM sessions ORDER BY created_at DESC LIMIT ?",
                    (limit,),
                )
                return [dict(row) for row in cursor.fetchall()]
            finally:
                conn.close()
        except Exception:
            return []
