#!/usr/bin/env python3
"""Primitive-first Step5d no-tube handoff contracts.

This module owns deterministic package binding, queue readiness, Home
observation, and the executable handoff state machine.  It does not open a
controller, Dashboard, bridge, or RTDE endpoint.  Live adapters are separate
consumers of these contracts so offline tests cannot accidentally authorize a
robot action.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import hashlib
import json
import math
from pathlib import Path
import time
from typing import Any, Mapping, Sequence

from step5d_autotune_v3.atomic_io import atomic_bytes


HANDOFF_MANIFEST_SCHEMA = "step5d.no-tube-handoff/manifest-v1"
HANDOFF_STATE_SCHEMA = "step5d.no-tube-handoff/state-v1"
TRANSITION_RECEIPT_SCHEMA = "step5d.no-tube-handoff/transition-receipt-v1"
QUEUE_READY_SCHEMA = "step5d.no-tube-handoff/queue-ready-v1"
HOME_VERIFIED_SCHEMA = "step5d.no-tube-handoff/home-verified-v1"

SCRIPT1_PROGRAM = "step5d_autotune_start_hover_r001"
SCRIPT2_PROGRAM = "step5d_strict_rnn_autotune_v3_r026"
STATIONARY_DWELL_S = 0.5
POSITION_ERROR_M = 0.003
ORIENTATION_ERROR_RAD = 0.05
TCP_LINEAR_SPEED_M_S = 0.001
TCP_ANGULAR_SPEED_RAD_S = 0.01
JOINT_SPEED_RAD_S = 0.01
MAX_HOME_SAMPLE_GAP_S = 0.2


class HandoffError(RuntimeError):
    """Handoff input, identity, receipt, or transition is unsafe."""


class HandoffState(str, Enum):
    RELEASE_READY = "RELEASE_READY"
    QUEUE_READY = "QUEUE_READY"
    SCRIPT1_LOADED = "SCRIPT1_LOADED"
    SCRIPT1_PLAYED = "SCRIPT1_PLAYED"
    HOME_VERIFIED = "HOME_VERIFIED"
    R026_LOADED = "R026_LOADED"
    R026_IDENTITY_VERIFIED = "R026_IDENTITY_VERIFIED"
    BRIDGE_READY = "BRIDGE_READY"
    CAMPAIGN_RUNNING = "CAMPAIGN_RUNNING"
    SAFE_HOLD = "SAFE_HOLD"
    FAILED_CLOSED = "FAILED_CLOSED"


_ALLOWED_TRANSITIONS: dict[HandoffState | None, frozenset[HandoffState]] = {
    None: frozenset({HandoffState.RELEASE_READY, HandoffState.FAILED_CLOSED}),
    HandoffState.RELEASE_READY: frozenset({HandoffState.QUEUE_READY, HandoffState.FAILED_CLOSED}),
    HandoffState.QUEUE_READY: frozenset({HandoffState.SCRIPT1_LOADED, HandoffState.FAILED_CLOSED}),
    HandoffState.SCRIPT1_LOADED: frozenset({HandoffState.SCRIPT1_PLAYED, HandoffState.FAILED_CLOSED}),
    HandoffState.SCRIPT1_PLAYED: frozenset({HandoffState.HOME_VERIFIED, HandoffState.FAILED_CLOSED}),
    HandoffState.HOME_VERIFIED: frozenset({HandoffState.R026_LOADED, HandoffState.FAILED_CLOSED}),
    HandoffState.R026_LOADED: frozenset({HandoffState.R026_IDENTITY_VERIFIED, HandoffState.FAILED_CLOSED}),
    HandoffState.R026_IDENTITY_VERIFIED: frozenset({HandoffState.BRIDGE_READY, HandoffState.FAILED_CLOSED}),
    HandoffState.BRIDGE_READY: frozenset({HandoffState.CAMPAIGN_RUNNING, HandoffState.FAILED_CLOSED}),
    HandoffState.CAMPAIGN_RUNNING: frozenset({HandoffState.SAFE_HOLD, HandoffState.FAILED_CLOSED}),
    HandoffState.SAFE_HOLD: frozenset({HandoffState.CAMPAIGN_RUNNING, HandoffState.FAILED_CLOSED}),
    HandoffState.FAILED_CLOSED: frozenset(),
}


def _canonical(value: Any) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _file_sha256(path: Path, role: str) -> str:
    if path.is_symlink() or not path.is_file():
        raise HandoffError(f"{role} must be a real regular file")
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _strict_json(path: Path, role: str) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise HandoffError(f"{role} must be a real regular file")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise HandoffError(f"{role} is not strict JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise HandoffError(f"{role} must be an object")
    return payload


def _sha256(value: Any, role: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise HandoffError(f"{role} must be a lowercase SHA-256")
    return value


def _finite_vector(value: Any, size: int, role: str) -> tuple[float, ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)) or len(value) != size:
        raise HandoffError(f"{role} must contain exactly {size} values")
    try:
        result = tuple(float(item) for item in value)
    except (TypeError, ValueError) as exc:
        raise HandoffError(f"{role} contains a non-numeric value") from exc
    if not all(math.isfinite(item) for item in result):
        raise HandoffError(f"{role} contains a non-finite value")
    return result


def _root_path(root: Path, relative: Any, role: str) -> Path:
    if not isinstance(relative, str) or not relative or Path(relative).is_absolute():
        raise HandoffError(f"{role} must be a relative path")
    candidate = (root / relative).resolve()
    if root.resolve() not in candidate.parents and candidate != root.resolve():
        raise HandoffError(f"{role} escapes experiment root")
    return candidate


@dataclass(frozen=True)
class HandoffManifest:
    path: Path
    experiment_root: Path
    payload: Mapping[str, Any]

    @property
    def campaign_id(self) -> str:
        return str(self.payload["campaign_id"])

    @property
    def high_watermark(self) -> int:
        return int(self.payload["queue"]["high_watermark"])

    @property
    def low_watermark(self) -> int:
        return int(self.payload["queue"]["low_watermark"])

    @property
    def target_pose(self) -> tuple[float, ...]:
        return _finite_vector(
            self.payload["script1"]["target_tcp_pose"], 6, "Script 1 target_tcp_pose"
        )

    @property
    def stationary(self) -> Mapping[str, Any]:
        return self.payload["stationary"]

    @property
    def remote_startup(self) -> Mapping[str, Any]:
        return self.payload["remote_startup"]

    def path_for(self, value: Any, role: str) -> Path:
        return _root_path(self.experiment_root, value, role)


def load_manifest(path: Path, *, experiment_root: Path | None = None) -> HandoffManifest:
    path = path.expanduser().resolve(strict=True)
    payload = _strict_json(path, "handoff manifest")
    required = {
        "schema",
        "campaign_id",
        "script1",
        "script2",
        "queue",
        "tube",
        "stationary",
        "remote_startup",
    }
    if set(payload) != required or payload["schema"] != HANDOFF_MANIFEST_SCHEMA:
        raise HandoffError("handoff manifest schema or fields differ")
    root = (experiment_root or path.parents[2]).expanduser().resolve(strict=True)
    if not root.is_dir():
        raise HandoffError("experiment root is not a directory")
    campaign_id = payload["campaign_id"]
    if not isinstance(campaign_id, str) or not campaign_id:
        raise HandoffError("campaign_id is invalid")

    script1 = payload["script1"]
    script2 = payload["script2"]
    queue = payload["queue"]
    tube = payload["tube"]
    stationary = payload["stationary"]
    remote_startup = payload["remote_startup"]
    if not all(
        isinstance(value, Mapping)
        for value in (script1, script2, queue, tube, stationary, remote_startup)
    ):
        raise HandoffError("handoff manifest sections must be objects")
    if script1.get("program_id") != SCRIPT1_PROGRAM:
        raise HandoffError("Script 1 program identity differs")
    if script2.get("program_id") != SCRIPT2_PROGRAM:
        raise HandoffError("Script 2 program identity differs")
    _finite_vector(script1.get("target_tcp_pose"), 6, "Script 1 target_tcp_pose")
    if script1.get("target_joint_q") is not None:
        raise HandoffError("Script 1 must not introduce a commanded target joint vector")
    for section, fields in (
        (script1, ("package_dir", "readback_receipt", "controller_target", "sha256")),
        (script2, ("package_dir", "release_manifest", "controller_target", "sha256")),
    ):
        if any(field not in section for field in fields):
            raise HandoffError("handoff package binding fields are incomplete")
        if not isinstance(section["sha256"], Mapping) or set(section["sha256"]) != {".script", ".txt", ".urp"}:
            raise HandoffError("handoff triplet SHA fields differ")
        for suffix in (".script", ".txt", ".urp"):
            _sha256(section["sha256"][suffix], f"handoff {suffix} SHA")
    for key in ("high_watermark", "low_watermark"):
        if isinstance(queue.get(key), bool) or not isinstance(queue.get(key), int):
            raise HandoffError(f"queue {key} must be an integer")
    if not 1 <= queue["low_watermark"] < queue["high_watermark"]:
        raise HandoffError("queue watermarks must satisfy 1 <= low < high")
    if tube.get("enabled") is not False or tube.get("policies") != []:
        raise HandoffError("current no-tube manifest must use an explicit empty policy set")
    remote_fields = {
        "route",
        "robot_host_env",
        "robot_host_default",
        "dashboard_port",
        "rtde_port",
        "home_frequency_hz",
        "home_timeout_s",
        "dashboard_timeout_s",
        "load_timeout_s",
        "play_timeout_s",
        "bridge_ready_timeout_s",
        "poll_interval_s",
    }
    if set(remote_startup) != remote_fields or remote_startup.get("route") != "remote_control":
        raise HandoffError("Remote startup contract must be explicit and Remote-only")
    if (
        not isinstance(remote_startup.get("robot_host_env"), str)
        or not remote_startup["robot_host_env"]
        or not remote_startup["robot_host_env"].isidentifier()
        or not isinstance(remote_startup.get("robot_host_default"), str)
        or not remote_startup["robot_host_default"]
    ):
        raise HandoffError("Remote startup host binding is invalid")
    for field in ("dashboard_port", "rtde_port"):
        value = remote_startup.get(field)
        if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 65535:
            raise HandoffError(f"Remote startup {field} is invalid")
    for field in (
        "home_frequency_hz",
        "home_timeout_s",
        "dashboard_timeout_s",
        "load_timeout_s",
        "play_timeout_s",
        "bridge_ready_timeout_s",
        "poll_interval_s",
    ):
        value = remote_startup.get(field)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)) or float(value) <= 0.0:
            raise HandoffError(f"Remote startup {field} is invalid")
    if stationary.get("dwell_s") != STATIONARY_DWELL_S:
        raise HandoffError("stationary dwell must be exactly 0.5 seconds")
    max_sample_gap_s = stationary.get("max_sample_gap_s")
    if (
        isinstance(max_sample_gap_s, bool)
        or not isinstance(max_sample_gap_s, (int, float))
        or not math.isfinite(float(max_sample_gap_s))
        or float(max_sample_gap_s) != MAX_HOME_SAMPLE_GAP_S
    ):
        raise HandoffError("stationary max_sample_gap_s must be exactly 0.2 seconds")
    expected_limits = {
        "position_error_m": POSITION_ERROR_M,
        "orientation_error_rad": ORIENTATION_ERROR_RAD,
        "tcp_linear_speed_m_s": TCP_LINEAR_SPEED_M_S,
        "tcp_angular_speed_rad_s": TCP_ANGULAR_SPEED_RAD_S,
        "joint_speed_rad_s": JOINT_SPEED_RAD_S,
    }
    if stationary.get("limits") != expected_limits:
        raise HandoffError("stationary limits differ from Script 1 contract")
    return HandoffManifest(path=path, experiment_root=root, payload=payload)


def validate_triplet(manifest: HandoffManifest, section_name: str) -> dict[str, Any]:
    section = manifest.payload[section_name]
    package_dir = manifest.path_for(section["package_dir"], f"{section_name} package_dir")
    observed_sha: dict[str, str] = {}
    for suffix in (".script", ".txt", ".urp"):
        observed_sha[suffix] = _file_sha256(
            package_dir / f"{section['program_id']}{suffix}",
            f"{section_name} {suffix}",
        )
    if observed_sha != dict(section["sha256"]):
        raise HandoffError(f"{section_name} local triplet SHA differs")

    readback_path_value = section.get("readback_receipt")
    if readback_path_value is not None:
        readback_path = manifest.path_for(readback_path_value, f"{section_name} readback receipt")
        readback = _strict_json(readback_path, f"{section_name} readback receipt")
        readback_status = readback.get("state", readback.get("status"))
        if readback_status != "controller read-back verified":
            raise HandoffError(f"{section_name} readback is not verified")
        if "verified" in readback and readback["verified"] is not True:
            raise HandoffError(f"{section_name} readback verification flag is false")
        readback_sha = readback.get("sha256") or readback.get("triplet_sha256")
        if not isinstance(readback_sha, Mapping):
            raise HandoffError(f"{section_name} readback SHA fields are missing")
        for suffix in (".script", ".txt", ".urp"):
            row = readback_sha.get(suffix)
            if isinstance(row, Mapping):
                verified = row.get("local") == observed_sha[suffix] and row.get("readback") == observed_sha[suffix]
            else:
                verified = row == observed_sha[suffix]
            if not verified:
                raise HandoffError(f"{section_name} readback SHA differs for {suffix}")
    return {
        "program_id": section["program_id"],
        "controller_target": section["controller_target"],
        "sha256": observed_sha,
        "readback_receipt": readback_path_value,
    }


def validate_release_binding(manifest: HandoffManifest) -> dict[str, Any]:
    script1 = validate_triplet(manifest, "script1")
    script2 = validate_triplet(manifest, "script2")
    release_path = manifest.path_for(manifest.payload["script2"]["release_manifest"], "r026 release manifest")
    release_sha = _file_sha256(release_path, "r026 release manifest")
    expected_release_sha = manifest.payload["script2"].get("release_manifest_sha256")
    if expected_release_sha is not None and release_sha != expected_release_sha:
        raise HandoffError("r026 release manifest SHA differs")
    release = _strict_json(release_path, "r026 release manifest")
    identity = release.get("identity")
    if not isinstance(identity, Mapping) or identity.get("program_id") != SCRIPT2_PROGRAM:
        raise HandoffError("r026 release program identity differs")
    current_path = manifest.experiment_root / "config/step5d/current.json"
    current = _strict_json(current_path, "current release pointer")
    current_manifest = _root_path(manifest.experiment_root, current.get("manifest_path"), "current release manifest")
    if _file_sha256(current_manifest, "current release manifest") != current.get("manifest_sha256"):
        raise HandoffError("current release pointer is not hash-closed")
    if current_manifest != release_path or current.get("manifest_sha256") != release_sha:
        raise HandoffError("r026 is not the current immutable release")
    return {
        "script1": script1,
        "script2": script2,
        "r026_release_manifest": str(release_path.relative_to(manifest.experiment_root)),
        "r026_release_manifest_sha256": release_sha,
        "current_pointer": str(current_path.relative_to(manifest.experiment_root)),
    }


def build_queue_ready_receipt(
    manifest: HandoffManifest,
    queue_view: Mapping[str, Any],
    *,
    feeder_receipt: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    state = queue_view.get("state")
    pending = queue_view.get("pending_requests")
    if not isinstance(state, Mapping) or not isinstance(pending, Sequence) or isinstance(pending, (str, bytes)):
        raise HandoffError("authoritative queue view is incomplete")
    pending_rows = list(pending)
    if len(pending_rows) < manifest.high_watermark:
        raise HandoffError("QUEUE_READY requires high watermark pending candidates")
    candidate_ids: list[str] = []
    request_uids: list[str] = []
    for row in pending_rows:
        if not isinstance(row, Mapping):
            raise HandoffError("queue pending row is malformed")
        candidate_id = row.get("control_candidate_uid")
        request_uid = row.get("request_uid")
        if not isinstance(candidate_id, str) or not candidate_id:
            raise HandoffError("queue pending row lacks candidate identity")
        if not isinstance(request_uid, str) or not request_uid:
            raise HandoffError("queue pending row lacks request identity")
        candidate_ids.append(candidate_id)
        request_uids.append(request_uid)
    receipt = {
        "schema": QUEUE_READY_SCHEMA,
        "status": "QUEUE_READY",
        "campaign_id": manifest.campaign_id,
        "execution_profile_id": manifest.payload["script2"].get("execution_profile_id"),
        "queue_revision": state.get("revision"),
        "pending_depth": len(pending_rows),
        "inflight": state.get("inflight"),
        "high_watermark": manifest.high_watermark,
        "low_watermark": manifest.low_watermark,
        "candidate_ids": sorted(candidate_ids),
        "request_uids": sorted(request_uids),
        "candidate_ids_sha256": _digest(sorted(candidate_ids)),
        "feeder_receipt": None if feeder_receipt is None else dict(feeder_receipt),
    }
    return receipt


def _norm(value: Sequence[float]) -> float:
    return math.sqrt(sum(float(item) * float(item) for item in value))


def build_home_verified_receipt(
    manifest: HandoffManifest,
    samples: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    if not samples:
        raise HandoffError("HOME_VERIFIED requires at least one RTDE sample")
    target = manifest.target_pose
    first_monotonic: int | None = None
    previous_monotonic: int | None = None
    previous_controller: int | None = None
    max_sample_gap_ns = int(
        round(float(manifest.stationary["max_sample_gap_s"]) * 1_000_000_000.0)
    )
    max_observed_gap_ns = 0
    max_controller_gap_ns = 0
    normalized: list[dict[str, Any]] = []
    for sample in samples:
        if not isinstance(sample, Mapping):
            raise HandoffError("Home observer sample must be an object")
        observed_monotonic = sample.get("observed_monotonic_ns")
        controller_timestamp = sample.get("controller_timestamp_ns")
        if isinstance(observed_monotonic, bool) or not isinstance(observed_monotonic, int) or observed_monotonic <= 0:
            raise HandoffError("Home observer monotonic timestamp is invalid")
        if isinstance(controller_timestamp, bool) or not isinstance(controller_timestamp, int) or controller_timestamp <= 0:
            raise HandoffError("Home observer controller timestamp is invalid")
        if previous_monotonic is not None and observed_monotonic <= previous_monotonic:
            raise HandoffError("Home observer monotonic samples are not sequential")
        if previous_controller is not None and controller_timestamp <= previous_controller:
            raise HandoffError("Home observer controller timestamps are not sequential")
        if previous_monotonic is not None and previous_controller is not None:
            observed_gap_ns = observed_monotonic - previous_monotonic
            controller_gap_ns = controller_timestamp - previous_controller
            max_observed_gap_ns = max(max_observed_gap_ns, observed_gap_ns)
            max_controller_gap_ns = max(max_controller_gap_ns, controller_gap_ns)
            if (
                observed_gap_ns > max_sample_gap_ns
                or controller_gap_ns > max_sample_gap_ns
            ):
                raise HandoffError(
                    "Home observer sample gap exceeds the continuous-dwell bound"
                )
        previous_monotonic = observed_monotonic
        previous_controller = controller_timestamp
        first_monotonic = observed_monotonic if first_monotonic is None else first_monotonic
        if sample.get("program_id") != SCRIPT1_PROGRAM:
            raise HandoffError("Home observer program identity differs")
        if sample.get("remote_control") is not True:
            raise HandoffError("HOME_VERIFIED requires Remote Control")
        if sample.get("robot_mode") != "RUNNING":
            raise HandoffError("HOME_VERIFIED requires robot mode RUNNING")
        if sample.get("fresh") is not True:
            raise HandoffError("HOME_VERIFIED requires fresh ordered observations")
        if sample.get("program_running") is not False or not str(sample.get("program_state", "")).startswith("STOPPED"):
            raise HandoffError("HOME_VERIFIED requires a stopped Script 1")
        if sample.get("safety_mode") != "NORMAL":
            raise HandoffError("HOME_VERIFIED requires Safety NORMAL")
        pose = _finite_vector(sample.get("tcp_pose"), 6, "Home observer tcp_pose")
        tcp_speed = _finite_vector(sample.get("tcp_speed"), 6, "Home observer tcp_speed")
        actual_q = _finite_vector(sample.get("actual_q"), 6, "Home observer actual_q")
        qdot = _finite_vector(sample.get("qdot"), 6, "Home observer qdot")
        position_error = _norm(tuple(pose[index] - target[index] for index in range(3)))
        orientation_error = _norm(tuple(pose[index] - target[index] for index in range(3, 6)))
        if position_error > POSITION_ERROR_M or orientation_error > ORIENTATION_ERROR_RAD:
            raise HandoffError("Home observer pose is outside the Script 1 target tolerance")
        if _norm(tcp_speed[:3]) > TCP_LINEAR_SPEED_M_S or _norm(tcp_speed[3:]) > TCP_ANGULAR_SPEED_RAD_S:
            raise HandoffError("Home observer TCP speed is not stationary")
        if max(abs(value) for value in qdot) > JOINT_SPEED_RAD_S:
            raise HandoffError("Home observer joint speed is not stationary")
        normalized.append(
            {
                "observed_monotonic_ns": observed_monotonic,
                "controller_timestamp_ns": controller_timestamp,
                "tcp_pose": list(pose),
                "tcp_speed": list(tcp_speed),
                "actual_q": list(actual_q),
                "qdot": list(qdot),
                "remote_control": True,
                "robot_mode": "RUNNING",
                "fresh": True,
            }
        )
    assert first_monotonic is not None and previous_monotonic is not None
    dwell_s = (previous_monotonic - first_monotonic) / 1_000_000_000.0
    if dwell_s < STATIONARY_DWELL_S:
        raise HandoffError("HOME_VERIFIED stationary dwell is shorter than 0.5 seconds")
    final = normalized[-1]
    return {
        "schema": HOME_VERIFIED_SCHEMA,
        "status": "HOME_VERIFIED",
        "program_id": SCRIPT1_PROGRAM,
        "target_tcp_pose": list(target),
        "observed_actual_tcp_pose": final["tcp_pose"],
        "observed_actual_q": final["actual_q"],
        "observed_tcp_speed": final["tcp_speed"],
        "observed_qdot": final["qdot"],
        "sample_count": len(normalized),
        "stationary_dwell_s": dwell_s,
        "max_sample_gap_s": float(manifest.stationary["max_sample_gap_s"]),
        "max_observed_monotonic_gap_s": max_observed_gap_ns / 1_000_000_000.0,
        "max_controller_timestamp_gap_s": max_controller_gap_ns / 1_000_000_000.0,
        "first_observed_monotonic_ns": first_monotonic,
        "last_observed_monotonic_ns": previous_monotonic,
        "limits": dict(manifest.stationary["limits"]),
        "target_joint_q": None,
        "remote_control": True,
        "robot_mode": "RUNNING",
        "fresh": True,
    }


def _write_immutable(path: Path, payload: Mapping[str, Any]) -> None:
    encoded = json.dumps(payload, indent=2, sort_keys=True, allow_nan=False).encode("utf-8") + b"\n"
    if path.exists() or path.is_symlink():
        if path.is_symlink() or not path.is_file() or path.read_bytes() != encoded:
            raise HandoffError(f"immutable handoff receipt differs: {path}")
        return
    atomic_bytes(path, encoded)


class HandoffStateStore:
    """The single canonical runtime status owner for handoff transitions."""

    def __init__(self, root: Path, *, handoff_id: str) -> None:
        self.root = root.expanduser().absolute()
        self.handoff_id = handoff_id
        if not handoff_id or any(character not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_" for character in handoff_id):
            raise HandoffError("handoff_id is invalid")

    @property
    def state_path(self) -> Path:
        return self.root / "state.json"

    def _load(self) -> dict[str, Any] | None:
        if not self.state_path.exists():
            return None
        payload = _strict_json(self.state_path, "handoff state")
        if set(payload) != {"schema", "handoff_id", "state", "revision", "last_receipt"}:
            raise HandoffError("handoff state fields differ")
        if payload["schema"] != HANDOFF_STATE_SCHEMA or payload["handoff_id"] != self.handoff_id:
            raise HandoffError("handoff state identity differs")
        try:
            state = HandoffState(payload["state"])
        except ValueError as exc:
            raise HandoffError("handoff state value is invalid") from exc
        if not isinstance(payload["revision"], int) or payload["revision"] < 0:
            raise HandoffError("handoff state revision is invalid")
        payload["state"] = state.value
        return payload

    def status(self) -> dict[str, Any]:
        payload = self._load()
        if payload is None:
            return {
                "schema": HANDOFF_STATE_SCHEMA,
                "handoff_id": self.handoff_id,
                "state": None,
                "revision": 0,
                "last_receipt": None,
            }
        return payload

    def transition(self, target: HandoffState, details: Mapping[str, Any] | None = None) -> dict[str, Any]:
        current_payload = self._load()
        current = None if current_payload is None else HandoffState(current_payload["state"])
        if target not in _ALLOWED_TRANSITIONS[current]:
            raise HandoffError(f"invalid handoff transition {current!s} -> {target.value}")
        revision = 0 if current_payload is None else int(current_payload["revision"]) + 1
        receipt = {
            "schema": TRANSITION_RECEIPT_SCHEMA,
            "handoff_id": self.handoff_id,
            "revision": revision,
            "previous_state": None if current is None else current.value,
            "state": target.value,
            "observed_at_unix_ns": time.time_ns(),
            "details": dict(details or {}),
        }
        receipt_path = self.root / "receipts" / f"{revision:04d}-{target.value.lower()}.json"
        _write_immutable(receipt_path, receipt)
        state = {
            "schema": HANDOFF_STATE_SCHEMA,
            "handoff_id": self.handoff_id,
            "state": target.value,
            "revision": revision,
            "last_receipt": str(receipt_path),
        }
        atomic_bytes(
            self.state_path,
            json.dumps(state, indent=2, sort_keys=True, allow_nan=False).encode("utf-8") + b"\n",
        )
        return receipt


def initialize_release_ready(store: HandoffStateStore, binding: Mapping[str, Any]) -> dict[str, Any]:
    return store.transition(HandoffState.RELEASE_READY, {"binding": dict(binding)})


__all__ = [
    "HANDOFF_MANIFEST_SCHEMA",
    "HANDOFF_STATE_SCHEMA",
    "HOME_VERIFIED_SCHEMA",
    "QUEUE_READY_SCHEMA",
    "HandoffError",
    "HandoffManifest",
    "HandoffState",
    "HandoffStateStore",
    "build_home_verified_receipt",
    "build_queue_ready_receipt",
    "initialize_release_ready",
    "load_manifest",
    "validate_release_binding",
    "validate_triplet",
]
