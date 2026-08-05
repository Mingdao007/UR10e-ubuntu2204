"""Surgical r008 B3 TP overlay: FAR/NEAR contact search on a canary triplet.

Reads the published r006 resident script, replaces the contact-search loop
inside ``codex_r006_execute_attempt`` and rewrites ``codex_r006_return_home``
to a two-segment adaptive route (target_z = home_z, then XY; no +5mm third
descend). Emits a new program identity that never overwrites mainline r006.
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
from step5d_autotune_v4_r006.tp import CONTROLLER_DIRECTORY, validate_urscript_block_balance

from .contact_search_schedule import (
    PROGRAM_B3,
    ContactSearchSchedule,
    ContactSearchScheduleError,
    load_schedule,
)

ROOT = Path(__file__).resolve().parents[2]
R006_PROGRAM = "step5d_strict_rnn_autotune_v4_r006"
R006_PUBLISHED_SCRIPT = (
    ROOT / "programs/step5/step5d" / f"{R006_PROGRAM}.script"
)
B3_STAMP_PREFIX = "STEP5D_AUTOTUNE_V4_R008_B3_TWO_STAGE"
FORCE_FUSE_REASON = 75

_CONTACT_LOOP_PATTERN = re.compile(
    r"(?ms)^  # R006 retains the derived parent stationary/Home gate semantics\.\n"
    r"  # ARM entry has already verified the fixed Home\.  Start the existing V4\n"
    r"  # bounded negative-Z search directly from that stationary pose\.\n"
    r"  local contact_start = get_actual_tcp_pose\(\)\n"
    r"  local contact_elapsed_s = 0\.0\n"
    r"  local contact_confirm_s = 0\.0\n"
    r"  local contact_done = False\n"
    r"  while not contact_done:\n"
    r".*?"
    r"  end\n"
    r"  stopl\(0\.010000000\)\n"
)

# Whole return_home def through the end that precedes execute_attempt.
_RETURN_HOME_PATTERN = re.compile(
    r"(?ms)^def codex_r006_return_home\(home_pose, home_q, epoch, ordinal, token, "
    r"kind, consumed, runtime_hi, runtime_lo, current_guard\):\n"
    r".*?"
    r"^end\n(?=\ndef codex_r006_execute_attempt\()"
)

# Match pose_close position tolerance (HOME_ENTRY: position <= 0.001m).
RETURN_Z_EPS_M = 0.001


class TwoStageSearchTPError(RuntimeError):
    """B3 two-stage TP transform failed."""


def _f(value: float) -> str:
    return f"{float(value):.9f}"


def source_stamp(now: datetime | None = None) -> str:
    moment = datetime.now(timezone(timedelta(hours=8))) if now is None else now
    return moment.astimezone(timezone(timedelta(hours=8))).strftime(
        f"%Y-%m-%dT%H%MHKT_{B3_STAMP_PREFIX}"
    )


def load_r006_published_script(path: Path | None = None) -> str:
    script_path = R006_PUBLISHED_SCRIPT if path is None else Path(path)
    if script_path.is_symlink() or not script_path.is_file():
        raise TwoStageSearchTPError(f"r006 published script missing: {script_path}")
    text = script_path.read_text(encoding="utf-8")
    if "def codex_r006_execute_attempt(" not in text:
        raise TwoStageSearchTPError("r006 script lacks execute_attempt")
    if "speedl([0.0, 0.0, -0.000200000" not in text:
        raise TwoStageSearchTPError(
            "r006 script does not contain the expected near-field speedl literal"
        )
    return text


def render_two_stage_contact_loop(schedule: ContactSearchSchedule) -> str:
    """URScript fragment replacing the single-speed contact search loop."""

    d_near_start = schedule.d_near_start_travel_m
    return f"""  # R008 B3 two-stage contact search (FAR then NEAR); near={_f(schedule.v_near_m_s)} m/s.
  # FAR: empty-air approach. NEAR: B3 canary force-search speed. Fuse << 48 N.
  local contact_start = get_actual_tcp_pose()
  local contact_elapsed_s = 0.0
  local contact_confirm_s = 0.0
  local contact_done = False
  local d_near_start_travel_m = {_f(d_near_start)}
  local f_far_n = {_f(schedule.F_far_n)}
  local force_fuse_n = {_f(schedule.force_fuse_n)}
  local v_far_m_s = {_f(schedule.v_far_m_s)}
  local v_near_m_s = {_f(schedule.v_near_m_s)}
  local far_accel_m_s2 = {_f(schedule.far_acceleration_m_s2)}
  local near_accel_m_s2 = {_f(schedule.near_acceleration_m_s2)}
  while not contact_done:
    packet_reason = codex_r006_packet_observe()
    # B3: raise search-loop abs-normal hard guard 60N → 100N; keep 50N fuse below.
    guard = codex_r006_packet_guard(packet_reason, 100.0, 100.0, 3.0)
    local contact_pose = get_actual_tcp_pose()
    local travel = contact_start[2] - contact_pose[2]
    local normal_force = read_input_float_register(24)
    local force_norm = read_input_float_register(25)
    if guard != 0:
      stopl(0.010000000)
      return codex_r006_fault(epoch, ordinal, token, kind, consumed, guard, runtime_hi, runtime_lo)
    elif normal_force >= force_fuse_n or force_norm >= force_fuse_n:
      stopl(0.010000000)
      return codex_r006_fault(epoch, ordinal, token, kind, consumed, {int(schedule.force_fuse_reason)}, runtime_hi, runtime_lo)
    elif travel >= {_f(schedule.max_travel_m)}:
      return codex_r006_fault(epoch, ordinal, token, kind, consumed, 8, runtime_hi, runtime_lo)
    elif contact_elapsed_s >= {_f(schedule.timeout_s)}:
      return codex_r006_fault(epoch, ordinal, token, kind, consumed, 10, runtime_hi, runtime_lo)
    else:
      local contact_dt = get_steptime()
      if normal_force >= {_f(schedule.confirm_normal_n)} or force_norm >= {_f(schedule.confirm_force_norm_n)}:
        contact_confirm_s = contact_confirm_s + contact_dt
      else:
        contact_confirm_s = 0.0
      end
      if contact_confirm_s >= {_f(schedule.confirm_hold_s)}:
        contact_done = True
      else:
        if travel < d_near_start_travel_m and normal_force < f_far_n and force_norm < f_far_n:
          speedl([0.0, 0.0, -v_far_m_s, 0.0, 0.0, 0.0], far_accel_m_s2, contact_dt)
        else:
          speedl([0.0, 0.0, -v_near_m_s, 0.0, 0.0, 0.0], near_accel_m_s2, contact_dt)
        end
        contact_elapsed_s = contact_elapsed_s + contact_dt
        codex_r006_echo(epoch, ordinal, 20, token, 0, consumed, kind, 0, runtime_hi, runtime_lo)
      end
    end
  end
  stopl(0.010000000)
