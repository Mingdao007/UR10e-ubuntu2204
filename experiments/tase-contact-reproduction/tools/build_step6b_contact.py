#!/usr/bin/env python3
"""Generate Step6b contact 8-shaped baseline TP package."""

from __future__ import annotations

import argparse
import gzip
import json
import math
from datetime import datetime, timedelta, timezone
from typing import Any

from build_step4e_line_programs import CONFIG_PATH, generated_at, line_cfg, load_json
from build_step4e_p0p1_programs import build_urp, step4fg_seed_normal_loop_script
from tase_protocol_table import resolve_experiment_profile
from step6_eight import (
    ALONG_AMPLITUDE_M,
    CONTROLLER_DIR,
    LATERAL_AMPLITUDE_M,
    LOCAL_PROGRAM_DIR,
    OMEGA_RAD_S,
    PATH_DURATION_S,
    STEP6_SAFE_FRAME_PATH,
    STEP6_TABLE_PATH,
    load_safe_frame,
    reference_samples,
    step6_stage,
)


_STEP6_CONTACT_V1 = resolve_experiment_profile("Step6.contact_eight_v1")
_STEP6_CONTACT_V2 = resolve_experiment_profile("Step6.contact_eight")
LINE_RUNTIME_LIMIT_S = float(_STEP6_CONTACT_V2["parameters"]["line_runtime_limit_s"])
TARGET_FORCE_N = float(_STEP6_CONTACT_V2["parameters"]["target_force_n"])
ORIENTATION_RUNTIME_LIMIT_S = 8.0
ORIENTATION_IGNORE_ERROR_RAD = 0.052360
ORIENTATION_HARD_STOP_ERROR_RAD = 0.523599


def _variant_from_profile(profile: dict[str, Any], **extra: Any) -> dict[str, Any]:
    limits = profile["safety_limits"]
    return {
        **extra,
        "bridge_version": profile["experiment"]["bridge_version"],
        "motion_limit_m_s": float(limits["motion_limit_m_s"]),
        "total_linear_limit_m_s": float(limits["total_linear_limit_m_s"]),
        "normal_velocity_limit_m_s": float(limits["normal_velocity_limit_m_s"]),
        "angular_limit_rad_s": float(limits["angular_limit_rad_s"]),
    }


VARIANTS: dict[str, dict[str, Any]] = {
    "v1": _variant_from_profile(
        _STEP6_CONTACT_V1,
        program_name="step6b_contact_eight_baseline_v1",
        stage_id="step6_contact_eight_baseline_v1",
        title="Step6b contact 8-shaped baseline v1",
        stamp_suffix="STEP6B_CONTACT_EIGHT_BASELINE_V1",
    ),
    "v2": _variant_from_profile(
        _STEP6_CONTACT_V2,
        program_name="step6b_contact_eight_baseline_v2",
        stage_id="step6_contact_eight_baseline_v2",
        title="Step6b contact 8-shaped baseline v2",
        stamp_suffix="STEP6B_CONTACT_EIGHT_BASELINE_V2",
    ),
}
DEFAULT_VARIANT = "v2"
ACTIVE_VARIANT = VARIANTS[DEFAULT_VARIANT]

PROGRAM_NAME = str(ACTIVE_VARIANT["program_name"])
STEP6_STAGE_ID = str(ACTIVE_VARIANT["stage_id"])
BRIDGE_VERSION = str(ACTIVE_VARIANT["bridge_version"])


def variant_config(name: str) -> dict[str, Any]:
    try:
        return VARIANTS[name]
    except KeyError as exc:
        raise SystemExit(f"unknown Step6b variant {name!r}; choose one of {sorted(VARIANTS)}") from exc


def source_stamp(now: datetime, variant: dict[str, Any] = ACTIVE_VARIANT) -> str:
    return now.strftime(f"%Y-%m-%dT%H%MHKT_{variant['stamp_suffix']}")


