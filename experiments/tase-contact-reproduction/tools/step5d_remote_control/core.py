"""Minimal Step5d remote-control pure core with strict config contract."""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Mapping, Sequence

import yaml

RepositoryPath = str | Path
Vector = tuple[float, float, float]
Twist = tuple[float, float, float, float, float, float]


class Step5dRemoteCoreError(ValueError):
    pass


def expect_exact_keys(data: object, schema: object, path: str = "<root>") -> None:
    if isinstance(schema, dict):
        if not isinstance(data, Mapping):
            raise Step5dRemoteCoreError(f"{path}: expected mapping")
        if set(data) != set(schema):
            missing = sorted(set(schema) - set(data))
            extra = sorted(set(data) - set(schema))
            raise Step5dRemoteCoreError(f"{path}: missing={missing} extra={extra}")
        for key, nested in schema.items():
            expect_exact_keys(data[key], nested, f"{path}.{key}")
        return
    if not isinstance(schema, tuple):
        raise Step5dRemoteCoreError(f"{path}: invalid schema")
    if schema[0] == "str":
        if not isinstance(data, str):
            raise Step5dRemoteCoreError(f"{path}: expected str")
        if len(schema) > 1 and data not in schema[1]:
            raise Step5dRemoteCoreError(f"{path}: invalid enum")
        return
    if schema[0] in {"float", "int"}:
        if isinstance(data, bool) or not isinstance(data, (int, float)):
            raise Step5dRemoteCoreError(f"{path}: expected number")
        if schema[0] == "int" and not float(data).is_integer():
            raise Step5dRemoteCoreError(f"{path}: expected int")
        value = float(data)
        if not math.isfinite(value):
            raise Step5dRemoteCoreError(f"{path}: non-finite")
        if len(schema) > 1 and schema[1] == "pos" and value <= 0:
            raise Step5dRemoteCoreError(f"{path}: must be > 0")
        if len(schema) > 1 and schema[1] == "nonneg" and value < 0:
            raise Step5dRemoteCoreError(f"{path}: must be >= 0")
        return
    if schema[0] == "vec":
        if not isinstance(data, Sequence):
            raise Step5dRemoteCoreError(f"{path}: invalid vector")
        if len(data) != int(schema[1]):
            raise Step5dRemoteCoreError(f"{path}: invalid vector shape")
        for i, item in enumerate(data):
            if isinstance(item, bool) or not isinstance(item, (int, float)):
                raise Step5dRemoteCoreError(f"{path}[{i}]: expected number")
            if not math.isfinite(float(item)):
                raise Step5dRemoteCoreError(f"{path}[{i}]: non-finite")
        return
    raise Step5dRemoteCoreError(f"{path}: unknown schema entry")


