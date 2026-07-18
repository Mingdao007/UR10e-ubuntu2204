"""Durable v3 orchestration metadata layered over the frozen v1 stores.

This module never opens a robot, bridge, RTDE, or controller endpoint.  Trial
truth remains owned by the v1 ``CampaignStore``, ``SupervisorJournal``,
candidate plan, and ``AtomicCommandMailbox``; v3 persists only service
ownership and the operator stop latch.
"""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
import re
import secrets
import stat
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Iterator, Mapping, Sequence


SERVICE_SCHEMA = "step5d.autotune-v3.service-state/v1"
STOP_SCHEMA = "step5d.autotune-v3.stop-after-current/v1"
LEDGER_SCHEMA = "step5d.autotune-v3.attempt-ledger/v1"
ATTEMPT_LEDGER_SHA256 = (
    "19cf2241ea070e3dc8eccfbe118660104f4c3f8e40ea25cb6f0efecabc7acf99"
)
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_INSTANCE_ID = re.compile(r"^[0-9a-f]{32}$")
_ACTIVE_PHASES = frozenset({"trial_active", "wait_ack"})
_SAFE_STOP_PHASES = frozenset({"home", "ready_home", "stopped_operator"})
_SERVICE_PHASES = frozenset(
    {
        "starting",
        "ready_home",
        "trial_active",
        "stop_pending_active_trial",
        "stopped_after_current",
        "stopped",
        "failed_closed",
    }
)


class StateError(RuntimeError):
    """Durable orchestration state is missing, unsafe, or inconsistent."""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _reject_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON constant is forbidden: {value}")


