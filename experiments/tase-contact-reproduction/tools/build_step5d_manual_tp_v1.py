#!/usr/bin/env python3
"""Build the isolated Step5d manual-hold TP triplet from frozen r009 bytes."""

from __future__ import annotations

import argparse
import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

import build_step5d_autotune_tp_v3 as r009


PROGRAM_NAME = "step5d_strict_rnn_manual_tune_v1"
PROTOCOL = "v3_full_home_manual_hold_v1"
PARENT_R009_COMMIT = "bf6eb59d9530cf7f170f29c68c8812f00613b381"
PARENT_PROGRAM = r009.PROGRAM_NAME
HEARTBEAT_STALE_S = 1.0
HEARTBEAT_LOSS_REASON = 20
MANUAL_IDENTITY_REASON = 21
CONTROLLER_DIR = r009.CONTROLLER_DIR
LOCAL_PROGRAM_DIR = r009.LOCAL_PROGRAM_DIR


def _replace_once(source: str, old: str, new: str, *, role: str) -> str:
    if source.count(old) != 1:
        raise ValueError(f"r009 rendered script {role} marker count differs")
    return source.replace(old, new, 1)


def _parent_wait_block() -> str:
    matches = [new for _old, new, role in r009._direct_arm_replacements() if role == "bounded terminal halt"]
    if len(matches) != 1:
        raise RuntimeError("r009 bounded wait replacement differs")
    return matches[0]


def _manual_wait_function() -> str:
    return f'''

def codex_autotune_wait_for_manual_arm(campaign_epoch, trial_id, state, candidate_token, terminal_reason, execution_profile_id, consumed_command_seq):
  local last_heartbeat = read_input_float_register(26)
  local heartbeat_stale_s = 0.0
  while True:
    codex_autotune_write_state(campaign_epoch, trial_id, state, candidate_token, terminal_reason, execution_profile_id, consumed_command_seq)
    local heartbeat = read_input_float_register(26)
    if heartbeat != last_heartbeat:
      last_heartbeat = heartbeat
      heartbeat_stale_s = 0.0
    else:
      heartbeat_stale_s = heartbeat_stale_s + get_steptime()
    end
    if heartbeat_stale_s > {HEARTBEAT_STALE_S:.3f}:
      codex_autotune_publish_fault_and_halt(campaign_epoch, trial_id, candidate_token, {HEARTBEAT_LOSS_REASON}, execution_profile_id, consumed_command_seq)
    end
    local next_command = read_input_integer_register(26)
    local next_sequence = read_input_integer_register(29)
    if next_command == 1 and next_sequence > consumed_command_seq:
      local next_epoch = read_input_integer_register(24)
      local next_trial = read_input_integer_register(25)
      local next_token = read_input_integer_register(27)
      local next_profile = read_input_integer_register(28)
      local next_row = read_input_integer_register(30)
      local next_logical_batch = read_input_integer_register(31)
      local initial_identity_ok = state == 10 and campaign_epoch == 0 and trial_id == 0 and next_epoch > 0 and next_trial == 1 and next_token > 0 and next_profile == 633 and next_row == 1 and next_logical_batch == 1
      local rolling_identity_ok = state == 78 and next_epoch == campaign_epoch and next_trial == trial_id + 1 and next_token > 0 and next_profile == execution_profile_id and next_row == 1 and next_logical_batch == codex_autotune_logical_batch_sequence_echo + 1
      if next_sequence == consumed_command_seq + 1 and (initial_identity_ok or rolling_identity_ok):
        return True
      end
      codex_autotune_publish_fault_and_halt(campaign_epoch, trial_id, candidate_token, {MANUAL_IDENTITY_REASON}, execution_profile_id, consumed_command_seq)
    elif state == 78 and next_command == 4 and next_sequence == consumed_command_seq + 1 and read_input_integer_register(24) == campaign_epoch and read_input_integer_register(25) == trial_id and read_input_integer_register(27) == candidate_token and read_input_integer_register(28) == execution_profile_id and read_input_integer_register(30) == codex_autotune_batch_row_echo and read_input_integer_register(31) == codex_autotune_logical_batch_sequence_echo:
      codex_autotune_publish_state_and_halt(campaign_epoch, trial_id, 77, candidate_token, terminal_reason, execution_profile_id, next_sequence)
    elif next_command == 3 and next_sequence > consumed_command_seq:
      codex_autotune_publish_fault_and_halt(campaign_epoch, trial_id, candidate_token, 4, execution_profile_id, next_sequence)
    elif trial_id > 0 and next_command == 2 and next_sequence > consumed_command_seq:
      codex_autotune_publish_fault_and_halt(campaign_epoch, trial_id, candidate_token, 13, execution_profile_id, consumed_command_seq)
    end
    sync()
  end
  return False
end'''


