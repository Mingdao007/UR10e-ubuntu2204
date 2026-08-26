"""Generate the R013 TP triplet from the already accepted R012 resident.

The motion body and register layout are intentionally inherited byte-for-byte
from the reviewed R012 resident.  Only the R013 readable identity and program
basename change; the host remains the owner of all candidate registers.
"""

from __future__ import annotations

import gzip
import html
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
import json
from pathlib import Path

import build_step4e_p0p1_programs as package_support
from step5d_autotune_v4_r012.path_cbf_live import (
    R012_HARD_TUBE_AXES_M,
    R012_SOFT_CBF_AXES_M,
)
from step5d_autotune_v4_r013.contact_search_strategy import (
    A0_HARD_TWO_STAGE,
    A1_BOUNDED_TANH_SIGMOID,
    a0_strategy,
    a1_strategy,
)


ROOT = Path(__file__).resolve().parents[2]
SOURCE_PROGRAM = "step5d_strict_rnn_autotune_v4_r012"
R013_PROGRAM = "step5d_strict_rnn_autotune_v4_r013"
SOURCE_SCRIPT = ROOT / "programs/step5/step5d" / f"{SOURCE_PROGRAM}.script"
OUTPUT_DIRECTORY = ROOT / "programs/step5/step5d"
CONTROLLER_DIRECTORY = "/programs/andyl/kunwei/step5"
R013_REVISION = 13
R013_RUNTIME_PROTOCOL = 613013
R013_V_FAR_M_S = 0.005
R013_V_NEAR_M_S = 0.0002
R013_A1_PROGRAM = "step5d_strict_rnn_autotune_v4_r013_a1"
R013_A1_REVISION = 14
R013_A1_RUNTIME_PROTOCOL = 613014
R013_LAUNCH_PROFILE = ROOT / "config/step5/step5d_autotune_v4_r013_launch_profile.json"
R012_LAUNCH_PROFILE = ROOT / "config/step5/step5d_autotune_v4_r012_launch_profile.json"


class R013ControllerTripletError(RuntimeError):
    """The local R013 TP transform or package is inconsistent."""


def source_stamp(now: datetime | None = None) -> str:
    moment = now or datetime.now(timezone.utc)
    return moment.strftime("%Y-%m-%dT%H%MZ_STEP5D_AUTOTUNE_V4_R013_613013")


def _validate_balance(script: str) -> None:
    starters = re.compile(r"^(def|thread|if|while|for|sec)\b.*:$")
    stack: list[int] = []
    for line_number, line in enumerate(script.splitlines(), 1):
        stripped = line.strip()
        if starters.match(stripped):
            stack.append(line_number)
        elif stripped == "end":
            if not stack:
                raise R013ControllerTripletError(f"unmatched URScript end at {line_number}")
            stack.pop()
    if stack:
        raise R013ControllerTripletError(f"unclosed URScript block at {stack[-1]}")


def transform_controller_script(source: str, *, stamp: str) -> str:
    if SOURCE_PROGRAM not in source or "while path_elapsed_s < 60.000000000" not in source:
        raise R013ControllerTripletError("R012 source is not the accepted 60-second resident")
    lines = source.splitlines()
    if not lines or not lines[0].startswith("# VERSION: "):
        raise R013ControllerTripletError("source lacks VERSION header")
    lines[0] = f"# VERSION: {stamp}"
    script = "\n".join(lines) + "\n"
    script = script.replace(SOURCE_PROGRAM, R013_PROGRAM)
    script = script.replace("R012", "R013")
    script = script.replace("r012", "r013")
    script = script.replace("612012", str(R013_RUNTIME_PROTOCOL))
    script = script.replace("write_output_integer_register(33, 12)", f"write_output_integer_register(33, {R013_REVISION})")
    script = re.sub(r"local runtime_revision = [-0-9]+", f"local runtime_revision = {R013_REVISION}", script)
    script = re.sub(r"local runtime_extension = [-0-9]+", f"local runtime_extension = {R013_RUNTIME_PROTOCOL}", script)
    script = script.replace("near=0.0005 m/s", f"near={R013_V_NEAR_M_S:g} m/s")
    script = script.replace(
        "local v_near_m_s = 0.000500000",
        f"local v_near_m_s = {R013_V_NEAR_M_S:.9f}",
    )
    script = script.replace(
        "near=0.000500000 m/s",
        f"near={R013_V_NEAR_M_S:.9f} m/s",
    )
    script = script.replace(
        "# ROLE: isolated V4 r013 host-owned 5D autotune over accepted R008 B3 motion",
        "# ROLE: isolated V4 r013 host-owned 6D I-on Bayesian autotune over accepted R008 B3 motion",
    )
    if f"# R013_PROTOCOL: {R013_RUNTIME_PROTOCOL}" not in script:
        raise R013ControllerTripletError("R013 protocol marker is missing")
    _validate_balance(script)
    return script


