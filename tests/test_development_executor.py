"""Tests for the durable, lease-gated Development executor."""

from __future__ import annotations

from datetime import datetime, timezone

from autopilot.adapters.autonomous_loop import AutonomousLoop, AutonomousLoopSupervisor
from autopilot.adapters.development_executor import (
    CommandResult,
    DevelopmentExecutor,
)
from autopilot.adapters.execution_bridge import BriefTask, DevelopmentExecutionBrief
from autopilot.lease import build_lease_preset, lease_to_dict
from autopilot.runtime import PluginToolRuntime
from autopilot.verification import validate_verification_profile


class FakeRuntime:
    def __init__(self, responses: dict[tuple[str, ...], CommandResult]):
        self.responses = responses
        self.calls: list[tuple[tuple[str, ...], str | None, int]] = []

    def run(self, argv: tuple[str, ...], *, cwd: str | None, timeout_seconds: int) -> CommandResult:
        self.calls.append((argv, cwd, timeout_seconds))
        return self.responses.get(argv, CommandResult(exit_code=127, stdout="", stderr="unexpected command"))


def _contract(tmp_workspace):
    project_id = "test-project-001"
    lease = build_lease_preset(
        "autonomous-development",
        project_id=project_id,
        workspace_root=str(tmp_workspace),
        now=datetime.now(timezone.utc),
    )
    state = {
        "registration": {
            "project_id": project_id,
            "workspace_root": str(tmp_workspace),
        },
        "lease": lease_to_dict(lease),
    }
    brief = DevelopmentExecutionBrief(
        brief_id="brief-001",
        brief_version=1,
        schema_version=1,
        project_id=project_id,
        workspace_root=str(tmp_workspace),
        discussion_session_id="discussion-001",
        development_session_id="development-001",
        display_title="Test Project",
        lease_id=lease.lease_id,
        lease_expiry=lease.expiry,
        granted_capabilities=lease.granted_capabilities,
        scope="Implement the approved change",
        tasks=(
            BriefTask(
                task_id="task-001",
                title="Implement feature",
                description="Make the approved local code change.",
                priority="medium",
                risk_level="medium",
                acceptance_criteria=("unit tests pass",),
            ),
        ),
        created_at=datetime.now(timezone.utc).isoformat(),
        created_by="test",
        human_gate_required=True,
        execution_authorized=True,
    )
    loop = AutonomousLoop(
        loop_id="loop-001",
        project_id=project_id,
        brief_id=brief.brief_id,
        workspace_root=str(tmp_workspace),
        discussion_session_id=brief.discussion_session_id,
        development_session_id=brief.development_session_id,
        lease_id=lease.lease_id,
        status="WAITING_FOR_DEVELOPMENT_EXECUTOR",
        mode="supervised-development",
        auto_answer_policy="recommended_low_risk_only",
        created_at=datetime.now(timezone.utc).isoformat(),
    )
    profile = validate_verification_profile(
        {
            "schema_version": 1,
            "project_id": project_id,
            "workspace_root": str(tmp_workspace),
            "prerequisites": ["python3"],
            "checks": [
                {
                    "check_id": "unit",
                    "argv": ["python3", "-m", "pytest", "-q"],
                    "cwd": ".",
                    "timeout_seconds": 60,
                    "required_evidence": ["exit_code", "stdout"],
                }
            ],
        },
        registered_project_id=project_id,
        registered_workspace_root=str(tmp_workspace),
    )
    return state, brief, loop, profile


def test_dispatch_fails_closed_when_gateway_is_not_running(tmp_workspace):
    state, brief, loop, profile = _contract(tmp_workspace)
    root = str(tmp_workspace.resolve())
    runtime = FakeRuntime(
        {
            ("git", "rev-parse", "--show-toplevel"): CommandResult(0, root + "\n", ""),
            ("git", "rev-parse", "HEAD"): CommandResult(0, "abc123\n", ""),
            ("git", "status", "--porcelain=v1", "-z"): CommandResult(0, "", ""),
            ("which", "python3"): CommandResult(0, "/usr/bin/python3\n", ""),
            ("hermes", "gateway", "status"): CommandResult(
                0,
                "Gateway is not running",
                "",
            ),
        }
    )

    result = DevelopmentExecutor(runtime).dispatch(
        loop=loop,
        brief=brief,
        state=state,
        profile=profile,
    )

    assert result.success is False
    assert any("gateway" in blocker.lower() for blocker in result.blockers)
    assert not any("kanban" in call[0] and "create" in call[0] for call in runtime.calls)


