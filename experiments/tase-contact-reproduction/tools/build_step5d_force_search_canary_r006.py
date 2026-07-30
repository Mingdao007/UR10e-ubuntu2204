#!/usr/bin/env python3
"""Build the immutable independent Kunwei 1 N canary r006 TP triplet."""

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
from step5d_force_search_canary_r006 import (
    CONTROLLER_DIR,
    PROGRAM_NAME,
    load_contract,
    render_script,
)
from step5d_new_eoat import load_new_eoat


ROOT = Path(__file__).resolve().parents[1]
OUTPUT_DIR = ROOT / "programs/step5/step5d"


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def source_stamp(now: datetime | None = None) -> str:
    value = now or datetime.now(timezone(timedelta(hours=8)))
    return value.strftime("%Y-%m-%dT%H%MHKT_STEP5D_FORCE_SEARCH_CANARY_R006")


def build_txt(stamp: str) -> str:
    contract = load_contract()
    eoat = load_new_eoat()
    return f"""Step5d independent Kunwei 1 N force-search canary r006

Controller target:
  {CONTROLLER_DIR}/{PROGRAM_NAME}.urp

Version:
  {stamp}

Program-side step table:
  P1 INIT: set payload={eoat.payload_kg:.3f} kg, CoG={list(eoat.cog_m)} m,
     TCP={list(eoat.tcp_pose_m_rad)}.
  P2 GATE: require writer register 29 controller-GET acknowledgement and three
     heartbeat increments, each strictly below {contract.heartbeat_gap_s * 1000:.0f} ms.
  P3 SEARCH: base -Z only, {contract.search_speed_m_s * 1000:.1f} mm/s,
     {contract.search_acceleration_m_s2:.3f} m/s^2, at most
     {contract.max_travel_m * 1000:.1f} mm / {contract.runtime_limit_s:.0f} s.
  P4 LATCH: positive normal_load >= {contract.contact_positive_normal_n:.1f} N
     OR force norm >= {contract.contact_force_norm_n:.1f} N.
  P5 STOP: stopl({contract.search_stopl_acceleration_m_s2:.2f}); continue Kunwei
     monitoring throughout deceleration.
  P6 DWELL: require fresh, finite, stationary state continuously for
     {contract.stationary_dwell_s:.2f} s.
  P7 RETRACT: success only, base +Z {contract.retract_distance_m * 1000:.1f} mm
     at {contract.retract_speed_m_s * 1000:.1f} mm/s; monitor every cycle.
  P8 TERMINAL: output stage 15/reason 11 on success, stage 90 on failure.

User-side / Remote-owner step table:
  U1 Confirm exact controller readback, Remote Control, Safety NORMAL,
     stationary robot, one live writer, and fresh Kunwei route.
  U2 Start the r006 register live writer and wait for its ready receipt.
  U3 Dashboard Load exact target, verify loaded identity, then Dashboard Play.
  U4 Observe writer terminal receipt; it exits immediately after TP terminal.
  U5 Do not restart or Home after any failure. Inspect stop reason and fresh
     controller/Kunwei evidence before a new governed attempt.

Hard guards:
  abs(normal)={contract.hard_abs_normal_n:.1f} N
  force_norm={contract.hard_force_norm_n:.1f} N
  torque_norm={contract.hard_torque_norm_nm:.1f} Nm

Claim boundary:
  Kunwei is the sole wrench authority. UR built-in force, force_mode, Home,
  XY/angular motion, hardware zero/tare, optimizer, RNN, and Autotune are absent.
  Absolute normal is a hard guard only; it is not a contact latch.
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
        "movej(",
        "movel(",
        "movep(",
        "speedj(",
        "servoj(",
        "zero_ftsensor(",
        "get_tcp_force(",
        "actual_TCP_force",
        "0.059140000",
        "0.487834547",
        "0.129337053",
    )
    first_speedl = script.index("speedl(")
    checks = {
        "stamp": script.startswith(f"# VERSION: {stamp}\n"),
        "program_name": root.attrib.get("name") == PROGRAM_NAME,
        "controller_directory": root.attrib.get("directory") == CONTROLLER_DIR,
        "script_node_path": script_path == f"{CONTROLLER_DIR}/{PROGRAM_NAME}.script",
        "cached_contents_exact": cached_contents == script,
        "base_z_only": script.count("speedl(") == 2
        and "speedl([0.0, 0.0, -" in script
        and "speedl([0.0, 0.0, 0.000500000" in script,
        "search_stop_exact": script.count(
            f"stopl({contract.search_stopl_acceleration_m_s2:.9f})"
        )
        == 2,
        "heartbeat_three_strict": "startup_increment_count < 3" in script
        and "startup_gap_s < 0.080000000" in script
        and "startup_gap_s >= 0.080000000" in script,
        "eoat_ack_before_motion": script.index(
            "eoat_get_ack = read_input_float_register(29)"
        )
        < first_speedl,
        "monitor_thread_before_motion": script.index(
            "run codex_deceleration_and_retract_monitor()"
        )
        < first_speedl,
        "positive_contact_not_absolute": (
            "normal_load >= 0.800000000" in script
            and "codex_abs(normal_load) >= 3.000000000" in script
        ),
        "success_stationary_dwell": "stationary_dwell_s < 0.250000000" in script
        and script.index("stationary_dwell_s < 0.250000000")
        < script.index("retract_start_z"),
        "terminal_after_motion": script.rindex("codex_echo(15.0, reason")
        > script.rindex("stopl("),
        "eoat_setters": script.count("set_target_payload(") == 1
        and script.count(
            "set_tcp(p[0.000000000, 0.000000000, 0.087400000,"
        )
        == 1,
        "no_forbidden_behavior": not any(token in script for token in forbidden),
        "paired_step_tables": "Program-side step table:" in txt
        and "User-side / Remote-owner step table:" in txt,
        "txt_identity": PROGRAM_NAME in txt and stamp in txt,
    }
    failed = [name for name, passed in checks.items() if not passed]
    if failed:
        raise ValueError(f"r006 triplet validation failed: {failed}")
    return checks


def _numeric_sanity(script: str) -> dict[str, Any]:
    contract = load_contract()
    search_runtime_travel_m = contract.search_speed_m_s * contract.runtime_limit_s
    checks = {
        "runtime_covers_travel": search_runtime_travel_m >= contract.max_travel_m,
        "search_speed_exact_0p5_mm_s": contract.search_speed_m_s == 0.0005,
        "search_accel_exact_0p01_m_s2": contract.search_acceleration_m_s2 == 0.01,
        "search_stopl_exact_0p01": contract.search_stopl_acceleration_m_s2 == 0.01,
        "heartbeat_gap_strict_80ms": contract.heartbeat_gap_s == 0.08,
        "success_dwell_exact_0p25s": contract.stationary_dwell_s == 0.25,
        "normal_contact_positive": "normal_load >= 0.800000000" in script,
        "absolute_normal_hard_guard_only": script.count(
            "codex_abs(normal_load)"
        )
        == 1,
    }
    if not all(checks.values()):
        raise ValueError(f"r006 numeric sanity failed: {checks}")
    return {
        "schema": "step5d.force-search-canary/numeric-sanity-v1",
        "program": PROGRAM_NAME,
        "contract_sha256": contract.sha256,
        "derived": {
            "runtime_limited_search_travel_m": search_runtime_travel_m,
            "travel_limit_m": contract.max_travel_m,
            "minimum_time_to_travel_limit_s": (
                contract.max_travel_m / contract.search_speed_m_s
            ),
        },
        "checks": checks,
        "passed": True,
    }


def _manifest(stamp: str, digests: dict[str, str], numeric_sha: str) -> dict[str, Any]:
    contract = load_contract()
    eoat = load_new_eoat()
    return {
        "schema_version": 2,
        "schema": "step5d.force-search-canary/local-candidate-v2",
        "basename": PROGRAM_NAME,
        "controller_directory": CONTROLLER_DIR,
        "controller_target": f"{CONTROLLER_DIR}/{PROGRAM_NAME}.urp",
        "source_stamp": stamp,
        "status": "local_candidate_not_uploaded",
        "immutable": True,
        "builder": "tools/build_step5d_force_search_canary_r006.py",
        "builder_sha256": _sha256_bytes(Path(__file__).read_bytes()),
        "force_search_contract": {
            "artifact_id": "new-eoat-kunwei-1n-canary-r006",
            "sha256": contract.sha256,
        },
        "eoat_binding": {
            "artifact_id": eoat.artifact_id,
            "contract_sha256": eoat.artifact_sha256,
            "controller_get_receipt_sha256": eoat.controller_receipt_sha256,
        },
        "register_live_writer": {
            "implementation": "tools/step5d_force_search_register_writer_r006.py",
            "single_writer_lease_required": True,
            "exit_on_tp_terminal": True,
        },
        "artifacts": [
            {
                "filename": f"{PROGRAM_NAME}{suffix}",
                "source": f"{PROGRAM_NAME}{suffix}",
                "sha256": digests[suffix],
            }
            for suffix in (".script", ".txt", ".urp")
        ],
        "numeric_sanity": {
            "filename": f"{PROGRAM_NAME}.numeric-sanity.json",
            "sha256": numeric_sha,
            "passed": True,
        },
        "delivery": {
            "local_urp_internal_content_gate": True,
            "controller_upload": False,
            "controller_readback": False,
            "current_release_pointer_changed": False,
        },
    }


def write_triplet(output_dir: Path, stamp: str) -> dict[str, Any]:
    body = render_script()
    script = f"# VERSION: {stamp}\n{body}"
    txt = build_txt(stamp)
    urp = package_support.build_urp(script, PROGRAM_NAME, CONTROLLER_DIR)
    checks = validate_triplet(script, txt, urp, stamp)
    numeric = _numeric_sanity(script)
    output_dir.mkdir(parents=True, exist_ok=True)
    paths = {
        ".script": output_dir / f"{PROGRAM_NAME}.script",
        ".txt": output_dir / f"{PROGRAM_NAME}.txt",
        ".urp": output_dir / f"{PROGRAM_NAME}.urp",
    }
    numeric_path = output_dir / f"{PROGRAM_NAME}.numeric-sanity.json"
    manifest_path = output_dir / f"{PROGRAM_NAME}.deploy-manifest.json"
    collisions = [
        str(path)
        for path in (*paths.values(), numeric_path, manifest_path)
        if path.exists()
    ]
    if collisions:
        raise FileExistsError(
            "r006 is immutable; existing output must not be overwritten: "
            + ", ".join(collisions)
        )
    paths[".script"].write_text(script, encoding="utf-8")
    paths[".txt"].write_text(txt, encoding="utf-8")
    paths[".urp"].write_bytes(urp)
    numeric_path.write_text(
        json.dumps(numeric, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    digests = {
        suffix: _sha256_bytes(path.read_bytes()) for suffix, path in paths.items()
    }
    numeric_sha = _sha256_bytes(numeric_path.read_bytes())
    manifest_path.write_text(
        json.dumps(_manifest(stamp, digests, numeric_sha), indent=2, sort_keys=True)
        + "\n",
        encoding="utf-8",
    )
    return {
        "program": PROGRAM_NAME,
        "controller_target": f"{CONTROLLER_DIR}/{PROGRAM_NAME}.urp",
        "stamp": stamp,
        "sha256": digests,
        "numeric_sanity_sha256": numeric_sha,
        "checks": checks,
        "manifest": str(manifest_path),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=OUTPUT_DIR)
    parser.add_argument("--stamp", default=None)
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