def _render_a1_sigmoid_helper() -> str:
    """URScript-safe bounded tanh-equivalent using the existing ``pow`` primitive."""

    return """def codex_r013_a1_sigmoid_speed(travel_m, boundary_m, width_m, far_m_s, near_m_s):
  # A1: 10 mm bounded tanh-equivalent pre-brake.  The resident already uses
  # pow(e, x), so the mathematically equivalent logistic form is explicit.
  local start_m = boundary_m - width_m
  if travel_m <= start_m:
    return far_m_s
  elif travel_m >= boundary_m:
    return near_m_s
  end
  local u = (travel_m - start_m) / width_m
  # tanh(6*x) == 2*logistic(12*x)-1.  The factor 12 is therefore part of
  # the canonical A1 contract, not an independently tuneable steepness.
  local z = 12.000000000 * (2.0 * u - 1.0)
  local e_pos = pow(2.718281828, z)
  local e_edge = pow(2.718281828, 12.000000000)
  local sigmoid = e_pos / (1.0 + e_pos)
  local sigmoid_lo = 1.0 / (1.0 + e_edge)
  local sigmoid_hi = e_edge / (1.0 + e_edge)
  local bounded = (sigmoid - sigmoid_lo) / (sigmoid_hi - sigmoid_lo)
  return far_m_s + (near_m_s - far_m_s) * bounded
end

"""


def transform_a1_sigmoid_controller_script(source: str, *, stamp: str) -> str:
    """Create an isolated A1 package while leaving current R013 A0 intact."""

    body = transform_controller_script(source, stamp=stamp)
    old_branch = """        if travel < d_near_start_travel_m and normal_force < f_far_n and force_norm < f_far_n:
          speedl([0.0, 0.0, -v_far_m_s, 0.0, 0.0, 0.0], far_accel_m_s2, contact_dt)
        else:
          speedl([0.0, 0.0, -v_near_m_s, 0.0, 0.0, 0.0], near_accel_m_s2, contact_dt)
        end"""
    new_branch = """        local search_speed_m_s = codex_r013_a1_sigmoid_speed(
          travel, d_near_start_travel_m, 0.010000000, v_far_m_s, v_near_m_s)
        local search_accel_m_s2 = far_accel_m_s2
        if travel >= d_near_start_travel_m:
          search_accel_m_s2 = near_accel_m_s2
        end
        speedl([0.0, 0.0, -search_speed_m_s, 0.0, 0.0, 0.0], search_accel_m_s2, contact_dt)"""
    if old_branch not in body:
        raise R013ControllerTripletError("R013 A0 hard-switch contact branch is missing")
    body = body.replace(old_branch, new_branch, 1)
    marker = "def codex_r006_execute_attempt("
    if marker not in body:
        raise R013ControllerTripletError("R013 execute_attempt marker is missing")
    body = body.replace(marker, _render_a1_sigmoid_helper() + marker, 1)
    body = body.replace(R013_PROGRAM, R013_A1_PROGRAM)
    body = body.replace(str(R013_RUNTIME_PROTOCOL), str(R013_A1_RUNTIME_PROTOCOL))
    body = body.replace(
        f"write_output_integer_register(33, {R013_REVISION})",
        f"write_output_integer_register(33, {R013_A1_REVISION})",
    )
    body = re.sub(r"local runtime_revision = [-0-9]+", f"local runtime_revision = {R013_A1_REVISION}", body)
    body = body.replace(
        "# CONTACT_SEARCH: r008 B3 FAR/NEAR schedule; near=0.0002 m/s; force_fuse<=50N; max=0.025m; timeout=90.0s",
        "# CONTACT_SEARCH: A1 bounded tanh-equivalent sigmoid pre-brake; width=0.010m; far=0.005 m/s; near=0.0002 m/s; force_fuse<=50N",
    )
    body = body.replace(
        "# R008 B3 two-stage contact search (FAR then NEAR); near=0.000200000 m/s.",
        "# R013 A1 bounded tanh-equivalent sigmoid contact search; near=0.000200000 m/s.",
    )
    body = body.replace(
        "# FAR: empty-air approach. NEAR: B3 canary force-search speed. Fuse << 48 N.",
        "# FAR: empty-air approach. A1 smoothly pre-brakes over 10 mm into the reviewed near speed. Fuse << 48 N.",
    )
    lines = body.splitlines()
    lines[0] = f"# VERSION: {stamp}"
    body = "\n".join(lines) + "\n"
    _validate_balance(body)
    return body


