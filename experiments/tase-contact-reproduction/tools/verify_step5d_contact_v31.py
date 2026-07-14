#!/usr/bin/env python3
"""Deterministically verify the Step5d v31 permissive-contact package."""

from __future__ import annotations

import argparse
import gzip
import hashlib
import html
import json
import xml.etree.ElementTree as ET
from pathlib import Path


PROGRAM = "step5d_strict_rnn_ablation_v31"
EXTENSIONS = (".script", ".txt", ".urp")


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    digest.update(path.read_bytes())
    return digest.hexdigest()


def verify(root: Path) -> dict[str, object]:
    package_dir = root / "programs" / "step5" / "step5d"
    paths = {ext: package_dir / f"{PROGRAM}{ext}" for ext in EXTENSIONS}
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
    row = next(item for item in table["stages"] if item.get("id") == PROGRAM)
    guard = row["guard"]
    checks = {
        "urp cached script byte-equivalent": cached == script,
        "program identity": f'URProgram name="{PROGRAM}"' in xml and f"def codex_{PROGRAM}()" in script,
        "home captured at Play": "local home_pose = get_actual_tcp_pose()" in script,
        "fixed entry and gravity-down": "local entry_xy_pose" in script and "local target_rx = 3.141592654" in script,
        "far/near search and contact latch": "codex_step5d_down_search(24.0, 24.2" in script and "first-contact normal latch" in script,
        "strict layout 524 only": "local joint_layout_code = 524.000" in script and "if cmd_valid < 0.5 or not joint_layout_ok" in script,
        "header layout claim agrees": all(
            token not in "\n".join(script.splitlines()[:25])
            for token in ("multi-layout", "523.0=Cartesian", "v30 host policy")
        ) and "layout-524-only speedj" in "\n".join(script.splitlines()[:25]),
        "no Cartesian Stage25 execution": "speedl([cmd_vx, cmd_vy, cmd_vz, cmd_wx, cmd_wy, cmd_wz]" not in script,
        "qdot cap": "local qdot_cap_rad_s = 0.500" in script and float(guard["qdot_cap_rad_s"]) == 0.5,
        "heartbeat stale one second": "if stale_s2 > 1.000:" in script and float(guard["heartbeat_stale_stop_s"]) == 1.0,
        "runtime 75 seconds": "local line_runtime_limit_s = 75.000" in script and float(guard["stage25_runtime_limit_s"]) == 75.0,
        "gross guards 60/100/3": "codex_abs(normal_force) > 60.0" in script and "force_norm > 100.0" in script and "torque_norm > 3.0" in script,
        "force window diagnostic-only": guard["force_window_guards_enabled"] is False and "force window is post-run acceptance evidence only" in txt,
        "Cartesian normal guards diagnostic-only": guard["cartesian_speed_guards_enabled"] is False and guard["normal_motion_guards_enabled"] is False,
        "frame contract only": guard["normal_motion_policy"] == "frame_contract_only" and "UR_FORCE_FRAME_CONTRACT.md" in script,
        "DLS no fallback": guard["dls_runtime_fallback_allowed"] is False and "forbids DLS runtime fallback" in txt,
        "entry has no force/speed hard stop": "stop_reason = 17.0" not in script and "speedl([cmd_vx, cmd_vy, cmd_vz, 0.0, 0.0, 0.0]" not in script,
        "normal completion retract and home": "short_retract_start[2] + short_retract_z_m" in script and "movel(home_pose" in script,
        "unsafe stop does not auto-home": "  elif stop_reason == 2.0:\n    # Transport/heartbeat loss may follow a protective stop; never auto-home.\n    return False" in script
        and "  elif stop_reason == 17.0:\n    # Reserved unsafe-entry/safety interruption reason; never auto-home.\n    return False" in script,
        "no settings writes": all(token not in script for token in ("zero_ftsensor", "set_payload", "set_tcp")),
    }
    failed = [name for name, passed in checks.items() if not passed]
    return {
        "schema": "step5d_contact_v31_verification_v1",
        "program": PROGRAM,
        "ok": not failed,
        "failed": failed,
        "checks": checks,
        "sha256": {ext: sha256(path) for ext, path in paths.items()},
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
