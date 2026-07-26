#!/usr/bin/env python3
"""Monotonic fencing authority for the canonical Step5d bridge owner."""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import hashlib
import json
import os
import re
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Mapping

sys.path.insert(0, str(Path(__file__).resolve().parent))

from step5d_autotune_v3.governance import (
    publish_launch_attempt,
    read_proc_starttime_ticks,
)


SCHEMA = "step5d.bridge/owner-authority-v1"
STATE_FILE = "owner-authority.json"
LOCK_FILE = ".owner-authority.lock"
DEFAULT_AUTHORITY_ROOT = (
    Path("/run/user") / str(os.getuid()) / "step5d-autotune-v3" / "resources"
)
DEFAULT_RESOURCE_ID = "step5d-bridge-writer"
RESOURCE_ID_PATTERN = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?$")

OWNER_REQUIRED_FIELDS = {
    "pid",
    "starttime_ticks",
}
BASE_REQUIRED_FIELDS = {
    "schema",
    "sequence",
    "state",
    "attempt_id",
    "owner",
    "activated_at_unix_ns",
    "revoked_at_unix_ns",
    "reason",
    "authority_epoch",
    "resource_id",
    "worktree_root",
    "repository_head",
    "launch_basis_path",
    "launch_basis_sha256",
}
ALLOWED_REASONS = {"completed", "cancelled", "failed"}


class BridgeAuthorityError(RuntimeError):
    pass


def _normalize_resource_id(resource_id: str | None) -> str:
    resolved = resource_id or DEFAULT_RESOURCE_ID
    if not RESOURCE_ID_PATTERN.fullmatch(resolved):
        raise BridgeAuthorityError(
            "resource_id must match ^[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?$"
        )
    return resolved


def _safe_path(path: Path | None) -> Path:
    if path is None:
        raise BridgeAuthorityError("bridge authority path is missing")
    normalized = path.expanduser()
    if not normalized.is_absolute():
        normalized = Path.cwd() / normalized
    cursor = normalized
    while True:
        if cursor.is_symlink():
            raise BridgeAuthorityError("bridge authority path is unsafe")
        if cursor.parent == cursor:
            break
        cursor = cursor.parent
    return normalized


def _safe_root(authority_root: Path | None, resource_id: str | None) -> Path:
    if authority_root is None:
        normalized_resource_id = _normalize_resource_id(resource_id)
        root = _safe_path(DEFAULT_AUTHORITY_ROOT / normalized_resource_id).resolve()
    else:
        root = _safe_path(authority_root).resolve()
    return root


def _read_boot_id() -> str | None:
    try:
        value = Path("/proc/sys/kernel/random/boot_id").read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return value or None


def _read_worktree_head(worktree_root: Path) -> str:
    git_root = worktree_root / ".git"
    if git_root.is_file():
        try:
            pointer = git_root.read_text(encoding="utf-8").strip()
        except OSError:
            return "unknown"
        if not pointer.startswith("gitdir:"):
            return "unknown"
        target = Path(pointer.removeprefix("gitdir:").strip())
        if not target.is_absolute():
            target = git_root.parent / target
        git_root = target.resolve()
    elif not git_root.is_dir():
        return "unknown"
    head_path = git_root / "HEAD"
    try:
        raw = head_path.read_text(encoding="utf-8").strip()
    except OSError:
        return "unknown"
    if raw.startswith("ref: "):
        ref_path = git_root / raw.removeprefix("ref: ")
        try:
            raw = ref_path.read_text(encoding="utf-8").strip()
        except OSError:
            return "unknown"
    return raw or "unknown"


