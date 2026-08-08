"""R009 renderer and offline TP triplet builder.

The resident body is derived from the validated r006 renderer.  R009 applies
only counted identity/protocol substitutions plus the extended diagnostic
publication seam; it does not recreate or patch the pinned r004/r005/r006
generated programs.
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
from step5d_autotune_v4_r006.contracts import load_contract as load_r006_contract
from step5d_autotune_v4_r006.motion_profile import ACTIVE_MOTION_ENVELOPE_V2
from step5d_autotune_v4_r006.tp import (
    CONTROLLER_DIRECTORY as R006_CONTROLLER_DIRECTORY,
    render_script as render_r006,
)

from .identity import R009BehaviorManifest, R009_RUNTIME_PROTOCOL, R009_PROGRAM


ROOT = Path(__file__).resolve().parents[2]
PROGRAM = R009_PROGRAM
CONTROLLER_DIRECTORY = R006_CONTROLLER_DIRECTORY
R009_STAMP = "2026-08-08T1200HKT_STEP5D_AUTOTUNE_V4_R009"
_STAMP_PATTERN = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{4}HKT_STEP5D_AUTOTUNE_V4_R009$"
)


class R009TPError(RuntimeError):
    """The R009 renderer cannot prove safe derivation from r006."""


def _behavior_manifest(value: Any) -> R009BehaviorManifest:
    if isinstance(value, R009BehaviorManifest):
        return value
    candidate = getattr(value, "behavior_manifest", None)
    if isinstance(candidate, R009BehaviorManifest):
        return candidate
    raise TypeError("R009 TP rendering requires a typed behavior manifest or contract")


def _resident_has_return_zero(script: str) -> bool:
    match = re.search(rf"(?m)^def {re.escape(PROGRAM)}\(\):$", script)
    if match is None:
        return True
    body = script[match.start() :]
    return re.search(r"(?m)^\s*return\s+0\s*$", body) is not None


def _replace_exact(source: str, old: str, new: str, *, expected: int) -> str:
    count = source.count(old)
    if count != expected:
        raise R009TPError(
            f"R009 counted substitution {old!r} occurred {count}, expected {expected}"
        )
    return source.replace(old, new)


def _replace_pattern(source: str, pattern: str, replacement: str, *, expected: int) -> str:
    result, count = re.subn(pattern, replacement, source, count=expected)
    if count != expected:
        raise R009TPError(
            f"R009 counted pattern substitution {pattern!r} occurred {count}, expected {expected}"
        )
    return result


def _derive_script(behavior_manifest: R009BehaviorManifest) -> str:
    if not isinstance(behavior_manifest, R009BehaviorManifest):
        raise TypeError("R009 TP rendering requires a typed behavior manifest")
    try:
        parent = load_r006_contract()
        source = render_r006(parent, envelope=ACTIVE_MOTION_ENVELOPE_V2)
    except Exception as exc:
        raise R009TPError(
            "takeover_required: validated r006 derivation is unavailable"
        ) from exc

    source = _replace_pattern(
        source,
        r"(?m)^# V4_CONTRACT_SHA256: [0-9a-f]{64}$",
        f"# R009_BEHAVIOR_MANIFEST_SHA256: {behavior_manifest.behavior_manifest_sha256}",
        expected=1,
    )
    source = _replace_pattern(
        source,
        r"(?m)^# V4_CAMPAIGN_FINGERPRINT: [0-9a-f]{64}$",
        f"# R009_CAMPAIGN_FINGERPRINT: {behavior_manifest.campaign_fingerprint}",
        expected=1,
    )

    program_count = source.count("step5d_strict_rnn_autotune_v4_r006")
    if program_count <= 0:
        raise R009TPError("r006 TP program identity marker is missing")
    source = _replace_exact(
        source,
        "step5d_strict_rnn_autotune_v4_r006",
        PROGRAM,
        expected=program_count,
    )
    lower_prefix_count = source.count("codex_r006_")
    if lower_prefix_count <= 0:
        raise R009TPError("r006 TP function identity marker is missing")
    source = _replace_exact(
        source,
        "codex_r006_",
        "codex_r009_",
        expected=lower_prefix_count,
    )
    upper_count = source.count("R006")
    if upper_count <= 0:
        raise R009TPError("r006 TP uppercase identity marker is missing")
    source = _replace_exact(source, "R006", "R009", expected=upper_count)
    text_count = source.count("r006")
    if text_count <= 0:
        raise R009TPError("r006 TP lowercase identity marker is missing")
    source = _replace_exact(source, "r006", "r009", expected=text_count)
    source = _replace_exact(source, "606006", str(R009_RUNTIME_PROTOCOL), expected=1)

    source = _replace_exact(
        source,
        "# OUTPUT_INT_24_34: epoch, ordinal, state, token, reason, consumed_session_seq, kind, return_guard, runtime_protocol, digest_hi, digest_lo\n"
        "# OUTPUT_DOUBLE_24: latest packet_sequence actually consumed by the TP loop\n",
        "# R009_OUTPUT_INT_24_35: epoch, ordinal, state, token, reason, consumed_session_seq, kind, return_guard, runtime_protocol, digest_hi, digest_lo, reason43_subtype\n"
        "# R009_OUTPUT_DOUBLE_24_27: consumed_or_cached_packet_sequence, observed_packet_sequence, cached_packet_sequence, cache_age_s\n",
        expected=1,
    )

    runtime_hi, runtime_lo = runtime_identity_limbs(
        PROGRAM,
        behavior_manifest.behavior_manifest_sha256,
        behavior_manifest.campaign_fingerprint,
    )
    source = _replace_pattern(
        source,
        r"(?m)^(\s*local runtime_hi = )\d+$",
        rf"\g<1>{runtime_hi}",
        expected=1,
    )
    source = _replace_pattern(
        source,
        r"(?m)^(\s*local runtime_lo = )\d+$",
        rf"\g<1>{runtime_lo}",
        expected=1,
    )

    global_marker = "global codex_r009_cache_age_s = 0.0\n"
    source = _replace_exact(
        source,
        global_marker,
        global_marker
        + "global codex_r009_observed_sequence = -1.0\n"
        + "global codex_r009_terminal_reason43_subtype = 0\n"
        + "global codex_r009_stationary_reason = 0\n"
        + "global codex_r009_reason43_subtype = 0\n",
        expected=1,
    )
    source = _replace_exact(
        source,
        "def codex_r009_packet_observe():\n",
        "def codex_r009_latch_reason43_subtype(subtype):\n"
        "  if codex_r009_terminal_reason43_subtype == 0:\n"
        "    codex_r009_terminal_reason43_subtype = subtype\n"
        "  end\n"
        "end\n\n"
        "def codex_r009_packet_observe():\n"
        "  codex_r009_reason43_subtype = 0\n",
        expected=1,
    )
    source = _replace_exact(
        source,
        "  local sequence = read_input_float_register(46)\n",
        "  local sequence = read_input_float_register(46)\n"
        "  codex_r009_observed_sequence = sequence\n",
        expected=1,
    )
    source = _replace_exact(
        source,
        "  elif sequence < codex_r009_cache_sequence:\n    return 43\n",
        "  elif sequence < codex_r009_cache_sequence:\n"
        "    codex_r009_reason43_subtype = 1\n"
        "    codex_r009_latch_reason43_subtype(1)\n"
        "    return 43\n",
        expected=1,
    )
    source = _replace_exact(
        source,
        "  elif not codex_r009_packet_payload_equal():\n    return 43\n",
        "  elif not codex_r009_packet_payload_equal():\n"
        "    codex_r009_reason43_subtype = 2\n"
        "    codex_r009_latch_reason43_subtype(2)\n"
        "    return 43\n",
        expected=1,
    )
    source = _replace_exact(
        source,
        "  if codex_r009_cache_age_s >= 0.080000000:\n    return 43\n",
        "  if codex_r009_cache_age_s >= 0.080000000:\n"
        "    codex_r009_reason43_subtype = 3\n"
        "    codex_r009_latch_reason43_subtype(3)\n"
        "    return 43\n",
        expected=1,
    )

    echo_old = """def codex_r009_echo(epoch, ordinal, state, token, reason, consumed, kind, return_guard, runtime_hi, runtime_lo):
  write_output_float_register(24, codex_r009_cache_sequence)
  write_output_integer_register(24, epoch)
  write_output_integer_register(25, ordinal)
  write_output_integer_register(26, state)
  write_output_integer_register(27, token)
  write_output_integer_register(28, reason)
  write_output_integer_register(29, consumed)
  write_output_integer_register(30, kind)
  write_output_integer_register(31, return_guard)
  write_output_integer_register(32, 609009)
  write_output_integer_register(33, runtime_hi)
  write_output_integer_register(34, runtime_lo)
