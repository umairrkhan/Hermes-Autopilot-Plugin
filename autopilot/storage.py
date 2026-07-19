"""Plugin-owned durable state with profile-safe paths, atomic replacement,
cross-process locking, schema versioning, corruption detection, deterministic
restart, and project-scoped state isolation.

Each registered Hermes Project gets its own state file:

    $HERMES_HOME/state/autopilot/projects/<project_id>/autopilot_state.json

A small manifest stores the currently active project id. Existing callers can
continue using load_state()/save_state()/mutate_state(); those functions resolve
to the active project when one is selected, or to the legacy/global state file
when no project is active yet.
"""

from __future__ import annotations

import fcntl
import json
import logging
import os
import tempfile
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from .constants import (
    SCHEMA_VERSION, STATE_DIR_NAME, STATE_FILE_NAME,
    LOCK_FILE_SUFFIX, STATE_IDLE,
)

logger = logging.getLogger(__name__)
_LOCK = threading.RLock()
_MANIFEST_FILE_NAME = "manifest.json"
_PROJECTS_DIR_NAME = "projects"


def _hermes_home() -> Path:
    """Resolve HERMES_HOME or fall back to ~/.hermes."""
    raw = os.environ.get("HERMES_HOME", "").strip()
    return Path(raw).expanduser() if raw else Path.home() / ".hermes"


def _state_dir() -> Path:
    """Return the plugin state directory under HERMES_HOME/state/."""
    return _hermes_home() / "state" / STATE_DIR_NAME


def _projects_dir() -> Path:
    """Return the directory that contains project-scoped states."""
    return _state_dir() / _PROJECTS_DIR_NAME


def _manifest_file() -> Path:
    """Return the project manifest path."""
    return _state_dir() / _MANIFEST_FILE_NAME


def _sanitize_project_id(project_id: str) -> str:
    """Validate a project id is safe to use as a path segment."""
    project_id = (project_id or "").strip()
    if not project_id:
        raise ValueError("project_id must be non-empty")
    if "/" in project_id or "\\" in project_id or project_id in {".", ".."}:
        raise ValueError(f"Unsafe project_id path segment: {project_id!r}")
    return project_id


def _project_state_dir(project_id: str) -> Path:
    """Return the state directory for a specific project id."""
    return _projects_dir() / _sanitize_project_id(project_id)


def _project_state_file(project_id: str) -> Path:
    """Return the state file for a specific project id."""
    return _project_state_dir(project_id) / STATE_FILE_NAME


def _legacy_state_file() -> Path:
    """Return the pre-project-scoping state path used before registration."""
    return _state_dir() / STATE_FILE_NAME


def get_active_project_id() -> str | None:
    """Return the active project id, if any."""
    path = _manifest_file()
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    pid = data.get("active_project_id")
    return str(pid) if pid else None


def set_active_project_id(project_id: str) -> None:
    """Persist the active project id in the manifest."""
    pid = _sanitize_project_id(project_id)
    path = _manifest_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps({"schema_version": SCHEMA_VERSION, "active_project_id": pid}, indent=2, sort_keys=True) + "\n"
    _write_text_atomic(path, payload)


def list_projects() -> list[str]:
    """List known project ids with state directories."""
    root = _projects_dir()
    if not root.exists():
        return []
    return sorted(p.name for p in root.iterdir() if p.is_dir() and (p / STATE_FILE_NAME).exists())


def _state_file(project_id: str | None = None) -> Path:
    """Return the current state file path.

    If project_id is provided, use that project. If not, use the active project
    from manifest. If no active project exists yet, use the legacy/global state
    file so pre-registration commands still work.
    """
    pid = project_id or get_active_project_id()
    if pid:
        return _project_state_file(pid)
    return _legacy_state_file()


# Alias for test compatibility
_state_path = _state_file


def _lock_file(project_id: str | None = None) -> Path:
    """Return the cross-process lock file path for the selected state."""
    return _state_file(project_id).with_suffix(LOCK_FILE_SUFFIX)


@contextmanager
def _state_guard(project_id: str | None = None):
    """Cross-process + cross-thread lock for state mutations."""
    with _LOCK:
        lock_path = _lock_file(project_id)
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with lock_path.open("a+", encoding="utf-8") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def default_state() -> dict[str, Any]:
    """Return a fresh default state dict."""
    return {
        "schema_version": SCHEMA_VERSION,
        "state": STATE_IDLE,
        "registration": None,
        "lease": None,
        "loop_iteration": 0,
        "max_loop_iterations": 1,
        "transition_history": [],
        "run_count": 0,
        "last_error": None,
        "kill_switch_active": False,
    }


