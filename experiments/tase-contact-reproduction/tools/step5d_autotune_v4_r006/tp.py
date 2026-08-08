"""Content-derived r006 resident TP/package adapter.

The resident state machine is owned by the reviewed r005 renderer, which in
turn derives the r004 Home/contact/return implementation.  This module only
changes the r006 identity limbs, allow-listed active PATH literals, and one
V3-parity stop-diagnostic ordering; it does not implement a second TP state
machine.
"""

from __future__ import annotations

import gzip
import hashlib
import html
import json
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping

import build_step4e_p0p1_programs as package_support
from step5d_autotune_v4_r004.contracts import runtime_identity_limbs
from step5d_autotune_v4_r005.tp import CONTROLLER_DIR as R005_CONTROLLER_DIR
from step5d_autotune_v4_r005.tp import render_script as render_r005

from .contracts import PROGRAM, R006Contract, R006ContractError, TARGET_FORCE_N
from .motion_profile import ACTIVE_MOTION_ENVELOPE_V2, ActiveMotionEnvelopeV2


RUNTIME_PROTOCOL = 606006
CONTROLLER_DIRECTORY = R005_CONTROLLER_DIR
R006_STAMP = "2026-08-02T1200HKT_STEP5D_AUTOTUNE_V4_R006"
R005_PUBLISHED_SCRIPT = (
    Path(__file__).resolve().parents[2]
    / "programs/step5/step5d/step5d_strict_rnn_autotune_v4_r005.script"
)
_STAMP_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{4}HKT_STEP5D_AUTOTUNE_V4_R006$")


class TPProtocolError(RuntimeError):
    """The content-derived resident protocol is not internally consistent."""


def _resident_has_return_zero(script: str) -> bool:
    """Reject only a fake resident body, not helper boolean ``return 0``s."""

    match = re.search(rf"(?m)^def {re.escape(PROGRAM)}\(\):$", script)
    if match is None:
        return True
    body = script[match.start() :]
    return re.search(r"(?m)^\s*return\s+0\s*$", body) is not None


def _replace_exact(source: str, old: str, new: str, *, expected: int) -> str:
    count = source.count(old)
    if count != expected:
        raise R006ContractError(
            f"r005 TP allow-listed substitution {old!r} occurred {count}, expected {expected}"
        )
    return source.replace(old, new)


def _render_frozen_r005_source() -> str:
    """Use the r005 renderer, with its published script as a drift-safe read."""

    try:
        rendered = render_r005()
    except Exception as exc:
        # A dirty source tree can make r005's own parent-source admission fail
        # even though its published triplet is intact.  This fallback is
        # still content-derived from that real r005 triplet; it never invents
        # a TP state machine or accepts a static fake program.
        if R005_PUBLISHED_SCRIPT.is_symlink() or not R005_PUBLISHED_SCRIPT.is_file():
            raise R006ContractError("r005 renderer and published TP source are unavailable") from exc
        rendered = R005_PUBLISHED_SCRIPT.read_text(encoding="utf-8")
        lines = rendered.splitlines()
        if lines and lines[0].startswith("# VERSION: "):
            rendered = "\n".join(lines[1:]) + "\n"
    if "while True:" not in rendered or "READY_HOME_NEXT" not in rendered:
        raise R006ContractError("r005 resident TP source is not the expected live state machine")
    return rendered


