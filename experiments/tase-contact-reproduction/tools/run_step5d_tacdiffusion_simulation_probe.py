#!/usr/bin/env python3
"""Drive the TacDiffusion Stage25 probe only after TP Simulation confirmation.

This tool never uploads or loads a program.  By default it also never plays or
stops the Dashboard server.  The explicit v3 ``--auto-play-simulation`` gate
allows Codex to start only the already-loaded TP Simulation package after a
fresh stationary/local/safety preflight.  RTDE input writes require all three
explicit start flags.
"""

from __future__ import annotations

import argparse
from collections import deque
from datetime import datetime
import json
import math
import os
from pathlib import Path
import select
import struct
import sys
import time
from typing import Any, Callable, Sequence


ROOT = Path(__file__).resolve().parents[1]
UR_HELPERS = Path("/home/andy/codex-private-skills-shared-main/skills/ur10e-realsetup/scripts")
if str(UR_HELPERS) not in sys.path:
    sys.path.insert(0, str(UR_HELPERS))

from _ur_common import RTDEClient, dashboard_exchange, read_rtde_once  # noqa: E402


PROGRAM = "step5d_tacdiffusion_direct_torque_simulation_probe_v1"
CONTROLLER_PROGRAM = f"/programs/andyl/kunwei/step5/{PROGRAM}.urp"


def controller_program_for(program: str) -> str:
    if not program or "/" in program or not program.endswith(("_v1", "_v2", "_v3")):
        raise ValueError(f"invalid_probe_program:{program}")
    return f"/programs/andyl/kunwei/step5/{program}.urp"
RUNTIME_STOPPED = 1
RUNTIME_PLAYING = 2
ROBOT_MODE_RUNNING = 7
SAFETY_MODE_NORMAL = 1
STATE_WAITING = 0
STATE_ZERO_STARTUP = 1
STATE_TORQUE = 2
STATE_SAFE_EXIT = 3
FAULT_SEQUENCE = 3
MODE_DISABLED = 0
MODE_ARMED = 1
MODE_COMPLETE = 2
FIXED_STIFFNESS = (600.0, 600.0, 600.0, 30.0, 30.0, 30.0)
FIXED_DAMPING = (
    69.2820323,
    69.2820323,
    69.2820323,
    4.898979486,
    4.898979486,
    4.898979486,
)
WRENCH_FRAME_TOKEN = 5_252_001
CONTROL_PERIOD_S = 0.002
EVENT_RING_SIZE = 256
SEQUENCE_PACING_INDEPENDENT = "independent"
SEQUENCE_PACING_ACK = "ack"

DOUBLE_INPUT_FIELDS = [f"input_double_register_{index}" for index in range(24, 48)]
INTEGER_INPUT_FIELDS = [f"input_int_register_{index}" for index in range(24, 32)]
INPUT_FIELDS = DOUBLE_INPUT_FIELDS + INTEGER_INPUT_FIELDS
OUTPUT_FIELDS = [
    "timestamp",
    "actual_TCP_pose",
    "actual_TCP_speed",
    "actual_q",
    "actual_qd",
    "runtime_state",
    "robot_mode",
    "safety_mode",
    *[f"output_double_register_{index}" for index in range(24, 36)],
    *[f"output_int_register_{index}" for index in range(24, 32)],
    *[f"output_int_register_{index}" for index in range(32, 38)],
]


def rtde_format(type_name: str) -> str:
    formats = {
        "DOUBLE": "d",
        "VECTOR6D": "6d",
        "INT32": "i",
        "UINT32": "I",
        "UINT64": "Q",
        "BOOL": "?",
    }
    try:
        return formats[type_name]
    except KeyError as exc:
        raise RuntimeError(f"unsupported_rtde_type:{type_name}") from exc


class ProbeRTDE(RTDEClient):
    def setup_inputs(self, fields: list[str]) -> tuple[int, list[str]]:
        self._send_packet("I", ",".join(fields).encode())
        packet_type, payload = self._recv_packet()
        if packet_type != ord("I"):
            raise RuntimeError(f"rtde_input_setup_response:{packet_type}")
        recipe = payload[0]
        types = payload[1:].decode("ascii", errors="replace").split(",")
        if recipe == 0 or any(value == "NOT_FOUND" for value in types):
            raise RuntimeError(f"rtde_input_recipe_invalid:{types}")
        return recipe, types

    def send_inputs(self, recipe: int, types: list[str], values: Sequence[Any]) -> None:
        if len(types) != len(values):
            raise RuntimeError("rtde_input_value_count_mismatch")
        payload = bytearray([recipe])
        for type_name, value in zip(types, values):
            payload.extend(struct.pack("!" + rtde_format(type_name), value))
        self._send_packet("U", bytes(payload))

    def receive_latest(
        self,
        recipe: int,
        types: list[str],
        fields: list[str],
        timeout_s: float,
    ) -> dict[str, Any] | None:
        latest, _drained = self.receive_latest_available(
            recipe, types, fields, timeout_s
        )
        return latest

    def receive_latest_available(
        self,
        recipe: int,
        types: list[str],
        fields: list[str],
        timeout_s: float,
    ) -> tuple[dict[str, Any] | None, int]:
        assert self.sock is not None
        ready, _, _ = select.select([self.sock], [], [], timeout_s)
        if not ready:
            return None, 0
        latest = None
        drained = 0
        while True:
            packet_type, payload = self._recv_packet()
            drained += 1
            if packet_type == ord("U") and payload and payload[0] == recipe:
                cursor = 1
                values: list[Any] = []
                for type_name in types:
                    fmt = rtde_format(type_name)
                    width = struct.calcsize("!" + fmt)
                    unpacked = struct.unpack("!" + fmt, payload[cursor : cursor + width])
                    cursor += width
                    values.append(unpacked[0] if len(unpacked) == 1 else tuple(unpacked))
                latest = dict(zip(fields, values))
            ready, _, _ = select.select([self.sock], [], [], 0.0)
            if not ready:
                return latest, drained