def render_script() -> str:
    parent = r009.render_script()
    parent_sha = hashlib.sha256(parent.encode("utf-8")).hexdigest()
    result = parent
    result = _replace_once(
        result,
        _parent_wait_block(),
        _parent_wait_block() + _manual_wait_function(),
        role="manual wait insertion",
    )
    result = _replace_once(
        result,
        "codex_autotune_wait_for_arm(campaign_epoch, trial_id, 78, candidate_token, stop_reason, execution_profile_id, last_consumed_command_seq)",
        "codex_autotune_wait_for_manual_arm(campaign_epoch, trial_id, 78, candidate_token, stop_reason, execution_profile_id, last_consumed_command_seq)",
        role="state-78 manual wait",
    )
    result = _replace_once(
        result,
        "codex_autotune_wait_for_arm(0, 0, 10, 0, 0, 0, 0)",
        "codex_autotune_wait_for_manual_arm(0, 0, 10, 0, 0, 0, 0)",
        role="initial manual wait",
    )
    result = _replace_once(
        result,
        "batch_row_index < 1 or batch_row_index > 5",
        "batch_row_index != 1",
        role="single-row ARM policy",
    )
    result = _replace_once(
        result,
        "def codex_step5d_strict_rnn_autotune_v3():",
        "def codex_step5d_strict_rnn_manual_tune_v1():",
        role="manual main definition",
    )
    result = _replace_once(
        result,
        "codex_step5d_strict_rnn_autotune_v3()",
        "codex_step5d_strict_rnn_manual_tune_v1()",
        role="manual main call",
    )
    for marker, replacement, role in (
        (
            f"# RELEASE_STAGE_ID: {PARENT_PROGRAM}",
            f"# RELEASE_STAGE_ID: {PROGRAM_NAME}",
            "release program",
        ),
        (
            f"# TP_PROGRAM_ID: {PARENT_PROGRAM}",
            f"# TP_PROGRAM_ID: {PROGRAM_NAME}",
            "TP program",
        ),
    ):
        result = _replace_once(result, marker, replacement, role=role)
    result = (
        f"# MANUAL_PROTOCOL_ID: {PROTOCOL}\n"
        f"# PARENT_R009_COMMIT: {PARENT_R009_COMMIT}\n"
        f"# PARENT_R009_RENDERED_SHA256: {parent_sha}\n"
        + result
    )
    validate_rendered_script(result, parent=parent)
    return result


def validate_rendered_script(script: str, *, parent: str | None = None) -> None:
    required = (
        f"# MANUAL_PROTOCOL_ID: {PROTOCOL}",
        f"# PARENT_R009_COMMIT: {PARENT_R009_COMMIT}",
        f"# RELEASE_STAGE_ID: {PROGRAM_NAME}",
        f"# TP_PROGRAM_ID: {PROGRAM_NAME}",
        "def codex_step5d_strict_rnn_manual_tune_v1():",
        "codex_step5d_strict_rnn_manual_tune_v1()",
        "def codex_autotune_wait_for_manual_arm(",
        f"heartbeat_stale_s > {HEARTBEAT_STALE_S:.3f}",
        f"candidate_token, {HEARTBEAT_LOSS_REASON}, execution_profile_id",
        f"candidate_token, {MANUAL_IDENTITY_REASON}, execution_profile_id",
        "next_sequence == consumed_command_seq + 1",
        "next_logical_batch == codex_autotune_logical_batch_sequence_echo + 1",
        "batch_row_index != 1",
        "codex_autotune_wait_for_manual_arm(campaign_epoch, trial_id, 78",
        "codex_autotune_wait_for_manual_arm(0, 0, 10",
    )
    missing = [marker for marker in required if marker not in script]
    if missing:
        raise ValueError(f"manual TP script lacks required markers: {missing}")
    if "while waiting_s < 30.000" not in script:
        raise ValueError("manual TP no longer embeds the unchanged r009 watchdog helper")
    original = r009.render_script() if parent is None else parent
    normalized = "\n".join(script.splitlines()[3:]) + "\n"
    normalized = _replace_once(
        normalized,
        _parent_wait_block() + _manual_wait_function(),
        _parent_wait_block(),
        role="normalized manual wait",
    )
    normalized = _replace_once(
        normalized,
        "codex_autotune_wait_for_manual_arm(campaign_epoch, trial_id, 78, candidate_token, stop_reason, execution_profile_id, last_consumed_command_seq)",
        "codex_autotune_wait_for_arm(campaign_epoch, trial_id, 78, candidate_token, stop_reason, execution_profile_id, last_consumed_command_seq)",
        role="normalized state-78 wait",
    )
    normalized = _replace_once(
        normalized,
        "codex_autotune_wait_for_manual_arm(0, 0, 10, 0, 0, 0, 0)",
        "codex_autotune_wait_for_arm(0, 0, 10, 0, 0, 0, 0)",
        role="normalized initial wait",
    )
    normalized = _replace_once(normalized, "batch_row_index != 1", "batch_row_index < 1 or batch_row_index > 5", role="normalized row policy")
    normalized = _replace_once(normalized, "def codex_step5d_strict_rnn_manual_tune_v1():", "def codex_step5d_strict_rnn_autotune_v3():", role="normalized main definition")
    normalized = _replace_once(normalized, "codex_step5d_strict_rnn_manual_tune_v1()", "codex_step5d_strict_rnn_autotune_v3()", role="normalized main call")
    normalized = _replace_once(normalized, f"# RELEASE_STAGE_ID: {PROGRAM_NAME}", f"# RELEASE_STAGE_ID: {PARENT_PROGRAM}", role="normalized release")
    normalized = _replace_once(normalized, f"# TP_PROGRAM_ID: {PROGRAM_NAME}", f"# TP_PROGRAM_ID: {PARENT_PROGRAM}", role="normalized program")
    if normalized != original:
        raise ValueError("manual TP differs from frozen r009 outside the manual protocol delta")


