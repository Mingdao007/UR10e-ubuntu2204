"""Pure control-math core for the ROS2-remote Step5b live-contact runner.

Increment 1a of the staged ROS2-remote Step5b runner. This module is a FAITHFUL
port of the proven Step5b contact-control primitives from
`experiments/tase-contact-reproduction/tools/kunwei_rtde_bridge.py` (the 500Hz
RTDE speedl force loop that is the executable spec). It contains ONLY pure
functions — no `rclpy`, no sockets, no RTDE, no robot motion — so a future ROS2
node (increment 2) and the offline replay validator (increment 1b) both call the
same code.

Scope of 1a: the frame/vector helpers, the cycloid reference math, the live
normal candidate, and the v31 filtered-live normal — i.e. the layer where the
Step5d v4 force/frame semantic bug lived. The contact latch (`cmd_valid`) and the
force-tracking commanded twist live in `compute_bridge_values` and are ported in
increment 1b, where correctness is proven by replaying the bridge CSVs.

Faithfulness: each function below preserves the bridge's math, signs, units, and
fallbacks exactly. Provenance line numbers refer to `kunwei_rtde_bridge.py`.
"""

from __future__ import annotations

import math

# Step5b cycloid reference constants (kunwei_rtde_bridge.py step4e_path_reference,
# "cycloid" branch, lines ~618-627): phase = 0.1 * path_time_s, amplitude 0.015 m.
CYCLOID_OMEGA_RAD_S = 0.1
CYCLOID_AMPLITUDE_M = 0.015

# v31 filtered-live normal defaults (v31_filtered_live_normal, lines ~816-841).
V31_FILTER_ALPHA = 0.35
V31_MIN_FORCE_N = 2.0

Vec3 = tuple[float, float, float]


def clamp(value: float, lo: float, hi: float) -> float:
    """kunwei_rtde_bridge.py:448."""
    return lo if value < lo else hi if value > hi else value


def dot3(a: Vec3 | list[float], b: Vec3 | list[float]) -> float:
    """kunwei_rtde_bridge.py:652."""
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def cross3(a: Vec3 | list[float], b: Vec3 | list[float]) -> Vec3:
    """kunwei_rtde_bridge.py:656."""
    return (
        a[1] * b[2] - a[2] * b[1],
        a[2] * b[0] - a[0] * b[2],
        a[0] * b[1] - a[1] * b[0],
    )


def norm3(values: Vec3 | list[float]) -> float:
    """kunwei_rtde_bridge.py:667."""
    return math.sqrt(dot3(values, values))


def normalize3(values: Vec3 | list[float], fallback: Vec3 = (0.0, 0.0, 1.0)) -> Vec3:
    """kunwei_rtde_bridge.py:671."""
    length = norm3(values)
    if length < 1e-9:
        return fallback
    return (values[0] / length, values[1] / length, values[2] / length)


def mat_vec3(matrix: list[list[float]], vector: Vec3 | list[float]) -> Vec3:
    """kunwei_rtde_bridge.py:681."""
    return (dot3(matrix[0], vector), dot3(matrix[1], vector), dot3(matrix[2], vector))


def rotvec_to_matrix(rx: float, ry: float, rz: float) -> list[list[float]]:
    """kunwei_rtde_bridge.py:696 (Rodrigues)."""
    theta = math.sqrt(rx * rx + ry * ry + rz * rz)
    if theta < 1e-12:
        return [[1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0]]
    kx, ky, kz = rx / theta, ry / theta, rz / theta
    c = math.cos(theta)
    s = math.sin(theta)
    v = 1.0 - c
    return [
        [c + kx * kx * v, kx * ky * v - kz * s, kx * kz * v + ky * s],
        [ky * kx * v + kz * s, c + ky * ky * v, ky * kz * v - kx * s],
        [kz * kx * v - ky * s, kz * ky * v + kx * s, c + kz * kz * v],
    ]