def _txt(stamp: str) -> str:
    return (
        f"VERSION={stamp}\n"
        f"PROGRAM={R013_PROGRAM}\n"
        f"REVISION={R013_REVISION}\n"
        "MOTION_PROTOCOL=606006\n"
        f"EXTENSION_PROTOCOL={R013_RUNTIME_PROTOCOL}\n"
        "CAMPAIGN_ID=campaign-unbound\nRUN_ID=run-unbound\nATTEMPT_ID=attempt-unbound\n"
        "ROUTE_ID=r013-route-run-unbound\nSESSION_ID=r013-session-run-unbound\nSESSION_EPOCH=1\n"
        f"CONTROLLER_PATH={CONTROLLER_DIRECTORY}/{R013_PROGRAM}.script\n"
        "MOTION_BASE=R008_B3_TWO_STAGE_WAVE9C\n"
        f"RUNTIME_EXTENSION_PROTOCOL={R013_RUNTIME_PROTOCOL}\n"
        "PATH_EARLY_END_REQUEST=input_integer_register_35\n"
        "PATH_EARLY_END_ACK=output_integer_register_36\n"
        "UPLOAD_PERFORMED=false\nCONTROLLER_READBACK=false\nLIVE_EVIDENCE=false\n"
    )


def _urp_metadata(urp: bytes) -> tuple[str, str, str]:
    root = ET.fromstring(gzip.decompress(urp).decode("utf-8"))
    cached = ""
    script_path = ""
    for node in root.iter():
        tag = node.tag.rsplit("}", 1)[-1]
        if tag == "cachedContents":
            cached = html.unescape(node.text or "")
        elif tag == "file" and node.attrib.get("resolves-to") == "file":
            script_path = (node.text or "").strip()
    return str(root.attrib.get("name", "")), cached, script_path


