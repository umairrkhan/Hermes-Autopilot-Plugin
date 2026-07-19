"""Tests for autonomous development session loop foundation."""

from __future__ import annotations

import json
from pathlib import Path

from autopilot.adapters.autonomous_loop import AutonomousLoopSupervisor
from autopilot.commands import handle_autopilot_command
from autopilot.question_policy import Question, decide_question
from autopilot.storage import load_state


def _register(sample_registration: dict) -> None:
    result = str(handle_autopilot_command(f"register {json.dumps(sample_registration)}"))
    assert "Registered" in result


def _approved_brief(sample_registration: dict) -> str:
    _register(sample_registration)
    assert "Lease approved" in str(handle_autopilot_command("lease approve phase2-readonly"))
    brief_output = str(handle_autopilot_command("brief"))
    brief_id = ""
    for line in brief_output.splitlines():
        if line.startswith("Brief ID:"):
            brief_id = line.split(":", 1)[1].strip()
            break
    assert brief_id
    assert "Lease approved" in str(handle_autopilot_command("lease approve autonomous-development"))
    assert "approved" in str(handle_autopilot_command(f"approve {brief_id}")).lower()
    return brief_id


class TestAutonomousDevelopmentPreset:
    def test_request_autonomous_development_shows_allow_session_scope(self, tmp_hermes_home, sample_registration):
        _register(sample_registration)

        result = str(handle_autopilot_command("lease request autonomous-development"))

        assert "Autonomous development session" in result
        assert "[x] workspace.write" in result
        assert "[x] auto-select recommended low-risk choices" in result
        assert "[x] git.commit" in result
        assert "[x] git.push" in result
        assert "[ ] deployment" in result
        assert load_state().get("lease") is None

    def test_approve_autonomous_development_creates_promotion_lease(self, tmp_hermes_home, sample_registration):
        _register(sample_registration)

        result = str(handle_autopilot_command("lease approve autonomous-development"))
        lease = load_state()["lease"]

        assert "Autonomous development session" in result
        assert lease["granted_capabilities"] == [
            "workspace.read",
            "git.read",
            "workspace.write",
            "git.commit",
            "git.push",
            "next-phase",
            "user.interaction",
        ]
        assert lease["git_policy"] == "allow-list"
        assert lease["external_write_policy"] == "deny"


class TestQuestionPolicy:
    def test_auto_selects_explicit_recommended_low_risk_choice(self):
        decision = decide_question(Question(
            question_id="q-001",
            text="Which style should I use?",
            category="routine_low_risk",
            choices=("Use custom formatting", "Use existing project style"),
            recommended_choice="Use existing project style",
            context="format low risk local reversible",
        ))

        assert decision.action == "auto_answer"
        assert decision.selected_choice == "Use existing project style"
        assert "recommended" in decision.reason.lower()

    def test_auto_answers_all_questions_with_recommended(self):
        """User granted blanket permission — all questions auto-answer."""
        # Business/security question — now auto-answers
        decision = decide_question(Question(
            question_id="q-002",
            text="Should we change customer pricing and auth policy?",
            category="business_rule",
            choices=("Keep old", "Use recommended"),
            recommended_choice="Use recommended",
            context="business pricing security auth",
        ))
        assert decision.action == "auto_answer"
        assert decision.selected_choice == "Use recommended"

    def test_falls_back_to_first_choice_when_no_recommended(self):
        """No recommended choice but choices exist — uses first."""
        decision = decide_question(Question(
            question_id="q-003",
            text="Which local formatting style should I use?",
            category="routine_low_risk",
            choices=("A", "B"),
            recommended_choice="",
            context="local reversible formatting",
        ))
        assert decision.action == "auto_answer"
        assert decision.selected_choice == "A"

    def test_auto_answers_architecture_and_privacy_questions(self):
        """Architecture/privacy — user said auto-answer everything."""
        for text in (
            "Use the recommended framework architecture?",
            "Apply the recommended privacy requirement?",
        ):
            decision = decide_question(Question(
                question_id="q-sensitive",
                text=text,
                category="architecture",
                choices=("Keep current", "Use recommended"),
                recommended_choice="Use recommended",
                context="recommended local reversible",
            ))
            assert decision.action == "auto_answer"
            assert decision.selected_choice == "Use recommended"

    def test_auto_answers_questions_without_category(self):
        """Missing category — still auto-answers when recommended exists."""
        decision = decide_question(Question(
            question_id="q-untyped",
            text="Use the recommended local style?",
            choices=("Keep", "Use recommended"),
            recommended_choice="Use recommended",
            context="local reversible",
        ))
        assert decision.action == "auto_answer"
        assert decision.selected_choice == "Use recommended"


