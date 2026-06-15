#!/usr/bin/env python3
"""Generate Step5b contact cycloid baseline TP package."""

from __future__ import annotations

import argparse
import gzip
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from build_step4e_line_programs import CONFIG_PATH, PROGRAM_DIR, generated_at, line_cfg, load_json
from build_step4e_p0p1_programs import build_urp, step4fg_seed_normal_loop_script
from step5_table import load_stage_frame, step5_stage


PROGRAM_NAME = "step5b_contact_cycloid_baseline_v1"
STEP5_STAGE_ID = "step5_contact_cycloid_baseline_v1"
BRIDGE_VERSION = "step5b_v1"
LOCAL_PROGRAM_DIR = PROGRAM_DIR / "step5"
CONTROLLER_DIR = "/programs/andyl/kunwei/step5"
SAFE_FRAME_PATH = CONFIG_PATH.with_name("step5_safe_frame.json")


def source_stamp(now: datetime) -> str:
    return now.strftime("%Y-%m-%dT%H%MHKT_STEP5B_CONTACT_CYCLOID_BASELINE_V1")


def load_safe_frame() -> dict:
    return load_stage_frame(step5_stage(STEP5_STAGE_ID))


def build_script(stamp: str, gen_at: str, geom: dict[str, float], frame: dict) -> str:
    basis = frame["basis"]
    guard = frame["guard"]
    entry_x, entry_y = [float(v) for v in basis["origin_xy_m"]]
    stage = step5_stage(STEP5_STAGE_ID)
    duration_s = float(stage["duration_s"])
    omega = float(stage["phase_law"]["omega_rad_s"])
    amplitude = float(stage["amplitude_m"])
    script = step4fg_seed_normal_loop_script(
        stamp=stamp,
        gen_at=gen_at,
        geom=geom,
        name=PROGRAM_NAME,
        title="Step5b contact cycloid baseline v1",
        version_token=BRIDGE_VERSION,
        path_shape="cycloid",
        path_formula=(
            f"Step5 table stage {STEP5_STAGE_ID}: local-basis x={amplitude:.3f} * "
            f"({omega:.1f}t - sin({omega:.1f}t)), y={amplitude:.3f} * "
            f"(1 - cos({omega:.1f}t)), duration={duration_s:.0f}s"
        ),
        down_search_fn="codex_step5b_down_search",
    )
    replacements = {
        "# FLOW_TABLE: STEP4E_FLOW.md": (
            "# FLOW_TABLE: STEP5_FLOW.md\n"
            f"# STEP5_STAGE_ID: {STEP5_STAGE_ID}\n"
            "# STEP5_TABLE_SOURCE: config/step5_stage_table.json\n"
            "# TP_ROLE: executor_and_guard_only; Step5 trajectory reference is computed by the bridge."
        ),
        "# PAPER_PATH_SOURCE: TASE paper Section VI-A; formula is used directly, not digitized from screenshots.\n": "",
        f"# ENTRY_XY_M: [{geom['start_x']:.9f}, {geom['start_y']:.9f}]": (
            f"# ENTRY_XY_M: [{entry_x:.9f}, {entry_y:.9f}]\n"
            "# SAFE_FRAME_SOURCE: config/step5_safe_frame.json from confirmed drag-teach start/mid/end.\n"
            "# NO_SCALE_POLICY: Step5 table frame uses rotation+translation only.\n"
            f"# X_GUARD: guard_line={float(guard['guard_line_x_m']):.9f} m, "
            f"path_max_x={float(guard['path_max_x_m']):.9f} m."
        ),
        f"local entry_x = {geom['start_x']:.9f}": f"local entry_x = {entry_x:.9f}",
        f"local entry_y = {geom['start_y']:.9f}": f"local entry_y = {entry_y:.9f}",
    }
    for old, new in replacements.items():
        if old not in script:
            raise RuntimeError(f"{PROGRAM_NAME} scaffold replacement failed: {old}")
        script = script.replace(old, new, 1)
    script = script.replace(
        "paper-derived cycloid XY reference for 60 s",
        "Step5 table-driven contact cycloid reference for 60 s",
    )
    script = script.replace(
        "PAPER_PATH_FORMULA:",
        "STEP5_PATH_FORMULA:",
    )
    return script


