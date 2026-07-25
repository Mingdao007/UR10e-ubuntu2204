#!/usr/bin/python3.10
"""Freeze one launch basis, then run campaign-prepare and preflight as siblings."""

from __future__ import annotations

import argparse
from datetime import datetime
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
from typing import Any, Mapping

from step5d_autotune_v3.bridge_admission import resolve_bridge_admission
from step5d_autotune_v3.launch_basis import (
    make_launch_basis,
    write_launch_basis,
)
from step5d_autotune_v3.launcher import check_effective_config
from step5d_autotune_v3.profile import load_contract
from step5d_autotune_v3.release_identity import (
    LAUNCH_PROFILE_PATH,
    SAFETY_ENVELOPE_PATH,
    load_runtime_release,
    release_payload_path,
)
from step5d_autotune_v3.runtime_profile import DEFAULT_OVERLAY, load_launch_profile
from step5d_autotune_v3.runtime_gate import release_runtime_contract
from step5d_autotune_v3.state import atomic_json
import step5d_bridge_authority as authority


SCHEMA = "step5d.autotune-v3/launch-coordinator-v1"
CAMPAIGN_PREPARE_SCHEMA = "step5d.autotune-v3/campaign-prepare-result-v1"
PREFLIGHT_SCHEMA = "step5d.autotune-v3/live-preflight-snapshot-v3"
RESULT_MAX_AGE_NS = 30 * 1_000_000_000
PREFLIGHT_REQUIRED_FIELDS = {
    "schema", "ok", "fresh", "created_at", "elapsed_s", "healthy_target_s",
    "candidate_stage_id", "control_profile_id", "tp_program_id",
    "release_manifest_sha256", "expected_loaded_program", "tp_runtime_identity",
    "launch_profile_fingerprint", "launch_profile", "controller_identity",
    "controller_identity_sha256", "predicates", "observations", "probe_reuse",
    "safety_boundary", "launch_basis_sha256",
}
CAMPAIGN_RESULT_FIELDS = {
    "ok", "campaign_id", "campaign_epoch", "campaign_fingerprint", "campaign_root",
    "campaign_binding_file", "launch_profile_path", "launch_profile_sha256",
    "machine_binding_status", "candidate_plan", "trial_overlay_plan", "receiver_root",
}


def _strict_json(path: Path, *, role: str) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise RuntimeError(f"{role} is missing or unsafe")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"{role} is not strict JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise RuntimeError(f"{role} must be a JSON object")
    return value


def _fresh_timestamp(payload: Mapping[str, Any], basis: Mapping[str, Any], now_ns: int) -> int:
    created = payload.get("created_at_unix_ns")
    if created is None:
        created_at = payload.get("created_at")
        if not isinstance(created_at, str):
            raise RuntimeError("lane result freshness timestamp is missing")
        try:
            created = int(
                datetime.fromisoformat(created_at.replace("Z", "+00:00")).timestamp()
                * 1_000_000_000
            )
        except (TypeError, ValueError, OverflowError) as exc:
            raise RuntimeError("lane result freshness timestamp is malformed") from exc
    if isinstance(created, bool) or not isinstance(created, int):
        raise RuntimeError("lane result freshness timestamp is malformed")
    if (
        created < basis["issued_at_unix_ns"]
        or created > now_ns
        or now_ns >= basis["expires_at_unix_ns"]
    ):
        raise RuntimeError("lane result is outside the launch-basis freshness window")
    if now_ns - created > RESULT_MAX_AGE_NS:
        raise RuntimeError("lane result is stale")
    return created


def _require_digest(value: Any, expected: Any, *, name: str) -> None:
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
        raise RuntimeError(f"lane result {name} digest is malformed")
    if value != expected:
        raise RuntimeError(f"lane result {name} digest differs from launch basis")


