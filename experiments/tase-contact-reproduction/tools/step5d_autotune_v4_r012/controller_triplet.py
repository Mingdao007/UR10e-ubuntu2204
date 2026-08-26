"""Build the local R012 controller triplet over the accepted R008 B3 motion.

This is an offline package transform. It adds the protocol-612012 PATH
early-end request/ack seam without uploading, loading, playing, or connecting
to a controller.
"""

from __future__ import annotations

import gzip
import html
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

import build_step4e_p0p1_programs as package_support

from .behavior import R012_PROGRAM, R012_RUNTIME_PROTOCOL
from .descriptor import R012_MOTION_PROTOCOL, R012_REVISION, build_descriptor


ROOT = Path(__file__).resolve().parents[2]
SOURCE_PROGRAM = "step5d_strict_rnn_autotune_v4_r008_b3_two_stage"
SOURCE_SCRIPT = ROOT / "programs/step5/step5d" / f"{SOURCE_PROGRAM}.script"
OUTPUT_DIRECTORY = ROOT / "programs/step5/step5d"
REQUEST_REGISTER = 35
ACK_REGISTER = 36
REASON_SUBTYPE_REGISTER = 35
UNDERLYING_MOTION_PROTOCOL = R012_MOTION_PROTOCOL
CONTROLLER_DIRECTORY = "/programs/andyl/kunwei/step5"
SOURCE_LAUNCH_PROFILE = ROOT / "config/step5/step5d_autotune_v4_r008_b3_two_stage_launch_profile.json"
R012_LAUNCH_PROFILE = ROOT / "config/step5/step5d_autotune_v4_r012_launch_profile.json"


class R012ControllerTripletError(RuntimeError):
    """The local R012 controller transform or package is inconsistent."""


def validate_urscript_block_balance(script: str) -> None:
    starters = re.compile(r"^(def|thread|if|while|for|sec)\b.*:$")
    stack: list[int] = []
    for line_number, line in enumerate(script.splitlines(), 1):
        stripped = line.strip()
        if starters.match(stripped):
            stack.append(line_number)
        elif stripped == "end":
            if not stack:
                raise R012ControllerTripletError(f"unmatched URScript end at {line_number}")
            stack.pop()
    if stack:
        raise R012ControllerTripletError(f"unclosed URScript block at {stack[-1]}")


def source_stamp(now: datetime | None = None) -> str:
    moment = now or datetime.now(timezone.utc)
    return moment.strftime("%Y-%m-%dT%H%MZ_STEP5D_AUTOTUNE_V4_R012_612012")


def _source(path: Path = SOURCE_SCRIPT) -> str:
    path = Path(path)
    if path.is_symlink() or not path.is_file():
        raise R012ControllerTripletError(f"R012 source script is unavailable: {path}")
    text = path.read_text(encoding="utf-8")
    if SOURCE_PROGRAM not in text or "while path_elapsed_s < 60.000000000:" not in text:
        raise R012ControllerTripletError("R012 source is not the expected B3 resident script")
    return text


