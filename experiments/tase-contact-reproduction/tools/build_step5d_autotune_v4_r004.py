#!/usr/bin/env python3
"""Build and validate the isolated Step5d V4 r004 resident TP triplet."""

from __future__ import annotations

import argparse
import gzip
import hashlib
import html
import json
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import build_step4e_p0p1_programs as package_support
from step5d_autotune_v4_r004.contracts import (
    D_ANCHOR,
    LINEAGE,
    MAX_ATTEMPTS,
    PROGRAM,
    TARGET_FORCE_N,
    load_contract,
)
from step5d_autotune_v4_r004.tp import CONTROLLER_DIR, RUNTIME_PROTOCOL, render_script
from step5d_autotune_v4_r004.wire import (
    DOUBLE_FIELDS,
    INPUT_INTEGER_REGISTERS,
    LAYOUT_TAG,
    OUTPUT_INTEGER_FIELDS,
    validate_register_mappings,
)


ROOT = Path(__file__).resolve().parents[1]
OUTPUT_DIR = ROOT / "programs/step5/step5d"
R004_STAMP = "2026-08-01T1542HKT_STEP5D_AUTOTUNE_V4_R004"
STAMP_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{4}HKT_STEP5D_AUTOTUNE_V4_R004$")


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def source_stamp(now: datetime | None = None) -> str:
    if now is None:
        return R004_STAMP
    value = now or datetime.now(timezone(timedelta(hours=8)))
    return value.strftime("%Y-%m-%dT%H%MHKT_STEP5D_AUTOTUNE_V4_R004")


def validate_stamp(stamp: str) -> str:
    if not isinstance(stamp, str) or STAMP_PATTERN.fullmatch(stamp) is None:
        raise ValueError(
            "r004 source stamp must use YYYY-MM-DDTHHMMHKT_STEP5D_AUTOTUNE_V4_R004"
        )
    return stamp