def source_stamp(now: datetime | None = None) -> str:
    value = now or datetime.now(timezone(timedelta(hours=8)))
    return value.strftime("%Y-%m-%dT%H%MHKT_STEP5D_MANUAL_HOLD_V1")


def build_package_script(stamp: str) -> str:
    if not stamp or "\n" in stamp:
        raise ValueError("source stamp must be one non-empty line")
    return f"# VERSION: {stamp}\n" + render_script()


def build_txt(stamp: str) -> str:
    return f"""Step5d manual-hold TP package

Controller target:
  {CONTROLLER_DIR}/{PROGRAM_NAME}.urp

Version:
  {stamp}

Protocol:
  {PROTOCOL}; parent r009 commit {PARENT_R009_COMMIT}

Frozen execution envelope:
  target force 12 N; orientation_ko 0.4; qdot cap 0.5 rad/s;
  nf100-slew050-a050 / integer profile 633; Stage25 success target 60 s.
  Each logical batch contains exactly one request and returns to campaign Home.
  Home wait has no elapsed-time limit while float-register 26 heartbeat is fresh.
  Heartbeat loss halts stationary with typed reason {HEARTBEAT_LOSS_REASON}; an
  inexact next ARM halts stationary with typed reason {MANUAL_IDENTITY_REASON}.

Safety boundary:
  Upload/read-back does not Load or Play. It does not start a bridge or authorize motion.
"""


def numeric_sanity(script: str) -> dict[str, Any]:
    validate_rendered_script(script.split("\n", 1)[1] if script.startswith("# VERSION:") else script)
    sanity = dict(r009.numeric_sanity(r009.render_script()))
    sanity.update(
        schema="step5d.manual-hold/tp-numeric-sanity-v1",
        program=PROGRAM_NAME,
        parent_r009_commit=PARENT_R009_COMMIT,
        host_protocol=PROTOCOL,
        batch_row_policy="one_request_per_logical_batch_every_row_campaign_home",
        ready_arm_timeout_s=None,
        home_wait_policy="unbounded_while_heartbeat_fresh",
        home_heartbeat_stale_s=HEARTBEAT_STALE_S,
        home_heartbeat_loss_reason=HEARTBEAT_LOSS_REASON,
        manual_identity_fault_reason=MANUAL_IDENTITY_REASON,
        stage25_success_target_s=60.0,
        target_force_n=12.0,
        orientation_ko=0.4,
    )
    return sanity