def transform_controller_script(source: str, *, stamp: str) -> str:
    lines = source.splitlines()
    if not lines or not lines[0].startswith("# VERSION: "):
        raise R012ControllerTripletError("R012 source script has no VERSION header")
    lines[0] = f"# VERSION: {stamp}"
    # The mature B3 script supplies motion, return-home, and physical guards.
    # Its release-only identity comments are deliberately not copied.
    lines = [
        line for line in lines
        if not line.startswith("# V4_")
        and not line.startswith("# EOAT_PROFILE_")
        and not line.startswith("# OUTPUT_INT_24_34:")
    ]
    script = "\n".join(lines) + "\n"
    script = script.replace(SOURCE_PROGRAM, R012_PROGRAM)
    script = script.replace("runtime_hi", "runtime_revision").replace("runtime_lo", "runtime_extension")
    script = re.sub(r"local runtime_revision = [-0-9]+", f"local runtime_revision = {R012_REVISION}", script)
    script = re.sub(r"local runtime_extension = [-0-9]+", f"local runtime_extension = {R012_RUNTIME_PROTOCOL}", script)
    script = script.replace("write_output_integer_register(33, runtime_revision)", f"write_output_integer_register(33, {R012_REVISION})")
    script = script.replace("write_output_integer_register(34, runtime_extension)", f"write_output_integer_register(34, {R012_RUNTIME_PROTOCOL})")
    header_marker = "# ROLE: isolated V4 r008 B3 two-stage contact-search canary"
    if header_marker not in script:
        raise R012ControllerTripletError("R012 source role marker differs")
    script = script.replace(
        header_marker,
        "# ROLE: isolated V4 r012 host-owned 5D autotune over accepted R008 B3 motion\n"
        "# R012_PROTOCOL: 612012 input_int[35]=attempt_sequence -> output_int[36]=ack",
        1,
    )
    path_head = "  local path_elapsed_s = 0.0\n  while path_elapsed_s < 60.000000000:\n"
    replacement_head = (
        "  # R012 clears the ack for every physical attempt before PATH starts.\n"
        f"  write_output_integer_register({REASON_SUBTYPE_REGISTER}, 0)\n"
        f"  write_output_integer_register({ACK_REGISTER}, 0)\n"
        "  local path_elapsed_s = 0.0\n"
        "  local r012_path_early_end = False\n"
        "  while path_elapsed_s < 60.000000000 and not r012_path_early_end:\n"
    )
    if script.count(path_head) != 1:
        raise R012ControllerTripletError("R012 PATH loop head differs")
    script = script.replace(path_head, replacement_head, 1)
    path_body = (
        "    local actual_path_dt = get_steptime()\n"
        "    if actual_path_dt <= 0.0 or actual_path_dt >= 0.080000000:\n"
        "      return codex_r006_fault(epoch, ordinal, token, kind, consumed, 46, runtime_revision, runtime_extension)\n"
        "    end\n"
        "    packet_reason = codex_r006_packet_observe()\n"
        "    guard = codex_r006_packet_guard(packet_reason, 60.0, 100.0, 3.0)\n"
        "    if guard != 0:\n"
        "      return codex_r006_fault(epoch, ordinal, token, kind, consumed, guard, runtime_revision, runtime_extension)\n"
        "    elif read_input_integer_register(25) != 2 or read_input_integer_register(24) < 3:\n"
        "      return codex_r006_fault(epoch, ordinal, token, kind, consumed, 50, runtime_revision, runtime_extension)\n"
        "    end\n"
        "    local path_qdot = [read_input_float_register(37), read_input_float_register(38), read_input_float_register(39), read_input_float_register(40), read_input_float_register(41), read_input_float_register(42)]\n"
        "    speedj(path_qdot, 40.000000000, actual_path_dt)\n"
        "    path_elapsed_s = path_elapsed_s + actual_path_dt\n"
        "    codex_r006_echo(epoch, ordinal, 25, token, 0, consumed, kind, 0, runtime_revision, runtime_extension)\n"
    )
    replacement_body = (
        f"    if read_input_integer_register({REQUEST_REGISTER}) == ordinal:\n"
        f"      write_output_integer_register({REASON_SUBTYPE_REGISTER}, 0)\n"
        f"      write_output_integer_register({ACK_REGISTER}, ordinal)\n"
        "      r012_path_early_end = True\n"
        "    else:\n"
        + "\n".join(f"  {line}" for line in path_body.rstrip("\n").splitlines())
        + "\n    end\n"
    )
    if script.count(path_body) != 1:
        raise R012ControllerTripletError("R012 PATH loop body differs")
    script = script.replace(path_body, replacement_body, 1)
    validate_urscript_block_balance(script)
    return script


def _txt(stamp: str) -> str:
    descriptor = build_descriptor(
        campaign_id="campaign-unbound", run_id="run-unbound", attempt_id="attempt-unbound"
    )
    values = descriptor.as_dict()
    return (
        f"VERSION={stamp}\n"
        f"PROGRAM={values['program']}\n"
        f"REVISION={values['revision']}\n"
        f"MOTION_PROTOCOL={values['motion_protocol']}\n"
        f"EXTENSION_PROTOCOL={values['extension_protocol']}\n"
        f"CAMPAIGN_ID={values['campaign_id']}\n"
        f"RUN_ID={values['run_id']}\n"
        f"ATTEMPT_ID={values['attempt_id']}\n"
        f"ROUTE_ID={values['route_id']}\n"
        f"SESSION_ID={values['session_id']}\n"
        f"SESSION_EPOCH={values['session_epoch']}\n"
        f"CONTROLLER_PATH={CONTROLLER_DIRECTORY}/{R012_PROGRAM}.script\n"
        "MOTION_BASE=R008_B3_TWO_STAGE_WAVE9C\n"
        f"RUNTIME_EXTENSION_PROTOCOL={R012_RUNTIME_PROTOCOL}\n"
        f"PATH_EARLY_END_REQUEST=input_integer_register_{REQUEST_REGISTER}\n"
        f"PATH_EARLY_END_ACK=output_integer_register_{ACK_REGISTER}\n"
        "UPLOAD_PERFORMED=false\nCONTROLLER_READBACK=false\nLIVE_EVIDENCE=false\n"
    )