_R012_SCHEMA: dict[str, object] = {
    "route": ("str",),
    "source": ("str",), "profile_id": ("str",), "run_duration_s": ("float", "pos"), "command_rate_hz": ("float", "pos"),
    "robot": {"ip": ("str",), "reverse_ip": ("str",), "controller_manager": ("str",)},
    "sensor": {
        "ip": ("str",),
        "port": ("int", "pos"),
        "window_s": ("float", "pos"),
        "latest_max_age_s": ("float", "pos"),
        "min_recent_samples": ("int", "pos"),
        "connect_timeout_s": ("float", "nonneg"),
        "recv_timeout_s": ("float", "pos"),
        "ready_timeout_s": ("float", "pos"),
        "baseline_s": ("float", "pos"),
        "rezero_s": ("float", "pos"),
    },
    "controller": {"command_topic": ("str",), "status_topic": ("str",), "joint_state_topic": ("str",)},
    "watchdog": {"command_stale_s": ("float", "pos"), "max_abs_velocity_rad_s": ("float", "pos"), "max_acceleration_rad_s2": ("float", "pos")},
    "force": {"target_force_n": ("float", "pos"), "hard_normal_force_n": ("float", "pos"), "max_force_norm_n": ("float", "pos"), "max_torque_norm_nm": ("float", "pos"), "p_gain": ("float", "pos"), "i_gain": ("float", "nonneg"), "damping": ("float", "pos"), "orientation_ko": ("float", "nonneg")},
    "path": {"shape": ("str", ("cycloid",)), "line_speed_m_s": ("float", "pos"), "path_gain": ("float", "pos"), "motion_limit_m_s": ("float", "pos"), "total_linear_limit_m_s": ("float", "pos"), "normal_velocity_limit_m_s": ("float", "pos"), "integral_limit_n_s": ("float", "pos"), "amplitude_m": ("float", "pos"), "omega_rad_s": ("float", "pos"), "basis": {"origin_xy_m": ("vec", 2), "u_along_xy": ("vec", 2), "p_lateral_xy": ("vec", 2)}},
    "guard": {"orientation_gain": ("float", "nonneg"), "angular_limit_rad_s": ("float", "pos"), "normal_follow_mode": ("str",), "normal_filter_tau_s": ("float", "nonneg"), "normal_filter_rate_rad_s": ("float", "pos"), "friction_projection": ("str", ("on", "off")), "min_force_n": ("float", "pos"), "min_force_for_control_n": ("float", "pos"), "contact_offset_min_fz_n": ("float", "nonneg"), "max_angle_from_latch_deg": ("float", "pos"), "rate_watchdog_max_miss_s": ("float", "nonneg")},
    "frame": {"reaction_normal_b": ("vec", 3), "approach_normal_b": ("vec", 3), "normal_axis": ("str", ("fz",)), "normal_sign": ("int",)},
    "calibration": {"expected_hash": ("str",)}, "kinematics": {"calibration_yaml": ("str",), "xacro_path": ("str",), "tcp_offset_tool0_m": ("vec", 3), "position_bound_gain_s_inv": ("float", "pos")},
    "solver": {"paper_truth_path": ("str",)}, "search": {"speed_m_s": ("float", "pos"), "timeout_s": ("float", "nonneg")},
    "preflight": {"prior_xyz": ("vec", 3), "prior_rotvec": ("vec", 3), "position_tolerance_m": ("float", "pos"), "orientation_tolerance_rad": ("float", "pos"), "qd_tolerance_rad_s": ("float", "pos"), "joint_state_stale_timeout_s": ("float", "pos"), "ready_timeout_s": ("float", "pos")},
    "canary": {"zero_s": ("float", "pos"), "free_space_s": ("float", "pos"), "guarded_contact_s": ("float", "pos"), "free_space_linear_limit_m_s": ("float", "pos"), "free_space_qdot_limit_rad_s": ("float", "pos"), "evidence_max_age_s": ("float", "pos")},
    "retract": {"distance_m": ("float", "pos"), "speed_m_s": ("float", "pos")},
    "preload": {"raw_min_n": ("float", "pos"), "raw_max_n": ("float", "pos"), "filtered_min_n": ("float", "pos"), "filtered_max_n": ("float", "pos"), "force_norm_max_n": ("float", "pos"), "hold_s": ("float", "nonneg"), "timeout_s": ("float", "nonneg")},
    "limits": {"qdot_limit_rad_s": ("float", "pos"), "host_slew_rad_s2": ("float", "pos")},
    "rates": {"rnn_epsilon": ("float", "pos"), "rnn_sigr_exponent_r": ("float", "pos"), "rnn_inner_iterations": ("int", "pos"), "rnn_backend": ("str", ("cupy",))}
}