def build_txt(stamp: str) -> str:
    contract = load_contract()
    return f"""Step5d independent new-EOAT 5 N Autotune V4 r004 resident loop

Controller target:
  {CONTROLLER_DIR}/{PROGRAM}.urp

Version:
  {stamp}

Program-side step table:
  P1 PLAY: bind the EOAT-specific fixed Home pose; stay READY_HOME_NEXT.
  P2 ARM: consume one newer typed SessionCommand ARM and one logical ordinal,
     then verify fixed Home position <=1 mm, orientation <=0.01 rad, and the
     locked IK branch q <=0.02 rad. Failure reason 69 stops without auto-Home.
  P3 CONTACT: integrated negative-Z acquisition at V3 near-field speed,
     0.0002 m/s, 0.005 m/s2, max 0.025 m or 90 s; no standalone search/canary.
  P4 BASELINE: wait pre-latch <=20 s, then reset a separate post-latch
     <=30 s force-acquisition timer after sticky 1 N is first observed.
  P5 PATH: only after baseline counter >=3 and typed PATH; bounded 60 s.
  P6 RETURN: successful attempts retract +Z >=5 mm, transfer XY at the clear
     height using V3 dynamics, then descend directly to the same fixed Home.
  P7 READY: echo epoch/ordinal/state/token/reason/consumed sequence/kind,
     return guard, and runtime identity digest; wait for the next ARM.
  P8 STOPPED: any abnormal stop or return failure stays stopped; no auto-Home.

Register wire:
  layout_tag=606 (layout-606); doubles 24..47 retain r003 sensor/qdot/setpoint meanings.
  EXPECTED_PROGRAM: {PROGRAM}; wire identity is layout-606.
  integer inputs 24..32 = baseline_successes, CommandMode, sticky_latch,
  SessionCommand, session_sequence, epoch, ordinal, kind, candidate_token.
  SessionCommand HOLD=0 ARM=1 COMPLETE=2 STOP=3.
  integer outputs 24..34 = epoch, ordinal, state, token, reason,
  consumed_sequence, kind, return_guard, runtime_protocol, digest_hi, digest_lo.
  runtime_protocol={RUNTIME_PROTOCOL}; digest material is
  program+contract_sha256+campaign_fingerprint (SHA-256 first 62 bits split
  into two non-negative 31-bit limbs); session epoch is a separate output.

Freshness and identity:
  TP=500 Hz and writer uses a 500 Hz absolute-deadline scheduler. A newer sequence replaces an immutable packet
  cache. An equal sequence is reusable only when all 24 doubles and 9 integer
  fields are byte/field-identical. Changed equal payload, regression, or held
  age >=80 ms fails closed with reason 43. Script1 receipt max age is 120 s;
  controller readback receipt max age is 300 s and is session-invalidated by
  stop/load/restart. Script1 is byte-bound to the exact frozen r001 triplet.

Qualification-bound first PATH trial and ledger:
  Only logical ordinal 4 with kind BATCH_A is live-enabled. It requires the
  same durable ledger to contain contiguous successful qualification ordinals
  1..3 with fsync, cold-read, and hash-chain verification. Ordinal 5 and later
  are blocked by the host before RTDE write and by this TP package.
  The ordinal-4 row carries epoch, fresh execution id, controller receipt SHA,
  Script1 receipt SHA, qualification-ledger binding, and output ledger SHA.
  PATH evidence requires genuine state 25, exactly 550 observed 100 ms bins,
  four distinct writer/RTDE/Kunwei/TP rates each >=460 Hz, feedback age p99
  <=10 ms, fresh gap <20 ms, Safety NORMAL, motion proof, contact, and guarded
  return to Home.

Deferred later campaign:
  The offline plan still specifies 3 qualification + Batch A 5 + Batch B 5 +
  retest 3. This release enables only the first anchor PATH trial; ordinal 5+,
  retest, and promotion remain disabled. No full-campaign readiness or
  promotion is claimed by this package.

Binding:
  contract_sha256={contract.sha256}
  campaign_fingerprint={contract.campaign_fingerprint}
  eoat_profile_sha256={contract.eoat_sha256}
  lineage={LINEAGE}, target_force_n={TARGET_FORCE_N:.1f}, D={D_ANCHOR:.1f}

Claim boundary:
  This is a local source/package build only. Upload, read-back, Load, Play,
  contact, motion, sensor writes, and promotion are all false/offline.
"""


def _urp_content(urp: bytes) -> tuple[ET.Element, str, str]:
    root = ET.fromstring(gzip.decompress(urp).decode("utf-8"))
    cached_contents = ""
    script_path = ""
    for node in root.iter():
        tag = node.tag.rsplit("}", 1)[-1]
        if tag == "cachedContents":
            cached_contents = html.unescape(node.text or "")
        elif tag == "file" and node.attrib.get("resolves-to") == "file":
            script_path = (node.text or "").strip()
    return root, cached_contents, script_path


def validate_urscript_block_balance(script: str) -> None:
    """Catch a missing nested ``end`` before a TP byte reaches packaging."""

    starters = re.compile(r"^(def|thread|if|while|for|sec)\b.*:$")
    stack: list[tuple[str, int]] = []
    for line_number, line in enumerate(script.splitlines(), start=1):
        stripped = line.strip()
        match = starters.match(stripped)
        if match:
            stack.append((match.group(1), line_number))
        elif stripped == "end":
            if not stack:
                raise ValueError(f"unmatched end at line {line_number}")
            stack.pop()
    if stack:
        kind, line_number = stack[-1]
        raise ValueError(f"unclosed {kind} block from line {line_number}")


