"""Offline R010 TP derivation: R009 protocol plus counted Wave7 replacement."""

from __future__ import annotations

import gzip
import hashlib
import html
import json
import re
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

import build_step4e_p0p1_programs as package_support
from step5d_autotune_v4_r004.contracts import runtime_identity_limbs
from step5d_autotune_v4_r009.contracts import load_contract as load_r009_contract
from step5d_autotune_v4_r009.tp import (
    CONTROLLER_DIRECTORY,
    render_script as render_r009,
    validate_urscript_block_balance,
)

from .behavior import R010_PROGRAM, R010_RUNTIME_PROTOCOL, render_wave7_contact_loop
from .identity import BehaviorManifest


PROGRAM = R010_PROGRAM
R010_STAMP = "2026-08-09T1200HKT_STEP5D_AUTOTUNE_V4_R010"
_STAMP_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{4}HKT_STEP5D_AUTOTUNE_V4_R010$")


class R010TPError(RuntimeError):
    """R010 TP derivation or package validation failed."""


def _manifest(value: Any) -> BehaviorManifest:
    if isinstance(value, BehaviorManifest):
        return value
    candidate = getattr(value, "behavior_manifest", None)
    if isinstance(candidate, BehaviorManifest):
        return candidate
    raise TypeError("R010 TP requires a typed behavior manifest or contract")


def _replace_exact(source: str, old: str, new: str, *, expected: int = 1) -> str:
    count = source.count(old)
    if count != expected:
        raise R010TPError(f"R010 counted substitution occurred {count}, expected {expected}: {old[:80]!r}")
    return source.replace(old, new)


def render_script(behavior_manifest: BehaviorManifest | Any) -> str:
    manifest = _manifest(behavior_manifest)
    parent = load_r009_contract()
    if (
        parent.release_identity.release_identity_sha256
        != manifest.raw["parent_r009_release_identity_sha256"]
    ):
        raise R010TPError("R010 typed parent differs from the cold-loaded R009 release")
    source = render_r009(parent.behavior_manifest)
    source = re.sub(
        r"(?m)^# R009_BEHAVIOR_MANIFEST_SHA256: [0-9a-f]{64}$",
        f"# R010_BEHAVIOR_MANIFEST_SHA256: {manifest.behavior_manifest_sha256}",
        source,
        count=1,
    )
    source = re.sub(
        r"(?m)^# R009_CAMPAIGN_FINGERPRINT: [0-9a-f]{64}$",
        f"# R010_CAMPAIGN_FINGERPRINT: {manifest.campaign_fingerprint}",
        source,
        count=1,
    )
    program_count = source.count("step5d_strict_rnn_autotune_v4_r009")
    if program_count < 1:
        raise R010TPError("R009 program marker is absent")
    source = _replace_exact(
        source,
        "step5d_strict_rnn_autotune_v4_r009",
        PROGRAM,
        expected=program_count,
    )
    prefix_count = source.count("codex_r009_")
    source = _replace_exact(source, "codex_r009_", "codex_r010_", expected=prefix_count)
    upper_count = source.count("R009")
    source = _replace_exact(source, "R009", "R010", expected=upper_count)
    lower_count = source.count("r009")
    source = _replace_exact(source, "r009", "r010", expected=lower_count)

    runtime_hi, runtime_lo = runtime_identity_limbs(
        PROGRAM,
        manifest.behavior_manifest_sha256,
        manifest.campaign_fingerprint,
    )
    source, count_hi = re.subn(
        r"(?m)^(\s*local runtime_hi = )\d+$", rf"\g<1>{runtime_hi}", source, count=1
    )
    source, count_lo = re.subn(
        r"(?m)^(\s*local runtime_lo = )\d+$", rf"\g<1>{runtime_lo}", source, count=1
    )
    if count_hi != 1 or count_lo != 1:
        raise R010TPError("R010 runtime identity limbs were not replaced exactly once")

    contact_pattern = re.compile(
        r"  # R010 retains the derived parent stationary/Home gate semantics\.\n"
        r".*?"
        r"  stopl\(0\.010000000\)\n",
        re.DOTALL,
    )
    source, contact_count = contact_pattern.subn(render_wave7_contact_loop(manifest.schedule), source, count=1)
    if contact_count != 1:
        raise R010TPError("R010 Wave7 contact block was not replaced exactly once")
    if "r009" in source.lower():
        raise R010TPError("R010 TP retained an R009 identity marker")
    if f"write_output_integer_register(32, {R010_RUNTIME_PROTOCOL})" not in source:
        raise R010TPError("R010 TP lost wire-compatible runtime protocol 609009")
    validate_urscript_block_balance(source)
    numeric_sanity(source)
    return source


def validate_stamp(stamp: str) -> str:
    if _STAMP_PATTERN.fullmatch(stamp) is None:
        raise R010TPError("R010 stamp format differs")
    return stamp


