"""Robot body truth — EE/TP kinematics vs host/dashboard claims.

Bottom-up primitive: sample + classify whether the arm is actually moving.
Does **not** drive motion, seal, or canary policy. Prefer ``--run-dir`` while
a live writer owns RTDE; ``sample_direct`` opens a second recipe and can fight
the host.

Agent contract: never treat Dashboard PLAYING / host phase marks alone as
proof of motion.
"""

from __future__ import annotations

import json
import math
import time
from dataclasses import asdict, dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Iterator, Mapping, Sequence

from step5d_autotune_v3.dashboard import dashboard_exchange
from step5d_autotune_v3.rtde_client import RTDEClient
from step5d_autotune_v4_r004.transport import OUTPUT_FIELDS, R004OutputSnapshot
from step5d_autotune_v4_r004.wire import TPState

from .state20_search_trace import STATE20_SEARCH_TRACE_SIDECAR_NAME

SCHEMA = "step5d.autotune-v4/r008-robot-body-truth-v1"
SNAPSHOT_FILENAME = "robot_body_truth.json"
PHASE_TIMINGS_NAME = "r008-phase-timings.jsonl"

STATIONARY_LINEAR_M_S = 0.0005
STATIONARY_ANGULAR_RAD_S = 0.005
# Search descent / PATH travel floors for "moving" verdicts (mm/s scale).
MOVING_SPEED_FLOOR_M_S = 0.001
CONTACT_FORCE_NORM_N = 1.0
CONTACT_NORMAL_ABS_N = 0.5
STALL_DZ_M = 0.0005  # 0.5 mm over the compare window
STALL_DXY_M = 0.0005

DASHBOARD_COMMANDS = (
    "is in remote control",
    "safetymode",
    "robotmode",
    "running",
    "programState",
    "get loaded program",
)


class BodyVerdict(str, Enum):
    MOVING_DOWN = "moving_down"
    MOVING_PATH = "moving_path"
    CONTACTED_STALLED = "contacted_stalled"
    HOLDING_READY = "holding_ready"
    PROGRAM_STOPPED = "program_stopped"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class MotionDiff:
    dt_s: float
    dz_m: float
    dxy_m: float
    speed_proxy_m_s: float

    def as_dict(self) -> dict[str, float]:
        return asdict(self)


@dataclass(frozen=True)
class RobotBodyTruth:
    """One body-truth snapshot (JSON-serializable via ``as_dict``)."""

    schema: str
    wall_time_s: float
    source: str
    tp_state: int | None
    command_mode: int | None
    packet_sequence: int | None
    tcp_pose_m_rad: tuple[float, float, float, float, float, float] | None
    tcp_speed_m_s_rad_s: tuple[float, float, float, float, float, float] | None
    linear_speed_m_s: float | None
    vz_m_s: float | None
    stationary: bool | None
    normal_load_n: float | None
    force_norm_n: float | None
    sensor_fresh: bool | None
    program_state: str | None
    program_running: bool | None
    safety_mode: str | None
    robot_mode: str | None
    host_claim_phase: str | None
    path_rows: int | None
    note: str | None = None

    def as_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "schema": self.schema,
            "wall_time_s": self.wall_time_s,
            "source": self.source,
            "tp_state": self.tp_state,
            "command_mode": self.command_mode,
            "packet_sequence": self.packet_sequence,
            "tcp_pose_m_rad": list(self.tcp_pose_m_rad) if self.tcp_pose_m_rad else None,
            "tcp_speed_m_s_rad_s": (
                list(self.tcp_speed_m_s_rad_s) if self.tcp_speed_m_s_rad_s else None
            ),
            "linear_speed_m_s": self.linear_speed_m_s,
            "vz_m_s": self.vz_m_s,
            "stationary": self.stationary,
            "normal_load_n": self.normal_load_n,
            "force_norm_n": self.force_norm_n,
            "sensor_fresh": self.sensor_fresh,
            "program_state": self.program_state,
            "program_running": self.program_running,
            "safety_mode": self.safety_mode,
            "robot_mode": self.robot_mode,
            "host_claim_phase": self.host_claim_phase,
            "path_rows": self.path_rows,
        }
        if self.note:
            out["note"] = self.note
        return out


