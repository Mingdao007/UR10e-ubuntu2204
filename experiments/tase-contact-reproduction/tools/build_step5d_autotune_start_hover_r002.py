#!/usr/bin/env python3
"""Build Script1 r002 with the new EOAT initialized before any TCP use."""

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

import build_step5d_autotune_start_hover_r001 as r001
from step5d_new_eoat import load_new_eoat, urscript_initialization_block


ROOT = Path(__file__).resolve().parents[1]
PROGRAM_NAME = "step5d_autotune_start_hover_r002"
CONTROLLER_DIR = r001.CONTROLLER_DIR
LOCAL_PROGRAM_DIR = r001.LOCAL_PROGRAM_DIR
EXPECTED_INSTALLATION_RELATIVE_PATH = r001.EXPECTED_INSTALLATION_RELATIVE_PATH
REVOKED_MARKER = (
    LOCAL_PROGRAM_DIR / "step5d_autotune_start_hover_r002.REVOKED.json"
)


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def source_stamp(now: datetime | None = None) -> str:
    value = now or datetime.now(timezone(timedelta(hours=8)))
    return value.strftime("%Y-%m-%dT%H%MHKT_STEP5D_AUTOTUNE_START_HOVER_R002")


def build_package_script(stamp: str) -> str:
    rendered = r001.build_package_script(stamp)
    rendered = rendered.replace(r001.PROGRAM_NAME, PROGRAM_NAME)
    marker = f"def {PROGRAM_NAME}():\n  local current_pose = get_actual_tcp_pose()"
    if rendered.count(marker) != 1:
        raise ValueError("r001 main-entry marker count differs")
    rendered = rendered.replace(
        marker,
        f"def {PROGRAM_NAME}():\n"
        f"{urscript_initialization_block()}\n"
        "  local current_pose = get_actual_tcp_pose()",
        1,
    )
    validate_rendered_script(rendered, stamp=stamp)
    return rendered


def validate_rendered_script(script: str, *, stamp: str) -> None:
    expected_header = f"# VERSION: {stamp}\n"
    if not script.startswith(expected_header):
        raise ValueError("package script stamp must be the first line")
    r001._validate_urscript_block_balance(script)
    eoat = load_new_eoat()
    initialization = urscript_initialization_block(eoat)
    required = (
        f"# TP_PROGRAM_ID: {PROGRAM_NAME}",
        initialization,
        "local current_pose = get_actual_tcp_pose()",
        "movel(rise_pose, a=0.060, v=0.040, r=0.0)",
        "codex_start_hover_final_stationary_verified(target_pose)",
    )
    missing = [marker for marker in required if marker not in script]
    if missing:
        raise ValueError(f"r002 one-shot script is missing markers: {missing}")
    if r001.PROGRAM_NAME in script:
        raise ValueError("r002 one-shot script retains the r001 program identity")
    if script.count("set_target_payload(") != 1 or script.count("set_tcp(") != 1:
        raise ValueError("r002 must contain exactly one EOAT initialization")
    main_start = script.index(f"def {PROGRAM_NAME}():")
    main = script[main_start:]
    payload_index = main.index("set_target_payload(")
    tcp_index = main.index("set_tcp(")
    pose_index = main.index("get_actual_tcp_pose()")
    motion_index = main.index("movel(")
    if not payload_index < tcp_index < pose_index < motion_index:
        raise ValueError("EOAT initialization must precede TCP observation and motion")
    if script.count(f"{PROGRAM_NAME}()") != 2:
        raise ValueError("r002 function must have exactly one definition call site")


def build_txt(stamp: str) -> str:
    eoat = load_new_eoat()
    return f"""Step5d one-shot campaign Home positioning TP package

Controller target:
  {CONTROLLER_DIR}/{PROGRAM_NAME}.urp

Version:
  {stamp}

Role:
  Standalone no-contact positioning helper for the new 3D-printed EOAT.
  On Play it initializes payload, CoG, and TCP before the first TCP observation
  and before any motion.

EOAT:
  contract_id={eoat.artifact_id}
  contract_sha256={eoat.artifact_sha256}
  payload_kg={eoat.payload_kg:.3f}
  cog_m={list(eoat.cog_m)}
  tcp_pose_m_rad={list(eoat.tcp_pose_m_rad)}

Motion geometry:
  Unchanged from r001: final target p[{r001._format_pose(r001.TARGET_POSE)}],
  three bounded movel segments with r=0.0 and stationary final verification.

Safety scope:
  No contact, force mode, sensor zero/tare, bridge/register protocol, or
  external runtime command path.
"""