def validate_triplet(script: str, txt: str, urp: bytes, stamp: str) -> dict[str, bool]:
    contract = load_contract()
    root, cached_contents, script_path = _urp_content(urp)
    validate_urscript_block_balance(script)
    validate_register_mappings()
    forbidden = (
        "force_mode(",
        "get_tcp_force(",
        "actual_TCP_force",
        "zero_ftsensor(",
        "freedrive_mode(",
        "movej(",
        "movep(",
        "set_target_payload(",
        "set_tcp(",
    )
    checks = {
        "version_stamp": script.startswith(f"# VERSION: {stamp}\n"),
        "program_name": root.attrib.get("name") == PROGRAM,
        "controller_directory": root.attrib.get("directory") == CONTROLLER_DIR,
        "installation_relative_path": root.attrib.get("installationRelativePath") == "../../../default",
        "script_node_path": script_path == f"{CONTROLLER_DIR}/{PROGRAM}.script",
        "cached_contents_exact": cached_contents == script,
        "contract_binding": contract.sha256 in script
        and contract.campaign_fingerprint in script
        and contract.eoat_sha256 in script,
        "r004_layout": all(
            token in script and token in txt
            for token in ("layout-606", f"EXPECTED_PROGRAM: {PROGRAM}")
        )
        and "LAYOUT_TAG" not in script,
        "resident_loop": "while True:" in script and "READY_HOME_NEXT" in script,
        "session_protocol": all(
            token in script
            for token in (
                "read_input_integer_register(27)",
                "session_sequence > consumed_session_sequence",
                "SessionCommand HOLD=0 ARM=1 COMPLETE=2 STOP=3",
                "write_output_integer_register(34",
            )
        ),
        "full_payload_cache": all(
            token in script
            for token in (
                "codex_r004_packet_payload_equal",
                "sequence > codex_r004_cache_sequence",
                "sequence < codex_r004_cache_sequence",
                "codex_r004_cache_age_s >= 0.080000000",
            )
        )
        and "sequence <= codex_r004_cache_sequence" not in script,
        "baseline_timer_split": all(
            token in script
            for token in (
                "pre_latch_elapsed_s",
                "post_latch_elapsed_s",
                "post_latch_elapsed_s = 0.0",
                "latch_seen = True",
                "latch_seen and latch == 0",
            )
        ),
        "return_home_guard": all(
            token in script
            for token in (
                "local current_pose = get_actual_tcp_pose()",
                "local safe_z = home_pose[2] + 0.005000000",
                "return_guard = return_guard + 1 + 2 + 8",
                "local fixed_home_pose = p[0.487834547, 0.129337053, 0.033000000",
                "local locked_home_q",
                "codex_r004_q_close",
                "movel(transfer_pose, a=0.135, v=0.090, r=0.0)",
                "movel(home_pose, a=0.060, v=0.040, r=0.0)",
            )
        )
        and "0.062863519" not in script,
        "no_auto_home_on_fault": "return codex_r004_fault" in script and "stopl(0.250000000)" in script,
        "script1_is_external": "Script1 owns the verified V4 EOAT setup" in script
        and not any(token in script for token in forbidden),
        "typed_output_mapping": all(
            f"write_output_integer_register({register}" in script
            for register in OUTPUT_INTEGER_FIELDS
        ),
        "wire_maps_all_inputs": all(
            f"read_input_integer_register({register})" in script
            for register in INPUT_INTEGER_REGISTERS
        ),
        "paired_step_tables": "Program-side step table:" in txt
        and "Qualification-bound first PATH trial and ledger:" in txt
        and "Claim boundary:" in txt,
        "offline_only": "Upload, read-back, Load, Play" in txt
        and "all false/offline" in txt,
    }
    failed = [name for name, passed in checks.items() if not passed]
    if failed:
        raise ValueError(f"V4 r004 triplet validation failed: {failed}")
    return checks