def feasibility_metrics(frame: dict, variant: dict[str, Any]) -> dict[str, float | bool]:
    rows = reference_samples(frame, dt_s=0.002)
    speeds = [math.hypot(row["base_vx_m_s"], row["base_vy_m_s"]) for row in rows]
    xs = [row["base_x_m"] for row in rows]
    ys = [row["base_y_m"] for row in rows]
    max_ref_speed = max(speeds)
    normal_reserve = float(variant["normal_velocity_limit_m_s"])
    required_total = math.hypot(max_ref_speed, normal_reserve)
    angular_capacity = float(variant["angular_limit_rad_s"]) * ORIENTATION_RUNTIME_LIMIT_S
    angular_required = ORIENTATION_HARD_STOP_ERROR_RAD - ORIENTATION_IGNORE_ERROR_RAD
    return {
        "reference_speed_max_m_s": max_ref_speed,
        "reference_speed_mean_m_s": sum(speeds) / len(speeds),
        "reference_x_span_m": max(xs) - min(xs),
        "reference_y_span_m": max(ys) - min(ys),
        "normal_reserve_m_s": normal_reserve,
        "required_total_linear_m_s": required_total,
        "motion_limit_m_s": float(variant["motion_limit_m_s"]),
        "total_linear_limit_m_s": float(variant["total_linear_limit_m_s"]),
        "angular_limit_rad_s": float(variant["angular_limit_rad_s"]),
        "orientation_runtime_limit_s": ORIENTATION_RUNTIME_LIMIT_S,
        "orientation_capacity_rad": angular_capacity,
        "orientation_required_rad": angular_required,
        "linear_feasible": max_ref_speed <= float(variant["motion_limit_m_s"]) + 1e-12
        and required_total <= float(variant["total_linear_limit_m_s"]) + 1e-12,
        "angular_feasible": angular_capacity + 1e-12 >= angular_required,
    }


def assert_feasible(frame: dict, variant: dict[str, Any]) -> dict[str, float | bool]:
    metrics = feasibility_metrics(frame, variant)
    if not metrics["linear_feasible"]:
        raise RuntimeError(
            "Step6b profile infeasible: reference max "
            f"{float(metrics['reference_speed_max_m_s']) * 1000.0:.3f} mm/s and total-with-normal "
            f"{float(metrics['required_total_linear_m_s']) * 1000.0:.3f} mm/s exceed motion/total caps "
            f"{float(metrics['motion_limit_m_s']) * 1000.0:.3f}/"
            f"{float(metrics['total_linear_limit_m_s']) * 1000.0:.3f} mm/s"
        )
    if not metrics["angular_feasible"]:
        raise RuntimeError(
            "Step6b profile infeasible: 25.2 angular capacity "
            f"{float(metrics['orientation_capacity_rad']):.6f} rad is below required "
            f"{float(metrics['orientation_required_rad']):.6f} rad"
        )
    return metrics


def feasibility_comment(metrics: dict[str, float | bool]) -> str:
    return (
        "ref_max={:.3f}mm/s, total_with_{:.1f}mm/s_normal={:.3f}mm/s, "
        "xy_span=[{:.3f},{:.3f}]mm, angular_capacity={:.3f}deg"
    ).format(
        float(metrics["reference_speed_max_m_s"]) * 1000.0,
        float(metrics["normal_reserve_m_s"]) * 1000.0,
        float(metrics["required_total_linear_m_s"]) * 1000.0,
        float(metrics["reference_x_span_m"]) * 1000.0,
        float(metrics["reference_y_span_m"]) * 1000.0,
        math.degrees(float(metrics["orientation_capacity_rad"])),
    )


