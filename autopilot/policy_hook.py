"""Per-tool enforcement for active Project Autopilot Kanban workers.

The hook is deliberately a no-op for ordinary Hermes sessions. It only applies
when Hermes supplies a task id that is durably bound to an Autopilot loop.
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import shlex
from typing import Any

from .audit import redact_text
from .kill_switch import check_kill_switch
from .lease import validate_lease
from .storage import load_project_state
from .verification import load_verification_profile

_ACTIVE_STATUSES = {
    "QUEUED",
    "RUNNING",
    "VERIFYING",
    "REMEDIATING",
    "NEEDS_HUMAN",
    "AWAITING_HUMAN_ACCEPTANCE",
}
_REPORT_ONLY_TOOLS = {"kanban_block"}
_KANBAN_SELF_TOOLS = {
    "kanban_show",
    "kanban_heartbeat",
    "kanban_complete",
    "kanban_block",
}
_ALLOWED_COMMON_TOOLS = {
    "read_file",
    "search_files",
    "todo",
    "skill_view",
    "skills_list",
    "kanban_show",
    "kanban_heartbeat",
    "kanban_complete",
    "kanban_block",
    "autopilot_decide",
    "terminal",
}
_ALLOWED_WRITE_TOOLS = {"write_file", "patch"}
_GIT_READ_SUBCOMMANDS = {
    "status",
    "diff",
    "show",
    "log",
    "rev-parse",
    "ls-files",
    "grep",
    "check-ignore",
    "blame",
}
_SHELL_OPERATORS = {"&&", "||", ";", "|", "&", ">", ">>", "<", "<<"}
_PATH_KEYS = {"path", "workdir"}
_GIT_REPOSITORY_OVERRIDE_OPTIONS = {
    "-C",
    "-c",
    "--git-dir",
    "--work-tree",
    "--namespace",
    "--config-env",
    "--exec-path",
}
_GIT_OUTPUT_OR_HELPER_OPTIONS = {
    "--output",
    "--ext-diff",
    "--textconv",
    "--no-index",
    "--open-files-in-pager",
    "--paginate",
    "-p",
    "-P",
    "-O",
}


def _block(message: str) -> dict[str, str]:
    return {
        "action": "block",
        "message": f"Project Autopilot blocked this tool call: {message}",
    }


def _hermes_home() -> Path:
    raw = os.environ.get("HERMES_HOME", "").strip()
    return Path(raw).expanduser() if raw else Path.home() / ".hermes"


def find_task_binding(task_id: str) -> dict[str, Any] | None:
    if not re.fullmatch(r"[A-Za-z0-9_-]+", task_id):
        return None
    projects = _hermes_home() / "state" / "autopilot" / "projects"
    if not projects.is_dir():
        with open("/tmp/autopilot_diag.txt", "a") as f:
            f.write(f"DIAG_find_task_binding: {projects} NOT a dir for task_id={task_id!r}\n")
        return None
    for path in projects.glob("*/loops/loop_*.json"):
        try:
            if path.stat().st_size > 1_000_000:
                continue
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            continue
        if not isinstance(payload, dict):
            continue
        role = ""
        if payload.get("development_task_id") == task_id:
            role = "development"
        elif payload.get("verifier_task_id") == task_id:
            role = "verifier"
        elif payload.get("current_remediation_task_id") == task_id:
            role = "remediation"
        if role:
            return payload | {"task_role": role}
    with open("/tmp/autopilot_diag.txt", "a") as f:
        f.write(f"DIAG_find_task_binding: searched {projects}/**/loop_*.json, found no match for {task_id=}\n")


def _expired(expiry: str) -> bool:
    try:
        expires = datetime.fromisoformat(expiry.replace("Z", "+00:00"))
        if expires.tzinfo is None:
            return True
        return datetime.now(timezone.utc) >= expires.astimezone(timezone.utc)
    except (TypeError, ValueError):
        return True


def _within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _expected_worktree(binding: dict[str, Any]) -> Path | None:
    workspace = binding.get("workspace_root")
    development_task_id = binding.get("development_task_id")
    if not isinstance(workspace, str) or not isinstance(development_task_id, str):
        return None
    try:
        return (Path(workspace).expanduser().resolve(strict=True) / ".worktrees" / development_task_id).resolve(strict=True)
    except (OSError, RuntimeError):
        return None


def _check_paths(args: dict[str, Any], worktree: Path) -> str | None:
    for key in _PATH_KEYS:
        value = args.get(key)
        if value in (None, ""):
            continue
        if not isinstance(value, str) or "\x00" in value:
            return "tool path is invalid."
        candidate = Path(value).expanduser()
        if not candidate.is_absolute():
            candidate = worktree / candidate
        try:
            resolved = candidate.resolve(strict=False)
        except (OSError, RuntimeError):
            return "tool path could not be resolved safely."
        if not _within(resolved, worktree):
            return "tool path is outside the isolated worktree."
    return None


def _matches_option(token: str, options: set[str]) -> bool:
    return any(token == option or token.startswith(f"{option}=") for option in options)


def _terminal_violation(
    command_args: dict[str, Any],
    *,
    project_id: str,
    role: str,
    worktree: Path,
    process_cwd: Path,
) -> str | None:
    if command_args.get("background") or command_args.get("pty"):
        return "background and interactive terminal commands are not allowed."
    command = command_args.get("command")
    if not isinstance(command, str) or not command.strip() or "\x00" in command:
        return "terminal command is invalid."
    if "\n" in command or "\r" in command:
        return "multi-line terminal commands are not allowed."
    try:
        tokens = shlex.split(command, posix=True)
    except ValueError:
        return "terminal command could not be parsed safely."
    if not tokens:
        return "terminal command is empty."
    if any(token in _SHELL_OPERATORS for token in tokens):
        return "shell composition and redirection are not allowed."

    executable = Path(tokens[0]).name
    try:
        profile = load_verification_profile(project_id)
    except Exception:
        profile = None

    if executable == "git":
        if any(_matches_option(token, _GIT_REPOSITORY_OVERRIDE_OPTIONS) for token in tokens[1:]):
            return "git repository or worktree override options are not allowed."
        if any(_matches_option(token, _GIT_OUTPUT_OR_HELPER_OPTIONS) for token in tokens[1:]):
            return "git output or external helper options are not allowed."
        if any(Path(token).is_absolute() or ".." in Path(token).parts for token in tokens[1:] if not token.startswith("-")):
            return "git command paths must remain inside the isolated worktree."
        subcommand = next((token for token in tokens[1:] if not token.startswith("-")), "")
        if subcommand not in _GIT_READ_SUBCOMMANDS:
            label = f"git {subcommand}".strip()
            return f"{label} is not authorized by this lease."
        return None

    configured_commands: list[Any] = [] if profile is None else list(profile.checks)
    if profile is not None and role != "verifier":
        configured_commands.extend(profile.development_commands)
    configured_check = next(
        (check for check in configured_commands if tuple(tokens) == check.argv),
        None,
    )
    if configured_check is None:
        return "terminal command must match an exact configured verification command or approved Development command for this worker role."
    expected_cwd = (worktree / configured_check.cwd).resolve(strict=False)
    requested_workdir = command_args.get("workdir")
    try:
        effective_cwd = (
            Path(requested_workdir).expanduser().resolve(strict=False)
            if isinstance(requested_workdir, str) and requested_workdir
            else process_cwd
        )
    except (OSError, RuntimeError):
        return "configured verification working directory could not be resolved."
    if effective_cwd != expected_cwd:
        return "terminal command must use the configured verification working directory."
    return None


def _metadata_violation(metadata: Any) -> str | None:
    if metadata is None:
        return None
    if not isinstance(metadata, dict):
        return "Autopilot completion metadata must be a JSON object."
    try:
        serialized = json.dumps(metadata, ensure_ascii=False, sort_keys=True)
    except (TypeError, ValueError):
        return "Autopilot completion metadata must be JSON serializable."
    if len(serialized.encode("utf-8")) > 100_000:
        return "Autopilot completion metadata exceeds 100 KB."

    def contains_sensitive(value: Any) -> bool:
        if isinstance(value, str):
            return redact_text(value, max_length=len(value)) != value
        if isinstance(value, dict):
            return any(
                contains_sensitive(key) or contains_sensitive(item)
                for key, item in value.items()
            )
        if isinstance(value, (list, tuple)):
            return any(contains_sensitive(item) for item in value)
        return False

    if contains_sensitive(metadata):
        return "Autopilot completion metadata contains sensitive or personal data; redact it before completion."
    return None


def _kanban_violation(
    *,
    tool_name: str,
    args: dict[str, Any],
    task_id: str,
    binding: dict[str, Any],
) -> str | None:
    if tool_name not in _KANBAN_SELF_TOOLS:
        return None
    selected_task = args.get("task_id")
    if selected_task not in (None, "") and selected_task != task_id:
        return "Kanban operation may address only the bound Autopilot task."
    selected_board = args.get("board")
    if selected_board not in (None, "") and selected_board != binding.get("board_slug"):
        return "Kanban operation may address only the bound Autopilot board."
    if tool_name == "kanban_complete" and args.get("artifacts"):
        return "Kanban completion artifact uploads are not allowed for Autopilot workers."
    if tool_name == "kanban_complete" and args.get("created_cards"):
        return "Autopilot workers cannot declare cards they were not authorized to create."
    if tool_name == "kanban_complete":
        return _metadata_violation(args.get("metadata"))
    return None


def pre_tool_call_guard(
    *,
    tool_name: str,
    args: dict[str, Any] | None = None,
    task_id: str = "",
    **_: Any,
) -> dict[str, str] | None:
    """Block policy-violating tools for a durably bound Autopilot task."""

    effective_task_id = os.environ.get("HERMES_KANBAN_TASK", "").strip() or task_id
    tenant = os.environ.get("HERMES_TENANT", "").strip()
    hh = os.environ.get("HERMES_HOME", "UNSET")
    with open("/tmp/autopilot_diag.txt", "a") as f:
        f.write(f"DIAG_TOP: effective_task_id={effective_task_id!r} task_id_param={task_id!r} tenant={tenant!r} HERMES_HOME={hh!r} CWD={os.getcwd()!r}\n")
        f.flush()
    binding = find_task_binding(effective_task_id)
    if binding is None:
        if effective_task_id and tenant.startswith("autopilot:"):
            with open("/tmp/autopilot_diag.txt", "a") as f:
                f.write(f"DIAG_BLOCK: task={effective_task_id!r} tenant={tenant!r} HERMES_HOME={hh!r} CWD={os.getcwd()!r}\n")
                f.flush()
            return _block("Autopilot tenant task has no valid durable loop binding.")
        return None
    task_id = effective_task_id
    tool_args = args if isinstance(args, dict) else {}
    kanban_violation = _kanban_violation(
        tool_name=tool_name,
        args=tool_args,
        task_id=task_id,
        binding=binding,
    )
    if kanban_violation:
        return _block(kanban_violation)
    if tool_name in _REPORT_ONLY_TOOLS:
        return None

    kill_reason = check_kill_switch()
    if kill_reason:
        return _block(f"kill switch is active ({kill_reason}).")

    project_id = binding.get("project_id")
    if not isinstance(project_id, str) or not project_id:
        return _block("task binding has no valid project id.")
    if tenant.startswith("autopilot:") and tenant != f"autopilot:{project_id}":
        return _block("Autopilot tenant does not match the bound project.")
    try:
        state = load_project_state(project_id)
        lease_data = state.get("lease")
        if not isinstance(lease_data, dict):
            raise ValueError("missing lease")
        lease = validate_lease(lease_data)
    except Exception:
        return _block("active autonomy lease is missing or invalid.")

    if lease.lease_id != binding.get("lease_id"):
        return _block("task lease no longer matches the active autonomy lease.")
    if _expired(lease.expiry):
        return _block("autonomy lease expired.")
    if binding.get("status") not in _ACTIVE_STATUSES:
        return _block("Autopilot loop is not active.")

    worktree = _expected_worktree(binding)
    if worktree is None:
        return _block("isolated worktree is missing or invalid.")
    try:
        process_cwd = Path.cwd().resolve(strict=True)
    except (OSError, RuntimeError):
        return _block("worker current directory cannot be verified.")
    if not _within(process_cwd, worktree):
        return _block("worker process is outside the isolated worktree.")

    role = binding.get("task_role")
    allowed_tools = _ALLOWED_COMMON_TOOLS | _ALLOWED_WRITE_TOOLS
    if tool_name not in allowed_tools:
        return _block(f"tool {tool_name!r} is outside the Autopilot worker allowlist.")
    if role == "verifier" and tool_name in _ALLOWED_WRITE_TOOLS:
        return _block("verifier is read-only and cannot modify files.")

    path_violation = _check_paths(tool_args, worktree)
    if path_violation:
        return _block(path_violation)
    if tool_name == "terminal":
        violation = _terminal_violation(
            tool_args,
            project_id=project_id,
            role=str(role or "development"),
            worktree=worktree,
            process_cwd=process_cwd,
        )
        if violation:
            return _block(violation)

    return None
