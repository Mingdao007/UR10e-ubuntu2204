#!/usr/bin/env python3
"""Validate or run the bounded Remote-Control Direct Torque v4 canary.

``status`` and ``validate`` are read-only/offline.  ``run`` is fail-closed and
requires explicit command-line gates for URScript send, RTDE input writes,
Direct Torque, physical motion, and the no-contact scope.
The live canary starts one Kunwei KWR75B 1 kHz capture owner.  Kunwei
software-baselined sensor-to-TCP SI wrench is the only experiment F/T source
and the only wrench used by the active no-contact guard.
"""

from __future__ import annotations

import argparse
import csv
from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import json
import math
import os
from pathlib import Path
import socket
import subprocess
import sys
import threading
import time
from typing import Any, Mapping, Sequence


ROOT = Path(__file__).resolve().parents[1]
VIC_ROOT = ROOT.parent / "ur10e-variable-impedance"
UR_HELPERS = Path(
    "/home/andy/codex-private-skills-shared-main/skills/ur10e-realsetup/scripts"
)
KUNWEI_TOOLS = Path(
    ROOT.parent / "sensor-integration/kunwei-kwr75b/tools"
)
for import_root in (VIC_ROOT, UR_HELPERS, KUNWEI_TOOLS, ROOT / "tools"):
    if str(import_root) not in sys.path:
        sys.path.insert(0, str(import_root))

from _ur_common import dashboard_exchange  # noqa: E402
from capture_kunwei_kwr75_1khz import (  # noqa: E402
    CaptureState,
    FORCE_KG_TO_N,
    MOMENT_KG_M_TO_NM,
    RunningStats,
    START_STREAM,
    STOP_STREAM,
    parse_frame,
    pop_frames,
)
from run_step5d_tacdiffusion_bridge import (  # noqa: E402
    apply_wrench_transform,
    validate_calibration,
)
from step5d_v34_transport_primitives import (  # noqa: E402
    RTDEBridgeClient,
    advance_periodic_deadline,
    require_v35_sched_other,
)
from ur10e_parallel import ResourceProfile, writer_lease  # noqa: E402
from ur10e_vic.tacdiffusion.direct_torque_live_v4 import (  # noqa: E402
    COMPILE_PROBE_PROTOCOL_TOKEN,
    FRICTION_PROFILE_ZERO_ISOLATION,
    LIVE_PROTOCOL_TOKEN,
    WRENCH_FRAME_TOKEN,
    NO_CONTACT_RELEASE_TOLERANCE_M,
    LiveTubeContract,
    build_compile_probe_source,
    parse_compile_probe_source,
    parse_live_receiver_source,
)
from ur10e_vic.tacdiffusion.unknown_surface_episode import (  # noqa: E402
    rotation_vector_distance_rad,
)
from ur10e_vic.tacdiffusion.eligibility import (  # noqa: E402
    EligibilityDecision,
    EligibilityValidator,
)
from ur10e_vic.tacdiffusion.episode_recorder import (  # noqa: E402
    EpisodeFrameV2,
    EpisodeRecorder,
    RecorderError,
    read_episode_artifact,
    validate_sealed_episode_manifest,
)


COMPILE_PROBE_EVIDENCE_SCHEMA = (
    "ur10e_tacdiffusion_compile_probe_evidence/v2"
)
RECEIVER_HANDSHAKE_EVIDENCE_SCHEMA = (
    "ur10e_tacdiffusion_receiver_handshake_evidence/v1"
)
BUNDLE_SCHEMA = "ur10e_tacdiffusion_direct_torque_live_bundle/v1"
REFERENCE_SCHEMA = "ur10e_tacdiffusion_unknown_surface_episode_artifact/v1"
CANARY_EVIDENCE_SCHEMA = (
    "ur10e_tacdiffusion_direct_torque_canary_evidence/v2"
)
MODE_IDLE = 0
MODE_RUN = 1
MODE_END = 2
MODE_ABORT = 3
STATE_WAITING = 0
STATE_STARTUP = 1
STATE_TORQUE = 2
STATE_SAFE_EXIT = 3
STATE_FAULT = 4
STATE_COMPLETE = 5
COMPILE_PROBE_STATE_ACTIVE = 77
COMPILE_PROBE_STATE_COMPLETE = 78
RUNTIME_STOPPED = 1
RUNTIME_PLAYING = 2
ROBOT_MODE_RUNNING = 7
SAFETY_MODE_NORMAL = 1
FIXED_STIFFNESS = (600.0, 600.0, 600.0, 30.0, 30.0, 30.0)
ZERO6 = (0.0,) * 6
KUNWEI_CALIBRATION_DEFAULT = (
    ROOT / "config/step5d_tacdiffusion_sensor_frame_v1.json"
)
KUNWEI_BASELINE_SAMPLES = 1000
KUNWEI_PREFLIGHT_FORCE_N = 2.0
KUNWEI_PREFLIGHT_TORQUE_NM = 0.2
KUNWEI_ACTIVE_FORCE_N = 6.0
KUNWEI_ACTIVE_TORQUE_NM = 0.5
DEFAULT_KUNWEI_DELIVERY_WATCHDOG_S = 0.080
COMPILE_PROBE_TRANSLATION_TOLERANCE_M = 0.0002
COMPILE_PROBE_TCP_SPEED_TOLERANCE_M_S = 0.001
COMPILE_PROBE_TCP_ANGULAR_SPEED_TOLERANCE_RAD_S = 0.001
COMPILE_PROBE_JOINT_SPEED_TOLERANCE_RAD_S = 0.001
STATIONARITY_WINDOW_S = 0.1
STATIONARITY_WINDOW_MIN_SAMPLES = 40
STATIONARITY_TCP_TRANSLATION_RADIUS_TOLERANCE_M = 0.0003
STATIONARITY_TCP_ROTATION_RADIUS_TOLERANCE_RAD = 0.0006
STATIONARITY_JOINT_POSITION_EXCURSION_TOLERANCE_RAD = 0.0003
KUNWEI_RAW_FIELDS = (
    "fx_kg_manual",
    "fy_kg_manual",
    "fz_kg_manual",
    "mx_kg_m_manual",
    "my_kg_m_manual",
    "mz_kg_m_manual",
)
LIVE_WRITER_TASK = "tacdiffusion-remote-direct-torque-v4"
CANARY_STAGE_HOLD = "hold_100ms"
CANARY_STAGE_RAMP = "ramp_0_2mm_500ms"
CANARY_STAGE_REFERENCE = "reference_2s"
CANARY_STAGE_REFERENCE_3S_SOUND_DIAGNOSTIC = "reference_3s_sound_diagnostic"
CANARY_STAGE_REFERENCE_7S_SOUND_DIAGNOSTIC = "reference_7s_sound_diagnostic"
CANARY_STAGE_REFERENCE_10S_DIAGNOSTIC = "reference_10s_diagnostic"
CANARY_STAGE_ORDER = (
    CANARY_STAGE_HOLD,
    CANARY_STAGE_RAMP,
    CANARY_STAGE_REFERENCE,
    CANARY_STAGE_REFERENCE_3S_SOUND_DIAGNOSTIC,
    CANARY_STAGE_REFERENCE_7S_SOUND_DIAGNOSTIC,
    CANARY_STAGE_REFERENCE_10S_DIAGNOSTIC,
)
CANARY_STAGE_DURATIONS_S = {
    CANARY_STAGE_HOLD: 0.1,
    CANARY_STAGE_RAMP: 0.5,
    CANARY_STAGE_REFERENCE: 2.0,
    CANARY_STAGE_REFERENCE_3S_SOUND_DIAGNOSTIC: 3.0,
    CANARY_STAGE_REFERENCE_7S_SOUND_DIAGNOSTIC: 7.0,
    CANARY_STAGE_REFERENCE_10S_DIAGNOSTIC: 10.0,
}
CANARY_STAGE_REQUIRED_PRIOR = {
    CANARY_STAGE_HOLD: None,
    CANARY_STAGE_RAMP: CANARY_STAGE_HOLD,
    CANARY_STAGE_REFERENCE: CANARY_STAGE_RAMP,
    CANARY_STAGE_REFERENCE_3S_SOUND_DIAGNOSTIC: CANARY_STAGE_RAMP,
    CANARY_STAGE_REFERENCE_7S_SOUND_DIAGNOSTIC: CANARY_STAGE_RAMP,
    CANARY_STAGE_REFERENCE_10S_DIAGNOSTIC: CANARY_STAGE_REFERENCE,
}
CANARY_REFERENCE_STAGES = (
    CANARY_STAGE_REFERENCE,
    CANARY_STAGE_REFERENCE_3S_SOUND_DIAGNOSTIC,
    CANARY_STAGE_REFERENCE_7S_SOUND_DIAGNOSTIC,
    CANARY_STAGE_REFERENCE_10S_DIAGNOSTIC,
)
ENTRY_ANALYSIS_WINDOW_S = 0.020
MIN_CONTROL_UPDATE_RATE_HZ = 150.0
MIN_TORQUE_CALL_RATE_HZ = 450.0
MAX_TORQUE_CALL_RATE_HZ = 550.0
MAX_CONTROL_UPDATE_GAP_S = 0.010


@dataclass
class TaskExecutor:
    """Task-cadence owner for recorder faults and the existing safe-exit path."""

    recorder: EpisodeRecorder | None = None
    poll_interval_s: float = 0.020
    _next_poll_s: float = -math.inf

    def __post_init__(self) -> None:
        if not math.isfinite(self.poll_interval_s) or self.poll_interval_s <= 0.0:
            raise ValueError("task poll interval must be finite and positive")

    def poll(self) -> None:
        if self.recorder is None:
            return
        now = time.monotonic()
        if now < self._next_poll_s:
            return
        self._next_poll_s = now + self.poll_interval_s
        fault = self.recorder.poll_fault()
        if fault is not None:
            raise RecorderError(fault)


@dataclass
class RecorderObservationHistory:
    """Causally compose the canonical current+previous 84D observation."""

    previous_slice_42d: tuple[float, ...] | None = None

    def compose(
        self,
        current_slice_42d: Sequence[float],
    ) -> tuple[tuple[float, ...], bool]:
        current = tuple(float(value) for value in current_slice_42d)
        if len(current) != 42 or not all(math.isfinite(value) for value in current):
            raise ValueError("recorder observation slice must contain 42 finite values")
        history_valid = self.previous_slice_42d is not None
        previous = self.previous_slice_42d if history_valid else (0.0,) * 42
        self.previous_slice_42d = current
        return current + previous, history_valid

_LIVE_WRITER_PATTERNS = (
    "run_tacdiffusion_remote_direct_torque_v4.py",
    "run_step5d_tacdiffusion_bridge.py",
    "run_tacdiffusion_bridge.py",
    "run_step5d_tacdiffusion_simulation_probe.py",
    "kunwei_rtde_bridge.py",
    "step5d_tacdiffusion_bridge",
    "ros2 run",
    "roslaunch",
)
_LIVE_WRITER_IGNORED_PATTERNS = ("codex", "pgrep", "pytest")
_PGREP_LIVE_WRITER_PATTERN = (
    "run_tacdiffusion_remote_direct_torque_v4.py|"
    "run_step5d_tacdiffusion_bridge.py|"
    "run_tacdiffusion_bridge.py|"
    "run_step5d_tacdiffusion_simulation_probe.py|"
    "kunwei_rtde_bridge.py|"
    "step5d_tacdiffusion_bridge|"
    "ros2 run|"
    "roslaunch"
)

DOUBLE_INPUT_FIELDS = [f"input_double_register_{index}" for index in range(24, 48)]
INTEGER_INPUT_FIELDS = [f"input_int_register_{index}" for index in range(24, 36)]
INPUT_FIELDS = DOUBLE_INPUT_FIELDS + INTEGER_INPUT_FIELDS
OUTPUT_FIELDS = [
    "timestamp",
    "actual_TCP_pose",
    "actual_TCP_speed",
    "actual_q",
    "actual_qd",
    "target_moment",
    "target_current",
    "actual_current",
    "actual_current_as_torque",
    "joint_control_output",
    "joint_mode",
    "runtime_state",
    "robot_mode",
    "safety_mode",
    *[f"output_double_register_{index}" for index in range(24, 48)],
    *[f"output_int_register_{index}" for index in range(24, 36)],
]


def _maximum_derived_abs_joint_acceleration(
    rows: Sequence[Mapping[str, Any]],
) -> float:
    maximum = 0.0
    for left, right in zip(rows, rows[1:]):
        dt_s = float(right["controller_timestamp_s"]) - float(
            left["controller_timestamp_s"]
        )
        if dt_s <= 0.0:
            continue
        maximum = max(
            maximum,
            *(
                abs(
                    float(right[f"actual_qd_{axis}"])
                    - float(left[f"actual_qd_{axis}"])
                )
                / dt_s
                for axis in range(6)
            ),
        )
    return maximum


def _action_echo_coherent(row: Mapping[str, Any]) -> bool:
    if "action_echo_coherent" in row:
        value = row["action_echo_coherent"]
        if isinstance(value, str):
            return value.strip().lower() in {"1", "true", "yes"}
        return bool(value)
    begin = row.get("action_publish_generation_begin")
    end = row.get("action_publish_generation_end")
    if begin in (None, "") or end in (None, ""):
        return False
    begin_value = int(float(begin))
    end_value = int(float(end))
    return begin_value > 0 and begin_value == end_value


def _counter_rate_hz(
    rows: Sequence[Mapping[str, Any]],
    field: str,
) -> float:
    active = [
        row
        for row in rows
        if int(float(row["receiver_state"])) in (STATE_STARTUP, STATE_TORQUE)
        and _action_echo_coherent(row)
        and row.get(field) not in (None, "")
    ]
    if len(active) < 2:
        return 0.0
    elapsed_s = float(active[-1]["controller_timestamp_s"]) - float(
        active[0]["controller_timestamp_s"]
    )
    if elapsed_s <= 0.0:
        return 0.0
    delta = float(active[-1][field]) - float(active[0][field])
    if delta < 0.0:
        return 0.0
    return delta / elapsed_s


def _maximum_active_control_update_gap_s(
    rows: Sequence[Mapping[str, Any]],
) -> float:
    """Ignore stale cadence registers before this receiver becomes active."""

    return max(
        (
            float(row["maximum_control_update_gap_s"])
            for row in rows
            if int(float(row["receiver_state"])) in (STATE_STARTUP, STATE_TORQUE)
            and _action_echo_coherent(row)
            and row.get("maximum_control_update_gap_s") not in (None, "")
        ),
        default=0.0,
    )