def validate_controller_triplet(script: str, txt: str, urp: bytes) -> dict[str, bool]:
    name, cached, script_path = _urp_metadata(urp)
    checks = {
        "program_name": name == R013_PROGRAM,
        "script_node_path": script_path == f"{CONTROLLER_DIRECTORY}/{R013_PROGRAM}.script",
        "cached_contents_exact": cached == script,
        "source_stamp": stamp_in(script),
        "path_duration": "while path_elapsed_s < 60.000000000" in script,
        "speedj_motion": "speedj(" in script,
        "serial_registers": "read_input_integer_register(35)" in script and "write_output_integer_register(36" in script,
        "revision_register": f"write_output_integer_register(33, {R013_REVISION})" in script,
        "extension_register": f"write_output_integer_register(34, {R013_RUNTIME_PROTOCOL})" in script,
        "contact_far_speed": f"local v_far_m_s = {R013_V_FAR_M_S:.9f}" in script,
        "contact_near_speed": f"local v_near_m_s = {R013_V_NEAR_M_S:.9f}" in script,
        "no_stale_near_speed": "near=0.000500000 m/s" not in script,
        "contact_safety_constants": all(
            marker in script
            for marker in (
                "ACTIVE_LEASE_S = 0.080000000",
                "local force_fuse_n = 50.000000000",
                "travel >= 0.025000000",
                "contact_elapsed_s >= 90.000000000",
            )
        ),
        "no_r012_identity": SOURCE_PROGRAM not in script and SOURCE_PROGRAM not in txt,
        "gzip_xml": urp[:2] == b"\x1f\x8b",
    }
    if not all(checks.values()):
        raise R013ControllerTripletError(f"R013 triplet validation failed: {checks}")
    return checks


def validate_a1_sigmoid_script(script: str) -> dict[str, bool]:
    checks = {
        "program_name": R013_A1_PROGRAM in script,
        "protocol": f"# R013_PROTOCOL: {R013_A1_RUNTIME_PROTOCOL}" in script,
        "sigmoid_helper": "def codex_r013_a1_sigmoid_speed(" in script,
        "bounded_formula": "local bounded = (sigmoid - sigmoid_lo) / (sigmoid_hi - sigmoid_lo)" in script,
        "canonical_tanh6_equivalence": (
            "local z = 12.000000000 * (2.0 * u - 1.0)" in script
            and "local e_edge = pow(2.718281828, 12.000000000)" in script
        ),
        "ten_mm_width": "0.010000000, v_far_m_s, v_near_m_s" in script,
        "no_a0_branch": "if travel < d_near_start_travel_m and normal_force < f_far_n" not in script,
        "safety_fuse": "local force_fuse_n = 50.000000000" in script,
        "path_duration": "while path_elapsed_s < 60.000000000" in script,
    }
    if not all(checks.values()):
        raise R013ControllerTripletError(f"R013 A1 validation failed: {checks}")
    return checks