class TestAutonomousLoopCommands:
    def test_loop_start_requires_approved_brief_and_autonomous_lease(self, tmp_hermes_home, sample_registration):
        _register(sample_registration)

        result = str(handle_autopilot_command("loop start missing-brief"))

        assert "blocked" in result.lower()
        assert "brief" in result.lower()

    def test_loop_start_creates_project_scoped_supervisor_artifact(self, tmp_hermes_home, sample_registration):
        brief_id = _approved_brief(sample_registration)

        result = str(handle_autopilot_command(f"loop start {brief_id}"))

        assert "Autonomous Development Loop Started" in result
        assert "Mode: supervised-development" in result
        assert "Status: WAITING_FOR_DEVELOPMENT_EXECUTOR" in result
        assert "Loop artifact:" in result

        loops_dir = Path(tmp_hermes_home) / "state" / "autopilot" / "projects" / sample_registration["project_id"] / "loops"
        loop_files = list(loops_dir.glob("loop_*.json"))
        assert len(loop_files) == 1
        payload = json.loads(loop_files[0].read_text())
        assert payload["project_id"] == sample_registration["project_id"]
        assert payload["brief_id"] == brief_id
        assert payload["auto_answer_policy"] == "always_recommended"
        assert payload["status"] == "WAITING_FOR_DEVELOPMENT_EXECUTOR"

        second = str(handle_autopilot_command(f"loop start {brief_id}"))
        assert "loop limit reached" in second.lower()
        assert len(list(loops_dir.glob("loop_*.json"))) == 1

    def test_loop_status_lists_active_project_loops(self, tmp_hermes_home, sample_registration):
        brief_id = _approved_brief(sample_registration)
        handle_autopilot_command(f"loop start {brief_id}")

        result = str(handle_autopilot_command("loop status"))

        assert "Autonomous Loops" in result
        assert brief_id in result
        assert "WAITING_FOR_DEVELOPMENT_EXECUTOR" in result

    def test_loop_start_with_runtime_dispatches_and_persists_kanban_ids(
        self,
        tmp_hermes_home,
        tmp_workspace,
        sample_registration,
        monkeypatch,
    ):
        from autopilot.adapters.development_executor import DispatchResult, DevelopmentExecutor

        brief_id = _approved_brief(sample_registration)
        profile = {
            "schema_version": 1,
            "project_id": sample_registration["project_id"],
            "workspace_root": str(tmp_workspace),
            "prerequisites": [],
            "checks": [
                {
                    "check_id": "unit",
                    "argv": ["python3", "-m", "pytest", "-q"],
                    "cwd": ".",
                    "timeout_seconds": 60,
                    "required_evidence": ["exit_code"],
                }
            ],
        }
        assert "configured" in str(
            handle_autopilot_command(f"verify configure {json.dumps(profile)}")
        ).lower()

        def fake_dispatch(self, **kwargs):
            assert kwargs["brief"].brief_id == brief_id
            loop = kwargs["loop"]
            persisted = self._supervisor.mark_dispatched(
                project_id=loop.project_id,
                loop_id=loop.loop_id,
                board_slug="test-project-001",
                development_task_id="task-dev",
                verifier_task_id="task-verify",
                starting_revision="abc123",
                verification_profile_digest="profile-digest",
                source_status_digest="source-digest",
                dirty_workspace=True,
            )
            assert persisted is not None
            assert self._supervisor.mark_status(
                project_id=loop.project_id,
                loop_id=loop.loop_id,
                status="RUNNING",
            ) is not None
            return DispatchResult(
                success=True,
                board_slug="test-project-001",
                development_task_id="task-dev",
                verifier_task_id="task-verify",
                starting_revision="abc123",
                verification_profile_digest="profile-digest",
                source_status_digest="source-digest",
                dirty_workspace=True,
                warnings=["main workspace preserved"],
            )

        monkeypatch.setattr(DevelopmentExecutor, "dispatch", fake_dispatch)

        result = str(
            handle_autopilot_command(
                f"loop start {brief_id}",
                runtime=object(),
            )
        )

        assert "Status: RUNNING" in result
        assert "Development task: task-dev" in result
        assert "Verifier task: task-verify" in result
        loop = AutonomousLoopSupervisor(hermes_home=tmp_hermes_home).list_loops(
            sample_registration["project_id"]
        )[0]
        assert loop["status"] == "RUNNING"
        assert loop["development_task_id"] == "task-dev"
        assert loop["verifier_task_id"] == "task-verify"

    def test_loop_stop_marks_loop_stopped(self, tmp_hermes_home, sample_registration):
        brief_id = _approved_brief(sample_registration)
        start = str(handle_autopilot_command(f"loop start {brief_id}"))
        loop_id = ""
        for line in start.splitlines():
            if line.startswith("Loop ID:"):
                loop_id = line.split(":", 1)[1].strip()
                break
        assert loop_id

        stopped = str(handle_autopilot_command(f"loop stop {loop_id}"))
        status = str(handle_autopilot_command("loop status"))

        assert "stopped" in stopped.lower()
        assert "STOPPED" in status

    def test_loop_stop_preserves_pre_dispatch_terminal_status(
        self,
        tmp_hermes_home,
        sample_registration,
    ):
        brief_id = _approved_brief(sample_registration)
        start = str(handle_autopilot_command(f"loop start {brief_id}"))
        loop_id = next(
            line.split(":", 1)[1].strip()
            for line in start.splitlines()
            if line.startswith("Loop ID:")
        )
        supervisor = AutonomousLoopSupervisor(hermes_home=tmp_hermes_home)
        supervisor.mark_dispatch_blocked(
            project_id=sample_registration["project_id"],
            loop_id=loop_id,
        )

        class UnexpectedRuntime:
            def run(self, *_args, **_kwargs):
                raise AssertionError("terminal pre-dispatch loop must not contact Kanban")

        stopped = str(handle_autopilot_command(
            f"loop stop {loop_id}",
            runtime=UnexpectedRuntime(),
        ))
        stored = next(
            loop for loop in supervisor.list_loops(sample_registration["project_id"])
            if loop["loop_id"] == loop_id
        )

        assert "already terminal" in stopped.lower()
        assert "DISPATCH_BLOCKED" in stopped
        assert stored["status"] == "DISPATCH_BLOCKED"

    def test_supervisor_result_report_artifact(self, tmp_hermes_home, sample_registration):
        brief_id = _approved_brief(sample_registration)
        handle_autopilot_command(f"loop start {brief_id}")
        supervisor = AutonomousLoopSupervisor(hermes_home=tmp_hermes_home)
        loop = supervisor.list_loops(sample_registration["project_id"])[0]

        report = supervisor.record_development_result(
            project_id=sample_registration["project_id"],
            loop_id=loop["loop_id"],
            summary="Phase completed with tests passing.",
            evidence={"tests": "passed", "files_changed": ["lib/main.dart"]},
        )

        assert report.success is True
        assert report.artifact_path
        payload = json.loads(Path(report.artifact_path).read_text())
        assert payload["status"] == "READY_FOR_DISCUSSION_REVIEW"
        assert payload["summary"] == "Phase completed with tests passing."