def build_script(
    stamp: str,
    gen_at: str,
    geom: dict[str, float],
    frame: dict,
    variant: dict[str, Any] = ACTIVE_VARIANT,
    metrics: dict[str, float | bool] | None = None,
) -> str:
    metrics = metrics or feasibility_metrics(frame, variant)
    basis = frame["basis"]
    guard = frame["guard"]
    entry_x, entry_y = [float(v) for v in basis["origin_xy_m"]]
    program_name = str(variant["program_name"])
    stage_id = str(variant["stage_id"])
    bridge_version = str(variant["bridge_version"])
    stage = step6_stage(stage_id)
    duration_s = float(stage["duration_s"])
    script = step4fg_seed_normal_loop_script(
        stamp=stamp,
        gen_at=gen_at,
        geom=geom,
        name=program_name,
        title=str(variant["title"]),
        version_token=bridge_version,
        path_shape="eight",
        path_formula=(
            f"Step6 table stage {stage_id}: local-basis along="
            f"{ALONG_AMPLITUDE_M:.2f}*sin({OMEGA_RAD_S:.1f}t), lateral="
            f"{LATERAL_AMPLITUDE_M:.2f}*sin({2.0 * OMEGA_RAD_S:.1f}t), "
            f"duration={duration_s:.0f}s"
        ),
        down_search_fn="codex_step6b_down_search",
    )
    replacements = {
        "# FLOW_TABLE: STEP4E_FLOW.md": (
            "# FLOW_TABLE: STEP6_FLOW.md\n"
            f"# STEP6_STAGE_ID: {stage_id}\n"
            f"# STEP6_PROGRAM: {program_name}\n"
            f"# STEP6_TABLE_SOURCE: {STEP6_TABLE_PATH.relative_to(STEP6_TABLE_PATH.parents[1])}\n"
            f"# STEP6_SAFE_FRAME_SOURCE: {STEP6_SAFE_FRAME_PATH.relative_to(STEP6_SAFE_FRAME_PATH.parents[1])}\n"
            f"# CONTACT_TARGET: --target-force-n {TARGET_FORCE_N:.1f}\n"
            f"# BRIDGE_LIMITS: step4e-motion-limit-m-s={float(variant['motion_limit_m_s']):.3f}, "
            f"step4e-total-linear-limit-m-s={float(variant['total_linear_limit_m_s']):.3f}, "
            f"step4e-normal-velocity-limit-m-s={float(variant['normal_velocity_limit_m_s']):.3f}, "
            f"step4e-angular-limit-rad-s={float(variant['angular_limit_rad_s']):.3f}\n"
            f"# OFFLINE_FEASIBILITY: {feasibility_comment(metrics)}\n"
            "# TP_ROLE: executor_and_guard_only; Step6 trajectory reference is computed by the bridge."
        ),
        "# PAPER_PATH_SOURCE: TASE paper Section VI-A; formula is used directly, not digitized from screenshots.\n": "",
        f"# ENTRY_XY_M: [{geom['start_x']:.9f}, {geom['start_y']:.9f}]": (
            f"# ENTRY_XY_M: [{entry_x:.9f}, {entry_y:.9f}]\n"
            "# SAFE_FRAME_SOURCE: config/step6_eight_safe_frame.json from five-point Step6 RTDE calibration.\n"
            "# NO_SCALE_POLICY: Step6 table frame uses rotation+translation only.\n"
            f"# X_GUARD: guard_line={float(guard['guard_line_x_m']):.9f} m, "
            f"path_max_x={float(guard['path_max_x_m']):.9f} m."
        ),
        f"local entry_x = {geom['start_x']:.9f}": f"local entry_x = {entry_x:.9f}",
        f"local entry_y = {geom['start_y']:.9f}": f"local entry_y = {entry_y:.9f}",
        "local line_runtime_limit_s = 65.000": f"local line_runtime_limit_s = {LINE_RUNTIME_LIMIT_S:.3f}",
        "local line_success_progress_m = 60.000000000": f"local line_success_progress_m = {PATH_DURATION_S:.9f}",
    }
    for old, new in replacements.items():
        if old not in script:
            raise RuntimeError(f"{program_name} scaffold replacement failed: {old}")
        script = script.replace(old, new, 1)
    script = script.replace(
        "paper-derived eight XY reference for 60 s",
        "Step6 safe-frame contact 8-shaped reference for 30 s",
    )
    script = script.replace(
        "PAPER_PATH_FORMULA:",
        "STEP6_PATH_FORMULA:",
    )
    return script


def build_txt(
    stamp: str,
    variant: dict[str, Any] = ACTIVE_VARIANT,
    metrics: dict[str, float | bool] | None = None,
) -> str:
    program_name = str(variant["program_name"])
    stage_id = str(variant["stage_id"])
    bridge_version = str(variant["bridge_version"])
    metrics = metrics or feasibility_metrics(load_safe_frame(), variant)
    return f"""Step6b contact 8-shaped baseline TP package

Open on Teach Pendant:
  {CONTROLLER_DIR}/{program_name}.urp

Version:
  {stamp}

Motion boundary:
  Contact motion.
  Reuses the current v31/Step5b contact search, first-contact normal latch,
  20 mm lift, 25.2 attitude correction, second contact, and 25.3 line-entry gate.
  Stage 25.0 consumes bridge command registers 37..44 only.
  Bridge profile: --step4e-version {bridge_version} --step4e-path-shape eight.
  Force target: --target-force-n {TARGET_FORCE_N:.1f}.
  Runtime: 30.0 s success threshold, 35.0 s guard.
  Bridge limits: motion {float(variant['motion_limit_m_s']) * 1000.0:.1f} mm/s,
  total linear {float(variant['total_linear_limit_m_s']) * 1000.0:.1f} mm/s,
  normal reserve {float(variant['normal_velocity_limit_m_s']) * 1000.0:.1f} mm/s,
  25.2 angular {float(variant['angular_limit_rad_s']):.3f} rad/s.
  Offline feasibility: {feasibility_comment(metrics)}.
  Raw normal guard: 50 N. Force norm guard: 60 N. Torque guard: 3.0 Nm.
  No UR zero_ftsensor(), no Kunwei tare/zero/config, no TCP/payload write.

Reference:
  STEP6_FLOW.md
  config/step6_stage_table.json stage {stage_id}
  config/step6_eight_safe_frame.json

Path:
  along=0.04*sin(0.2t), lateral=0.01*sin(0.4t), duration=30s
"""