def build_txt(stamp: str) -> str:
    return f"""Step5b contact cycloid baseline TP package

Open on Teach Pendant:
  {CONTROLLER_DIR}/{PROGRAM_NAME}.urp

Version:
  {stamp}

Motion boundary:
  Contact motion.
  Reuses the current v31 contact search, first-contact normal latch, 20 mm lift,
  25.2 attitude correction, second contact, and 25.3 line-entry gate.
  Stage 25.0 consumes bridge command registers 37..44 only.
  Bridge profile: --step4e-version {BRIDGE_VERSION} --step4e-path-shape cycloid.
  Force target: --target-force-n 5.0.
  Raw normal guard: 50 N. Force norm guard: 60 N. Torque guard: 3.0 Nm.
  No UR zero_ftsensor(), no Kunwei tare/zero/config, no TCP/payload write.

Reference:
  STEP5_FLOW.md
  config/step5_stage_table.json stage {STEP5_STAGE_ID}
"""


def validate_package(script: str, txt: str, urp: bytes, stamp: str) -> None:
    xml = gzip.decompress(urp).decode("utf-8")
    checks = {
        "script stamp": stamp in script,
        "txt stamp": stamp in txt,
        "program name": f'URProgram name="{PROGRAM_NAME}"' in xml,
        "controller directory": f'directory="{CONTROLLER_DIR}"' in xml,
        "script file": f"{CONTROLLER_DIR}/{PROGRAM_NAME}.script" in xml,
        "cached stamp": stamp in xml,
        "function name": f"def codex_{PROGRAM_NAME}()" in script,
        "step5 flow": "STEP5_FLOW.md" in script and "STEP5_FLOW.md" in txt,
        "step5 stage": STEP5_STAGE_ID in script and STEP5_STAGE_ID in txt,
        "bridge profile": f"step4e-version={BRIDGE_VERSION}" in script
        and f"--step4e-version {BRIDGE_VERSION}" in txt,
        "path source": "STEP5_TABLE_SOURCE: config/step5_stage_table.json" in script,
        "executor only": "TP_ROLE: executor_and_guard_only" in script,
        "line runtime": "local line_runtime_limit_s = 65.000" in script,
        "line success": "local line_success_progress_m = 60.000000000" in script,
        "v31 scaffold": "first-contact normal latch" in script
        and "25.2 attitude correction" in script
        and "25.3 line-entry gate" in script,
        "command registers": "read_input_float_register(37)" in script
        and "read_input_float_register(44)" in script,
        "raw guards": "codex_abs(normal_force) > 50.0" in script
        and "force_norm > 60.0" in script
        and "torque_norm > 3.0" in script,
        "no stale step4 package names": "step4f_cycloid_seed_normal_v1" not in script
        and "step4g_eight_seed_normal_v1" not in script,
    }
    failed = [label for label, ok in checks.items() if not ok]
    if failed:
        raise RuntimeError(f"{PROGRAM_NAME} validation failed: {failed}")


def write_outputs(stamp: str, gen_at: str) -> dict[str, str]:
    frame = load_safe_frame()
    geom = line_cfg(load_json(CONFIG_PATH))
    script = build_script(stamp, gen_at, geom, frame)
    txt = build_txt(stamp)
    urp = build_urp(script, PROGRAM_NAME, CONTROLLER_DIR)
    validate_package(script, txt, urp, stamp)

    LOCAL_PROGRAM_DIR.mkdir(parents=True, exist_ok=True)
    script_path = LOCAL_PROGRAM_DIR / f"{PROGRAM_NAME}.script"
    txt_path = LOCAL_PROGRAM_DIR / f"{PROGRAM_NAME}.txt"
    urp_path = LOCAL_PROGRAM_DIR / f"{PROGRAM_NAME}.urp"

    script_path.write_text(script, encoding="utf-8")
    txt_path.write_text(txt, encoding="utf-8")
    urp_path.write_bytes(urp)
    return {
        "script": str(script_path),
        "txt": str(txt_path),
        "urp": str(urp_path),
        "controller_urp": f"{CONTROLLER_DIR}/{PROGRAM_NAME}.urp",
        "stamp": stamp,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stamp-prefix", default=None)
    args = parser.parse_args()
    now = datetime.now(timezone(timedelta(hours=8)))
    stamp = args.stamp_prefix or source_stamp(now)
    result = write_outputs(stamp, generated_at(now))
    print(json.dumps({"generated": {PROGRAM_NAME: result}}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
