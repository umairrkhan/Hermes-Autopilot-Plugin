from __future__ import annotations

import json
from pathlib import Path

from autopilot.adapters.autonomous_loop import AutonomousLoopSupervisor
from autopilot.adapters.development_executor import CommandResult
from autopilot.commands import handle_autopilot_command
from autopilot.decision_runtime import WorkerDecisionHandler
from autopilot.storage import save_project_state, set_active_project_id


def _bound_loop(
    home: Path,
    *,
    project_id: str = "project-001",
    board_slug: str = "project-001",
) -> Path:
    path = (
        home
        / "state"
        / "autopilot"
        / "projects"
        / project_id
        / "loops"
        / "loop_loop-001.json"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "loop_id": "loop-001",
        "project_id": project_id,
        "status": "RUNNING",
        "board_slug": board_slug,
        "development_task_id": "task-dev",
        "current_remediation_task_id": "",
        "verifier_task_id": "task-verify",
        "decision_count": 0,
        "pending_decision_artifact_path": "",
        "artifact_path": str(path),
    }), encoding="utf-8")
    return path


def _args(**changes):
    payload = {
        "question_id": "q-style",
        "text": "Which local formatting style should I use?",
        "category": "routine_low_risk",
        "choices": ["Custom", "Existing project style"],
        "recommended_choice": "Existing project style",
        "context": "Recommended local reversible formatting convention.",
    }
    payload.update(changes)
    return payload


def test_worker_decision_auto_answers_and_persists_artifact(
    tmp_hermes_home,
    monkeypatch,
):
    loop_path = _bound_loop(Path(tmp_hermes_home))
    monkeypatch.setenv("HERMES_KANBAN_TASK", "task-dev")
    monkeypatch.setenv("HERMES_KANBAN_BOARD", "project-001")
    monkeypatch.setenv("HERMES_TENANT", "autopilot:project-001")

    def unexpected_dispatch(*_args, **_kwargs):
        raise AssertionError("low-risk decision must not block the task")

    result = json.loads(WorkerDecisionHandler(unexpected_dispatch).handle(_args()))

    assert result["ok"] is True
    assert result["action"] == "auto_answer"
    assert result["selected_choice"] == "Existing project style"
    decision = json.loads(Path(result["artifact_path"]).read_text(encoding="utf-8"))
    assert decision["status"] == "auto_answered"
    loop = json.loads(loop_path.read_text(encoding="utf-8"))
    assert loop["status"] == "RUNNING"
    assert loop["decision_count"] == 1


def test_worker_decision_blocks_high_risk_question_and_marks_loop_first(
    tmp_hermes_home,
    monkeypatch,
):
    loop_path = _bound_loop(Path(tmp_hermes_home))
    monkeypatch.setenv("HERMES_KANBAN_TASK", "task-dev")
    monkeypatch.setenv("HERMES_KANBAN_BOARD", "project-001")
    monkeypatch.setenv("HERMES_TENANT", "autopilot:project-001")
    calls = []

    def dispatch(name, args):
        loop = json.loads(loop_path.read_text(encoding="utf-8"))
        assert loop["status"] == "NEEDS_HUMAN"
        assert loop["pending_decision_artifact_path"]
        calls.append((name, args))
        return json.dumps({"ok": True, "status": "blocked"})

    result = json.loads(WorkerDecisionHandler(dispatch).handle(_args(
        question_id="q-auth",
        text="Should authentication policy change?",
        category="security",
        choices=["Keep", "Change"],
        recommended_choice="Change",
        context="Security behavior.",
    )))

    assert result["ok"] is True
    assert result["action"] == "needs_human"
    assert calls[0][0] == "kanban_block"
    assert calls[0][1]["kind"] == "needs_input"
    loop = json.loads(loop_path.read_text(encoding="utf-8"))
    assert loop["status"] == "NEEDS_HUMAN"
    assert loop["decision_count"] == 1


