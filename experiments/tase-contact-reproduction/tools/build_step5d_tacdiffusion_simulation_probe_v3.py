#!/usr/bin/env python3
"""Build the ACK-paced v3 TacDiffusion TP Simulation probe.

The controller-side diagnostic Stage25 source is identical to v2.  v3 is a
new immutable package identity that binds the runtime evidence to the ACK-
paced host writer; it does not relax the controller sequence guard.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import html
import json
from pathlib import Path
import xml.etree.ElementTree as ET

from build_step4e_p0p1_programs import build_urp
from build_step5d_tacdiffusion_simulation_probe_v2 import (
    CONTROLLER_DIR,
    DIRECT_TEMPLATE_SHA256,
    diagnostic_stage_source,
)


ROOT = Path(__file__).resolve().parents[1]
PROGRAM_NAME = "step5d_tacdiffusion_direct_torque_simulation_probe_v3"
OUTPUT_DIR = ROOT / "programs/step5/step5d_tacdiffusion"
DEFAULT_STAMP = "2026-07-26TDIAGNOSTIC_STEP5D_TACDIFFUSION_DIRECT_TORQUE_SIMULATION_PROBE_V3"


PROBE_WRAPPER = r'''
def codex_step5d_tacdiffusion_simulation_probe_v3():
  # v3 uses the same diagnostic registers and controller guards as v2.
  # The host writer is ACK-paced and must not advance sequence independently
  # of the controller's acknowledged packet.
  write_output_integer_register(32, 0)
  write_output_integer_register(33, 0)
  write_output_integer_register(34, 0)
  write_output_integer_register(35, 0)
  write_output_integer_register(36, 0)
  write_output_integer_register(37, 0)

  write_output_integer_register(31, 1)
  write_output_float_register(34, 1.0)
  write_output_float_register(35, 25.0)
  local normal_result = codex_step5d_direct_torque_stage25(34)
  write_output_float_register(32, normal_result)

  write_output_integer_register(31, 2)
  write_output_float_register(34, 2.0)
  write_output_float_register(35, 25.0)
  local sequence_fault_result = codex_step5d_direct_torque_stage25(36)
  write_output_float_register(33, sequence_fault_result)

  write_output_integer_register(31, 99)
  write_output_float_register(34, 99.0)
  write_output_float_register(35, 99.0)
  textmsg("TacDiffusion Direct Torque TP Simulation ACK-paced probe complete")
end

codex_step5d_tacdiffusion_simulation_probe_v3()
'''


def sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def render_script(stamp: str) -> str:
    header = f"""# VERSION: {stamp}
# STEP5_STAGE_ID: step5d_tacdiffusion_direct_torque_simulation_probe_v3
# DIRECT_TEMPLATE_SHA256: {DIRECT_TEMPLATE_SHA256}
# SEQUENCE_PACING: ack_gated_host_driver; controller_sequence_guard_unchanged
# EXECUTION_SCOPE: TP Simulation Mode only; physical robot motion is forbidden.
# MOTION_PRIMITIVES: none; only direct_torque() and controlled stopj() exist.
# PURPOSE: validate strict sequence semantics with controller-ACK-paced host
# packets while preserving the v2 diagnostic telemetry and fail-closed guards.
"""
    script = header + diagnostic_stage_source().rstrip() + "\n\n" + PROBE_WRAPPER.lstrip()
    validate_script(script, stamp)
    return script


def validate_script(script: str, stamp: str) -> None:
    required = (
        f"# VERSION: {stamp}",
        "# SEQUENCE_PACING: ack_gated_host_driver; controller_sequence_guard_unchanged",
        "def vic_diag(diagnostic_base, reason, detail):",
        "def vic_safe_exit_tick(decaying_feedforward, diagnostic_base, failure_reason):",
        "def codex_step5d_direct_torque_stage25(diagnostic_base):",
        "write_output_integer_register(34, 0)",
        "write_output_integer_register(36, 0)",
        "codex_step5d_direct_torque_stage25(34)",
        "codex_step5d_direct_torque_stage25(36)",
        "write_output_integer_register(31, 99)",
        "codex_step5d_tacdiffusion_simulation_probe_v3()",
    )
    missing = [marker for marker in required if marker not in script]
    if missing:
        raise ValueError(f"simulation probe v3 missing required markers: {missing}")
    forbidden = (
        "movel(",
        "movej(",
        "movep(",
        "speedl(",
        "speedj(",
        "servoj(",
        "force_mode(",
        "zero_ftsensor",
        "set_tcp(",
        "set_payload(",
        "socket_open(",
    )
    present = [marker for marker in forbidden if marker in script]
    if present:
        raise ValueError(f"simulation probe v3 contains forbidden operations: {present}")
    if "codex_step5d_tacdiffusion_simulation_probe_v2()" in script:
        raise ValueError("v3 wrapper identity drifted to v2")


def build_txt(stamp: str) -> str:
    return f"""Step5d TacDiffusion Direct Torque Simulation ACK-paced probe v3

