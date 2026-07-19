"""Project-scoped verification profile contracts.

Verification commands are represented as argv tuples and are never interpreted by
a shell. Profiles are immutable and bound to one registered project/workspace.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
from typing import Any


VERIFICATION_PROFILE_SCHEMA_VERSION = 1
SUPPORTED_EVIDENCE_FIELDS = frozenset({
    "exit_code",
    "duration_seconds",
    "stdout_excerpt",
    "stderr_excerpt",
})


@dataclass(frozen=True)
class VerificationCheck:
    """One bounded verification command."""

    check_id: str
    argv: tuple[str, ...]
    cwd: str
    timeout_seconds: int
    required_evidence: tuple[str, ...]


@dataclass(frozen=True)
class DevelopmentCommand:
    """One explicitly approved exact command available only to Development workers."""

    command_id: str
    argv: tuple[str, ...]
    cwd: str
    timeout_seconds: int


@dataclass(frozen=True)
class VerificationProfile:
    """Immutable verification configuration for one registered project."""

    schema_version: int
    project_id: str
    workspace_root: str
    prerequisites: tuple[str, ...]
    checks: tuple[VerificationCheck, ...]
    development_commands: tuple[DevelopmentCommand, ...] = ()
    max_remediation_cycles: int = 1


def _canonical_workspace(path: str) -> Path:
    if not isinstance(path, str) or not path.strip():
        raise ValueError("workspace_root must be a non-empty string")
    candidate = Path(path).expanduser()
    if not candidate.exists() or not candidate.is_dir():
        raise ValueError("workspace_root must be an existing directory")
    return candidate.resolve(strict=True)


def validate_verification_profile(
    data: dict[str, Any],
    *,
    registered_project_id: str,
    registered_workspace_root: str,
) -> VerificationProfile:
    """Validate and normalize a profile against immutable registration."""

    if not isinstance(data, dict):
        raise ValueError("verification profile must be an object")
    schema_version = data.get("schema_version")
    if schema_version != VERIFICATION_PROFILE_SCHEMA_VERSION:
        raise ValueError("unsupported verification profile schema_version")

    project_id = data.get("project_id")
    if not isinstance(project_id, str) or project_id != registered_project_id:
        raise ValueError("verification profile project_id does not match registration")

    registered_root = _canonical_workspace(registered_workspace_root)
    profile_root = _canonical_workspace(data.get("workspace_root", ""))
    if profile_root != registered_root:
        raise ValueError("verification profile workspace_root does not match registration")

    prerequisites_data = data.get("prerequisites", [])
    if not isinstance(prerequisites_data, list) or not all(
        isinstance(item, str) and item.strip() for item in prerequisites_data
    ):
        raise ValueError("prerequisites must be a list of non-empty strings")

    checks_data = data.get("checks")
    if not isinstance(checks_data, list) or not checks_data:
        raise ValueError("verification profile requires at least one check")
    if len(checks_data) > 16:
        raise ValueError("verification profile cannot contain more than 16 checks")

    max_remediation_cycles = data.get("max_remediation_cycles", 1)
    if (
        not isinstance(max_remediation_cycles, int)
        or isinstance(max_remediation_cycles, bool)
        or not 0 <= max_remediation_cycles <= 3
    ):
        raise ValueError("max_remediation_cycles must be an integer from 0 to 3")

    checks: list[VerificationCheck] = []
    check_ids: set[str] = set()
    for raw in checks_data:
        if not isinstance(raw, dict):
            raise ValueError("each verification check must be an object")
        check_id = raw.get("check_id")
        argv = raw.get("argv")
        cwd = raw.get("cwd", ".")
        timeout = raw.get("timeout_seconds")
        evidence = raw.get("required_evidence", [])
        if not isinstance(check_id, str) or not check_id.strip():
            raise ValueError("check_id must be a non-empty string")
        check_id = check_id.strip()
        if check_id in check_ids:
            raise ValueError(f"duplicate check_id: {check_id}")
        check_ids.add(check_id)
        if not isinstance(argv, list) or not argv or not all(
            isinstance(part, str) and part and "\x00" not in part for part in argv
        ):
            raise ValueError("argv must be a non-empty list of strings")
        if not isinstance(cwd, str) or not cwd.strip() or Path(cwd).is_absolute():
            raise ValueError("check cwd must be a non-empty relative path")
        resolved_cwd = (profile_root / cwd).resolve(strict=False)
        try:
            resolved_cwd.relative_to(profile_root)
        except ValueError as exc:
            raise ValueError("check cwd escapes the registered workspace") from exc
        if (
            not isinstance(timeout, int)
            or isinstance(timeout, bool)
            or not 1 <= timeout <= 3600
        ):
            raise ValueError("timeout_seconds must be an integer from 1 to 3600")
        if not isinstance(evidence, list) or not all(
            isinstance(item, str) and item.strip() for item in evidence
        ):
            raise ValueError("required_evidence must be a list of non-empty strings")
        evidence_aliases = {
            "stdout": "stdout_excerpt",
            "stderr": "stderr_excerpt",
            "duration": "duration_seconds",
        }
        normalized_evidence = tuple(
            evidence_aliases.get(str(item).strip(), str(item).strip())
            for item in evidence
        )
        if set(normalized_evidence) - SUPPORTED_EVIDENCE_FIELDS:
            raise ValueError("required_evidence contains an unsupported field")
        if len(normalized_evidence) != len(set(normalized_evidence)):
            raise ValueError("required_evidence contains duplicates")
        checks.append(
            VerificationCheck(
                check_id=check_id.strip(),
                argv=tuple(argv),
                cwd=cwd,
                timeout_seconds=timeout,
                required_evidence=normalized_evidence,
            )
        )

    development_data = data.get("development_commands", [])
    if not isinstance(development_data, list) or len(development_data) > 16:
        raise ValueError("development_commands must be a list of at most 16 commands")
    development_commands: list[DevelopmentCommand] = []
    command_ids: set[str] = set()
    denied_wrappers = {"sh", "bash", "zsh", "fish", "pwsh", "powershell", "cmd", "env", "xargs"}
    for raw in development_data:
        if not isinstance(raw, dict):
            raise ValueError("each development command must be an object")
        command_id = raw.get("command_id")
        argv = raw.get("argv")
        cwd = raw.get("cwd", ".")
        timeout = raw.get("timeout_seconds")
        if not isinstance(command_id, str) or not command_id.strip() or command_id in command_ids:
            raise ValueError("development command_id must be non-empty and unique")
        command_ids.add(command_id)
        if not isinstance(argv, list) or not argv or not all(
            isinstance(part, str) and part and "\x00" not in part for part in argv
        ):
            raise ValueError("development command argv must be a non-empty list of strings")
        executable = Path(argv[0]).name.lower()
        if executable in denied_wrappers:
            raise ValueError("development command cannot invoke a shell or command wrapper")
        if executable.startswith("python") and "-c" in argv[1:]:
            raise ValueError("development command cannot use Python inline evaluation")
        if executable in {"node", "ruby", "perl"} and any(
            flag in argv[1:] for flag in ("-e", "--eval")
        ):
            raise ValueError("development command cannot use inline evaluation")
        if not isinstance(cwd, str) or not cwd.strip() or Path(cwd).is_absolute():
            raise ValueError("development command cwd must be a non-empty relative path")
        resolved_cwd = (profile_root / cwd).resolve(strict=False)
        try:
            resolved_cwd.relative_to(profile_root)
        except ValueError as exc:
            raise ValueError("development command cwd escapes the registered workspace") from exc
        if (
            not isinstance(timeout, int)
            or isinstance(timeout, bool)
            or not 1 <= timeout <= 3600
        ):
            raise ValueError("development command timeout_seconds must be an integer from 1 to 3600")
        development_commands.append(DevelopmentCommand(
            command_id=command_id.strip(),
            argv=tuple(argv),
            cwd=cwd,
            timeout_seconds=timeout,
        ))

    return VerificationProfile(
        schema_version=schema_version,
        project_id=project_id,
        workspace_root=str(profile_root),
        prerequisites=tuple(item.strip() for item in prerequisites_data),
        checks=tuple(checks),
        development_commands=tuple(development_commands),
        max_remediation_cycles=max_remediation_cycles,
    )


def verification_profile_to_dict(profile: VerificationProfile) -> dict[str, Any]:
    """Serialize an immutable profile for project-scoped state storage."""

    return {
        "schema_version": profile.schema_version,
        "project_id": profile.project_id,
        "workspace_root": profile.workspace_root,
        "prerequisites": list(profile.prerequisites),
        "max_remediation_cycles": profile.max_remediation_cycles,
        "development_commands": [
            {
                "command_id": command.command_id,
                "argv": list(command.argv),
                "cwd": command.cwd,
                "timeout_seconds": command.timeout_seconds,
            }
            for command in profile.development_commands
        ],
        "checks": [
            {
                "check_id": check.check_id,
                "argv": list(check.argv),
                "cwd": check.cwd,
                "timeout_seconds": check.timeout_seconds,
                "required_evidence": list(check.required_evidence),
            }
            for check in profile.checks
        ],
    }


def verification_profile_digest(profile: VerificationProfile) -> str:
    """Return the canonical SHA-256 digest bound into a dispatched run."""

    canonical = json.dumps(
        verification_profile_to_dict(profile),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def save_verification_profile(profile: VerificationProfile) -> None:
    """Persist a profile inside its registered project's atomic state file."""

    from .storage import mutate_project_state

    def apply(state: dict[str, Any]) -> None:
        registration = state.get("registration")
        if not isinstance(registration, dict):
            raise ValueError("project registration is required before verification configuration")
        validated = validate_verification_profile(
            verification_profile_to_dict(profile),
            registered_project_id=str(registration.get("project_id", "")),
            registered_workspace_root=str(registration.get("workspace_root", "")),
        )
        state["verification_profile"] = verification_profile_to_dict(validated)

    mutate_project_state(profile.project_id, apply)


def load_verification_profile(project_id: str) -> VerificationProfile | None:
    """Load and revalidate a project's persisted verification profile."""

    from .storage import load_project_state

    state = load_project_state(project_id)
    raw = state.get("verification_profile")
    registration = state.get("registration")
    if not isinstance(raw, dict) or not isinstance(registration, dict):
        return None
    return validate_verification_profile(
        raw,
        registered_project_id=str(registration.get("project_id", "")),
        registered_workspace_root=str(registration.get("workspace_root", "")),
    )