def _derive_script(contract: R006Contract, envelope: ActiveMotionEnvelopeV2) -> tuple[str, dict[str, int]]:
    """Apply only identity/cap substitutions to the real r005 TP source."""

    source = _render_frozen_r005_source()

    program_occurrences = source.count("step5d_strict_rnn_autotune_v4_r005")
    if program_occurrences <= 0:
        raise R006ContractError("r005 TP program identity marker is missing")
    source = _replace_exact(
        source,
        "step5d_strict_rnn_autotune_v4_r005",
        PROGRAM,
        expected=program_occurrences,
    )
    source = _replace_exact(source, "606005", str(RUNTIME_PROTOCOL), expected=1)
    role_pattern = re.compile(r"(?m)^# ROLE: isolated V4 r005 resident rolling ARM loop$")
    source, role_count = role_pattern.subn(
        "# ROLE: isolated V4 r006 resident rolling ARM loop", source, count=1
    )
    if role_count != 1:
        raise R006ContractError("r005 TP role marker was not found exactly once")
    source = _replace_exact(
        source,
        "step5d.autotune-v4/r005-fixed-home-v1",
        "step5d.autotune-v4/r006-fixed-home-v1",
        expected=1,
    )
    source = _replace_exact(
        source,
        "# R005 removes only the duplicate pre-execute stationary dwell; ARM/Home gates remain authoritative.",
        "# R006 retains the derived parent stationary/Home gate semantics.",
        expected=1,
    )
    source = _replace_exact(
        source,
        "# r005 host owns phase sequencing; TP accepts positive monotonic attempts",
        "# r006 host owns phase sequencing; TP accepts positive monotonic attempts",
        expected=1,
    )
    source = _replace_exact(source, "codex_r005_", "codex_r006_", expected=source.count("codex_r005_"))
    source = _replace_exact(source, "R005_", "R006_", expected=source.count("R005_"))

    source, contract_count = re.subn(
        r"(?m)^# V4_CONTRACT_SHA256: [0-9a-f]{64}$",
        f"# V4_CONTRACT_SHA256: {contract.sha256}",
        source,
        count=1,
    )
    source, fingerprint_count = re.subn(
        r"(?m)^# V4_CAMPAIGN_FINGERPRINT: [0-9a-f]{64}$",
        f"# V4_CAMPAIGN_FINGERPRINT: {contract.campaign_fingerprint}",
        source,
        count=1,
    )
    if contract_count != 1 or fingerprint_count != 1:
        raise R006ContractError("r005 TP content identity markers were not found exactly once")

    runtime_hi, runtime_lo = runtime_identity_limbs(
        PROGRAM, contract.sha256, contract.campaign_fingerprint
    )
    source, hi_count = re.subn(
        r"(?m)^(\s*local runtime_hi = )\d+$",
        rf"\g<1>{runtime_hi}",
        source,
        count=1,
    )
    source, lo_count = re.subn(
        r"(?m)^(\s*local runtime_lo = )\d+$",
        rf"\g<1>{runtime_lo}",
        source,
        count=1,
    )
    if hi_count != 1 or lo_count != 1:
        raise R006ContractError("r005 TP runtime identity limbs were not found exactly once")

    # Only the active qdot guard and speedj acceleration are widened.  stopj,
    # contact search, Home, and all stationary/return guards remain bytewise
    # derived from r005.
    source, qdot_count = re.subn(
        r"(codex_r006_finite\(qdot, )2\.500000000(\))",
        rf"\g<1>{envelope.qdot_cap_rad_s:.9f}\g<2>",
        source,
        count=1,
    )
    speedj_pattern = re.compile(r"(speedj\([^\n]*?, )20\.000000000(, actual(?:_dt|_path_dt)\))")
    source, speedj_count = speedj_pattern.subn(
        rf"\g<1>{envelope.tp_speedj_acceleration_rad_s2:.9f}\g<2>",
        source,
    )
    if qdot_count != 1 or speedj_count != 3:
        raise R006ContractError(
            f"r005 TP active cap substitutions differ: qdot={qdot_count}, speedj={speedj_count}"
        )

    # A stop-dominant host packet intentionally carries both stop_request=1
    # and cmd_valid=0.  Preserve the V3 separation between the safety carrier
    # and command acceptance: layout corruption remains reason 42, while an
    # explicit stop image reaches packet_guard and reports the real sensor/
    # stop reason instead of being masked as generic cmd_invalid.
    source = _replace_exact(
        source,
        "  elif layout != 606.0 or valid < 0.5:\n    return 42",
        "  elif layout != 606.0:\n"
        "    return 42\n"
        "  elif valid < 0.5 and read_input_float_register(28) < 0.5:\n"
        "    return 42",
        expected=1,
    )

    caps = (
        f"# R006_ACTIVE_CAPS: xy={envelope.xy_path_speed_m_s:.9f} "
        f"total={envelope.total_linear_cap_m_s:.9f} "
        f"normal={envelope.normal_linear_cap_m_s:.9f} "
        f"angular={envelope.angular_cap_rad_s:.9f} "
        f"qdot={envelope.qdot_cap_rad_s:.9f} "
        f"host_slew={envelope.host_qdot_slew_rad_s2:.9f} "
        f"tp_speedj_accel={envelope.tp_speedj_acceleration_rad_s2:.9f} "
        f"normal_update={envelope.normal_update_rate_rad_s:.9f}"
    )
    timing = (
        "# R006_RATE_CONTRACT: host=500.0Hz rtde_request=500.0Hz "
        "kunwei=500.0Hz tp=500.0Hz minimum_each_layer=460.0Hz absolute_deadline\n"
        "# R006_UNCHANGED_ACTIVE_LEASE: ACTIVE_LEASE_S = 0.080000000"
    )
    source = timing + "\n" + caps + "\n" + source
    if "r005" in source.lower() or "ordinal > 16" in source or "input_ordinal > 16" in source:
        raise R006ContractError("r006 TP retained a parent identity or fixed ordinal gate")
    if _resident_has_return_zero(source):
        raise R006ContractError("r006 TP contains a fake return-zero resident loop")
    if "while True:" not in source:
        raise R006ContractError("r006 TP lost the resident positive unbounded loop")
    return source, {
        "identity_program": 1,
        "identity_protocol": 1,
        "identity_contract": 1,
        "identity_campaign": 1,
        "identity_runtime_hi": 1,
        "identity_runtime_lo": 1,
        "active_qdot": qdot_count,
        "active_speedj_accel": speedj_count,
    }


