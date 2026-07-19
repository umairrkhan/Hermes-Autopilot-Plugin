"""Tests for audit trail — secret/PII-safe logging."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

from autopilot.audit import (
    log_event, get_audit_log_path, _redact_value,
)

# ---------------------------------------------------------------------------
# Redaction
# ---------------------------------------------------------------------------

class TestRedaction:
    def test_redact_email(self):
        result = _redact_value("user@example.com is the email")
        assert "user@example.com" not in result
        assert "[REDACTED" in result

    def test_redact_phone(self):
        result = _redact_value("Call +1-555-123-4567")
        assert "+1-555-123-4567" not in result
        assert "[REDACTED" in result

    def test_redact_ssn(self):
        result = _redact_value("SSN: 123-45-6789")
        assert "123-45-6789" not in result
        assert "[REDACTED" in result

    def test_redact_api_key(self):
        result = _redact_value("api_key=OPENAI_SECRET")
        assert "OPENAI_SECRET" not in result
        assert "[REDACTED" in result

    def test_redact_token(self):
        result = _redact_value("Bearer tok_12345abcdef")
        assert "tok_12345abcdef" not in result
        assert "[REDACTED" in result

    def test_no_redaction_needed(self):
        result = _redact_value("just a normal string")
        assert result == "just a normal string"

    def test_redact_password(self):
        result = _redact_value("password: hunter2")
        assert "hunter2" not in result
        assert "[REDACTED" in result

    def test_redact_secret(self):
        result = _redact_value("secret_key: abcdef123456")
        assert "abcdef123456" not in result
        assert "[REDACTED" in result

    def test_empty_string(self):
        result = _redact_value("")
        assert result == ""

    def test_redact_nested_dict(self):
        data = {"message": "user@test.com sent email", "count": 5}
        result = _redact_value(data)
        assert isinstance(result, dict)
        assert "user@test.com" not in result["message"]


# ---------------------------------------------------------------------------
# Audit log writing
# ---------------------------------------------------------------------------

class TestAuditLog:
    def test_log_event_creates_file(self, tmp_hermes_home):
        log_event("test_event", "STATE_A", "STATE_B", "test detail")
        path = get_audit_log_path()
        assert path.exists()

    def test_log_event_is_jsonl(self, tmp_hermes_home):
        log_event("event1", "A", "B", "detail1")
        log_event("event2", "B", "C", "detail2")
        path = get_audit_log_path()
        lines = path.read_text().strip().split("\n")
        assert len(lines) == 2
        for line in lines:
            data = json.loads(line)
            assert "event_type" in data
            assert "timestamp" in data
            assert "state_from" in data
            assert "state_to" in data

    def test_log_event_no_raw_content(self, tmp_hermes_home):
        """Audit events must never contain raw chat content."""
        log_event("test", "A", "B", "normal detail")
        path = get_audit_log_path()
        content = path.read_text()
        # Should not contain chat markers
        assert "```" not in content  # no code blocks from chat
        assert "ASSISTANT:" not in content
        assert "USER:" not in content

    def test_log_event_with_sensitive_detail(self, tmp_hermes_home):
        log_event("test", "A", "B", "user email: user@test.com")
        path = get_audit_log_path()
        content = path.read_text()
        assert "user@test.com" not in content

    def test_audit_log_path_location(self, tmp_hermes_home):
        path = get_audit_log_path()
        assert str(tmp_hermes_home) in str(path)
        assert path.name == "audit.jsonl"

    def test_log_event_records_timestamp(self, tmp_hermes_home):
        log_event("timed_event", "A", "B")
        path = get_audit_log_path()
        line = path.read_text().strip().split("\n")[-1]
        data = json.loads(line)
        assert "T" in data["timestamp"] or "Z" in data["timestamp"]  # ISO format