def cycloid_reference(path_time_s: float) -> dict[str, float]:
    """Step5b cycloid reference in along/lateral frame coordinates.

    Faithful to kunwei_rtde_bridge.py step4e_path_reference "cycloid" branch
    (lines ~619-623). Returned along/lateral values are later mapped into the
    base frame via the Step5 safe-frame basis (ported in increment 1b alongside
    the latch/twist, where it is validated against the logged _step4e_desired_*
    columns). Kept frame-agnostic here so the math is independently testable.
    """
    phase = CYCLOID_OMEGA_RAD_S * path_time_s
    return {
        "phase_rad": phase,
        "along_m": CYCLOID_AMPLITUDE_M * (phase - math.sin(phase)),
        "lateral_m": CYCLOID_AMPLITUDE_M * (1.0 - math.cos(phase)),
        "along_v_m_s": CYCLOID_OMEGA_RAD_S * CYCLOID_AMPLITUDE_M * (1.0 - math.cos(phase)),
        "lateral_v_m_s": CYCLOID_OMEGA_RAD_S * CYCLOID_AMPLITUDE_M * math.sin(phase),
    }


def reaction_normal(filtered_normal_b: Vec3) -> Vec3:
    """Contact force-frame contract: reaction_normal carries positive load.

    The filtered live normal IS the reaction normal (unit vector along the
    sensed reaction force). Source contract:
    `/home/andy/.codex/context/ur-contact-force-frame-contract.md`.
    """
    return normalize3(filtered_normal_b)


def approach_normal(filtered_normal_b: Vec3) -> Vec3:
    """approach_normal = -reaction_normal (posture/press direction).

    This sign is the exact site of the Step5d v4 semantic bug; it is asserted in
    the unit tests. Do NOT normalize a raw force into a posture target.
    """
    r = reaction_normal(filtered_normal_b)
    return (-r[0], -r[1], -r[2])


def normal_load_n(force_base: Vec3, filtered_normal_b: Vec3) -> float:
    """normal_load_n = dot(force_base, reaction_normal). Positive = load."""
    return dot3(force_base, reaction_normal(filtered_normal_b))


def live_normal_candidate(force_b: Vec3, *, friction_projection: bool) -> tuple[Vec3, Vec3, float]:
    """kunwei_rtde_bridge.py:800. Returns (raw_normal_b, candidate_b, candidate_force_n).

    `tangent_b` uses the Step4e line unit; for the Step5b cycloid baseline the
    bridge runs with friction_projection disabled (filtered_live), so the
    tangent branch is inert. Kept faithful for parity with the source.
    """
    raw_normal_b = normalize3(force_b)
    candidate_force_b: Vec3 = force_b
    if friction_projection:
        tangent_b = (_STEP4E_LINE_UNIT_XY[0], _STEP4E_LINE_UNIT_XY[1], 0.0)
        tangent_load = dot3(force_b, tangent_b)
        candidate_force_b = tuple(force_b[idx] - tangent_load * tangent_b[idx] for idx in range(3))  # type: ignore[assignment]
    candidate_force_n = norm3(candidate_force_b)
    candidate_b = normalize3(candidate_force_b, raw_normal_b)
    return raw_normal_b, candidate_b, candidate_force_n


def v31_filtered_live_normal(
    filtered_current_b: Vec3,
    live_candidate_b: Vec3,
    live_candidate_force_n: float,
    *,
    sensor_ok: float,
    alpha: float = V31_FILTER_ALPHA,
    min_force_n: float = V31_MIN_FORCE_N,
) -> tuple[Vec3, str]:
    """kunwei_rtde_bridge.py:816. Holds on stale/low-force/reverse, else EMA-blends.

    The three hold conditions are the proven Step5b safety behavior; preserve
    them exactly.
    """
    if sensor_ok <= 0.5:
        return filtered_current_b, "hold_stale"
    if live_candidate_force_n < min_force_n:
        return filtered_current_b, "hold_low_force"
    if dot3(filtered_current_b, live_candidate_b) < 0.0:
        return filtered_current_b, "hold_reverse"
    alpha = clamp(alpha, 0.0, 1.0)
    blended = tuple(
        (1.0 - alpha) * filtered_current_b[idx] + alpha * live_candidate_b[idx] for idx in range(3)
    )
    return normalize3(blended, filtered_current_b), "filtered_live_alpha"  # type: ignore[arg-type]


# Step4e line unit (kunwei_rtde_bridge.py safe-frame constants). Only used by the
# inert friction-projection branch of live_normal_candidate for source parity.
_STEP4E_LINE_UNIT_XY: tuple[float, float] = (1.0, 0.0)
