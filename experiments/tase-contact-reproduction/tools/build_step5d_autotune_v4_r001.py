#!/usr/bin/env python3
"""Build the immutable, independent new-EOAT Autotune V4 r001 TP triplet."""

from __future__ import annotations

import argparse
import gzip
import hashlib
import html
import json
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import build_step5d_autotune_start_hover_r001 as package_support
from step5d_autotune_v4.contracts import (
    LINEAGE,
    PROGRAM_R001 as PROGRAM,
    R001_CONTRACT,
    TARGET_FORCE_N,
    load_contract as _load_contract,
)
from step5d_autotune_v4.tp import CONTROLLER_DIR, render_script


ROOT = Path(__file__).resolve().parents[1]
OUTPUT_DIR = ROOT / "programs/step5/step5d"


def load_contract():
    return _load_contract(R001_CONTRACT)


def _sha(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def source_stamp(now: datetime | None = None) -> str:
    value = now or datetime.now(timezone(timedelta(hours=8)))
    return value.strftime("%Y-%m-%dT%H%MHKT_STEP5D_AUTOTUNE_V4_R001")


def build_txt(stamp: str) -> str:
    contract = load_contract()
    return f"""Step5d independent new-EOAT 5 N Autotune V4 r001

Controller target:
  {CONTROLLER_DIR}/{PROGRAM}.urp

Version:
  {stamp}

Program-side step table:
  P1 INIT: actively set the hash-bound payload/CoG/TCP and require controller
     GET acknowledgement before motion.
  P2 STARTUP: require two fresh writer heartbeat increments within 250 ms.
  P3 ENTRY: rise at 40 mm/s; transfer at <=10 mm/s and <=0.10 rad/s;
     descend at <=5 mm/s; every movel uses r=0 and stationary validation.
  P4 SEARCH: base -Z only at 0.5 mm/s, stopl(0.01), positive 0.8 N normal
     or 1.0 N force-norm latch, 25 mm / 90 s bounds.
  P5 BASELINE: immutable candidate target 5 N; internal setpoint ramps
     1->5 N over 8 s, then requires the 4-6 / 3-7 / 7 N / 0.30 Nm
     readiness dwell and a continuous 10 s hold.
  P6 UNLOCK: three consecutive successful 5 N baselines are required before
     the full 60 s path.
  P7 EXECUTE: accept only fresh qdot packets pre-gated against the contract's
     exact model/Jacobian hashes; speedj/stopj acceleration is 2.5 rad/s^2.
  P8 TERMINAL: failure remains stopped; only fresh, stationary success may
     retract base +Z. No automatic Home.

Remote-owner step table:
  U1 Keep V3/r034 selected while using the legacy EOAT. Do not Load/Play V4.
  U2 After fitting the new EOAT, complete r006 and three governed 5 N baseline
     successes, formal Review v3, and fresh route/force-frame gates.
  U3 Use the canonical lineage transition; never hand-edit either pointer.
  U4 Load/Play only the exact controller-readback-verified V4 target through
     the bound Remote route with one writer and fresh Kunwei authority.

Immutable identity:
  lineage={LINEAGE}
  program={PROGRAM}
  target_force_n={TARGET_FORCE_N:.1f}
  contract_sha256={contract.sha256}
  campaign_fingerprint={contract.campaign_fingerprint}
  eoat_contract_sha256={contract.eoat_sha256}

Claim boundary:
  This package is staged independently from V3. Generation/upload/read-back
  do not select, Load, Play, ARM, move, contact, zero/tare, or import V3 data.
"""


def validate_triplet(
    script: str, txt: str, urp: bytes, stamp: str
) -> dict[str, bool]:
    contract = load_contract()
    root = ET.fromstring(gzip.decompress(urp).decode("utf-8"))
    cached_contents = ""
    script_path = ""
    for node in root.iter():
        tag = node.tag.rsplit("}", 1)[-1]
        if tag == "cachedContents":
            cached_contents = html.unescape(node.text or "")
        elif tag == "file" and node.attrib.get("resolves-to") == "file":
            script_path = (node.text or "").strip()
    forbidden = (
        "force_mode(",
        "get_tcp_force(",
        "actual_TCP_force",
        "zero_ftsensor(",
        "freedrive_mode(",
        "movej(",
        "movep(",
        "12.000000000",
        "0.059140000",
        "0.052863519",
    )
    checks = {
        "stamp": script.startswith(f"# VERSION: {stamp}\n"),
        "program_name": root.attrib.get("name") == PROGRAM,
        "controller_directory": root.attrib.get("directory") == CONTROLLER_DIR,
        "script_node_path": script_path == f"{CONTROLLER_DIR}/{PROGRAM}.script",
        "cached_contents_exact": cached_contents == script,
        "contract_binding": contract.sha256 in script
        and contract.campaign_fingerprint in script
        and contract.eoat_sha256 in script,
        "eoat_setters": script.count("set_target_payload(") == 1
        and script.count(
            "set_tcp(p[0.000000000, 0.000000000, 0.087400000,"
        )
        == 1,
        "startup_v4": "startup_increments < 2" in script
        and "startup_elapsed_s - startup_first_increment_s <= 0.250000000"
        in script,
        "entry_r0": script.count("movel(") == 3
        and script.count("r=0.0)") == 3
        and "v=0.040000000" in script
        and "v=0.010000000" in script
        and "v=0.005000000" in script,
        "entry_and_search_stop_separate": "stopl(0.250000000)" in script
        and "stopl(0.010000000)" in script,
        "target_immutable": "# TARGET_FORCE_N: 5.0 immutable" in script
        and "internal_setpoint > 5.0" in script
        and "internal_setpoint = 5.0" in script,
        "three_baselines": "prior_baseline_successes + 1 < 3" in script,
        "qdot_packet_gate": "layout !=" in script
        and "not codex_v4_qdot_finite_and_bounded(qdot)" in script,
        "speedj_stopj_accel": "speedj(qdot, 2.500000000, actual_dt)" in script
        and script.count("stopj(2.500000000)") == 2,
        "success_only_retract": script.index("if baseline_reason != 0.0:")
        < script.index("baseline_retract_start_pose")
        and script.index("if path_reason != 0.0 or not")
        < script.index("local retract_start_pose"),
        "no_forbidden_behavior": not any(token in script for token in forbidden),
        "paired_step_tables": "Program-side step table:" in txt
        and "Remote-owner step table:" in txt,
        "v3_preserved_instruction": "Keep V3/r034 selected" in txt,
        "txt_identity": PROGRAM in txt and stamp in txt,
    }
    failed = [name for name, passed in checks.items() if not passed]
    if failed:
        raise ValueError(f"V4 r001 triplet validation failed: {failed}")
    return checks


def _numeric_sanity(script: str) -> dict[str, Any]:
    checks = {
        "target_exact_5n": TARGET_FORCE_N == 5.0,
        "entry_xyz_exact": all(
            token in script
            for token in ("0.487834547", "0.129337053", "0.022863519")
        ),
        "expected_contact_z_not_motion_target": "0.008044839" not in script,
        "no_duplicate_tcp_z_compensation": "0.034700000" not in script,
        "search_base_z_only": (
            "speedl([0.0, 0.0, -0.000500000, 0.0, 0.0, 0.0]" in script
        ),
        "baseline_post_contact_guard": (
            "codex_v4_stationary_dwell(0.250000000, 15.0, 20.0, 1.0)"
            in script
        ),
        "search_guard_remains_3n": (
            "codex_v4_stationary_dwell(0.250000000, 3.0, 3.0, 0.2)"
            in script
        ),
        "full_path_exact_60s": "path_elapsed_s < 60.000000000" in script,
        "no_auto_home": "movej(" not in script and "home(" not in script.lower(),
    }
    if not all(checks.values()):
        raise ValueError(f"V4 r001 numeric sanity failed: {checks}")
    return {
        "schema": "step5d.autotune-v4/numeric-sanity-v1",
        "program": PROGRAM,
        "checks": checks,
        "passed": True,
    }


def _manifest(
    stamp: str, digests: dict[str, str], numeric_sha: str
) -> dict[str, Any]:
    contract = load_contract()
    return {
        "schema_version": 2,
        "schema": "step5d.autotune-v4/local-candidate-v1",
        "basename": PROGRAM,
        "controller_directory": CONTROLLER_DIR,
        "controller_target": f"{CONTROLLER_DIR}/{PROGRAM}.urp",
        "source_stamp": stamp,
        "status": "local_candidate_not_uploaded_live_blocked",
        "immutable": True,
        "builder": "tools/build_step5d_autotune_v4_r001.py",
        "builder_sha256": _sha(Path(__file__).read_bytes()),
        "v4_binding": {
            "lineage": LINEAGE,
            "release_contract_sha256": contract.sha256,
            "campaign_fingerprint": contract.campaign_fingerprint,
            "target_force_n": TARGET_FORCE_N,
            "eoat_contract_sha256": contract.eoat_sha256,
            "wrench_authority": "kunwei_only",
        },
        "artifacts": [
            {
                "filename": f"{PROGRAM}{suffix}",
                "source": f"{PROGRAM}{suffix}",
                "sha256": digests[suffix],
            }
            for suffix in (".script", ".txt", ".urp")
        ],
        "numeric_sanity": {
            "filename": f"{PROGRAM}.numeric-sanity.json",
            "sha256": numeric_sha,
            "passed": True,
        },
        "delivery": {
            "local_urp_internal_content_gate": True,
            "controller_upload": False,
            "controller_readback": False,
            "controller_load_or_play": False,
            "v3_current_pointer_changed": False,
            "v4_lineage_selected": False,
        },
    }


def write_triplet(output_dir: Path, stamp: str) -> dict[str, Any]:
    script = f"# VERSION: {stamp}\n{render_script()}"
    txt = build_txt(stamp)
    urp = package_support.build_urp(script, PROGRAM, CONTROLLER_DIR)
    checks = validate_triplet(script, txt, urp, stamp)
    numeric = _numeric_sanity(script)
    output_dir.mkdir(parents=True, exist_ok=True)
    paths = {
        suffix: output_dir / f"{PROGRAM}{suffix}"
        for suffix in (".script", ".txt", ".urp")
    }
    numeric_path = output_dir / f"{PROGRAM}.numeric-sanity.json"
    manifest_path = output_dir / f"{PROGRAM}.deploy-manifest.json"
    collisions = [
        str(path)
        for path in (*paths.values(), numeric_path, manifest_path)
        if path.exists() or path.is_symlink()
    ]
    if collisions:
        raise FileExistsError(
            "V4 r001 is immutable; existing output must not be overwritten: "
            + ", ".join(collisions)
        )
    paths[".script"].write_text(script, encoding="utf-8")
    paths[".txt"].write_text(txt, encoding="utf-8")
    paths[".urp"].write_bytes(urp)
    numeric_path.write_text(
        json.dumps(numeric, indent=2, sort_keys=True, allow_nan=False) + "\n",
        encoding="utf-8",
    )
    digests = {suffix: _sha(path.read_bytes()) for suffix, path in paths.items()}
    numeric_sha = _sha(numeric_path.read_bytes())
    manifest_path.write_text(
        json.dumps(
            _manifest(stamp, digests, numeric_sha),
            indent=2,
            sort_keys=True,
            allow_nan=False,
        )
        + "\n",
        encoding="utf-8",
    )
    return {
        "program": PROGRAM,
        "controller_target": f"{CONTROLLER_DIR}/{PROGRAM}.urp",
        "stamp": stamp,
        "sha256": digests,
        "numeric_sanity_sha256": numeric_sha,
        "checks": checks,
        "manifest": str(manifest_path),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=OUTPUT_DIR)
    parser.add_argument("--stamp")
    args = parser.parse_args(argv)
    result = write_triplet(args.output_dir, args.stamp or source_stamp())
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