def _validate_campaign_prepare(
    payload: Mapping[str, Any], basis: Mapping[str, Any], *, now_ns: int | None = None
) -> dict[str, Any]:
    required = {
        "schema", "ok", "fresh", "created_at_unix_ns", "launch_basis_sha256",
        "identity", "result",
    } | CAMPAIGN_RESULT_FIELDS
    if set(payload) != required or payload.get("schema") != CAMPAIGN_PREPARE_SCHEMA:
        raise RuntimeError("campaign preparation schema differs")
    if payload.get("ok") is not True or payload.get("fresh") is not True:
        raise RuntimeError("campaign preparation is false or stale")
    _require_digest(payload.get("launch_basis_sha256"), basis["basis_sha256"], name="basis")
    _fresh_timestamp(payload, basis, time.time_ns() if now_ns is None else now_ns)
    identity = payload.get("identity")
    result = payload.get("result")
    if not isinstance(identity, dict) or not isinstance(result, dict):
        raise RuntimeError("campaign preparation identity/result is malformed")
    if set(identity) != {
        "campaign_id", "campaign_epoch", "campaign_fingerprint",
        "release_manifest_sha256", "runtime_identity_sha256",
    }:
        raise RuntimeError("campaign preparation identity schema differs")
    if not isinstance(identity["campaign_id"], str) or not identity["campaign_id"]:
        raise RuntimeError("campaign preparation campaign identity is malformed")
    if isinstance(identity["campaign_epoch"], bool) or not isinstance(identity["campaign_epoch"], int):
        raise RuntimeError("campaign preparation campaign epoch is malformed")
    _require_digest(identity["campaign_fingerprint"], basis["campaign_fingerprint"], name="campaign")
    _require_digest(identity["release_manifest_sha256"], basis["release_manifest_sha256"], name="release")
    _require_digest(identity["runtime_identity_sha256"], basis["runtime_identity_sha256"], name="runtime identity")
    if set(result) != CAMPAIGN_RESULT_FIELDS:
        raise RuntimeError("campaign preparation result schema differs")
    if any(payload.get(key) != result.get(key) for key in CAMPAIGN_RESULT_FIELDS):
        raise RuntimeError("campaign preparation top-level result differs")
    if result.get("ok") is not True or result.get("campaign_fingerprint") != identity["campaign_fingerprint"]:
        raise RuntimeError("campaign preparation result identity differs")
    if result.get("campaign_id") != identity["campaign_id"] or result.get("campaign_epoch") != identity["campaign_epoch"]:
        raise RuntimeError("campaign preparation campaign identity differs")
    return dict(payload)


def _validate_preflight(
    payload: Mapping[str, Any], basis: Mapping[str, Any], *, now_ns: int | None = None
) -> dict[str, Any]:
    if set(payload) != PREFLIGHT_REQUIRED_FIELDS or payload.get("schema") != PREFLIGHT_SCHEMA:
        raise RuntimeError("preflight schema differs")
    if payload.get("ok") is not True or payload.get("fresh") is not True:
        raise RuntimeError("preflight is false or stale")
    _require_digest(payload.get("launch_basis_sha256"), basis["basis_sha256"], name="basis")
    _fresh_timestamp(payload, basis, time.time_ns() if now_ns is None else now_ns)
    _require_digest(payload.get("release_manifest_sha256"), basis["release_manifest_sha256"], name="release")
    runtime_identity = payload.get("tp_runtime_identity")
    if not isinstance(runtime_identity, dict):
        raise RuntimeError("preflight runtime identity is malformed")
    if _sha256_json(runtime_identity) != basis["runtime_identity_sha256"]:
        raise RuntimeError("preflight runtime identity differs from launch basis")
    predicates = payload.get("predicates")
    if not isinstance(predicates, dict) or not predicates or any(
        not isinstance(item, dict) or item.get("ok") is not True for item in predicates.values()
    ):
        raise RuntimeError("preflight predicates are incomplete or false")
    return dict(payload)


def _terminate_and_wait(processes: list[subprocess.Popen[Any]], timeout_s: float) -> None:
    for process in processes:
        try:
            process.terminate()
        except Exception:
            pass
    for process in processes:
        try:
            if process.poll() is not None:
                continue
            process.wait(timeout=timeout_s)
        except subprocess.TimeoutExpired:
            try:
                process.kill()
            except Exception:
                pass
            try:
                process.wait(timeout=0.5)
            except Exception:
                pass
        except Exception:
            pass


def _sha256_json(value: Any) -> str:
    import hashlib

    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    ).hexdigest()


def _sha256_path(path: Path) -> str:
    import hashlib

    return hashlib.sha256(path.read_bytes()).hexdigest()