"""


def render_two_segment_return_home() -> str:
    """URScript: rise/descend to home_z, then XY at home_z (no +5mm third descend)."""

    eps = _f(RETURN_Z_EPS_M)
    return f"""def codex_r006_return_home(home_pose, home_q, epoch, ordinal, token, kind, consumed, runtime_hi, runtime_lo, current_guard):
  local return_guard = current_guard
  local packet_reason = codex_r006_packet_observe()
  local guard = codex_r006_packet_guard(packet_reason, 60.0, 100.0, 3.0)
  if guard != 0 or not codex_r006_stationary(0.250000000):
    return codex_r006_return_fault(epoch, ordinal, token, kind, consumed, 58, runtime_hi, runtime_lo, return_guard)
  end
  # R008 B3 Wave2b: two-segment adaptive return. Lift/descend to target_z =
  # home_pose[2] (not home+5mm), XY-transfer at that Z, no third descend.
  local current_pose = get_actual_tcp_pose()
  local target_z = home_pose[2]
  local z_eps_m = {eps}
  if current_pose[2] < target_z - z_eps_m or current_pose[2] > target_z + z_eps_m:
    local vertical_pose = p[current_pose[0], current_pose[1], target_z, current_pose[3], current_pose[4], current_pose[5]]
    movel(vertical_pose, a=0.060, v=0.040, r=0.0)
    stopl(0.1)
    if not codex_r006_stationary(0.250000000):
      return codex_r006_return_fault(epoch, ordinal, token, kind, consumed, 59, runtime_hi, runtime_lo, return_guard)
    end
  end
  return_guard = return_guard + 1 + 2 + 8
  codex_r006_echo(epoch, ordinal, 40, token, 0, consumed, kind, return_guard, runtime_hi, runtime_lo)
  local transfer_pose = p[home_pose[0], home_pose[1], target_z, home_pose[3], home_pose[4], home_pose[5]]
  movel(transfer_pose, a=0.135, v=0.090, r=0.0)
  stopl(0.1)
  if not codex_r006_stationary(0.250000000):
    return codex_r006_return_fault(epoch, ordinal, token, kind, consumed, 59, runtime_hi, runtime_lo, return_guard)
  end
  return_guard = return_guard + 16
  codex_r006_echo(epoch, ordinal, 40, token, 0, consumed, kind, return_guard, runtime_hi, runtime_lo)
  if not codex_r006_home_close(home_pose, home_q):
    return codex_r006_return_fault(epoch, ordinal, token, kind, consumed, 60, runtime_hi, runtime_lo, return_guard)
  end
  return_guard = return_guard + 32 + 64
  codex_r006_attempt_guard = return_guard
  codex_r006_attempt_reason = 0
  codex_r006_attempt_success = True
  codex_r006_echo(epoch, ordinal, 78, token, 0, consumed, kind, return_guard, runtime_hi, runtime_lo)
  return True