def test_answered_question_replays_human_answer_without_reblocking(
    tmp_hermes_home,
    monkeypatch,
):
    _bound_loop(Path(tmp_hermes_home))
    monkeypatch.setenv("HERMES_KANBAN_TASK", "task-dev")
    monkeypatch.setenv("HERMES_TENANT", "autopilot:project-001")
    args = _args(
        question_id="q-security",
        text="Should security behavior change?",
        category="security",
        choices=["Keep", "Change"],
        recommended_choice="Change",
        context="Security behavior.",
    )
    first = WorkerDecisionHandler(
        lambda *_: json.dumps({"ok": True, "status": "blocked"})
    )
    assert json.loads(first.handle(args))["action"] == "needs_human"

    from autopilot.adapters.autonomous_loop import AutonomousLoopSupervisor

    supervisor = AutonomousLoopSupervisor()
    supervisor.stage_question_answer(
        project_id="project-001",
        loop_id="loop-001",
        question_id="q-security",
        answer="Keep the existing behavior",
    )
    supervisor.finalize_question_answer(
        project_id="project-001",
        loop_id="loop-001",
        question_id="q-security",
    )

    def unexpected_dispatch(*_args, **_kwargs):
        raise AssertionError("an answered replay must not block again")

    replay = json.loads(WorkerDecisionHandler(unexpected_dispatch).handle(args))
    assert replay["ok"] is True
    assert replay["action"] == "human_answer"
    assert replay["selected_choice"] == "Keep the existing behavior"


def test_loop_answer_command_stages_comments_unblocks_and_finalizes(
    tmp_hermes_home,
    monkeypatch,
):
    _bound_loop(
        Path(tmp_hermes_home),
        project_id="test-project-001",
        board_slug="board-1",
    )
    save_project_state("test-project-001", {
        "state": "EXECUTING",
        "registration": {
            "project_id": "test-project-001",
            "workspace_root": "/tmp/workspace",
        },
    })
    set_active_project_id("test-project-001")
    assert len(AutonomousLoopSupervisor().list_loops("test-project-001")) == 1
    monkeypatch.setenv("HERMES_KANBAN_TASK", "task-dev")
    monkeypatch.setenv("HERMES_TENANT", "autopilot:test-project-001")
    WorkerDecisionHandler(lambda *_args, **_kwargs: {"ok": True}).handle({
        "question_id": "q-architecture",
        "text": "Should we replace the authentication architecture?",
        "category": "architecture",
        "choices": ["replace", "preserve"],
        "recommended_choice": "replace",
    })

    class Runtime:
        def __init__(self):
            self.calls = []

        def run(self, argv, *, cwd, timeout_seconds):
            self.calls.append((argv, cwd, timeout_seconds))
            return CommandResult(0, "Unblocked task-dev", "")

    runtime = Runtime()
    response = str(handle_autopilot_command(
        "loop answer loop-001 q-architecture preserve",
        runtime=runtime,
    ))

    assert "Human answer recorded" in response
    assert runtime.calls[0][0] == (
        "hermes", "kanban", "--board", "board-1", "unblock", "--reason",
        "Human answer to q-architecture: preserve", "task-dev",
    )
    stored = AutonomousLoopSupervisor().list_loops("test-project-001")[0]
    assert stored["status"] == "QUEUED"
    assert stored["pending_decision_artifact_path"] == ""
    decisions = AutonomousLoopSupervisor().list_question_decisions(
        "test-project-001", "loop-001"
    )
    assert decisions[0]["status"] == "answered"
    assert decisions[0]["human_answer"] == "preserve"


def test_worker_decision_fails_closed_without_durable_binding(
    tmp_hermes_home,
    monkeypatch,
):
    monkeypatch.setenv("HERMES_KANBAN_TASK", "task-missing")
    monkeypatch.setenv("HERMES_TENANT", "autopilot:project-001")

    result = json.loads(WorkerDecisionHandler(lambda *_: None).handle(_args()))

    assert result["ok"] is False
    assert "binding" in result["error"].lower()