def _revoke(args: argparse.Namespace, reason: str) -> None:
    before = authority.load_current(args.authority_root)
    expected_owner = {"pid": args.owner_pid, "starttime_ticks": args.owner_starttime}
    if (
        not isinstance(before, dict)
        or before.get("state") != "ACTIVE"
        or before.get("attempt_id") != args.attempt_id
        or before.get("owner") != expected_owner
        or isinstance(before.get("sequence"), bool)
        or not isinstance(before.get("sequence"), int)
    ):
        raise RuntimeError("bridge authority fencing is not confirmed before revoke")
    try:
        completed = subprocess.run(
            [
                sys.executable,
                str(Path(__file__).with_name("step5d_bridge_authority.py")),
                "revoke",
                "--authority-root",
                str(args.authority_root),
                "--attempt-id",
                args.attempt_id,
                "--owner-pid",
                str(args.owner_pid),
                "--owner-starttime",
                str(args.owner_starttime),
                "--reason",
                reason,
            ],
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise RuntimeError(f"bridge authority revoke command failed: {exc}") from exc
    output = completed.stdout.strip()
    if not re.fullmatch(r"[1-9][0-9]*", output):
        raise RuntimeError("bridge authority revoke sequence is missing")
    revoked = authority.load_current(args.authority_root)
    expected_sequence = before["sequence"] + 1
    if (
        not isinstance(revoked, dict)
        or revoked.get("state") != "REVOKED"
        or revoked.get("attempt_id") != args.attempt_id
        or revoked.get("owner") != expected_owner
        or revoked.get("reason") != reason
        or revoked.get("sequence") != expected_sequence
        or int(output) != expected_sequence
    ):
        raise RuntimeError("bridge authority fencing is unconfirmed after revoke")


def _basis(args: argparse.Namespace) -> dict[str, Any]:
    root = args.experiment_root.resolve(strict=True)
    release = load_runtime_release(root)
    admission_path, admission = resolve_bridge_admission(root, release=release)
    if admission_path.resolve() != args.admission.resolve():
        raise RuntimeError("coordinator admission path is not the current validated admission")
    current = authority.load_current(args.authority_root)
    if not isinstance(current, dict) or current.get("state") != "ACTIVE":
        raise RuntimeError("coordinator authority is not active")
    if current.get("attempt_id") != args.attempt_id or current.get("sequence") != args.authority_epoch:
        raise RuntimeError("coordinator authority epoch/attempt differs")
    owner = current.get("owner")
    if owner != {"pid": args.owner_pid, "starttime_ticks": args.owner_starttime}:
        raise RuntimeError("coordinator authority owner differs")
    campaign_fingerprint = admission.get("campaign_fingerprint")
    if not isinstance(campaign_fingerprint, str) or len(campaign_fingerprint) != 64:
        raise RuntimeError("validated admission lacks the exact campaign fingerprint")
    contract_path = release_payload_path(root, release, SAFETY_ENVELOPE_PATH)
    profile_path = release_payload_path(root, release, LAUNCH_PROFILE_PATH)
    contract = load_contract(contract_path)
    profile = load_launch_profile(
        profile_path, contract=contract, expected_tp_program_id=release.program_id
    )
    runtime_root = args.output_root / "runtime"
    runtime_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    effective = check_effective_config(
        runtime_root=runtime_root,
        contract_path=contract_path,
        launch_profile_path=profile_path,
        expected_tp_program_id=release.program_id,
        trial_overlay=DEFAULT_OVERLAY,
    )
    delivery = admission["delivery_observation"]
    delivery_path = root / str(delivery["path"])
    runtime_contract = release_runtime_contract(root, release)
    runtime_identity = runtime_contract.get("tp_runtime_identity")
    basis = make_launch_basis(
        release_manifest_sha256=release.manifest_sha256,
        runtime_identity_sha256=_sha256_json(runtime_identity),
        campaign_fingerprint=campaign_fingerprint,
        delivery_observation_sha256=_sha256_path(delivery_path),
        owner_pid=args.owner_pid,
        owner_starttime=args.owner_starttime,
        authority_epoch=args.authority_epoch,
        launch_nonce=os.environ.get("STEP5D_V3_LAUNCH_ATTEMPT_ID", args.attempt_id),
        argv_sha256=_sha256_json(sys.argv),
        effective_config_sha256=_sha256_json(effective["effective_config"]),
        issued_at_unix_ns=time.time_ns(),
        expires_at_unix_ns=time.time_ns() + args.basis_ttl_s * 1_000_000_000,
    )
    return write_launch_basis(args.launch_basis, basis)


def _campaign_worker_code(basis: Mapping[str, Any]) -> str:
    prepare_path = Path(__file__).with_name("prepare_step5d_autotune_launch.py").resolve()
    basis_literal = repr({
        "basis_sha256": basis["basis_sha256"],
        "campaign_fingerprint": basis["campaign_fingerprint"],
        "release_manifest_sha256": basis["release_manifest_sha256"],
        "runtime_identity_sha256": basis["runtime_identity_sha256"],
    })
    return f'''\
import json, sys, time
sys.path.insert(0, {str(prepare_path.parent)!r})
from prepare_step5d_autotune_launch import parse_args, prepare
basis = {basis_literal}
result = prepare(parse_args())
campaign_result = {{key: result.get(key) for key in {sorted(CAMPAIGN_RESULT_FIELDS)!r}}}
identity = {{
    "campaign_id": campaign_result.get("campaign_id"),
    "campaign_epoch": campaign_result.get("campaign_epoch"),
    "campaign_fingerprint": campaign_result.get("campaign_fingerprint"),
    "release_manifest_sha256": basis["release_manifest_sha256"],
    "runtime_identity_sha256": basis["runtime_identity_sha256"],
}}
payload = {{
    **campaign_result,
    "schema": {CAMPAIGN_PREPARE_SCHEMA!r},
    "ok": result.get("ok") is True,
    "fresh": True,
    "created_at_unix_ns": time.time_ns(),
    "launch_basis_sha256": basis["basis_sha256"],
    "identity": identity,
    "result": campaign_result,
}}
print(json.dumps(payload, sort_keys=True, separators=(",", ":")), flush=True)
'''


def _lane_commands(args: argparse.Namespace, basis: dict[str, Any]) -> list[list[str]]:
    python = sys.executable
    release = load_runtime_release(args.experiment_root.resolve(strict=True))
    launch_profile_path = release_payload_path(
        args.experiment_root.resolve(strict=True), release, LAUNCH_PROFILE_PATH
    )
    common = [
        "--launch-basis", str(args.launch_basis),
        "--launch-basis-sha256", basis["basis_sha256"],
        "--owner-pid", str(args.owner_pid),
        "--owner-starttime", str(args.owner_starttime),
    ]
    campaign = [
        python, "-c", _campaign_worker_code(basis),
        "--experiment-root", str(args.experiment_root),
        "--campaign-root", str(args.campaign_root),
        "--binding-file", str(args.output_root / "runtime" / "campaign_binding.json"),
        "--binding-source", "coordinator_post_basis_campaign_prepare",
        "--launch-profile", str(launch_profile_path),
        "--campaign-fingerprint", basis["campaign_fingerprint"],
        *common,
    ]
    preflight = [
        python, str(Path(__file__).with_name("preflight_step5d_autotune_v3.py")),
        "--mailbox", str(args.output_root / "runtime" / "command.json"),
        "--delivery-observation", str(args.delivery_observation),
        "--output", str(args.preflight),
        "--json",
        *common,
    ]
    return [campaign, preflight]


def run(args: argparse.Namespace) -> int:
    args.output_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    basis = _basis(args)
    atomic_json(args.output_root / "launch-coordinator-basis.json", basis)
    commands = _lane_commands(args, basis)
    logs = [args.output_root / "campaign-prepare.log", args.output_root / "preflight.log"]
    processes: list[subprocess.Popen[Any]] = []
    process_indices: dict[int, int] = {}
    try:
        for index, (command, log) in enumerate(zip(commands, logs)):
            process = subprocess.Popen(command, stdout=log.open("w"), stderr=subprocess.STDOUT)
            processes.append(process)
            process_indices[id(process)] = index
    except BaseException:
        _terminate_and_wait(processes, timeout_s=5.0)
        _revoke(args, "failed")
        raise
    revocation_done = False
    failure_reason = "cancelled"
    try:
        while processes:
            for process in tuple(processes):
                code = process.poll()
                if code is None:
                    continue
                processes.remove(process)
                index = process_indices[id(process)]
                if code != 0:
                    _terminate_and_wait(processes, timeout_s=5.0)
                    _revoke(args, "failed")
                    revocation_done = True
                    return code or 2
                try:
                    if index == 0:
                        _validate_campaign_prepare(
                            _strict_json(logs[0], role="campaign preparation"), basis
                        )
                    else:
                        _validate_preflight(
                            _strict_json(args.preflight, role="preflight"), basis
                        )
                except BaseException:
                    failure_reason = "failed"
                    _terminate_and_wait(processes, timeout_s=5.0)
                    raise
            time.sleep(0.01)
    except BaseException:
        _terminate_and_wait(processes, timeout_s=5.0)
        if not revocation_done:
            revocation_done = True
            _revoke(args, failure_reason)
        raise
    try:
        campaign_payload = _validate_campaign_prepare(
            _strict_json(logs[0], role="campaign preparation"), basis
        )
        _validate_preflight(_strict_json(args.preflight, role="preflight"), basis)
    except BaseException:
        _revoke(args, "failed")
        raise
    atomic_json(args.output_root / "campaign-prepare.json", campaign_payload)
    atomic_json(
        args.output_root / "launch-coordinator.json",
        {"schema": SCHEMA, "ok": True, "basis_sha256": basis["basis_sha256"], "parallel": True},
    )
    return 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--experiment-root", type=Path, required=True)
    parser.add_argument("--admission", type=Path, required=True)
    parser.add_argument("--authority-root", type=Path, required=True)
    parser.add_argument("--attempt-id", required=True)
    parser.add_argument("--authority-epoch", type=int, required=True)
    parser.add_argument("--owner-pid", type=int, required=True)
    parser.add_argument("--owner-starttime", type=int, required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--campaign-root", type=Path, required=True)
    parser.add_argument("--delivery-observation", type=Path, required=True)
    parser.add_argument("--preflight", type=Path, required=True)
    parser.add_argument("--launch-basis", type=Path, required=True)
    parser.add_argument("--basis-ttl-s", type=int, default=900)
    return parser.parse_args(argv)


if __name__ == "__main__":
    raise SystemExit(run(parse_args()))