end

"""


def _replace_identity_markers(
    script: str,
    *,
    contract_sha256: str,
    campaign_fingerprint: str,
    v_near_m_s: float,
    program: str = PROGRAM_B3,
) -> str:
    replacements = [
        (R006_PROGRAM, program),
        (
            "# ROLE: isolated V4 r006 resident rolling ARM loop",
            "# ROLE: isolated V4 r008 B3 two-stage contact-search canary",
        ),
        (
            "# CONTACT_SEARCH: downward V4 speed=0.0002000m/s, accel=0.005m/s^2, max=0.025m, timeout=90.0s",
            (
                "# CONTACT_SEARCH: r008 B3 FAR/NEAR schedule; near="
                f"{float(v_near_m_s):g} m/s; force_fuse<=50N; max=0.025m; timeout=90.0s"
            ),
        ),
        (
            "# RETURN: V3 rise/descent a=0.060 v=0.040; XY transfer a=0.135 v=0.090; fixed Home",
            (
                "# RETURN: B3 Wave2b two-segment adaptive to home_z a=0.060 v=0.040; "
                "XY a=0.135 v=0.090; no +5mm third descend"
            ),
        ),
    ]
    out = script
    for old, new in replacements:
        if old not in out:
            raise TwoStageSearchTPError(f"identity marker missing: {old!r}")
        out = out.replace(old, new)

    out, n_contract = re.subn(
        r"(?m)^# V4_CONTRACT_SHA256: [0-9a-f]{64}$",
        f"# V4_CONTRACT_SHA256: {contract_sha256}",
        out,
        count=1,
    )
    out, n_fp = re.subn(
        r"(?m)^# V4_CAMPAIGN_FINGERPRINT: [0-9a-f]{64}$",
        f"# V4_CAMPAIGN_FINGERPRINT: {campaign_fingerprint}",
        out,
        count=1,
    )
    if n_contract != 1 or n_fp != 1:
        raise TwoStageSearchTPError("contract/fingerprint markers not unique")

    runtime_hi, runtime_lo = runtime_identity_limbs(
        program, contract_sha256, campaign_fingerprint
    )
    out, n_hi = re.subn(
        r"(?m)^(\s*local runtime_hi = )\d+$",
        rf"\g<1>{runtime_hi}",
        out,
        count=1,
    )
    out, n_lo = re.subn(
        r"(?m)^(\s*local runtime_lo = )\d+$",
        rf"\g<1>{runtime_lo}",
        out,
        count=1,
    )
    if n_hi != 1 or n_lo != 1:
        raise TwoStageSearchTPError("runtime identity limbs not unique")
    return out


def transform_script(
    source_script: str,
    schedule: ContactSearchSchedule,
    *,
    contract_sha256: str,
    campaign_fingerprint: str,
    stamp: str | None = None,
) -> str:
    if campaign_fingerprint == schedule.parent_campaign_fingerprint:
        raise TwoStageSearchTPError("canary fingerprint must differ from mainline")
    body = source_script
    # Drop version line if present; rebuild with B3 stamp.
    if body.startswith("# VERSION:"):
        body = "\n".join(body.splitlines()[1:]) + "\n"
    match = _CONTACT_LOOP_PATTERN.search(body)
    if match is None:
        raise TwoStageSearchTPError("contact search loop pattern not found in r006 script")
    body = body[: match.start()] + render_two_stage_contact_loop(schedule) + body[match.end() :]
    return_match = _RETURN_HOME_PATTERN.search(body)
    if return_match is None:
        raise TwoStageSearchTPError("return_home pattern not found in r006 script")
    body = (
        body[: return_match.start()]
        + render_two_segment_return_home()
        + body[return_match.end() :]
    )
    body = _replace_identity_markers(
        body,
        contract_sha256=contract_sha256,
        campaign_fingerprint=campaign_fingerprint,
        v_near_m_s=schedule.v_near_m_s,
        program=schedule.program,
    )
    if "speedl([0.0, 0.0, -v_far_m_s" not in body and f"-{_f(schedule.v_far_m_s)}" not in body:
        # locals carry speeds; ensure far/near locals exist
        if "local v_far_m_s =" not in body or "local v_near_m_s =" not in body:
            raise TwoStageSearchTPError("transformed script missing FAR/NEAR speed locals")
    if f"local v_near_m_s = {_f(schedule.v_near_m_s)}" not in body:
        raise TwoStageSearchTPError(
            f"transformed script lost near-field {_f(schedule.v_near_m_s)} speed"
        )
    if f"force_fuse_n = {_f(schedule.force_fuse_n)}" not in body:
        raise TwoStageSearchTPError("transformed script missing force fuse")
    if f", {int(schedule.force_fuse_reason)}," not in body:
        raise TwoStageSearchTPError("transformed script missing fuse fault reason")
    if "local target_z = home_pose[2]" not in body:
        raise TwoStageSearchTPError("transformed script missing adaptive target_z return")
    if "home_pose[2] + 0.005000000" in body:
        raise TwoStageSearchTPError("transformed script retained +5mm third-segment return")
    # Ensure single-speed mainline literal is gone from contact loop (may remain nowhere).
    validate_urscript_block_balance(body)
    stamp_value = stamp or source_stamp()
    return f"# VERSION: {stamp_value}\n{body}"


def build_txt(stamp: str, script: str, *, program: str = PROGRAM_B3) -> str:
    return f"""Step5d r008 B3 two-stage contact-search canary (FAR/NEAR)