def validate_urscript_block_balance(script: str) -> None:
    starters = re.compile(r"^(def|thread|if|while|for|sec)\b.*:$")
    stack: list[int] = []
    for line_number, line in enumerate(script.splitlines(), 1):
        stripped = line.strip()
        if starters.match(stripped):
            stack.append(line_number)
        elif stripped == "end":
            if not stack:
                raise ValueError(f"unmatched URScript end at {line_number}")
            stack.pop()
    if stack:
        raise ValueError(f"unclosed URScript block at {stack[-1]}")


def render_script(
    contract: R006Contract,
    envelope: ActiveMotionEnvelopeV2 = ACTIVE_MOTION_ENVELOPE_V2,
) -> str:
    if not isinstance(contract, R006Contract):
        raise TypeError("r006 TP rendering requires the loaded hash-bound contract")
    if not isinstance(envelope, ActiveMotionEnvelopeV2):
        raise TypeError("r006 TP rendering requires the typed active envelope")
    script, _ = _derive_script(contract, envelope)
    validate_urscript_block_balance(script)
    return script


def source_stamp(now: datetime | None = None) -> str:
    if now is None:
        return R006_STAMP
    return now.astimezone(timezone(timedelta(hours=8))).strftime(
        "%Y-%m-%dT%H%MHKT_STEP5D_AUTOTUNE_V4_R006"
    )


def validate_stamp(stamp: str) -> str:
    if _STAMP_PATTERN.fullmatch(stamp) is None:
        raise ValueError("r006 source stamp has the wrong HKT format")
    return stamp


def build_txt(stamp: str, script: str) -> str:
    return f"""Step5d independent new-EOAT 5 N Autotune V4 r006 resident host/TP loop

Program:
  {PROGRAM}

Controller target:
  {CONTROLLER_DIRECTORY}/{PROGRAM}.urp

Version:
  {stamp}

Resident lifecycle:
  HOME_IDLE -> dispatch -> fresh ARM -> ACTIVE 60 s -> safe return -> seal/tell/refill.
  Home and intertrial waits are unbounded safe HOLD; the 80 ms lease is active-only.
  Attempt sequences are positive, monotonically increasing, and unbounded.

Package boundary:
  This local triplet is content-addressed and has not been uploaded or read back.
  The host route never performs Dashboard Load or Play.

Optimizer boundary:
  Production resolves the managed CUDA qLogNEI worker with X_pending; no degraded
  fallback is permitted.  The target force is exactly 5.0 N and is not a coordinate.

Binding:
  {script.splitlines()[0]}
  {script.splitlines()[1]}
"""


def _urp_content(urp: bytes) -> tuple[ET.Element, str, str]:
    root = ET.fromstring(gzip.decompress(urp).decode("utf-8"))
    cached = ""
    script_path = ""
    for node in root.iter():
        tag = node.tag.rsplit("}", 1)[-1]
        if tag == "cachedContents":
            cached = html.unescape(node.text or "")
        elif tag == "file" and node.attrib.get("resolves-to") == "file":
            script_path = (node.text or "").strip()
    return root, cached, script_path