Open only on the physical 5.26 Teach Pendant with Simulation visibly enabled:
  {CONTROLLER_DIR}/{PROGRAM_NAME}.urp

Version:
  {stamp}

The controller-side Stage25 sequence guard is unchanged from v2. The v3 host
writer advances packet sequence only after the controller acknowledges the
previous sequence. Host release timing remains an absolute 2 ms schedule,
while effective sequence/ACK rate is measured separately and may be below
500 Hz. Diagnostic registers are current 32/33, phase 1 34/35, and phase 2
36/37.

Do not press Play in Real Robot mode. Do not bypass a sequence or safe-exit
guard based on a diagnostic result.
"""


def validate_triplet(script: str, txt: str, urp: bytes, stamp: str) -> None:
    root = ET.fromstring(gzip.decompress(urp).decode("utf-8"))
    cached = ""
    script_file = ""
    for node in root.iter():
        if node.tag == "cachedContents":
            cached = html.unescape(node.text or "")
        elif node.tag == "file" and node.attrib.get("resolves-to") == "file":
            script_file = node.text or ""
    checks = {
        "script stamp": stamp in script,
        "txt stamp": stamp in txt,
        "program name": root.attrib.get("name") == PROGRAM_NAME,
        "controller directory": root.attrib.get("directory") == CONTROLLER_DIR,
        "script file": script_file == f"{CONTROLLER_DIR}/{PROGRAM_NAME}.script",
        "cached script exact": cached == script,
    }
    failed = [name for name, passed in checks.items() if not passed]
    if failed:
        raise ValueError(f"simulation probe v3 triplet validation failed: {failed}")


def write_triplet(output_dir: Path, stamp: str) -> dict[str, object]:
    script = render_script(stamp)
    txt = build_txt(stamp)
    urp = build_urp(script, PROGRAM_NAME, CONTROLLER_DIR)
    validate_triplet(script, txt, urp, stamp)
    output_dir.mkdir(parents=True, exist_ok=True)
    paths = {
        ".script": output_dir / f"{PROGRAM_NAME}.script",
        ".txt": output_dir / f"{PROGRAM_NAME}.txt",
        ".urp": output_dir / f"{PROGRAM_NAME}.urp",
    }
    paths[".script"].write_text(script, encoding="utf-8")
    paths[".txt"].write_text(txt, encoding="utf-8")
    paths[".urp"].write_bytes(urp)
    hashes = {suffix: sha256_bytes(path.read_bytes()) for suffix, path in paths.items()}
    manifest_path = output_dir / f"{PROGRAM_NAME}.deploy.json"
    manifest = {
        "schema_version": 1,
        "basename": PROGRAM_NAME,
        "controller_directory": CONTROLLER_DIR,
        "artifacts": [
            {
                "filename": path.name,
                "source": path.name,
                "sha256": hashes[suffix],
            }
            for suffix, path in sorted(paths.items())
        ],
    }
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return {
        "program": PROGRAM_NAME,
        "controller_dir": CONTROLLER_DIR,
        "stamp": stamp,
        "paths": {suffix: str(path) for suffix, path in paths.items()},
        "sha256": hashes,
        "deploy_manifest": str(manifest_path),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=OUTPUT_DIR)
    parser.add_argument("--stamp", default=DEFAULT_STAMP)
    args = parser.parse_args(argv)
    print(json.dumps(write_triplet(args.output_dir, args.stamp), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