def build_txt(stamp: str, script: str) -> str:
    bindings = [
        line
        for line in script.splitlines()
        if line.startswith("# R010_BEHAVIOR_MANIFEST_SHA256:")
        or line.startswith("# R010_CAMPAIGN_FINGERPRINT:")
    ]
    if len(bindings) != 2:
        raise R010TPError("R010 package identity markers are incomplete")
    return f"""Step5d independent new-EOAT 5 N Autotune V4 R010 offline candidate

Program:
  {PROGRAM}

Controller target (not uploaded):
  {CONTROLLER_DIRECTORY}/{PROGRAM}.urp

Version:
  {stamp}

Behavior:
  R009 reason43/observability/early-abort protocol is retained at wire protocol 609009.
  Contact entry is Wave7 travel-logistic speed with independent F_far and F_soft latches.
  Early-abort remains shadow-only and active mode is rejected.

Offline boundary:
  This triplet has not been uploaded or read back. No Dashboard Load/Play occurred.

Binding:
  {bindings[0]}
  {bindings[1]}
"""


def _urp_content(urp: bytes) -> tuple[ET.Element, str, str]:
    try:
        root = ET.fromstring(gzip.decompress(urp).decode("utf-8"))
    except (OSError, EOFError, UnicodeError, ET.ParseError) as exc:
        raise R010TPError("R010 URP is not valid gzipped PolyScope XML") from exc
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
        "runtime_protocol_609009": f"write_output_integer_register(32, {R010_RUNTIME_PROTOCOL})" in script,
        "reason43_subtypes": all(f"codex_r010_reason43_subtype = {value}" in script for value in (1, 2, 3)),
        "wave7_far_speed": "local v_far_m_s = 0.015000000" in script,
        "wave7_near_speed": "local v_near_m_s = 0.005000000" in script,
        "wave7_creep_speed": "local v_creep_m_s = 0.000700000" in script,
        "wave7_travel_sigmoid": "-8.0 * (sig_alpha - 0.5)" in script,
        "wave7_force_latch": "if far_force_s >= f_far_hold_s:" in script,
        "wave7_creep_precedence": "if creep_latched:" in script,
        "path_60s": "path_elapsed_s < 60.000000000" in script,
        "resident_loop": "while True:" in script and "READY_HOME_NEXT" in script,
        "no_parent_identity": "r009" not in script.lower(),
        "no_live_shortcut": "V4_CONTRACT_SHA256" not in script,
    }
    if not all(checks.values()):
        raise R010TPError(f"R010 numeric sanity failed: {checks}")
    return {
        "schema": "step5d.autotune-v4/r010-numeric-sanity-v1",
        "program": PROGRAM,
        "runtime_protocol": R010_RUNTIME_PROTOCOL,
        "checks": checks,
        "passed": True,
        "live_evidence": False,
    }


def validate_triplet(
    script: str,
    txt: str,
    urp: bytes,
    stamp: str,
    manifest: BehaviorManifest,
) -> dict[str, bool]:
    root, cached, script_path = _urp_content(urp)
    validate_stamp(stamp)
    sanity = numeric_sanity(script)
    checks = {
        "version_stamp": script.startswith(f"# VERSION: {stamp}\n"),
        "program_name": root.attrib.get("name") == PROGRAM,
        "controller_directory": root.attrib.get("directory") == CONTROLLER_DIRECTORY,
        "script_node_path": script_path == f"{CONTROLLER_DIRECTORY}/{PROGRAM}.script",
        "cached_contents_exact": cached == script,
        "behavior_identity": manifest.behavior_manifest_sha256 in script,
        "campaign_identity": manifest.campaign_fingerprint in script,
        "wire_protocol": sanity["checks"]["runtime_protocol_609009"],
        "package_text_offline": "not been uploaded or read back" in txt,
        "gzip_urp": urp[:2] == b"\x1f\x8b",
    }
    if not all(checks.values()):
        raise R010TPError(f"R010 triplet validation failed: {checks}")
    return checks


def build_triplet(
    output_dir: Path,
    *,
    behavior_manifest: BehaviorManifest,
    stamp: str = R010_STAMP,
) -> dict[str, Path]:
    manifest = _manifest(behavior_manifest)
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    stamp = validate_stamp(stamp)
    script = f"# VERSION: {stamp}\n{render_script(manifest)}"
    txt = build_txt(stamp, script)
    urp = package_support.build_urp(script, PROGRAM, CONTROLLER_DIRECTORY)
    checks = validate_triplet(script, txt, urp, stamp, manifest)
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
        json.dumps({**numeric_sanity(script), "triplet_checks": checks}, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    paths[".numeric-sanity.json"] = sanity_path
    triplet = {
        suffix.lstrip("."): hashlib.sha256(paths[suffix].read_bytes()).hexdigest()
        for suffix in (".script", ".txt", ".urp")
    }
    deploy_path = output_dir / f"{PROGRAM}.deploy-manifest.json"
    deploy_path.write_text(
        json.dumps(
            {
                "schema": "step5d.autotune-v4/r010-deploy-manifest-v1",
                "program": PROGRAM,
                "controller_directory": CONTROLLER_DIRECTORY,
                "campaign_fingerprint": manifest.campaign_fingerprint,
                "behavior_manifest_sha256": manifest.behavior_manifest_sha256,
                "runtime_protocol": R010_RUNTIME_PROTOCOL,
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
    paths[".deploy-manifest.json"] = deploy_path
    return paths


__all__ = [
    "CONTROLLER_DIRECTORY",
    "PROGRAM",
    "R010_STAMP",
    "R010TPError",
    "build_triplet",
    "numeric_sanity",
    "render_script",
    "validate_triplet",
]