def _read_launch_basis(path: Path) -> tuple[str, str]:
    candidate = _safe_path(path).resolve()
    if candidate.is_symlink() or not candidate.is_file():
        raise BridgeAuthorityError("bridge authority launch basis path is unsafe")
    try:
        encoded = candidate.read_bytes()
    except OSError as exc:
        raise BridgeAuthorityError(
            f"bridge authority launch basis is unavailable: {exc}"
        ) from exc
    digest = hashlib.sha256(encoded).hexdigest()
    try:
        payload = json.loads(encoded)
    except (UnicodeDecodeError, json.JSONDecodeError):
        payload = None
    if isinstance(payload, dict) and "basis_sha256" in payload:
        supplied = payload.get("basis_sha256")
        unsigned = dict(payload)
        unsigned.pop("basis_sha256", None)
        canonical = json.dumps(
            unsigned,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        ).encode("utf-8")
        expected = hashlib.sha256(canonical).hexdigest()
        if not _validate_required_hex(supplied, length=64) or supplied != expected:
            raise BridgeAuthorityError("bridge authority launch basis digest differs")
        digest = supplied
    return str(candidate), digest


def _validate_required_hex(value: object, *, length: int) -> bool:
    if not isinstance(value, str) or len(value) != length:
        return False
    return all(character in "0123456789abcdef" for character in value)


def _atomic_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.is_symlink() or path.parent.is_symlink():
        raise BridgeAuthorityError("bridge authority destination is unsafe")
    encoded = (
        json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n"
    ).encode("utf-8")
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary_path = Path(temporary)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_path, path)
        directory = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        temporary_path.unlink(missing_ok=True)


def _normalize_attempt_value(value: object) -> str:
    if not isinstance(value, str) or not value:
        raise BridgeAuthorityError("bridge authority attempt id is invalid")
    return value


def _normalize_sequence_value(value: object) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise BridgeAuthorityError("bridge authority sequence is invalid")
    return value


def _normalize_owner(owner_pid: int | None, owner_starttime_ticks: int | None) -> dict[str, int]:
    if owner_pid is None or owner_starttime_ticks is None:
        raise BridgeAuthorityError("bridge authority owner is required")
    if not isinstance(owner_pid, int) or owner_pid <= 0:
        raise BridgeAuthorityError("bridge authority owner pid is invalid")
    if not isinstance(owner_starttime_ticks, int) or owner_starttime_ticks < 0:
        raise BridgeAuthorityError("bridge authority owner starttime is invalid")
    return {"pid": owner_pid, "starttime_ticks": owner_starttime_ticks}