class AbsoluteReleaseSchedule:
    """A no-burst absolute release schedule for the 500 Hz input contract."""

    def __init__(
        self,
        *,
        period_s: float = CONTROL_PERIOD_S,
        clock: Callable[[], float] | None = None,
        sleeper: Callable[[float], None] | None = None,
    ) -> None:
        if period_s <= 0.0:
            raise ValueError("period_s must be positive")
        self.period_s = float(period_s)
        self.clock = time.monotonic if clock is None else clock
        self.sleeper = time.sleep if sleeper is None else sleeper
        self.next_deadline = self.clock()
        self.release_count = 0
        self.missed_release_count = 0
        self.lateness_sum_s = 0.0
        self.max_lateness_s = 0.0

    def release(self) -> dict[str, float | int]:
        scheduled = self.next_deadline
        now = self.clock()
        if now < scheduled:
            self.sleeper(scheduled - now)
            now = self.clock()
        lateness = max(0.0, now - scheduled)
        missed = max(0, int(math.floor(lateness / self.period_s)))
        self.next_deadline = scheduled + (missed + 1) * self.period_s
        self.release_count += 1
        self.missed_release_count += missed
        self.lateness_sum_s += lateness
        self.max_lateness_s = max(self.max_lateness_s, lateness)
        return {
            "scheduled_monotonic_s": scheduled,
            "actual_monotonic_s": now,
            "deadline_lateness_s": lateness,
            "missed_releases": missed,
            "release_index": self.release_count,
        }

    def timing(self) -> dict[str, float | int]:
        return {
            "period_s": self.period_s,
            "release_count": self.release_count,
            "missed_release_count": self.missed_release_count,
            "maximum_deadline_lateness_s": self.max_lateness_s,
            "mean_deadline_lateness_s": (
                self.lateness_sum_s / self.release_count
                if self.release_count
                else None
            ),
        }


class ProbeEventRing:
    """Keep the final controller/host timeline bounded across failures."""

    def __init__(self, maxlen: int = EVENT_RING_SIZE) -> None:
        self._events: deque[dict[str, Any]] = deque(maxlen=maxlen)

    def append(self, event: dict[str, Any]) -> None:
        self._events.append(dict(event))

    def snapshot(self) -> list[dict[str, Any]]:
        return list(self._events)


def scheduler_metadata() -> dict[str, Any]:
    """Read scheduler/affinity facts without changing the runtime policy."""

    try:
        affinity = sorted(int(value) for value in os.sched_getaffinity(0))
    except (AttributeError, OSError):
        affinity = None
    try:
        policy = int(os.sched_getscheduler(0))
        priority = int(os.sched_getparam(0).sched_priority)
    except (AttributeError, OSError):
        policy = None
        priority = None
    policy_names = {
        getattr(os, "SCHED_OTHER", -1): "SCHED_OTHER",
        getattr(os, "SCHED_FIFO", -2): "SCHED_FIFO",
        getattr(os, "SCHED_RR", -3): "SCHED_RR",
    }
    return {
        "policy": policy_names.get(policy, "unknown"),
        "policy_value": policy,
        "priority": priority,
        "cpu_affinity": affinity,
    }


def default_evidence_path() -> Path:
    stamp = datetime.now().astimezone().strftime("%Y%m%d_%H%M%S_%f")
    return ROOT / "runs" / f"step5d_tacdiffusion_controller_simulation_probe_{stamp}" / "evidence.json"


