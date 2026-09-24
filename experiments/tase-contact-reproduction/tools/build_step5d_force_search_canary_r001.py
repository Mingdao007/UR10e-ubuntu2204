#!/usr/bin/env python3
"""Build the immutable new-EOAT 1 N force-search canary TP package."""

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
from step5d_force_search_primitive import (
    CONTROLLER_DIR,
    PROGRAM_NAME,
    load_contract,
    render_script,
)
from step5d_new_eoat import load_new_eoat


ROOT = Path(__file__).resolve().parents[1]
OUTPUT_DIR = ROOT / "programs/step5/step5d"
BLOCKED_MARKER = (
    OUTPUT_DIR / "step5d_force_search_canary_r001.BLOCKED.json"
)


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def source_stamp(now: datetime | None = None) -> str:
    value = now or datetime.now(timezone(timedelta(hours=8)))
    return value.strftime("%Y-%m-%dT%H%MHKT_STEP5D_FORCE_SEARCH_CANARY_R001")


def build_txt(stamp: str) -> str:
    contract = load_contract()
    eoat = load_new_eoat()
    return f"""Step5d new-EOAT force-search canary

Controller target:
  {CONTROLLER_DIR}/{PROGRAM_NAME}.urp

Version:
  {stamp}

Primitive:
  Start at the current stationary pose.
  Require vertical-down tool Z and a fresh zeroed Kunwei wrench.
  Search base -Z at {abs(contract.search_speed_m_s) * 1000.0:.3f} mm/s.
  Latch first contact at abs(normal) >= {contract.contact_abs_normal_force_n:.3f} N
  or force_norm >= {contract.contact_force_norm_n:.3f} N.
  Stop immediately, then retract {contract.retract_distance_m * 1000.0:.1f} mm
  at {contract.retract_speed_m_s * 1000.0:.1f} mm/s on successful contact only.

Hard bounds:
  normal={contract.hard_abs_normal_force_n:.1f} N
  force_norm={contract.hard_force_norm_n:.1f} N
  torque_norm={contract.hard_torque_norm_nm:.1f} Nm
  travel={contract.max_travel_m * 1000.0:.1f} mm
  runtime={contract.runtime_limit_s:.1f} s

EOAT:
  payload={eoat.payload_kg:.3f} kg
  CoG={list(eoat.cog_m)} m
  TCP={list(eoat.tcp_pose_m_rad)}

Excluded:
  Absolute XY/Home, lateral/angular motion, force_mode, Autotune, RNN,
  optimizer, mailbox, hardware zero/tare, and motion after an unsafe stop.
"""


def validate_triplet(script: str, txt: str, urp: bytes, stamp: str) -> dict[str, bool]:
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
        "speedj(",
        "servoj(",
        "zero_ftsensor(",
        "step5d_autotune",
        "0.487834547",
        "0.129337053",
        "0.033000000",
    )
    checks = {
        "stamp": script.startswith(f"# VERSION: {stamp}\n"),
        "program_name": root.attrib.get("name") == PROGRAM_NAME,
        "controller_directory": root.attrib.get("directory") == CONTROLLER_DIR,
        "script_node_path": script_path == f"{CONTROLLER_DIR}/{PROGRAM_NAME}.script",
        "cached_contents_exact": cached_contents == script,
        "one_search_speedl": script.count(
            "speedl([0.0, 0.0, search_speed_m_s, 0.0, 0.0, 0.0]"
        )
        == 1,
        "one_success_retract": script.count("movel(retract_pose") == 1,
        "no_forbidden_behavior": not any(token in script for token in forbidden),
        "txt_identity": PROGRAM_NAME in txt and stamp in txt,
    }
    failed = [name for name, passed in checks.items() if not passed]
    if failed:
        raise ValueError(f"force-search canary triplet validation failed: {failed}")
    return checks


def _manifest(
    stamp: str,
    output_dir: Path,
    digests: dict[str, str],
) -> dict[str, Any]:
    contract = load_contract()
    eoat = load_new_eoat()
    return {
        "schema_version": 2,
        "schema": "step5d.force-search-canary/local-candidate-v1",
        "basename": PROGRAM_NAME,
        "controller_directory": CONTROLLER_DIR,
        "controller_target": f"{CONTROLLER_DIR}/{PROGRAM_NAME}.urp",
        "source_stamp": stamp,
        "status": "local_candidate_not_uploaded",
        "immutable": True,
        "builder": "tools/build_step5d_force_search_canary_r001.py",
        "builder_sha256": _sha256_bytes(Path(__file__).read_bytes()),
        "force_search_contract": {
            "artifact_id": contract.artifact_id,
            "sha256": contract.artifact_sha256,
        },
        "eoat_contract": {
            "artifact_id": eoat.artifact_id,
            "sha256": eoat.artifact_sha256,
        },
        "artifacts": [
            {
                "filename": f"{PROGRAM_NAME}{suffix}",
                "source": f"{PROGRAM_NAME}{suffix}",
                "sha256": digests[suffix],
            }
            for suffix in (".script", ".txt", ".urp")
        ],
        "delivery": {
            "local_urp_internal_content_gate": True,
            "controller_upload": False,
            "controller_readback": False,
            "current_release_pointer_changed": False,
        },
    }


def write_triplet(output_dir: Path, stamp: str) -> dict[str, Any]:
    raise RuntimeError(
        "step5d_force_search_canary_r001 is blocked by Opus review; "
        f"see {BLOCKED_MARKER}"
    )
    # Historical implementation is retained below for forensic source review.
    body = render_script()
    script = f"# VERSION: {stamp}\n{body}"
    txt = build_txt(stamp)
    urp = package_support.build_urp(script, PROGRAM_NAME, CONTROLLER_DIR)
    checks = validate_triplet(script, txt, urp, stamp)
    output_dir.mkdir(parents=True, exist_ok=True)
    paths = {
        ".script": output_dir / f"{PROGRAM_NAME}.script",
        ".txt": output_dir / f"{PROGRAM_NAME}.txt",
        ".urp": output_dir / f"{PROGRAM_NAME}.urp",
    }
    manifest_path = output_dir / f"{PROGRAM_NAME}.deploy-manifest.json"
    outputs = (*paths.values(), manifest_path)
    collisions = [str(path) for path in outputs if path.exists()]
    if collisions:
        raise FileExistsError(
            "force-search candidate is immutable; existing output must not be overwritten: "
            + ", ".join(collisions)
        )
    paths[".script"].write_text(script, encoding="utf-8")
    paths[".txt"].write_text(txt, encoding="utf-8")
    paths[".urp"].write_bytes(urp)
    digests = {
        suffix: _sha256_bytes(path.read_bytes()) for suffix, path in paths.items()
    }
    manifest_path.write_text(
        json.dumps(_manifest(stamp, output_dir, digests), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return {
        "program": PROGRAM_NAME,
        "controller_target": f"{CONTROLLER_DIR}/{PROGRAM_NAME}.urp",
        "stamp": stamp,
        "sha256": digests,
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
