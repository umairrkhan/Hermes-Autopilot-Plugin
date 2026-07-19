"""Security and prohibited-pattern scan tests.

These tests verify that no dangerous patterns exist in the codebase:
- No shell=True, os.system, eval, exec, pickle
- No direct writes to Hermes session/Kanban databases
- No secret or PII fixtures
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
AUTOPILOT_DIR = PROJECT_ROOT / "autopilot"
TESTS_DIR = PROJECT_ROOT / "tests"
THIS_FILE = Path(__file__).resolve()


def _find_python_files() -> list[Path]:
    """Find all .py files in autopilot/ and tests/ (excluding this file)."""
    files = []
    for d in [AUTOPILOT_DIR, TESTS_DIR]:
        if d.exists():
            for f in d.rglob("*.py"):
                if f.resolve() != THIS_FILE:
                    files.append(f)
    return files


# ---------------------------------------------------------------------------
# Prohibited pattern scan
# ---------------------------------------------------------------------------

class TestProhibitedPatterns:
    def test_no_shell_true(self):
        files = _find_python_files()
        violations = []
        for f in files:
            content = f.read_text(errors="ignore")
            for line_num, line in enumerate(content.splitlines(), 1):
                stripped = line.strip()
                if stripped.startswith("#"):
                    continue
                if "shell=True" in stripped:
                    violations.append(f"{f.name}:{line_num}: {stripped}")
        assert not violations, f"shell=True found: {violations}"

    def test_no_os_system(self):
        files = _find_python_files()
        violations = []
        for f in files:
            content = f.read_text(errors="ignore")
            for line_num, line in enumerate(content.splitlines(), 1):
                stripped = line.strip()
                if stripped.startswith("#"):
                    continue
                if "os.system(" in stripped:
                    violations.append(f"{f.name}:{line_num}: {stripped}")
        assert not violations, f"os.system found: {violations}"

    def test_no_eval(self):
        files = _find_python_files()
        violations = []
        for f in files:
            content = f.read_text(errors="ignore")
            for line_num, line in enumerate(content.splitlines(), 1):
                stripped = line.strip()
                if stripped.startswith("#"):
                    continue
                # Skip string literals that mention eval as a pattern name
                if "eval(" in stripped and "re.compile" not in stripped:
                    violations.append(f"{f.name}:{line_num}: {stripped}")
        assert not violations, f"eval() found: {violations}"

    def test_no_exec(self):
        files = _find_python_files()
        violations = []
        for f in files:
            content = f.read_text(errors="ignore")
            for line_num, line in enumerate(content.splitlines(), 1):
                stripped = line.strip()
                if stripped.startswith("#"):
                    continue
                if "exec(" in stripped and "re.compile" not in stripped:
                    violations.append(f"{f.name}:{line_num}: {stripped}")
        assert not violations, f"exec() found: {violations}"

    def test_no_pickle(self):
        files = _find_python_files()
        violations = []
        for f in files:
            content = f.read_text(errors="ignore")
            for line_num, line in enumerate(content.splitlines(), 1):
                stripped = line.strip()
                if stripped.startswith("#"):
                    continue
                # Skip string literals in regex patterns
                if "re.compile" in stripped:
                    continue
                if "pickle.dump" in stripped or "pickle.loads" in stripped or "pickle.load(" in stripped:
                    violations.append(f"{f.name}:{line_num}: {stripped}")
        assert not violations, f"pickle found: {violations}"

    def test_no_direct_db_writes(self):
        """Ensure production SQLite connections always use read_only mode."""
        files = list(AUTOPILOT_DIR.rglob("*.py"))
        violations = []
        for f in files:
            content = f.read_text(errors="ignore")
            for line_num, line in enumerate(content.splitlines(), 1):
                stripped = line.strip()
                if stripped.startswith("#"):
                    continue
                # Check for sqlite3.connect without mode=ro
                if "sqlite3.connect(" in stripped:
                    if "mode=ro" not in stripped and "mode=\"ro\"" not in stripped:
                        violations.append(f"{f.name}:{line_num}: {stripped}")
        assert not violations, f"SQLite without read_only: {violations}"


# ---------------------------------------------------------------------------
# Secret/PII fixture scan
# ---------------------------------------------------------------------------

class TestNoSecretFixtures:
    def test_no_api_keys_in_test_data(self):
        files = _find_python_files()
        violations = []
        secret_patterns = {
            "possible API key": re.compile(
                r"(?<![A-Za-z0-9_])sk-[A-Za-z0-9_-]{16,}"
            ),
            "possible GitHub token": re.compile(
                r"(?<![A-Za-z0-9_])ghp_[A-Za-z0-9_-]{16,}"
            ),
            "possible Slack token": re.compile(
                r"(?<![A-Za-z0-9_])xoxb-[A-Za-z0-9_-]{16,}"
            ),
        }
        for f in files:
            content = f.read_text(errors="ignore")
            for line_num, line in enumerate(content.splitlines(), 1):
                stripped = line.strip()
                if stripped.startswith("#"):
                    continue
                # Skip the scanner/redaction regex definitions themselves.
                if "re.compile" in stripped:
                    continue
                for label, pattern in secret_patterns.items():
                    if pattern.search(stripped):
                        violations.append(f"{f.name}:{line_num}: {label}")
        assert not violations, f"Possible secrets found: {violations}"

    def test_no_email_in_fixtures(self):
        """Test fixtures should not contain real email addresses."""
        files = _find_python_files()
        email_pattern = re.compile(r'[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}')
        # Known safe test-only emails
        safe_emails = {"user@example.com", "user@test.com"}
        violations = []
        for f in files:
            if "conftest" in f.name:
                continue
            content = f.read_text(errors="ignore")
            for line_num, line in enumerate(content.splitlines(), 1):
                # Skip regex pattern definitions
                if "re.compile" in line:
                    continue
                matches = email_pattern.findall(line)
                for m in matches:
                    if m not in safe_emails:
                        violations.append(f"{f.name}:{line_num}: {m}")
        assert not violations, f"Real emails in fixtures: {violations}"


# ---------------------------------------------------------------------------
# Workspace boundary enforcement
# ---------------------------------------------------------------------------

class TestWorkspaceBoundary:
    def test_registration_rejects_home(self, tmp_hermes_home):
        from autopilot.registration import validate_workspace_path
        valid, _, _ = validate_workspace_path(str(Path.home()))
        assert valid is False

    def test_registration_rejects_hermes_home(self, tmp_hermes_home):
        from autopilot.registration import validate_workspace_path
        valid, _, _ = validate_workspace_path(str(tmp_hermes_home))
        assert valid is False

    def test_lease_validates_workspace_scope(self, tmp_workspace):
        from autopilot.policy import validate_lease_for_workspace
        from autopilot.lease import validate_lease
        lease_data = {
            "lease_id": "test", "lease_version": 1, "project_id": "p",
            "scope": "test", "created_at": "2026-01-01T00:00:00Z",
            "expiry": "2099-01-01T00:00:00Z",
            "max_runtime_seconds": 3600, "max_loop_iterations": 1,
            "max_budget_cents": 0, "granted_capabilities": [],
            "workspace_root": str(tmp_workspace),
        }
        lev = validate_lease(lease_data)
        valid, _ = validate_lease_for_workspace(lev, str(tmp_workspace))
        assert valid is True

    def test_lease_blocks_outside_workspace(self, tmp_workspace, tmp_path):
        from autopilot.policy import validate_lease_for_workspace
        from autopilot.lease import validate_lease
        outside = tmp_path / "outside"
        outside.mkdir()
        lease_data = {
            "lease_id": "test", "lease_version": 1, "project_id": "p",
            "scope": "test", "created_at": "2026-01-01T00:00:00Z",
            "expiry": "2099-01-01T00:00:00Z",
            "max_runtime_seconds": 3600, "max_loop_iterations": 1,
            "max_budget_cents": 0, "granted_capabilities": [],
            "workspace_root": str(tmp_workspace),
        }
        lev = validate_lease(lease_data)
        valid, _ = validate_lease_for_workspace(lev, str(outside))
        assert valid is False