def write_evidence(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def dashboard_snapshot(host: str) -> dict[str, str]:
    return dashboard_exchange(
        host,
        [
            "PolyscopeVersion",
            "get loaded program",
            "programState",
            "robotmode",
            "safetystatus",
            "is in remote control",
        ],
    )


def dashboard_play(host: str) -> dict[str, str]:
    """Start only an explicitly authorized TP-Simulation invocation."""

    responses = dashboard_exchange(host, ["play"])
    response = responses.get("play", "")
    if not response.lower().startswith("starting program"):
        raise RuntimeError(f"dashboard_play_rejected:{response}")
    return responses


def readonly_status(host: str, program: str = PROGRAM) -> dict[str, Any]:
    controller_program = controller_program_for(program)
    dashboard = dashboard_snapshot(host)
    rtde = read_rtde_once(
        host,
        [
            "actual_TCP_pose",
            "actual_TCP_speed",
            "actual_q",
            "actual_qd",
            "runtime_state",
            "robot_mode",
            "safety_mode",
        ],
        frequency_hz=10.0,
    )
    return {
        "program": program,
        "expected_controller_program": controller_program,
        "dashboard": dashboard,
        "rtde": rtde,
        "loaded_program_matches": controller_program in dashboard.get("get loaded program", ""),
        "controller_5_26": "URSoftware 5.26." in dashboard.get("PolyscopeVersion", ""),
        "local_control": dashboard.get("is in remote control", "").strip().lower() == "false",
        "stopped": int(rtde["runtime_state"]) == RUNTIME_STOPPED,
        # A stopped 5.26 controller can expose denormal values around 1e-45.
        # Treat only bounded numerical zero as stationary.
        "stationary": max(
            abs(float(value)) for value in (*rtde["actual_TCP_speed"], *rtde["actual_qd"])
        )
        <= 1.0e-9,
        "motion_performed": False,
        "rtde_inputs_written": False,
    }


def input_values(
    pose: Sequence[float],
    *,
    mode: int,
    sequence: int,
    heartbeat: int | None = None,
    lease_id: int,
) -> list[Any]:
    if len(pose) != 6 or not all(math.isfinite(float(value)) for value in pose):
        raise RuntimeError("equilibrium_pose_invalid")
    doubles = [
        *[float(value) for value in pose],
        *FIXED_STIFFNESS,
        *FIXED_DAMPING,
        *([0.0] * 6),
    ]
    integers = [
        int(mode),
        int(sequence),
        int(sequence if heartbeat is None else heartbeat),
        int(lease_id),
        0,
        0,
        0,
        WRENCH_FRAME_TOKEN,
    ]
    return doubles + integers


def _maximum_abs(values: Sequence[float]) -> float:
    return max(abs(float(value)) for value in values)


def _vector_distance(left: Sequence[float], right: Sequence[float]) -> float:
    return math.sqrt(sum((float(a) - float(b)) ** 2 for a, b in zip(left, right)))


def validate_start_preflight(status: dict[str, Any]) -> None:
    failures = []
    if not status["controller_5_26"]:
        failures.append("controller_not_5_26")
    if not status["local_control"]:
        failures.append("controller_not_local_control")
    if not status["loaded_program_matches"]:
        failures.append("simulation_probe_not_loaded")
    if status["dashboard"].get("safetystatus") != "Safetystatus: NORMAL":
        failures.append("dashboard_safety_not_normal")
    if status["dashboard"].get("robotmode") != "Robotmode: RUNNING":
        failures.append("dashboard_robotmode_not_running")
    if int(status["rtde"]["safety_mode"]) != SAFETY_MODE_NORMAL:
        failures.append("rtde_safety_not_normal")
    if int(status["rtde"]["robot_mode"]) != ROBOT_MODE_RUNNING:
        failures.append("rtde_robotmode_not_running")
    if not status["stationary"]:
        failures.append("robot_not_stationary")
    if failures:
        raise RuntimeError("preflight_failed:" + ",".join(failures))


def run_probe(args: argparse.Namespace) -> dict[str, Any]:
    if not (
        args.run_probe
        and args.write_rtde_inputs
        and args.tp_simulation_visible
    ):
        raise RuntimeError(
            "start_requires_--run-probe_--write-rtde-inputs_--tp-simulation-visible"
        )
    program = getattr(args, "program", PROGRAM)
    controller_program = controller_program_for(program)
    sequence_pacing = getattr(args, "sequence_pacing", None) or (
        SEQUENCE_PACING_ACK if program.endswith("_v3") else SEQUENCE_PACING_INDEPENDENT
    )
    if sequence_pacing not in {SEQUENCE_PACING_INDEPENDENT, SEQUENCE_PACING_ACK}:
        raise RuntimeError(f"invalid_sequence_pacing:{sequence_pacing}")
    auto_play_simulation = bool(getattr(args, "auto_play_simulation", False))
    if auto_play_simulation and (
        not program.endswith("_v3")
        or sequence_pacing != SEQUENCE_PACING_ACK
        or not args.tp_simulation_visible
    ):
        raise RuntimeError(
            "auto_play_requires_v3_ack_pacing_and_tp_simulation_visible"
        )
    evidence_schema = (
        "step5d_tacdiffusion_controller_simulation_probe_v3"
        if program.endswith("_v3")
        else (
        "step5d_tacdiffusion_controller_simulation_probe_v2"
        if program.endswith("_v2")
        else "step5d_tacdiffusion_controller_simulation_probe_v1"
        )
    )
    evidence_path = Path(getattr(args, "evidence", None) or default_evidence_path())
    started_at = datetime.now().astimezone().isoformat()
    recorder = ProbeEventRing()
    initial_status: dict[str, Any] | None = None
    final_dashboard_state: dict[str, Any] | None = None
    initial_pose: tuple[float, ...] = (0.0,) * 6
    initial_q: tuple[float, ...] = (0.0,) * 6
    lease_id = int(time.time()) & 0x7FFFFFFF or 1
    phase = 0
    phase_reset_sent = False
    phase_equilibrium: tuple[float, ...] | None = None
    phase_equilibria: dict[int, tuple[float, ...]] = {}
    outgoing_sequence = 0
    outgoing_mode = MODE_DISABLED
    outgoing_heartbeat = 0
    normal_completion_sent = False
    sequence_fault_sent = False
    phase_valid_acks = {1: 0, 2: 0}
    saw_states: set[int] = set()
    saw_faults: set[int] = set()
    states_by_phase: dict[int, set[int]] = {1: set(), 2: set()}
    faults_by_phase: dict[int, set[int]] = {1: set(), 2: set()}
    phase_results: dict[str, float] = {}
    steptime_samples: list[float] = []
    controller_timestamps: list[float] = []
    controller_timestamp: float | None = None
    last_output: dict[str, Any] | None = None
    last_output_host_monotonic: float | None = None
    latest_runtime_state = RUNTIME_STOPPED
    latest_state = STATE_WAITING
    latest_fault = 0
    latest_model_fault = 0
    latest_ack_sequence = 0
    latest_diagnostic_code = 0
    latest_diagnostic_detail = 0
    phase_diagnostics = {
        "1": {"code": 0, "detail": 0},
        "2": {"code": 0, "detail": 0},
    }
    phase_sequence_stats: dict[int, dict[str, Any]] = {
        phase_id: {
            "armed_packets_sent": 0,
            "sequence_wait_releases": 0,
            "ack_observations": 0,
            "first_send_monotonic_s": None,
            "last_send_monotonic_s": None,
            "maximum_sequence_lag": 0,
        }
        for phase_id in (1, 2)
    }
    dashboard_play_result: dict[str, str] | None = None
    max_tcp_speed = 0.0
    max_qd = 0.0
    max_tcp_pose_delta = 0.0
    max_joint_delta = 0.0
    samples = 0
    completed = False
    observed_playing_phase = False
    schedule: AbsoluteReleaseSchedule | None = None
    last_values: list[Any] = input_values(
        initial_pose,
        mode=MODE_DISABLED,
        sequence=0,
        lease_id=lease_id,
    )
    success_result: dict[str, Any] | None = None

    def equilibrium_hex(pose: Sequence[float] | None) -> str | None:
        if pose is None:
            return None
        return struct.pack("!6d", *(float(value) for value in pose)).hex()

    def current_observation() -> dict[str, Any]:
        if last_output is None:
            return {
                "controller_timestamp_s": controller_timestamp,
                "runtime_state": latest_runtime_state,
                "phase": phase,
                "stage_state": latest_state,
                "fault": latest_fault,
                "model_fault": latest_model_fault,
                "ack_sequence": latest_ack_sequence,
                "diagnostic_code": latest_diagnostic_code,
                "diagnostic_detail": latest_diagnostic_detail,
                "phase_diagnostics": phase_diagnostics,
                "sequence_pacing": sequence_pacing,
                "phase_sequence_stats": phase_sequence_stats,
            }
        return {
            "controller_timestamp_s": controller_timestamp,
            "runtime_state": latest_runtime_state,
            "phase": phase,
            "stage_state": latest_state,
            "fault": latest_fault,
            "model_fault": latest_model_fault,
            "ack_sequence": latest_ack_sequence,
            "diagnostic_code": latest_diagnostic_code,
            "diagnostic_detail": latest_diagnostic_detail,
            "phase_diagnostics": phase_diagnostics,
            "sequence_pacing": sequence_pacing,
            "phase_sequence_stats": phase_sequence_stats,
            "actual_TCP_pose": [float(value) for value in last_output["actual_TCP_pose"]],
            "actual_TCP_speed": [float(value) for value in last_output["actual_TCP_speed"]],
            "actual_q": [float(value) for value in last_output["actual_q"]],
            "actual_qd": [float(value) for value in last_output["actual_qd"]],
        }

    def record_event(
        kind: str,
        *,
        release: dict[str, float | int] | None = None,
        output_fresh: bool | None = None,
        output_drained: int = 0,
        outgoing: Sequence[Any] | None = None,
        completed_send: bool | None = None,
    ) -> None:
        packet = None
        event_outgoing_mode = outgoing_mode
        event_outgoing_sequence = outgoing_sequence
        event_outgoing_heartbeat = outgoing_heartbeat
        if outgoing is not None:
            packet = {
                "mode": int(outgoing[24]),
                "sequence": int(outgoing[25]),
                "heartbeat": int(outgoing[26]),
                "lease_id": int(outgoing[27]),
            }
            event_outgoing_mode = packet["mode"]
            event_outgoing_sequence = packet["sequence"]
            event_outgoing_heartbeat = packet["heartbeat"]
        pose_delta = None
        joint_delta = None
        tcp_speed = None
        joint_speed = None
        if last_output is not None:
            pose_delta = _vector_distance(last_output["actual_TCP_pose"][:3], initial_pose[:3])
            joint_delta = _vector_distance(last_output["actual_q"], initial_q)
            tcp_speed = _maximum_abs(last_output["actual_TCP_speed"])
            joint_speed = _maximum_abs(last_output["actual_qd"])
        event = {
            "kind": kind,
            "host_monotonic_s": (
                float(release["actual_monotonic_s"])
                if release is not None
                else time.monotonic()
            ),
            "controller_timestamp_s": controller_timestamp,
            "phase": phase,
            "runtime_state": latest_runtime_state,
            "stage_state": latest_state,
            "fault": latest_fault,
            "model_fault": latest_model_fault,
            "ack_sequence": latest_ack_sequence,
            "diagnostic_code": latest_diagnostic_code,
            "diagnostic_detail": latest_diagnostic_detail,
            "phase_diagnostics": phase_diagnostics,
            "sequence_pacing": sequence_pacing,
            "sequence_lag": max(0, outgoing_sequence - latest_ack_sequence),
            "outgoing": packet,
            "outgoing_mode": event_outgoing_mode,
            "outgoing_sequence": event_outgoing_sequence,
            "outgoing_heartbeat": event_outgoing_heartbeat,
            "deadline_lateness_s": (
                float(release["deadline_lateness_s"]) if release is not None else None
            ),
            "missed_releases": (
                int(release["missed_releases"]) if release is not None else 0
            ),
            "steptime_s": (
                float(last_output["output_double_register_25"])
                if last_output is not None
                else None
            ),
            "tcp_pose_delta_m": pose_delta,
            "joint_position_delta_rad": joint_delta,
            "tcp_speed": tcp_speed,
            "joint_speed": joint_speed,
            "equilibrium_bytes_hex": equilibrium_hex(phase_equilibrium),
            "output_fresh": output_fresh,
            "output_drained": output_drained,
            "output_age_s": (
                time.monotonic() - last_output_host_monotonic
                if last_output_host_monotonic is not None
                else None
            ),
            "completed_send": completed_send,
        }
        recorder.append(event)

    def send_packet(
        rtde: ProbeRTDE,
        input_recipe: int,
        input_types: list[str],
        values: Sequence[Any],
        *,
        kind: str,
        release: dict[str, float | int] | None = None,
        output_fresh: bool | None = None,
        output_drained: int = 0,
    ) -> None:
        nonlocal last_values
        record_event(
            f"{kind}_attempt",
            release=release,
            output_fresh=output_fresh,
            output_drained=output_drained,
            outgoing=values,
            completed_send=False,
        )
        rtde.send_inputs(input_recipe, input_types, values)
        last_values = list(values)
        if kind in {"normal_armed", "fault_phase_armed", "sequence_gap_injected"}:
            stats = phase_sequence_stats.get(phase)
            if stats is not None:
                now = float(
                    release["actual_monotonic_s"]
                    if release is not None
                    else time.monotonic()
                )
                stats["armed_packets_sent"] += 1
                stats["first_send_monotonic_s"] = (
                    now
                    if stats["first_send_monotonic_s"] is None
                    else stats["first_send_monotonic_s"]
                )
                stats["last_send_monotonic_s"] = now
        record_event(
            kind,
            release=release,
            output_fresh=output_fresh,
            output_drained=output_drained,
            outgoing=values,
            completed_send=True,
        )

    def build_values(mode: int, sequence: int, pose: Sequence[float]) -> list[Any]:
        return input_values(
            pose,
            mode=mode,
            sequence=sequence,
            heartbeat=sequence,
            lease_id=lease_id,
        )

    def sequence_stats_snapshot() -> dict[str, dict[str, Any]]:
        snapshot: dict[str, dict[str, Any]] = {}
        for phase_id, raw in phase_sequence_stats.items():
            item = dict(raw)
            first = item["first_send_monotonic_s"]
            last = item["last_send_monotonic_s"]
            count = int(item["armed_packets_sent"])
            elapsed = (
                float(last) - float(first)
                if first is not None and last is not None
                else None
            )
            item["effective_send_rate_hz"] = (
                (count - 1) / elapsed
                if elapsed is not None and elapsed > 0.0 and count > 1
                else None
            )
            snapshot[str(phase_id)] = item
        return snapshot

    try:
        try:
            if program == PROGRAM:
                initial_status = readonly_status(args.robot_host)
            else:
                initial_status = readonly_status(args.robot_host, program=program)
            validate_start_preflight(initial_status)
            initial_pose = tuple(float(value) for value in initial_status["rtde"]["actual_TCP_pose"])
            initial_q = tuple(float(value) for value in initial_status["rtde"]["actual_q"])
            last_values = build_values(MODE_DISABLED, 0, initial_pose)
            start_monotonic = time.monotonic()
            deadline = start_monotonic + args.wait_for_play_s

            with ProbeRTDE(args.robot_host, timeout=args.connect_timeout_s) as rtde:
                rtde.negotiate()
                output_recipe, output_types = rtde.setup_outputs(500.0, OUTPUT_FIELDS)
                input_recipe, input_types = rtde.setup_inputs(INPUT_FIELDS)
                rtde.start()
                send_packet(rtde, input_recipe, input_types, last_values, kind="initial_disabled")
                if auto_play_simulation:
                    dashboard_play_result = dashboard_play(args.robot_host)
                    record_event("dashboard_play")
                schedule = AbsoluteReleaseSchedule(period_s=CONTROL_PERIOD_S)
                try:
                    while time.monotonic() < deadline:
                        release = schedule.release()
                        current, drained = rtde.receive_latest_available(
                            output_recipe,
                            output_types,
                            OUTPUT_FIELDS,
                            timeout_s=0.0,
                        )
                        output_fresh = current is not None
                        if current is not None:
                            samples += 1
                            last_output = current
                            last_output_host_monotonic = float(release["actual_monotonic_s"])
                            latest_runtime_state = int(current["runtime_state"])
                            robot_mode = int(current["robot_mode"])
                            safety_mode = int(current["safety_mode"])
                            latest_state = int(current["output_int_register_24"])
                            latest_fault = int(current["output_int_register_26"])
                            latest_model_fault = int(current["output_int_register_29"])
                            latest_ack_sequence = int(current["output_int_register_25"])
                            latest_diagnostic_code = int(current["output_int_register_32"])
                            latest_diagnostic_detail = int(current["output_int_register_33"])
                            phase_diagnostics["1"] = {
                                "code": int(current["output_int_register_34"]),
                                "detail": int(current["output_int_register_35"]),
                            }
                            phase_diagnostics["2"] = {
                                "code": int(current["output_int_register_36"]),
                                "detail": int(current["output_int_register_37"]),
                            }
                            current_phase = int(current["output_int_register_31"])
                            if current_phase in {1, 2}:
                                stats = phase_sequence_stats[current_phase]
                                if latest_ack_sequence > 0:
                                    stats["ack_observations"] += 1
                                stats["maximum_sequence_lag"] = max(
                                    int(stats["maximum_sequence_lag"]),
                                    max(0, outgoing_sequence - latest_ack_sequence),
                                )
                            if robot_mode != ROBOT_MODE_RUNNING or safety_mode != SAFETY_MODE_NORMAL:
                                raise RuntimeError(
                                    f"runtime_safety_changed:robot_mode={robot_mode},safety_mode={safety_mode}"
                                )
                            tcp_speed = _maximum_abs(current["actual_TCP_speed"])
                            qd = _maximum_abs(current["actual_qd"])
                            tcp_delta = _vector_distance(current["actual_TCP_pose"][:3], initial_pose[:3])
                            q_delta = _vector_distance(current["actual_q"], initial_q)
                            max_tcp_speed = max(max_tcp_speed, tcp_speed)
                            max_qd = max(max_qd, qd)
                            max_tcp_pose_delta = max(max_tcp_pose_delta, tcp_delta)
                            max_joint_delta = max(max_joint_delta, q_delta)
                            if (
                                tcp_speed > args.tcp_speed_abort
                                or qd > args.joint_speed_abort
                                or tcp_delta > args.tcp_position_abort
                                or q_delta > args.joint_position_abort
                            ):
                                raise RuntimeError(
                                    "physical_motion_guard:"
                                    f"tcp_speed={tcp_speed},qd={qd},tcp_delta={tcp_delta},q_delta={q_delta}"
                                )
                            steptime = float(current["output_double_register_25"])
                            if steptime > 0.0:
                                steptime_samples.append(steptime)
                            controller_timestamp = float(current["timestamp"])
                            controller_timestamps.append(controller_timestamp)
                            # Output registers survive Dashboard Stop and can
                            # still contain the prior run's terminal marker.
                            # Accept phase 99 only after this invocation has
                            # observed a playing phase 1/2 from the controller.
                            if current_phase == 99 and observed_playing_phase:
                                phase_results["normal"] = float(current["output_double_register_32"])
                                phase_results["sequence_fault"] = float(current["output_double_register_33"])
                                completed = True
                                send_packet(
                                    rtde,
                                    input_recipe,
                                    input_types,
                                    last_values,
                                    kind="terminal_hold",
                                    release=release,
                                    output_fresh=True,
                                    output_drained=drained,
                                )
                                break
                            if current_phase in {1, 2} and latest_runtime_state == RUNTIME_PLAYING:
                                observed_playing_phase = True
                                saw_states.add(latest_state)
                                saw_faults.add(latest_fault)
                                states_by_phase[current_phase].add(latest_state)
                                faults_by_phase[current_phase].add(latest_fault)
                                if current_phase != phase:
                                    phase = current_phase
                                    phase_reset_sent = True
                                    phase_equilibrium = None
                                    phase_equilibria.pop(phase, None)
                                    outgoing_sequence = 0
                                    outgoing_mode = MODE_DISABLED
                                    outgoing_heartbeat = 0
                                    normal_completion_sent = False
                                    sequence_fault_sent = False
                                    phase_valid_acks[phase] = 0
                                    last_values = build_values(
                                        MODE_DISABLED,
                                        0,
                                        tuple(float(value) for value in current["actual_TCP_pose"]),
                                    )
                                    send_packet(
                                        rtde,
                                        input_recipe,
                                        input_types,
                                        last_values,
                                        kind="phase_reset",
                                        release=release,
                                        output_fresh=True,
                                        output_drained=drained,
                                    )
                                    continue
                        if current is None and phase == 0:
                            send_packet(
                                rtde,
                                input_recipe,
                                input_types,
                                last_values,
                                kind="waiting_disabled",
                                release=release,
                                output_fresh=False,
                                output_drained=drained,
                            )
                            continue
                        if latest_runtime_state != RUNTIME_PLAYING or phase not in {1, 2}:
                            send_packet(
                                rtde,
                                input_recipe,
                                input_types,
                                last_values,
                                kind="not_playing_hold",
                                release=release,
                                output_fresh=output_fresh,
                                output_drained=drained,
                            )
                            continue
                        if phase_reset_sent:
                            if latest_state != STATE_WAITING or latest_ack_sequence != 0:
                                send_packet(
                                    rtde,
                                    input_recipe,
                                    input_types,
                                    last_values,
                                    kind="phase_waiting_reset",
                                    release=release,
                                    output_fresh=output_fresh,
                                    output_drained=drained,
                                )
                                continue
                            if last_output is None:
                                raise RuntimeError("phase_waiting_output_missing")
                            phase_equilibrium = tuple(float(value) for value in last_output["actual_TCP_pose"])
                            phase_equilibria[phase] = phase_equilibrium
                            phase_reset_sent = False
                            record_event(
                                "equilibrium_captured",
                                release=release,
                                output_fresh=output_fresh,
                                output_drained=drained,
                            )
                        if phase_equilibrium is None:
                            send_packet(
                                rtde,
                                input_recipe,
                                input_types,
                                last_values,
                                kind="equilibrium_wait",
                                release=release,
                                output_fresh=output_fresh,
                                output_drained=drained,
                            )
                            continue
                        if phase == 1:
                            if normal_completion_sent:
                                values = last_values
                                kind = "normal_completion_hold"
                            elif (
                                latest_state == STATE_TORQUE
                                and latest_ack_sequence >= args.valid_ticks
                                and latest_ack_sequence == outgoing_sequence
                            ):
                                outgoing_sequence = latest_ack_sequence + 1
                                outgoing_mode = MODE_COMPLETE
                                outgoing_heartbeat = outgoing_sequence
                                values = build_values(MODE_COMPLETE, outgoing_sequence, phase_equilibrium)
                                normal_completion_sent = True
                                kind = "normal_completion"
                            else:
                                if sequence_pacing == SEQUENCE_PACING_ACK:
                                    if latest_ack_sequence > outgoing_sequence:
                                        raise RuntimeError(
                                            f"ack_ahead_of_sent_sequence:phase=1:ack={latest_ack_sequence}:sent={outgoing_sequence}"
                                        )
                                    if latest_ack_sequence != outgoing_sequence:
                                        phase_sequence_stats[1]["sequence_wait_releases"] += 1
                                        phase_valid_acks[1] = max(
                                            phase_valid_acks[1], latest_ack_sequence
                                        )
                                        record_event(
                                            "normal_sequence_wait",
                                            release=release,
                                            output_fresh=output_fresh,
                                            output_drained=drained,
                                        )
                                        continue
                                    outgoing_sequence = latest_ack_sequence + 1
                                else:
                                    outgoing_sequence += 1
                                outgoing_mode = MODE_ARMED
                                outgoing_heartbeat = outgoing_sequence
                                values = build_values(MODE_ARMED, outgoing_sequence, phase_equilibrium)
                                phase_valid_acks[1] = max(phase_valid_acks[1], latest_ack_sequence)
                                kind = "normal_armed"
                        else:
                            if sequence_fault_sent:
                                values = last_values
                                kind = "sequence_fault_hold"
                            elif (
                                latest_state == STATE_TORQUE
                                and latest_ack_sequence >= args.fault_after_ticks
                                and latest_ack_sequence == outgoing_sequence
                            ):
                                outgoing_sequence = latest_ack_sequence + 2
                                outgoing_mode = MODE_ARMED
                                outgoing_heartbeat = outgoing_sequence
                                values = build_values(MODE_ARMED, outgoing_sequence, phase_equilibrium)
                                sequence_fault_sent = True
                                kind = "sequence_gap_injected"
                            else:
                                if sequence_pacing == SEQUENCE_PACING_ACK:
                                    if latest_ack_sequence > outgoing_sequence:
                                        raise RuntimeError(
                                            f"ack_ahead_of_sent_sequence:phase=2:ack={latest_ack_sequence}:sent={outgoing_sequence}"
                                        )
                                    if latest_ack_sequence != outgoing_sequence:
                                        phase_sequence_stats[2]["sequence_wait_releases"] += 1
                                        phase_valid_acks[2] = max(
                                            phase_valid_acks[2], latest_ack_sequence
                                        )
                                        record_event(
                                            "fault_sequence_wait",
                                            release=release,
                                            output_fresh=output_fresh,
                                            output_drained=drained,
                                        )
                                        continue
                                    outgoing_sequence = latest_ack_sequence + 1
                                else:
                                    outgoing_sequence += 1
                                outgoing_mode = MODE_ARMED
                                outgoing_heartbeat = outgoing_sequence
                                values = build_values(MODE_ARMED, outgoing_sequence, phase_equilibrium)
                                phase_valid_acks[2] = max(phase_valid_acks[2], latest_ack_sequence)
                                kind = "fault_phase_armed"
                        send_packet(
                            rtde,
                            input_recipe,
                            input_types,
                            values,
                            kind=kind,
                            release=release,
                            output_fresh=output_fresh,
                            output_drained=drained,
                        )
                finally:
                    # A disabled next packet makes an active Stage25 path enter
                    # its own controlled safe-exit loop. This is not Dashboard
                    # Stop and is best effort during teardown.
                    abort_sequence = max(outgoing_sequence + 1, 1)
                    abort_pose = phase_equilibrium or initial_pose
                    try:
                        send_packet(
                            rtde,
                            input_recipe,
                            input_types,
                            build_values(MODE_DISABLED, abort_sequence, abort_pose),
                            kind="abort_disabled",
                        )
                    except (OSError, RuntimeError):
                        record_event("abort_disabled_failed")

            if not completed:
                raise RuntimeError("probe_timeout_or_tp_not_played")
            if phase_results != {"normal": 1.0, "sequence_fault": 13.0}:
                raise RuntimeError(f"unexpected_phase_results:{phase_results}")
            if STATE_ZERO_STARTUP not in saw_states or STATE_TORQUE not in saw_states:
                raise RuntimeError(f"direct_torque_states_missing:{sorted(saw_states)}")
            if STATE_SAFE_EXIT not in saw_states or FAULT_SEQUENCE not in saw_faults:
                raise RuntimeError(
                    f"sequence_fault_exit_missing:states={sorted(saw_states)},faults={sorted(saw_faults)}"
                )
            if FAULT_SEQUENCE not in faults_by_phase[2] or any(
                FAULT_SEQUENCE in faults_by_phase[value] for value in (1,)
            ):
                raise RuntimeError(f"sequence_fault_phase_binding:{faults_by_phase}")
            if not steptime_samples:
                raise RuntimeError("steptime_samples_missing")
            intervals = [
                right - left
                for left, right in zip(controller_timestamps, controller_timestamps[1:])
                if right > left
            ]
            success_result = {
                "schema": evidence_schema,
                "status": "passed",
                "program": program,
                "controller_program": controller_program,
                "started_at": started_at,
                "completed_at": datetime.now().astimezone().isoformat(),
                "explicit_gates": {
                    "run_probe": args.run_probe,
                    "write_rtde_inputs": args.write_rtde_inputs,
                    "tp_simulation_visible": args.tp_simulation_visible,
                    "auto_play_simulation": auto_play_simulation,
                },
                "initial_status": initial_status,
                "dashboard_play_result": dashboard_play_result,
                "sequence_pacing": sequence_pacing,
                "phase_sequence_stats": sequence_stats_snapshot(),
                "phase_results": phase_results,
                "phase_diagnostics": phase_diagnostics,
                "observed_playing_phase": observed_playing_phase,
                "phase_valid_acks": phase_valid_acks,
                "states_observed": sorted(saw_states),
                "faults_observed": sorted(saw_faults),
                "states_by_phase": {str(key): sorted(value) for key, value in states_by_phase.items()},
                "faults_by_phase": {str(key): sorted(value) for key, value in faults_by_phase.items()},
                "phase_equilibria": {
                    str(key): list(value) for key, value in phase_equilibria.items()
                },
                "samples": samples,
                "steptime_s": {
                    "minimum": min(steptime_samples),
                    "maximum": max(steptime_samples),
                    "mean": sum(steptime_samples) / len(steptime_samples),
                },
                "rtde_output_interval_s": {
                    "count": len(intervals),
                    "minimum": min(intervals) if intervals else None,
                    "maximum": max(intervals) if intervals else None,
                    "mean": sum(intervals) / len(intervals) if intervals else None,
                },
                "release_timing": schedule.timing() if schedule is not None else None,
                "scheduler": scheduler_metadata(),
                "no_motion_observation": {
                    "maximum_tcp_speed": max_tcp_speed,
                    "maximum_joint_speed": max_qd,
                    "maximum_tcp_position_delta_m": max_tcp_pose_delta,
                    "maximum_joint_position_delta_rad": max_joint_delta,
                },
                "last_controller_observation": current_observation(),
                "last_outgoing_packet": {
                    "mode": int(last_values[24]),
                    "sequence": int(last_values[25]),
                    "heartbeat": int(last_values[26]),
                    "lease_id": int(last_values[27]),
                    "equilibrium_bytes_hex": equilibrium_hex(last_values[:6]),
                },
                "events": recorder.snapshot(),
                "claim_boundary": (
                    "Physical 5.26 controller TP Simulation runtime/API and RTDE behavior only; "
                    "does not verify physical torque, contact, calibration, or data collection."
                ),
            }
        except BaseException:
            raise
    finally:
        try:
            final_dashboard_state = dashboard_snapshot(args.robot_host)
        except BaseException as exc:
            final_dashboard_state = {
                "error": f"{type(exc).__name__}: {exc}",
            }
        if success_result is None:
            failure = sys.exc_info()[1]
            payload: dict[str, Any] = {
                "schema": evidence_schema,
                "status": "failed",
                "program": program,
                "controller_program": controller_program,
                "started_at": started_at,
                "completed_at": datetime.now().astimezone().isoformat(),
                "explicit_gates": {
                    "run_probe": getattr(args, "run_probe", False),
                    "write_rtde_inputs": getattr(args, "write_rtde_inputs", False),
                    "tp_simulation_visible": getattr(args, "tp_simulation_visible", False),
                    "auto_play_simulation": auto_play_simulation,
                },
                "initial_status": initial_status,
                "dashboard_play_result": dashboard_play_result,
                "sequence_pacing": sequence_pacing,
                "phase_sequence_stats": sequence_stats_snapshot(),
                "phase_results": phase_results,
                "phase_diagnostics": phase_diagnostics,
                "observed_playing_phase": observed_playing_phase,
                "phase_valid_acks": phase_valid_acks,
                "states_observed": sorted(saw_states),
                "faults_observed": sorted(saw_faults),
                "states_by_phase": {str(key): sorted(value) for key, value in states_by_phase.items()},
                "faults_by_phase": {str(key): sorted(value) for key, value in faults_by_phase.items()},
                "phase_equilibria": {
                    str(key): list(value) for key, value in phase_equilibria.items()
                },
                "samples": samples,
                "release_timing": schedule.timing() if schedule is not None else None,
                "scheduler": scheduler_metadata(),
                "no_motion_observation": {
                    "maximum_tcp_speed": max_tcp_speed,
                    "maximum_joint_speed": max_qd,
                    "maximum_tcp_position_delta_m": max_tcp_pose_delta,
                    "maximum_joint_position_delta_rad": max_joint_delta,
                },
                "last_controller_observation": current_observation(),
                "last_outgoing_packet": {
                    "mode": int(last_values[24]),
                    "sequence": int(last_values[25]),
                    "heartbeat": int(last_values[26]),
                    "lease_id": int(last_values[27]),
                    "equilibrium_bytes_hex": equilibrium_hex(last_values[:6]),
                },
                "final_dashboard_state": final_dashboard_state,
                "events": recorder.snapshot(),
                "failure": (
                    {
                        "type": type(failure).__name__,
                        "message": str(failure),
                    }
                    if failure is not None
                    else {"type": "unknown", "message": "probe did not produce a result"}
                ),
                "claim_boundary": (
                    "Physical 5.26 controller TP Simulation runtime/API and RTDE behavior only; "
                    "does not verify physical torque, contact, calibration, or data collection."
                ),
            }
        else:
            payload = dict(success_result)
            payload["final_dashboard_state"] = final_dashboard_state
            payload["events"] = recorder.snapshot()
        payload["evidence_path"] = str(evidence_path)
        write_evidence(evidence_path, payload)
        if success_result is not None:
            success_result["final_dashboard_state"] = final_dashboard_state
            success_result["events"] = payload["events"]
            success_result["evidence_path"] = str(evidence_path)
    if success_result is None:
        raise RuntimeError("probe_failed_without_evidence")
    return success_result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", nargs="?", choices=("status", "start"), default="status")
    parser.add_argument("--robot-host", default="192.168.1.18")
    parser.add_argument("--program", default=PROGRAM)
    parser.add_argument(
        "--sequence-pacing",
        choices=(SEQUENCE_PACING_INDEPENDENT, SEQUENCE_PACING_ACK),
    )
    parser.add_argument("--connect-timeout-s", type=float, default=3.0)
    parser.add_argument("--wait-for-play-s", type=float, default=180.0)
    parser.add_argument("--valid-ticks", type=int, default=100)
    parser.add_argument("--fault-after-ticks", type=int, default=20)
    parser.add_argument("--tcp-speed-abort", type=float, default=0.0005)
    parser.add_argument("--joint-speed-abort", type=float, default=0.01)
    parser.add_argument("--tcp-position-abort", type=float, default=0.0002)
    parser.add_argument("--joint-position-abort", type=float, default=0.002)
    parser.add_argument("--evidence", type=Path)
    parser.add_argument("--run-probe", action="store_true")
    parser.add_argument("--write-rtde-inputs", action="store_true")
    parser.add_argument("--tp-simulation-visible", action="store_true")
    parser.add_argument("--auto-play-simulation", action="store_true")
    args = parser.parse_args(argv)
    if args.command == "status":
        print(
            json.dumps(
                readonly_status(args.robot_host, program=args.program),
                indent=2,
                sort_keys=True,
            )
        )
        return 0
    if args.valid_ticks < 6 or args.fault_after_ticks < 6:
        raise SystemExit("valid tick counts must be at least 6 to reach STATE_TORQUE")
    print(json.dumps(run_probe(args), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(130)
    except RuntimeError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, sort_keys=True), file=sys.stderr)
        raise SystemExit(2)