def _load(path: Path) -> dict[str, Any] | None:
    if path.is_symlink():
        raise BridgeAuthorityError("bridge owner authority path is unsafe")
    if not path.exists():
        return None
    if not path.is_file():
        raise BridgeAuthorityError("bridge owner authority path is unsafe")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BridgeAuthorityError(f"bridge owner authority is invalid: {exc}") from exc

    if not isinstance(payload, dict):
        raise BridgeAuthorityError("bridge owner authority fields differ")

    if not BASE_REQUIRED_FIELDS.issubset(payload):
        raise BridgeAuthorityError("bridge owner authority fields differ")

    owner = payload.get("owner")
    if not (
        isinstance(owner, Mapping)
        and OWNER_REQUIRED_FIELDS.issubset(owner)
        and isinstance(owner["pid"], int)
        and not isinstance(owner["pid"], bool)
        and owner["pid"] > 0
        and isinstance(owner["starttime_ticks"], int)
        and not isinstance(owner["starttime_ticks"], bool)
        and owner["starttime_ticks"] >= 0
    ):
        raise BridgeAuthorityError("bridge owner authority owner fields differ")

    if payload["schema"] != SCHEMA:
        raise BridgeAuthorityError("bridge owner authority fields differ")

    if (
        not isinstance(payload.get("sequence"), int)
        or isinstance(payload.get("sequence"), bool)
        or payload["sequence"] < 1
        or not isinstance(payload.get("authority_epoch"), int)
        or isinstance(payload.get("authority_epoch"), bool)
        or payload["authority_epoch"] < 1
    ):
        raise BridgeAuthorityError("bridge owner authority fields differ")

    if payload.get("state") not in {"ACTIVE", "REVOKED"}:
        raise BridgeAuthorityError("bridge owner authority fields differ")

    if not isinstance(payload.get("attempt_id"), str) or not payload["attempt_id"]:
        raise BridgeAuthorityError("bridge owner authority fields differ")

    if not isinstance(payload.get("activated_at_unix_ns"), int) or payload[
        "activated_at_unix_ns"
    ] < 0:
        raise BridgeAuthorityError("bridge owner authority fields differ")

    if payload["state"] == "ACTIVE":
        if payload["revoked_at_unix_ns"] is not None or payload["reason"] is not None:
            raise BridgeAuthorityError("active bridge authority contains revocation state")
    else:
        if (
            not isinstance(payload.get("revoked_at_unix_ns"), int)
            or payload["revoked_at_unix_ns"] < 0
            or payload.get("reason") not in ALLOWED_REASONS
        ):
            raise BridgeAuthorityError("revoked bridge authority is incomplete")

    if (
        not isinstance(payload.get("resource_id"), str)
        or not payload["resource_id"]
        or not isinstance(payload.get("worktree_root"), str)
        or not payload["worktree_root"]
        or not isinstance(payload.get("repository_head"), str)
        or not payload.get("repository_head")
    ):
        raise BridgeAuthorityError("bridge owner authority fields differ")
    if payload.get("launch_basis_path") is None and payload.get("launch_basis_sha256") is None:
        pass
    elif (
        payload.get("launch_basis_path") is None
        or payload.get("launch_basis_sha256") is None
        or not isinstance(payload["launch_basis_path"], str)
        or not payload["launch_basis_path"]
        or not isinstance(payload["launch_basis_sha256"], str)
        or not _validate_required_hex(payload["launch_basis_sha256"], length=64)
    ):
        raise BridgeAuthorityError("bridge owner authority fields differ")

    return payload