Program:
  {program}

Controller target:
  {CONTROLLER_DIRECTORY}/{program}.urp

Version:
  {stamp}

Boundary:
  Independent canary triplet. Does not overwrite mainline r006.
  Near-field speed is 0.0005 m/s; force fuse <= 50 N during search.
  Never resume into campaign fingerprint 1db4f9bf / live_20260803_1113_stage_d.

Binding:
  {script.splitlines()[0]}
  {script.splitlines()[1] if len(script.splitlines()) > 1 else ''}
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


def validate_b3_triplet(
    script: str,
    txt: str,
    urp: bytes,
    *,
    stamp: str,
    program: str,
    contract_sha256: str,
    campaign_fingerprint: str,
    schedule: ContactSearchSchedule,
) -> dict[str, bool]:
    root, cached, script_path = _urp_content(urp)
    validate_urscript_block_balance(script)
    checks = {
        "version_stamp": script.startswith(f"# VERSION: {stamp}\n"),
        "program_name": root.attrib.get("name") == program,
        "controller_directory": root.attrib.get("directory") == CONTROLLER_DIRECTORY,
        "script_node_path": script_path == f"{CONTROLLER_DIRECTORY}/{program}.script",
        "cached_contents_exact": cached == script,
        "contract_binding": contract_sha256 in script and campaign_fingerprint in script,
        "not_mainline_fingerprint": campaign_fingerprint
        != schedule.parent_campaign_fingerprint,
        "has_far_local": "local v_far_m_s =" in script,
        "has_near_speed": f"local v_near_m_s = {_f(schedule.v_near_m_s)}" in script,
        "has_force_fuse": f"force_fuse_n = {_f(schedule.force_fuse_n)}" in script,
        "has_fuse_reason": f", {int(schedule.force_fuse_reason)}," in script,
        "keeps_confirm_thresholds": (
            f">= {_f(schedule.confirm_normal_n)}" in script
            and f">= {_f(schedule.confirm_force_norm_n)}" in script
        ),
        "package_text": "two-stage contact-search canary" in txt,
        "no_plain_xml": urp[:2] == b"\x1f\x8b",
        "not_r006_basename": program != R006_PROGRAM,
        "two_segment_return_target_z": "local target_z = home_pose[2]" in script,
        "no_plus_5mm_return_clearance": "home_pose[2] + 0.005000000" not in script,
    }
    if not all(checks.values()):
        raise TwoStageSearchTPError(f"B3 triplet validation failed: {checks}")
    return checks


