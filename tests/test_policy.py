"""Tests for policy engine — risk classifier and capability engine."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from autopilot.policy import (
    classify_risk, check_capability, evaluate_action,
    is_denied_by_default, validate_lease_for_workspace,
)
from autopilot.constants import (
    RISK_LOW, RISK_MEDIUM, RISK_HIGH,
    CAP_WORKSPACE_WRITE, CAP_GIT_PUSH, CAP_SECRET_ACCESS,
    CAP_DEPLOYMENT, CAP_NEXT_PHASE,
)
from autopilot.lease import validate_lease

# ---------------------------------------------------------------------------
# Risk classification
# ---------------------------------------------------------------------------

class TestRiskClassifier:
    def test_low_risk_rename(self):
        r = classify_risk("rename the function to foobar")
        assert r.level == RISK_LOW
        assert r.action == "proceed_and_log"

    def test_low_risk_typo(self):
        r = classify_risk("fix the typo in the comment")
        assert r.level == RISK_LOW

    def test_low_risk_format(self):
        r = classify_risk("format the code")
        assert r.level == RISK_LOW

    def test_low_risk_documentation(self):
        r = classify_risk("update documentation")
        assert r.level == RISK_LOW

    def test_medium_risk_commit(self):
        r = classify_risk("commit the changes")
        assert r.level == RISK_MEDIUM
        assert r.action == "check_capability"

    def test_medium_risk_push(self):
        r = classify_risk("push to remote")
        assert r.level == RISK_MEDIUM

    def test_medium_risk_merge(self):
        r = classify_risk("merge the branch")
        assert r.level == RISK_MEDIUM

    def test_medium_risk_install(self):
        r = classify_risk("install the dependency")
        assert r.level == RISK_MEDIUM

    def test_high_risk_security(self):
        r = classify_risk("security audit of the auth system")
        assert r.level == RISK_HIGH
        assert r.action == "pause_for_human"

    def test_high_risk_deployment(self):
        r = classify_risk("deploy to production")
        assert r.level == RISK_HIGH

    def test_high_risk_data_loss(self):
        r = classify_risk("data loss prevention review")
        assert r.level == RISK_HIGH

    def test_high_risk_yolo(self):
        r = classify_risk("enable yolo mode")
        assert r.level == RISK_HIGH

    def test_high_risk_approval_off(self):
        r = classify_risk("set approvals.mode=off")
        assert r.level == RISK_HIGH

    def test_high_risk_migration(self):
        r = classify_risk("apply the migration")
        assert r.level == RISK_HIGH

    def test_high_risk_next_phase(self):
        r = classify_risk("start the next-phase work")
        assert r.level == RISK_HIGH

    def test_high_risk_privileged(self):
        r = classify_risk("privileged account access")
        assert r.level == RISK_HIGH

    def test_high_risk_unknown_action(self):
        """Unknown actions default to HIGH (fail-closed)."""
        r = classify_risk("do something completely unknown")
        assert r.level == RISK_HIGH
        assert "fail-closed" in r.reason.lower()

    def test_high_precedence_over_medium(self):
        """HIGH risk patterns take precedence over MEDIUM."""
        r = classify_risk("deploy the security fix")
        assert r.level == RISK_HIGH

    def test_medium_precedence_over_low(self):
        """MEDIUM risk patterns take precedence over LOW."""
        r = classify_risk("commit the formatting changes")
        assert r.level == RISK_MEDIUM


# ---------------------------------------------------------------------------
# Capability checking
# ---------------------------------------------------------------------------

class TestCapabilityEngine:
    def test_has_capability(self, sample_lease):
        lev = validate_lease(sample_lease)
        granted, msg = check_capability(lev, "workspace.read")
        assert granted is True
        assert msg == ""

    def test_missing_capability(self, sample_lease):
        lev = validate_lease(sample_lease)
        granted, msg = check_capability(lev, CAP_WORKSPACE_WRITE)
        assert granted is False
        assert "not granted" in msg

    def test_no_lease(self):
        granted, msg = check_capability(None, "workspace.read")
        assert granted is False
        assert "No active lease" in msg

    def test_expired_lease(self, expired_lease):
        lev = validate_lease(expired_lease)
        granted, msg = check_capability(lev, "workspace.read")
        assert granted is False
        assert "expired" in msg

    def test_default_denied(self):
        assert is_denied_by_default(CAP_WORKSPACE_WRITE)
        assert is_denied_by_default(CAP_GIT_PUSH)
        assert is_denied_by_default(CAP_SECRET_ACCESS)
        assert is_denied_by_default(CAP_DEPLOYMENT)
        assert is_denied_by_default(CAP_NEXT_PHASE)

    def test_read_not_denied_by_default(self):
        assert not is_denied_by_default("workspace.read")
        assert not is_denied_by_default("git.read")


# ---------------------------------------------------------------------------
# Full risk evaluation
# ---------------------------------------------------------------------------

class TestEvaluateAction:
    def test_low_always_proceeds(self):
        r = evaluate_action("fix the typo")
        assert r.level == RISK_LOW
        assert r.action == "proceed_and_log"

    def test_medium_with_granted_capability(self, sample_lease):
        lev = validate_lease(sample_lease)
        r = evaluate_action("commit the changes", lev, "workspace.read")
        assert r.action == "proceed_and_log"

    def test_medium_without_granted_capability(self, sample_lease):
        lev = validate_lease(sample_lease)
        r = evaluate_action("commit the changes", lev, CAP_WORKSPACE_WRITE)
        assert r.action == "pause_for_human"

    def test_high_always_pauses(self):
        r = evaluate_action("deploy to production")
        assert r.action == "pause_for_human"

    def test_no_lease_medium(self):
        """Without a lease, medium-risk action stays at medium (check_capability)."""
        r = evaluate_action("commit the changes")
        # "commit" matches MEDIUM risk pattern; without required_cap, stays MEDIUM
        assert r.action == "check_capability"


# ---------------------------------------------------------------------------
# Workspace validation
# ---------------------------------------------------------------------------

class TestLeaseWorkspaceValidation:
    def test_valid_workspace(self, sample_lease, tmp_workspace):
        lev = validate_lease(sample_lease)
        valid, err = validate_lease_for_workspace(lev, str(tmp_workspace))
        assert valid is True

    def test_empty_workspace_root(self, sample_lease, tmp_workspace):
        sample_lease["workspace_root"] = ""
        lev = validate_lease(sample_lease)
        valid, err = validate_lease_for_workspace(lev, str(tmp_workspace))
        assert valid is True  # no restriction = allowed

    def test_outside_workspace(self, sample_lease, tmp_path):
        sample_lease["workspace_root"] = str(tmp_path / "outside")
        lev = validate_lease(sample_lease)
        valid, err = validate_lease_for_workspace(lev, str(tmp_path / "inside"))
        assert valid is False
        assert "outside" in err