def numeric_sanity(stamp: str) -> dict[str, Any]:
    payload = r001.numeric_sanity(stamp)
    eoat = load_new_eoat()
    payload["program"] = PROGRAM_NAME
    payload["controller_target"] = f"{CONTROLLER_DIR}/{PROGRAM_NAME}.urp"
    payload["eoat_initialization"] = {
        "artifact_id": eoat.artifact_id,
        "artifact_sha256": eoat.artifact_sha256,
        "payload_kg": eoat.payload_kg,
        "cog_m": list(eoat.cog_m),
        "tcp_pose_m_rad": list(eoat.tcp_pose_m_rad),
        "before_first_tcp_observation": True,
        "before_first_motion": True,
    }
    return payload


def validate_triplet(script: str, txt: str, urp: bytes, stamp: str) -> dict[str, bool]:
    validate_rendered_script(script, stamp=stamp)
    root = ET.fromstring(gzip.decompress(urp).decode("utf-8"))
    cached_contents = ""
    script_path = ""
    for node in root.iter():
        tag = node.tag.rsplit("}", 1)[-1]
        if tag == "cachedContents":
            cached_contents = html.unescape(node.text or "")
        elif tag == "file" and node.attrib.get("resolves-to") == "file":
            script_path = (node.text or "").strip()
    checks = {
        "script_stamp": stamp in script,
        "txt_stamp": stamp in txt,
        "program_name": root.attrib.get("name") == PROGRAM_NAME,
        "controller_directory": root.attrib.get("directory") == CONTROLLER_DIR,
        "installation_relative_path": root.attrib.get("installationRelativePath")
        == EXPECTED_INSTALLATION_RELATIVE_PATH,
        "script_node_path": script_path == f"{CONTROLLER_DIR}/{PROGRAM_NAME}.script",
        "cached_contents_exact": cached_contents == script,
    }
    failed = [name for name, passed in checks.items() if not passed]
    if failed:
        raise ValueError(f"r002 TP package validation failed: {failed}")
    return checks


def _manifest(
    stamp: str,
    output_dir: Path,
    digests: dict[str, str],
) -> dict[str, Any]:
    eoat = load_new_eoat()
    return {
        "schema_version": 2,
        "schema": "step5d.autotune-start-hover/local-candidate-v1",
        "basename": PROGRAM_NAME,
        "controller_directory": CONTROLLER_DIR,
        "controller_target": f"{CONTROLLER_DIR}/{PROGRAM_NAME}.urp",
        "source_stamp": stamp,
        "status": "local_candidate_not_uploaded",
        "immutable": True,
        "builder": "tools/build_step5d_autotune_start_hover_r002.py",
        "builder_sha256": sha256_bytes(Path(__file__).read_bytes()),
        "local_package_dir": str(output_dir.resolve().relative_to(ROOT)),
        "geometry_basis": {
            "program": r001.PROGRAM_NAME,
            "design": "r001_motion_geometry_plus_pre_observation_new_eoat_initialization",
        },
        "eoat_contract": {
            "artifact_id": eoat.artifact_id,
            "artifact_sha256": eoat.artifact_sha256,
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
        "step5d_autotune_start_hover_r002 is revoked after a protective stop; "
        f"see {REVOKED_MARKER}"
    )
    # Historical implementation is retained below for forensic source review.
    script = build_package_script(stamp)
    txt = build_txt(stamp)
    urp = r001.build_urp(script, PROGRAM_NAME, CONTROLLER_DIR)
    checks = validate_triplet(script, txt, urp, stamp)
    sanity = numeric_sanity(stamp)
    output_dir.mkdir(parents=True, exist_ok=True)
    paths = {
        ".script": output_dir / f"{PROGRAM_NAME}.script",
        ".txt": output_dir / f"{PROGRAM_NAME}.txt",
        ".urp": output_dir / f"{PROGRAM_NAME}.urp",
    }
    manifest_path = output_dir / f"{PROGRAM_NAME}.deploy-manifest.json"
    sanity_path = output_dir / f"{PROGRAM_NAME}.numeric-sanity.json"
    outputs = (*paths.values(), manifest_path, sanity_path)
    collisions = [str(path) for path in outputs if path.exists()]
    if collisions:
        raise FileExistsError(
            "TP candidate is immutable; existing output must not be overwritten: "
            + ", ".join(collisions)
        )
    paths[".script"].write_text(script, encoding="utf-8")
    paths[".txt"].write_text(txt, encoding="utf-8")
    paths[".urp"].write_bytes(urp)
    digests = {
        suffix: sha256_bytes(path.read_bytes()) for suffix, path in paths.items()
    }
    manifest_path.write_text(
        json.dumps(_manifest(stamp, output_dir, digests), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    sanity_path.write_text(
        json.dumps(sanity, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return {
        "program": PROGRAM_NAME,
        "controller_dir": CONTROLLER_DIR,
        "stamp": stamp,
        "paths": {suffix: str(path) for suffix, path in paths.items()},
        "sha256": digests,
        "deploy_manifest": str(manifest_path),
        "numeric_sanity": str(sanity_path),
        "checks": checks,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=LOCAL_PROGRAM_DIR)
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