def validate_triplet(script: str, txt: str, urp: bytes, stamp: str) -> dict[str, Any]:
    import gzip
    import html
    import xml.etree.ElementTree as ET

    root = ET.fromstring(gzip.decompress(urp).decode("utf-8"))
    cached = next(html.unescape(node.text or "") for node in root.iter() if node.tag == "cachedContents")
    script_file = next(node.text or "" for node in root.iter() if node.tag == "file" and node.attrib.get("resolves-to") == "file")
    checks = {
        "script stamp": stamp in script,
        "txt stamp": stamp in txt,
        "program name": root.attrib.get("name") == PROGRAM_NAME,
        "controller directory": root.attrib.get("directory") == CONTROLLER_DIR,
        "script node": script_file == f"{CONTROLLER_DIR}/{PROGRAM_NAME}.script",
        "cached script": cached == script,
        "installation path": any(
            bool(node.attrib.get("installationRelativePath"))
            for node in root.iter()
            if node.tag == "URProgram"
        ),
        "main entrypoint": script.rstrip().endswith("codex_step5d_strict_rnn_manual_tune_v1()"),
        "txt protocol": PROTOCOL in txt,
    }
    failed = [name for name, passed in checks.items() if not passed]
    if failed:
        raise ValueError(f"manual TP triplet validation failed: {failed}")
    validate_rendered_script(script.split("\n", 1)[1])
    return checks


def write_triplet(output_dir: Path, stamp: str) -> dict[str, Any]:
    script = build_package_script(stamp)
    txt = build_txt(stamp)
    urp = r009.v1.build_urp(script, PROGRAM_NAME, CONTROLLER_DIR)
    checks = validate_triplet(script, txt, urp, stamp)
    output_dir.mkdir(parents=True, exist_ok=True)
    paths = {
        ".script": output_dir / f"{PROGRAM_NAME}.script",
        ".txt": output_dir / f"{PROGRAM_NAME}.txt",
        ".urp": output_dir / f"{PROGRAM_NAME}.urp",
    }
    manifest_path = output_dir / f"{PROGRAM_NAME}.deploy-manifest.json"
    sanity_path = output_dir / f"{PROGRAM_NAME}.numeric-sanity.json"
    collisions = [str(path) for path in (*paths.values(), manifest_path, sanity_path) if path.exists()]
    if collisions:
        raise FileExistsError("manual TP release is immutable: " + ", ".join(collisions))
    for extension, path in paths.items():
        if extension == ".urp":
            path.write_bytes(urp)
        else:
            path.write_text(script if extension == ".script" else txt, encoding="utf-8")
    digests = {extension: hashlib.sha256(path.read_bytes()).hexdigest() for extension, path in paths.items()}
    manifest = {
        "schema_version": 1,
        "basename": PROGRAM_NAME,
        "controller_directory": CONTROLLER_DIR,
        "protocol": PROTOCOL,
        "parent_r009_commit": PARENT_R009_COMMIT,
        "artifacts": [
            {"filename": path.name, "source": path.name, "sha256": digests[extension]}
            for extension, path in paths.items()
        ],
    }
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    sanity_path.write_text(json.dumps(numeric_sanity(script), indent=2, sort_keys=True) + "\n")
    return {
        "program": PROGRAM_NAME,
        "protocol": PROTOCOL,
        "parent_r009_commit": PARENT_R009_COMMIT,
        "paths": {extension: str(path) for extension, path in paths.items()},
        "sha256": digests,
        "deploy_manifest": str(manifest_path),
        "numeric_sanity": str(sanity_path),
        "checks": checks,
    }


def simulate_manual_home_wait(
    samples: Sequence[Mapping[str, Any]], *, step_s: float = 0.1
) -> dict[str, Any]:
    """Deterministic no-motion mirror of the manual Home wait decision order."""

    stale_s = 0.0
    last_heartbeat: float | None = None
    for index, sample in enumerate(samples):
        heartbeat = float(sample["heartbeat"])
        if last_heartbeat is None or heartbeat != last_heartbeat:
            stale_s = 0.0
            last_heartbeat = heartbeat
        else:
            stale_s += step_s
        if stale_s > HEARTBEAT_STALE_S:
            return {"outcome": "fault", "reason": HEARTBEAT_LOSS_REASON, "sample": index}
        if sample.get("command") == "arm":
            return {
                "outcome": "arm" if sample.get("identity_exact") is True else "fault",
                "reason": None if sample.get("identity_exact") is True else MANUAL_IDENTITY_REASON,
                "sample": index,
            }
        if sample.get("command") == "complete" and sample.get("identity_exact") is True:
            return {"outcome": "complete", "reason": None, "sample": index}
    return {"outcome": "waiting", "reason": None, "sample": len(samples)}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=LOCAL_PROGRAM_DIR)
    parser.add_argument("--stamp", default=None)
    args = parser.parse_args(argv)
    print(json.dumps(write_triplet(args.output_dir, args.stamp or source_stamp()), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