def analyze_entry_bumplessness(
    rows: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Classify the first 20 ms of a recorded Direct Torque transition."""

    if not rows:
        raise ValueError("entry_analysis_rows_empty")
    raw_active_indices = [
        index
        for index, row in enumerate(rows)
        if int(float(row["receiver_state"])) in (STATE_STARTUP, STATE_TORQUE)
        and int(float(row["ack_sequence"])) > 0
    ]
    if not raw_active_indices:
        raise ValueError("entry_analysis_direct_torque_state_missing")
    active_indices = [
        index
        for index in raw_active_indices
        if _action_echo_coherent(rows[index])
    ]
    if not active_indices:
        raise ValueError("entry_analysis_coherent_action_echo_missing")
    first_raw_timestamp = float(
        rows[raw_active_indices[0]]["controller_timestamp_s"]
    )
    entry_raw_indices = [
        index
        for index in raw_active_indices
        if float(rows[index]["controller_timestamp_s"]) - first_raw_timestamp
        <= ENTRY_ANALYSIS_WINDOW_S + 1e-12
    ]
    rejected_incoherent_rows = sum(
        not _action_echo_coherent(rows[index]) for index in entry_raw_indices
    )
    physical_window = [rows[index] for index in entry_raw_indices]
    if len(physical_window) < 2:
        raise ValueError("entry_analysis_physical_window_too_short")
    first_index = active_indices[0]
    first = rows[first_index]
    first_timestamp = float(first["controller_timestamp_s"])
    window = [
        row
        for row in rows[first_index:]
        if float(row["controller_timestamp_s"]) - first_timestamp
        <= ENTRY_ANALYSIS_WINDOW_S + 1e-12
        and _action_echo_coherent(row)
    ]
    if len(window) < 2:
        raise ValueError("entry_analysis_window_too_short")

    first_tau = tuple(
        float(first[f"commanded_joint_torque_nm_{axis}"]) for axis in range(6)
    )
    first_tau_max_abs = max(abs(value) for value in first_tau)
    first_pose_error_translation_m = math.dist(
        [
            float(first[f"command_desired_pose_{axis}"])
            for axis in range(3)
        ],
        [float(first[f"actual_TCP_pose_{axis}"]) for axis in range(3)],
    )
    first_pose_error_rotation_raw_rad = math.dist(
        [
            float(first[f"command_desired_pose_{axis}"])
            for axis in range(3, 6)
        ],
        [float(first[f"actual_TCP_pose_{axis}"]) for axis in range(3, 6)],
    )

    maximum_abs_joint_speed_rad_s = max(
        abs(float(row[f"actual_qd_{axis}"]))
        for row in physical_window
        for axis in range(6)
    )
    maximum_tcp_translation_speed_m_s = max(
        math.sqrt(
            sum(
                float(row[f"actual_TCP_speed_{axis}"]) ** 2
                for axis in range(3)
            )
        )
        for row in physical_window
    )
    maximum_tcp_rotation_speed_rad_s = max(
        math.sqrt(
            sum(
                float(row[f"actual_TCP_speed_{axis}"]) ** 2
                for axis in range(3, 6)
            )
        )
        for row in physical_window
    )
    maximum_derived_abs_joint_acceleration_rad_s2 = (
        _maximum_derived_abs_joint_acceleration(physical_window)
    )

    maximum_abs_joint_excursion_rad = max(
        max(float(row[f"actual_q_{axis}"]) for row in physical_window)
        - min(float(row[f"actual_q_{axis}"]) for row in physical_window)
        for axis in range(6)
    )
    first_physical_pose = [
        float(physical_window[0][f"actual_TCP_pose_{axis}"])
        for axis in range(3)
    ]
    maximum_tcp_translation_excursion_m = max(
        math.dist(
            first_physical_pose,
            [
                float(row[f"actual_TCP_pose_{axis}"])
                for axis in range(3)
            ],
        )
        for row in physical_window
    )
    maximum_commanded_tau_nm = max(
        abs(float(row[f"commanded_joint_torque_nm_{axis}"]))
        for row in window
        for axis in range(6)
    )
    transition_failures: list[str] = []
    diagnostic_events: list[str] = []
    if maximum_derived_abs_joint_acceleration_rad_s2 > 1.0:
        diagnostic_events.append("joint_acceleration_gt_1rad_s2")
    if maximum_abs_joint_excursion_rad > 0.0005:
        transition_failures.append("joint_excursion_gt_0_5mrad")
    if maximum_tcp_translation_excursion_m > 0.0003:
        transition_failures.append("tcp_translation_excursion_gt_0_3mm")
    if maximum_tcp_translation_speed_m_s > 0.002:
        diagnostic_events.append("tcp_translation_speed_gt_2mm_s")
    if maximum_commanded_tau_nm > 0.05:
        diagnostic_events.append("commanded_tau_gt_0_05nm")
    if rejected_incoherent_rows:
        outcome = "INDETERMINATE"
    elif transition_failures:
        outcome = "FAIL"
    else:
        outcome = "PASS"
    return {
        "schema": "ur10e_direct_torque_entry_transition/v3",
        "claim_class": "offline_replay_of_recorded_live_no_contact_entry",
        "first_coherent_direct_torque_row_index": first_index,
        "analysis_window_s": ENTRY_ANALYSIS_WINDOW_S,
        "analysis_window_physical_rows": len(physical_window),
        "analysis_window_coherent_rows": len(window),
        "rejected_incoherent_rows": rejected_incoherent_rows,
        "first_commanded_joint_torque_nm": list(first_tau),
        "first_commanded_joint_torque_max_abs_nm": first_tau_max_abs,
        "first_pose_error_translation_m": first_pose_error_translation_m,
        "first_pose_error_rotation_raw_rad": first_pose_error_rotation_raw_rad,
        "maximum_abs_joint_speed_rad_s": maximum_abs_joint_speed_rad_s,
        "maximum_tcp_translation_speed_m_s": (
            maximum_tcp_translation_speed_m_s
        ),
        "maximum_tcp_rotation_speed_rad_s": maximum_tcp_rotation_speed_rad_s,
        "maximum_derived_abs_joint_acceleration_rad_s2": (
            maximum_derived_abs_joint_acceleration_rad_s2
        ),
        "maximum_abs_joint_excursion_rad": maximum_abs_joint_excursion_rad,
        "maximum_tcp_translation_excursion_m": (
            maximum_tcp_translation_excursion_m
        ),
        "maximum_commanded_tau_nm": maximum_commanded_tau_nm,
        "transition_failures": transition_failures,
        "diagnostic_events": diagnostic_events,
        "outcome": outcome,
        "causal_limit": (
            "PASS/FAIL applies only when every sampled row in the first 20 ms "
            "has a coherent action publication; otherwise the outcome is "
            "INDETERMINATE"
        ),
        "motion_performed": False,
        "controller_io_performed": False,
    }


def analyze_entry_csv(
    csv_path: Path,
    output_path: Path | None = None,
) -> dict[str, Any]:
    with csv_path.open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    result = analyze_entry_bumplessness(rows)
    result["source_csv"] = str(csv_path.resolve())
    result["source_csv_sha256"] = _sha256(csv_path)
    if output_path is not None:
        _write_json_new(output_path.resolve(), result)
    return result


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def runtime_source_binding() -> dict[str, Any]:
    """Identify every host-side source that can affect a live canary."""

    paths = {
        "remote_direct_torque_runner": Path(__file__).resolve(),
        "v34_transport_primitives": (
            ROOT / "tools/step5d_v34_transport_primitives.py"
        ).resolve(),
        "kunwei_native_capture_primitives": (
            KUNWEI_TOOLS / "capture_kunwei_kwr75_1khz.py"
        ).resolve(),
        "direct_torque_contract": (
            VIC_ROOT / "ur10e_vic/tacdiffusion/direct_torque_live_v4.py"
        ).resolve(),
        "calibration_validator": (
            ROOT / "tools/run_step5d_tacdiffusion_bridge.py"
        ).resolve(),
        "live_writer_lease": (ROOT / "tools/ur10e_parallel.py").resolve(),
        "controller_status_helpers": (UR_HELPERS / "_ur_common.py").resolve(),
    }
    missing = [name for name, path in paths.items() if not path.is_file()]
    if missing:
        raise RuntimeError(
            "runtime_source_binding_missing:" + ",".join(sorted(missing))
        )
    files = {
        name: {"path": str(path), "sha256": _sha256(path)}
        for name, path in sorted(paths.items())
    }
    digest_payload = {
        name: entry["sha256"] for name, entry in files.items()
    }
    composite = hashlib.sha256(
        json.dumps(
            digest_payload,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    return {
        "schema": "ur10e_tacdiffusion_direct_torque_runtime_binding/v1",
        "sha256": composite,
        "files": files,
    }


def _finite6(values: Sequence[float], name: str) -> tuple[float, ...]:
    result = tuple(float(value) for value in values)
    if len(result) != 6 or not all(math.isfinite(value) for value in result):
        raise ValueError(f"{name} must contain six finite values")
    return result


def _new_live_identity_pair() -> tuple[int, int]:
    """Mint positive int31 identities locally for one controller episode."""

    identities: list[int] = []
    while len(identities) < 2:
        candidate = int.from_bytes(os.urandom(4), "big") & 0x7FFFFFFF
        if candidate > 0 and candidate not in identities:
            identities.append(candidate)
    return identities[0], identities[1]


def _sample_translation_error_sqm3(a_pose: Sequence[float], b_pose: Sequence[float]) -> float:
    return math.sqrt(
        sum((float(a_pose[index]) - float(b_pose[index])) ** 2 for index in range(3))
    )


def _is_stationary(rtde: Mapping[str, Any]) -> bool:
    """Use the same no-motion limits before and after the compile probe."""

    return (
        max(abs(float(value)) for value in rtde["actual_TCP_speed"][:3])
        <= COMPILE_PROBE_TCP_SPEED_TOLERANCE_M_S
        and max(abs(float(value)) for value in rtde["actual_TCP_speed"][3:])
        <= COMPILE_PROBE_TCP_ANGULAR_SPEED_TOLERANCE_RAD_S
        and max(abs(float(value)) for value in rtde["actual_qd"])
        <= COMPILE_PROBE_JOINT_SPEED_TOLERANCE_RAD_S
    )


def _stationarity_window_evidence(
    samples: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Separate bounded pose drift from high-frequency velocity estimation."""

    def vector(sample: Mapping[str, Any], name: str) -> tuple[float, ...]:
        raw = sample.get(name)
        if isinstance(raw, (list, tuple)):
            return tuple(float(value) for value in raw)
        return tuple(float(sample[f"{name}_{axis}"]) for axis in range(6))

    if not samples:
        return {
            "ok": False,
            "sample_count": 0,
            "reason": "no_samples",
        }
    poses = [vector(sample, "actual_TCP_pose") for sample in samples]
    speeds = [vector(sample, "actual_TCP_speed") for sample in samples]
    joints = [vector(sample, "actual_q") for sample in samples]
    joint_speeds = [vector(sample, "actual_qd") for sample in samples]
    translation_center = tuple(
        sum(pose[axis] for pose in poses) / len(poses) for axis in range(3)
    )
    maximum_translation_radius_m = max(
        math.sqrt(
            sum(
                (pose[axis] - translation_center[axis]) ** 2
                for axis in range(3)
            )
        )
        for pose in poses
    )
    orientation_reference = poses[0][3:]
    maximum_rotation_radius_rad = max(
        rotation_vector_distance_rad(orientation_reference, pose[3:])
        for pose in poses
    )
    maximum_joint_position_excursion_rad = max(
        max(joint[axis] for joint in joints)
        - min(joint[axis] for joint in joints)
        for axis in range(6)
    )
    mean_tcp_speed = tuple(
        sum(speed[axis] for speed in speeds) / len(speeds)
        for axis in range(6)
    )
    mean_joint_speed = tuple(
        sum(speed[axis] for speed in joint_speeds) / len(joint_speeds)
        for axis in range(6)
    )
    mean_tcp_translation_speed_m_s = math.sqrt(
        sum(value * value for value in mean_tcp_speed[:3])
    )
    mean_tcp_rotation_speed_rad_s = math.sqrt(
        sum(value * value for value in mean_tcp_speed[3:])
    )
    maximum_abs_mean_joint_speed_rad_s = max(
        abs(value) for value in mean_joint_speed
    )
    checks = {
        "minimum_40_samples": len(samples) >= STATIONARITY_WINDOW_MIN_SAMPLES,
        "tcp_translation_radius_le_0_3mm": (
            maximum_translation_radius_m
            <= STATIONARITY_TCP_TRANSLATION_RADIUS_TOLERANCE_M
        ),
        "tcp_rotation_radius_le_0_6mrad": (
            maximum_rotation_radius_rad
            <= STATIONARITY_TCP_ROTATION_RADIUS_TOLERANCE_RAD
        ),
        "joint_position_excursion_le_0_3mrad": (
            maximum_joint_position_excursion_rad
            <= STATIONARITY_JOINT_POSITION_EXCURSION_TOLERANCE_RAD
        ),
        "mean_tcp_translation_speed_le_1mm_s": (
            mean_tcp_translation_speed_m_s
            <= COMPILE_PROBE_TCP_SPEED_TOLERANCE_M_S
        ),
        "mean_tcp_rotation_speed_le_1mrad_s": (
            mean_tcp_rotation_speed_rad_s
            <= COMPILE_PROBE_TCP_ANGULAR_SPEED_TOLERANCE_RAD_S
        ),
        "maximum_abs_mean_joint_speed_le_1mrad_s": (
            maximum_abs_mean_joint_speed_rad_s
            <= COMPILE_PROBE_JOINT_SPEED_TOLERANCE_RAD_S
        ),
    }
    return {
        "ok": all(checks.values()),
        "sample_count": len(samples),
        "window_s": STATIONARITY_WINDOW_S,
        "maximum_translation_radius_m": maximum_translation_radius_m,
        "maximum_rotation_radius_rad": maximum_rotation_radius_rad,
        "maximum_joint_position_excursion_rad": (
            maximum_joint_position_excursion_rad
        ),
        "mean_tcp_translation_speed_m_s": mean_tcp_translation_speed_m_s,
        "mean_tcp_rotation_speed_rad_s": mean_tcp_rotation_speed_rad_s,
        "maximum_abs_mean_joint_speed_rad_s": (
            maximum_abs_mean_joint_speed_rad_s
        ),
        "checks": checks,
    }


def _read_rtde_status_window(
    robot_host: str,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    fields = [
        "timestamp",
        "actual_TCP_pose",
        "actual_TCP_speed",
        "actual_q",
        "actual_qd",
        "runtime_state",
        "robot_mode",
        "safety_mode",
    ]
    samples: list[dict[str, Any]] = []
    with LiveRTDE(robot_host, timeout=3.0) as rtde:
        rtde.negotiate()
        recipe, types = rtde.setup_outputs(500.0, fields)
        rtde.start()
        deadline = time.monotonic() + 1.0
        while (
            len(samples) < round(STATIONARITY_WINDOW_S * 500)
            and time.monotonic() < deadline
        ):
            samples.extend(
                _receive_available(rtde, recipe, types, fields, 0.01)
            )
    if len(samples) < STATIONARITY_WINDOW_MIN_SAMPLES:
        raise RuntimeError(
            f"stationarity_window_too_short:{len(samples)}"
        )
    samples = samples[: round(STATIONARITY_WINDOW_S * 500)]
    return dict(samples[-1]), samples


def _next_available_run_dir(output_dir: Path) -> Path:
    output_dir = output_dir.resolve()
    if output_dir.exists() and not output_dir.is_dir():
        raise RuntimeError(f"output_dir_is_file:{output_dir}")
    if not output_dir.exists():
        return output_dir
    suffix = 1
    while True:
        candidate = output_dir.with_name(f"{output_dir.name}_{suffix}")
        if not candidate.exists():
            return candidate
        suffix += 1


def _detect_live_writer_processes() -> list[str]:
    try:
        probe = subprocess.run(
            ["pgrep", "-af", _PGREP_LIVE_WRITER_PATTERN],
            capture_output=True,
            text=True,
            check=False,
            timeout=2.0,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError(f"active_writer_probe_failed:{type(exc).__name__}") from exc
    if probe.returncode not in {0, 1}:
        raise RuntimeError("active_writer_probe_failed:pgrep_error")
    own_pid = os.getpid()
    matches: list[str] = []
    for line in probe.stdout.splitlines():
        if not line.strip():
            continue
        fields = line.split(maxsplit=1)
        if len(fields) < 2:
            continue
        try:
            pid = int(fields[0])
        except ValueError:
            continue
        if pid == own_pid:
            continue
        command = fields[1].lower()
        if any(pattern in command for pattern in _LIVE_WRITER_IGNORED_PATTERNS):
            continue
        if any(pattern in command for pattern in _LIVE_WRITER_PATTERNS):
            matches.append(f"{pid}:{fields[1]}")
    matches.sort()
    return matches


def _enforce_no_live_writer_conflict() -> None:
    matches = _detect_live_writer_processes()
    if matches:
        raise RuntimeError("active_live_writer_detected:" + ",".join(matches))


LiveRTDE = RTDEBridgeClient


@dataclass(frozen=True)
class KunweiSnapshot:
    sample_index: int
    receive_batch_id: int
    t_monotonic_s: float
    nominal_sensor_time_s: float
    raw_si: tuple[float, ...]
    wrench_tcp_si: tuple[float, ...]
    normal_load_n: float
    force_norm_n: float
    torque_norm_nm: float


class KunweiGuardCapture:
    """Single-owner raw capture plus software-baselined hard-guard stream."""

    def __init__(
        self,
        *,
        sensor_ip: str,
        sensor_port: int,
        connect_timeout_s: float,
        output_dir: Path,
        calibration: Mapping[str, Any],
        delivery_watchdog_s: float,
    ) -> None:
        self.sensor_ip = sensor_ip
        self.sensor_port = int(sensor_port)
        self.connect_timeout_s = float(connect_timeout_s)
        self.delivery_watchdog_s = float(delivery_watchdog_s)
        if (
            not math.isfinite(self.delivery_watchdog_s)
            or not 0.0 < self.delivery_watchdog_s <= 0.5
        ):
            raise ValueError("kunwei_delivery_watchdog_s_invalid")
        self.output_dir = output_dir
        self.matrix = calibration["wrench_transform_sensor_to_tcp_6x6"]
        self.normal_axis = {"fx": 0, "fy": 1, "fz": 2}[
            str(calibration["normal_force_axis"])
        ]
        self.normal_sign = float(calibration["normal_force_sign"])
        self.socket: socket.socket | None = None
        self.thread: threading.Thread | None = None
        self.stop_event = threading.Event()
        self.lock = threading.Lock()
        self.ready_event = threading.Event()
        self.error: BaseException | None = None
        self.latest: KunweiSnapshot | None = None
        self.bias_sum = [0.0] * 6
        self.bias: tuple[float, ...] | None = None
        self.samples = 0
        self.post_baseline_samples = 0
        self.parse_errors = 0
        self.dropped_sync_bytes = 0
        self.bytes_received = 0
        self.capture_state = CaptureState()
        self.capture_start_monotonic_s: float | None = None
        self.receive_batches = 0
        self.frames_per_batch = RunningStats()
        self.delivery_intervals = RunningStats()
        self.last_batch_arrival_monotonic_s: float | None = None
        self.max_force_norm_n = 0.0
        self.max_torque_norm_nm = 0.0
        self.csv_path = output_dir / "kunwei_sensor_1khz.csv"
        self.raw_path = output_dir / "kunwei_raw_frames.bin"

    def start(self) -> None:
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.socket = socket.create_connection(
            (self.sensor_ip, self.sensor_port), timeout=self.connect_timeout_s
        )
        self.socket.settimeout(0.05)
        self.socket.sendall(START_STREAM)
        self.capture_start_monotonic_s = time.monotonic()
        self.thread = threading.Thread(
            target=self._capture_loop,
            name="kunwei-direct-torque-v4-capture",
            daemon=True,
        )
        self.thread.start()

    def __enter__(self) -> "KunweiGuardCapture":
        self.start()
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        self.stop()

    def _capture_loop(self) -> None:
        assert self.socket is not None
        buffer = bytearray()
        fields = [
            "sample_index",
            "receive_batch_id",
            "frame_index_in_batch",
            "receive_batch_size",
            "t_wall_ns",
            "batch_arrival_monotonic_s",
            "nominal_sensor_time_s",
            *KUNWEI_RAW_FIELDS,
            "fx_n_raw",
            "fy_n_raw",
            "fz_n_raw",
            "mx_nm_raw",
            "my_nm_raw",
            "mz_nm_raw",
            "baseline_ready",
            "fx_n_zeroed_tcp",
            "fy_n_zeroed_tcp",
            "fz_n_zeroed_tcp",
            "mx_nm_zeroed_tcp",
            "my_nm_zeroed_tcp",
            "mz_nm_zeroed_tcp",
            "normal_load_n",
            "force_norm_n",
            "torque_norm_nm",
            "frame_hex",
        ]
        try:
            with (
                self.csv_path.open("x", newline="", encoding="utf-8") as csv_handle,
                self.raw_path.open("xb") as raw_handle,
            ):
                writer = csv.DictWriter(csv_handle, fieldnames=fields)
                writer.writeheader()
                while not self.stop_event.is_set():
                    try:
                        chunk = self.socket.recv(8192)
                    except socket.timeout:
                        continue
                    if not chunk:
                        if not self.stop_event.is_set():
                            raise RuntimeError("kunwei_stream_closed")
                        break
                    self.bytes_received += len(chunk)
                    self.capture_state.bytes_received += len(chunk)
                    self.capture_state.packets_received += 1
                    buffer.extend(chunk)
                    frames, dropped = pop_frames(buffer, 0x48)
                    self.dropped_sync_bytes += dropped
                    self.capture_state.dropped_sync_bytes += dropped
                    if not frames:
                        continue
                    batch_arrival_monotonic_s = time.monotonic()
                    batch_arrival_wall_ns = time.time_ns()
                    self.receive_batches += 1
                    receive_batch_id = self.receive_batches
                    self.frames_per_batch.push(float(len(frames)))
                    if self.last_batch_arrival_monotonic_s is not None:
                        self.delivery_intervals.push(
                            batch_arrival_monotonic_s
                            - self.last_batch_arrival_monotonic_s
                        )
                    self.last_batch_arrival_monotonic_s = (
                        batch_arrival_monotonic_s
                    )
                    for frame_index_in_batch, frame in enumerate(frames):
                        try:
                            raw_values = parse_frame(frame)
                        except ValueError:
                            self.parse_errors += 1
                            self.capture_state.parse_errors += 1
                            continue
                        raw_handle.write(frame)
                        raw_si = tuple(
                            float(value)
                            * (FORCE_KG_TO_N if index < 3 else MOMENT_KG_M_TO_NM)
                            for index, value in enumerate(raw_values)
                        )
                        if not all(math.isfinite(value) for value in raw_si):
                            raise RuntimeError("kunwei_raw_nonfinite")
                        self.capture_state.push(
                            frame,
                            raw_values,
                            batch_arrival_monotonic_s,
                        )
                        self.samples = self.capture_state.rows
                        nominal_sensor_time_s = (
                            self.samples - 1
                        ) / 1000.0
                        if self.bias is None:
                            for index, value in enumerate(raw_si):
                                self.bias_sum[index] += value
                            if self.samples == KUNWEI_BASELINE_SAMPLES:
                                self.bias = tuple(
                                    value / KUNWEI_BASELINE_SAMPLES
                                    for value in self.bias_sum
                                )
                        baseline_ready = self.bias is not None
                        if baseline_ready:
                            assert self.bias is not None
                            zeroed_sensor = tuple(
                                value - offset
                                for value, offset in zip(raw_si, self.bias)
                            )
                            wrench_tcp = apply_wrench_transform(
                                zeroed_sensor, self.matrix
                            )
                            force_norm = math.sqrt(
                                sum(value * value for value in wrench_tcp[:3])
                            )
                            torque_norm = math.sqrt(
                                sum(value * value for value in wrench_tcp[3:])
                            )
                            normal_load = (
                                self.normal_sign * wrench_tcp[self.normal_axis]
                            )
                            self.post_baseline_samples += 1
                            self.max_force_norm_n = max(
                                self.max_force_norm_n, force_norm
                            )
                            self.max_torque_norm_nm = max(
                                self.max_torque_norm_nm, torque_norm
                            )
                            snapshot = KunweiSnapshot(
                                sample_index=self.samples,
                                receive_batch_id=receive_batch_id,
                                t_monotonic_s=batch_arrival_monotonic_s,
                                nominal_sensor_time_s=nominal_sensor_time_s,
                                raw_si=raw_si,
                                wrench_tcp_si=tuple(wrench_tcp),
                                normal_load_n=normal_load,
                                force_norm_n=force_norm,
                                torque_norm_nm=torque_norm,
                            )
                            with self.lock:
                                self.latest = snapshot
                            if self.post_baseline_samples >= 50:
                                self.ready_event.set()
                        else:
                            wrench_tcp = ("",) * 6
                            force_norm = ""
                            torque_norm = ""
                            normal_load = ""
                        writer.writerow(
                            {
                                "sample_index": self.samples,
                                "receive_batch_id": receive_batch_id,
                                "frame_index_in_batch": frame_index_in_batch,
                                "receive_batch_size": len(frames),
                                "t_wall_ns": batch_arrival_wall_ns,
                                "batch_arrival_monotonic_s": (
                                    f"{batch_arrival_monotonic_s:.9f}"
                                ),
                                "nominal_sensor_time_s": (
                                    f"{nominal_sensor_time_s:.9f}"
                                ),
                                **dict(zip(KUNWEI_RAW_FIELDS, raw_values)),
                                **dict(
                                    zip(
                                        (
                                            "fx_n_raw",
                                            "fy_n_raw",
                                            "fz_n_raw",
                                            "mx_nm_raw",
                                            "my_nm_raw",
                                            "mz_nm_raw",
                                        ),
                                        raw_si,
                                    )
                                ),
                                "baseline_ready": int(baseline_ready),
                                **dict(
                                    zip(
                                        (
                                            "fx_n_zeroed_tcp",
                                            "fy_n_zeroed_tcp",
                                            "fz_n_zeroed_tcp",
                                            "mx_nm_zeroed_tcp",
                                            "my_nm_zeroed_tcp",
                                            "mz_nm_zeroed_tcp",
                                        ),
                                        wrench_tcp,
                                    )
                                ),
                                "normal_load_n": normal_load,
                                "force_norm_n": force_norm,
                                "torque_norm_nm": torque_norm,
                                "frame_hex": frame.hex(),
                            }
                        )
                csv_handle.flush()
                raw_handle.flush()
        except BaseException as exc:
            with self.lock:
                self.error = exc
            self.ready_event.set()

    def wait_preflight(self, timeout_s: float = 3.0) -> KunweiSnapshot:
        if not self.ready_event.wait(timeout_s):
            raise RuntimeError("kunwei_software_baseline_timeout")
        snapshot = self.snapshot(max_age_s=self.delivery_watchdog_s)
        if snapshot.force_norm_n > KUNWEI_PREFLIGHT_FORCE_N:
            raise RuntimeError("kunwei_preflight_force_over_2n")
        if snapshot.torque_norm_nm > KUNWEI_PREFLIGHT_TORQUE_NM:
            raise RuntimeError("kunwei_preflight_torque_over_0_2nm")
        return snapshot

    def snapshot(self, *, max_age_s: float) -> KunweiSnapshot:
        with self.lock:
            error = self.error
            snapshot = self.latest
        if error is not None:
            raise RuntimeError(
                f"kunwei_capture_failed:{type(error).__name__}:{error}"
            ) from error
        if snapshot is None:
            raise RuntimeError("kunwei_baseline_not_ready")
        if time.monotonic() - snapshot.t_monotonic_s > max_age_s:
            raise RuntimeError("kunwei_delivery_stale")
        if snapshot.force_norm_n > KUNWEI_ACTIVE_FORCE_N:
            raise RuntimeError("kunwei_active_force_over_6n")
        if snapshot.torque_norm_nm > KUNWEI_ACTIVE_TORQUE_NM:
            raise RuntimeError("kunwei_active_torque_over_0_5nm")
        return snapshot

    def stop(self) -> None:
        if self.thread is None:
            return
        self.stop_event.set()
        if self.socket is not None:
            try:
                self.socket.sendall(STOP_STREAM)
            except OSError:
                pass
        self.thread.join(timeout=2.0)
        if self.thread.is_alive():
            raise RuntimeError("kunwei_capture_thread_stop_timeout")
        if self.socket is not None:
            self.socket.close()
        self.thread = None

    def summary(self) -> dict[str, Any]:
        first = self.capture_state.first_t_monotonic
        last = self.capture_state.last_t_monotonic
        span = (
            None if first is None or last is None else last - first
        )
        batch_sizes = self.frames_per_batch.as_dict()
        delivery = self.delivery_intervals.as_dict()
        return {
            "force_source": "kunwei_software_baselined_sensor_to_tcp_si",
            "native_sensor_rate_hz": 1000,
            "sensor_ip": self.sensor_ip,
            "sensor_port": self.sensor_port,
            "samples": self.samples,
            "post_baseline_samples": self.post_baseline_samples,
            "software_baseline_samples": KUNWEI_BASELINE_SAMPLES,
            "baseline_si_offsets": self.bias,
            "duration_first_last_s": span,
            "rate_hz_by_first_last": (
                (self.samples - 1) / span
                if span is not None and span > 0.0 and self.samples > 1
                else None
            ),
            "parse_errors": self.parse_errors,
            "dropped_sync_bytes": self.dropped_sync_bytes,
            "bytes_received": self.bytes_received,
            "receive_batches": self.receive_batches,
            "mean_frames_per_batch": batch_sizes.get("mean"),
            "max_frames_per_batch": batch_sizes.get("max"),
            "mean_delivery_gap_ms": (
                None
                if delivery.get("mean") is None
                else float(delivery["mean"]) * 1000.0
            ),
            "max_delivery_gap_ms": (
                None
                if delivery.get("max") is None
                else float(delivery["max"]) * 1000.0
            ),
            "sensor_delivery_watchdog_s": self.delivery_watchdog_s,
            "causal_1khz_alignment_valid": False,
            "arrival_timestamp_semantics": (
                "one_host_monotonic_timestamp_per_tcp_receive_batch"
            ),
            "nominal_sensor_time_semantics": (
                "sample_index_divided_by_vendor_native_1000hz"
            ),
            "max_zeroed_force_norm_n": self.max_force_norm_n,
            "max_zeroed_torque_norm_nm": self.max_torque_norm_nm,
            "csv": str(self.csv_path),
            "raw_frames": str(self.raw_path),
        }


def _receive_available(
    rtde: Any,
    recipe: int,
    types: list[str],
    fields: list[str],
    timeout_s: float,
) -> list[dict[str, Any]]:
    receiver = getattr(rtde, "receive_available", None)
    if callable(receiver):
        return list(receiver(recipe, types, fields, timeout_s))
    sample = rtde.receive_latest(recipe, types, fields, timeout_s)
    return [] if sample is None else [sample]


@dataclass(frozen=True)
class ReferenceTimeline:
    rows: tuple[Mapping[str, Any], ...]
    duration_s: float

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "ReferenceTimeline":
        if payload.get("schema") != REFERENCE_SCHEMA:
            raise ValueError("reference schema mismatch")
        raw_rows = payload.get("references")
        trajectory = payload.get("trajectory")
        if not isinstance(raw_rows, list) or len(raw_rows) < 2:
            raise ValueError("reference rows are missing")
        if not isinstance(trajectory, Mapping):
            raise ValueError("reference trajectory is missing")
        duration = float(trajectory.get("capture_duration_s", 0.0))
        if not 0.0 < duration <= 2.0:
            raise ValueError("reference duration exceeds the no-contact canary")
        rows = tuple(row for row in raw_rows if isinstance(row, Mapping))
        if len(rows) != len(raw_rows):
            raise ValueError("reference row type mismatch")
        progress = [float(row["progress_s"]) for row in rows]
        if progress[0] != 0.0 or progress[-1] < duration:
            raise ValueError("reference timeline does not cover capture duration")
        if any(right <= left for left, right in zip(progress, progress[1:])):
            raise ValueError("reference progress must be strictly increasing")
        for row in rows:
            _finite6(row["desired_pose_base"], "desired_pose_base")
        return cls(rows=rows, duration_s=duration)

    def row_at(self, elapsed_s: float) -> Mapping[str, Any]:
        bounded = min(max(float(elapsed_s), 0.0), self.duration_s)
        low = 0
        high = len(self.rows)
        while low < high:
            middle = (low + high) // 2
            if float(self.rows[middle]["progress_s"]) <= bounded:
                low = middle + 1
            else:
                high = middle
        return self.rows[max(0, low - 1)]


@dataclass(frozen=True)
class CanaryTimeline:
    """Bounded stage trajectory, anchored to the fresh actual TCP pose."""

    stage: str
    start_pose: tuple[float, ...]
    duration_s: float
    bundle_timeline: ReferenceTimeline
    ramp_axis: tuple[float, ...]

    @classmethod
    def from_stage(
        cls,
        stage: str,
        *,
        actual_pose: Sequence[float],
        bundle: "ValidatedBundle",
    ) -> "CanaryTimeline":
        if stage not in CANARY_STAGE_ORDER:
            raise ValueError(f"unknown_canary_stage:{stage}")
        start_pose = _finite6(actual_pose, "canary_start_pose")
        duration_s = CANARY_STAGE_DURATIONS_S[stage]
        if stage == CANARY_STAGE_REFERENCE:
            duration_s = bundle.timeline.duration_s
        timeline = cls(
            stage=stage,
            start_pose=start_pose,
            duration_s=duration_s,
            bundle_timeline=bundle.timeline,
            ramp_axis=tuple(float(value) for value in bundle.tube.v_axis_base),
        )
        episode_tube = bundle.tube.rebased(start_pose)
        episode_tube.assert_contains_pose(
            timeline.row_at(0.0)["desired_pose_base"], role="desired"
        )
        episode_tube.assert_contains_pose(
            timeline.row_at(duration_s)["desired_pose_base"], role="desired"
        )
        if stage in CANARY_REFERENCE_STAGES:
            for row in bundle.timeline.rows:
                stage_progress_s = (
                    float(row["progress_s"])
                    * duration_s
                    / bundle.timeline.duration_s
                )
                episode_tube.assert_contains_pose(
                    timeline.row_at(stage_progress_s)["desired_pose_base"],
                    role="desired",
                )
        return timeline

    @property
    def rows(self) -> tuple[Mapping[str, Any], ...]:
        return (self.row_at(0.0), self.row_at(self.duration_s))

    def row_at(self, elapsed_s: float) -> Mapping[str, Any]:
        bounded = min(max(float(elapsed_s), 0.0), self.duration_s)
        if self.stage in CANARY_REFERENCE_STAGES:
            reference_progress_s = (
                bounded
                * self.bundle_timeline.duration_s
                / self.duration_s
            )
            reference_row = self.bundle_timeline.row_at(reference_progress_s)
            reference_origin = _finite6(
                self.bundle_timeline.rows[0]["desired_pose_base"],
                "reference_origin_pose",
            )
            reference_pose = _finite6(
                reference_row["desired_pose_base"],
                "reference_desired_pose",
            )
            pose = list(self.start_pose)
            for index in range(3):
                pose[index] += reference_pose[index] - reference_origin[index]
            return {
                "progress_s": float(reference_row["progress_s"]),
                "desired_pose_base": tuple(pose),
            }
        pose = list(self.start_pose)
        if self.stage == CANARY_STAGE_RAMP:
            phase = bounded / self.duration_s
            smooth = phase * phase * (3.0 - 2.0 * phase)
            for index in range(3):
                pose[index] += 0.0002 * smooth * self.ramp_axis[index]
        return {"progress_s": bounded, "desired_pose_base": tuple(pose)}


@dataclass
class AckPacedScheduler:
    """v34 absolute-deadline release plus ACK-gated, no-burst sequencing."""

    last_sent_sequence: int = 0
    period_s: float = 0.002
    next_release_s: float | None = None
    release_count: int = 0
    missed_slots_total: int = 0
    overrun_events: int = 0
    max_lateness_s: float = 0.0

    def arm(self, now_s: float) -> None:
        if self.next_release_s is not None:
            raise RuntimeError("command_scheduler_already_armed")
        if not math.isfinite(float(now_s)):
            raise ValueError("command_scheduler_start_nonfinite")
        self.next_release_s = float(now_s)

    def release_due(self, now_s: float) -> bool:
        if self.next_release_s is None:
            raise RuntimeError("command_scheduler_not_armed")
        now = float(now_s)
        if not math.isfinite(now):
            raise ValueError("command_scheduler_now_nonfinite")
        if now < self.next_release_s:
            return False
        next_release, missed_slots, lateness = advance_periodic_deadline(
            self.next_release_s,
            now,
            self.period_s,
        )
        self.next_release_s = next_release
        self.release_count += 1
        self.missed_slots_total += missed_slots
        if missed_slots > 0:
            self.overrun_events += 1
        self.max_lateness_s = max(self.max_lateness_s, lateness)
        return True

    def next_sequence(self, acknowledged_sequence: int) -> int | None:
        acknowledged = int(acknowledged_sequence)
        if acknowledged < 0 or acknowledged > self.last_sent_sequence:
            raise RuntimeError("controller_ack_out_of_range")
        if acknowledged != self.last_sent_sequence:
            return None
        self.last_sent_sequence += 1
        return self.last_sent_sequence

    def summary(self) -> dict[str, Any]:
        return {
            "policy": "v34_absolute_deadline_ack_gated_no_burst",
            "period_s": self.period_s,
            "release_count": self.release_count,
            "missed_slots_total": self.missed_slots_total,
            "overrun_events": self.overrun_events,
            "max_lateness_s": self.max_lateness_s,
            "burst_catchup_allowed": False,
        }


@dataclass(frozen=True)
class CommandLineage:
    """Immutable command/reference identity attached to one command sequence."""

    command_mode: int
    command_sequence: int
    progress_s: float
    desired_pose: tuple[float, ...]
    commanded_k: tuple[float, ...]
    kunwei_guard_wrench_tcp_si: tuple[float, ...]
    commanded_raw_f_ff: tuple[float, ...]
    kunwei_sample_index: int
    kunwei_receive_batch_id: int
    kunwei_nominal_sensor_time_s: float | None
    kunwei_batch_arrival_monotonic_s: float | None
    lease: int
    episode: int
    model_mode: int
    frame_token: int


@dataclass(frozen=True)
class CommandPacket:
    lineage: CommandLineage
    values: tuple[Any, ...]


def _command_packet(
    *,
    command: int,
    sequence: int,
    progress_s: float,
    pose: Sequence[float],
    lease_id: int,
    episode_identity: int,
    kunwei_guard_wrench_tcp_si: Sequence[float] = ZERO6,
    kunwei_sample_index: int = 0,
    kunwei_receive_batch_id: int = 0,
    kunwei_nominal_sensor_time_s: float | None = None,
    kunwei_batch_arrival_monotonic_s: float | None = None,
) -> CommandPacket:
    desired_pose = _finite6(pose, "desired_pose")
    guard_wrench = _finite6(
        kunwei_guard_wrench_tcp_si, "kunwei_guard_wrench_tcp_si"
    )
    packet_values = tuple(
        command_values(
            command=command,
            sequence=sequence,
            pose=desired_pose,
            lease_id=lease_id,
            episode_identity=episode_identity,
            kunwei_guard_wrench_tcp_si=guard_wrench,
        )
    )
    lineage = CommandLineage(
        command_mode=int(command),
        command_sequence=int(sequence),
        progress_s=float(progress_s),
        desired_pose=desired_pose,
        commanded_k=tuple(float(value) for value in packet_values[6:12]),
        kunwei_guard_wrench_tcp_si=tuple(
            float(value) for value in packet_values[12:18]
        ),
        commanded_raw_f_ff=tuple(float(value) for value in packet_values[18:24]),
        kunwei_sample_index=int(kunwei_sample_index),
        kunwei_receive_batch_id=int(kunwei_receive_batch_id),
        kunwei_nominal_sensor_time_s=(
            None
            if kunwei_nominal_sensor_time_s is None
            else float(kunwei_nominal_sensor_time_s)
        ),
        kunwei_batch_arrival_monotonic_s=(
            None
            if kunwei_batch_arrival_monotonic_s is None
            else float(kunwei_batch_arrival_monotonic_s)
        ),
        lease=int(packet_values[27]),
        episode=int(packet_values[35]),
        model_mode=int(packet_values[30]),
        frame_token=int(packet_values[31]),
    )
    return CommandPacket(lineage=lineage, values=packet_values)


def _register_command_lineage(
    lineages: dict[int, CommandLineage], packet: CommandPacket
) -> None:
    sequence = packet.lineage.command_sequence
    if sequence in lineages and lineages[sequence] != packet.lineage:
        raise RuntimeError(f"command_sequence_lineage_rebound:{sequence}")
    lineages[sequence] = packet.lineage


@dataclass(frozen=True)
class ValidatedBundle:
    source_path: Path
    manifest_path: Path
    reference_path: Path
    source_sha256: str
    manifest_sha256: str
    reference_sha256: str
    friction_profile: str
    viscous_scale: tuple[float, ...]
    coulomb_scale: tuple[float, ...]
    source: str
    reference: Mapping[str, Any]
    timeline: ReferenceTimeline
    tube: LiveTubeContract


def validate_bundle(manifest_path: Path) -> ValidatedBundle:
    manifest_path = manifest_path.resolve()
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(manifest, Mapping) or manifest.get("schema") != BUNDLE_SCHEMA:
        raise ValueError("bundle manifest schema mismatch")
    source_path = manifest_path.parent / str(manifest["receiver_source"])
    reference_path = Path(str(manifest["reference_artifact"])).resolve()
    source_sha = _sha256(source_path)
    reference_sha = _sha256(reference_path)
    if source_sha != manifest.get("receiver_source_sha256"):
        raise ValueError("receiver source SHA-256 mismatch")
    if reference_sha != manifest.get("reference_artifact_sha256"):
        raise ValueError("reference artifact SHA-256 mismatch")
    source = source_path.read_text(encoding="utf-8")
    contract = parse_live_receiver_source(source)
    if contract.protocol_token != LIVE_PROTOCOL_TOKEN:
        raise ValueError("receiver protocol token mismatch")
    manifest_profile = manifest.get("friction_profile")
    manifest_viscous_raw = manifest.get("viscous_scale")
    manifest_coulomb_raw = manifest.get("coulomb_scale")
    if (
        manifest_profile is None
        and manifest_viscous_raw is None
        and manifest_coulomb_raw is None
        and contract.friction_profile == FRICTION_PROFILE_ZERO_ISOLATION
    ):
        manifest_profile = contract.friction_profile
        manifest_viscous = contract.viscous_scale
        manifest_coulomb = contract.coulomb_scale
    else:
        manifest_viscous = _finite6(manifest_viscous_raw, "viscous_scale")
        manifest_coulomb = _finite6(manifest_coulomb_raw, "coulomb_scale")
    if (
        manifest_profile != contract.friction_profile
        or manifest_viscous != contract.viscous_scale
        or manifest_coulomb != contract.coulomb_scale
    ):
        raise ValueError("bundle friction profile does not match receiver source")
    reference = json.loads(reference_path.read_text(encoding="utf-8"))
    if not isinstance(reference, Mapping):
        raise ValueError("reference artifact type mismatch")
    timeline = ReferenceTimeline.from_payload(reference)
    tube = LiveTubeContract.from_reference_artifact(reference_path)
    for row in timeline.rows:
        tube.assert_contains_pose(row["desired_pose_base"], role="desired")
    return ValidatedBundle(
        source_path=source_path,
        manifest_path=manifest_path,
        reference_path=reference_path,
        source_sha256=source_sha,
        manifest_sha256=_sha256(manifest_path),
        reference_sha256=reference_sha,
        friction_profile=contract.friction_profile,
        viscous_scale=contract.viscous_scale,
        coulomb_scale=contract.coulomb_scale,
        source=source,
        reference=reference,
        timeline=timeline,
        tube=tube,
    )


def validate_compile_probe_evidence(
    path: Path,
    *,
    robot_host: str,
) -> Mapping[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    expected_source_sha = hashlib.sha256(
        build_compile_probe_source().encode("utf-8")
    ).hexdigest()
    checks = {
        "schema": COMPILE_PROBE_EVIDENCE_SCHEMA,
        "claim_class": "live_controller_compile_probe_no_motion",
        "ok": True,
        "robot_host": robot_host,
        "compile_probe_source_sha256": expected_source_sha,
        "motion_performed": False,
        "direct_torque_called": False,
        "rtde_inputs_written": False,
        "kunwei_stream_started": False,
    }
    failures = [
        key for key, expected in checks.items() if payload.get(key) != expected
    ]
    strict = payload.get("strict_success_gate")
    if not isinstance(strict, Mapping) or strict.get("ok") is not True:
        failures.append("strict_success_gate")
    if failures:
        raise RuntimeError(
            "compile_probe_evidence_mismatch:" + ",".join(sorted(set(failures)))
        )
    return payload


def validate_prior_stage_evidence(
    path: Path | None,
    *,
    required_stage: str | None,
    bundle: ValidatedBundle,
    robot_host: str,
) -> Mapping[str, Any] | None:
    if required_stage is None:
        if path is not None:
            raise RuntimeError("prior_stage_evidence_not_allowed_for_first_stage")
        return None
    if path is None:
        raise RuntimeError(f"prior_stage_evidence_required:{required_stage}")
    payload = json.loads(path.read_text(encoding="utf-8"))
    runtime_binding = runtime_source_binding()
    checks = {
        "schema": CANARY_EVIDENCE_SCHEMA,
        "ok": True,
        "robot_host": robot_host,
        "receiver_source_sha256": bundle.source_sha256,
        "bundle_manifest_sha256": bundle.manifest_sha256,
        "reference_artifact_sha256": bundle.reference_sha256,
        "runtime_source_sha256": runtime_binding["sha256"],
        "canary_stage": required_stage,
        "kunwei_stream_started": True,
        "kunwei_force_source": "kunwei_software_baselined_sensor_to_tcp_si",
    }
    if (
        payload.get("friction_profile") is not None
        or bundle.friction_profile != FRICTION_PROFILE_ZERO_ISOLATION
    ):
        checks["friction_profile"] = bundle.friction_profile
    failures = [
        key for key, expected in checks.items() if payload.get(key) != expected
    ]
    strict = payload.get("strict_success_gate")
    if not isinstance(strict, Mapping) or strict.get("ok") is not True:
        failures.append("strict_success_gate")
    if failures:
        raise RuntimeError(
            "prior_stage_evidence_mismatch:" + ",".join(sorted(set(failures)))
        )
    return payload


def command_values(
    *,
    command: int,
    sequence: int,
    pose: Sequence[float],
    lease_id: int,
    episode_identity: int,
    kunwei_guard_wrench_tcp_si: Sequence[float] = ZERO6,
) -> list[Any]:
    desired_pose = _finite6(pose, "desired_pose")
    guard_wrench = _finite6(
        kunwei_guard_wrench_tcp_si, "kunwei_guard_wrench_tcp_si"
    )
    doubles = [
        *desired_pose,
        *FIXED_STIFFNESS,
        *guard_wrench,
        *ZERO6,
    ]
    integers = [
        int(command),
        int(sequence),
        int(sequence),
        int(lease_id),
        0,
        0,
        0,
        WRENCH_FRAME_TOKEN,
        0,
        0,
        0,
        int(episode_identity),
    ]
    return doubles + integers


def readonly_status(robot_host: str) -> dict[str, Any]:
    dashboard = dashboard_exchange(
        robot_host,
        [
            "PolyscopeVersion",
            "get loaded program",
            "programState",
            "robotmode",
            "safetystatus",
            "is in remote control",
        ],
    )
    rtde, stationarity_samples = _read_rtde_status_window(robot_host)
    stationarity_window = _stationarity_window_evidence(stationarity_samples)
    stationary = bool(stationarity_window["ok"])
    return {
        "dashboard": dashboard,
        "rtde": rtde,
        "stationarity_window": stationarity_window,
        "remote_control": dashboard.get("is in remote control", "").lower() == "true",
        "stopped": int(rtde["runtime_state"]) == RUNTIME_STOPPED,
        "stationary": stationary,
        "motion_performed": False,
        "urscript_sent": False,
        "rtde_inputs_written": False,
        "kunwei_stream_started": False,
    }


def validate_compile_probe_preflight(status: Mapping[str, Any]) -> None:
    dashboard = status["dashboard"]
    rtde = status["rtde"]
    failures = []
    if not status["remote_control"]:
        failures.append("controller_not_remote_control")
    if not status["stopped"]:
        failures.append("controller_program_not_stopped")
    if not status["stationary"]:
        failures.append("robot_not_stationary")
    if "URSoftware 5.26." not in dashboard.get("PolyscopeVersion", ""):
        failures.append("controller_not_5_26")
    if dashboard.get("safetystatus") != "Safetystatus: NORMAL":
        failures.append("dashboard_safety_not_normal")
    if dashboard.get("robotmode") != "Robotmode: RUNNING":
        failures.append("dashboard_robotmode_not_running")
    if int(rtde["safety_mode"]) != SAFETY_MODE_NORMAL:
        failures.append("rtde_safety_not_normal")
    if int(rtde["robot_mode"]) != ROBOT_MODE_RUNNING:
        failures.append("rtde_robotmode_not_running")
    if failures:
        raise RuntimeError(
            "compile_probe_preflight_failed:" + ",".join(failures)
        )


def validate_live_preflight(
    status: Mapping[str, Any],
    bundle: ValidatedBundle,
    *,
    timeline: ReferenceTimeline | CanaryTimeline,
) -> None:
    dashboard = status["dashboard"]
    rtde = status["rtde"]
    first_desired_pose = timeline.rows[0]["desired_pose_base"]
    failures = []
    if not status["remote_control"]:
        failures.append("controller_not_remote_control")
    if not status["stopped"]:
        failures.append("controller_program_not_stopped")
    if not status["stationary"]:
        failures.append("robot_not_stationary")
    if "URSoftware 5.26." not in dashboard.get("PolyscopeVersion", ""):
        failures.append("controller_not_5_26")
    if dashboard.get("safetystatus") != "Safetystatus: NORMAL":
        failures.append("dashboard_safety_not_normal")
    if dashboard.get("robotmode") != "Robotmode: RUNNING":
        failures.append("dashboard_robotmode_not_running")
    if int(rtde["safety_mode"]) != SAFETY_MODE_NORMAL:
        failures.append("rtde_safety_not_normal")
    if int(rtde["robot_mode"]) != ROBOT_MODE_RUNNING:
        failures.append("rtde_robotmode_not_running")
    if (
        _sample_translation_error_sqm3(rtde["actual_TCP_pose"], first_desired_pose)
        > NO_CONTACT_RELEASE_TOLERANCE_M
    ):
        failures.append("release_translation_error_exceeds_1mm")
    try:
        bundle.tube.rebased(first_desired_pose).assert_contains_pose(
            rtde["actual_TCP_pose"], role="actual"
        )
    except RuntimeError as exc:
        failures.append(str(exc))
    if failures:
        raise RuntimeError("live_preflight_failed:" + ",".join(failures))


def _wait_for_fresh_receiver_waiting(
    rtde: LiveRTDE,
    output_recipe: int,
    output_types: list[str],
    output_fields: list[str],
    *,
    receiver_wait_s: float,
    lease_id: int,
    episode_identity: int,
    samples_out: list[dict[str, Any]] | None = None,
    diagnostic_samples_out: list[dict[str, Any]] | None = None,
) -> tuple[float, Mapping[str, Any]]:
    deadline = time.monotonic() + receiver_wait_s
    diagnostics = (
        diagnostic_samples_out
        if diagnostic_samples_out is not None
        else []
    )
    while time.monotonic() < deadline:
        batch = _receive_available(
            rtde, output_recipe, output_types, output_fields, 0.01
        )
        if not batch:
            continue
        diagnostics.extend(dict(value) for value in batch)
        for index, sample in enumerate(batch):
            state = int(sample["output_int_register_24"])
            runtime_state = int(sample["runtime_state"])
            protocol = int(sample["output_int_register_32"])
            if protocol != LIVE_PROTOCOL_TOKEN:
                continue
            if state != STATE_WAITING:
                continue
            if runtime_state != RUNTIME_PLAYING:
                continue
            if int(sample["output_int_register_34"]) != MODE_IDLE:
                continue
            if int(sample["output_int_register_35"]) != 1:
                continue
            if int(sample["output_int_register_27"]) != lease_id:
                continue
            if int(sample["output_int_register_31"]) != episode_identity:
                continue
            if samples_out is not None:
                samples_out.clear()
                samples_out.extend(dict(value) for value in batch[index:])
            return time.monotonic(), sample
    summary = summarize_receiver_handshake_samples(
        diagnostics,
        lease_id=lease_id,
        episode_identity=episode_identity,
    )
    if summary["protocol_rows"] == 0:
        reason = "receiver_protocol_missing_after_send"
    elif summary["waiting_rows"] == 0:
        reason = "receiver_waiting_state_missing"
    elif summary["waiting_playing_rows"] == 0:
        reason = "receiver_waiting_stale_after_send"
    elif summary["idle_echo_rows"] == 0:
        reason = "receiver_idle_echo_missing"
    elif summary["episode_latched_rows"] == 0:
        reason = "receiver_episode_not_latched"
    elif summary["lease_match_rows"] == 0:
        reason = "receiver_lease_echo_mismatch"
    elif summary["episode_match_rows"] == 0:
        reason = "receiver_episode_echo_mismatch"
    else:
        reason = "receiver_handshake_unclassified_timeout"
    raise RuntimeError(
        reason
        + ":"
        + json.dumps(summary, sort_keys=True, separators=(",", ":"))
    )


def summarize_receiver_handshake_samples(
    samples: Sequence[Mapping[str, Any]],
    *,
    lease_id: int,
    episode_identity: int,
) -> dict[str, Any]:
    protocol_rows = [
        sample
        for sample in samples
        if int(sample.get("output_int_register_32", -1))
        == LIVE_PROTOCOL_TOKEN
    ]
    waiting_rows = [
        sample
        for sample in protocol_rows
        if int(sample.get("output_int_register_24", -1)) == STATE_WAITING
    ]
    playing_rows = [
        sample
        for sample in waiting_rows
        if int(sample.get("runtime_state", -1)) == RUNTIME_PLAYING
    ]
    idle_rows = [
        sample
        for sample in playing_rows
        if int(sample.get("output_int_register_34", -1)) == MODE_IDLE
    ]
    latched_rows = [
        sample
        for sample in idle_rows
        if int(sample.get("output_int_register_35", -1)) == 1
    ]
    lease_rows = [
        sample
        for sample in latched_rows
        if int(sample.get("output_int_register_27", -1)) == lease_id
    ]
    episode_rows = [
        sample
        for sample in lease_rows
        if int(sample.get("output_int_register_31", -1))
        == episode_identity
    ]
    last = samples[-1] if samples else {}
    return {
        "total_rows": len(samples),
        "protocol_rows": len(protocol_rows),
        "waiting_rows": len(waiting_rows),
        "waiting_playing_rows": len(playing_rows),
        "idle_echo_rows": len(idle_rows),
        "episode_latched_rows": len(latched_rows),
        "lease_match_rows": len(lease_rows),
        "episode_match_rows": len(episode_rows),
        "accepted_rows": len(episode_rows),
        "last_observed": {
            "controller_timestamp_s": last.get("timestamp"),
            "runtime_state": last.get("runtime_state"),
            "receiver_state": last.get("output_int_register_24"),
            "protocol_echo": last.get("output_int_register_32"),
            "observed_command_echo": last.get("output_int_register_34"),
            "episode_latched_echo": last.get("output_int_register_35"),
            "lease_echo": last.get("output_int_register_27"),
            "episode_echo": last.get("output_int_register_31"),
        },
    }


def _stale_terminal_receiver_allows_one_resend(
    samples: Sequence[Mapping[str, Any]],
    *,
    lease_id: int,
    episode_identity: int,
) -> bool:
    """Allow one resend only when no new receiver execution was observed."""

    summary = summarize_receiver_handshake_samples(
        samples,
        lease_id=lease_id,
        episode_identity=episode_identity,
    )
    last = summary["last_observed"]
    return bool(
        summary["protocol_rows"] > 0
        and summary["waiting_rows"] == 0
        and summary["accepted_rows"] == 0
        and int(last.get("runtime_state", -1)) == RUNTIME_STOPPED
        and int(last.get("receiver_state", -1)) in (STATE_COMPLETE, STATE_FAULT)
        and int(last.get("lease_echo", -1)) != lease_id
        and int(last.get("episode_echo", -1)) != episode_identity
    )


def _prime_idle_inputs(
    rtde: LiveRTDE,
    input_recipe: int,
    input_types: list[str],
    idle_values: Sequence[Any],
    output_recipe: int,
    output_types: list[str],
    output_fields: list[str],
    *,
    timeout_s: float = 0.25,
    minimum_fresh_ticks: int = 5,
) -> None:
    """Overwrite stale terminal commands before starting the URScript receiver."""
    deadline = time.monotonic() + timeout_s
    baseline_timestamp: float | None = None
    while time.monotonic() < deadline and baseline_timestamp is None:
        baseline_batch = _receive_available(
            rtde, output_recipe, output_types, output_fields, 0.01
        )
        for sample in baseline_batch:
            if int(sample["robot_mode"]) != ROBOT_MODE_RUNNING:
                raise RuntimeError("idle_prime_robotmode_changed")
            if int(sample["safety_mode"]) != SAFETY_MODE_NORMAL:
                raise RuntimeError("idle_prime_safety_changed")
            if int(sample["runtime_state"]) != RUNTIME_STOPPED:
                raise RuntimeError("idle_prime_runtime_not_stopped")
            timestamp = float(sample["timestamp"])
            if baseline_timestamp is None or timestamp > baseline_timestamp:
                baseline_timestamp = timestamp
    if baseline_timestamp is None:
        raise RuntimeError("idle_prime_baseline_timeout")

    timestamps: list[float] = []
    while time.monotonic() < deadline:
        rtde.send_inputs(input_recipe, input_types, idle_values)
        batch = _receive_available(
            rtde, output_recipe, output_types, output_fields, 0.01
        )
        for sample in batch:
            if int(sample["robot_mode"]) != ROBOT_MODE_RUNNING:
                raise RuntimeError("idle_prime_robotmode_changed")
            if int(sample["safety_mode"]) != SAFETY_MODE_NORMAL:
                raise RuntimeError("idle_prime_safety_changed")
            if int(sample["runtime_state"]) != RUNTIME_STOPPED:
                raise RuntimeError("idle_prime_runtime_not_stopped")
            timestamp = float(sample["timestamp"])
            if timestamp <= baseline_timestamp:
                continue
            if not timestamps or timestamp > timestamps[-1]:
                timestamps.append(timestamp)
            if (
                len(timestamps) >= minimum_fresh_ticks
                and timestamps[-1] - timestamps[0]
                >= (minimum_fresh_ticks - 1) / 500.0
            ):
                return
    raise RuntimeError("idle_prime_timeout")


def _send_urscript(host: str, source: str, timeout_s: float) -> None:
    payload = source if source.endswith("\n") else source + "\n"
    with socket.create_connection((host, 30002), timeout=timeout_s) as connection:
        connection.sendall(payload.encode("utf-8"))


@contextmanager
def _primary_client_observer(
    host: str,
    *,
    timeout_s: float,
):
    """Hold a read-only Primary Client connection while Secondary runs."""

    connection = socket.create_connection((host, 30001), timeout=timeout_s)
    connection.settimeout(0.1)
    stop_event = threading.Event()
    stats = {"connected": True, "bytes_received": 0}

    def drain() -> None:
        while not stop_event.is_set():
            try:
                payload = connection.recv(65536)
            except socket.timeout:
                continue
            except OSError:
                break
            if not payload:
                break
            stats["bytes_received"] += len(payload)

    thread = threading.Thread(
        target=drain,
        name="ur-primary-client-observer",
        daemon=True,
    )
    thread.start()
    try:
        yield stats
    finally:
        stop_event.set()
        try:
            connection.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        connection.close()
        thread.join(timeout=1.0)


def _send_urscript_with_primary_start_barrier(
    host: str,
    source: str,
    *,
    timeout_s: float,
    barrier_hold_s: float = 0.15,
) -> dict[str, Any]:
    """Send Secondary source beside a fresh, read-only Primary connection."""

    with _primary_client_observer(host, timeout_s=timeout_s) as stats:
        _send_urscript(host, source, timeout_s)
        time.sleep(barrier_hold_s)
    return {
        **stats,
        "barrier_hold_s": barrier_hold_s,
    }


def _output_row(
    sample: Mapping[str, Any],
    host_elapsed_s: float,
    *,
    outgoing: CommandLineage,
    acked: CommandLineage | None,
) -> dict[str, Any]:
    receiver_state = int(sample["output_int_register_24"])
    publish_generation_begin = int(sample["output_int_register_29"])
    publish_generation_end = int(sample["output_int_register_33"])
    action_echo_coherent = (
        receiver_state in {STATE_STARTUP, STATE_TORQUE}
        and publish_generation_begin > 0
        and publish_generation_begin == publish_generation_end
    )
    row: dict[str, Any] = {
        "host_elapsed_s": host_elapsed_s,
        "controller_timestamp_s": float(sample["timestamp"]),
        "runtime_state": int(sample["runtime_state"]),
        "robot_mode": int(sample["robot_mode"]),
        "safety_mode": int(sample["safety_mode"]),
        "receiver_state": receiver_state,
        "ack_sequence": int(sample["output_int_register_25"]),
        "fault": int(sample["output_int_register_26"]),
        "lease_echo": int(sample["output_int_register_27"]),
        "model_sequence_echo": int(sample["output_int_register_28"]),
        "action_publish_generation_begin": publish_generation_begin,
        "frame_echo": int(sample["output_int_register_30"]),
        "episode_echo": int(sample["output_int_register_31"]),
        "protocol_echo": int(sample["output_int_register_32"]),
        "action_publish_generation_end": publish_generation_end,
        "action_echo_coherent": action_echo_coherent,
        "exit_reason": (
            publish_generation_end
            if receiver_state in {STATE_FAULT, STATE_COMPLETE}
            else 0
        ),
        "observed_command_echo": int(sample["output_int_register_34"]),
        "episode_latched_echo": int(sample["output_int_register_35"]),
        "max_abs_tau_nm": float(sample["output_double_register_24"]),
        "steptime_s": float(sample["output_double_register_25"]),
        "control_update_dt_s": float(sample["output_double_register_44"]),
        "control_update_count": float(sample["output_double_register_45"]),
        "maximum_control_update_gap_s": float(
            sample["output_double_register_46"]
        ),
        "torque_thread_tick_count": float(
            sample["output_double_register_47"]
        ),
        "outgoing_command_mode": outgoing.command_mode,
        "outgoing_command_sequence": outgoing.command_sequence,
        "acked_command_mode": "" if acked is None else acked.command_mode,
        "acked_command_sequence": "" if acked is None else acked.command_sequence,
        "ack_command_lineage_missing": int(acked is None),
    }
    associated = acked
    row["command_progress_s"] = "" if associated is None else associated.progress_s
    for index in range(6):
        row[f"command_desired_pose_{index}"] = (
            "" if associated is None else associated.desired_pose[index]
        )
        row[f"commanded_k_{index}"] = (
            "" if associated is None else associated.commanded_k[index]
        )
        row[f"kunwei_guard_wrench_tcp_si_{index}"] = (
            ""
            if associated is None
            else associated.kunwei_guard_wrench_tcp_si[index]
        )
        row[f"commanded_raw_f_ff_{index}"] = (
            "" if associated is None else associated.commanded_raw_f_ff[index]
        )
    row["command_lease"] = "" if associated is None else associated.lease
    row["command_episode"] = "" if associated is None else associated.episode
    row["command_model_mode"] = "" if associated is None else associated.model_mode
    row["command_frame_token"] = "" if associated is None else associated.frame_token
    row["kunwei_sample_index"] = (
        "" if associated is None else associated.kunwei_sample_index
    )
    row["kunwei_receive_batch_id"] = (
        "" if associated is None else associated.kunwei_receive_batch_id
    )
    row["kunwei_nominal_sensor_time_s"] = (
        ""
        if associated is None or associated.kunwei_nominal_sensor_time_s is None
        else associated.kunwei_nominal_sensor_time_s
    )
    row["kunwei_batch_arrival_monotonic_s"] = (
        ""
        if associated is None
        or associated.kunwei_batch_arrival_monotonic_s is None
        else associated.kunwei_batch_arrival_monotonic_s
    )
    for prefix, lineage in (
        ("outgoing_command", outgoing),
        ("acked_command", acked),
    ):
        row[f"{prefix}_progress_s"] = (
            "" if lineage is None else lineage.progress_s
        )
        for index in range(6):
            row[f"{prefix}_desired_pose_{index}"] = (
                "" if lineage is None else lineage.desired_pose[index]
            )
            row[f"{prefix}ed_k_{index}"] = (
                "" if lineage is None else lineage.commanded_k[index]
            )
            row[f"{prefix}_kunwei_guard_wrench_tcp_si_{index}"] = (
                ""
                if lineage is None
                else lineage.kunwei_guard_wrench_tcp_si[index]
            )
            row[f"{prefix}ed_raw_f_ff_{index}"] = (
                "" if lineage is None else lineage.commanded_raw_f_ff[index]
            )
        row[f"{prefix}_lease"] = "" if lineage is None else lineage.lease
        row[f"{prefix}_episode"] = "" if lineage is None else lineage.episode
        row[f"{prefix}_model_mode"] = "" if lineage is None else lineage.model_mode
        row[f"{prefix}_frame_token"] = "" if lineage is None else lineage.frame_token
        row[f"{prefix}_kunwei_sample_index"] = (
            "" if lineage is None else lineage.kunwei_sample_index
        )
        row[f"{prefix}_kunwei_receive_batch_id"] = (
            "" if lineage is None else lineage.kunwei_receive_batch_id
        )
        row[f"{prefix}_kunwei_nominal_sensor_time_s"] = (
            ""
            if lineage is None or lineage.kunwei_nominal_sensor_time_s is None
            else lineage.kunwei_nominal_sensor_time_s
        )
        row[f"{prefix}_kunwei_batch_arrival_monotonic_s"] = (
            ""
            if lineage is None
            or lineage.kunwei_batch_arrival_monotonic_s is None
            else lineage.kunwei_batch_arrival_monotonic_s
        )
    for name in (
        "actual_TCP_pose",
        "actual_TCP_speed",
        "actual_q",
        "actual_qd",
        "target_moment",
        "target_current",
        "actual_current",
        "actual_current_as_torque",
        "joint_control_output",
    ):
        for index, value in enumerate(sample[name]):
            row[f"{name}_{index}"] = float(value)
    for index, value in enumerate(sample["joint_mode"]):
        row[f"joint_mode_{index}"] = int(value)
    for index in range(6):
        row[f"applied_f_ff_{index}"] = float(
            sample[f"output_double_register_{26 + index}"]
        )
        row[f"applied_k_{index}"] = float(
            sample[f"output_double_register_{32 + index}"]
        )
        row[f"commanded_joint_torque_nm_{index}"] = float(
            sample[f"output_double_register_{38 + index}"]
        )
    return row


def _row_float(row: Mapping[str, Any], key: str, default: float = 0.0) -> float:
    value = row.get(key, default)
    if value in (None, ""):
        return float(default)
    try:
        value = float(value)
    except (TypeError, ValueError):
        return float(default)
    return value if math.isfinite(value) else float(default)


def _recorder_frame(
    row: Mapping[str, Any],
    *,
    sample_index: int,
    control_sequence: int,
    acked: CommandLineage | None,
    outgoing: CommandLineage,
    recorder_start_s: float,
    observation_history: RecorderObservationHistory,
) -> EpisodeFrameV2:
    """Translate one retained RTDE row into the offline recorder contract.

    This seam deliberately records the host command and controller echo from
    the existing register block.  The current runner does not expose the
    previous no-gravity command/Jacobian/dynamics needed for a valid internal
    wrench, so that flag remains false and the EligibilityValidator downgrades
    every current live shadow row.
    """

    external = tuple(
        _row_float(row, f"kunwei_guard_wrench_tcp_si_{index}") for index in range(6)
    )
    actual_pose = tuple(_row_float(row, f"actual_TCP_pose_{index}") for index in range(6))
    desired_pose = tuple(
        _row_float(row, f"command_desired_pose_{index}") for index in range(6)
    )
    tracking_error = tuple(desired_pose[index] - actual_pose[index] for index in range(6))
    # Exact v3 slice order: external, internal, EE twist, desired pose, desired
    # twist, desired acceleration, tracking error.  The 84D contract is
    # canonical current+previous; the first retained row has no causal history
    # and is therefore explicitly invalid rather than silently training on it.
    current_observation_slice = (
        external
        + (0.0,) * 6
        + tuple(_row_float(row, f"actual_TCP_speed_{index}") for index in range(6))
        + desired_pose
        + (0.0,) * 6
        + (0.0,) * 6
        + tracking_error
    )
    observation, history_valid = observation_history.compose(
        current_observation_slice
    )
    host_command = (
        (0.0,) * 12
        if acked is None
        else acked.commanded_raw_f_ff + acked.commanded_k
    )
    controller_echo = tuple(
        _row_float(row, f"applied_f_ff_{index}") for index in range(6)
    ) + tuple(_row_float(row, f"applied_k_{index}") for index in range(6))
    coherent = bool(row.get("action_echo_coherent", False))
    external_device_time = (
        None
        if acked is None or acked.kunwei_nominal_sensor_time_s is None
        else float(acked.kunwei_nominal_sensor_time_s)
    )
    control_time = recorder_start_s + _row_float(row, "host_elapsed_s")
    external_host_visible_time = (
        None
        if acked is None or acked.kunwei_batch_arrival_monotonic_s is None
        else float(acked.kunwei_batch_arrival_monotonic_s)
    )
    external_batch_id = None if acked is None else acked.kunwei_receive_batch_id
    external_sample_index = None if acked is None else acked.kunwei_sample_index
    lineage_present = (
        acked is not None
        and external_device_time is not None
        and external_host_visible_time is not None
        and external_batch_id is not None
        and external_sample_index is not None
        and external_host_visible_time <= control_time + 1e-12
    )
    ack_sequence = int(row.get("ack_sequence", 0))
    action_age_ticks = max(0, outgoing.command_sequence - ack_sequence)
    receiver_state = int(row.get("receiver_state", STATE_WAITING))
    echo_expected = receiver_state in {STATE_STARTUP, STATE_TORQUE}
    return EpisodeFrameV2(
        episode_id=str(outgoing.episode),
        sample_index=sample_index,
        control_sequence=control_sequence,
        control_time_s=control_time,
        controller_time_s=max(0.0, _row_float(row, "controller_timestamp_s")),
        control_clock="host_monotonic",
        observation_84d=observation,
        expert_action_12d=host_command,
        applied_action_12d=host_command,
        echoed_action_12d=controller_echo,
        action_generation=max(
            0,
            int(row.get("action_publish_generation_end", 0) or 0),
        ),
        action_age_ticks=action_age_ticks,
        action_echo_coherent=coherent,
        echoed_action_valid=coherent,
        external_device_time_s=external_device_time,
        external_host_visible_time_s=external_host_visible_time,
        external_batch_id=external_batch_id,
        external_sample_index=external_sample_index,
        external_hold=False,
        external_held_ticks=0,
        device_age_samples=0,
        host_age_s=(
            None
            if external_host_visible_time is None
            or external_host_visible_time > control_time
            else control_time - external_host_visible_time
        ),
        internal_wrench_valid=False,
        external_lineage_valid=lineage_present,
        source_row_torn=echo_expected and not coherent,
        source_row_invalid=(
            acked is None
            or not echo_expected
            or not lineage_present
            or not history_valid
        ),
        recorder_valid=True,
    )


def _sample_safety_errors(
    sample: Mapping[str, Any],
    *,
    lease_id: int,
    episode_identity: int,
    bundle: ValidatedBundle,
    desired_pose: Sequence[float],
    tube_anchor_pose: Sequence[float],
) -> list[str]:
    errors: list[str] = []
    if int(sample["robot_mode"]) != ROBOT_MODE_RUNNING or int(
        sample["safety_mode"]
    ) != SAFETY_MODE_NORMAL:
        errors.append("runtime_safety_changed")
    if int(sample["output_int_register_32"]) != LIVE_PROTOCOL_TOKEN:
        errors.append("receiver_protocol_identity_changed")
    state = int(sample["output_int_register_24"])
    fault = int(sample["output_int_register_26"])
    if (
        state not in {STATE_FAULT, STATE_COMPLETE}
        and int(sample["runtime_state"]) != RUNTIME_PLAYING
    ):
        errors.append("receiver_runtime_stopped_before_terminal_state")
    if fault != 0 or state == STATE_FAULT:
        errors.append(f"receiver_fault:{fault}")
    if state in {STATE_STARTUP, STATE_TORQUE}:
        if int(sample["output_int_register_27"]) != lease_id:
            errors.append("lease_echo_mismatch")
        if int(sample["output_int_register_31"]) != episode_identity:
            errors.append("episode_echo_mismatch")
        if int(sample["output_int_register_30"]) != WRENCH_FRAME_TOKEN:
            errors.append("frame_echo_mismatch")
    try:
        episode_tube = bundle.tube.rebased(tube_anchor_pose)
        episode_tube.assert_contains_pose(sample["actual_TCP_pose"], role="actual")
    except RuntimeError as exc:
        errors.append(str(exc))
    try:
        episode_tube.assert_contains_pose(desired_pose, role="desired")
    except RuntimeError as exc:
        errors.append(str(exc))
    return errors


def _append_output_batch(
    batch: Sequence[Mapping[str, Any]],
    *,
    start: float,
    outgoing: CommandPacket,
    lineages: Mapping[int, CommandLineage],
    lease_id: int,
    episode_identity: int,
    bundle: ValidatedBundle,
    samples: list[dict[str, Any]],
    timeline: ReferenceTimeline | CanaryTimeline | None = None,
    recorder: EpisodeRecorder | None = None,
    recorder_start_s: float | None = None,
    recorder_observation_history: RecorderObservationHistory | None = None,
) -> tuple[Mapping[str, Any], bool, list[str]]:
    if not batch:
        raise RuntimeError("empty_output_batch")
    errors: list[str] = []
    observed_torque = False
    for sample in batch:
        elapsed = time.monotonic() - start
        ack_sequence = int(sample["output_int_register_25"])
        acked = lineages.get(ack_sequence)
        active_timeline = bundle.timeline if timeline is None else timeline
        desired_row = active_timeline.row_at(elapsed)
        errors.extend(
            _sample_safety_errors(
                sample,
                lease_id=lease_id,
                episode_identity=episode_identity,
                bundle=bundle,
                desired_pose=desired_row["desired_pose_base"],
                tube_anchor_pose=active_timeline.rows[0]["desired_pose_base"],
            )
        )
        samples.append(
            _output_row(sample, elapsed, outgoing=outgoing.lineage, acked=acked)
        )
        if recorder is not None:
            if recorder_start_s is None:
                raise RuntimeError("recorder_start_clock_missing")
            if recorder_observation_history is None:
                raise RuntimeError("recorder_observation_history_missing")
            recorder_frame = _recorder_frame(
                samples[-1],
                sample_index=len(samples) - 1,
                control_sequence=len(samples),
                acked=acked,
                outgoing=outgoing.lineage,
                recorder_start_s=recorder_start_s,
                observation_history=recorder_observation_history,
            )
            if not recorder.enqueue(recorder_frame):
                errors.append("recorder_enqueue_failed")
        if acked is None:
            errors.append(f"ack_command_lineage_missing:{ack_sequence}")
        observed_torque = observed_torque or int(
            sample["output_int_register_24"]
        ) in {STATE_STARTUP, STATE_TORQUE}
    return batch[-1], observed_torque, errors


def _write_csv(path: Path, rows: Sequence[Mapping[str, Any]]) -> None:
    if not rows:
        raise RuntimeError("no_live_samples_collected")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def _write_json_new(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True, allow_nan=False)
        handle.write("\n")


def _compile_probe_row(
    sample: Mapping[str, Any],
    *,
    host_elapsed_s: float,
) -> dict[str, Any]:
    row: dict[str, Any] = {
        "host_elapsed_s": host_elapsed_s,
        "controller_timestamp_s": float(sample["timestamp"]),
        "runtime_state": int(sample["runtime_state"]),
        "robot_mode": int(sample["robot_mode"]),
        "safety_mode": int(sample["safety_mode"]),
        "probe_state": int(sample["output_int_register_24"]),
        "probe_tick": int(sample["output_int_register_25"]),
        "probe_fault": int(sample["output_int_register_26"]),
        "probe_protocol": int(sample["output_int_register_32"]),
    }
    for name in (
        "actual_TCP_pose",
        "actual_TCP_speed",
        "actual_q",
        "actual_qd",
        "target_moment",
        "target_current",
        "actual_current",
        "actual_current_as_torque",
        "joint_control_output",
    ):
        for index, value in enumerate(sample[name]):
            row[f"{name}_{index}"] = float(value)
    for index, value in enumerate(sample["joint_mode"]):
        row[f"joint_mode_{index}"] = int(value)
    return row


def _update_compile_probe_markers(
    observed_active: bool,
    observed_complete: bool,
    sample: Mapping[str, Any],
) -> tuple[bool, bool]:
    """Reject a persistent COMPLETE register until this run has been ACTIVE."""

    token = int(sample["output_int_register_32"])
    state = int(sample["output_int_register_24"])
    if token != COMPILE_PROBE_PROTOCOL_TOKEN:
        return observed_active, observed_complete
    if (
        state == COMPILE_PROBE_STATE_ACTIVE
        and int(sample["runtime_state"]) == RUNTIME_PLAYING
    ):
        return True, observed_complete
    if observed_active and state == COMPILE_PROBE_STATE_COMPLETE:
        return observed_active, True
    return observed_active, observed_complete


def _update_receiver_handshake_markers(
    observed_waiting: bool,
    observed_direct_torque: bool,
    sample: Mapping[str, Any],
) -> tuple[bool, bool]:
    if int(sample["output_int_register_32"]) != LIVE_PROTOCOL_TOKEN:
        return observed_waiting, observed_direct_torque
    state = int(sample["output_int_register_24"])
    if state in (STATE_STARTUP, STATE_TORQUE):
        observed_direct_torque = True
    if state == STATE_WAITING and int(sample["runtime_state"]) == RUNTIME_PLAYING:
        observed_waiting = True
    return observed_waiting, observed_direct_torque


def run_compile_probe(args: argparse.Namespace) -> dict[str, Any]:
    if not (args.live and args.send_urscript and args.no_motion):
        raise RuntimeError("compile_probe_live_send_and_no_motion_gates_required")
    source = build_compile_probe_source()
    parse_compile_probe_source(source)
    source_sha256 = hashlib.sha256(source.encode("utf-8")).hexdigest()
    status = readonly_status(args.robot_host)
    validate_compile_probe_preflight(status)
    output_dir = _next_available_run_dir(args.output_dir.resolve())
    rows: list[dict[str, Any]] = []
    failure: str | None = None
    observed_active = False
    observed_complete = False
    with _live_writer_lease():
        _enforce_no_live_writer_conflict()
        start = time.monotonic()
        with LiveRTDE(args.robot_host, timeout=args.connect_timeout_s) as rtde:
            rtde.negotiate()
            output_recipe, output_types = rtde.setup_outputs(500.0, OUTPUT_FIELDS)
            rtde.start()
            try:
                _send_urscript(args.robot_host, source, args.connect_timeout_s)
                deadline = time.monotonic() + args.probe_timeout_s
                while time.monotonic() < deadline:
                    batch = _receive_available(
                        rtde,
                        output_recipe,
                        output_types,
                        OUTPUT_FIELDS,
                        0.01,
                    )
                    if not batch:
                        continue
                    for sample in batch:
                        rows.append(
                            _compile_probe_row(
                                sample,
                                host_elapsed_s=time.monotonic() - start,
                            )
                        )
                        observed_active, observed_complete = (
                            _update_compile_probe_markers(
                                observed_active,
                                observed_complete,
                                sample,
                            )
                        )
                    if observed_active and observed_complete:
                        break
                if not observed_active:
                    raise RuntimeError("compile_probe_active_marker_missing")
                if not observed_complete:
                    raise RuntimeError("compile_probe_complete_marker_missing")
            except Exception as exc:
                failure = f"{type(exc).__name__}: {exc}"
                raise
            finally:
                csv_path = output_dir / "compile_probe_rtde.csv"
                evidence_path = output_dir / "evidence.json"
                if rows:
                    _write_csv(csv_path, rows)
                baseline_pose = tuple(
                    float(value) for value in status["rtde"]["actual_TCP_pose"]
                )
                maximum_translation_m = max(
                    (
                        _sample_translation_error_sqm3(
                            [row[f"actual_TCP_pose_{axis}"] for axis in range(6)],
                            baseline_pose,
                        )
                        for row in rows
                    ),
                    default=0.0,
                )
                maximum_speed_m_s = max(
                    (
                        math.sqrt(
                            sum(
                                float(row[f"actual_TCP_speed_{axis}"]) ** 2
                                for axis in range(3)
                            )
                        )
                        for row in rows
                    ),
                    default=0.0,
                )
                maximum_tcp_angular_speed_rad_s = max(
                    (
                        math.sqrt(
                            sum(
                                float(row[f"actual_TCP_speed_{axis}"]) ** 2
                                for axis in range(3, 6)
                            )
                        )
                        for row in rows
                    ),
                    default=0.0,
                )
                maximum_joint_speed_rad_s = max(
                    (
                        max(
                            abs(float(row[f"actual_qd_{axis}"]))
                            for axis in range(6)
                        )
                        for row in rows
                    ),
                    default=0.0,
                )
                stationarity_window = _stationarity_window_evidence(rows)
                active_rows = sum(
                    row["probe_protocol"] == COMPILE_PROBE_PROTOCOL_TOKEN
                    and row["probe_state"] == COMPILE_PROBE_STATE_ACTIVE
                    for row in rows
                )
                strict_gate = {
                    "source_contains_no_torque_or_motion_api": True,
                    "observed_active_runtime_marker": observed_active,
                    "observed_complete_marker": observed_complete,
                    "at_least_45_active_500hz_rows": active_rows >= 45,
                    "bounded_stationarity_window": stationarity_window["ok"],
                }
                strict_gate["ok"] = all(strict_gate.values()) and failure is None
                evidence = {
                    "schema": COMPILE_PROBE_EVIDENCE_SCHEMA,
                    "claim_class": "live_controller_compile_probe_no_motion",
                    "ok": strict_gate["ok"],
                    "failure": failure,
                    "robot_host": args.robot_host,
                    "compile_probe_source_sha256": source_sha256,
                    "sample_count": len(rows),
                    "active_500hz_rows": active_rows,
                    "maximum_tcp_translation_m": maximum_translation_m,
                    "maximum_tcp_speed_m_s": maximum_speed_m_s,
                    "maximum_tcp_angular_speed_rad_s": (
                        maximum_tcp_angular_speed_rad_s
                    ),
                    "maximum_joint_speed_rad_s": maximum_joint_speed_rad_s,
                    "stationarity_window": stationarity_window,
                    "observed_active_marker": observed_active,
                    "observed_complete_marker": observed_complete,
                    "strict_success_gate": strict_gate,
                    "motion_performed": False,
                    "direct_torque_called": False,
                    "rtde_inputs_written": False,
                    "kunwei_stream_started": False,
                    "training_dataset": False,
                    "data_csv": str(csv_path) if rows else None,
                }
                _write_json_new(evidence_path, evidence)
    return json.loads((output_dir / "evidence.json").read_text())


def build_receiver_handshake_probe_source(receiver_source: str) -> str:
    """Inject one self-terminating WAITING window before the full main loop."""

    parse_live_receiver_source(receiver_source)
    main_loop = "  while running:\n"
    if receiver_source.count(main_loop) != 1:
        raise ValueError("receiver handshake main loop is not uniquely parseable")
    probe_prefix = """  local receiver_handshake_probe_tick = 0
  while receiver_handshake_probe_tick < 50:
    sync()
    receiver_handshake_probe_tick = receiver_handshake_probe_tick + 1
  end
  running = False
"""
    source = receiver_source.replace(
        main_loop,
        probe_prefix + main_loop,
        1,
    )
    if source.replace(probe_prefix, "", 1) != receiver_source:
        raise RuntimeError("receiver handshake source derivation mismatch")
    parse_live_receiver_source(source)
    return source


def run_receiver_handshake_probe(
    args: argparse.Namespace,
    bundle: ValidatedBundle,
) -> dict[str, Any]:
    """Compile the full receiver and execute only a bounded WAITING prefix."""

    if not (args.live and args.send_urscript and args.no_motion):
        raise RuntimeError(
            "receiver_handshake_live_send_and_no_motion_gates_required"
        )
    if not (
        math.isfinite(args.handshake_timeout_s)
        and 0.2 <= args.handshake_timeout_s <= 0.8
    ):
        raise ValueError("receiver_handshake_timeout_must_be_0_2_to_0_8s")
    probe_source = build_receiver_handshake_probe_source(bundle.source)
    probe_source_sha256 = hashlib.sha256(
        probe_source.encode("utf-8")
    ).hexdigest()
    status = readonly_status(args.robot_host)
    validate_compile_probe_preflight(status)
    output_dir = _next_available_run_dir(args.output_dir.resolve())
    rows: list[dict[str, Any]] = []
    observed_waiting = False
    observed_direct_torque = False
    observed_complete = False
    probe_source_sent = False
    primary_start_barrier: dict[str, Any] | None = None
    failure: str | None = None
    start = time.monotonic()
    with _live_writer_lease():
        _enforce_no_live_writer_conflict()
        with LiveRTDE(args.robot_host, timeout=args.connect_timeout_s) as rtde:
            rtde.negotiate()
            output_recipe, output_types = rtde.setup_outputs(
                500.0, OUTPUT_FIELDS
            )
            rtde.start()
            try:
                primary_start_barrier = _send_urscript_with_primary_start_barrier(
                    args.robot_host,
                    probe_source,
                    timeout_s=args.connect_timeout_s,
                )
                probe_source_sent = True
                deadline = time.monotonic() + args.handshake_timeout_s
                while time.monotonic() < deadline:
                    batch = _receive_available(
                        rtde,
                        output_recipe,
                        output_types,
                        OUTPUT_FIELDS,
                        0.01,
                    )
                    for sample in batch:
                        rows.append(
                            _compile_probe_row(
                                sample,
                                host_elapsed_s=time.monotonic() - start,
                            )
                        )
                        observed_waiting, observed_direct_torque = (
                            _update_receiver_handshake_markers(
                                observed_waiting,
                                observed_direct_torque,
                                sample,
                            )
                        )
                        if (
                            observed_waiting
                            and int(sample["output_int_register_32"])
                            == LIVE_PROTOCOL_TOKEN
                            and int(sample["output_int_register_24"])
                            == STATE_COMPLETE
                        ):
                            observed_complete = True
                    if observed_complete or observed_direct_torque:
                        break
                if observed_direct_torque:
                    raise RuntimeError(
                        "receiver_handshake_observed_direct_torque_state"
                    )
                if not observed_waiting:
                    raise RuntimeError(
                        "receiver_handshake_waiting_marker_missing"
                    )
                if not observed_complete:
                    raise RuntimeError(
                        "receiver_handshake_complete_marker_missing"
                    )
            except Exception as exc:
                failure = f"{type(exc).__name__}: {exc}"

    baseline_pose = tuple(
        float(value) for value in status["rtde"]["actual_TCP_pose"]
    )
    maximum_translation_m = max(
        (
            _sample_translation_error_sqm3(
                [row[f"actual_TCP_pose_{axis}"] for axis in range(6)],
                baseline_pose,
            )
            for row in rows
        ),
        default=0.0,
    )
    maximum_tcp_speed_m_s = max(
        (
            math.sqrt(
                sum(
                    float(row[f"actual_TCP_speed_{axis}"]) ** 2
                    for axis in range(3)
                )
            )
            for row in rows
        ),
        default=0.0,
    )
    maximum_tcp_angular_speed_rad_s = max(
        (
            math.sqrt(
                sum(
                    float(row[f"actual_TCP_speed_{axis}"]) ** 2
                    for axis in range(3, 6)
                )
            )
            for row in rows
        ),
        default=0.0,
    )
    maximum_joint_speed_rad_s = max(
        (
            max(
                abs(float(row[f"actual_qd_{axis}"]))
                for axis in range(6)
            )
            for row in rows
        ),
        default=0.0,
    )
    stationarity_window = _stationarity_window_evidence(rows)
    strict_gate = {
        "full_receiver_source_compiled_with_bounded_prefix": probe_source_sent,
        "observed_waiting_runtime_marker": observed_waiting,
        "observed_direct_torque_state": observed_direct_torque is False,
        "observed_complete_marker": observed_complete,
        "bounded_stationarity_window": stationarity_window["ok"],
    }
    strict_gate["ok"] = (
        all(strict_gate.values())
        and failure is None
    )
    csv_path = output_dir / "receiver_handshake_rtde.csv"
    evidence_path = output_dir / "evidence.json"
    if rows:
        _write_csv(csv_path, rows)
    evidence = {
        "schema": RECEIVER_HANDSHAKE_EVIDENCE_SCHEMA,
        "claim_class": "live_full_receiver_handshake_no_motion",
        "ok": bool(strict_gate["ok"]),
        "failure": failure,
        "robot_host": args.robot_host,
        "receiver_source_sha256": bundle.source_sha256,
        "bundle_manifest_sha256": bundle.manifest_sha256,
        "friction_profile": bundle.friction_profile,
        "viscous_scale": list(bundle.viscous_scale),
        "coulomb_scale": list(bundle.coulomb_scale),
        "receiver_handshake_probe_source_sha256": probe_source_sha256,
        "receiver_handshake_source_derivation": (
            "exact_bundle_receiver_plus_single_50tick_pre_main_early_exit"
        ),
        "primary_start_barrier": primary_start_barrier,
        "sample_count": len(rows),
        "duration_s": time.monotonic() - start,
        "observed_waiting_marker": observed_waiting,
        "observed_direct_torque_state": observed_direct_torque,
        "observed_complete_marker": observed_complete,
        "maximum_tcp_translation_m": maximum_translation_m,
        "maximum_tcp_speed_m_s": maximum_tcp_speed_m_s,
        "maximum_tcp_angular_speed_rad_s": maximum_tcp_angular_speed_rad_s,
        "maximum_joint_speed_rad_s": maximum_joint_speed_rad_s,
        "stationarity_window": stationarity_window,
        "strict_success_gate": strict_gate,
        "motion_performed": False,
        "direct_torque_called": False,
        "rtde_inputs_written": False,
        "run_packet_sent": False,
        "kunwei_stream_started": False,
        "training_dataset": False,
        "data_csv": str(csv_path) if rows else None,
    }
    _write_json_new(evidence_path, evidence)
    if not evidence["ok"]:
        detail = failure or "strict_gate_failed"
        raise RuntimeError(f"receiver_handshake_probe_failed:{detail}")
    return evidence


@contextmanager
def _live_writer_lease():
    profile = ResourceProfile.from_env()
    try:
        lease_context = writer_lease(profile, LIVE_WRITER_TASK, blocking=False)
        lease_context.__enter__()
    except BlockingIOError as exc:
        raise RuntimeError("live_writer_lock_unavailable") from exc
    try:
        yield
    except BaseException:
        lease_context.__exit__(*sys.exc_info())
        raise
    else:
        lease_context.__exit__(None, None, None)


def run_live(args: argparse.Namespace, bundle: ValidatedBundle) -> dict[str, Any]:
    if not (
        args.live
        and args.send_urscript
        and args.write_rtde_inputs
        and args.allow_direct_torque
        and args.allow_motion
        and args.no_contact
        and args.allow_kunwei_stream_command
    ):
        raise RuntimeError("all_independent_live_cli_gates_are_required")
    with _live_writer_lease():
        return _run_live_locked(args, bundle)


def _run_live_locked(args: argparse.Namespace, bundle: ValidatedBundle) -> dict[str, Any]:
    canary_stage = str(args.canary_stage)
    runtime_binding = runtime_source_binding()
    scheduler_runtime = require_v35_sched_other()
    sensor_delivery_watchdog_s = float(
        getattr(
            args,
            "sensor_delivery_watchdog_s",
            DEFAULT_KUNWEI_DELIVERY_WATCHDOG_S,
        )
    )
    if canary_stage not in CANARY_STAGE_ORDER:
        raise ValueError(f"unknown_canary_stage:{canary_stage}")
    compile_probe_evidence = validate_compile_probe_evidence(
        args.compile_probe_evidence.resolve(),
        robot_host=args.robot_host,
    )
    required_prior_stage = CANARY_STAGE_REQUIRED_PRIOR[canary_stage]
    prior_stage_evidence = validate_prior_stage_evidence(
        (
            None
            if args.prior_stage_evidence is None
            else args.prior_stage_evidence.resolve()
        ),
        required_stage=required_prior_stage,
        bundle=bundle,
        robot_host=args.robot_host,
    )
    calibration, calibration_sha256 = validate_calibration(
        args.kunwei_calibration.resolve()
    )
    status = readonly_status(args.robot_host)
    timeline = CanaryTimeline.from_stage(
        canary_stage,
        actual_pose=status["rtde"]["actual_TCP_pose"],
        bundle=bundle,
    )
    validate_live_preflight(status, bundle, timeline=timeline)
    _enforce_no_live_writer_conflict()
    lease_id, episode_identity = _new_live_identity_pair()
    initial_pose = timeline.rows[0]["desired_pose_base"]
    scheduler = AckPacedScheduler()
    samples: list[dict[str, Any]] = []
    sent_sequences = 0
    receiver_send_attempts = 0
    primary_start_barriers: list[dict[str, Any]] = []
    observed_torque = False
    torque_start_elapsed_s: float | None = None
    complete = False
    start: float | None = None
    initial_command = _command_packet(
        command=MODE_IDLE,
        sequence=0,
        progress_s=0.0,
        pose=initial_pose,
        lease_id=lease_id,
        episode_identity=episode_identity,
    )
    lineages: dict[int, CommandLineage] = {0: initial_command.lineage}
    outgoing = initial_command
    failure: str | None = None
    output_dir = _next_available_run_dir(args.output_dir.resolve())
    recorder: EpisodeRecorder | None = None
    task_executor = TaskExecutor()
    recorder_observation_history: RecorderObservationHistory | None = None
    recorder_manifest: dict[str, Any] | None = None
    recorder_eligibility: EligibilityDecision | None = None
    recorder_receipt_path: Path | None = None
    recorder_finalize_error: str | None = None
    recorder_output = getattr(args, "recorder_output", None)
    if recorder_output is not None:
        recorder_root = Path(recorder_output).resolve()
        recorder = EpisodeRecorder(
            recorder_root,
            episode_id=str(episode_identity),
            metadata={
                "claim_class": "offline_recorder_sidecar_live_shadow_only",
                "first_live_shadow_training_eligible": False,
                "motion_enabled_by_recorder": False,
                "durability_mode": "batch_fsync_10",
            },
        )
        task_executor = TaskExecutor(recorder)
        recorder_observation_history = RecorderObservationHistory()
    with (
        KunweiGuardCapture(
            sensor_ip=args.sensor_ip,
            sensor_port=args.sensor_port,
            connect_timeout_s=args.connect_timeout_s,
            output_dir=output_dir,
            calibration=calibration,
            delivery_watchdog_s=sensor_delivery_watchdog_s,
        ) as kunwei,
        LiveRTDE(args.robot_host, timeout=args.connect_timeout_s) as rtde,
    ):
        if recorder is not None:
            recorder.start()
        kunwei.wait_preflight()
        rtde.negotiate()
        output_recipe, output_types = rtde.setup_outputs(500.0, OUTPUT_FIELDS)
        input_recipe, input_types = rtde.setup_inputs(INPUT_FIELDS)
        rtde.start()
        _prime_idle_inputs(
            rtde,
            input_recipe,
            input_types,
            outgoing.values,
            output_recipe,
            output_types,
            OUTPUT_FIELDS,
        )
        handshake_samples: list[dict[str, Any]] = []
        handshake_diagnostic_samples: list[dict[str, Any]] = []
        try:
            while True:
                receiver_send_attempts += 1
                primary_start_barriers.append(
                    _send_urscript_with_primary_start_barrier(
                        args.robot_host,
                        bundle.source,
                        timeout_s=args.connect_timeout_s,
                    )
                )
                try:
                    start, sample = _wait_for_fresh_receiver_waiting(
                        rtde,
                        output_recipe,
                        output_types,
                        OUTPUT_FIELDS,
                        receiver_wait_s=args.receiver_wait_s,
                        lease_id=lease_id,
                        episode_identity=episode_identity,
                        samples_out=handshake_samples,
                        diagnostic_samples_out=handshake_diagnostic_samples,
                    )
                    break
                except RuntimeError:
                    if not (
                        receiver_send_attempts == 1
                        and _stale_terminal_receiver_allows_one_resend(
                            handshake_diagnostic_samples,
                            lease_id=lease_id,
                            episode_identity=episode_identity,
                        )
                    ):
                        raise
                    _prime_idle_inputs(
                        rtde,
                        input_recipe,
                        input_types,
                        outgoing.values,
                        output_recipe,
                        output_types,
                        OUTPUT_FIELDS,
                    )
            scheduler.arm(start)
            pending = handshake_samples or [dict(sample)]
            while True:
                # Recorder health is polled here at host task cadence.  A
                # latched fault enters the existing exception -> MODE_ABORT ->
                # receiver safe-exit ordering; it is never read by the
                # controller's 500 Hz torque thread.
                task_executor.poll()
                guard_snapshot = kunwei.snapshot(
                    max_age_s=sensor_delivery_watchdog_s
                )
                if not pending:
                    pending = _receive_available(
                        rtde,
                        output_recipe,
                        output_types,
                        OUTPUT_FIELDS,
                        0.01,
                    )
                    if not pending:
                        raise RuntimeError("rtde_output_stale")

                last_sample, batch_torque, errors = _append_output_batch(
                    pending,
                    start=start,
                    outgoing=outgoing,
                    lineages=lineages,
                    lease_id=lease_id,
                    episode_identity=episode_identity,
                    bundle=bundle,
                    samples=samples,
                    timeline=timeline,
                    recorder=recorder,
                    recorder_start_s=start,
                    recorder_observation_history=recorder_observation_history,
                )
                task_executor.poll()
                observed_torque = observed_torque or batch_torque
                if errors:
                    raise RuntimeError("output_batch_safety_failed:" + ";".join(errors))

                elapsed = time.monotonic() - start
                if batch_torque and torque_start_elapsed_s is None:
                    torque_start_elapsed_s = elapsed
                active_elapsed_s = (
                    0.0
                    if torque_start_elapsed_s is None
                    else max(0.0, elapsed - torque_start_elapsed_s)
                )
                if active_elapsed_s >= timeline.duration_s and observed_torque:
                    end_row = timeline.row_at(active_elapsed_s)
                    end_command = _command_packet(
                        command=MODE_END,
                        sequence=outgoing.lineage.command_sequence,
                        progress_s=float(end_row["progress_s"]),
                        pose=end_row["desired_pose_base"],
                        lease_id=lease_id,
                        episode_identity=episode_identity,
                        kunwei_guard_wrench_tcp_si=guard_snapshot.wrench_tcp_si,
                        kunwei_sample_index=guard_snapshot.sample_index,
                        kunwei_receive_batch_id=(
                            guard_snapshot.receive_batch_id
                        ),
                        kunwei_nominal_sensor_time_s=(
                            guard_snapshot.nominal_sensor_time_s
                        ),
                        kunwei_batch_arrival_monotonic_s=(
                            guard_snapshot.t_monotonic_s
                        ),
                    )
                    rtde.send_inputs(input_recipe, input_types, end_command.values)
                    outgoing = end_command
                    end_deadline = time.monotonic() + 1.0
                    while time.monotonic() < end_deadline:
                        task_executor.poll()
                        kunwei.snapshot(
                            max_age_s=sensor_delivery_watchdog_s
                        )
                        final_batch = _receive_available(
                            rtde,
                            output_recipe,
                            output_types,
                            OUTPUT_FIELDS,
                            0.01,
                        )
                        if not final_batch:
                            continue
                        _, batch_torque, errors = _append_output_batch(
                            final_batch,
                            start=start,
                            outgoing=outgoing,
                            lineages=lineages,
                            lease_id=lease_id,
                            episode_identity=episode_identity,
                            bundle=bundle,
                            samples=samples,
                            timeline=timeline,
                            recorder=recorder,
                            recorder_start_s=start,
                            recorder_observation_history=recorder_observation_history,
                        )
                        task_executor.poll()
                        observed_torque = observed_torque or batch_torque
                        if errors:
                            raise RuntimeError(
                                "completion_batch_safety_failed:" + ";".join(errors)
                            )
                        if any(
                            int(final["output_int_register_24"]) == STATE_COMPLETE
                            for final in final_batch
                        ):
                            complete = True
                            break
                    if not complete:
                        raise RuntimeError("receiver_completion_timeout")
                    break
                if scheduler.release_due(start + elapsed):
                    next_sequence = scheduler.next_sequence(
                        int(last_sample["output_int_register_25"])
                    )
                    if next_sequence is not None:
                        command_row = timeline.row_at(active_elapsed_s)
                        next_command = _command_packet(
                            command=MODE_RUN,
                            sequence=next_sequence,
                            progress_s=float(command_row["progress_s"]),
                            pose=command_row["desired_pose_base"],
                            lease_id=lease_id,
                            episode_identity=episode_identity,
                            kunwei_guard_wrench_tcp_si=(
                                guard_snapshot.wrench_tcp_si
                            ),
                            kunwei_sample_index=guard_snapshot.sample_index,
                            kunwei_receive_batch_id=(
                                guard_snapshot.receive_batch_id
                            ),
                            kunwei_nominal_sensor_time_s=(
                                guard_snapshot.nominal_sensor_time_s
                            ),
                            kunwei_batch_arrival_monotonic_s=(
                                guard_snapshot.t_monotonic_s
                            ),
                        )
                        rtde.send_inputs(
                            input_recipe, input_types, next_command.values
                        )
                        _register_command_lineage(lineages, next_command)
                        outgoing = next_command
                        sent_sequences += 1
                pending = []
        except Exception as exc:
            failure = f"{type(exc).__name__}: {exc}"
            try:
                abort_command = _command_packet(
                    command=MODE_ABORT,
                    sequence=outgoing.lineage.command_sequence,
                    progress_s=outgoing.lineage.progress_s,
                    pose=outgoing.lineage.desired_pose,
                    lease_id=lease_id,
                    episode_identity=episode_identity,
                    kunwei_guard_wrench_tcp_si=outgoing.lineage.kunwei_guard_wrench_tcp_si,
                    kunwei_sample_index=outgoing.lineage.kunwei_sample_index,
                    kunwei_receive_batch_id=(
                        outgoing.lineage.kunwei_receive_batch_id
                    ),
                    kunwei_nominal_sensor_time_s=(
                        outgoing.lineage.kunwei_nominal_sensor_time_s
                    ),
                    kunwei_batch_arrival_monotonic_s=(
                        outgoing.lineage.kunwei_batch_arrival_monotonic_s
                    ),
                )
                rtde.send_inputs(input_recipe, input_types, abort_command.values)
                outgoing = abort_command
                abort_batch = _receive_available(
                    rtde, output_recipe, output_types, OUTPUT_FIELDS, 0.05
                )
                if abort_batch and start is not None:
                    _append_output_batch(
                        abort_batch,
                        start=start,
                        outgoing=outgoing,
                        lineages=lineages,
                        lease_id=lease_id,
                        episode_identity=episode_identity,
                        bundle=bundle,
                        samples=samples,
                        timeline=timeline,
                        recorder=recorder,
                        recorder_start_s=start,
                        recorder_observation_history=recorder_observation_history,
                    )
            except Exception:
                pass
            raise
        finally:
            kunwei.stop()
            kunwei_summary = kunwei.summary()
            if recorder is not None:
                try:
                    recorder_manifest = recorder.close(seal=True)
                except Exception as exc:
                    recorder_finalize_error = (
                        f"{type(exc).__name__}: {exc}"
                    )
                try:
                    validate_sealed_episode_manifest(
                        recorder.artifact_path,
                        recorder.manifest_path,
                    )
                    recorder_health = recorder.health()
                    recorder_receipt_path = (
                        recorder.output_dir / "training_eligibility.json"
                    )
                    _, recorder_frames = read_episode_artifact(
                        recorder.artifact_path
                    )
                    recorder_eligibility, _ = EligibilityValidator().evaluate_and_write(
                        recorder_receipt_path,
                        recorder_frames,
                        recorder_health=recorder_health,
                        first_live_shadow=True,
                        episode_id=str(episode_identity),
                    )
                except Exception as exc:
                    recorder_finalize_error = (
                        recorder_finalize_error
                        or f"{type(exc).__name__}: {exc}"
                    )
            csv_path = output_dir / "direct_torque_rtde.csv"
            evidence_path = output_dir / "evidence.json"
            handshake_diagnostics_path = (
                output_dir / "receiver_handshake_diagnostics.json"
            )
            if samples:
                _write_csv(csv_path, samples)
            handshake_summary = summarize_receiver_handshake_samples(
                handshake_diagnostic_samples,
                lease_id=lease_id,
                episode_identity=episode_identity,
            )
            if handshake_diagnostic_samples:
                _write_json_new(
                    handshake_diagnostics_path,
                    {
                        "schema": (
                            "ur10e_tacdiffusion_receiver_handshake_"
                            "diagnostics/v1"
                        ),
                        "receiver_source_sha256": bundle.source_sha256,
                        "lease_id": lease_id,
                        "episode_identity": episode_identity,
                        "summary": handshake_summary,
                        "rows": [
                            {
                                "controller_timestamp_s": sample.get(
                                    "timestamp"
                                ),
                                "runtime_state": sample.get("runtime_state"),
                                "robot_mode": sample.get("robot_mode"),
                                "safety_mode": sample.get("safety_mode"),
                                "receiver_state": sample.get(
                                    "output_int_register_24"
                                ),
                                "fault": sample.get(
                                    "output_int_register_26"
                                ),
                                "lease_echo": sample.get(
                                    "output_int_register_27"
                                ),
                                "episode_echo": sample.get(
                                    "output_int_register_31"
                                ),
                                "protocol_echo": sample.get(
                                    "output_int_register_32"
                                ),
                                "observed_command_echo": sample.get(
                                    "output_int_register_34"
                                ),
                                "episode_latched_echo": sample.get(
                                    "output_int_register_35"
                                ),
                            }
                            for sample in handshake_diagnostic_samples
                        ],
                    },
                )
            evidence = {
                "schema": CANARY_EVIDENCE_SCHEMA,
                "claim_class": "live_no_contact_direct_torque_canary",
                "ok": False,
                "failure": failure,
                "robot_host": args.robot_host,
                "receiver_source_sha256": bundle.source_sha256,
                "bundle_manifest_sha256": bundle.manifest_sha256,
                "friction_profile": bundle.friction_profile,
                "viscous_scale": list(bundle.viscous_scale),
                "coulomb_scale": list(bundle.coulomb_scale),
                "reference_artifact_sha256": bundle.reference_sha256,
                "runtime_source_sha256": runtime_binding["sha256"],
                "runtime_source_binding": runtime_binding,
                "canary_stage": canary_stage,
                "required_prior_stage": required_prior_stage,
                "compile_probe_evidence_sha256": _sha256(
                    args.compile_probe_evidence.resolve()
                ),
                "prior_stage_evidence_sha256": (
                    None
                    if args.prior_stage_evidence is None
                    else _sha256(args.prior_stage_evidence.resolve())
                ),
                "compile_probe_evidence_ok": bool(compile_probe_evidence["ok"]),
                "prior_stage_evidence_ok": (
                    None
                    if prior_stage_evidence is None
                    else bool(prior_stage_evidence["ok"])
                ),
                "lease_id": lease_id,
                "episode_identity": episode_identity,
                "duration_s": time.monotonic() - start if start is not None else 0.0,
                "active_direct_torque_duration_s": (
                    None
                    if torque_start_elapsed_s is None or start is None
                    else max(
                        0.0,
                        time.monotonic() - start - torque_start_elapsed_s,
                    )
                ),
                "stationary_entry_dwell_excluded_from_stage_clock": True,
                "primary_client_start_barriers": primary_start_barriers,
                "receiver_handshake_summary": handshake_summary,
                "receiver_send_attempts": receiver_send_attempts,
                "receiver_handshake_diagnostics": (
                    str(handshake_diagnostics_path)
                    if handshake_diagnostic_samples
                    else None
                ),
                "receiver_handshake_diagnostics_sha256": (
                    _sha256(handshake_diagnostics_path)
                    if handshake_diagnostic_samples
                    else None
                ),
                "sample_count": len(samples),
                "total_rows": len(samples),
                "sent_sequences": sent_sequences,
                "command_release_scheduler": scheduler.summary(),
                "runtime_scheduler": scheduler_runtime,
                "observed_direct_torque_state": observed_torque,
                "observed_complete_state": complete,
                "applied_action_echo_captured": False,
                "kunwei_stream_started": True,
                "kunwei_force_source": "kunwei_software_baselined_sensor_to_tcp_si",
                "kunwei_calibration_sha256": calibration_sha256,
                "kunwei": kunwei_summary,
                "program_start_mode": "remote_secondary_client",
                "transport_reuse_basis": (
                    "step5d_v34_rtde_and_absolute_deadline_primitives_with_"
                    "v35_sched_other_policy"
                ),
                "sensor_native_sample_rate_hz": 1000,
                "sensor_delivery_watchdog_s": sensor_delivery_watchdog_s,
                "causal_1khz_alignment_valid": False,
                "ur_internal_ft_used_for_guard": False,
                "ur_internal_ft_in_experiment_data": False,
                "contact_authorized": False,
                "training_dataset": False,
                "training_dataset_reason": (
                    "Ethernet TCP batches native 1 kHz frames; exact 1 ms "
                    "causal robot/force alignment is not validated."
                ),
                "recorder_enabled": recorder is not None,
                "recorder_durability_mode": (
                    "batch_fsync_10" if recorder is not None else None
                ),
                "recorder_manifest": (
                    None
                    if recorder_manifest is None
                    else str(recorder.manifest_path)
                ),
                "recorder_health_receipt": (
                    None if recorder is None else str(recorder.health_path)
                ),
                "recorder_health": (
                    None if recorder is None else recorder.health().as_json()
                ),
                "eligibility_receipt": (
                    None
                    if recorder_receipt_path is None
                    else str(recorder_receipt_path)
                ),
                "training_eligible": (
                    False
                    if recorder_eligibility is None
                    else recorder_eligibility.training_eligible
                ),
                "recorder_live_ready": False,
                "recorder_finalize_error": recorder_finalize_error,
                "data_csv": str(csv_path) if samples else None,
            }
            timestamps = [
                float(row["controller_timestamp_s"])
                for row in samples
                if row.get("controller_timestamp_s") is not None
            ]
            unique_timestamps = len(set(timestamps))
            duplicate_count = len(timestamps) - unique_timestamps
            nonmonotonic_count = sum(
                right < left for left, right in zip(timestamps, timestamps[1:])
            )
            if timestamps:
                controller_span = max(0.0, timestamps[-1] - timestamps[0])
            else:
                controller_span = 0.0
            expected_rows = controller_span * 500.0 + (1.0 if timestamps else 0.0)
            achieved_rate = (
                (len(timestamps) - 1) / controller_span
                if controller_span > 0.0 and len(timestamps) > 1
                else 0.0
            )
            lineage_misses = sum(
                int(row["ack_command_lineage_missing"]) for row in samples
            )
            active_action_rows = [
                row
                for row in samples
                if int(float(row["receiver_state"]))
                in (STATE_STARTUP, STATE_TORQUE)
            ]
            coherent_action_rows = [
                row for row in active_action_rows if _action_echo_coherent(row)
            ]
            incoherent_action_row_count = (
                len(active_action_rows) - len(coherent_action_rows)
            )
            coherent_action_row_fraction = (
                len(coherent_action_rows) / len(active_action_rows)
                if active_action_rows
                else 0.0
            )
            maximum_derived_abs_joint_acceleration_rad_s2 = (
                _maximum_derived_abs_joint_acceleration(samples)
            )
            achieved_control_update_rate_hz = _counter_rate_hz(
                samples, "control_update_count"
            )
            achieved_torque_call_rate_hz = _counter_rate_hz(
                samples, "torque_thread_tick_count"
            )
            maximum_control_update_gap_s = (
                _maximum_active_control_update_gap_s(samples)
            )
            entry_analysis: dict[str, Any] | None = None
            try:
                entry_analysis = analyze_entry_bumplessness(samples)
            except ValueError:
                pass
            rate_gate = bool(
                timestamps
                and len(timestamps) >= 0.9 * expected_rows
            )
            strict_gate = {
                "no_lineage_misses": lineage_misses == 0,
                "no_nonmonotonic_timestamps": nonmonotonic_count == 0,
                "observed_direct_torque": observed_torque,
                "observed_complete": complete,
                "entry_bumplessness_analysis_present": (
                    entry_analysis is not None
                ),
                "coherent_action_echo_rows_present": (
                    len(coherent_action_rows) >= 2
                ),
                "entry_joint_excursion_le_0_5mrad": (
                    entry_analysis is not None
                    and entry_analysis["maximum_abs_joint_excursion_rad"]
                    <= 0.0005
                ),
                "entry_tcp_translation_excursion_le_0_3mm": (
                    entry_analysis is not None
                    and entry_analysis[
                        "maximum_tcp_translation_excursion_m"
                    ]
                    <= 0.0003
                ),
                "at_least_90_percent_expected_500hz_rows": rate_gate,
                "torque_call_rate_between_450_and_550hz": (
                    MIN_TORQUE_CALL_RATE_HZ
                    <= achieved_torque_call_rate_hz
                    <= MAX_TORQUE_CALL_RATE_HZ
                ),
                "control_update_rate_at_least_150hz": (
                    achieved_control_update_rate_hz
                    >= MIN_CONTROL_UPDATE_RATE_HZ
                ),
                "maximum_control_update_gap_le_10ms": (
                    0.0
                    < maximum_control_update_gap_s
                    <= MAX_CONTROL_UPDATE_GAP_S
                ),
                "kunwei_baseline_complete": (
                    kunwei_summary["post_baseline_samples"] >= 50
                ),
                "kunwei_parse_errors_zero": (
                    kunwei_summary["parse_errors"] == 0
                ),
                "kunwei_dropped_sync_bytes_zero": (
                    kunwei_summary["dropped_sync_bytes"] == 0
                ),
                "kunwei_rate_between_900_and_1100hz": (
                    kunwei_summary["rate_hz_by_first_last"] is not None
                    and 900.0
                    <= kunwei_summary["rate_hz_by_first_last"]
                    <= 1100.0
                ),
                "kunwei_delivery_gap_within_watchdog": (
                    kunwei_summary["max_delivery_gap_ms"] is not None
                    and kunwei_summary["max_delivery_gap_ms"]
                    <= sensor_delivery_watchdog_s * 1000.0
                ),
            }
            strict_gate["ok"] = all(strict_gate.values()) and failure is None
            evidence.update(
                {
                    "unique_controller_timestamps": unique_timestamps,
                    "observed_controller_span_s": controller_span,
                    "expected_500hz_rows": expected_rows,
                    "achieved_output_row_rate_hz": achieved_rate,
                    "achieved_control_update_rate_hz": (
                        achieved_control_update_rate_hz
                    ),
                    "achieved_torque_call_rate_hz": (
                        achieved_torque_call_rate_hz
                    ),
                    "maximum_control_update_gap_s": (
                        maximum_control_update_gap_s
                    ),
                    "duplicate_timestamp_count": duplicate_count,
                    "nonmonotonic_timestamp_count": nonmonotonic_count,
                    "ack_command_lineage_misses": lineage_misses,
                    "active_action_echo_rows": len(active_action_rows),
                    "coherent_action_echo_rows": len(coherent_action_rows),
                    "incoherent_action_echo_rows": (
                        incoherent_action_row_count
                    ),
                    "coherent_action_echo_row_fraction": (
                        coherent_action_row_fraction
                    ),
                    "applied_action_echo_captured": bool(
                        coherent_action_rows
                    ),
                    "maximum_derived_abs_joint_acceleration_rad_s2": (
                        maximum_derived_abs_joint_acceleration_rad_s2
                    ),
                    "entry_bumplessness": entry_analysis,
                    "strict_success_gate": strict_gate,
                }
            )
            evidence["ok"] = bool(strict_gate["ok"])
            _write_json_new(evidence_path, evidence)
    return json.loads((output_dir / "evidence.json").read_text())


def run_normal_torque_baseline(args: argparse.Namespace) -> dict[str, Any]:
    """Capture normal UR position-control torque without sending any command."""

    if not math.isfinite(args.duration_s) or not 0.1 <= args.duration_s <= 10.0:
        raise ValueError("normal_torque_baseline_duration_out_of_range")
    status = readonly_status(args.robot_host)
    failures: list[str] = []
    if status["rtde"]["robot_mode"] != ROBOT_MODE_RUNNING:
        failures.append("robot_not_running")
    if status["rtde"]["safety_mode"] != SAFETY_MODE_NORMAL:
        failures.append("safety_not_normal")
    if not status["stopped"]:
        failures.append("program_not_stopped")
    if not status["stationary"]:
        failures.append("robot_not_stationary")
    if failures:
        raise RuntimeError(
            "normal_torque_baseline_preflight_failed:"
            + ",".join(sorted(failures))
        )
    _enforce_no_live_writer_conflict()
    output_dir = _next_available_run_dir(args.output_dir.resolve())
    rows: list[dict[str, Any]] = []
    start = time.monotonic()
    with LiveRTDE(args.robot_host, timeout=args.connect_timeout_s) as rtde:
        rtde.negotiate()
        output_recipe, output_types = rtde.setup_outputs(500.0, OUTPUT_FIELDS)
        rtde.start()
        deadline = start + args.duration_s
        while time.monotonic() < deadline:
            batch = _receive_available(
                rtde,
                output_recipe,
                output_types,
                OUTPUT_FIELDS,
                min(0.01, max(0.0, deadline - time.monotonic())),
            )
            for sample in batch:
                rows.append(
                    _compile_probe_row(
                        sample,
                        host_elapsed_s=time.monotonic() - start,
                    )
                )
    if len(rows) < int(args.duration_s * 500.0 * 0.9):
        raise RuntimeError("normal_torque_baseline_rtde_rows_insufficient")
    csv_path = output_dir / "normal_position_control_rtde.csv"
    _write_csv(csv_path, rows)
    baseline_pose = [rows[0][f"actual_TCP_pose_{axis}"] for axis in range(6)]
    joint_stats: list[dict[str, Any]] = []
    for joint in range(6):
        values = [float(row[f"target_moment_{joint}"]) for row in rows]
        sorted_abs = sorted(abs(value) for value in values)
        p95_index = min(
            len(sorted_abs) - 1,
            max(0, math.ceil(0.95 * len(sorted_abs)) - 1),
        )
        joint_stats.append(
            {
                "joint": joint,
                "mean_nm": sum(values) / len(values),
                "min_nm": min(values),
                "max_nm": max(values),
                "max_abs_nm": max(abs(value) for value in values),
                "p95_abs_nm": sorted_abs[p95_index],
            }
        )
    motor_diagnostic_stats: dict[str, list[dict[str, Any]]] = {}
    for signal in (
        "target_current",
        "actual_current",
        "actual_current_as_torque",
        "joint_control_output",
    ):
        signal_stats: list[dict[str, Any]] = []
        for joint in range(6):
            values = [float(row[f"{signal}_{joint}"]) for row in rows]
            mean = sum(values) / len(values)
            signal_stats.append(
                {
                    "joint": joint,
                    "mean": mean,
                    "min": min(values),
                    "max": max(values),
                    "peak_to_peak": max(values) - min(values),
                    "rms_about_mean": math.sqrt(
                        sum((value - mean) ** 2 for value in values)
                        / len(values)
                    ),
                }
            )
        motor_diagnostic_stats[signal] = signal_stats
    joint_modes = [
        sorted({int(row[f"joint_mode_{joint}"]) for row in rows})
        for joint in range(6)
    ]
    maximum_translation_m = max(
        _sample_translation_error_sqm3(
            [row[f"actual_TCP_pose_{axis}"] for axis in range(6)],
            baseline_pose,
        )
        for row in rows
    )
    maximum_tcp_speed_m_s = max(
        math.sqrt(
            sum(
                float(row[f"actual_TCP_speed_{axis}"]) ** 2
                for axis in range(3)
            )
        )
        for row in rows
    )
    maximum_joint_speed_rad_s = max(
        max(abs(float(row[f"actual_qd_{axis}"])) for axis in range(6))
        for row in rows
    )
    evidence = {
        "schema": "ur10e_normal_position_control_torque_baseline/v1",
        "claim_class": "live_read_only_normal_position_control_torque",
        "ok": True,
        "robot_host": args.robot_host,
        "duration_s": time.monotonic() - start,
        "sample_count": len(rows),
        "target_moment_by_joint": joint_stats,
        "motor_diagnostic_by_signal": motor_diagnostic_stats,
        "joint_mode_values_by_joint": joint_modes,
        "motor_diagnostic_semantics": (
            "RTDE motor current/torque diagnostics only; these fields are not "
            "an external F/T source and do not replace the Kunwei wrench."
        ),
        "maximum_tcp_translation_m": maximum_translation_m,
        "maximum_tcp_speed_m_s": maximum_tcp_speed_m_s,
        "maximum_joint_speed_rad_s": maximum_joint_speed_rad_s,
        "data_csv": str(csv_path),
        "program_sent": False,
        "rtde_inputs_written": False,
        "direct_torque_called": False,
        "kunwei_stream_started": False,
        "motion_performed": False,
        "training_dataset": False,
    }
    evidence_path = output_dir / "evidence.json"
    _write_json_new(evidence_path, evidence)
    return evidence


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    status = subparsers.add_parser("status", help="read-only controller snapshot")
    status.add_argument("--robot-host", default="192.168.1.18")
    baseline = subparsers.add_parser(
        "normal-torque-baseline",
        help="read-only 500 Hz target_moment capture under normal position control",
    )
    baseline.add_argument("--robot-host", default="192.168.1.18")
    baseline.add_argument("--duration-s", type=float, default=2.0)
    baseline.add_argument("--output-dir", type=Path, required=True)
    baseline.add_argument("--connect-timeout-s", type=float, default=3.0)
    analyze_entry = subparsers.add_parser(
        "analyze-entry",
        help="offline replay of the first 20 ms of a recorded torque transition",
    )
    analyze_entry.add_argument("--csv", type=Path, required=True)
    analyze_entry.add_argument("--output", type=Path)
    probe = subparsers.add_parser(
        "compile-probe",
        help="explicitly authorized controller parser/connectivity probe without motion",
    )
    probe.add_argument("--robot-host", default="192.168.1.18")
    probe.add_argument("--output-dir", type=Path, required=True)
    probe.add_argument("--connect-timeout-s", type=float, default=3.0)
    probe.add_argument("--probe-timeout-s", type=float, default=1.0)
    probe.add_argument("--live", action="store_true")
    probe.add_argument("--send-urscript", action="store_true")
    probe.add_argument("--no-motion", action="store_true")
    handshake = subparsers.add_parser(
        "receiver-handshake",
        help="start the exact full receiver without RTDE inputs or Direct Torque",
    )
    handshake.add_argument("--robot-host", default="192.168.1.18")
    handshake.add_argument("--bundle-manifest", type=Path, required=True)
    handshake.add_argument("--output-dir", type=Path, required=True)
    handshake.add_argument("--connect-timeout-s", type=float, default=3.0)
    handshake.add_argument(
        "--handshake-timeout-s", type=float, default=0.6
    )
    handshake.add_argument("--live", action="store_true")
    handshake.add_argument("--send-urscript", action="store_true")
    handshake.add_argument("--no-motion", action="store_true")
    validate = subparsers.add_parser("validate", help="offline bundle validation")
    validate.add_argument("--bundle-manifest", type=Path, required=True)
    run = subparsers.add_parser("run", help="explicitly authorized live canary")
    run.add_argument("--robot-host", default="192.168.1.18")
    run.add_argument("--sensor-ip", default="192.168.50.25")
    run.add_argument("--sensor-port", type=int, default=5152)
    run.add_argument(
        "--kunwei-calibration",
        type=Path,
        default=KUNWEI_CALIBRATION_DEFAULT,
    )
    run.add_argument("--bundle-manifest", type=Path, required=True)
    run.add_argument(
        "--canary-stage",
        choices=CANARY_STAGE_ORDER,
        required=True,
        help="staged live sequence; later stages require the immediately prior evidence",
    )
    run.add_argument("--compile-probe-evidence", type=Path, required=True)
    run.add_argument("--prior-stage-evidence", type=Path)
    run.add_argument("--output-dir", type=Path, required=True)
    run.add_argument("--connect-timeout-s", type=float, default=3.0)
    run.add_argument("--receiver-wait-s", type=float, default=2.0)
    run.add_argument(
        "--sensor-delivery-watchdog-s",
        type=float,
        default=DEFAULT_KUNWEI_DELIVERY_WATCHDOG_S,
        help=(
            "maximum host TCP delivery gap; this is not the native 1 kHz "
            "sensor sample period"
        ),
    )
    run.add_argument("--live", action="store_true")
    run.add_argument("--send-urscript", action="store_true")
    run.add_argument("--write-rtde-inputs", action="store_true")
    run.add_argument("--allow-direct-torque", action="store_true")
    run.add_argument("--allow-motion", action="store_true")
    run.add_argument("--no-contact", action="store_true")
    run.add_argument("--allow-kunwei-stream-command", action="store_true")
    run.add_argument(
        "--recorder-output",
        type=Path,
        help=(
            "optional offline episode-recorder sidecar directory; rows are "
            "always shadow/ineligible until independent acceptance evidence"
        ),
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "status":
            result = readonly_status(args.robot_host)
        elif args.command == "normal-torque-baseline":
            result = run_normal_torque_baseline(args)
        elif args.command == "analyze-entry":
            result = analyze_entry_csv(args.csv.resolve(), args.output)
        elif args.command == "compile-probe":
            result = run_compile_probe(args)
        elif args.command == "receiver-handshake":
            bundle = validate_bundle(args.bundle_manifest)
            result = run_receiver_handshake_probe(args, bundle)
        else:
            bundle = validate_bundle(args.bundle_manifest)
            if args.command == "validate":
                runtime_binding = runtime_source_binding()
                result = {
                    "ok": True,
                    "claim_class": "offline_validated_no_live_actions",
                    "receiver_source_sha256": bundle.source_sha256,
                    "bundle_manifest_sha256": bundle.manifest_sha256,
                    "friction_profile": bundle.friction_profile,
                    "viscous_scale": list(bundle.viscous_scale),
                    "coulomb_scale": list(bundle.coulomb_scale),
                    "reference_artifact_sha256": bundle.reference_sha256,
                    "runtime_source_sha256": runtime_binding["sha256"],
                    "runtime_source_binding": runtime_binding,
                    "reference_rows": len(bundle.timeline.rows),
                    "capture_duration_s": bundle.timeline.duration_s,
                    "motion_performed": False,
                    "urscript_sent": False,
                    "rtde_inputs_written": False,
                    "kunwei_stream_started": False,
                }
            else:
                result = run_live(args, bundle)
    except Exception as exc:
        print(
            json.dumps(
                {"ok": False, "error": f"{type(exc).__name__}: {exc}"},
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 2
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