end
    """.rstrip()
    echo_new = """def codex_r009_echo(epoch, ordinal, state, token, reason, consumed, kind, return_guard, runtime_hi, runtime_lo):
  if reason == 43:
    codex_r009_reason43_subtype = codex_r009_terminal_reason43_subtype
  else:
    codex_r009_reason43_subtype = 0
    codex_r009_terminal_reason43_subtype = 0
  end
  write_output_float_register(24, codex_r009_cache_sequence)
  write_output_float_register(25, codex_r009_observed_sequence)
  write_output_float_register(26, codex_r009_cache_sequence)
  write_output_float_register(27, codex_r009_cache_age_s)
  write_output_integer_register(24, epoch)
  write_output_integer_register(25, ordinal)
  write_output_integer_register(26, state)
  write_output_integer_register(27, token)
  write_output_integer_register(28, reason)
  write_output_integer_register(29, consumed)
  write_output_integer_register(30, kind)
  write_output_integer_register(31, return_guard)
  write_output_integer_register(32, 609009)
  write_output_integer_register(33, runtime_hi)
  write_output_integer_register(34, runtime_lo)
  write_output_integer_register(35, codex_r009_reason43_subtype)
end
    """.rstrip()
    source = _replace_exact(source, echo_old, echo_new, expected=1)

    stationary_marker = "def codex_r009_stationary(required_s):\n"
    source = _replace_exact(
        source,
        stationary_marker,
        "def codex_r009_stationary_failure_reason(fallback):\n"
        "  if codex_r009_stationary_reason != 0:\n"
        "    return codex_r009_stationary_reason\n"
        "  end\n"
        "  return fallback\n"
        "end\n\n"
        + stationary_marker,
        expected=1,
    )
    source = _replace_exact(
        source,
        "def codex_r009_stationary(required_s):\n  local dwell_s = 0.0\n",
        "def codex_r009_stationary(required_s):\n"
        "  codex_r009_stationary_reason = 0\n"
        "  local dwell_s = 0.0\n",
        expected=1,
    )
    source = _replace_exact(
        source,
        "    if guard != 0:\n      return False\n"
        "    elif linear > 0.000500000 or angular > 0.005000000:\n",
        "    if guard != 0:\n"
        "      codex_r009_stationary_reason = guard\n"
        "      return False\n"
        "    elif linear > 0.000500000 or angular > 0.005000000:\n",
        expected=1,
    )
    source = _replace_exact(
        source,
        "  if guard != 0 or not codex_r009_stationary(0.250000000):\n"
        "    return codex_r009_return_fault(epoch, ordinal, token, kind, consumed, 58, runtime_hi, runtime_lo, return_guard)\n"
        "  end\n",
        "  if guard != 0:\n"
        "    return codex_r009_return_fault(epoch, ordinal, token, kind, consumed, guard, runtime_hi, runtime_lo, return_guard)\n"
        "  elif not codex_r009_stationary(0.250000000):\n"
        "    return codex_r009_return_fault(epoch, ordinal, token, kind, consumed, codex_r009_stationary_failure_reason(58), runtime_hi, runtime_lo, return_guard)\n"
        "  end\n",
        expected=1,
    )
    for fallback, call, suffix, expected in (
        (11, "codex_r009_fault", "runtime_hi, runtime_lo)", 1),
        (58, "codex_r009_fault", "runtime_hi, runtime_lo)", 2),
        (59, "codex_r009_return_fault", "runtime_hi, runtime_lo, return_guard)", 2),
    ):
        old = (
            f"return {call}(epoch, ordinal, token, kind, consumed, {fallback}, "
            f"{suffix}"
        )
        new = old.replace(
            f", {fallback},",
            f", codex_r009_stationary_failure_reason({fallback}),",
        )
        source = _replace_exact(source, old, new, expected=expected)
    source = _replace_exact(
        source,
        "  if not codex_r009_stationary(0.250000000):\n"
        "    return codex_r009_return_fault(epoch, ordinal, token, kind, consumed, 60, runtime_hi, runtime_lo, return_guard)\n"
        "  end\n"
        "  codex_r009_echo(epoch, ordinal, 40, token, 0, consumed, kind, return_guard, runtime_hi, runtime_lo)\n",
        "  if not codex_r009_stationary(0.250000000):\n"
        "    return codex_r009_return_fault(epoch, ordinal, token, kind, consumed, codex_r009_stationary_failure_reason(60), runtime_hi, runtime_lo, return_guard)\n"
        "  end\n"
        "  codex_r009_echo(epoch, ordinal, 40, token, 0, consumed, kind, return_guard, runtime_hi, runtime_lo)\n",
        expected=1,
    )

    source = _replace_exact(
        source,
        "    elif integer_reason != 0 and session_command != 0:\n",
        "    elif packet_reason != 0:\n"
        "      stopl(0.250000000)\n"
        "      session_active = False\n"
        "      completed = False\n"
        "      last_failed_epoch = input_epoch\n"
        "      active_epoch = input_epoch\n"
        "      state = 90\n"
        "      reason = packet_reason\n"
        "      return_guard = 0\n"
        "    elif integer_reason != 0 and session_command != 0:\n",
        expected=1,
    )

    header = (
        "# R009_REASON43_PROTOCOL: terminal=43 subtype=1 sequence_regression "
        "subtype=2 equal_sequence_payload_changed subtype=3 held_age_timeout\n"
        "# R009_OUTPUT_DOUBLE_24_27: consumed_or_cached, observed, cached, age_s\n"
        "# R009_OUTPUT_INT_35: reason43_subtype; normal publication is subtype 0\n"
    )
    source = header + source
    if "r006" in source.lower() or "606006" in source:
        raise R009TPError("R009 TP retained an r006 identity marker")
    if "V4_CONTRACT_SHA256" in source or "FINAL_CONTRACT" in source:
        raise R009TPError("R009 TP identity is bound to a final contract")
    if _resident_has_return_zero(source):
        raise R009TPError("R009 TP contains a fake return-zero resident loop")
    if "while True:" not in source or "READY_HOME_NEXT" not in source:
        raise R009TPError("R009 TP lost the mature resident loop")
    return source


def validate_urscript_block_balance(script: str) -> None:
    starters = re.compile(r"^(def|thread|if|while|for|sec)\b.*:$")
    stack: list[int] = []
    for line_number, line in enumerate(script.splitlines(), 1):
        stripped = line.strip()
        if starters.match(stripped):
            stack.append(line_number)
        elif stripped == "end":
            if not stack:
                raise R009TPError(f"unmatched URScript end at {line_number}")
            stack.pop()
    if stack:
        raise R009TPError(f"unclosed URScript block at {stack[-1]}")


def render_script(
    behavior_manifest: R009BehaviorManifest | Any = None,
    *,
    contract: Any | None = None,
) -> str:
    if behavior_manifest is not None and contract is not None:
        raise TypeError("R009 TP rendering received both behavior manifest and contract")
    script = _derive_script(
        _behavior_manifest(behavior_manifest if behavior_manifest is not None else contract)
    )
    validate_urscript_block_balance(script)
    return script


def source_stamp(now: datetime | None = None) -> str:
    if now is None:
        return R009_STAMP
    return now.astimezone(timezone(timedelta(hours=8))).strftime(
        "%Y-%m-%dT%H%MHKT_STEP5D_AUTOTUNE_V4_R009"
    )


def validate_stamp(stamp: str) -> str:
    if _STAMP_PATTERN.fullmatch(stamp) is None:
        raise ValueError("r009 source stamp has the wrong HKT format")
    return stamp


def build_txt(stamp: str, script: str) -> str:
    binding_lines = [
        line
        for line in script.splitlines()
        if line.startswith("# R009_BEHAVIOR_MANIFEST_SHA256:")
        or line.startswith("# R009_CAMPAIGN_FINGERPRINT:")
    ]
    if len(binding_lines) != 2:
        raise R009TPError("R009 TP identity binding markers are incomplete")
    return f"""Step5d independent new-EOAT 5 N Autotune V4 r009 resident host/TP loop

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

