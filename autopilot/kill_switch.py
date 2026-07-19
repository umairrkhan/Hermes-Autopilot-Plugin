"""Kill switch — works even when run state is malformed.

The kill switch operates independently of the state machine. It writes a
simple boolean flag that is checked before any state-dependent operation.
Even if the JSON state file is corrupted, truncated, or missing, the kill
switch can be detected and honored.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import fcntl

logger = logging.getLogger(__name__)
_LOCK = threading.RLock()


def _kill_switch_file() -> Path:
    """Path to the kill switch file (separate from main state)."""
    raw = os.environ.get("HERMES_HOME", "").strip()
    home = Path(raw).expanduser() if raw else Path.home() / ".hermes"
    return home / "state" / "autopilot" / "kill_switch.json"


# Alias for test compatibility
_kill_switch_path = _kill_switch_file


@contextmanager
def _kill_guard():
    """Cross-process lock for kill switch file."""
    with _LOCK:
        path = _kill_switch_file()
        lock_path = path.with_suffix(".lock")
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with lock_path.open("a+", encoding="utf-8") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def is_kill_switch_active() -> bool:
    """Check if the kill switch is active.

    This works even if the main state file is corrupted or missing.
    It reads a separate, minimal file.
    """
    with _kill_guard():
        path = _kill_switch_file()
        if not path.exists():
            return False
        try:
            raw = path.read_text(encoding="utf-8")
            if not raw.strip():
                return False
            data = json.loads(raw)
            return bool(data.get("active", False))
        except Exception:
            # Any corruption = treat as active (fail-closed)
            logger.warning("Kill switch file corrupted, treating as active")
            return True


def _write_kill_switch(active: bool, reason: str = "") -> None:
    """Write the kill switch state (must be called under _kill_guard)."""
    path = _kill_switch_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {"active": active, "reason": reason}
    payload = json.dumps(data, indent=2) + "\n"

    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
    finally:
        try:
            if os.path.exists(tmp_name):
                os.unlink(tmp_name)
        except OSError:
            pass


def activate_kill_switch(reason: str = "Manual activation") -> None:
    """Activate the kill switch."""
    with _kill_guard():
        _write_kill_switch(True, reason)
    logger.warning("Kill switch activated: %s", reason)


def deactivate_kill_switch(reason: str = "Manual deactivation") -> None:
    """Deactivate the kill switch."""
    with _kill_guard():
        _write_kill_switch(False, reason)
    logger.info("Kill switch deactivated: %s", reason)


def check_kill_switch() -> str | None:
    """Check kill switch and return error message if active, None if clear.

    This is the primary guard function — call before any state-dependent work.
    """
    if is_kill_switch_active():
        path = _kill_switch_file()
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            reason = data.get("reason", "Unknown reason")
        except Exception:
            reason = "Kill switch active (could not read reason)"
        return reason
    return None