def numeric_sanity(script: str) -> dict[str, Any]:
    checks = {
        "target_exact_5n": TARGET_FORCE_N == 5.0 and "TARGET_FORCE_N: 5.0" in script,
        "damping_exact_28": D_ANCHOR == 28.0 and "D_ANCHOR: 28.0" in script,
        "layout_exact_606": "606.0" in script and "layout != 606.0" in script,
        "contact_speed": "-0.000200000" in script,
        "contact_accel": "0.005000000" in script,
        "contact_travel": "0.025000000" in script,
        "contact_timeout": "90.000000000" in script,
        "baseline_pre_20": "pre_latch_elapsed_s + actual_dt > 20.000000000" in script,
        "baseline_post_30": "post_latch_elapsed_s > 30.000000000" in script,
        "path_runtime_60": "path_elapsed_s < 60.000000000" in script,
        "retract_5mm": "local safe_z = home_pose[2] + 0.005000000" in script
        and "current_pose[2] + 0.005000000" in script,
        "fixed_home_direct_route": "0.062863519" not in script
        and "local fixed_home_pose = p[0.487834547, 0.129337053, 0.033000000" in script
        and "movel(transfer_pose, a=0.135, v=0.090, r=0.0)" in script
        and "movel(home_pose, a=0.060, v=0.040, r=0.0)" in script,
        "v3_motion_caps": "speedj(baseline_qdot, 20.000000000, actual_dt)" in script
        and "speedj(path_qdot, 20.000000000, actual_path_dt)" in script,
        "hard_safety_caps": "codex_r004_packet_guard(packet_reason, 60.0, 100.0, 3.0)" in script,
        "no_motion_write_primitives": "set_target_payload(" not in script and "set_tcp(" not in script,
        "no_ur_force": "force_mode(" not in script and "get_tcp_force(" not in script,
        "no_sensor_write": "zero_ftsensor(" not in script
        and "tare(" not in script.lower()
        and "configure(" not in script.lower(),
    }
    if not all(checks.values()):
        raise ValueError(f"V4 r004 numeric sanity failed: {checks}")
    return {
        "schema": "step5d.autotune-v4/r004-numeric-sanity-v1",
        "program": PROGRAM,
        "checks": checks,
        "passed": True,
    }


def write_triplet(
    output_dir: Path, stamp: str, *, replace_existing: bool = False
) -> dict[str, Any]:
    output_dir = Path(output_dir)
    stamp = validate_stamp(stamp)
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
    existing = tuple(
        path
        for path in (*paths.values(), numeric_path, manifest_path)
        if path.exists() or path.is_symlink()
    )
    if existing and not replace_existing:
        raise FileExistsError("V4 r004 output is immutable; existing artifact must not be overwritten")
    if existing and replace_existing:
        if any(path.is_symlink() or not path.is_file() for path in existing):
            raise FileExistsError("r004 replacement target must be a regular local file")
        for path in existing:
            path.unlink()
    paths[".script"].write_text(script, encoding="utf-8")
    paths[".txt"].write_text(txt, encoding="utf-8")
    paths[".urp"].write_bytes(urp)
    numeric_path.write_text(
        json.dumps(sanity, indent=2, sort_keys=True, allow_nan=False) + "\n",
        encoding="utf-8",
    )
    digests = {suffix: sha256_bytes(path.read_bytes()) for suffix, path in paths.items()}
    numeric_sha = sha256_bytes(numeric_path.read_bytes())
    contract = load_contract()
    # Keep the controller delivery manifest deliberately minimal.  Release,
    # campaign, EOAT and runtime bindings live in the hash-bound r004 contract
    # and in-script identity; the controller helper accepts only this strict
    # four-field schema and rejects metadata drift before any write.
    manifest = {
        "schema_version": 1,
        "basename": PROGRAM,
        "controller_directory": CONTROLLER_DIR,
        "artifacts": [
            {
                "filename": f"{PROGRAM}{suffix}",
                "source": f"{PROGRAM}{suffix}",
                "sha256": digests[suffix],
            }
            for suffix in (".script", ".txt", ".urp")
        ],
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
    parser.add_argument(
        "--replace-existing",
        action="store_true",
        help="replace only existing local r004 artifacts after contract regeneration",
    )
    args = parser.parse_args(argv)
    stamp = validate_stamp(args.stamp or source_stamp())
    print(
        json.dumps(
            write_triplet(
                args.output_dir,
                stamp,
                replace_existing=args.replace_existing,
            ),
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