def _unique_object(pairs: Sequence[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key is forbidden: {key}")
        result[key] = value
    return result


def strict_json_bytes(encoded: bytes, *, role: str) -> Any:
    try:
        return json.loads(
            encoded.decode("utf-8", errors="strict"),
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
        )
    except (UnicodeError, json.JSONDecodeError, ValueError) as exc:
        raise StateError(f"{role} is not strict JSON: {exc}") from exc


def read_strict_json(path: Path, *, role: str) -> Any:
    if path.is_symlink() or not path.is_file():
        raise StateError(f"{role} must be a real regular file: {path}")
    return strict_json_bytes(path.read_bytes(), role=role)


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def atomic_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.is_symlink() or path.parent.is_symlink():
        raise StateError(f"refusing unsafe state path: {path}")
    encoded = (
        json.dumps(payload, allow_nan=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary_path = Path(temporary)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
        _fsync_directory(path.parent)
    finally:
        temporary_path.unlink(missing_ok=True)


@dataclass(frozen=True)
class CampaignPaths:
    root: Path

    def __post_init__(self) -> None:
        root = self.root.expanduser().absolute()
        if root.exists() and (root.is_symlink() or not root.is_dir()):
            raise StateError("campaign root must be a real directory")
        object.__setattr__(self, "root", root)

    @property
    def control(self) -> Path:
        return self.root / "control"

    @property
    def candidate_plan(self) -> Path:
        return self.control / "candidate_plan.json"

    @property
    def trial_overlays(self) -> Path:
        return self.control / "v3_trial_overlays.json"

    @property
    def service_state(self) -> Path:
        return self.control / "v3_service_state.json"

    @property
    def stop_latch(self) -> Path:
        return self.control / "stop_after_current.json"

    @property
    def mailbox(self) -> Path:
        return self.control / "command_mailbox.json"

    @property
    def postprocess(self) -> Path:
        return self.root / "postprocess"


@contextlib.contextmanager
def control_lock(paths: CampaignPaths, *, owner: bool = False) -> Iterator[str]:
    paths.control.mkdir(parents=True, exist_ok=True, mode=0o700)
    if paths.control.is_symlink() or not paths.control.is_dir():
        raise StateError("campaign control root must be a real directory")
    lock_path = paths.control / (".service-owner.lock" if owner else ".metadata.lock")
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(lock_path, flags, 0o600)
    token = secrets.token_hex(16)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise StateError("campaign control lock must be a singly-linked regular file")
        mode = fcntl.LOCK_EX | (fcntl.LOCK_NB if owner else 0)
        try:
            fcntl.flock(descriptor, mode)
        except BlockingIOError as exc:
            raise StateError("another v3 service already owns this campaign") from exc
        yield token
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def _default_stop() -> dict[str, Any]:
    return {
        "schema": STOP_SCHEMA,
        "armed": False,
        "revision": 0,
        "requested_at": None,
        "cleared_at": None,
    }


def read_stop_latch(paths: CampaignPaths) -> dict[str, Any]:
    if paths.stop_latch.is_symlink():
        raise StateError("stop-after-current latch must not be a symlink")
    if not paths.stop_latch.exists():
        return _default_stop()
    payload = read_strict_json(paths.stop_latch, role="stop-after-current latch")
    expected = {"schema", "armed", "revision", "requested_at", "cleared_at"}
    if not isinstance(payload, dict) or set(payload) != expected:
        raise StateError("stop-after-current latch fields differ")
    if payload["schema"] != STOP_SCHEMA or type(payload["armed"]) is not bool:
        raise StateError("stop-after-current latch schema differs")
    if type(payload["revision"]) is not int or payload["revision"] < 0:
        raise StateError("stop-after-current latch revision is invalid")
    for key in ("requested_at", "cleared_at"):
        if payload[key] is not None and not isinstance(payload[key], str):
            raise StateError(f"stop-after-current {key} is invalid")
    return payload


def set_stop_latch(paths: CampaignPaths, *, armed: bool) -> dict[str, Any]:
    if type(armed) is not bool:
        raise StateError("stop-after-current armed value must be boolean")
    with control_lock(paths):
        current = read_stop_latch(paths)
        now = utc_now()
        payload = {
            "schema": STOP_SCHEMA,
            "armed": armed,
            "revision": current["revision"] + 1,
            "requested_at": now if armed else current["requested_at"],
            "cleared_at": None if armed else now,
        }
        atomic_json(paths.stop_latch, payload)
        return payload


def publish_service_state(
    paths: CampaignPaths,
    *,
    instance_id: str,
    phase: str,
    control_fingerprint: str,
    orchestration_fingerprint: str,
    primary_blocker: str | None,
    details: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    if _INSTANCE_ID.fullmatch(instance_id) is None or phase not in _SERVICE_PHASES:
        raise StateError("service instance or phase is invalid")
    for name, value in (
        ("control_fingerprint", control_fingerprint),
        ("orchestration_fingerprint", orchestration_fingerprint),
    ):
        if _SHA256.fullmatch(value) is None:
            raise StateError(f"{name} must be sha256")
    payload = {
        "schema": SERVICE_SCHEMA,
        "instance_id": instance_id,
        "pid": os.getpid(),
        "phase": phase,
        "observed_at": utc_now(),
        "hardware_enabled": False,
        "control_fingerprint": control_fingerprint,
        "orchestration_fingerprint": orchestration_fingerprint,
        "primary_blocker": primary_blocker,
        "details": dict(details or {}),
    }
    atomic_json(paths.service_state, payload)
    return payload


def read_service_state(paths: CampaignPaths) -> dict[str, Any]:
    if paths.service_state.is_symlink():
        raise StateError("v3 service state must not be a symlink")
    if not paths.service_state.exists():
        return {
            "schema": SERVICE_SCHEMA,
            "phase": "stopped",
            "hardware_enabled": False,
            "primary_blocker": "offline_service_not_started",
            "fresh": False,
            "pid_alive": False,
        }
    payload = read_strict_json(paths.service_state, role="v3 service state")
    required = {
        "schema",
        "instance_id",
        "pid",
        "phase",
        "observed_at",
        "hardware_enabled",
        "control_fingerprint",
        "orchestration_fingerprint",
        "primary_blocker",
        "details",
    }
    if not isinstance(payload, dict) or set(payload) != required:
        raise StateError("v3 service state fields differ")
    if payload["schema"] != SERVICE_SCHEMA or payload["hardware_enabled"] is not False:
        raise StateError("v3 service state violates the offline-only contract")
    if (
        type(payload["pid"]) is not int
        or payload["pid"] <= 0
        or not isinstance(payload["instance_id"], str)
        or _INSTANCE_ID.fullmatch(payload["instance_id"]) is None
        or payload["phase"] not in _SERVICE_PHASES
        or _SHA256.fullmatch(str(payload["control_fingerprint"])) is None
        or _SHA256.fullmatch(str(payload["orchestration_fingerprint"])) is None
        or not isinstance(payload["details"], dict)
        or (
            payload["primary_blocker"] is not None
            and not isinstance(payload["primary_blocker"], str)
        )
    ):
        raise StateError("v3 service state identity is invalid")
    try:
        observed = datetime.fromisoformat(payload["observed_at"])
        if observed.tzinfo is None or observed.utcoffset() is None:
            raise ValueError("naive timestamp")
        age_s = (datetime.now(timezone.utc) - observed).total_seconds()
    except (TypeError, ValueError) as exc:
        raise StateError("v3 service observed_at is invalid") from exc
    try:
        os.kill(payload["pid"], 0)
        pid_alive = True
    except ProcessLookupError:
        pid_alive = False
    except PermissionError:
        pid_alive = True
    fresh = pid_alive and -1.0 <= age_s <= 5.0
    payload = {**payload, "age_s": age_s, "fresh": fresh, "pid_alive": pid_alive}
    if not fresh and payload["phase"] not in {
        "stopped",
        "stopped_after_current",
        "failed_closed",
    }:
        payload["primary_blocker"] = "offline_service_state_stale"
    return payload


ORCHESTRATION_RELATIVE_PATHS = (
    # Frozen v1 physical ownership and recovery sources.  Queue *data* is
    # intentionally excluded; only the append-only plan implementation is
    # orchestration code.
    "tools/run_step5d_autotune_campaign.py",
        "tools/step5d_autotune_coordinator.py",
        "tools/step5d_autotune_journal.py",
        "tools/step5d_autotune_store.py",
        "tools/step5d_autotune_live_driver.py",
        "tools/step5d_autotune_batch_plan.py",
        "tools/step5d_autotune_v3/state.py",
        "tools/step5d_autotune_v3/service.py",
        "tools/step5d_autotune_v3/postprocess.py",
        "tools/step5d_autotune_v3/cli.py",
        "tools/step5d_autotune_v3/launcher.py",
        "tools/step5d_autotune_v3/runtime_calibration.py",
        "tools/step5d_autotune_v3/runtime_profile.py",
        "tools/run_step5d_autotune_v3_bridge.py",
        "tools/run_step5d_autotune_v3_hil_hold.py",
        "tools/run_step5d_autotune_v3_live.py",
        "tools/preflight_step5d_autotune_v3.py",
        "tools/verify_step5d_autotune_v3_hil_authorization.py",
        "tools/verify_step5d_autotune_v3_execution_readiness.py",
        "tools/promote_step5d_autotune_v3_hil.py",
        "scripts/step5d-autotune-v3.sh",
        "scripts/step5d-autotune-v3-hil-hold.sh",
        "config/systemd/step5d-autotune-v3.service",
        "config/step5/step5d_autotune_v3_launch_profile.json",
    "config/step5d/manifests/step5d_strict_rnn_autotune_v3/runtime_calibration.json",
)


def orchestration_fingerprint(experiment_root: Path) -> str:
    digest = hashlib.sha256()
    for relative in ORCHESTRATION_RELATIVE_PATHS:
        path = experiment_root / relative
        if path.is_symlink() or not path.is_file():
            raise StateError(f"orchestration fingerprint input is missing: {relative}")
        digest.update(relative.encode("utf-8") + b"\0")
        digest.update(path.read_bytes())
    return digest.hexdigest()


def orchestration_source_sha256(experiment_root: Path) -> dict[str, str]:
    """Return a checkout-stable content manifest for the orchestration surface."""

    manifest: dict[str, str] = {}
    for relative in ORCHESTRATION_RELATIVE_PATHS:
        path = experiment_root / relative
        if path.is_symlink() or not path.is_file():
            raise StateError(f"orchestration manifest input is missing: {relative}")
        manifest[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
    return manifest


@dataclass(frozen=True)
class AttemptLedger:
    tuples: Mapping[tuple[Decimal, Decimal, Decimal, str], str]
    summary: Mapping[str, int]
    sha256: str

    def attempted_group(
        self, candidate: Mapping[str, Any], execution_profile_id: str
    ) -> str | None:
        try:
            key = (
                Decimal(str(candidate["force_p_gain"])),
                Decimal(str(candidate["force_i_gain"])),
                Decimal(str(candidate["force_damping"])),
                execution_profile_id,
            )
        except (KeyError, InvalidOperation, ValueError) as exc:
            raise StateError("candidate cannot be compared with attempt ledger") from exc
        return self.tuples.get(key)


def _exact_mapping(name: str, value: Any, keys: set[str]) -> Mapping[str, Any]:
    if not isinstance(value, dict) or set(value) != keys:
        raise StateError(f"{name} fields differ")
    return value


def _positive_decimal(name: str, value: Any) -> Decimal:
    if not isinstance(value, str) or not value:
        raise StateError(f"{name} must be a decimal string")
    try:
        result = Decimal(value)
    except InvalidOperation as exc:
        raise StateError(f"{name} is not decimal") from exc
    if not result.is_finite() or result <= 0:
        raise StateError(f"{name} must be finite and positive")
    return result


def load_attempt_ledger(path: Path) -> AttemptLedger:
    if path.is_symlink() or not path.is_file():
        raise StateError("attempt ledger is missing or unsafe")
    encoded = path.read_bytes()
    digest = hashlib.sha256(encoded).hexdigest()
    if digest != ATTEMPT_LEDGER_SHA256:
        raise StateError("attempt ledger sha256 differs from the frozen migration epoch")
    payload = strict_json_bytes(encoded, role="attempt ledger")
    row = _exact_mapping(
        "attempt ledger", payload, {"schema", "source", "policy", "summary", "entries"}
    )
    if row["schema"] != LEDGER_SCHEMA:
        raise StateError("attempt ledger schema differs")
    source = _exact_mapping(
        "attempt ledger source",
        row["source"],
        {"label", "sha256", "schema", "created_at", "event_chain_count", "event_chain_head_hash"},
    )
    if source["schema"] != "step5d.autotune.db/v2" or _SHA256.fullmatch(
        str(source["sha256"])
    ) is None:
        raise StateError("attempt ledger source identity is invalid")
    policy = _exact_mapping(
        "attempt ledger policy",
        row["policy"],
        {"pending_candidates_imported", "automatic_retry", "explicit_replay_requires"},
    )
    if policy != {
        "pending_candidates_imported": False,
        "automatic_retry": "forbidden",
        "explicit_replay_requires": [
            "new_nonce",
            "reason",
            "current_owner_authorization",
        ],
    }:
        raise StateError("attempt ledger policy differs")
    if not isinstance(row["entries"], list) or not row["entries"]:
        raise StateError("attempt ledger entries are missing")

    tuples: dict[tuple[Decimal, Decimal, Decimal, str], str] = {}
    complete = uncertain = attempt_count = 0
    for index, entry_value in enumerate(row["entries"], 1):
        entry = _exact_mapping(
            "attempt ledger entry",
            entry_value,
            {"group_id", "parameters", "disposition", "automatic_retry_allowed", "attempts"},
        )
        group_id = entry["group_id"]
        if group_id != f"G{index}" or entry["automatic_retry_allowed"] is not False:
            raise StateError("attempt ledger group order or retry policy differs")
        disposition = entry["disposition"]
        if disposition not in {"complete", "uncertain_attempt"}:
            raise StateError("attempt ledger disposition is unknown")
        complete += disposition == "complete"
        uncertain += disposition == "uncertain_attempt"
        parameters = _exact_mapping(
            "attempt ledger parameters",
            entry["parameters"],
            {"force_p_gain", "force_i_gain", "force_damping", "execution_profile_id"},
        )
        profile = parameters["execution_profile_id"]
        if not isinstance(profile, str) or not profile:
            raise StateError("attempt ledger execution profile is invalid")
        key = (
            _positive_decimal("force_p_gain", parameters["force_p_gain"]),
            _positive_decimal("force_i_gain", parameters["force_i_gain"]),
            _positive_decimal("force_damping", parameters["force_damping"]),
            profile,
        )
        if key in tuples:
            raise StateError("attempt ledger repeats a parameter tuple")
        attempts = entry["attempts"]
        if not isinstance(attempts, list) or not attempts:
            raise StateError("attempt ledger entry lacks attempts")
        for attempt in attempts:
            attempt_row = _exact_mapping(
                "attempt ledger attempt",
                attempt,
                {"candidate_id", "disposition", "physical_attempted_at", "uncertain_attempt_at", "replay_nonce", "replay_reason"},
            )
            if _SHA256.fullmatch(str(attempt_row["candidate_id"])) is None:
                raise StateError("attempt ledger candidate id is invalid")
            if attempt_row["disposition"] not in {"complete", "uncertain_attempt"}:
                raise StateError("attempt ledger attempt disposition is unknown")
            if not isinstance(attempt_row["physical_attempted_at"], str):
                raise StateError("attempt ledger lacks physical attempt evidence")
        attempt_count += len(attempts)
        tuples[key] = group_id

    summary = _exact_mapping(
        "attempt ledger summary",
        row["summary"],
        {"unique_parameter_tuples", "complete_tuples", "uncertain_attempt_tuples", "attempt_records"},
    )
    expected_summary = {
        "unique_parameter_tuples": len(tuples),
        "complete_tuples": complete,
        "uncertain_attempt_tuples": uncertain,
        "attempt_records": attempt_count,
    }
    if summary != expected_summary:
        raise StateError("attempt ledger summary differs from entries")
    return AttemptLedger(tuples=tuples, summary=expected_summary, sha256=digest)


def _active_epoch_root(root: Path) -> Path:
    if (root / "store" / "campaign.json").is_file() or (root / "journal").is_dir():
        return root
    epochs = root / "epochs"
    if not epochs.is_dir() or epochs.is_symlink():
        return root
    candidates = [path for path in epochs.iterdir() if path.is_dir() and path.name.isdigit()]
    return max(candidates, key=lambda path: int(path.name)) if candidates else root


def physical_status(paths: CampaignPaths) -> dict[str, Any]:
    """Read v1 physical truth without creating an alternate trial database."""

    epoch_root = _active_epoch_root(paths.root)
    result: dict[str, Any] = {
        "epoch_root": str(epoch_root),
        "phase": None,
        "journal_revision": None,
        "history_count": 0,
        "mailbox_command": None,
        "integrity_error": None,
    }
    try:
        journal_root = epoch_root / "journal"
        if (journal_root / "latest.json").is_file():
            from step5d_autotune_journal import SupervisorJournal

            latest = SupervisorJournal(journal_root.resolve()).load_latest()
            result["phase"] = latest.state.phase
            result["journal_revision"] = latest.revision
        result["history_count"] = len(campaign_history(paths))
        from step5d_autotune_live_driver import AtomicCommandMailbox

        command = AtomicCommandMailbox(paths.mailbox, network_mode=True).read_latest()
        if command is not None:
            result["mailbox_command"] = command.handshake
    except Exception as exc:
        result["integrity_error"] = f"{type(exc).__name__}:{exc}"
    phase = result["phase"]
    result["trial_active"] = phase in _ACTIVE_PHASES
    result["safe_to_stop"] = result["integrity_error"] is None and (
        phase in _SAFE_STOP_PHASES
        or (
            phase is None
            and result["history_count"] == 0
            and result["mailbox_command"] is None
        )
    )
    return result


def campaign_history(paths: CampaignPaths) -> list[dict[str, Any]]:
    epoch_root = _active_epoch_root(paths.root)
    store_root = epoch_root / "store"
    if not (store_root / "campaign.json").is_file():
        return []
    from step5d_autotune_store import CampaignStore

    # Recovery-grade read: any quarantined/malformed outcome must surface as
    # unavailable physical truth instead of being silently omitted from status.
    return CampaignStore(store_root).read_resume_history()


def campaign_status(paths: CampaignPaths) -> dict[str, Any]:
    from step5d_autotune_batch_plan import load_plan

    plan_payload: dict[str, Any]
    if paths.candidate_plan.is_symlink():
        raise StateError("candidate plan must not be a symlink")
    if paths.candidate_plan.is_file():
        plan = load_plan(paths.candidate_plan)
        plan_payload = {
            "campaign_id": plan.campaign_id,
            "revision": plan.revision,
            "candidate_count": len(plan.candidates),
            "closed": plan.closed,
        }
    else:
        plan_payload = {
            "campaign_id": None,
            "revision": 0,
            "candidate_count": 0,
            "closed": False,
        }
    return {
        "schema": "step5d.autotune-v3.status/v1",
        "service": read_service_state(paths),
        "stop_after_current": read_stop_latch(paths),
        "queue": plan_payload,
        "physical": physical_status(paths),
    }
