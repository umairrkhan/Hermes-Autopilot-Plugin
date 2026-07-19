"""Abstract adapter interfaces."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any


class BaseSessionAdapter(ABC):
    """Read-only interface to Hermes sessions.

    Phase 1: read-only only. Never writes to session databases.
    """

    @abstractmethod
    def get_session_info(self, session_id: str) -> dict[str, Any]:
        """Return read-only info about a session."""
        ...

    @abstractmethod
    def list_sessions(self, limit: int = 10) -> list[dict[str, Any]]:
        """List recent sessions (read-only)."""
        ...


class BaseKanbanAdapter(ABC):
    """Read-only interface to the Kanban board.

    Phase 1: read-only only. Never writes to the kanban database.
    """

    @abstractmethod
    def list_tasks(self, board: str | None = None, limit: int = 20) -> list[dict[str, Any]]:
        """List tasks from a board (read-only)."""
        ...

    @abstractmethod
    def get_task(self, task_id: str) -> dict[str, Any] | None:
        """Get a single task (read-only)."""
        ...
