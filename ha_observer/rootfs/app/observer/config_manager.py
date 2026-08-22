"""Constrained, transactional Home Assistant YAML configuration management."""

from __future__ import annotations

import asyncio
import difflib
import hashlib
import hmac
import json
import os
import re
import secrets
import stat
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any, Protocol

import yaml

from .redaction import redact_text

MAX_FILE_BYTES = 1024 * 1024
MAX_EDIT_BYTES = 256 * 1024
MAX_READ_LINES = 500
MAX_DIFF_LENGTH = 30_000

_CHANGE_ID = re.compile(r"^[0-9a-f]{32}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_SENSITIVE_KEYS = {
    "access_token",
    "api_key",
    "authorization",
    "client_secret",
    "password",
    "refresh_token",
    "secret",
    "token",
}
_CREDENTIAL_VALUE = re.compile(
    r"(?i)(?:\bBearer\s+[A-Za-z0-9._~+/=-]{8,}|\bsk-[A-Za-z0-9_-]{12,}|"
    r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,})"
)
_FORBIDDEN_FILENAMES = {
    "known_devices.yaml",
    "secrets.yaml",
}
_FORBIDDEN_PARTS = {
    ".git",
    ".storage",
    "backup",
    "backups",
    "custom_components",
    "deps",
    "esphome",
    "ssl",
    "tts",
    "www",
}
_ALLOWED_DIRECTORIES = {
    "automations",
    "blueprints",
    "packages",
    "scenes",
    "scripts",
    "templates",
}


class ConfigCheckClient(Protocol):
    async def check_config(self) -> dict[str, Any]: ...


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _read_limited(path: Path) -> bytes:
    size = path.stat().st_size
    if size > MAX_FILE_BYTES:
        raise ValueError(f"YAML file exceeds the {MAX_FILE_BYTES}-byte safety limit")
    data = path.read_bytes()
    if len(data) > MAX_FILE_BYTES:
        raise ValueError(f"YAML file exceeds the {MAX_FILE_BYTES}-byte safety limit")
    return data


def _decode_yaml(data: bytes) -> str:
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("YAML file must use UTF-8 encoding") from exc