def _float(value: object, path: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise Step5dRemoteCoreError(f"{path}: expected float")
    value = float(value)
    if not math.isfinite(value):
        raise Step5dRemoteCoreError(f"{path}: non-finite")
    return value


def _norm(v: Sequence[float]) -> float:
    return math.sqrt(sum(float(x) * float(x) for x in v))


def _normalize(v: Sequence[float], path: str) -> Vector:
    if len(v) != 3:
        raise Step5dRemoteCoreError(f"{path}: expected len=3")
    v = tuple(_float(item, f"{path}[{i}]") for i, item in enumerate(v))
    norm = _norm(v)
    if norm <= 0.0:
        raise Step5dRemoteCoreError(f"{path}: zero vector")
    return (float(v[0]) / norm, float(v[1]) / norm, float(v[2]) / norm)


def normalize_vector(v: Sequence[float]) -> Vector:
    return _normalize(v, "vector")


def _as_vector(value: object, path: str, n: int) -> tuple[float, ...]:
    if not isinstance(value, Sequence) or len(value) != n:
        raise Step5dRemoteCoreError(f"{path}: expected vector len {n}")
    return tuple(_float(v, f"{path}[{i}]") for i, v in enumerate(value))


def _slerp(a: Vector, b: Vector, t: float) -> Vector:
    if t <= 0.0:
        return a
    if t >= 1.0:
        return b
    cosine = max(-1.0, min(1.0, sum(x * y for x, y in zip(a, b))))
    angle = math.acos(cosine)
    if angle < 1e-12:
        return b
    sin_angle = math.sin(angle)
    if sin_angle < 1e-12:
        return b
    w0 = math.sin((1.0 - t) * angle) / sin_angle
    w1 = math.sin(t * angle) / sin_angle
    return _normalize((w0 * a[0] + w1 * b[0], w0 * a[1] + w1 * b[1], w0 * a[2] + w1 * b[2]), "slerp")


def rotate_toward(current: Sequence[float], target: Sequence[float], max_angle_rad: float) -> Vector:
    cur = normalize_vector(current)
    tgt = normalize_vector(target)
    if max_angle_rad <= 0.0:
        return cur
    cosine = max(-1.0, min(1.0, sum(a * b for a, b in zip(cur, tgt))))
    angle = math.acos(cosine)
    if angle <= max_angle_rad:
        return tgt
    frac = max_angle_rad / max(angle, 1e-12)
    return _slerp(cur, tgt, frac)


def load_r012_config(repo_root: RepositoryPath, config_path: str = "config/step5d_remote/r012.yaml") -> dict:
    path = Path(config_path)
    if not path.is_absolute():
        path = (Path(repo_root).resolve() / path).resolve()
    with path.open("r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    if not isinstance(cfg, Mapping):
        raise Step5dRemoteCoreError("r012.yaml must be mapping")
    expect_exact_keys(cfg, _R012_SCHEMA)
    r_raw = _as_vector(cfg["frame"]["reaction_normal_b"], "frame.reaction_normal_b", 3)
    a_raw = _as_vector(cfg["frame"]["approach_normal_b"], "frame.approach_normal_b", 3)
    if abs(_norm(r_raw) - 1.0) > 1e-6:
        raise Step5dRemoteCoreError("frame.reaction_normal_b must be unit")
    if abs(_norm(a_raw) - 1.0) > 1e-6:
        raise Step5dRemoteCoreError("frame.approach_normal_b must be unit")
    if cfg["frame"]["normal_sign"] != 1:
        raise Step5dRemoteCoreError("frame.normal_sign must be exactly 1")
    r = _normalize(r_raw, "frame.reaction_normal_b")
    a = _normalize(a_raw, "frame.approach_normal_b")
    if any(abs(r[i] + a[i]) > 1e-6 for i in range(3)):
        raise Step5dRemoteCoreError("reaction/approach must be opposite within 1e-6")
    if not (0.0 < cfg["rates"]["rnn_sigr_exponent_r"] <= 1.0):
        raise Step5dRemoteCoreError("rates.rnn_sigr_exponent_r must satisfy 0 < value <= 1")
    if cfg["preload"]["raw_min_n"] > cfg["preload"]["raw_max_n"]:
        raise Step5dRemoteCoreError("preload raw_min_n must be <= raw_max_n")
    if cfg["preload"]["filtered_min_n"] > cfg["preload"]["filtered_max_n"]:
        raise Step5dRemoteCoreError("preload filtered_min_n must be <= filtered_max_n")
    if cfg["guard"]["rate_watchdog_max_miss_s"] > cfg["watchdog"]["command_stale_s"]:
        raise Step5dRemoteCoreError("guard.rate_watchdog_max_miss_s must be <= watchdog.command_stale_s")
    for key in ("origin_xy_m", "u_along_xy", "p_lateral_xy"):
        if len(cfg["path"]["basis"][key]) != 2:
            raise Step5dRemoteCoreError(f"path.basis.{key} must be 2D")
    cfg = dict(cfg)
    cfg["frame"]["reaction_normal_b"] = list(r)
    cfg["frame"]["approach_normal_b"] = list(a)
    return cfg


def compute_outer_force_terms(force_cfg: Mapping[str, object]) -> dict[str, float]:
    p = _float(force_cfg["p_gain"], "force.p_gain")
    return {
        "Md": 1.0 / p,
        "kf": _float(force_cfg["i_gain"], "force.i_gain") / p,
        "Bd": _float(force_cfg["damping"], "force.damping") / p,
    }


def _friction_projection(v: Vector, cfg: Mapping[str, object]) -> Vector:
    basis = _as_vector(cfg["u_along_xy"], "path.basis.u_along_xy", 2)
    u = _normalize((basis[0], basis[1], 0.0), "path.basis.u_along_xy")
    dot = v[0] * u[0] + v[1] * u[1]
    projected = (v[0] - dot * u[0], v[1] - dot * u[1], v[2] - dot * u[2])
    if _norm(projected) <= 1e-12:
        raise Step5dRemoteCoreError("ReactionNormalFilter candidate degenerate")
    return normalize_vector(projected)


class ReactionNormalFilter:
    def __init__(self, cfg: Mapping[str, object]) -> None:
        guard = cfg["guard"]
        frame = cfg["frame"]
        self._min_force = _float(guard["min_force_n"], "guard.min_force_n")
        self._tau = _float(guard["normal_filter_tau_s"], "guard.normal_filter_tau_s")
        self._rate = _float(guard["normal_filter_rate_rad_s"], "guard.normal_filter_rate_rad_s")
        self._max_angle = math.radians(_float(guard["max_angle_from_latch_deg"], "guard.max_angle_from_latch_deg"))
        self._projection = str(guard["friction_projection"])
        self._path = cfg["path"]["basis"]
        self._prior = _normalize(frame["reaction_normal_b"], "frame.reaction_normal_b")
        self._filtered = self._prior

    def update(self, force_base_b: Sequence[float], load_force_n: float, *, dt: float) -> Vector:
        if dt < 0.0:
            raise Step5dRemoteCoreError("dt must be non-negative")
        if load_force_n < self._min_force:
            return self._filtered
        raw = _normalize(force_base_b, "force_base_b")
        try:
            if self._projection == "on":
                candidate = _friction_projection(raw, self._path)
            else:
                candidate = raw
        except Step5dRemoteCoreError:
            return self._filtered
        prior_dot_candidate = candidate[0] * self._prior[0] + candidate[1] * self._prior[1] + candidate[2] * self._prior[2]
        if prior_dot_candidate <= 0.0:
            return self._filtered
        angle_from_prior = math.acos(max(-1.0, min(1.0, prior_dot_candidate)))
        if angle_from_prior > self._max_angle:
            return self._filtered
        alpha = 1.0 if self._tau <= 0.0 else 1.0 - math.exp(-dt / self._tau)
        ema = _slerp(self._filtered, candidate, alpha)
        self._filtered = rotate_toward(self._filtered, ema, self._rate * dt)
        return self._filtered


def cycloid_xy_reference(elapsed_s: float, *, basis: Mapping[str, Sequence[float]], amplitude_m: float, omega_rad_s: float) -> tuple[float, float]:
    if elapsed_s < 0.0:
        raise Step5dRemoteCoreError("elapsed_s must be non-negative")
    phi = omega_rad_s * float(elapsed_s)
    ox, oy = basis["origin_xy_m"]
    ux, uy = basis["u_along_xy"]
    px, py = basis["p_lateral_xy"]
    local_x = amplitude_m * (phi - math.sin(phi))
    local_y = amplitude_m * (1.0 - math.cos(phi))
    return (float(ox) + float(ux) * local_x + float(px) * local_y, float(oy) + float(uy) * local_x + float(py) * local_y)


def apply_linear_caps(linear_twist: Sequence[float], angular_twist: Sequence[float], normal_axis_b: Sequence[float], *, tangent_limit_m_s: float, normal_limit_m_s: float, total_linear_limit_m_s: float, angular_limit_rad_s: float) -> tuple[Vector, tuple[float, float, float]]:
    if len(linear_twist) != 3 or len(angular_twist) != 3:
        raise Step5dRemoteCoreError("twists must be len-3")
    normal = _normalize(normal_axis_b, "normal_axis_b")
    lin = (float(linear_twist[0]), float(linear_twist[1]), float(linear_twist[2]))
    normal_component = sum(a * b for a, b in zip(lin, normal))
    tangent = (lin[0] - normal_component * normal[0], lin[1] - normal_component * normal[1], lin[2] - normal_component * normal[2])
    t_norm = _norm(tangent)
    n_norm = abs(normal_component)
    v_norm = _norm(lin)
    scale = 1.0
    if n_norm > normal_limit_m_s and n_norm > 0.0:
        scale = min(scale, normal_limit_m_s / n_norm)
    if t_norm > tangent_limit_m_s and t_norm > 0.0:
        scale = min(scale, tangent_limit_m_s / t_norm)
    if v_norm > total_linear_limit_m_s and v_norm > 0.0:
        scale = min(scale, total_linear_limit_m_s / v_norm)
    bounded_linear = (lin[0] * scale, lin[1] * scale, lin[2] * scale)
    bounded_angular = (float(angular_twist[0]), float(angular_twist[1]), float(angular_twist[2]))
    ang_norm = _norm(bounded_angular)
    if ang_norm > angular_limit_rad_s and ang_norm > 0.0:
        ratio = angular_limit_rad_s / ang_norm
        bounded_angular = (bounded_angular[0] * ratio, bounded_angular[1] * ratio, bounded_angular[2] * ratio)
    return bounded_linear, bounded_angular


def joint_omega_bounds(q: Sequence[float], q_min: Sequence[float], q_max: Sequence[float], *, alpha_s_inv: float, qdot_limit_rad_s: float) -> tuple[tuple[float, ...], tuple[float, ...]]:
    q_v = _as_vector(q, "q", 6)
    q_min_v = _as_vector(q_min, "q_min", 6)
    q_max_v = _as_vector(q_max, "q_max", 6)
    alpha = _float(alpha_s_inv, "alpha_s_inv")
    limit = _float(qdot_limit_rad_s, "qdot_limit_rad_s")
    lower = []
    upper = []
    for i in range(6):
        lo = max(alpha * (q_min_v[i] - q_v[i]), -limit)
        hi = min(limit, alpha * (q_max_v[i] - q_v[i]))
        if lo > hi:
            raise Step5dRemoteCoreError("joint bounds inverted")
        lower.append(lo)
        upper.append(hi)
    return tuple(lower), tuple(upper)


def desired_search_vectors(cfg: Mapping[str, object], phase_elapsed_s: float) -> tuple[Twist, Vector]:
    _ = phase_elapsed_s
    approach = _normalize(cfg["frame"]["approach_normal_b"], "frame.approach_normal_b")
    speed = _float(cfg["search"]["speed_m_s"], "search.speed_m_s")
    return (speed * approach[0], speed * approach[1], speed * approach[2], 0.0, 0.0, 0.0), approach


def desired_retract_vectors(cfg: Mapping[str, object], retract_progress: float) -> tuple[Twist, Vector]:
    progress = max(0.0, min(1.0, float(retract_progress)))
    reaction = _normalize(cfg["frame"]["reaction_normal_b"], "frame.reaction_normal_b")
    speed = _float(cfg["retract"]["speed_m_s"], "retract.speed_m_s")
    if progress >= 1.0:
        return (0.0, 0.0, 0.0, 0.0, 0.0, 0.0), reaction
    return (speed * reaction[0], speed * reaction[1], speed * reaction[2], 0.0, 0.0, 0.0), reaction


class RemotePhase(str, Enum):
    READY = "READY"
    SEARCH = "SEARCH"
    TRACK = "TRACK"
    RETRACT = "RETRACT"
    STOP = "STOP"
    ABORT = "ABORT"


@dataclass
class RemoteControlState:
    phase: RemotePhase = RemotePhase.READY
    phase_elapsed_s: float = 0.0
    search_elapsed_s: float = 0.0
    track_elapsed_s: float = 0.0


@dataclass(frozen=True)
class RemoteControlResult:
    phase: RemotePhase
    faulted: bool


class RemoteControlStateMachine:
    def __init__(self, cfg: Mapping[str, object]) -> None:
        self._cfg = cfg
        self._state = RemoteControlState()
        self._search_timeout = _float(cfg["search"]["timeout_s"], "search.timeout_s")
        self._track_timeout = _float(cfg["run_duration_s"], "run_duration_s")

    @property
    def state(self) -> RemoteControlState:
        return self._state

    def step(
        self,
        dt: float,
        *,
        preload_ready: bool,
        preload_fault: bool,
        retract_progress: float,
        fault: bool,
    ) -> RemoteControlResult:
        if dt < 0.0:
            raise Step5dRemoteCoreError("dt must be non-negative")
        if self._state.phase in {RemotePhase.STOP, RemotePhase.ABORT}:
            return RemoteControlResult(self._state.phase, self._state.phase == RemotePhase.ABORT)
        self._state.phase_elapsed_s += dt
        if fault or preload_fault:
            self._state.phase = RemotePhase.ABORT
            return RemoteControlResult(RemotePhase.ABORT, True)
        if self._state.phase == RemotePhase.READY:
            self._state.phase = RemotePhase.SEARCH
            return RemoteControlResult(RemotePhase.SEARCH, False)
        if self._state.phase == RemotePhase.SEARCH:
            self._state.search_elapsed_s += dt
            if self._state.search_elapsed_s >= self._search_timeout:
                self._state.phase = RemotePhase.ABORT
                return RemoteControlResult(self._state.phase, True)
            elif preload_ready:
                self._state.phase = RemotePhase.TRACK
            return RemoteControlResult(self._state.phase, False)
        if self._state.phase == RemotePhase.TRACK:
            self._state.track_elapsed_s += dt
            if self._state.track_elapsed_s >= self._track_timeout:
                self._state.phase = RemotePhase.RETRACT
            return RemoteControlResult(self._state.phase, False)
        if self._state.phase == RemotePhase.RETRACT:
            if retract_progress >= 1.0:
                self._state.phase = RemotePhase.STOP
            return RemoteControlResult(self._state.phase, False)
        raise Step5dRemoteCoreError("invalid phase")


class PreloadGate:
    def __init__(self, cfg: Mapping[str, object]) -> None:
        preload = cfg["preload"]
        self._raw_min = _float(preload["raw_min_n"], "preload.raw_min_n")
        self._raw_max = _float(preload["raw_max_n"], "preload.raw_max_n")
        self._filtered_min = _float(preload["filtered_min_n"], "preload.filtered_min_n")
        self._filtered_max = _float(preload["filtered_max_n"], "preload.filtered_max_n")
        self._force_norm_max = _float(preload["force_norm_max_n"], "preload.force_norm_max_n")
        self._hold_s = _float(preload["hold_s"], "preload.hold_s")
        self._fault = False
        self._timer = 0.0

    @property
    def fault(self) -> bool:
        return self._fault

    def reset(self) -> None:
        self._fault = False
        self._timer = 0.0

    def step(self, *, dt: float, raw_force_n: float, filtered_force_n: float, force_norm_n: float) -> bool:
        if dt < 0.0:
            raise Step5dRemoteCoreError("dt must be non-negative")
        if self._fault:
            return False
        raw = _float(raw_force_n, "raw_force_n")
        filtered = _float(filtered_force_n, "filtered_force_n")
        norm = _float(force_norm_n, "force_norm_n")
        if raw < self._raw_min or filtered < self._filtered_min:
            self._timer = 0.0
            return False
        if raw > self._raw_max or filtered > self._filtered_max or norm > self._force_norm_max:
            self._fault = True
            self._timer = 0.0
            return False
        self._timer = min(self._hold_s, self._timer + dt)
        return self._timer >= self._hold_s