def build_b3_triplet(
    output_dir: Path,
    *,
    contract_sha256: str,
    campaign_fingerprint: str,
    schedule: ContactSearchSchedule | None = None,
    source_script_path: Path | None = None,
    stamp: str | None = None,
) -> dict[str, Path]:
    sched = schedule if schedule is not None else load_schedule()
    if sched.force_fuse_reason != FORCE_FUSE_REASON:
        raise ContactSearchScheduleError(
            f"force_fuse_reason must be {FORCE_FUSE_REASON} for this overlay"
        )
    source = load_r006_published_script(source_script_path)
    stamp_value = stamp or source_stamp()
    script = transform_script(
        source,
        sched,
        contract_sha256=contract_sha256,
        campaign_fingerprint=campaign_fingerprint,
        stamp=stamp_value,
    )
    # Extract stamp from script header for consistency.
    header = script.splitlines()[0]
    if not header.startswith("# VERSION: "):
        raise TwoStageSearchTPError("transformed script missing VERSION header")
    stamp_value = header[len("# VERSION: ") :]
    txt = build_txt(stamp_value, script, program=sched.program)
    urp = package_support.build_urp(script, sched.program, CONTROLLER_DIRECTORY)
    checks = validate_b3_triplet(
        script,
        txt,
        urp,
        stamp=stamp_value,
        program=sched.program,
        contract_sha256=contract_sha256,
        campaign_fingerprint=campaign_fingerprint,
        schedule=sched,
    )
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    paths: dict[str, Path] = {}
    for suffix, content in ((".script", script.encode()), (".txt", txt.encode()), (".urp", urp)):
        path = output_dir / f"{sched.program}{suffix}"
        if path.name.startswith(R006_PROGRAM):
            raise TwoStageSearchTPError("refusing to write mainline r006 basename")
        path.write_bytes(content)
        paths[suffix] = path

    sanity = {
        "schema": "step5d.autotune-v4/r008-b3-two-stage-numeric-sanity-v1",
        "program": sched.program,
        "schedule": sched.as_dict(),
        "campaign_fingerprint": campaign_fingerprint,
        "contract_sha256": contract_sha256,
        "triplet_checks": checks,
        "passed": True,
        "live_evidence": False,
    }
    sanity_path = output_dir / f"{sched.program}.numeric-sanity.json"
    sanity_path.write_text(
        json.dumps(sanity, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    paths[".numeric-sanity.json"] = sanity_path

    manifest = {
        "schema": "step5d.autotune-v4/r008-b3-two-stage-deploy-manifest-v1",
        "program": sched.program,
        "controller_directory": CONTROLLER_DIRECTORY,
        "campaign_fingerprint": campaign_fingerprint,
        "contract_sha256": contract_sha256,
        "parent_mainline_fingerprint": sched.parent_campaign_fingerprint,
        "same_basename_triplet": True,
        "timestamp": stamp_value,
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
        "overwrites_mainline_r006": False,
    }
    manifest_path = output_dir / f"{sched.program}.deploy-manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    paths[".deploy-manifest.json"] = manifest_path
    return paths


__all__ = [
    "FORCE_FUSE_REASON",
    "RETURN_Z_EPS_M",
    "TwoStageSearchTPError",
    "build_b3_triplet",
    "build_txt",
    "load_r006_published_script",
    "render_two_segment_return_home",
    "render_two_stage_contact_loop",
    "source_stamp",
    "transform_script",
    "validate_b3_triplet",
]