def build_a1_sigmoid_canary_triplet(
    output_dir: Path,
    *,
    source_script_path: Path = SOURCE_SCRIPT,
    stamp: str | None = None,
) -> dict[str, Path]:
    """Package A1 separately; this function never overwrites R013 A0."""

    source_path = Path(source_script_path)
    if source_path.is_symlink() or not source_path.is_file():
        raise R013ControllerTripletError(f"source script is unavailable: {source_path}")
    stamp_value = stamp or datetime.now(timezone.utc).strftime(
        "%Y-%m-%dT%H%MZ_STEP5D_AUTOTUNE_V4_R013_A1_613014"
    )
    script = transform_a1_sigmoid_controller_script(
        source_path.read_text(encoding="utf-8"), stamp=stamp_value
    )
    txt = (
        "Step5d R013 A1 bounded sigmoid contact-search canary\n"
        f"PROGRAM={R013_A1_PROGRAM}\nREVISION={R013_A1_REVISION}\n"
        f"EXTENSION_PROTOCOL={R013_A1_RUNTIME_PROTOCOL}\n"
        f"BASELINE={A0_HARD_TWO_STAGE}\n"
        f"CANDIDATE={A1_BOUNDED_TANH_SIGMOID}\n"
        "LIVE_EVIDENCE=false\n"
    )
    urp = package_support.build_urp(script, R013_A1_PROGRAM, CONTROLLER_DIRECTORY)
    checks = validate_a1_sigmoid_script(script)
    output = Path(output_dir)
    output.mkdir(parents=True, exist_ok=True)
    paths: dict[str, Path] = {}
    for suffix, content in (("script", script.encode()), ("txt", txt.encode()), ("urp", urp)):
        path = output / f"{R013_A1_PROGRAM}.{suffix}"
        path.write_bytes(content)
        paths[suffix] = path
    sanity = {
        "schema": "step5d.autotune-v4/r013-a1-contact-search-canary-v1",
        "baseline": a0_strategy().as_dict(),
        "candidate": a1_strategy().as_dict(),
        "triplet_checks": checks,
        "live_evidence": False,
    }
    sanity_path = output / f"{R013_A1_PROGRAM}.numeric-sanity.json"
    sanity_path.write_text(
        json.dumps(sanity, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    paths["numeric-sanity"] = sanity_path
    return paths


def stamp_in(script: str) -> bool:
    return bool(re.search(r"^# VERSION: \d{4}-\d{2}-\d{2}T\d{4}Z_STEP5D_AUTOTUNE_V4_R013_613013$", script, re.MULTILINE))


def build_controller_triplet(
    output_dir: Path = OUTPUT_DIRECTORY,
    *,
    source_script_path: Path = SOURCE_SCRIPT,
    stamp: str | None = None,
) -> dict[str, Path]:
    source_path = Path(source_script_path)
    if source_path.is_symlink() or not source_path.is_file():
        raise R013ControllerTripletError(f"source script is unavailable: {source_path}")
    stamp_value = stamp or source_stamp()
    script = transform_controller_script(source_path.read_text(encoding="utf-8"), stamp=stamp_value)
    txt = _txt(stamp_value)
    urp = package_support.build_urp(script, R013_PROGRAM, CONTROLLER_DIRECTORY)
    validate_controller_triplet(script, txt, urp)
    output = Path(output_dir)
    output.mkdir(parents=True, exist_ok=True)
    paths: dict[str, Path] = {}
    for suffix, content in (("script", script.encode()), ("txt", txt.encode()), ("urp", urp)):
        path = output / f"{R013_PROGRAM}.{suffix}"
        path.write_bytes(content)
        paths[suffix] = path
    profile = json.loads(R012_LAUNCH_PROFILE.read_text(encoding="utf-8"))
    profile["tp_program_id"] = R013_PROGRAM
    profile["hard_tube_policy"] = {"enabled": True, "mode": "host_hard_ellipse"}
    profile["r013_runtime"] = {
        "schema": "step5d.autotune-v4/r013-live-runtime-v1",
        "model_dimensions": 6,
        "integral_policy": "conditional-double-clamp-v1",
        "integral_limit_n_s": 1.0,
        "i_term_authority_error_n": 0.5,
        "soft_tube": {"mode": "active", "semi_axes_m": list(R012_SOFT_CBF_AXES_M)},
        "hard_tube": {
            "enabled": True,
            "independent": True,
            "checked_first": True,
            "axes_m": list(R012_HARD_TUBE_AXES_M),
        },
    }
    R013_LAUNCH_PROFILE.write_text(json.dumps(profile, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    paths["launch_profile"] = R013_LAUNCH_PROFILE
    return paths


if __name__ == "__main__":
    result = build_controller_triplet()
    print(json.dumps({key: str(value) for key, value in result.items()}, sort_keys=True, indent=2))


__all__ = [
    "CONTROLLER_DIRECTORY", "R013ControllerTripletError", "R013_LAUNCH_PROFILE",
    "R013_PROGRAM", "R013_A1_PROGRAM", "R013_REVISION", "R013_A1_REVISION",
    "R013_RUNTIME_PROTOCOL", "R013_A1_RUNTIME_PROTOCOL", "R013_V_FAR_M_S", "R013_V_NEAR_M_S",
    "build_a1_sigmoid_canary_triplet", "build_controller_triplet", "source_stamp",
    "transform_controller_script", "transform_a1_sigmoid_controller_script",
    "validate_a1_sigmoid_script", "validate_controller_triplet",
]
