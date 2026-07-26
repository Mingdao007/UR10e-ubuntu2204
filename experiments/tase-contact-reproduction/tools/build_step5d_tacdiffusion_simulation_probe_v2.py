#!/usr/bin/env python3
"""Build the diagnostic-only v2 TacDiffusion TP Simulation probe.

v1 remains immutable.  This builder adds persistent controller-side
diagnostic telemetry to the same hash-bound Direct Torque Stage25 core; it
does not weaken any packet, lease, runtime, impedance, equilibrium, or
safe-exit guard.
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
from build_step5d_tacdiffusion_tp import DIRECT_TEMPLATE_SHA256, direct_stage_source


ROOT = Path(__file__).resolve().parents[1]
PROGRAM_NAME = "step5d_tacdiffusion_direct_torque_simulation_probe_v2"
CONTROLLER_DIR = "/programs/andyl/kunwei/step5"
OUTPUT_DIR = ROOT / "programs/step5/step5d_tacdiffusion"
DEFAULT_STAMP = "2026-07-26TDIAGNOSTIC_STEP5D_TACDIFFUSION_DIRECT_TORQUE_SIMULATION_PROBE_V2"


DIAGNOSTIC_HELPER = r'''def vic_diag(diagnostic_base, reason, detail):
  # Current diagnosis is volatile; phase diagnosis remains until the next
  # invocation of that phase.  Integer and double register namespaces are
  # independent on RTDE, so these do not alter the existing result registers.
  write_output_integer_register(32, reason)
  write_output_integer_register(33, detail)
  write_output_integer_register(diagnostic_base, reason)
  write_output_integer_register(diagnostic_base + 1, detail)
end

'''


PROBE_WRAPPER = r'''
def codex_step5d_tacdiffusion_simulation_probe_v2():
  # Current and phase-persistent diagnostics.  Phase 1 uses 34/35; phase 2
  # uses 36/37.  The phase marker/result registers are unchanged from v1.
  write_output_integer_register(32, 0)
  write_output_integer_register(33, 0)
  write_output_integer_register(34, 0)
  write_output_integer_register(35, 0)
  write_output_integer_register(36, 0)
  write_output_integer_register(37, 0)

  # Phase 1: valid coherent packets, Direct Torque entry, and normal exit.
  write_output_integer_register(31, 1)
  write_output_float_register(34, 1.0)
  write_output_float_register(35, 25.0)
  local normal_result = codex_step5d_direct_torque_stage25(34)
  write_output_float_register(32, normal_result)

  # Phase 2: the host resets the packet sequence, re-enters Direct Torque,
  # then injects one deliberate sequence gap.
  write_output_integer_register(31, 2)
  write_output_float_register(34, 2.0)
  write_output_float_register(35, 25.0)
  local sequence_fault_result = codex_step5d_direct_torque_stage25(36)
  write_output_float_register(33, sequence_fault_result)

  write_output_integer_register(31, 99)
  write_output_float_register(34, 99.0)
  write_output_float_register(35, 99.0)
  textmsg("TacDiffusion Direct Torque TP Simulation diagnostic probe complete")
end

codex_step5d_tacdiffusion_simulation_probe_v2()
'''


def sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def replace_once(source: str, old: str, new: str, role: str) -> str:
    count = source.count(old)
    if count != 1:
        raise ValueError(f"{role} marker count changed: {count}")
    return source.replace(old, new, 1)


def diagnostic_stage_source() -> str:
    source = direct_stage_source()
    source = source.replace(
        "def vic_safe_exit_tick(decaying_feedforward):",
        DIAGNOSTIC_HELPER + "def vic_safe_exit_tick(decaying_feedforward, diagnostic_base, failure_reason):",
        1,
    )
    source = replace_once(
        source,
        """    if not vic_finite(q[joint], 7.0) or not vic_finite(qd[joint], 1.0) or not vic_finite(coriolis[joint], 20.0):
      return False
    end""",
        """    if not vic_finite(q[joint], 7.0):
      vic_diag(diagnostic_base, failure_reason, 100 + joint)
      return False
    end
    if not vic_finite(qd[joint], 1.0):
      vic_diag(diagnostic_base, failure_reason, 200 + joint)
      return False
    end
    if not vic_finite(coriolis[joint], 20.0):
      vic_diag(diagnostic_base, failure_reason, 300 + joint)
      return False
    end""",
        "safe-exit component diagnostics",
    )
    source = replace_once(
        source,
        """    if not vic_finite(tau[joint], 20.0):
      return False
    end""",
        """    if not vic_finite(tau[joint], 20.0):
      vic_diag(diagnostic_base, failure_reason, 400 + joint)
      return False
    end""",
        "safe-exit torque diagnostics",
    )
    source = replace_once(
        source,
        "def codex_step5d_direct_torque_stage25():",
        "def codex_step5d_direct_torque_stage25(diagnostic_base):",
        "diagnostic stage signature",
    )
    source = replace_once(
        source,
        "  local safe_exit_count = 0\n",
        "  local safe_exit_count = 0\n  vic_diag(diagnostic_base, 0, 0)\n",
        "diagnostic stage clear",
    )
    source = replace_once(
        source,
        "local input_ok = vic_input_valid(eq, stiffness, damping) and vic_fixed_impedance_exact(stiffness, damping)",
        "local input_shape_ok = vic_input_valid(eq, stiffness, damping)\n    local fixed_impedance_ok = vic_fixed_impedance_exact(stiffness, damping)\n    local input_ok = input_shape_ok and fixed_impedance_ok",
        "diagnostic input predicates",
    )
    zero_exit_marker = "if not vic_safe_exit_tick(vic_zero_six()):"
    if source.count(zero_exit_marker) != 2:
        raise ValueError(f"zero-startup/completion marker count changed: {source.count(zero_exit_marker)}")
    source = source.replace(
        zero_exit_marker,
        "if not vic_safe_exit_tick(vic_zero_six(), diagnostic_base, 14):",
        1,
    )
    source = source.replace(
        zero_exit_marker,
        "if not vic_safe_exit_tick(vic_zero_six(), diagnostic_base, 11):",
        1,
    )
    source = replace_once(
        source,
        "if not vic_safe_exit_tick(last_applied_feedforward):",
        "if not vic_safe_exit_tick(last_applied_feedforward, diagnostic_base, 13):",
        "fault safe-exit diagnostic reason",
    )
    source = replace_once(
        source,
        """        else:
          input_ok = False
        end""",
        """        else:
          input_ok = False
          vic_diag(diagnostic_base, 12, 1)
        end""",
        "compute-tau diagnostic reason",
    )
    terminal_anchor = """      end
      if torque_active:
"""
    terminal_diagnostics = """      end
      # Preserve the first failing pre-arm predicate as the code and expose
      # every failed predicate in the detail bitmask.  A compute-tau or
      # safe-exit diagnostic written above is not overwritten when all these
      # predicates are valid.
      local diagnostic_reason = 0
      local diagnostic_detail = 0
      if not coherent_packet:
        diagnostic_reason = 1
      elif not sequence_ok:
        diagnostic_reason = 2
      elif not heartbeat_ok:
        diagnostic_reason = 3
      elif not lease_ok:
        diagnostic_reason = 4
      elif not model_packet_ok:
        diagnostic_reason = 5
      elif not input_shape_ok:
        diagnostic_reason = 6
      elif not fixed_impedance_ok:
        diagnostic_reason = 7
      elif not eq_ok:
        diagnostic_reason = 8
      elif not release_ok:
        diagnostic_reason = 9
      elif not runtime_ok:
        diagnostic_reason = 10
      end
      if not coherent_packet:
        diagnostic_detail = diagnostic_detail + 1
      end
      if not sequence_ok:
        diagnostic_detail = diagnostic_detail + 2
      end
      if not heartbeat_ok:
        diagnostic_detail = diagnostic_detail + 4
      end
      if not lease_ok:
        diagnostic_detail = diagnostic_detail + 8
      end
      if not model_packet_ok:
        diagnostic_detail = diagnostic_detail + 16
      end
      if not input_shape_ok:
        diagnostic_detail = diagnostic_detail + 32
      end
      if not fixed_impedance_ok:
        diagnostic_detail = diagnostic_detail + 64
      end
      if not eq_ok:
        diagnostic_detail = diagnostic_detail + 128
      end
      if not release_ok:
        diagnostic_detail = diagnostic_detail + 256
      end
      if not runtime_ok:
        diagnostic_detail = diagnostic_detail + 512
      end
      if diagnostic_reason > 0:
        vic_diag(diagnostic_base, diagnostic_reason, diagnostic_detail)
      end
      if torque_active:
