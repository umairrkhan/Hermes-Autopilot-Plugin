"""Secret/PII-safe audit trail without storing raw chat content.

Audit events are written to a JSONL file under the autopilot state directory.
Each event has a timestamp, event_type, state transition, and sanitized detail.
Raw chat content, secrets, and PII are never stored. Redaction failures
omit content rather than exposing it.
"""

from __future__ import annotations

import json
import logging
import os
import re
import tempfile
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import fcntl

from .constants import AUDIT_FILE_NAME, MAX_AUDIT_ROTATION_BYTES

logger = logging.getLogger(__name__)
_LOCK = threading.RLock()

# Patterns that look like secrets or PII — strip from audit
_SECRET_PATTERNS = (
    re.compile(r"sk-[a-zA-Z0-9_-]{8,}"),  # API keys (shortened min for flexibility)
    re.compile(r"ghp_[a-zA-Z0-9_-]+"),  # GitHub tokens
    re.compile(r"xoxb-[a-zA-Z0-9_-]+"),  # Slack tokens
    re.compile(r"tok_[a-zA-Z0-9_-]+"),  # Generic tokens
    re.compile(r"Bearer\s+\S+", re.IGNORECASE),  # Bearer tokens (any length)
    re.compile(r"\b\d{3}-\d{2}-\d{4}\b"),  # SSN-like
    re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b"),  # email-like
    re.compile(r"\b\+?\d{1,3}[-.\s]?\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}\b"),  # phone
    re.compile(r"\bpassword\s*[=:]\s*\S+", re.IGNORECASE),  # password values
    re.compile(r"\bsecret[_\s]*(?:key|token)?\s*[=:]\s*\S+", re.IGNORECASE),  # secret key/token
    re.compile(r"\bapi[_\s]*key\s*[=:]\s*\S+", re.IGNORECASE),  # api_key assignments
)


def _redact(text: str) -> str:
    """Redact secrets and PII from text. On failure, return empty string."""
    if not isinstance(text, str):
        return ""
    try:
        result = text
        for pattern in _SECRET_PATTERNS:
            result = pattern.sub("[REDACTED]", result)
        return result
    except Exception:
        return ""


def redact_text(text: str, *, max_length: int = 4000) -> str:
    """Return bounded secret/PII-redacted text for durable evidence."""

    if not isinstance(max_length, int) or max_length < 0:
        max_length = 0
    return _redact(text)[:max_length]


def _audit_file() -> Path:
    """Path to the audit JSONL file."""
    raw = os.environ.get("HERMES_HOME", "").strip()
    home = Path(raw).expanduser() if raw else Path.home() / ".hermes"
    return home / "state" / "autopilot" / AUDIT_FILE_NAME


@contextmanager
def _audit_guard():
    """Cross-process lock for audit writes."""
    with _LOCK:
        path = _audit_file()
        lock_path = path.with_suffix(".lock")
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with lock_path.open("a+", encoding="utf-8") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def log_event(
    event_type: str,
    state_from: str = "",
    state_to: str = "",
    detail: str = "",
    lease_id: str = "",
    risk_level: str = "",
    loop_iteration: int = 0,
) -> None:
    """Log an audit event. All fields are sanitized before storage."""
    now = datetime.now(timezone.utc).isoformat()
    event = {
        "timestamp": now,
        "event_type": str(event_type)[:200],
        "state_from": str(state_from)[:50],
        "state_to": str(state_to)[:50],
        "detail": _redact(str(detail))[:500],
        "lease_id": str(lease_id)[:100],
        "risk_level": str(risk_level)[:20],
        "loop_iteration": int(loop_iteration),
    }
    line = json.dumps(event, sort_keys=True) + "\n"

    with _audit_guard():
        path = _audit_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        # Rotate if too large
        if path.exists():
            try:
                size = path.stat().st_size
                if size > MAX_AUDIT_ROTATION_BYTES:
                    rotated = path.with_suffix(".jsonl.1")
                    if rotated.exists():
                        rotated.unlink()
                    path.rename(rotated)
            except OSError:
                pass

        with path.open("a", encoding="utf-8") as f:
            f.write(line)
            f.flush()


def _redact_value(text: Any) -> Any:
    """Redact secrets and PII from text. On failure, return empty string.

    Accepts strings, dicts, or any JSON-serializable value.
    """
    if isinstance(text, dict):
        return {k: _redact_value(v) for k, v in text.items()}
    if isinstance(text, list):
        return [_redact_value(item) for item in text]
    return _redact(str(text))


# Alias for test compatibility
redact = _redact
redact_value = _redact_value


def get_audit_log_path() -> Path:
    """Public accessor for the audit log path."""
    return _audit_file()


def read_audit_trail(limit: int = 100) -> list[dict[str, Any]]:
    """Read the most recent audit events (read-only)."""
    path = _audit_file()
    if not path.exists():
        return []
    try:
        lines = path.read_text(encoding="utf-8").strip().split("\n")
        events = []
        for line in lines[-limit:]:
            if line.strip():
                try:
                    events.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        return events
    except Exception:
        return []


def clear_audit_trail() -> None:
    """Clear the audit trail (for testing only)."""
    with _audit_guard():
        path = _audit_file()
        if path.exists():
            path.unlink()
