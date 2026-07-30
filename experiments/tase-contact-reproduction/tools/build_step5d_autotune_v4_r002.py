#!/usr/bin/env python3
"""Build the immutable behavioral-primitives Autotune V4 r002 TP triplet."""

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
from step5d_autotune_v4.contracts import LINEAGE, PROGRAM, TARGET_FORCE_N, load_contract
from step5d_autotune_v4.tp import CONTROLLER_DIR, render_script


ROOT = Path(__file__).resolve().parents[1]
OUTPUT_DIR = ROOT / "programs/step5/step5d"


def _sha(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def source_stamp(now: datetime | None = None) -> str:
    value = now or datetime.now(timezone(timedelta(hours=8)))
    return value.strftime("%Y-%m-%dT%H%MHKT_STEP5D_AUTOTUNE_V4_R002")


def build_txt(stamp: str) -> str:
    contract = load_contract()
    return f"""Step5d independent new-EOAT 5 N Autotune V4 r002

Controller target:
  {CONTROLLER_DIR}/{PROGRAM}.urp

Version:
  {stamp}

Program-side step table:
  P1 INIT: apply the hash-bound new EOAT profile using the shared profile
     renderer; require a fresh controller GET acknowledgement before motion.
  P2 STARTUP: require two fresh writer heartbeat increments within 250 ms.
  P3 ENTRY: consume the fixed three-segment entry contract; r=0 and stationary
     validation remain TP-side invariants.
  P4 SEARCH: use the shared 1 N force-search profile; base -Z only, bounded
     travel/runtime, Kunwei-only guards, stopl(0.01).
  P5 BASELINE: consume typed BASELINE packets from an injected host policy.
     The TP validates actual dt, setpoint 1..5 N and <=0.5 N/s rise, qdot,
     hard guards, layout, sequence, and command mode.
  P6 QUALIFY: only BaselineQualificationLedger owns stage-22 success receipts;
     typed RETRACT is accepted below three receipts and typed PATH only at >=3.
  P7 EXECUTE: consume typed PATH packets after the hash-bound Jacobian gate;
     every packet remains inside the fixed V4 invariant envelope.
  P8 TERMINAL: failure stays stopped; fresh stationary success may retract.
     No automatic Home.

Remote-owner step table:
  U1 Keep V3/r034 active for the old EOAT; V4 r001 is superseded and unloadable.
  U2 Do not Load/Play r002 until the new EOAT is installed, r006 succeeds,
     three governed 5 N baselines close, Review v3 closes, and route gates pass.
  U3 Use only the canonical lineage transition; never hand-edit either pointer.

Behavioral primitive identities:
  lineage={LINEAGE}
  program={PROGRAM}
  target_force_n={TARGET_FORCE_N:.1f}
  contract_sha256={contract.sha256}
  campaign_fingerprint={contract.campaign_fingerprint}
  eoat_profile_sha256={contract.eoat_sha256}
  wire_layout=605 typed command mode

Claim boundary:
  Generation/upload/read-back are package delivery only. They do not select,
  Load, Play, ARM, move, contact, zero/tare, or import V3 observations.
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
        "0.034700000",
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
        "shared_eoat_profile": "# EOAT_PROFILE_ID: new-3d-printed-eoat-v4" in script
        and script.count("set_target_payload(") == 1
        and script.count("set_tcp(") == 1,
        "typed_modes": "read_input_integer_register(25)" in script
        and "baseline_transition_mode == 3" in script
        and "baseline_transition_mode != 2" in script,
        "ledger_owned_count": "read_input_integer_register(24)" in script
        and "baseline_successes < 3" in script,
        "host_baseline_policy": "local internal_setpoint = read_input_float_register(44)"
        in script
        and "readiness_dwell_s" not in script
        and "hold_s" not in script,
        "invariant_setpoint": (
            "internal_setpoint - prior_internal_setpoint > 0.5 * actual_dt"
            in script
        ),
        "layout_605": "layout != 605.0" in script,
        "entry_r0": script.count("movel(") == 3
        and script.count("r=0.0)") == 3,
        "distinct_stops": "stopl(0.250000000)" in script
        and "stopl(0.010000000)" in script,
        "qdot_gate": "codex_v4_qdot_finite_and_bounded(qdot)" in script,
        "no_forbidden_behavior": not any(token in script for token in forbidden),
        "paired_step_tables": "Program-side step table:" in txt
        and "Remote-owner step table:" in txt,
        "v3_preserved": "Keep V3/r034 active" in txt,
    }
    failed = [name for name, passed in checks.items() if not passed]
    if failed:
        raise ValueError(f"V4 r002 triplet validation failed: {failed}")
    return checks


def numeric_sanity(script: str) -> dict[str, Any]:
    checks = {
        "target_exact_5n": TARGET_FORCE_N == 5.0,
        "entry_xyz_exact": all(
            token in script
            for token in ("0.487834547", "0.129337053", "0.022863519")
        ),
        "expected_contact_z_evidence_only": "0.008044839" not in script,
        "program_z_delta_zero": "0.034700000" not in script,
        "search_base_z_only": (
            "speedl([0.0, 0.0, -0.000500000, 0.0, 0.0, 0.0]" in script
        ),
        "search_guards_3_3_0p2": (
            "codex_v4_stationary_dwell(0.250000000, 3.0, 3.0, 0.2)"
            in script
        ),
        "baseline_guards_15_20_1": (
            "codex_v4_stationary_dwell(0.250000000, 15.0, 20.0, 1.0)"
            in script
        ),
        "full_path_60s": "path_elapsed_s < 60.000000000" in script,
        "layout_605": "layout != 605.0" in script,
        "no_auto_home": "movej(" not in script and "home(" not in script.lower(),
    }
    if not all(checks.values()):
        raise ValueError(f"V4 r002 numeric sanity failed: {checks}")
    return {
        "schema": "step5d.autotune-v4/numeric-sanity-v2",
        "program": PROGRAM,
        "checks": checks,
        "passed": True,
    }


def write_triplet(output_dir: Path, stamp: str) -> dict[str, Any]:
    script = f"# VERSION: {stamp}\n{render_script()}"
    txt = build_txt(stamp)
    urp = package_support.build_urp(script, PROGRAM, CONTROLLER_DIR)
    checks = validate_triplet(script, txt, urp, stamp)
    sanity = numeric_sanity(script)
    output_dir.mkdir(parents=True, exist_ok=True)
    paths = {
        suffix: output_dir / f"{PROGRAM}{suffix}"
        for suffix in (".script", ".txt", ".urp")
    }
    numeric_path = output_dir / f"{PROGRAM}.numeric-sanity.json"
    manifest_path = output_dir / f"{PROGRAM}.deploy-manifest.json"
    if any(path.exists() or path.is_symlink() for path in (*paths.values(), numeric_path, manifest_path)):
        raise FileExistsError("V4 r002 is immutable; existing output must not be overwritten")
    paths[".script"].write_text(script, encoding="utf-8")
    paths[".txt"].write_text(txt, encoding="utf-8")
    paths[".urp"].write_bytes(urp)
    numeric_path.write_text(
        json.dumps(sanity, indent=2, sort_keys=True, allow_nan=False) + "\n",
        encoding="utf-8",
    )
    digests = {suffix: _sha(path.read_bytes()) for suffix, path in paths.items()}
    numeric_sha = _sha(numeric_path.read_bytes())
    contract = load_contract()
    manifest = {
        "schema_version": 2,
        "schema": "step5d.autotune-v4/local-candidate-v2",
        "basename": PROGRAM,
        "controller_directory": CONTROLLER_DIR,
        "controller_target": f"{CONTROLLER_DIR}/{PROGRAM}.urp",
        "source_stamp": stamp,
        "status": "local_candidate_not_uploaded_live_blocked",
        "immutable": True,
        "builder": "tools/build_step5d_autotune_v4_r002.py",
        "builder_sha256": _sha(Path(__file__).read_bytes()),
        "v4_binding": {
            "lineage": LINEAGE,
            "release_contract_sha256": contract.sha256,
            "campaign_fingerprint": contract.campaign_fingerprint,
            "target_force_n": TARGET_FORCE_N,
            "eoat_profile_sha256": contract.eoat_sha256,
            "wire_layout": 605,
            "baseline_qualification_owner": "BaselineQualificationLedger",
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
            "filename": numeric_path.name,
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
    manifest_path.write_text(
        json.dumps(manifest, indent=2, sort_keys=True, allow_nan=False) + "\n",
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
    print(
        json.dumps(
            write_triplet(args.output_dir, args.stamp or source_stamp()),
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