"""
    source = replace_once(source, terminal_anchor, terminal_diagnostics, "pre-arm diagnostic block")
    return source


def render_script(stamp: str) -> str:
    header = f"""# VERSION: {stamp}
# STEP5_STAGE_ID: step5d_tacdiffusion_direct_torque_simulation_probe_v2
# DIRECT_TEMPLATE_SHA256: {DIRECT_TEMPLATE_SHA256}
# EXECUTION_SCOPE: TP Simulation Mode only; physical robot motion is forbidden.
# MOTION_PRIMITIVES: none; only direct_torque() and controlled stopj() exist.
# PURPOSE: diagnose the first rejected Direct Torque predicate or safe-exit
# component while preserving the v1 packet contract and all fail-closed guards.
"""
    script = header + diagnostic_stage_source().rstrip() + "\n\n" + PROBE_WRAPPER.lstrip()
    validate_script(script, stamp)
    return script


def validate_script(script: str, stamp: str) -> None:
    required = (
        f"# VERSION: {stamp}",
        f"# DIRECT_TEMPLATE_SHA256: {DIRECT_TEMPLATE_SHA256}",
        "def vic_diag(diagnostic_base, reason, detail):",
        "def vic_safe_exit_tick(decaying_feedforward, diagnostic_base, failure_reason):",
        "def codex_step5d_direct_torque_stage25(diagnostic_base):",
        "local input_shape_ok = vic_input_valid(eq, stiffness, damping)",
        "local fixed_impedance_ok = vic_fixed_impedance_exact(stiffness, damping)",
        "vic_diag(diagnostic_base, 12, 1)",
        "write_output_integer_register(34, 0)",
        "write_output_integer_register(36, 0)",
        "codex_step5d_direct_torque_stage25(34)",
        "codex_step5d_direct_torque_stage25(36)",
        "write_output_integer_register(31, 99)",
        "codex_step5d_tacdiffusion_simulation_probe_v2()",
    )
    missing = [marker for marker in required if marker not in script]
    if missing:
        raise ValueError(f"simulation probe v2 missing required markers: {missing}")
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
        raise ValueError(f"simulation probe v2 contains forbidden operations: {present}")
    if script.count("codex_step5d_direct_torque_stage25(34)") != 1:
        raise ValueError("v2 phase 1 must call Stage25 exactly once")
    if script.count("codex_step5d_direct_torque_stage25(36)") != 1:
        raise ValueError("v2 phase 2 must call Stage25 exactly once")


def build_txt(stamp: str) -> str:
    return f"""Step5d TacDiffusion Direct Torque Simulation diagnostic probe v2

Open only on the physical 5.26 Teach Pendant with Simulation visibly enabled:
  {CONTROLLER_DIR}/{PROGRAM_NAME}.urp

Version:
  {stamp}

This package is v1-compatible and diagnostic-only.  It preserves all existing
packet, lease, model, runtime, fixed-impedance, equilibrium, and safe-exit
guards.  Current diagnostic code/detail are output integer registers 32/33;
phase 1 persists in 34/35 and phase 2 persists in 36/37.

Do not press Play in Real Robot mode.  Do not treat a diagnostic code as a
normal pass or as authorization to bypass a controller-side guard.
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
        raise ValueError(f"simulation probe v2 triplet validation failed: {failed}")


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