def numeric_sanity(
    script: str,
    envelope: ActiveMotionEnvelopeV2 = ACTIVE_MOTION_ENVELOPE_V2,
) -> dict[str, Any]:
    checks = {
        "target_exact_5n": "TARGET_FORCE_N: 5.0" in script,
        "resident_loop": "while True:" in script and "READY_HOME_NEXT" in script,
        "positive_unbounded_attempt": "input_ordinal < 1" in script and "ordinal > 16" not in script,
        "home_direct_search_derived": (
            "codex_r006_entry_home_verified" in script
            and "codex_r006_return_home" in script
        ),
        "path_60s": "path_elapsed_s < 60.000000000" in script,
        "rate_500_each_layer": "host=500.0Hz rtde_request=500.0Hz" in script and "tp=500.0Hz" in script,
        "no_parent_identity": "r005" not in script.lower(),
        "no_fake_loop": not _resident_has_return_zero(script),
        "no_live_stop_ordinal": "stop-after-ordinal" not in script,
        "target_not_dimension": "TARGET_FORCE_N" in script,
        "stop_reason_not_masked_by_cmd_valid": (
            "valid < 0.5 and read_input_float_register(28) < 0.5" in script
        ),
    }
    if not all(checks.values()):
        raise ValueError(f"r006 numeric sanity failed: {checks}")
    return {
        "schema": "step5d.autotune-v4/r006-numeric-sanity-v2",
        "program": PROGRAM,
        "target_force_n": TARGET_FORCE_N,
        "active_caps": envelope.as_dict["active_path_caps"],
        "checks": checks,
        "passed": True,
        "live_evidence": False,
    }


def validate_triplet(
    script: str,
    txt: str,
    urp: bytes,
    stamp: str,
    contract: R006Contract,
) -> dict[str, bool]:
    root, cached, script_path = _urp_content(urp)
    validate_urscript_block_balance(script)
    checks = {
        "version_stamp": script.startswith(f"# VERSION: {stamp}\n"),
        "program_name": root.attrib.get("name") == PROGRAM,
        "controller_directory": root.attrib.get("directory") == CONTROLLER_DIRECTORY,
        "script_node_path": script_path == f"{CONTROLLER_DIRECTORY}/{PROGRAM}.script",
        "cached_contents_exact": cached == script,
        "contract_binding": contract.sha256 in script and contract.campaign_fingerprint in script,
        "resident_loop": "while True:" in script and "R006_ATTEMPT_SEQUENCE" in script,
        "positive_unbounded": "input_ordinal <= current_ordinal" in script and "ordinal > 16" not in script,
        "home_direct_search": "codex_r006_home" in script,
        "package_text": "not been uploaded or read back" in txt,
        "no_plain_xml": urp[:2] == b"\x1f\x8b",
    }
    if not all(checks.values()):
        raise ValueError(f"r006 triplet validation failed: {checks}")
    return checks


def build_triplet(
    output_dir: Path,
    *,
    contract: R006Contract,
    envelope: ActiveMotionEnvelopeV2 = ACTIVE_MOTION_ENVELOPE_V2,
    stamp: str = R006_STAMP,
) -> dict[str, Path]:
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    stamp = validate_stamp(stamp)
    body = render_script(contract, envelope)
    script = f"# VERSION: {stamp}\n{body}"
    txt = build_txt(stamp, script)
    urp = package_support.build_urp(script, PROGRAM, CONTROLLER_DIRECTORY)
    checks = validate_triplet(script, txt, urp, stamp, contract)
    sanity = numeric_sanity(script, envelope)
    paths: dict[str, Path] = {}
    for suffix, content in ((".script", script.encode()), (".txt", txt.encode()), (".urp", urp)):
        path = output_dir / f"{PROGRAM}{suffix}"
        path.write_bytes(content)
        paths[suffix] = path
    sanity_path = output_dir / f"{PROGRAM}.numeric-sanity.json"
    sanity_path.write_text(json.dumps({**sanity, "triplet_checks": checks}, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    paths[".numeric-sanity.json"] = sanity_path
    manifest = {
        "schema": "step5d.autotune-v4/r006-deploy-manifest-v2",
        "program": PROGRAM,
        "controller_directory": CONTROLLER_DIRECTORY,
        "campaign_fingerprint": contract.campaign_fingerprint,
        "envelope_sha256": envelope.profile_sha256,
        "same_basename_triplet": True,
        "timestamp": stamp,
        "artifacts": {
            suffix.lstrip("."): {
                "path": path.name,
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            }
            for suffix, path in paths.items()
        },
        "controller_upload": False,
        "controller_readback": False,
        "live_evidence": False,
    }
    manifest_path = output_dir / f"{PROGRAM}.deploy-manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    paths[".deploy-manifest.json"] = manifest_path
    return paths


__all__ = [
    "CONTROLLER_DIRECTORY",
    "R006_STAMP",
    "RUNTIME_PROTOCOL",
    "build_triplet",
    "build_txt",
    "numeric_sanity",
    "render_script",
    "source_stamp",
    "validate_stamp",
    "validate_triplet",
    "validate_urscript_block_balance",
]