def _normalize_boot_id(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise BridgeAuthorityError("bridge authority boot-id field is invalid")
    return value


def _is_live_owner(owner: dict[str, int]) -> bool:
    starttime = read_proc_starttime_ticks(owner["pid"])
    return starttime is not None and starttime == owner["starttime_ticks"]


def _is_stale(current: dict[str, Any]) -> bool:
    if current.get("state") != "ACTIVE":
        return False
    current_boot_id = current.get("boot_id")
    now_boot_id = _read_boot_id()
    if isinstance(current_boot_id, str) and isinstance(now_boot_id, str):
        if current_boot_id != now_boot_id:
            return True
    return not _is_live_owner(current["owner"])


@contextlib.contextmanager
def _locked_state(root: Path):
    lock_path = root / LOCK_FILE
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    with lock_path.open("a+") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def _resolve_authority_root(authority_root: Path | None, resource_id: str | None) -> Path:
    return _safe_root(authority_root, resource_id)


def _resolve_worktree_and_head(
    worktree_root: str | None,
    repository_head: str | None,
) -> tuple[str, str]:
    if worktree_root is None:
        worktree_root_path = Path.cwd()
    else:
        worktree_root_path = Path(worktree_root).expanduser()
    worktree_root_path = worktree_root_path.resolve()
    repository = repository_head or _read_worktree_head(worktree_root_path)
    if not repository:
        raise BridgeAuthorityError("repository head is unavailable")
    return str(worktree_root_path), repository


def _resolve_launch_context(
    authority_root: Path | None,
    worktree_root: str | None,
    repository_head: str | None,
    launch_basis_path: str | None,
    launch_basis_sha256: str | None,
) -> tuple[str, str, str, str]:
    worktree_root_path, repository = _resolve_worktree_and_head(
        worktree_root, repository_head
    )
    if launch_basis_path is None and launch_basis_sha256 is None:
        if authority_root is None:
            raise BridgeAuthorityError("launch-basis path and sha256 are required")
        return (
            worktree_root_path,
            repository,
            * _read_launch_basis(Path(__file__)),
        )
    if launch_basis_path is None or launch_basis_sha256 is None:
        raise BridgeAuthorityError(
            "launch-basis path and sha256 must be provided together"
        )
    resolved_path, resolved_sha = _read_launch_basis(Path(launch_basis_path))
    if launch_basis_sha256 != resolved_sha:
        raise BridgeAuthorityError("launch-basis sha256 does not match")
    return worktree_root_path, repository, resolved_path, resolved_sha


def _assert_basis_bound(
    current: Mapping[str, Any],
    launch_basis_path: str | None = None,
    launch_basis_sha256: str | None = None,
) -> None:
    if current.get("launch_basis_path") is None or current.get("launch_basis_sha256") is None:
        raise BridgeAuthorityError("bridge authority launch basis is not bound")
    if launch_basis_path is None and launch_basis_sha256 is None:
        return
    if current.get("launch_basis_path") != launch_basis_path:
        raise BridgeAuthorityError("bridge authority launch-basis path differs")
    if current.get("launch_basis_sha256") != launch_basis_sha256:
        raise BridgeAuthorityError("bridge authority launch-basis sha256 differs")


def _resolve_bind_basis(
    launch_basis_path: str | None,
    launch_basis_sha256: str | None,
) -> tuple[str, str]:
    if launch_basis_path is None or launch_basis_sha256 is None:
        raise BridgeAuthorityError("launch-basis path and sha256 must be provided together")
    resolved_path, resolved_sha = _read_launch_basis(Path(launch_basis_path))
    if launch_basis_sha256 != resolved_sha:
        raise BridgeAuthorityError("launch-basis sha256 does not match")
    return resolved_path, resolved_sha


def load_current(
    authority_root: Path | None = None,
    *,
    attempt_id: str | None = None,
    owner_pid: int | None = None,
    owner_starttime_ticks: int | None = None,
    worktree_root: str | None = None,
    repository_head: str | None = None,
    launch_basis_path: str | None = None,
    launch_basis_sha256: str | None = None,
    require_basis_bound: bool = False,
    resource_id: str | None = DEFAULT_RESOURCE_ID,
) -> dict[str, Any] | None:
    if launch_basis_path is None and launch_basis_sha256 is not None:
        raise BridgeAuthorityError("launch-basis path and sha256 must be provided together")
    if launch_basis_sha256 is None and launch_basis_path is not None:
        raise BridgeAuthorityError("launch-basis path and sha256 must be provided together")
    root = _resolve_authority_root(authority_root, resource_id)
    state = _load(root / STATE_FILE)
    if state is None:
        return None
    if attempt_id is not None and state["attempt_id"] != attempt_id:
        raise BridgeAuthorityError("bridge authority attempt_id differs")
    owner = state.get("owner", {})
    if (
        owner_pid is not None
        and owner_starttime_ticks is not None
        and (
            owner.get("pid") != owner_pid
            or owner.get("starttime_ticks") != owner_starttime_ticks
        )
    ):
        raise BridgeAuthorityError("bridge authority owner binding differs")
    if worktree_root is not None and state.get("worktree_root") != worktree_root:
        raise BridgeAuthorityError("bridge authority worktree root differs")
    if repository_head is not None and state.get("repository_head") != repository_head:
        raise BridgeAuthorityError("bridge authority repository head differs")
    if launch_basis_path is not None and state.get("launch_basis_path") != launch_basis_path:
        raise BridgeAuthorityError("bridge authority launch-basis path differs")
    if launch_basis_sha256 is not None and state.get(
        "launch_basis_sha256"
    ) != launch_basis_sha256:
        raise BridgeAuthorityError("bridge authority launch-basis sha256 differs")
    if require_basis_bound:
        _assert_basis_bound(state, launch_basis_path, launch_basis_sha256)
    state["boot_id"] = _normalize_boot_id(state.get("boot_id"))
    return state


def require_active(
    authority_root: Path,
    *,
    attempt_id: str,
    sequence: int,
    owner_pid: int | None = None,
    owner_starttime_ticks: int | None = None,
    launch_basis_path: str | None = None,
    launch_basis_sha256: str | None = None,
    require_basis_bound: bool = False,
    resource_id: str | None = DEFAULT_RESOURCE_ID,
) -> dict[str, Any]:
    current = load_current(
        authority_root,
        attempt_id=attempt_id,
        owner_pid=owner_pid,
        owner_starttime_ticks=owner_starttime_ticks,
        launch_basis_path=launch_basis_path,
        launch_basis_sha256=launch_basis_sha256,
        require_basis_bound=require_basis_bound,
        resource_id=resource_id,
    )
    if (
        not isinstance(current, dict)
        or current.get("state") != "ACTIVE"
        or current.get("sequence") != sequence
        or current.get("attempt_id") != attempt_id
    ):
        raise BridgeAuthorityError(
            "bridge authority is not the expected ACTIVE root/attempt/sequence"
        )
    return current


def require_revoked(
    authority_root: Path,
    *,
    attempt_id: str,
    minimum_sequence: int,
    resource_id: str | None = DEFAULT_RESOURCE_ID,
) -> dict[str, Any]:
    current = load_current(authority_root, attempt_id=attempt_id, resource_id=resource_id)
    if (
        not isinstance(current, dict)
        or current.get("state") != "REVOKED"
        or not isinstance(current.get("sequence"), int)
        or current.get("sequence", 0) < minimum_sequence
    ):
        raise BridgeAuthorityError(
            "bridge authority is not the expected REVOKED root/attempt"
        )
    return current


class AuthorityFence:
    """Fence wrapper used at runtime boundaries."""

    def __init__(
        self,
        authority_root: Path,
        *,
        attempt_id: str,
        sequence: int,
        owner_pid: int | None = None,
        owner_starttime_ticks: int | None = None,
        launch_basis_path: str | None = None,
        launch_basis_sha256: str | None = None,
        require_basis_bound: bool = True,
        resource_id: str = DEFAULT_RESOURCE_ID,
    ) -> None:
        self.root = _safe_root(authority_root, None)
        self.attempt_id = attempt_id
        self.sequence = sequence
        self.owner_pid = owner_pid
        self.owner_starttime_ticks = owner_starttime_ticks
        self.launch_basis_path = launch_basis_path
        self.launch_basis_sha256 = launch_basis_sha256
        self.require_basis_bound = require_basis_bound
        self.resource_id = resource_id

    def assert_active(self) -> dict[str, Any]:
        return require_active(
            self.root,
            attempt_id=self.attempt_id,
            sequence=self.sequence,
            owner_pid=self.owner_pid,
            owner_starttime_ticks=self.owner_starttime_ticks,
            launch_basis_path=self.launch_basis_path,
            launch_basis_sha256=self.launch_basis_sha256,
            require_basis_bound=self.require_basis_bound,
            resource_id=self.resource_id,
        )

    def assert_revoked(self) -> dict[str, Any]:
        return require_revoked(
            self.root,
            attempt_id=self.attempt_id,
            minimum_sequence=self.sequence + 1,
            resource_id=self.resource_id,
        )


def _caller_owner(owner_pid: int, owner_starttime_ticks: int) -> None:
    if os.getppid() != owner_pid:
        raise BridgeAuthorityError("bound shell owner differs")
    if read_proc_starttime_ticks(owner_pid) != owner_starttime_ticks:
        raise BridgeAuthorityError("bound shell owner differs")


def begin(
    authority_root: Path | None = None,
    attempt_id: str | None = None,
    owner_pid: int | None = None,
    owner_starttime_ticks: int | None = None,
    *,
    worktree_root: str | None = None,
    repository_head: str | None = None,
    launch_basis_path: str | None = None,
    launch_basis_sha256: str | None = None,
    resource_id: str | None = DEFAULT_RESOURCE_ID,
) -> dict[str, Any]:
    normalized_attempt = _normalize_attempt_value(attempt_id)
    owner = _normalize_owner(owner_pid, owner_starttime_ticks)
    _caller_owner(owner["pid"], owner["starttime_ticks"])
    root = _resolve_authority_root(authority_root, resource_id)
    if authority_root is None:
        worktree_root, repository_head = _resolve_worktree_and_head(
            worktree_root, repository_head
        )
        if launch_basis_path is not None or launch_basis_sha256 is not None:
            _resolve_bind_basis(launch_basis_path, launch_basis_sha256)
        launch_basis_path = None
        launch_basis_sha256 = None
    else:
        worktree_root, repository_head, launch_basis_path, launch_basis_sha256 = (
            _resolve_launch_context(
                authority_root,
                worktree_root,
                repository_head,
                launch_basis_path,
                launch_basis_sha256,
            )
        )

    with _locked_state(root):
        current = _load(root / STATE_FILE)
        if current is not None:
            if _is_stale(current):
                stale_owner = current.get("owner")
                stale_epoch = int(current.get("authority_epoch", current["sequence"]))
            else:
                stale_owner = None
                stale_epoch = int(current.get("authority_epoch", current["sequence"]))

            if current.get("state") == "ACTIVE" and stale_owner is None:
                owner_match = (
                    current["owner"]["pid"] == owner["pid"]
                    and current["owner"]["starttime_ticks"] == owner["starttime_ticks"]
                )
                if owner_match and current["attempt_id"] == normalized_attempt:
                    return current
                raise BridgeAuthorityError("another canonical owner is already active")

            if current.get("state") == "ACTIVE" and stale_owner is not None:
                # recover from dead/pid-recycled/boot-changed live owner
                next_sequence = int(current["sequence"]) + 1
                next_epoch = int(current["authority_epoch"]) + 1
            elif current["state"] == "REVOKED":
                next_sequence = int(current["sequence"]) + 1
                next_epoch = int(current["authority_epoch"]) + 1
            else:
                next_sequence = 1
                next_epoch = 1
        else:
            next_sequence = 1
            next_epoch = 1

        payload: dict[str, Any] = {
            "schema": SCHEMA,
            "sequence": next_sequence,
            "state": "ACTIVE",
            "attempt_id": normalized_attempt,
            "owner": owner,
            "activated_at_unix_ns": time.time_ns(),
            "revoked_at_unix_ns": None,
            "reason": None,
            "authority_epoch": next_epoch,
            "resource_id": _normalize_resource_id(resource_id),
            "worktree_root": worktree_root,
            "repository_head": repository_head,
            "launch_basis_path": launch_basis_path,
            "launch_basis_sha256": launch_basis_sha256,
            "boot_id": _read_boot_id(),
        }
        _atomic_json(root / STATE_FILE, payload)
        return payload


def bind_basis(
    authority_root: Path | None = None,
    attempt_id: str | None = None,
    owner_pid: int | None = None,
    owner_starttime_ticks: int | None = None,
    *,
    sequence: int | None = None,
    launch_basis_path: str | None = None,
    launch_basis_sha256: str | None = None,
    resource_id: str | None = DEFAULT_RESOURCE_ID,
) -> dict[str, Any]:
    normalized_attempt = _normalize_attempt_value(attempt_id)
    normalized_sequence = _normalize_sequence_value(sequence)
    owner = _normalize_owner(owner_pid, owner_starttime_ticks)
    _caller_owner(owner["pid"], owner["starttime_ticks"])
    root = _resolve_authority_root(authority_root, resource_id)
    resolved_basis_path, resolved_basis_sha = _resolve_bind_basis(
        launch_basis_path,
        launch_basis_sha256,
    )

    with _locked_state(root):
        current = _load(root / STATE_FILE)
        if current is None:
            raise BridgeAuthorityError("bridge authority not found")
        if current.get("state") != "ACTIVE":
            raise BridgeAuthorityError("bridge authority is not ACTIVE")
        if current.get("attempt_id") != normalized_attempt or current.get("owner") != owner:
            raise BridgeAuthorityError("bridge authority owner binding differs")
        if current.get("sequence") != normalized_sequence:
            raise BridgeAuthorityError("bridge authority sequence mismatch")

        if (
            current.get("launch_basis_path") is None
            and current.get("launch_basis_sha256") is None
        ):
            payload = dict(current)
            payload["launch_basis_path"] = resolved_basis_path
            payload["launch_basis_sha256"] = resolved_basis_sha
            _atomic_json(root / STATE_FILE, payload)
            return payload

        if (
            current.get("launch_basis_path") == resolved_basis_path
            and current.get("launch_basis_sha256") == resolved_basis_sha
        ):
            return current
        raise BridgeAuthorityError("bridge authority launch basis is already bound")


def revoke(
    authority_root: Path | None = None,
    attempt_id: str | None = None,
    owner_pid: int | None = None,
    owner_starttime_ticks: int | None = None,
    *,
    reason: str,
    resource_id: str | None = DEFAULT_RESOURCE_ID,
) -> dict[str, Any]:
    normalized_attempt = _normalize_attempt_value(attempt_id)
    owner = _normalize_owner(owner_pid, owner_starttime_ticks)
    if reason not in ALLOWED_REASONS:
        raise BridgeAuthorityError("bridge authority reason is invalid")
    _caller_owner(owner["pid"], owner["starttime_ticks"])
    root = _resolve_authority_root(authority_root, resource_id)
    with _locked_state(root):
        current = _load(root / STATE_FILE)
        if current is None:
            raise BridgeAuthorityError("bridge authority not found")
        if current.get("state") == "REVOKED":
            if (
                current.get("attempt_id") == normalized_attempt
                and current.get("owner") == owner
            ):
                return current
            raise BridgeAuthorityError("bridge authority is owned by another attempt")
        if current.get("state") != "ACTIVE":
            raise BridgeAuthorityError("bridge authority is not ACTIVE")
        if (
            current.get("attempt_id") != normalized_attempt
            or current.get("owner") != owner
        ):
            raise BridgeAuthorityError("bridge authority owner binding differs")

        payload = dict(current)
        payload["state"] = "REVOKED"
        payload["revoked_at_unix_ns"] = time.time_ns()
        payload["reason"] = reason
        payload["sequence"] = int(current["sequence"]) + 1
        payload["authority_epoch"] = int(current["authority_epoch"])
        _atomic_json(root / STATE_FILE, payload)
        return payload


def record_runtime_attempt(
    authority_root: Path | None = None,
    attempt_id: str | None = None,
    owner_pid: int | None = None,
    owner_starttime_ticks: int | None = None,
    *,
    state: str,
    exit_code: int | None = None,
    reason_code: str | None = None,
    detail: str | None = None,
    worktree_root: str | None = None,
    repository_head: str | None = None,
    launch_basis_path: str | None = None,
    launch_basis_sha256: str | None = None,
    resource_id: str | None = DEFAULT_RESOURCE_ID,
) -> dict[str, Any]:
    normalized_attempt = _normalize_attempt_value(attempt_id)
    owner = _normalize_owner(owner_pid, owner_starttime_ticks)
    root = _resolve_authority_root(authority_root, resource_id)
    worktree_root, repository_head, launch_basis_path, launch_basis_sha256 = (
        _resolve_launch_context(
            authority_root,
            worktree_root,
            repository_head,
            launch_basis_path,
            launch_basis_sha256,
        )
    )
    _caller_owner(owner["pid"], owner["starttime_ticks"])
    _ = load_current(
        root,
        attempt_id=normalized_attempt,
        owner_pid=owner["pid"],
        owner_starttime_ticks=owner["starttime_ticks"],
        worktree_root=worktree_root,
        repository_head=repository_head,
        launch_basis_path=launch_basis_path,
        launch_basis_sha256=launch_basis_sha256,
        require_basis_bound=True,
        resource_id=resource_id,
    )
    publish_payload = publish_launch_attempt(
        root,
        attempt_id=normalized_attempt,
        state=state,
        phase="runtime_gate",
        manifest_sha256=launch_basis_sha256,
        observed_at_unix_ns=time.time_ns(),
        exit_code=exit_code,
        reason_code=reason_code,
        detail=detail,
    )
    return {
        "attempt_id": normalized_attempt,
        "state": state,
        "attestation": publish_payload["attestation"],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "action",
        choices=("begin", "bind-basis", "revoke", "runtime-start", "runtime-fail"),
    )
    parser.add_argument("--authority-root", type=Path)
    parser.add_argument("--attempt-id", required=True)
    parser.add_argument("--owner-pid", type=int, required=True)
    parser.add_argument("--owner-starttime", type=int, required=True)
    parser.add_argument("--worktree-root", default=None)
    parser.add_argument("--repository-head", default=None)
    parser.add_argument("--launch-basis-path", default=None)
    parser.add_argument("--launch-basis-sha256", default=None)
    parser.add_argument("--sequence", type=int)
    parser.add_argument("--reason", choices=sorted(ALLOWED_REASONS))
    parser.add_argument("--exit-code", type=int)
    parser.add_argument("--reason-code")
    parser.add_argument("--detail")
    parser.add_argument("--resource-id", default=None)
    args = parser.parse_args(argv)

    if args.resource_id is not None and not RESOURCE_ID_PATTERN.fullmatch(args.resource_id):
        parser.error("resource-id is invalid")

    payload = None
    if args.action == "begin":
        if args.reason is not None:
            parser.error("begin does not accept --reason")
        payload = begin(
            args.authority_root,
            args.attempt_id,
            args.owner_pid,
            args.owner_starttime,
            worktree_root=args.worktree_root,
            repository_head=args.repository_head,
            launch_basis_path=args.launch_basis_path,
            launch_basis_sha256=args.launch_basis_sha256,
            resource_id=args.resource_id,
        )
    elif args.action == "bind-basis":
        if args.sequence is None:
            parser.error("bind-basis requires --sequence")
        payload = bind_basis(
            args.authority_root,
            args.attempt_id,
            args.owner_pid,
            args.owner_starttime,
            sequence=args.sequence,
            launch_basis_path=args.launch_basis_path,
            launch_basis_sha256=args.launch_basis_sha256,
            resource_id=args.resource_id,
        )
    elif args.action == "revoke":
        if args.reason is None:
            parser.error("revoke requires --reason")
        payload = revoke(
            args.authority_root,
            args.attempt_id,
            args.owner_pid,
            args.owner_starttime,
            reason=args.reason,
            resource_id=args.resource_id,
        )
    elif args.action == "runtime-start":
        if args.reason is not None:
            parser.error("runtime-start does not accept --reason")
        payload = record_runtime_attempt(
            args.authority_root,
            args.attempt_id,
            args.owner_pid,
            args.owner_starttime,
            state="STARTED",
            worktree_root=args.worktree_root,
            repository_head=args.repository_head,
            launch_basis_path=args.launch_basis_path,
            launch_basis_sha256=args.launch_basis_sha256,
            resource_id=args.resource_id,
        )
    else:
        if args.reason is not None:
            parser.error("runtime-fail does not accept --reason")
        payload = record_runtime_attempt(
            args.authority_root,
            args.attempt_id,
            args.owner_pid,
            args.owner_starttime,
            state="FAILED",
            exit_code=args.exit_code,
            reason_code=args.reason_code,
            detail=args.detail,
            worktree_root=args.worktree_root,
            repository_head=args.repository_head,
            launch_basis_path=args.launch_basis_path,
            launch_basis_sha256=args.launch_basis_sha256,
            resource_id=args.resource_id,
        )
    if args.action in {"begin", "bind-basis", "revoke"}:
        print(payload["sequence"])
    else:
        print(payload["attestation"]["sequence"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