def _atomic_write(path: Path, data: bytes, *, mode: int) -> None:
    """Write data atomically without following a target symlink."""
    temporary = path.with_name(f".{path.name}.ha-observer-{secrets.token_hex(8)}.tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        temporary.unlink(missing_ok=True)


class ConfigManager:
    """Safely stage, validate, apply, and roll back single-file YAML changes."""

    def __init__(
        self,
        config_root: str | Path,
        data_root: str | Path,
        *,
        enabled: bool,
        client: ConfigCheckClient,
    ) -> None:
        self.config_root = Path(config_root).resolve()
        self.data_root = Path(data_root).resolve()
        self.enabled = enabled
        self.client = client
        self.changes_root = self.data_root / "changes"
        self.backups_root = self.data_root / "backups"
        self.lock = asyncio.Lock()

    def status(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "config_root_mounted": self.config_root.is_dir(),
            "workflow": "stage -> Home Assistant check -> confirmed apply -> optional rollback",
            "automatic_reload_or_restart": False,
        }

    def _require_enabled(self) -> None:
        if not self.enabled:
            raise PermissionError(
                "YAML configuration management is disabled. Enable "
                "config_management_enabled in the Home Assistant app options first."
            )
        if not self.config_root.is_dir():
            raise RuntimeError("The Home Assistant configuration mount is unavailable")

    def _relative_path(
        self, raw_path: str, *, allow_directory: bool = False
    ) -> PurePosixPath:
        value = raw_path.strip()
        if "\\" in value or "\x00" in value:
            raise ValueError("Invalid configuration path")
        if allow_directory and value in {"", "."}:
            return PurePosixPath(".")
        relative = PurePosixPath(value)
        if (
            not value
            or relative.is_absolute()
            or any(part in {"", ".", ".."} for part in relative.parts)
        ):
            raise ValueError("Configuration path must be a normalized relative path")
        if any(part.startswith(".") for part in relative.parts):
            raise PermissionError("Hidden configuration paths are blocked")
        lowered = tuple(part.casefold() for part in relative.parts)
        if any(part in _FORBIDDEN_PARTS for part in lowered):
            raise PermissionError("That configuration path is blocked by policy")
        if not allow_directory:
            if relative.suffix.casefold() not in {".yaml", ".yml"}:
                raise PermissionError("Only YAML files can be managed")
            if relative.name.casefold() in _FORBIDDEN_FILENAMES:
                raise PermissionError(
                    "Sensitive Home Assistant YAML files cannot be accessed"
                )
        if len(relative.parts) > 1 and lowered[0] not in _ALLOWED_DIRECTORIES:
            raise PermissionError(
                "Nested YAML access is limited to approved configuration folders"
            )
        return relative

    def _target(
        self,
        raw_path: str,
        *,
        must_exist: bool | None,
        allow_directory: bool = False,
    ) -> tuple[str, Path]:
        relative = self._relative_path(raw_path, allow_directory=allow_directory)
        target = (
            self.config_root
            if relative == PurePosixPath(".")
            else self.config_root.joinpath(*relative.parts)
        )
        current = self.config_root
        for part in () if relative == PurePosixPath(".") else relative.parts:
            current = current / part
            if current.exists() and current.is_symlink():
                raise PermissionError(
                    "Symbolic links are blocked for configuration access"
                )
        try:
            target.resolve(strict=False).relative_to(self.config_root)
        except ValueError as exc:
            raise PermissionError(
                "Configuration path escapes the allowed root"
            ) from exc
        if must_exist is True:
            if not target.is_file() or target.is_symlink():
                raise FileNotFoundError(
                    f"YAML configuration file does not exist: {relative.as_posix()}"
                )
        elif must_exist is False:
            if target.exists():
                raise FileExistsError(
                    f"YAML configuration file already exists: {relative.as_posix()}"
                )
            if not target.parent.is_dir() or target.parent.is_symlink():
                raise FileNotFoundError(
                    "The target configuration folder does not exist"
                )
        return relative.as_posix(), target

    @staticmethod
    def _validate_sha(expected_sha256: str) -> str:
        value = expected_sha256.strip().lower()
        if not _SHA256.fullmatch(value):
            raise ValueError(
                "expected_sha256 must be a 64-character lowercase SHA-256 digest"
            )
        return value

    @staticmethod
    def _validate_edit_text(value: str, *, name: str, allow_empty: bool = True) -> str:
        if not allow_empty and not value:
            raise ValueError(f"{name} cannot be empty")
        if len(value.encode("utf-8")) > MAX_EDIT_BYTES:
            raise ValueError(f"{name} exceeds the {MAX_EDIT_BYTES}-byte safety limit")
        if "\x00" in value:
            raise ValueError(f"{name} contains a null byte")
        if "[REDACTED" in value.upper():
            raise ValueError(
                f"{name} contains a redaction marker and cannot be written safely"
            )
        if _CREDENTIAL_VALUE.search(value):
            raise PermissionError(
                "Credential-like values cannot be written; use !secret references"
            )
        for line in value.splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or ":" not in stripped:
                continue
            key, raw_value = stripped.split(":", 1)
            normalized_key = key.strip(" '\"-").casefold().replace("-", "_")
            candidate_value = raw_value.strip()
            if (
                normalized_key in _SENSITIVE_KEYS
                and candidate_value
                and not candidate_value.startswith("!secret")
                and candidate_value not in {"null", "~", "''", '""'}
            ):
                raise PermissionError(
                    f"Literal value for sensitive key {normalized_key!r} is blocked; use !secret"
                )
        return value

    @staticmethod
    def _validate_yaml(content: str) -> None:
        if len(content.encode("utf-8")) > MAX_FILE_BYTES:
            raise ValueError(
                f"Resulting YAML exceeds the {MAX_FILE_BYTES}-byte safety limit"
            )
        try:
            list(yaml.compose_all(content, Loader=yaml.SafeLoader))
        except yaml.YAMLError as exc:
            problem = getattr(exc, "problem", None) or exc.__class__.__name__
            mark = getattr(exc, "problem_mark", None)
            location = (
                f" at line {mark.line + 1}, column {mark.column + 1}" if mark else ""
            )
            raise ValueError(f"YAML syntax error{location}: {problem}") from exc

    @staticmethod
    def _diff(path: str, original: str, candidate: str) -> str:
        diff = "".join(
            difflib.unified_diff(
                original.splitlines(keepends=True),
                candidate.splitlines(keepends=True),
                fromfile=f"a/{path}",
                tofile=f"b/{path}",
            )
        )
        return redact_text(diff, max_length=MAX_DIFF_LENGTH)

    def list_yaml(
        self, directory: str = "", *, recursive: bool, limit: int
    ) -> dict[str, Any]:
        self._require_enabled()
        if not 1 <= limit <= 500:
            raise ValueError("limit must be between 1 and 500")
        _, root = self._target(directory, must_exist=None, allow_directory=True)
        if not root.is_dir() or root.is_symlink():
            raise FileNotFoundError("Configuration directory does not exist")
        iterator = root.rglob("*") if recursive else root.iterdir()
        files: list[dict[str, Any]] = []
        total = 0
        for candidate in sorted(iterator):
            if not candidate.is_file() or candidate.is_symlink():
                continue
            try:
                relative = candidate.relative_to(self.config_root).as_posix()
                _, safe_target = self._target(relative, must_exist=True)
            except (FileNotFoundError, PermissionError, ValueError):
                continue
            total += 1
            if len(files) >= limit:
                continue
            data = _read_limited(safe_target)
            file_stat = safe_target.stat()
            files.append(
                {
                    "path": relative,
                    "size_bytes": len(data),
                    "sha256": _sha256(data),
                    "modified_at": datetime.fromtimestamp(
                        file_stat.st_mtime, UTC
                    ).isoformat(),
                }
            )
        return {
            "files": files,
            "returned": len(files),
            "total": total,
            "truncated": total > limit,
        }

    def read_yaml(self, path: str, *, start_line: int, end_line: int) -> dict[str, Any]:
        self._require_enabled()
        normalized, target = self._target(path, must_exist=True)
        data = _read_limited(target)
        content = _decode_yaml(data)
        lines = content.splitlines(keepends=True)
        if start_line < 1 or end_line < start_line:
            raise ValueError("Use a positive line range with end_line >= start_line")
        if end_line - start_line + 1 > MAX_READ_LINES:
            raise ValueError(f"At most {MAX_READ_LINES} lines can be read per call")
        excerpt = "".join(lines[start_line - 1 : end_line])
        safe_excerpt = redact_text(excerpt, max_length=100_000)
        return {
            "path": normalized,
            "sha256": _sha256(data),
            "size_bytes": len(data),
            "line_count": len(lines),
            "start_line": start_line,
            "end_line": min(end_line, len(lines)),
            "content": safe_excerpt,
            "content_redacted": safe_excerpt != excerpt,
        }

    def _change_path(self, change_id: str) -> Path:
        if not _CHANGE_ID.fullmatch(change_id):
            raise ValueError("Invalid staged change ID")
        return self.changes_root / f"{change_id}.json"

    def _save_change(self, change: dict[str, Any]) -> None:
        self.changes_root.mkdir(parents=True, exist_ok=True)
        path = self._change_path(str(change["change_id"]))
        _atomic_write(
            path,
            json.dumps(change, ensure_ascii=False, sort_keys=True, indent=2).encode(
                "utf-8"
            ),
            mode=0o600,
        )

    def _load_change(self, change_id: str) -> dict[str, Any]:
        path = self._change_path(change_id.strip().lower())
        if not path.is_file() or path.is_symlink():
            raise FileNotFoundError("Staged YAML change was not found")
        try:
            value = json.loads(_read_limited(path))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise RuntimeError("Staged YAML change metadata is corrupt") from exc
        if (
            not isinstance(value, dict)
            or value.get("change_id") != change_id.strip().lower()
        ):
            raise RuntimeError("Staged YAML change metadata is invalid")
        return value

    def _candidate(
        self, change: dict[str, Any]
    ) -> tuple[Path, bytes | None, bytes, int]:
        operation = change.get("operation")
        path = str(change.get("path", ""))
        if operation == "create":
            _, target = self._target(path, must_exist=False)
            original_bytes = None
            original = ""
            mode = 0o644
        else:
            _, target = self._target(path, must_exist=True)
            original_bytes = _read_limited(target)
            current_sha = _sha256(original_bytes)
            if not hmac.compare_digest(
                current_sha, str(change.get("expected_sha256", ""))
            ):
                raise RuntimeError(
                    "The YAML file changed after staging; stage a fresh change"
                )
            original = _decode_yaml(original_bytes)
            mode = stat.S_IMODE(target.stat().st_mode) or 0o644

        if operation == "replace":
            old_text = str(change.get("old_text", ""))
            if original.count(old_text) != 1:
                raise RuntimeError(
                    "The exact patch context no longer occurs once in the YAML file"
                )
            candidate = original.replace(old_text, str(change.get("new_text", "")), 1)
        elif operation == "append":
            addition = str(change.get("new_text", ""))
            separator = "" if not original or original.endswith("\n") else "\n"
            candidate = original + separator + addition
        elif operation == "create":
            candidate = str(change.get("new_text", ""))
        else:
            raise RuntimeError("Unknown staged YAML operation")

        self._validate_yaml(candidate)
        candidate_bytes = candidate.encode("utf-8")
        if not hmac.compare_digest(
            _sha256(candidate_bytes), str(change.get("candidate_sha256", ""))
        ):
            raise RuntimeError(
                "The staged YAML candidate no longer matches its recorded digest"
            )
        return target, original_bytes, candidate_bytes, mode

    def _new_change(
        self,
        *,
        path: str,
        operation: str,
        expected_sha256: str | None,
        old_text: str,
        new_text: str,
        original: str,
        candidate: str,
    ) -> dict[str, Any]:
        change = {
            "change_id": secrets.token_hex(16),
            "path": path,
            "operation": operation,
            "expected_sha256": expected_sha256,
            "old_text": old_text,
            "new_text": new_text,
            "candidate_sha256": _sha256(candidate.encode("utf-8")),
            "created_at": _utc_now(),
            "status": "staged",
        }
        self._save_change(change)
        return {
            "change_id": change["change_id"],
            "path": path,
            "operation": operation,
            "status": "staged",
            "candidate_sha256": change["candidate_sha256"],
            "yaml_syntax": "valid",
            "diff": self._diff(path, original, candidate),
            "next_step": "Run ha_check_staged_yaml before applying this change.",
        }

    def stage_patch(
        self,
        path: str,
        *,
        expected_sha256: str,
        old_text: str,
        new_text: str,
    ) -> dict[str, Any]:
        self._require_enabled()
        normalized, target = self._target(path, must_exist=True)
        expected = self._validate_sha(expected_sha256)
        old_text = self._validate_edit_text(
            old_text, name="old_text", allow_empty=False
        )
        new_text = self._validate_edit_text(new_text, name="new_text")
        original_bytes = _read_limited(target)
        if not hmac.compare_digest(_sha256(original_bytes), expected):
            raise RuntimeError(
                "The YAML file digest does not match; read it again before staging"
            )
        original = _decode_yaml(original_bytes)
        occurrences = original.count(old_text)
        if occurrences != 1:
            raise ValueError(
                f"old_text must occur exactly once; found {occurrences} occurrences"
            )
        candidate = original.replace(old_text, new_text, 1)
        if candidate == original:
            raise ValueError("The staged patch makes no change")
        self._validate_yaml(candidate)
        return self._new_change(
            path=normalized,
            operation="replace",
            expected_sha256=expected,
            old_text=old_text,
            new_text=new_text,
            original=original,
            candidate=candidate,
        )

    def stage_append(
        self, path: str, *, expected_sha256: str, content: str
    ) -> dict[str, Any]:
        self._require_enabled()
        normalized, target = self._target(path, must_exist=True)
        expected = self._validate_sha(expected_sha256)
        content = self._validate_edit_text(content, name="content", allow_empty=False)
        original_bytes = _read_limited(target)
        if not hmac.compare_digest(_sha256(original_bytes), expected):
            raise RuntimeError(
                "The YAML file digest does not match; read it again before staging"
            )
        original = _decode_yaml(original_bytes)
        separator = "" if not original or original.endswith("\n") else "\n"
        candidate = original + separator + content
        self._validate_yaml(candidate)
        return self._new_change(
            path=normalized,
            operation="append",
            expected_sha256=expected,
            old_text="",
            new_text=content,
            original=original,
            candidate=candidate,
        )

    def stage_create(self, path: str, *, content: str) -> dict[str, Any]:
        self._require_enabled()
        normalized, _ = self._target(path, must_exist=False)
        content = self._validate_edit_text(content, name="content", allow_empty=False)
        self._validate_yaml(content)
        return self._new_change(
            path=normalized,
            operation="create",
            expected_sha256=None,
            old_text="",
            new_text=content,
            original="",
            candidate=content,
        )

    def preview(self, change_id: str) -> dict[str, Any]:
        self._require_enabled()
        change = self._load_change(change_id)
        target, original_bytes, candidate_bytes, _ = self._candidate(change)
        original = _decode_yaml(original_bytes) if original_bytes is not None else ""
        candidate = _decode_yaml(candidate_bytes)
        return {
            "change_id": change["change_id"],
            "path": target.relative_to(self.config_root).as_posix(),
            "operation": change["operation"],
            "status": change["status"],
            "created_at": change["created_at"],
            "candidate_sha256": change["candidate_sha256"],
            "diff": self._diff(str(change["path"]), original, candidate),
        }

    def _ensure_backup(
        self,
        change: dict[str, Any],
        *,
        original_bytes: bytes | None,
        mode: int,
    ) -> None:
        backup_dir = self.backups_root / str(change["change_id"])
        backup_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(backup_dir, 0o700)
        metadata = {
            "change_id": change["change_id"],
            "path": change["path"],
            "original_exists": original_bytes is not None,
            "original_sha256": _sha256(original_bytes)
            if original_bytes is not None
            else None,
            "original_mode": mode,
            "created_at": _utc_now(),
        }
        if original_bytes is not None:
            _atomic_write(backup_dir / "original.yaml", original_bytes, mode=0o600)
        _atomic_write(
            backup_dir / "metadata.json",
            json.dumps(metadata, sort_keys=True, indent=2).encode("utf-8"),
            mode=0o600,
        )

    def _restore_original(
        self,
        change: dict[str, Any],
        *,
        target: Path,
        original_bytes: bytes | None,
        candidate_bytes: bytes,
        mode: int,
    ) -> None:
        candidate_sha = _sha256(candidate_bytes)
        if target.exists():
            if target.is_symlink() or not target.is_file():
                raise RuntimeError(
                    "Configuration target changed type during validation"
                )
            current_sha = _sha256(_read_limited(target))
            original_sha = (
                _sha256(original_bytes) if original_bytes is not None else None
            )
            if original_sha and hmac.compare_digest(current_sha, original_sha):
                return
            if not hmac.compare_digest(current_sha, candidate_sha):
                raise RuntimeError(
                    "Configuration target changed externally during validation"
                )
        elif original_bytes is not None:
            raise RuntimeError("Configuration target disappeared during validation")
        else:
            return

        if original_bytes is None:
            target.unlink()
        else:
            _atomic_write(target, original_bytes, mode=mode)

    async def check(self, change_id: str) -> dict[str, Any]:
        self._require_enabled()
        async with self.lock:
            change = self._load_change(change_id)
            if change.get("status") not in {"staged", "invalid", "check_failed"}:
                raise RuntimeError(
                    f"Change cannot be checked while status is {change.get('status')!r}"
                )
            target, original_bytes, candidate_bytes, mode = self._candidate(change)
            self._ensure_backup(change, original_bytes=original_bytes, mode=mode)
            change.update({"status": "checking", "check_started_at": _utc_now()})
            self._save_change(change)
            _atomic_write(target, candidate_bytes, mode=mode)

            check_result: dict[str, Any] | None = None
            check_error: Exception | None = None
            try:
                check_result = await self.client.check_config()
            except Exception as exc:  # noqa: BLE001 - external API boundary
                check_error = exc
            try:
                self._restore_original(
                    change,
                    target=target,
                    original_bytes=original_bytes,
                    candidate_bytes=candidate_bytes,
                    mode=mode,
                )
            except Exception:
                change.update(
                    {"status": "recovery_conflict", "check_finished_at": _utc_now()}
                )
                self._save_change(change)
                raise

            if check_error is not None:
                change.update(
                    {"status": "check_failed", "check_finished_at": _utc_now()}
                )
                self._save_change(change)
                raise RuntimeError(
                    "Home Assistant configuration check failed; the original file was restored: "
                    + redact_text(str(check_error), max_length=1_000)
                ) from check_error

            assert check_result is not None
            valid = check_result.get("result") == "valid"
            change.update(
                {
                    "status": "validated" if valid else "invalid",
                    "check_finished_at": _utc_now(),
                    "ha_result": "valid" if valid else "invalid",
                    "ha_errors": redact_text(
                        str(check_result.get("errors") or ""), max_length=10_000
                    ),
                    "ha_warnings": redact_text(
                        str(check_result.get("warnings") or ""), max_length=10_000
                    ),
                }
            )
            if valid:
                change["approval_code"] = secrets.token_urlsafe(24)
            else:
                change.pop("approval_code", None)
            self._save_change(change)
            response = {
                "change_id": change["change_id"],
                "path": change["path"],
                "status": change["status"],
                "home_assistant_result": change["ha_result"],
                "errors": change["ha_errors"] or None,
                "warnings": change["ha_warnings"] or None,
                "original_restored_after_check": True,
            }
            if valid:
                response.update(
                    {
                        "approval_code": change["approval_code"],
                        "next_step": (
                            "After explicit user confirmation, call ha_apply_staged_yaml with this "
                            "change_id and approval_code."
                        ),
                    }
                )
            return response

    async def apply(self, change_id: str, *, approval_code: str) -> dict[str, Any]:
        self._require_enabled()
        async with self.lock:
            change = self._load_change(change_id)
            if change.get("status") != "validated":
                raise RuntimeError(
                    "Only a successfully checked staged change can be applied"
                )
            expected_code = str(change.get("approval_code", ""))
            if not expected_code or not hmac.compare_digest(
                expected_code, approval_code
            ):
                raise PermissionError("The approval code is missing or invalid")
            target, original_bytes, candidate_bytes, mode = self._candidate(change)
            self._ensure_backup(change, original_bytes=original_bytes, mode=mode)
            change.update({"status": "applying", "apply_started_at": _utc_now()})
            self._save_change(change)
            try:
                _atomic_write(target, candidate_bytes, mode=mode)
            except Exception:
                change.update(
                    {"status": "validated", "approval_code": secrets.token_urlsafe(24)}
                )
                self._save_change(change)
                raise
            if not hmac.compare_digest(
                _sha256(_read_limited(target)), _sha256(candidate_bytes)
            ):
                raise RuntimeError(
                    "The applied YAML file failed its post-write digest check"
                )
            change.update(
                {
                    "status": "applied",
                    "applied_at": _utc_now(),
                    "applied_sha256": _sha256(candidate_bytes),
                    "rollback_code": secrets.token_urlsafe(24),
                }
            )
            change.pop("approval_code", None)
            self._save_change(change)
            return {
                "change_id": change["change_id"],
                "path": change["path"],
                "status": "applied",
                "sha256": change["applied_sha256"],
                "rollback_code": change["rollback_code"],
                "reloaded_or_restarted": False,
                "note": (
                    "The YAML file is now on disk. This version deliberately does not reload "
                    "integrations or restart Home Assistant."
                ),
            }

    async def rollback(self, change_id: str, *, rollback_code: str) -> dict[str, Any]:
        self._require_enabled()
        async with self.lock:
            change = self._load_change(change_id)
            if change.get("status") != "applied":
                raise RuntimeError("Only an applied YAML change can be rolled back")
            expected_code = str(change.get("rollback_code", ""))
            if not expected_code or not hmac.compare_digest(
                expected_code, rollback_code
            ):
                raise PermissionError("The rollback code is missing or invalid")
            _, target = self._target(str(change["path"]), must_exist=True)
            current = _read_limited(target)
            if not hmac.compare_digest(
                _sha256(current), str(change.get("applied_sha256", ""))
            ):
                raise RuntimeError(
                    "The YAML file changed after apply; refusing to overwrite newer work"
                )
            backup_dir = self.backups_root / str(change["change_id"])
            metadata_path = backup_dir / "metadata.json"
            if not metadata_path.is_file() or metadata_path.is_symlink():
                raise RuntimeError("Rollback metadata is unavailable")
            metadata = json.loads(_read_limited(metadata_path))
            change.update({"status": "rolling_back", "rollback_started_at": _utc_now()})
            self._save_change(change)
            if metadata.get("original_exists"):
                backup_path = backup_dir / "original.yaml"
                if not backup_path.is_file() or backup_path.is_symlink():
                    raise RuntimeError("Rollback backup is unavailable")
                original = _read_limited(backup_path)
                if not hmac.compare_digest(
                    _sha256(original), str(metadata.get("original_sha256", ""))
                ):
                    raise RuntimeError("Rollback backup digest is invalid")
                _atomic_write(
                    target, original, mode=int(metadata.get("original_mode", 0o644))
                )
                restored_sha: str | None = _sha256(original)
            else:
                target.unlink()
                restored_sha = None
            change.update({"status": "rolled_back", "rolled_back_at": _utc_now()})
            change.pop("rollback_code", None)
            self._save_change(change)
            return {
                "change_id": change["change_id"],
                "path": change["path"],
                "status": "rolled_back",
                "restored_sha256": restored_sha,
                "reloaded_or_restarted": False,
            }

    def recover_interrupted_operations(self) -> list[dict[str, str]]:
        """Recover a file if the app stopped during check, apply, or rollback."""
        if not self.changes_root.is_dir() or not self.config_root.is_dir():
            return []
        recovered: list[dict[str, str]] = []
        for change_path in sorted(self.changes_root.glob("*.json")):
            if change_path.is_symlink() or not _CHANGE_ID.fullmatch(change_path.stem):
                continue
            try:
                change = self._load_change(change_path.stem)
                operation_status = change.get("status")
                if operation_status not in {"applying", "checking", "rolling_back"}:
                    continue
                _, target = self._target(str(change["path"]), must_exist=None)
                backup_dir = self.backups_root / str(change["change_id"])
                metadata = json.loads(_read_limited(backup_dir / "metadata.json"))
                candidate_sha = str(change.get("candidate_sha256", ""))
                original_sha = str(metadata.get("original_sha256") or "")
                current_sha = None
                if target.is_file() and not target.is_symlink():
                    current_sha = _sha256(_read_limited(target))
                original_is_current = (
                    bool(metadata.get("original_exists"))
                    and current_sha is not None
                    and hmac.compare_digest(current_sha, original_sha)
                ) or (not metadata.get("original_exists") and not target.exists())
                candidate_is_current = current_sha is not None and hmac.compare_digest(
                    current_sha, candidate_sha
                )

                if operation_status == "applying":
                    if candidate_is_current:
                        change.update(
                            {
                                "status": "applied",
                                "applied_at": _utc_now(),
                                "applied_sha256": candidate_sha,
                                "rollback_code": secrets.token_urlsafe(24),
                            }
                        )
                        change.pop("approval_code", None)
                        outcome = "applied"
                    elif original_is_current:
                        change.update(
                            {
                                "status": "validated",
                                "approval_code": secrets.token_urlsafe(24),
                            }
                        )
                        outcome = "not_applied"
                    else:
                        raise RuntimeError(
                            "Configuration file has an unexpected digest during apply recovery"
                        )
                    self._save_change(change)
                    recovered.append({"change_id": change_path.stem, "status": outcome})
                    continue

                if operation_status == "rolling_back":
                    if original_is_current:
                        change.update(
                            {"status": "rolled_back", "rolled_back_at": _utc_now()}
                        )
                        change.pop("rollback_code", None)
                        outcome = "rolled_back"
                    elif candidate_is_current:
                        change.update(
                            {
                                "status": "applied",
                                "rollback_code": secrets.token_urlsafe(24),
                            }
                        )
                        outcome = "not_rolled_back"
                    else:
                        raise RuntimeError(
                            "Configuration file has an unexpected digest during rollback recovery"
                        )
                    self._save_change(change)
                    recovered.append({"change_id": change_path.stem, "status": outcome})
                    continue

                if metadata.get("original_exists"):
                    original = _read_limited(backup_dir / "original.yaml")
                    if candidate_is_current:
                        _atomic_write(
                            target,
                            original,
                            mode=int(metadata.get("original_mode", 0o644)),
                        )
                    elif not original_is_current:
                        raise RuntimeError(
                            "Configuration file has an unexpected digest"
                        )
                elif candidate_is_current:
                    target.unlink()
                elif not original_is_current:
                    raise RuntimeError("New configuration file changed during recovery")
                change.update({"status": "check_failed", "recovered_at": _utc_now()})
                self._save_change(change)
                recovered.append({"change_id": change_path.stem, "status": "restored"})
            except Exception as exc:  # noqa: BLE001 - startup recovery must continue
                try:
                    change = self._load_change(change_path.stem)
                    change.update(
                        {
                            "status": "recovery_conflict",
                            "recovery_error": redact_text(str(exc), max_length=500),
                            "recovered_at": _utc_now(),
                        }
                    )
                    self._save_change(change)
                except Exception:
                    pass
                recovered.append({"change_id": change_path.stem, "status": "conflict"})
        return recovered