def _validate_state(data: Any) -> tuple[dict[str, Any], bool]:
    """Validate loaded state. Returns (validated_state, was_migrated)."""
    if not isinstance(data, dict):
        raise ValueError("State root must be a dict")

    migrated = False

    sv = data.get("schema_version")
    if sv is None:
        data["schema_version"] = SCHEMA_VERSION
        migrated = True
    elif not isinstance(sv, int) or sv < 1:
        raise ValueError(f"Unsupported schema_version: {sv}")

    if isinstance(data.get("schema_version"), int) and data["schema_version"] > SCHEMA_VERSION:
        raise ValueError(
            f"State schema_version {data['schema_version']} is newer than "
            f"supported version {SCHEMA_VERSION}. Cannot read."
        )

    state_label = data.get("state", STATE_IDLE)
    from .constants import ALL_STATES
    if state_label not in ALL_STATES:
        raise ValueError(f"Invalid state: {state_label}")

    defaults = default_state()
    merged = {**defaults, **data}

    if not isinstance(merged.get("transition_history"), list):
        merged["transition_history"] = []

    if not isinstance(merged.get("run_count"), int) or merged["run_count"] < 0:
        merged["run_count"] = 0

    if not isinstance(merged.get("loop_iteration"), int) or merged["loop_iteration"] < 0:
        merged["loop_iteration"] = 0

    if not isinstance(merged.get("kill_switch_active"), bool):
        merged["kill_switch_active"] = False

    return merged, migrated


def _fail_closed_state(exc: Exception) -> dict[str, Any]:
    """Build a fail-closed state after corruption detection."""
    state = default_state()
    state["kill_switch_active"] = True
    state["last_error"] = f"State corruption detected: {type(exc).__name__}: {exc}"
    return state


def _load_state_unlocked(project_id: str | None = None) -> tuple[dict[str, Any], bool]:
    """Load state from disk (must be called under _state_guard)."""
    path = _state_file(project_id)
    dirty = False

    if not path.exists():
        state = default_state()
        if project_id:
            state["project_id"] = project_id
        return state, True

    try:
        raw = path.read_text(encoding="utf-8")
        if not raw.strip():
            raise ValueError("State file is empty")
        data = json.loads(raw)
        state, migrated = _validate_state(data)
        if project_id and state.get("project_id") != project_id:
            state["project_id"] = project_id
            migrated = True
        return state, dirty or migrated
    except json.JSONDecodeError as exc:
        logger.error("Corrupted state file, failing closed: %s", exc)
        return _fail_closed_state(exc), True
    except ValueError as exc:
        logger.error("Invalid state, failing closed: %s", exc)
        return _fail_closed_state(exc), True
    except Exception as exc:
        logger.error("Unexpected error loading state, failing closed: %s", exc)
        return _fail_closed_state(exc), True


def _write_text_atomic(path: Path, payload: str) -> None:
    """Write a text payload atomically."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
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


def _write_state_unlocked(state: dict[str, Any], project_id: str | None = None) -> None:
    """Write state to disk atomically (must be called under _state_guard)."""
    path = _state_file(project_id)
    payload = json.dumps(state, indent=2, sort_keys=True) + "\n"
    _write_text_atomic(path, payload)


def load_project_state(project_id: str) -> dict[str, Any]:
    """Load a specific project's isolated state."""
    pid = _sanitize_project_id(project_id)
    with _state_guard(pid):
        state, dirty = _load_state_unlocked(pid)
        if dirty:
            _write_state_unlocked(state, pid)
        return dict(state)


def save_project_state(project_id: str, state: dict[str, Any]) -> None:
    """Save a specific project's isolated state."""
    pid = _sanitize_project_id(project_id)
    validated, _ = _validate_state(dict(state))
    validated["project_id"] = pid
    with _state_guard(pid):
        _write_state_unlocked(validated, pid)


def mutate_project_state(project_id: str, mutator: Any) -> dict[str, Any]:
    """Atomically read-modify-write a specific project's state."""
    pid = _sanitize_project_id(project_id)
    with _state_guard(pid):
        state, _ = _load_state_unlocked(pid)
        mutator(state)
        validated, _ = _validate_state(state)
        validated["project_id"] = pid
        _write_state_unlocked(validated, pid)
        return dict(validated)


def reset_project_state(project_id: str) -> dict[str, Any]:
    """Reset a specific project's state to defaults."""
    pid = _sanitize_project_id(project_id)
    with _state_guard(pid):
        state = default_state()
        state["project_id"] = pid
        _write_state_unlocked(state, pid)
        return dict(state)


def load_state() -> dict[str, Any]:
    """Load active project state, or legacy global state before registration."""
    pid = get_active_project_id()
    if pid:
        return load_project_state(pid)
    with _state_guard():
        state, dirty = _load_state_unlocked()
        if dirty:
            _write_state_unlocked(state)
        return dict(state)


def save_state(state: dict[str, Any]) -> None:
    """Save active project state, or legacy global state before registration."""
    pid = get_active_project_id()
    if pid:
        save_project_state(pid, state)
        return
    validated, _ = _validate_state(dict(state))
    with _state_guard():
        _write_state_unlocked(validated)


def mutate_state(mutator: Any) -> dict[str, Any]:
    """Atomically read-modify-write active project state."""
    pid = get_active_project_id()
    if pid:
        return mutate_project_state(pid, mutator)
    with _state_guard():
        state, _ = _load_state_unlocked()
        mutator(state)
        validated, _ = _validate_state(state)
        _write_state_unlocked(validated)
        return dict(validated)


def reset_state() -> dict[str, Any]:
    """Reset active project state to defaults."""
    pid = get_active_project_id()
    if pid:
        return reset_project_state(pid)
    with _state_guard():
        state = default_state()
        _write_state_unlocked(state)
        return dict(state)
