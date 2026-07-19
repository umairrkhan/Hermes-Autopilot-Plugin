"""Immutable checkpoints and one-time local commit authorization."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import tempfile
from typing import Any
import zipfile

from .adapters.autonomous_loop import AutonomousLoopSupervisor
from .adapters.development_executor import CommandRuntime


@dataclass
class CheckpointResult:
    success: bool
    blockers: list[str] = field(default_factory=list)
    artifact_path: str = ""
    revision: str = ""


class CheckpointManager:
    """Create exact local checkpoints without touching the source workspace."""

    def __init__(
        self,
        runtime: CommandRuntime,
        *,
        hermes_home: Path | None = None,
        supervisor: AutonomousLoopSupervisor | None = None,
    ) -> None:
        raw = os.environ.get("HERMES_HOME", "").strip()
        self._home = hermes_home or (
            Path(raw).expanduser() if raw else Path.home() / ".hermes"
        )
        self._runtime = runtime
        self._supervisor = supervisor or AutonomousLoopSupervisor(hermes_home=self._home)

    @staticmethod
    def _safe_id(value: Any) -> str:
        text = str(value or "")
        return text if re.fullmatch(r"[A-Za-z0-9_-]+", text) else ""

    @staticmethod
    def _digest(raw: str) -> str:
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    @staticmethod
    def _safe_relative(path: str) -> bool:
        pure = PurePosixPath(path.replace("\\", "/"))
        return bool(path) and not pure.is_absolute() and ".." not in pure.parts

    @classmethod
    def _changed_paths(cls, raw: str) -> tuple[set[str], list[str]]:
        parts = raw.split("\x00")
        changed: set[str] = set()
        blockers: list[str] = []
        index = 0
        while index < len(parts):
            record = parts[index]
            index += 1
            if not record:
                continue
            if len(record) < 4:
                blockers.append("Git status returned a malformed record.")
                continue
            code = record[:2]
            path = record[3:]
            if not cls._safe_relative(path):
                blockers.append("Git status returned an unsafe changed path.")
            else:
                changed.add(path.replace("\\", "/"))
            if "R" in code or "C" in code:
                if index >= len(parts) or not parts[index]:
                    blockers.append("Git status returned a malformed rename record.")
                    continue
                old_path = parts[index]
                index += 1
                if not cls._safe_relative(old_path):
                    blockers.append("Git status returned an unsafe rename source.")
                else:
                    changed.add(old_path.replace("\\", "/"))
        return changed, blockers

    def _status(self, workspace: Path) -> tuple[str, set[str], list[str]]:
        result = self._runtime.run(
            ("git", "status", "--porcelain=v1", "-z", "--untracked-files=all"),
            cwd=str(workspace),
            timeout_seconds=60,
        )
        if result.exit_code != 0:
            return "", set(), ["Git status could not be inspected."]
        paths, blockers = self._changed_paths(result.stdout)
        return self._digest(result.stdout), paths, blockers

    def _paths(self, loop: dict[str, Any]) -> tuple[Path | None, Path | None, list[str]]:
        project_id = self._safe_id(loop.get("project_id"))
        task_id = self._safe_id(loop.get("development_task_id"))
        workspace_raw = loop.get("workspace_root")
        if not project_id or not task_id or not isinstance(workspace_raw, str):
            return None, None, ["Loop workspace binding is invalid."]
        try:
            workspace = Path(workspace_raw).resolve(strict=True)
            worktree = (workspace / ".worktrees" / task_id).resolve(strict=True)
            worktree.relative_to(workspace)
        except (OSError, RuntimeError, ValueError):
            return None, None, ["Isolated Development worktree is missing or unsafe."]
        return workspace, worktree, []

    def _project_dir(self, project_id: str) -> Path:
        path = self._home / "state" / "autopilot" / "projects" / project_id / "checkpoints"
        path.mkdir(parents=True, exist_ok=True)
        return path

    @staticmethod
    def _load_json(path: Path, *, max_bytes: int = 1_000_000) -> dict[str, Any]:
        if path.stat().st_size > max_bytes:
            raise ValueError("artifact exceeds its size limit")
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("artifact must be a JSON object")
        return payload

    @staticmethod
    def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        handle, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
        temp = Path(name)
        try:
            with os.fdopen(handle, "w", encoding="utf-8") as stream:
                json.dump(payload, stream, indent=2, sort_keys=True)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.chmod(temp, 0o600)
            os.replace(temp, path)
        finally:
            if temp.exists():
                temp.unlink()

    def _evidence_paths(self, loop: dict[str, Any]) -> tuple[set[str], list[str]]:
        result_path = loop.get("result_artifact_path")
        project_id = self._safe_id(loop.get("project_id"))
        loop_id = self._safe_id(loop.get("loop_id"))
        task_id = self._safe_id(loop.get("result_task_id"))
        run_id = loop.get("result_run_id")
        if (
            not project_id
            or not loop_id
            or not task_id
            or not isinstance(run_id, int)
            or isinstance(run_id, bool)
            or run_id < 1
            or not isinstance(result_path, str)
            or not result_path
        ):
            return set(), ["Validated verification evidence is missing."]
        expected = (
            self._home
            / "state"
            / "autopilot"
            / "projects"
            / project_id
            / "results"
            / f"result_{loop_id}_{task_id}_{run_id}.json"
        )
        if result_path != str(expected):
            return set(), ["Validated evidence path is not project-scoped."]
        try:
            payload = self._load_json(expected.resolve(strict=True))
        except (OSError, ValueError, json.JSONDecodeError):
            return set(), ["Validated verification evidence could not be read."]
        paths = payload.get("changed_files")
        if payload.get("accepted") is not True or not isinstance(paths, list):
            return set(), ["Validated evidence is not an accepted result."]
        normalized = {
            item.replace("\\", "/") for item in paths
            if isinstance(item, str) and self._safe_relative(item)
        }
        if len(normalized) != len(paths):
            return set(), ["Validated evidence contains unsafe or duplicate changed paths."]
        return normalized, []

    def _validate_bound_state(
        self,
        loop: dict[str, Any],
    ) -> tuple[Path | None, Path | None, str, set[str], list[str]]:
        if loop.get("status") != "ACCEPTED":
            return None, None, "", set(), ["Loop must be human-accepted first."]
        workspace, worktree, blockers = self._paths(loop)
        if blockers or workspace is None or worktree is None:
            return workspace, worktree, "", set(), blockers
        source_digest, _, source_blockers = self._status(workspace)
        expected_source = loop.get("source_status_digest")
        if not isinstance(expected_source, str) or not expected_source:
            source_blockers.append("Dispatch did not bind the original workspace status.")
        elif source_digest != expected_source:
            source_blockers.append(
                "Original workspace Git status changed after dispatch; checkpoint requires human review."
            )
        worktree_digest, changed, worktree_blockers = self._status(worktree)
        evidence_paths, evidence_blockers = self._evidence_paths(loop)
        blockers = source_blockers + worktree_blockers + evidence_blockers
        if changed != evidence_paths:
            blockers.append(
                "Current worktree changed paths do not exactly match validated verifier evidence."
            )
        return workspace, worktree, worktree_digest, changed, blockers

    @staticmethod
    def _contains_secret(data: bytes) -> bool:
        lowered = data.lower()
        patterns = (b"sk-", b"ghp_", b"xoxb-", b"authorization: bearer")
        return any(pattern in lowered for pattern in patterns)

    @classmethod
    def _current_content_digest(
        cls,
        worktree: Path,
        changed: set[str],
    ) -> tuple[str, list[str]]:
        hasher = hashlib.sha256()
        blockers: list[str] = []
        total = 0
        for relative in sorted(changed):
            path = worktree / relative
            if path.is_symlink():
                blockers.append(f"Checkpoint path is a symlink: {relative}")
                continue
            try:
                resolved = path.resolve(strict=True)
                resolved.relative_to(worktree)
            except FileNotFoundError:
                hasher.update(b"D\x00")
                hasher.update(relative.encode("utf-8"))
                hasher.update(b"\x00")
                continue
            except (OSError, RuntimeError, ValueError):
                blockers.append(f"Unsafe checkpoint path: {relative}")
                continue
            if not resolved.is_file():
                blockers.append(f"Checkpoint path is not a regular file: {relative}")
                continue
            if resolved.stat().st_size > 5_000_000:
                blockers.append(f"Checkpoint file exceeds 5 MB: {relative}")
                continue
            data = resolved.read_bytes()
            total += len(data)
            if total > 20_000_000:
                blockers.append("Checkpoint payload exceeds 20 MB.")
                break
            if cls._contains_secret(data):
                blockers.append(f"Checkpoint secret scan blocked: {relative}")
                continue
            mode = resolved.stat().st_mode & 0o777
            hasher.update(b"F\x00")
            hasher.update(relative.encode("utf-8"))
            hasher.update(b"\x00")
            hasher.update(str(mode).encode("ascii"))
            hasher.update(b"\x00")
            hasher.update(data)
            hasher.update(b"\x00")
        return hasher.hexdigest(), blockers

    def create(self, *, loop: dict[str, Any]) -> CheckpointResult:
        workspace, worktree, status_digest, changed, blockers = self._validate_bound_state(loop)
        if blockers or workspace is None or worktree is None:
            return CheckpointResult(False, blockers=blockers)
        project_id = self._safe_id(loop.get("project_id"))
        loop_id = self._safe_id(loop.get("loop_id"))
        task_id = self._safe_id(loop.get("result_task_id"))
        run_id = loop.get("result_run_id")
        if not project_id or not loop_id or not task_id or not isinstance(run_id, int) or run_id < 1:
            return CheckpointResult(False, blockers=["Loop result provenance is invalid."])

        files: list[tuple[str, bytes, int]] = []
        deleted: list[str] = []
        total = 0
        for relative in sorted(changed):
            path = worktree / relative
            if path.is_symlink():
                blockers.append(f"Checkpoint path is a symlink: {relative}")
                continue
            try:
                resolved = path.resolve(strict=True)
                resolved.relative_to(worktree)
            except FileNotFoundError:
                deleted.append(relative)
                continue
            except (OSError, RuntimeError, ValueError):
                blockers.append(f"Unsafe checkpoint path: {relative}")
                continue
            if resolved.is_symlink() or not resolved.is_file():
                blockers.append(f"Checkpoint path is not a regular file: {relative}")
                continue
            size = resolved.stat().st_size
            if size > 5_000_000:
                blockers.append(f"Checkpoint file exceeds 5 MB: {relative}")
                continue
            data = resolved.read_bytes()
            total += len(data)
            if total > 20_000_000:
                blockers.append("Checkpoint payload exceeds 20 MB.")
                break
            if self._contains_secret(data):
                blockers.append(f"Checkpoint secret scan blocked: {relative}")
                continue
            files.append((relative, data, resolved.stat().st_mode & 0o777))
        if blockers:
            return CheckpointResult(False, blockers=blockers)

        content_hasher = hashlib.sha256()
        for relative, data, mode in files:
            content_hasher.update(b"F\x00")
            content_hasher.update(relative.encode("utf-8"))
            content_hasher.update(b"\x00")
            content_hasher.update(str(mode).encode("ascii"))
            content_hasher.update(b"\x00")
            content_hasher.update(data)
            content_hasher.update(b"\x00")
        for relative in sorted(deleted):
            content_hasher.update(b"D\x00")
            content_hasher.update(relative.encode("utf-8"))
            content_hasher.update(b"\x00")
        content_digest = content_hasher.hexdigest()

        artifact = self._project_dir(project_id) / f"checkpoint_{loop_id}_{task_id}_{run_id}.zip"
        manifest = {
            "schema_version": 1,
            "project_id": project_id,
            "loop_id": loop_id,
            "brief_id": loop.get("brief_id", ""),
            "starting_revision": loop.get("starting_revision", ""),
            "source_status_digest": loop.get("source_status_digest", ""),
            "worktree_status_digest": status_digest,
            "worktree_content_digest": content_digest,
            "result_artifact_path": loop.get("result_artifact_path", ""),
            "result_task_id": task_id,
            "result_run_id": run_id,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "files": [
                {
                    "path": relative,
                    "mode": mode,
                    "size": len(data),
                    "sha256": hashlib.sha256(data).hexdigest(),
                }
                for relative, data, mode in files
            ],
            "deleted_paths": deleted,
        }
        if artifact.exists():
            try:
                with zipfile.ZipFile(artifact, "r") as archive:
                    existing_manifest = json.loads(archive.read("manifest.json"))
            except (OSError, KeyError, ValueError, zipfile.BadZipFile, json.JSONDecodeError):
                return CheckpointResult(False, blockers=["Existing checkpoint artifact is corrupted."])
            if (
                not isinstance(existing_manifest, dict)
                or existing_manifest.get("worktree_status_digest") != status_digest
                or existing_manifest.get("worktree_content_digest") != content_digest
                or existing_manifest.get("result_task_id") != task_id
                or existing_manifest.get("result_run_id") != run_id
            ):
                return CheckpointResult(
                    False,
                    blockers=["Existing immutable checkpoint does not match current accepted evidence."],
                )
        else:
            handle, name = tempfile.mkstemp(prefix=f".{artifact.name}.", dir=str(artifact.parent))
            os.close(handle)
            temp = Path(name)
            try:
                with zipfile.ZipFile(temp, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                    archive.writestr("manifest.json", json.dumps(manifest, indent=2, sort_keys=True) + "\n")
                    for relative, data, _ in files:
                        archive.writestr(f"files/{relative}", data)
                os.chmod(temp, 0o600)
                os.replace(temp, artifact)
            finally:
                if temp.exists():
                    temp.unlink()
        self._supervisor.mark_status(
            project_id=project_id,
            loop_id=loop_id,
            status="ACCEPTED",
            checkpoint_artifact_path=str(artifact),
            checkpoint_status_digest=status_digest,
            checkpoint_content_digest=content_digest,
        )
        return CheckpointResult(True, artifact_path=str(artifact))

    @classmethod
    def _checkpoint_payload_blockers(
        cls,
        archive: zipfile.ZipFile,
        payload: dict[str, Any],
    ) -> list[str]:
        files = payload.get("files")
        deleted = payload.get("deleted_paths")
        if not isinstance(files, list) or not isinstance(deleted, list):
            return ["Checkpoint payload manifest is malformed."]
        names = archive.namelist()
        if len(names) != len(set(names)):
            return ["Checkpoint payload contains duplicate archive members."]

        expected_names = {"manifest.json"}
        content_hasher = hashlib.sha256()
        seen_paths: set[str] = set()
        total = 0
        for record in files:
            if not isinstance(record, dict):
                return ["Checkpoint payload file record is malformed."]
            relative = record.get("path")
            size = record.get("size")
            mode = record.get("mode")
            digest = record.get("sha256")
            if (
                not isinstance(relative, str)
                or not cls._safe_relative(relative)
                or relative in seen_paths
                or not isinstance(size, int)
                or isinstance(size, bool)
                or size < 0
                or size > 5_000_000
                or not isinstance(mode, int)
                or isinstance(mode, bool)
                or not isinstance(digest, str)
                or not re.fullmatch(r"[0-9a-f]{64}", digest)
            ):
                return ["Checkpoint payload file record is unsafe."]
            seen_paths.add(relative)
            member = f"files/{relative}"
            expected_names.add(member)
            try:
                info = archive.getinfo(member)
                if info.file_size != size:
                    return ["Checkpoint payload size does not match its manifest."]
                total += info.file_size
                if total > 20_000_000:
                    return ["Checkpoint payload exceeds 20 MB."]
                data = archive.read(member)
            except (KeyError, OSError, RuntimeError, zipfile.BadZipFile):
                return ["Checkpoint payload file could not be read."]
            if len(data) != size or hashlib.sha256(data).hexdigest() != digest:
                return ["Checkpoint payload hash does not match its manifest."]
            content_hasher.update(b"F\x00")
            content_hasher.update(relative.encode("utf-8"))
            content_hasher.update(b"\x00")
            content_hasher.update(str(mode).encode("ascii"))
            content_hasher.update(b"\x00")
            content_hasher.update(data)
            content_hasher.update(b"\x00")

        deleted_paths: set[str] = set()
        for relative in deleted:
            if (
                not isinstance(relative, str)
                or not cls._safe_relative(relative)
                or relative in deleted_paths
                or relative in seen_paths
            ):
                return ["Checkpoint deleted-path record is unsafe."]
            deleted_paths.add(relative)
        for relative in sorted(deleted_paths):
            content_hasher.update(b"D\x00")
            content_hasher.update(relative.encode("utf-8"))
            content_hasher.update(b"\x00")

        if set(names) != expected_names:
            return ["Checkpoint payload archive members do not match its manifest."]
        if content_hasher.hexdigest() != payload.get("worktree_content_digest"):
            return ["Checkpoint payload content digest does not match its manifest."]
        return []

    def _checkpoint_manifest(self, loop: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        project_id = self._safe_id(loop.get("project_id"))
        loop_id = self._safe_id(loop.get("loop_id"))
        raw = loop.get("checkpoint_artifact_path")
        if not project_id or not loop_id or not isinstance(raw, str) or not raw:
            return None, ["Checkpoint artifact is missing."]
        path = Path(raw)
        try:
            path.resolve(strict=True).relative_to(self._project_dir(project_id).resolve(strict=True))
            with zipfile.ZipFile(path, "r") as archive:
                payload = json.loads(archive.read("manifest.json"))
                if not isinstance(payload, dict):
                    return None, ["Checkpoint manifest is not an object."]
                payload_blockers = self._checkpoint_payload_blockers(archive, payload)
        except (OSError, ValueError, KeyError, zipfile.BadZipFile, json.JSONDecodeError):
            return None, ["Checkpoint artifact is invalid."]
        if payload_blockers:
            return None, payload_blockers
        if (
            not isinstance(payload, dict)
            or payload.get("project_id") != project_id
            or payload.get("loop_id") != loop_id
            or payload.get("worktree_status_digest") != loop.get("checkpoint_status_digest")
            or payload.get("worktree_content_digest") != loop.get("checkpoint_content_digest")
        ):
            return None, ["Checkpoint manifest does not match the loop binding."]
        return payload, []

    def authorize_commit(self, *, loop: dict[str, Any]) -> CheckpointResult:
        _, worktree, status_digest, changed, blockers = self._validate_bound_state(loop)
        manifest, manifest_blockers = self._checkpoint_manifest(loop)
        blockers.extend(manifest_blockers)
        content_digest = ""
        if worktree is not None:
            content_digest, content_blockers = self._current_content_digest(worktree, changed)
            blockers.extend(content_blockers)
        if manifest is not None and status_digest != manifest.get("worktree_status_digest"):
            blockers.append("Worktree changed after checkpoint; create a new checkpoint.")
        if manifest is not None and content_digest != manifest.get("worktree_content_digest"):
            blockers.append("Worktree contents changed after checkpoint; create a new checkpoint.")
        if blockers:
            return CheckpointResult(False, blockers=blockers)
        project_id = self._safe_id(loop.get("project_id"))
        loop_id = self._safe_id(loop.get("loop_id"))
        auth_path = self._project_dir(project_id) / f"commit_authorization_{loop_id}.json"
        now = datetime.now(timezone.utc)
        authorization = {
            "schema_version": 1,
            "project_id": project_id,
            "loop_id": loop_id,
            "checkpoint_artifact_path": loop.get("checkpoint_artifact_path", ""),
            "worktree_status_digest": status_digest,
            "worktree_content_digest": content_digest,
            "authorized_at": now.isoformat(),
            "expires_at": (now + timedelta(minutes=15)).isoformat(),
            "used": False,
        }
        self._atomic_json(auth_path, authorization)
        self._supervisor.mark_status(
            project_id=project_id,
            loop_id=loop_id,
            status="ACCEPTED",
            commit_authorization_path=str(auth_path),
        )
        return CheckpointResult(True, artifact_path=str(auth_path))

    def commit(self, *, loop: dict[str, Any]) -> CheckpointResult:
        workspace, worktree, status_digest, changed, blockers = self._validate_bound_state(loop)
        if blockers or workspace is None or worktree is None:
            return CheckpointResult(False, blockers=blockers)
        content_digest, content_blockers = self._current_content_digest(worktree, changed)
        blockers.extend(content_blockers)
        manifest, manifest_blockers = self._checkpoint_manifest(loop)
        blockers.extend(manifest_blockers)
        if manifest is not None and status_digest != manifest.get("worktree_status_digest"):
            blockers.append("Worktree changed after checkpoint; commit authorization is invalid.")
        if manifest is not None and content_digest != manifest.get("worktree_content_digest"):
            blockers.append("Worktree contents changed after checkpoint; commit authorization is invalid.")
        auth_raw = loop.get("commit_authorization_path")
        project_id = self._safe_id(loop.get("project_id"))
        loop_id = self._safe_id(loop.get("loop_id"))
        expected_auth = self._project_dir(project_id) / f"commit_authorization_{loop_id}.json"
        if auth_raw != str(expected_auth):
            blockers.append("Separate commit authorization is missing.")
            authorization = None
        else:
            try:
                authorization = self._load_json(expected_auth)
            except (OSError, ValueError, json.JSONDecodeError):
                authorization = None
                blockers.append("Commit authorization artifact is invalid.")
        if authorization is not None:
            try:
                expires = datetime.fromisoformat(str(authorization.get("expires_at", "")).replace("Z", "+00:00"))
            except ValueError:
                expires = datetime.min.replace(tzinfo=timezone.utc)
            if authorization.get("used") is not False:
                blockers.append("Commit authorization was already used.")
            if expires <= datetime.now(timezone.utc):
                blockers.append("Commit authorization expired.")
            if authorization.get("checkpoint_artifact_path") != loop.get("checkpoint_artifact_path"):
                blockers.append("Commit authorization targets a different checkpoint.")
            if authorization.get("worktree_status_digest") != status_digest:
                blockers.append("Worktree changed after commit authorization.")
            if authorization.get("worktree_content_digest") != content_digest:
                blockers.append("Worktree contents changed after commit authorization.")
        if blockers:
            return CheckpointResult(False, blockers=blockers)
        if authorization is None:
            return CheckpointResult(False, blockers=["Commit authorization artifact is invalid."])

        staged = self._runtime.run(
            ("git", "add", "--all"), cwd=str(worktree), timeout_seconds=60
        )
        if staged.exit_code != 0:
            return CheckpointResult(False, blockers=["Git staging failed in the isolated worktree."])
        message = f"Autopilot: {loop.get('brief_id', loop_id)}"
        committed = self._runtime.run(
            ("git", "commit", "-m", message), cwd=str(worktree), timeout_seconds=120
        )
        if committed.exit_code != 0:
            return CheckpointResult(False, blockers=["Git commit failed in the isolated worktree."])
        revision = self._runtime.run(
            ("git", "rev-parse", "HEAD"), cwd=str(worktree), timeout_seconds=60
        )
        commit_revision = revision.stdout.strip()
        if revision.exit_code != 0 or not re.fullmatch(r"[0-9a-fA-F]{7,64}", commit_revision):
            return CheckpointResult(False, blockers=["Committed revision could not be verified."])
        authorization["used"] = True
        authorization["used_at"] = datetime.now(timezone.utc).isoformat()
        authorization["commit_revision"] = commit_revision
        self._atomic_json(expected_auth, authorization)
        self._supervisor.mark_status(
            project_id=project_id,
            loop_id=loop_id,
            status="ACCEPTED",
            commit_revision=commit_revision,
        )
        return CheckpointResult(True, artifact_path=str(expected_auth), revision=commit_revision)
