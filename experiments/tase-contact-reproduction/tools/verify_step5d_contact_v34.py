#!/usr/bin/env python3
"""Deterministically verify the Step5d v34 TP/runtime contract."""

from __future__ import annotations

import argparse
import gzip
import hashlib
import html
import json
import xml.etree.ElementTree as ET
from pathlib import Path


PROGRAM = "step5d_strict_rnn_ablation_v34"
EXTENSIONS = (".script", ".txt", ".urp")


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def verify(root: Path) -> dict[str, object]:
    package_dir = root / "programs" / "step5" / "step5d"
    paths = {ext: package_dir / f"{PROGRAM}{ext}" for ext in EXTENSIONS}
    missing = [str(path) for path in paths.values() if not path.is_file()]
    if missing:
        return {"schema": "step5d_contact_v34_verification_v1", "ok": False, "failed": [f"missing: {missing}"]}
    script = paths[".script"].read_text(encoding="utf-8")
    txt = paths[".txt"].read_text(encoding="utf-8")
    xml = gzip.decompress(paths[".urp"].read_bytes()).decode("utf-8")
    xml_root = ET.fromstring(xml)
    cached = next((html.unescape(node.text or "") for node in xml_root.iter() if node.tag == "cachedContents"), "")
    table = json.loads((root / "config" / "step5_stage_table.json").read_text(encoding="utf-8"))
    row = next(item for item in table["stages"] if item.get("id") == PROGRAM)
    outer = row["stage25_outer_profile"]
    scheduler = row["scheduler_lifecycle"]
    checks = {
        "urp cached script byte-equivalent": cached == script,
        "program identity": f'URProgram name="{PROGRAM}"' in xml and f"def codex_{PROGRAM}()" in script,
        "60 s full identity": "local line_success_progress_m = 60.000000000" in script
        and "local line_runtime_limit_s = 75.000" in script,
        "joint-only Stage25": "local joint_layout_code = 524.000" in script
        and "speedl([cmd_vx, cmd_vy, cmd_vz, cmd_wx, cmd_wy, cmd_wz]" not in script,
        "TP speedj acceleration 0.1": "local joint_accel_rad_s2 = 0.100" in script,
        "host slew 0.1": float(row["runtime_profile"]["command_slew_rad_s2"]) == 0.1
        and float(row["guard"]["qdot_slew_rad_s2"]) == 0.1,
        "gross guards unchanged": "raw normal guard 60 N, force norm guard 100 N, torque guard 3.0 Nm" in script,
        "Step5b-equivalent outer unchanged": outer == {
            "profile_id": "step5d_v33_step5b_discrete_equivalent_v1",
            "exact_binding": True,
            "tangential_kp": 1.5,
            "orientation_ko": 0.4,
            "force_Md": 1000,
            "force_Bd": 7000,
            "force_kf": 0.01,
            "force_integral_limit_n_s": 1,
            "contact_search_cli_parameters_are_not_active_outer_parameters": True,
        },
        "late FIFO scheduler contract": scheduler["launch_policy"] == "SCHED_OTHER/0"
        and scheduler["control_thread_policy"] == "SCHED_FIFO/20"
        and scheduler["helper_thread_policy"] == "SCHED_OTHER/0"
        and scheduler["kernel_sched_rt_runtime_us_write_allowed"] is False,
        "permissive ordinary guards": row["guard"]["force_window_guards_enabled"] is False
        and row["guard"]["cartesian_speed_guards_enabled"] is False
        and row["guard"]["normal_motion_guards_enabled"] is False,
        "no settings writes": all(token not in script for token in ("zero_ftsensor", "set_payload", "set_tcp")),
        "documentation identifies v34": PROGRAM in txt and "0.100 rad/s^2" in txt,
    }
    failed = [name for name, passed in checks.items() if not passed]
    return {
        "schema": "step5d_contact_v34_verification_v1",
        "ok": not failed,
        "failed": failed,
        "checks": checks,
        "sha256": {ext: sha256(path) for ext, path in paths.items()},
        "claim_boundary": "offline package/runtime verification only; no live authorization",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = verify(args.root.resolve())
    rendered = json.dumps(result, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
    print(rendered, end="")
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