R009 reason-43 protocol:
  reason 43 remains terminal-compatible; subtype 1/2/3 identify regression,
  equal-sequence payload change, and held-age timeout respectively.
  Output doubles 24/25/26/27 are consumed-or-cached, observed, cached, age_s;
  output integer 35 is the typed subtype and normal output is subtype 0.

Package boundary:
  This local triplet is content-addressed and has not been uploaded or read back.
  The host route never performs Dashboard Load or Play.

Binding:
  {binding_lines[0]}
  {binding_lines[1]}
"""


def _urp_content(urp: bytes) -> tuple[ET.Element, str, str]:
    try:
        root = ET.fromstring(gzip.decompress(urp).decode("utf-8"))
    except (OSError, EOFError, UnicodeError, ET.ParseError) as exc:
        raise R009TPError("R009 URP is not a valid gzipped PolyScope XML") from exc
    cached = ""
    script_path = ""
    for node in root.iter():
        tag = node.tag.rsplit("}", 1)[-1]
        if tag == "cachedContents":
            cached = html.unescape(node.text or "")
        elif tag == "file" and node.attrib.get("resolves-to") == "file":
            script_path = (node.text or "").strip()
    return root, cached, script_path


def numeric_sanity(script: str) -> dict[str, Any]:
    checks = {
        "target_exact_5n": "TARGET_FORCE_N: 5.0" in script,
        "resident_loop": "while True:" in script and "READY_HOME_NEXT" in script,
        "positive_unbounded_attempt": "input_ordinal < 1" in script and "ordinal > 16" not in script,
        "path_60s": "path_elapsed_s < 60.000000000" in script,
        "rate_500_each_layer": "host=500.0Hz rtde_request=500.0Hz" in script and "tp=500.0Hz" in script,
        "no_parent_identity": "r006" not in script.lower(),
        "no_final_contract_identity": "V4_CONTRACT_SHA256" not in script and "FINAL_CONTRACT" not in script,
        "no_fake_loop": not _resident_has_return_zero(script),
        "reason43_sequence_regression": "codex_r009_reason43_subtype = 1" in script,
        "reason43_payload_changed": "codex_r009_reason43_subtype = 2" in script,
        "reason43_held_timeout": "codex_r009_reason43_subtype = 3" in script,
        "first_fault_reason43_latch": all(
            marker in script
            for marker in (
                "def codex_r009_latch_reason43_subtype(subtype):",
                "if codex_r009_terminal_reason43_subtype == 0:",
                "codex_r009_terminal_reason43_subtype = subtype",
                "codex_r009_latch_reason43_subtype(1)",
                "codex_r009_latch_reason43_subtype(2)",
                "codex_r009_latch_reason43_subtype(3)",
            )
        ),
        "resident_packet_reason_propagation": all(
            marker in script
            for marker in (
                "elif packet_reason != 0:",
                "state = 90\n      reason = packet_reason",
                "if session_command == 3 and session_sequence > consumed_session_sequence:",
            )
        ),
        "stationary_guard_carrier": all(
            marker in script
            for marker in (
                "codex_r009_stationary_reason = 0",
                "codex_r009_stationary_reason = guard",
                "codex_r009_stationary_failure_reason(fallback)",
            )
        ),
        "stationary_fallback_seams": all(
            script.count(f"codex_r009_stationary_failure_reason({fallback})") == count
            for fallback, count in ((11, 1), (58, 3), (59, 2), (60, 1))
        ),
        "terminal_reason43_subtype_latch": all(
            marker in script
            for marker in (
                "if reason == 43:",
                "codex_r009_reason43_subtype = codex_r009_terminal_reason43_subtype",
                "codex_r009_terminal_reason43_subtype = 0",
            )
        ),
        "extended_output_registers": all(
            marker in script
            for marker in (
                "write_output_float_register(24, codex_r009_cache_sequence)",
                "write_output_float_register(25, codex_r009_observed_sequence)",
                "write_output_float_register(26, codex_r009_cache_sequence)",
                "write_output_float_register(27, codex_r009_cache_age_s)",
                "write_output_integer_register(35, codex_r009_reason43_subtype)",
            )
        ),
        "runtime_protocol": f"write_output_integer_register(32, {R009_RUNTIME_PROTOCOL})" in script,
    }
    if not all(checks.values()):
        raise R009TPError(f"r009 numeric sanity failed: {checks}")
    return {
        "schema": "step5d.autotune-v4/r009-numeric-sanity-v1",
        "program": PROGRAM,
        "runtime_protocol": R009_RUNTIME_PROTOCOL,
        "checks": checks,
        "passed": True,
        "live_evidence": False,
    }


def validate_triplet(
    script: str,
    txt: str,
    urp: bytes,
    stamp: str,
    behavior_manifest: R009BehaviorManifest,
) -> dict[str, bool]:
    root, cached, script_path = _urp_content(urp)
    validate_urscript_block_balance(script)
    checks = {
        "version_stamp": script.startswith(f"# VERSION: {stamp}\n"),
        "program_name": root.attrib.get("name") == PROGRAM,
        "controller_directory": root.attrib.get("directory") == CONTROLLER_DIRECTORY,
        "script_node_path": script_path == f"{CONTROLLER_DIRECTORY}/{PROGRAM}.script",
        "cached_contents_exact": cached == script,
        "behavior_identity": behavior_manifest.behavior_manifest_sha256 in script,
        "campaign_identity": behavior_manifest.campaign_fingerprint in script,
        "runtime_protocol": f"{R009_RUNTIME_PROTOCOL}" in script,
        "no_final_contract_identity": "V4_CONTRACT_SHA256" not in script,
        "resident_loop": "while True:" in script and "R009_ATTEMPT_SEQUENCE" in script,
        "reason43_assignments": all(
            script.count(f"codex_r009_reason43_subtype = {value}") == 1
            for value in (1, 2, 3)
        ),
        "resident_packet_reason_propagation": (
            "elif packet_reason != 0:" in script
            and "state = 90\n      reason = packet_reason" in script
        ),
        "first_fault_reason43_latch": (
            "def codex_r009_latch_reason43_subtype(subtype):" in script
            and script.count("codex_r009_latch_reason43_subtype(") == 4
        ),
        "stationary_guard_seams": all(
            script.count(f"codex_r009_stationary_failure_reason({fallback})") == count
            for fallback, count in ((11, 1), (58, 3), (59, 2), (60, 1))
        ),
        "terminal_subtype_latch": (
            "codex_r009_reason43_subtype = codex_r009_terminal_reason43_subtype" in script
        ),
        "extended_output": all(
            marker in script
            for marker in (
                "write_output_float_register(25, codex_r009_observed_sequence)",
                "write_output_float_register(26, codex_r009_cache_sequence)",
                "write_output_float_register(27, codex_r009_cache_age_s)",
                "write_output_integer_register(35, codex_r009_reason43_subtype)",
            )
        ),
        "package_text": "not been uploaded or read back" in txt,
        "no_plain_xml": urp[:2] == b"\x1f\x8b",
    }
    if not all(checks.values()):
        raise R009TPError(f"r009 triplet validation failed: {checks}")
    return checks


def build_triplet(
    output_dir: Path,
    *,
    behavior_manifest: R009BehaviorManifest | None = None,
    contract: Any | None = None,
    stamp: str = R009_STAMP,
) -> dict[str, Path]:
    if behavior_manifest is not None and contract is not None:
        raise TypeError("R009 triplet builder received both behavior manifest and contract")
    manifest = _behavior_manifest(
        behavior_manifest if behavior_manifest is not None else contract
    )
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    stamp = validate_stamp(stamp)
    body = render_script(manifest)
    script = f"# VERSION: {stamp}\n{body}"
    txt = build_txt(stamp, script)
    urp = package_support.build_urp(script, PROGRAM, CONTROLLER_DIRECTORY)
    checks = validate_triplet(script, txt, urp, stamp, manifest)
    sanity = numeric_sanity(script)
    paths: dict[str, Path] = {}
    for suffix, content in (
        (".script", script.encode("utf-8")),
        (".txt", txt.encode("utf-8")),
        (".urp", urp),
    ):
        path = output_dir / f"{PROGRAM}{suffix}"
        path.write_bytes(content)
        paths[suffix] = path
    sanity_path = output_dir / f"{PROGRAM}.numeric-sanity.json"
    sanity_path.write_text(
        json.dumps({**sanity, "triplet_checks": checks}, indent=2, sort_keys=True)
        + "\n",
        encoding="utf-8",
    )
    paths[".numeric-sanity.json"] = sanity_path
    triplet = {
        suffix.lstrip("."): hashlib.sha256(path.read_bytes()).hexdigest()
        for suffix, path in paths.items()
        if suffix in {".script", ".txt", ".urp"}
    }
    manifest_path = output_dir / f"{PROGRAM}.deploy-manifest.json"
    manifest_path.write_text(
        json.dumps(
            {
                "schema": "step5d.autotune-v4/r009-deploy-manifest-v1",
                "program": PROGRAM,
                "controller_directory": CONTROLLER_DIRECTORY,
                "campaign_fingerprint": manifest.campaign_fingerprint,
                "behavior_manifest_sha256": manifest.behavior_manifest_sha256,
                "runtime_protocol": R009_RUNTIME_PROTOCOL,
                "same_basename_triplet": True,
                "timestamp": stamp,
                "artifacts": triplet,
                "controller_upload": False,
                "controller_readback": False,
                "live_evidence": False,
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    paths[".deploy-manifest.json"] = manifest_path
    return paths


__all__ = [
    "CONTROLLER_DIRECTORY",
    "PROGRAM",
    "R009_STAMP",
    "R009TPError",
    "build_triplet",
    "build_txt",
    "numeric_sanity",
    "render_script",
    "source_stamp",
    "validate_stamp",
    "validate_triplet",
    "validate_urscript_block_balance",
]
