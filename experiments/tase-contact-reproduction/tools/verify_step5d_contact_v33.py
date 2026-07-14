#!/usr/bin/env python3
"""Deterministically verify both Step5d v33 TP package identities."""

from __future__ import annotations

import argparse
import gzip
import hashlib
import html
import json
import xml.etree.ElementTree as ET
from pathlib import Path


PROGRAMS = {
    "step5d_strict_rnn_ablation_v33c20": (20.0, 35.0),
    "step5d_strict_rnn_ablation_v33": (60.0, 75.0),
}
EXTENSIONS = (".script", ".txt", ".urp")


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def verify_program(root: Path, program: str, success_s: float, runtime_s: float) -> dict[str, object]:
    package_dir = root / "programs" / "step5" / "step5d"
    paths = {ext: package_dir / f"{program}{ext}" for ext in EXTENSIONS}
    missing = [str(path) for path in paths.values() if not path.is_file()]
    if missing:
        return {"ok": False, "failed": [f"missing package files: {missing}"]}
    script = paths[".script"].read_text(encoding="utf-8")
    txt = paths[".txt"].read_text(encoding="utf-8")
    xml = gzip.decompress(paths[".urp"].read_bytes()).decode("utf-8")
    xml_root = ET.fromstring(xml)
    cached = next(
        (html.unescape(node.text or "") for node in xml_root.iter() if node.tag == "cachedContents"),
        "",
    )
    table = json.loads((root / "config" / "step5_stage_table.json").read_text(encoding="utf-8"))
    row = next(item for item in table["stages"] if item.get("id") == program)
    outer = row["stage25_outer_profile"]
    feedback = row["feedback_policy"]
    checks = {
        "urp cached script byte-equivalent": cached == script,
        "program identity": f'URProgram name="{program}"' in xml and f"def codex_{program}()" in script,
        "stage25 duration identity": f"local line_success_progress_m = {success_s:.9f}" in script
        and f"local line_runtime_limit_s = {runtime_s:.3f}" in script,
        "joint-only Stage25": "local joint_layout_code = 524.000" in script
        and "speedl([cmd_vx, cmd_vy, cmd_vz, cmd_wx, cmd_wy, cmd_wz]" not in script,
        "speedj acceleration unchanged": "local joint_accel_rad_s2 = 0.050" in script,
        "gross guards": "raw normal guard 60 N, force norm guard 100 N, torque guard 3.0 Nm" in script,
        "outer exact binding": outer == {
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
        "feedback structural policy": feedback["receive"] == "drain_all_currently_readable_packets_use_latest"
        and float(feedback["feedback_age_limit_s"]) == 0.05
        and float(feedback["continuous_stale_dwell_stop_s"]) == 0.1,
        "permissive ordinary guards": row["guard"]["force_window_guards_enabled"] is False
        and row["guard"]["cartesian_speed_guards_enabled"] is False
        and row["guard"]["normal_motion_guards_enabled"] is False,
        "no settings writes": all(token not in script for token in ("zero_ftsensor", "set_payload", "set_tcp")),
        "documentation names latest feedback": "strict RNN" in txt,
    }
    failed = [name for name, passed in checks.items() if not passed]
    return {
        "ok": not failed,
        "failed": failed,
        "checks": checks,
        "sha256": {ext: sha256(path) for ext, path in paths.items()},
    }


def verify(root: Path) -> dict[str, object]:
    programs = {
        program: verify_program(root, program, success_s, runtime_s)
        for program, (success_s, runtime_s) in PROGRAMS.items()
    }
    return {
        "schema": "step5d_contact_v33_verification_v1",
        "ok": all(result["ok"] is True for result in programs.values()),
        "programs": programs,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = verify(args.root.resolve())
    rendered = json.dumps(result, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.write_text(rendered, encoding="utf-8")
    print(rendered, end="")
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