def test_dispatch_creates_linked_worktree_development_and_verifier_tasks(
    tmp_hermes_home,
    tmp_workspace,
):
    state, brief, _, profile = _contract(tmp_workspace)
    supervisor = AutonomousLoopSupervisor(hermes_home=tmp_hermes_home)
    started = supervisor.start_loop(brief, state)
    assert started.success and started.loop is not None
    loop = started.loop
    root = str(tmp_workspace.resolve())

    class SuccessfulRuntime(FakeRuntime):
        def run(self, argv, *, cwd, timeout_seconds):
            self.calls.append((argv, cwd, timeout_seconds))
            fixed = {
                ("git", "rev-parse", "--show-toplevel"): CommandResult(0, root + "\n", ""),
                ("git", "rev-parse", "HEAD"): CommandResult(0, "abc123\n", ""),
                ("git", "status", "--porcelain=v1", "-z"): CommandResult(0, " M existing.txt\x00", ""),
                ("which", "python3"): CommandResult(0, "/usr/bin/python3\n", ""),
                ("hermes", "gateway", "status"): CommandResult(0, "Gateway is running\n", ""),
                ("hermes", "project", "show", "test-project-001"): CommandResult(
                    0,
                    f"test-project-001\n  primary: {root}\n",
                    "",
                ),
                ("hermes", "kanban", "boards", "list", "--json"): CommandResult(
                    0,
                    '[{"slug":"test-project-001"}]',
                    "",
                ),
            }
            if argv in fixed:
                return fixed[argv]
            if argv[:5] == (
                "hermes",
                "kanban",
                "--board",
                "test-project-001",
                "create",
            ):
                if "--parent" in argv:
                    return CommandResult(0, '{"id":"task-verify"}', "")
                return CommandResult(0, '{"id":"task-dev"}', "")
            if argv[:5] == (
                "hermes",
                "kanban",
                "--board",
                "test-project-001",
                "promote",
            ):
                return CommandResult(0, "Promoted task-dev", "")
            return CommandResult(127, "", f"unexpected command: {argv}")

    runtime = SuccessfulRuntime({})
    result = DevelopmentExecutor(runtime, supervisor=supervisor).dispatch(
        loop=loop,
        brief=brief,
        state=state,
        profile=profile,
    )

    assert result.success is True
    assert result.development_task_id == "task-dev"
    assert result.verifier_task_id == "task-verify"
    assert result.starting_revision == "abc123"
    assert result.source_status_digest
    assert result.dirty_workspace is True

    creates = [call[0] for call in runtime.calls if call[0][:5] == (
        "hermes",
        "kanban",
        "--board",
        "test-project-001",
        "create",
    )]
    assert len(creates) == 2
    development, verifier = creates
    assert development[development.index("--project") + 1] == "test-project-001"
    assert development[development.index("--workspace") + 1] == "worktree"
    assert "--parent" not in development
    assert verifier[verifier.index("--parent") + 1] == "task-dev"
    assert verifier[verifier.index("--workspace") + 1] == f"dir:{root}/.worktrees/task-dev"
    assert state["lease"]["expiry"] in development[development.index("--body") + 1]
    assert "git commit" in development[development.index("--body") + 1].lower()
    assert "python3" in verifier[verifier.index("--body") + 1]


def test_dispatch_persists_policy_binding_before_promoting_worker(
    tmp_hermes_home,
    tmp_workspace,
):
    state, brief, _, profile = _contract(tmp_workspace)
    supervisor = AutonomousLoopSupervisor(hermes_home=tmp_hermes_home)
    started = supervisor.start_loop(brief, state)
    assert started.success and started.loop is not None
    loop = started.loop
    root = str(tmp_workspace.resolve())
    observed_at_promote: dict[str, object] = {}

    class RaceRuntime(FakeRuntime):
        def run(self, argv, *, cwd, timeout_seconds):
            self.calls.append((argv, cwd, timeout_seconds))
            fixed = {
                ("git", "rev-parse", "--show-toplevel"): CommandResult(0, root + "\n", ""),
                ("git", "rev-parse", "HEAD"): CommandResult(0, "abc123\n", ""),
                ("git", "status", "--porcelain=v1", "-z"): CommandResult(0, "", ""),
                ("which", "python3"): CommandResult(0, "/usr/bin/python3\n", ""),
                ("hermes", "gateway", "status"): CommandResult(0, "Gateway is running\n", ""),
                ("hermes", "project", "show", "test-project-001"): CommandResult(
                    0, f"test-project-001\n  primary: {root}\n", ""
                ),
                ("hermes", "kanban", "boards", "list", "--json"): CommandResult(
                    0, '[{"slug":"test-project-001"}]', ""
                ),
            }
            if argv in fixed:
                return fixed[argv]
            if argv[:5] == (
                "hermes", "kanban", "--board", "test-project-001", "create"
            ):
                task_id = "task-verify" if "--parent" in argv else "task-dev"
                return CommandResult(0, f'{{"id":"{task_id}"}}', "")
            if argv[:5] == (
                "hermes", "kanban", "--board", "test-project-001", "promote"
            ):
                observed_at_promote.update(supervisor.list_loops("test-project-001")[0])
                return CommandResult(0, "Promoted task-dev", "")
            return CommandResult(127, "", f"unexpected command: {argv}")

    runtime = RaceRuntime({})
    result = DevelopmentExecutor(runtime, supervisor=supervisor).dispatch(
        loop=loop,
        brief=brief,
        state=state,
        profile=profile,
    )

    assert result.success is True
    assert observed_at_promote["development_task_id"] == "task-dev"
    assert observed_at_promote["verifier_task_id"] == "task-verify"
    assert observed_at_promote["status"] == "QUEUED"
    assert observed_at_promote["starting_revision"] == "abc123"
    assert observed_at_promote["verification_profile_digest"]
    assert observed_at_promote["source_status_digest"]


def test_plugin_runtime_dispatches_shell_quoted_argv_through_terminal_tool():
    class Context:
        def __init__(self):
            self.calls = []

        def dispatch_tool(self, name, args):
            self.calls.append((name, args))
            return '{"output":"ok\\n","exit_code":0,"error":null}'

    context = Context()
    runtime = PluginToolRuntime(context)

    result = runtime.run(
        ("printf", "%s", "a b;$(touch nope)"),
        cwd="/tmp/work tree",
        timeout_seconds=7,
    )

    assert result == CommandResult(0, "ok\n", "")
    assert context.calls == [
        (
            "terminal",
            {
                "command": "printf %s 'a b;$(touch nope)'",
                "workdir": "/tmp/work tree",
                "timeout": 7,
            },
        )
    ]