def _urp_values(urp: bytes) -> tuple[str, str, str]:
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
    program_name, cached, script_path = _urp_values(urp)
    validate_urscript_block_balance(script)
    checks = {
        "program_name": program_name == R012_PROGRAM,
        "script_node_path": script_path == f"{CONTROLLER_DIRECTORY}/{R012_PROGRAM}.script",
        "cached_contents_exact": cached == script,
        "request_sequence_match": f"read_input_integer_register({REQUEST_REGISTER}) == ordinal" in script,
        "ack_sequence_echo": f"write_output_integer_register({ACK_REGISTER}, ordinal)" in script,
        "reason_subtype_zero_on_graceful_end": f"write_output_integer_register({REASON_SUBTYPE_REGISTER}, 0)" in script,
        "ack_cleared_per_attempt": f"write_output_integer_register({ACK_REGISTER}, 0)" in script,
        "graceful_path_exit": "r012_path_early_end = True" in script,
        "legacy_motion_protocol_preserved": f"write_output_integer_register(32, {UNDERLYING_MOTION_PROTOCOL})" in script,
        "revision_register_fixed": f"write_output_integer_register(33, {R012_REVISION})" in script,
        "extension_register_fixed": f"write_output_integer_register(34, {R012_RUNTIME_PROTOCOL})" in script,
        "readable_revision": f"REVISION={R012_REVISION}" in txt and f"local runtime_revision = {R012_REVISION}" in script,
        "readable_motion_protocol": f"MOTION_PROTOCOL={UNDERLYING_MOTION_PROTOCOL}" in txt,
        "readable_extension_protocol": f"EXTENSION_PROTOCOL={R012_RUNTIME_PROTOCOL}" in txt and f"local runtime_extension = {R012_RUNTIME_PROTOCOL}" in script,
        "no_plain_xml": urp[:2] == b"\x1f\x8b",
    }
    if not all(checks.values()):
        raise R012ControllerTripletError(f"R012 controller triplet validation failed: {checks}")
    return checks


def build_controller_triplet(
    output_dir: Path = OUTPUT_DIRECTORY,
    *,
    source_script_path: Path = SOURCE_SCRIPT,
    stamp: str | None = None,
) -> dict[str, Path]:
    stamp_value = stamp or source_stamp()
    script = transform_controller_script(_source(source_script_path), stamp=stamp_value)
    txt = _txt(stamp_value)
    urp = package_support.build_urp(script, R012_PROGRAM, CONTROLLER_DIRECTORY)
    checks = validate_controller_triplet(script, txt, urp)
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    paths: dict[str, Path] = {}
    for suffix, content in (("script", script.encode()), ("txt", txt.encode()), ("urp", urp)):
        path = output_dir / f"{R012_PROGRAM}.{suffix}"
        path.write_bytes(content)
        paths[suffix] = path
    import json
    launch_profile_path = R012_LAUNCH_PROFILE if R012_LAUNCH_PROFILE.is_file() else SOURCE_LAUNCH_PROFILE
    launch_profile = json.loads(launch_profile_path.read_text(encoding="utf-8"))
    launch_profile["tp_program_id"] = R012_PROGRAM
    R012_LAUNCH_PROFILE.write_text(json.dumps(launch_profile, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    paths["launch_profile"] = R012_LAUNCH_PROFILE
    return paths


__all__ = [
    "ACK_REGISTER", "OUTPUT_DIRECTORY", "REASON_SUBTYPE_REGISTER", "REQUEST_REGISTER", "R012ControllerTripletError",
    "SOURCE_PROGRAM", "SOURCE_SCRIPT", "UNDERLYING_MOTION_PROTOCOL", "build_controller_triplet",
    "source_stamp", "transform_controller_script", "validate_controller_triplet",
]
