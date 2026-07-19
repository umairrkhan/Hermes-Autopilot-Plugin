"""Read-only Kanban adapter — never writes to the Kanban SQLite database."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from .base import BaseKanbanAdapter


class ReadOnlyKanbanAdapter(BaseKanbanAdapter):
    """Read adapter for Hermes Kanban board data.

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
        return self._hermes_home / "kanban.db"

    def list_tasks(self, board: str | None = None, limit: int = 20) -> list[dict[str, Any]]:
        """List tasks from a board (read-only)."""
        if not self._db_path.exists():
            return []
        try:
            conn = sqlite3.connect(f"file:{self._db_path}?mode=ro", uri=True)
            conn.row_factory = sqlite3.Row
            try:
                if board:
                    cursor = conn.execute(
                        "SELECT * FROM tasks WHERE board = ? ORDER BY created_at DESC LIMIT ?",
                        (board, limit),
                    )
                else:
                    cursor = conn.execute(
                        "SELECT * FROM tasks ORDER BY created_at DESC LIMIT ?",
                        (limit,),
                    )
                return [dict(row) for row in cursor.fetchall()]
            finally:
                conn.close()
        except Exception:
            return []

    def get_task(self, task_id: str) -> dict[str, Any] | None:
        """Get a single task (read-only)."""
        if not self._db_path.exists():
            return None
        try:
            conn = sqlite3.connect(f"file:{self._db_path}?mode=ro", uri=True)
            conn.row_factory = sqlite3.Row
            try:
                cursor = conn.execute(
                    "SELECT * FROM tasks WHERE id = ? LIMIT 1",
                    (task_id,),
                )
                row = cursor.fetchone()
                return dict(row) if row else None
            finally:
                conn.close()
        except Exception:
            return None