def validate_package(
    script: str,
    txt: str,
    urp: bytes,
    stamp: str,
    variant: dict[str, Any] = ACTIVE_VARIANT,
) -> None:
    program_name = str(variant["program_name"])
    stage_id = str(variant["stage_id"])
    bridge_version = str(variant["bridge_version"])
    xml = gzip.decompress(urp).decode("utf-8")
    checks = {
        "script stamp": stamp in script,
        "txt stamp": stamp in txt,
        "program name": f'URProgram name="{program_name}"' in xml,
        "controller directory": f'directory="{CONTROLLER_DIR}"' in xml,
        "script file": f"{CONTROLLER_DIR}/{program_name}.script" in xml,
        "cached stamp": stamp in xml,
        "function name": f"def codex_{program_name}()" in script,
        "step6 flow": "STEP6_FLOW.md" in script and "STEP6_FLOW.md" in txt,
        "step6 stage": stage_id in script and stage_id in txt,
        "bridge profile": f"step4e-version={bridge_version}" in script
        and f"--step4e-version {bridge_version}" in txt
        and "--step4e-path-shape eight" in txt,
        "target force": f"--target-force-n {TARGET_FORCE_N:.1f}" in script
        and f"--target-force-n {TARGET_FORCE_N:.1f}" in txt,
        "bridge limits": f"step4e-motion-limit-m-s={float(variant['motion_limit_m_s']):.3f}" in script
        and f"step4e-total-linear-limit-m-s={float(variant['total_linear_limit_m_s']):.3f}" in script
        and f"step4e-angular-limit-rad-s={float(variant['angular_limit_rad_s']):.3f}" in script
        and "Offline feasibility:" in txt,
        "step6 table source": "STEP6_TABLE_SOURCE: config/step6_stage_table.json" in script,
        "step6 safe frame source": "STEP6_SAFE_FRAME_SOURCE: config/step6_eight_safe_frame.json" in script,
        "executor only": "TP_ROLE: executor_and_guard_only" in script,
        "30s contact runtime": "local line_runtime_limit_s = 35.000" in script
        and "local line_success_progress_m = 30.000000000" in script,
        "step6 formula": "along=0.04*sin(0.2t)" in script + txt
        and "lateral=0.01*sin(0.4t)" in script + txt,
        "v31 scaffold": "first-contact normal latch" in script
        and "25.2 attitude correction" in script
        and "25.3 line-entry gate" in script
        and "normal projection and force-loop composition" in script,
        "command registers": "read_input_float_register(37)" in script
        and "read_input_float_register(44)" in script,
        "raw guards": "codex_abs(normal_force) > 50.0" in script
        and "force_norm > 60.0" in script
        and "torque_norm > 3.0" in script,
        "no stale step4 step5 package names": "step4f_cycloid_seed_normal_v1" not in script
        and "step4g_eight_seed_normal_v1" not in script
        and "step5b_contact_cycloid_baseline_v1" not in script,
        "cached exact script": stamp in xml and f"def codex_{PROGRAM_NAME}()" in xml,
    }
    checks["cached exact script"] = stamp in xml and f"def codex_{program_name}()" in xml
    failed = [label for label, ok in checks.items() if not ok]
    if failed:
        raise RuntimeError(f"{program_name} validation failed: {failed}")


def write_outputs(stamp: str, gen_at: str, variant: dict[str, Any] = ACTIVE_VARIANT) -> dict[str, str]:
    frame = load_safe_frame()
    metrics = assert_feasible(frame, variant)
    geom = line_cfg(load_json(CONFIG_PATH))
    script = build_script(stamp, gen_at, geom, frame, variant, metrics)
    txt = build_txt(stamp, variant, metrics)
    program_name = str(variant["program_name"])
    urp = build_urp(script, program_name, CONTROLLER_DIR)
    validate_package(script, txt, urp, stamp, variant)

    LOCAL_PROGRAM_DIR.mkdir(parents=True, exist_ok=True)
    script_path = LOCAL_PROGRAM_DIR / f"{program_name}.script"
    txt_path = LOCAL_PROGRAM_DIR / f"{program_name}.txt"
    urp_path = LOCAL_PROGRAM_DIR / f"{program_name}.urp"

    script_path.write_text(script, encoding="utf-8")
    txt_path.write_text(txt, encoding="utf-8")
    urp_path.write_bytes(urp)
    return {
        "script": str(script_path),
        "txt": str(txt_path),
        "urp": str(urp_path),
        "controller_urp": f"{CONTROLLER_DIR}/{program_name}.urp",
        "stamp": stamp,
        "feasibility": feasibility_comment(metrics),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stamp-prefix", default=None)
    parser.add_argument("--variant", choices=sorted(VARIANTS), default=DEFAULT_VARIANT)
    args = parser.parse_args()
    variant = variant_config(args.variant)
    now = datetime.now(timezone(timedelta(hours=8)))
    stamp = args.stamp_prefix or source_stamp(now, variant)
    result = write_outputs(stamp, generated_at(now), variant)
    print(json.dumps({"generated": {variant["program_name"]: result}}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