def _finite(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _pose6(value: Any) -> tuple[float, float, float, float, float, float] | None:
    if not isinstance(value, (list, tuple)) or len(value) != 6:
        return None
    out: list[float] = []
    for item in value:
        number = _finite(item)
        if number is None:
            return None
        out.append(number)
    return (out[0], out[1], out[2], out[3], out[4], out[5])


def _linear_speed(speed: Sequence[float] | None) -> float | None:
    if speed is None or len(speed) < 3:
        return None
    return math.sqrt(sum(float(v) * float(v) for v in speed[:3]))


def _angular_speed(speed: Sequence[float] | None) -> float | None:
    if speed is None or len(speed) < 6:
        return None
    return math.sqrt(sum(float(v) * float(v) for v in speed[3:6]))


def _stationary_from_speed(speed: Sequence[float] | None) -> bool | None:
    lin = _linear_speed(speed)
    ang = _angular_speed(speed)
    if lin is None or ang is None:
        return None
    return lin <= STATIONARY_LINEAR_M_S and ang <= STATIONARY_ANGULAR_RAD_S


def _contact_force(snap: RobotBodyTruth) -> bool:
    fn = snap.force_norm_n
    nl = snap.normal_load_n
    if fn is not None and fn >= CONTACT_FORCE_NORM_N:
        return True
    if nl is not None and abs(nl) >= CONTACT_NORMAL_ABS_N:
        return True
    return False


def diff_motion(a: RobotBodyTruth, b: RobotBodyTruth) -> MotionDiff:
    """Kinematic delta from ``a`` → ``b`` (pose required on both)."""

    if a.tcp_pose_m_rad is None or b.tcp_pose_m_rad is None:
        raise ValueError("diff_motion requires tcp_pose_m_rad on both snapshots")
    ta = a.wall_time_s
    tb = b.wall_time_s
    # Prefer monotonic from note? wall_time may be equal for synthetic rows —
    # callers can set wall_time_s to monotonic for run-dir rows.
    dt = tb - ta
    if not math.isfinite(dt) or dt <= 0.0:
        raise ValueError(f"diff_motion needs increasing time, got dt={dt}")
    dz = b.tcp_pose_m_rad[2] - a.tcp_pose_m_rad[2]
    dxy = math.hypot(
        b.tcp_pose_m_rad[0] - a.tcp_pose_m_rad[0],
        b.tcp_pose_m_rad[1] - a.tcp_pose_m_rad[1],
    )
    speed = math.hypot(dz, dxy) / dt
    return MotionDiff(dt_s=dt, dz_m=dz, dxy_m=dxy, speed_proxy_m_s=speed)


def _dashboard_fields(observation: Mapping[str, str]) -> dict[str, Any]:
    running_raw = observation.get("running", "")
    program_running: bool | None = None
    if running_raw.lower().startswith("program running:"):
        value = running_raw.split(":", 1)[1].strip().lower()
        if value in {"true", "false"}:
            program_running = value == "true"
    return {
        "program_state": observation.get("programState"),
        "program_running": program_running,
        "safety_mode": observation.get("safetymode"),
        "robot_mode": observation.get("robotmode"),
    }


def observe_dashboard(robot_host: str, *, timeout_s: float = 2.0) -> dict[str, Any]:
    raw = dashboard_exchange(robot_host, list(DASHBOARD_COMMANDS), timeout=timeout_s)
    return _dashboard_fields(raw)


def snapshot_from_r004(
    output: R004OutputSnapshot,
    *,
    source: str = "direct_rtde",
    dashboard: Mapping[str, Any] | None = None,
    normal_load_n: float | None = None,
    force_norm_n: float | None = None,
    sensor_fresh: bool | None = None,
    command_mode: int | None = None,
    host_claim_phase: str | None = None,
    path_rows: int | None = None,
    note: str | None = None,
) -> RobotBodyTruth:
    speed = output.tcp_speed_m_s_rad_s
    dash = dict(dashboard or {})
    tp_state = int(output.integer_echoes.get(26)) if 26 in output.integer_echoes else None
    return RobotBodyTruth(
        schema=SCHEMA,
        wall_time_s=float(output.observed_at_s),
        source=source,
        tp_state=tp_state,
        command_mode=command_mode,
        packet_sequence=int(output.consumed_packet_sequence),
        tcp_pose_m_rad=output.tcp_pose_m_rad,
        tcp_speed_m_s_rad_s=speed,
        linear_speed_m_s=_linear_speed(speed),
        vz_m_s=float(speed[2]) if speed is not None else None,
        stationary=output.stationary,
        normal_load_n=_finite(normal_load_n),
        force_norm_n=_finite(force_norm_n),
        sensor_fresh=sensor_fresh,
        program_state=dash.get("program_state"),
        program_running=dash.get("program_running"),
        safety_mode=dash.get("safety_mode"),
        robot_mode=dash.get("robot_mode"),
        host_claim_phase=host_claim_phase,
        path_rows=path_rows,
        note=note,
    )


def snapshot_from_state20_row(
    row: Mapping[str, Any],
    *,
    source: str = "run_dir_state20",
    dashboard: Mapping[str, Any] | None = None,
    host_claim_phase: str | None = None,
    path_rows: int | None = None,
) -> RobotBodyTruth:
    pose = _pose6(row.get("tcp_pose_m_rad"))
    wall = _finite(row.get("wall_time_s"))
    if wall is None:
        wall = _finite(row.get("monotonic_s"))
    if wall is None:
        wall = time.time()
    speed = _pose6(row.get("tcp_speed_m_s_rad_s"))  # usually absent
    dash = dict(dashboard or {})
    return RobotBodyTruth(
        schema=SCHEMA,
        wall_time_s=float(wall),
        source=source,
        tp_state=int(row["tp_state"]) if row.get("tp_state") is not None else None,
        command_mode=int(row["command_mode"]) if row.get("command_mode") is not None else None,
        packet_sequence=(
            int(row["packet_sequence"]) if row.get("packet_sequence") is not None else None
        ),
        tcp_pose_m_rad=pose,
        tcp_speed_m_s_rad_s=speed,
        linear_speed_m_s=_linear_speed(speed),
        vz_m_s=float(speed[2]) if speed is not None else None,
        stationary=_stationary_from_speed(speed),
        normal_load_n=_finite(row.get("normal_load_n")),
        force_norm_n=_finite(row.get("force_norm_n")),
        sensor_fresh=bool(row["sensor_fresh"]) if "sensor_fresh" in row else None,
        program_state=dash.get("program_state"),
        program_running=dash.get("program_running"),
        safety_mode=dash.get("safety_mode"),
        robot_mode=dash.get("robot_mode"),
        host_claim_phase=host_claim_phase,
        path_rows=path_rows,
    )


def verdict(
    snap: RobotBodyTruth,
    *,
    prior: RobotBodyTruth | None = None,
    motion: MotionDiff | None = None,
) -> BodyVerdict:
    """Classify body state. Never returns moving_* from program_running alone."""

    if snap.program_running is False or (
        snap.program_state is not None and str(snap.program_state).upper().startswith("STOPPED")
    ):
        if snap.tp_state == int(TPState.STOPPED) or snap.command_mode == 4:
            return BodyVerdict.PROGRAM_STOPPED
        # Dashboard stopped is enough when body feed is thin.
        if snap.tcp_pose_m_rad is None:
            return BodyVerdict.PROGRAM_STOPPED

    if snap.tp_state == int(TPState.STOPPED) or snap.command_mode == 4:
        # STOP with contact load still counts as stalled contact for agents.
        if _contact_force(snap) and snap.tp_state in {
            int(TPState.CONTACT_ACQUISITION),
            int(TPState.BASELINE),
            int(TPState.STOPPED),
        }:
            if prior is not None and motion is None:
                try:
                    motion = diff_motion(prior, snap)
                except ValueError:
                    motion = None
            if motion is not None and abs(motion.dz_m) <= STALL_DZ_M:
                return BodyVerdict.CONTACTED_STALLED
        return BodyVerdict.PROGRAM_STOPPED

    delta = motion
    if delta is None and prior is not None and prior.tcp_pose_m_rad and snap.tcp_pose_m_rad:
        try:
            delta = diff_motion(prior, snap)
        except ValueError:
            delta = None

    tp = snap.tp_state
    in_search = tp in {int(TPState.CONTACT_ACQUISITION), int(TPState.BASELINE)}
    in_path = tp == int(TPState.PATH)
    ready = tp in {int(TPState.READY_HOME_NEXT), int(TPState.WAITING_FOR_PLAY)}

    speed_ok = False
    if snap.linear_speed_m_s is not None:
        speed_ok = snap.linear_speed_m_s >= MOVING_SPEED_FLOOR_M_S
    if delta is not None and delta.speed_proxy_m_s >= MOVING_SPEED_FLOOR_M_S:
        speed_ok = True

    if in_search and _contact_force(snap):
        stalled = False
        if snap.stationary is True:
            stalled = True
        if delta is not None and abs(delta.dz_m) <= STALL_DZ_M:
            stalled = True
        if snap.vz_m_s is not None and abs(snap.vz_m_s) < MOVING_SPEED_FLOOR_M_S:
            if delta is None or abs(delta.dz_m) <= STALL_DZ_M:
                stalled = True
        if stalled:
            return BodyVerdict.CONTACTED_STALLED

    if in_search and speed_ok:
        # Prefer negative-Z as descent signal when available.
        if snap.vz_m_s is not None and snap.vz_m_s <= -MOVING_SPEED_FLOOR_M_S:
            return BodyVerdict.MOVING_DOWN
        if delta is not None and delta.dz_m <= -STALL_DZ_M:
            return BodyVerdict.MOVING_DOWN
        if speed_ok:
            return BodyVerdict.MOVING_DOWN

    if in_path and speed_ok:
        return BodyVerdict.MOVING_PATH
    if in_path and delta is not None and delta.dxy_m >= STALL_DXY_M:
        return BodyVerdict.MOVING_PATH

    if ready or (snap.stationary is True and not _contact_force(snap)):
        return BodyVerdict.HOLDING_READY

    if snap.stationary is True and in_search and not _contact_force(snap):
        return BodyVerdict.HOLDING_READY

    return BodyVerdict.UNKNOWN


def format_human(snap: RobotBodyTruth, v: BodyVerdict) -> str:
    pose = snap.tcp_pose_m_rad
    z = f"{pose[2]:.4f}" if pose else "?"
    vz = f"{snap.vz_m_s:.4f}" if snap.vz_m_s is not None else "?"
    f_n = f"{snap.force_norm_n:.2f}" if snap.force_norm_n is not None else "?"
    tp = snap.tp_state if snap.tp_state is not None else "?"
    dash = "PLAYING" if snap.program_running else (
        str(snap.program_state).split()[0] if snap.program_state else "?"
    )
    claim = snap.host_claim_phase or "n/a"
    mismatch = ""
    if v == BodyVerdict.CONTACTED_STALLED and snap.program_running:
        mismatch = " | dash=PLAYING claim≠body"
    elif v in {BodyVerdict.MOVING_DOWN, BodyVerdict.MOVING_PATH}:
        mismatch = ""
    elif snap.program_running and v == BodyVerdict.HOLDING_READY:
        mismatch = " | dash=PLAYING (hold)"
    return (
        f"tp={tp} z={z} vz≈{vz} F≈{f_n} {v.value} | dash={dash} "
        f"claim={claim}{mismatch} [{snap.source}]"
    )


def sample_direct(
    robot_host: str,
    *,
    include_dashboard: bool = True,
    note: str | None = None,
) -> RobotBodyTruth:
    """One-shot RTDE sample. Unsafe while the live writer owns RTDE."""

    warn = note or "WARN: may fight live writer if host owns RTDE"
    dash = observe_dashboard(robot_host) if include_dashboard else {}
    with RTDEClient(robot_host, timeout=3.0) as client:
        client.negotiate(version=2)
        recipe, types = client.setup_outputs(500.0, OUTPUT_FIELDS)
        client.start()
        raw = dict(
            zip(OUTPUT_FIELDS, client.recv_recipe_sample(recipe, types), strict=True)
        )
    snap = R004OutputSnapshot.from_mapping(time.monotonic(), raw)
    return snapshot_from_r004(snap, source="direct_rtde", dashboard=dash, note=warn)


def _read_jsonl_tail(path: Path, *, max_rows: int = 64) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    lines = path.read_text(encoding="utf-8").splitlines()
    rows: list[dict[str, Any]] = []
    for line in lines[-max_rows:]:
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


def _host_claim_from_run_dir(run_dir: Path) -> tuple[str | None, int | None]:
    path_rows = 0
    last_phase: str | None = None
    pt = run_dir / PHASE_TIMINGS_NAME
    for row in _read_jsonl_tail(pt, max_rows=200):
        if row.get("schema") == "step5d.autotune-v4/r008-phase-timings-v1":
            d = row.get("durations_s") or {}
            if d.get("path_60_s") is not None and d.get("dispatch_s") is not None:
                path_rows += 1
            marks = row.get("marks") or row.get("phase") or row.get("kind")
            if marks is not None:
                last_phase = str(marks)
            elif row.get("attempt_kind"):
                last_phase = str(row.get("attempt_kind"))
    # Peek host.log tail; pick the chronologically last known phase token.
    host_log = run_dir / "host.log"
    if host_log.is_file():
        text = host_log.read_text(encoding="utf-8", errors="replace")[-8000:]
        tokens = (
            "CONTACT_SEARCH",
            "STAGE25",
            "SAFE_RETURN",
            "DISPATCH",
            "ARM",
            "HOME",
            "QUEUE_COMPLETE",
            "SEAL",
        )
        best_idx = -1
        best_token: str | None = None
        for token in tokens:
            idx = text.rfind(token)
            if idx > best_idx:
                best_idx = idx
                best_token = token
        if best_token is not None:
            last_phase = best_token
    return last_phase, path_rows if path_rows else None


def sample_from_run_dir(run_dir: Path | str) -> RobotBodyTruth:
    """Read newest body evidence without opening RTDE."""

    root = Path(run_dir)
    claim, path_rows = _host_claim_from_run_dir(root)

    snap_path = root / SNAPSHOT_FILENAME
    if snap_path.is_file():
        try:
            doc = json.loads(snap_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            return RobotBodyTruth(
                schema=SCHEMA,
                wall_time_s=time.time(),
                source="run_dir_snapshot",
                tp_state=None,
                command_mode=None,
                packet_sequence=None,
                tcp_pose_m_rad=None,
                tcp_speed_m_s_rad_s=None,
                linear_speed_m_s=None,
                vz_m_s=None,
                stationary=None,
                normal_load_n=None,
                force_norm_n=None,
                sensor_fresh=None,
                program_state=None,
                program_running=None,
                safety_mode=None,
                robot_mode=None,
                host_claim_phase=claim,
                path_rows=path_rows,
                note=f"snapshot_json_error:{exc}",
            )
        if isinstance(doc, Mapping):
            return snapshot_from_state20_row(
                doc,
                source="run_dir_snapshot",
                host_claim_phase=claim,
                path_rows=path_rows,
            )

    state20 = root / STATE20_SEARCH_TRACE_SIDECAR_NAME
    rows = _read_jsonl_tail(state20, max_rows=8)
    if rows:
        return snapshot_from_state20_row(
            rows[-1],
            source="run_dir_state20",
            host_claim_phase=claim,
            path_rows=path_rows,
        )

    return RobotBodyTruth(
        schema=SCHEMA,
        wall_time_s=time.time(),
        source="run_dir_empty",
        tp_state=None,
        command_mode=None,
        packet_sequence=None,
        tcp_pose_m_rad=None,
        tcp_speed_m_s_rad_s=None,
        linear_speed_m_s=None,
        vz_m_s=None,
        stationary=None,
        normal_load_n=None,
        force_norm_n=None,
        sensor_fresh=None,
        program_state=None,
        program_running=None,
        safety_mode=None,
        robot_mode=None,
        host_claim_phase=claim,
        path_rows=path_rows,
        note="no body feed (no robot_body_truth.json / state20 sidecar)",
    )


def sample_pair_from_run_dir(
    run_dir: Path | str, *, min_dt_s: float = 0.05
) -> tuple[RobotBodyTruth, RobotBodyTruth] | None:
    """Return two state20 rows spanning at least ``min_dt_s`` when available."""

    root = Path(run_dir)
    rows = _read_jsonl_tail(root / STATE20_SEARCH_TRACE_SIDECAR_NAME, max_rows=64)
    if len(rows) < 2:
        return None
    claim, path_rows = _host_claim_from_run_dir(root)
    newest = snapshot_from_state20_row(
        rows[-1], source="run_dir_state20", host_claim_phase=claim, path_rows=path_rows
    )
    prior_row = rows[-2]
    for row in reversed(rows[:-1]):
        cand = snapshot_from_state20_row(
            row, source="run_dir_state20", host_claim_phase=claim, path_rows=path_rows
        )
        try:
            if diff_motion(cand, newest).dt_s >= min_dt_s:
                prior_row = row
                break
        except ValueError:
            continue
    prior = snapshot_from_state20_row(
        prior_row, source="run_dir_state20", host_claim_phase=claim, path_rows=path_rows
    )
    return prior, newest


def classify_run_dir(run_dir: Path | str) -> tuple[RobotBodyTruth, BodyVerdict, MotionDiff | None]:
    pair = sample_pair_from_run_dir(run_dir)
    if pair is None:
        snap = sample_from_run_dir(run_dir)
        return snap, verdict(snap), None
    prior, snap = pair
    try:
        motion = diff_motion(prior, snap)
    except ValueError:
        motion = None
    return snap, verdict(snap, prior=prior, motion=motion), motion


def watch_run_dir(
    run_dir: Path | str, *, interval_s: float = 0.5
) -> Iterator[tuple[RobotBodyTruth, BodyVerdict, MotionDiff | None]]:
    while True:
        yield classify_run_dir(run_dir)
        time.sleep(max(0.05, float(interval_s)))


def watch_direct(
    robot_host: str, *, interval_s: float = 0.5, include_dashboard: bool = True
) -> Iterator[tuple[RobotBodyTruth, BodyVerdict, MotionDiff | None]]:
    prior: RobotBodyTruth | None = None
    while True:
        snap = sample_direct(robot_host, include_dashboard=include_dashboard)
        motion = None
        if prior is not None and prior.tcp_pose_m_rad and snap.tcp_pose_m_rad:
            try:
                motion = diff_motion(prior, snap)
            except ValueError:
                motion = None
        yield snap, verdict(snap, prior=prior, motion=motion), motion
        prior = snap
        time.sleep(max(0.05, float(interval_s)))


__all__ = [
    "SCHEMA",
    "SNAPSHOT_FILENAME",
    "BodyVerdict",
    "MotionDiff",
    "RobotBodyTruth",
    "classify_run_dir",
    "diff_motion",
    "format_human",
    "observe_dashboard",
    "sample_direct",
    "sample_from_run_dir",
    "sample_pair_from_run_dir",
    "snapshot_from_r004",
    "snapshot_from_state20_row",
    "verdict",
    "watch_direct",
    "watch_run_dir",
]
