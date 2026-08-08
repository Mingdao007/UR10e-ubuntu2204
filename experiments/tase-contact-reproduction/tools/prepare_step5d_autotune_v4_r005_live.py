#!/usr/bin/env python3
"""Prepare one r005 Remote live session and emit fresh admission receipts.

This is an explicitly live tool: it may stop the currently loaded program,
personally execute Script1 through Dashboard Load/Play, load/play the exact r005
Script2, and open the Kunwei stream read-only to measure a software baseline.
It never sends ARM, force-sensor zero/tare/configuration, or candidate packets.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import statistics
import time
from types import SimpleNamespace
from typing import Any, Mapping

from step5d_autotune_v3.dashboard import dashboard_exchange
from step5d_autotune_v3.rtde_client import RTDEClient
from step5d_autotune_v4_r004.contracts import (
    load_contract as load_r004_contract,
    runtime_identity_limbs,
)
from step5d_autotune_v4_r004.transport import OUTPUT_FIELDS, R004OutputSnapshot
from step5d_autotune_v4_r005.contracts import PROGRAM, load_contract
from step5d_autotune_v4_r005.observations import ObservationLedger
from step5d_autotune_v4_r005.tp import RUNTIME_PROTOCOL
from step5d_remote_startup import RemoteDashboardWriter
import step5d_autotune_v4_live_writer as canonical_v4


DASHBOARD_COMMANDS = (
    "is in remote control",
    "safetymode",
    "robotmode",
    "running",
    "programState",
    "get loaded program",
)
STATE_FIELDS = (
    "timestamp",
    "payload",
    "payload_cog",
    "tcp_offset",
    "actual_TCP_pose",
    "actual_TCP_speed",
    "actual_q",
    "actual_qd",
    "safety_mode",
    "robot_mode",
    "runtime_state",
)
SCRIPT1_TARGET = "/programs/andyl/kunwei/step5/step5d_autotune_start_hover_r001.urp"
R005_TARGET = f"/programs/andyl/kunwei/step5/{PROGRAM}.urp"
INT32_MAX = 2**31 - 1
R005_LEDGER_FILENAME = "r005-observations.jsonl"
R005_QUEUE_DIRNAME = "r005-queue"


def _initialize_r005_run_state(
    run_dir: Path, *, campaign_fingerprint: str, eoat_sha256: str
) -> tuple[Path, Path]:
    """Create only the empty durable host state after resident readiness."""

    root = Path(run_dir)
    ledger_path = root / R005_LEDGER_FILENAME
    queue_root = root / R005_QUEUE_DIRNAME
    if queue_root.is_symlink() or queue_root.exists() and not queue_root.is_dir():
        raise RuntimeError("r005 queue root must be a new regular directory")
    if queue_root.exists() and any(queue_root.iterdir()):
        raise RuntimeError("r005 queue root must be empty before host admission")
    queue_root.mkdir(exist_ok=True)
    ledger = ObservationLedger(
        ledger_path,
        campaign_fingerprint=campaign_fingerprint,
        eoat_sha256=eoat_sha256,
    )
    if ledger.rows:
        raise RuntimeError("r005 startup ledger must contain only its genesis header")
    ledger.fresh_process_verify()
    return ledger_path, queue_root


def _wire_epoch(identity_nonce_ns: int) -> int:
    """Derive the TP/RTDE INT32 epoch without weakening unique host IDs."""

    value = int(identity_nonce_ns) // 1_000_000_000
    if not 1 <= value <= INT32_MAX:
        raise RuntimeError("current Unix time cannot be represented by the TP INT32 epoch")
    return value


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _receipt_digest(value: Mapping[str, Any]) -> str:
    return _sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    )


def _write_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(value, handle, sort_keys=True, indent=2, allow_nan=False)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def _observe_dashboard(host: str) -> dict[str, str]:
    return dashboard_exchange(host, DASHBOARD_COMMANDS, timeout=2.0)


def _loaded_target(observation: Mapping[str, str]) -> str:
    raw = observation.get("get loaded program", "")
    if not raw.startswith("Loaded program: "):
        raise RuntimeError("Dashboard loaded-program response is malformed")
    return raw.removeprefix("Loaded program: ").strip()


def _dashboard_running(observation: Mapping[str, str]) -> bool:
    raw = observation.get("running", "").strip().lower()
    if not raw.startswith("program running:"):
        raise RuntimeError("Dashboard running response is malformed")
    value = raw.split(":", 1)[1].strip()
    if value not in {"true", "false"}:
        raise RuntimeError("Dashboard running response is not boolean")
    return value == "true"


def _require_remote_normal(observation: Mapping[str, str]) -> None:
    if observation.get("is in remote control", "").strip().lower() != "true":
        raise RuntimeError("controller is not in Remote Control")
    if "NORMAL" not in observation.get("safetymode", ""):
        raise RuntimeError("controller Safety mode is not NORMAL")
    if "RUNNING" not in observation.get("robotmode", ""):
        raise RuntimeError("robot mode is not RUNNING")


def _wait_dashboard(
    host: str, target: str, *, running: bool, timeout_s: float = 15.0
) -> dict[str, str]:
    deadline = time.monotonic() + timeout_s
    last: dict[str, str] = {}
    while time.monotonic() < deadline:
        last = _observe_dashboard(host)
        try:
            _require_remote_normal(last)
            stopped = last.get("programState", "").startswith("STOPPED")
            if (
                _loaded_target(last) == target
                and _dashboard_running(last) is running
                and (running or stopped)
            ):
                return last
        except RuntimeError:
            pass
        time.sleep(0.02)
    raise RuntimeError(f"Dashboard target state was not reached: {last}")


def _state_sample(host: str) -> dict[str, Any]:
    with RTDEClient(host, timeout=3.0) as client:
        client.negotiate(version=2)
        recipe, types = client.setup_outputs(500.0, STATE_FIELDS)
        client.start()
        return dict(
            zip(STATE_FIELDS, client.recv_recipe_sample(recipe, types), strict=True)
        )


def _stationary_normal(sample: Mapping[str, Any], role: str) -> None:
    speed = tuple(float(value) for value in sample["actual_TCP_speed"])
    linear = math.sqrt(sum(value * value for value in speed[:3]))
    angular = math.sqrt(sum(value * value for value in speed[3:]))
    if int(sample["safety_mode"]) != 1 or linear > 0.0005 or angular > 0.005:
        raise RuntimeError(f"{role} is not stationary Safety NORMAL")


def _verify_controller_readback(root: Path, readback_dir: Path) -> dict[str, str]:
    validation = json.loads((readback_dir / "manifest.json").read_text())
    if (
        validation.get("status") != "controller read-back verified"
        or validation.get("fresh_controller_sha_verified") is not True
        or validation.get("readback_source") != "fresh_controller_get"
    ):
        raise RuntimeError("r005 controller read-back validation is not passing")
    local = root / "programs/step5/step5d" / PROGRAM
    hashes: dict[str, str] = {}
    for role, suffix in (("script", ".script"), ("txt", ".txt"), ("urp", ".urp")):
        local_path = local.with_suffix(suffix)
        readback_path = readback_dir / f"{PROGRAM}{suffix}"
        local_hash = _sha256(local_path.read_bytes())
        if _sha256(readback_path.read_bytes()) != local_hash:
            raise RuntimeError(f"r005 controller read-back {role} differs from local artifact")
        hashes[role] = local_hash
    return hashes


def _capture_software_baseline(host: str, port: int) -> dict[str, Any]:
    transport = canonical_v4.LiveKunweiTransport(
        SimpleNamespace(sensor_host=host, sensor_port=port)
    )
    transport.open()
    rows: list[tuple[float, ...]] = []
    warmup_deadline = time.monotonic() + 0.5
    capture_deadline = warmup_deadline + 1.2
    capture_started = False
    startup_parse_errors = 0
    startup_dropped_bytes = 0
    try:
        while time.monotonic() < capture_deadline:
            frames, _count = transport.poll()
            now = time.monotonic()
            if not capture_started and now >= warmup_deadline:
                # Kunwei START_STREAM may begin between wire-frame boundaries.
                # Preserve that synchronization diagnostic separately, then
                # require the actual stationary capture interval to be clean.
                startup_parse_errors = int(transport.parse_errors)
                startup_dropped_bytes = int(transport.dropped_bytes)
                transport.parse_errors = 0
                transport.dropped_bytes = 0
                capture_started = True
            if capture_started:
                rows.extend(
                    canonical_v4.zeroed_wrench(frame, (0.0,) * 6) for frame in frames
                )
            time.sleep(0.001)
    finally:
        transport.close()
    if len(rows) < 900:
        raise RuntimeError(f"Kunwei baseline has too few physical frames: {len(rows)}")
    columns = list(zip(*rows, strict=True))
    result = {
        "schema": "step5d.autotune-v4/r005-software-baseline-v1",
        "observed_at_s": time.time(),
        "capture_duration_s": 1.2,
        "sample_count": len(rows),
        "mean_wrench_n_nm": [statistics.fmean(column) for column in columns],
        "stdev_wrench_n_nm": [statistics.pstdev(column) for column in columns],
        "parse_errors": int(transport.parse_errors),
        "dropped_bytes": int(transport.dropped_bytes),
        "startup_sync_parse_errors": startup_parse_errors,
        "startup_sync_dropped_bytes": startup_dropped_bytes,
        "zero_tare_config_write": False,
    }
    if result["parse_errors"] != 0 or result["dropped_bytes"] != 0:
        raise RuntimeError(
            "Kunwei baseline capture has parser or framing errors: "
            f"parse_errors={result['parse_errors']} dropped_bytes={result['dropped_bytes']}"
        )
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--controller-readback-dir", type=Path, required=True)
    parser.add_argument("--robot-host", required=True)
    parser.add_argument("--kunwei-host", required=True)
    parser.add_argument("--kunwei-port", type=int, required=True)
    args = parser.parse_args()

    root = args.root.resolve()
    run_dir = args.run_dir.resolve()
    run_dir.mkdir(parents=True, exist_ok=True)
    if any(run_dir.iterdir()):
        raise RuntimeError("r005 live run directory must be empty")
    contract = load_contract(root / "config/step5d/autotune_v4_r005.json")
    r004_parent = load_r004_contract(root / "config/step5d/autotune_v4_r004.json")
    triplet = _verify_controller_readback(root, args.controller_readback_dir.resolve())
    runtime_hi, runtime_lo = runtime_identity_limbs(
        PROGRAM, contract.sha256, contract.campaign_fingerprint
    )
    identity_nonce_ns = time.time_ns()
    epoch = _wire_epoch(identity_nonce_ns)
    route_id = f"r005-remote-live-{identity_nonce_ns}"
    session_id = f"r005-resident-{identity_nonce_ns}"
    r005_writer = RemoteDashboardWriter(
        args.robot_host, load_target=R005_TARGET, timeout_s=3.0
    )
    play_active = False
    try:
        current = _observe_dashboard(args.robot_host)
        _require_remote_normal(current)
        if _dashboard_running(current):
            current_target = _loaded_target(current)
            stopper = RemoteDashboardWriter(
                args.robot_host, load_target=current_target, timeout_s=3.0
            )
            stopper.write("stop")
            _wait_dashboard(args.robot_host, current_target, running=False)

        script1_writer = RemoteDashboardWriter(
            args.robot_host, load_target=SCRIPT1_TARGET, timeout_s=3.0
        )
        script1_writer.write(f"load {SCRIPT1_TARGET}")
        _wait_dashboard(args.robot_host, SCRIPT1_TARGET, running=False)
        script1_writer.write("play")
        time.sleep(0.25)
        _wait_dashboard(args.robot_host, SCRIPT1_TARGET, running=False)
        home = _state_sample(args.robot_host)
        _stationary_normal(home, "Script1 final state")
        script1_receipt: dict[str, Any] = {
            "script_sha256": r004_parent.script1_sha256["script"],
            "observed_at_s": time.time(),
            "final_pose": home["actual_TCP_pose"],
            "final_q": home["actual_q"],
            "stationary": True,
            "safety_mode": "NORMAL",
            "eoat_identity_sha256": contract.raw["invariants"]["eoat_profile"]["sha256"],
        }
        script1_receipt["receipt_sha256"] = _receipt_digest(script1_receipt)

        r005_writer.write(f"load {R005_TARGET}")
        _wait_dashboard(args.robot_host, R005_TARGET, running=False)
        loaded = _state_sample(args.robot_host)
        _stationary_normal(loaded, "r005 loaded state")
        controller_receipt: dict[str, Any] = {
            "program": PROGRAM,
            "controller_target": R005_TARGET,
            **{f"{role}_sha256": digest for role, digest in triplet.items()},
            "observed_at_s": time.time(),
            "runtime_protocol": RUNTIME_PROTOCOL,
            "runtime_digest_hi": runtime_hi,
            "runtime_digest_lo": runtime_lo,
            "eoat_identity_sha256": contract.raw["invariants"]["eoat_profile"]["sha256"],
            "readback": {
                "actual_tcp_speed_m_s_rad_s": loaded["actual_TCP_speed"],
                "payload_kg": loaded["payload"],
                "payload_cog_m": loaded["payload_cog"],
                "tcp_offset_m_rad": loaded["tcp_offset"],
            },
            "safety_mode": "NORMAL",
            "stationary": True,
            "route_id": route_id,
        }
        controller_receipt["receipt_sha256"] = _receipt_digest(controller_receipt)
        software_baseline = _capture_software_baseline(args.kunwei_host, args.kunwei_port)
        launch_context = {
            "route_id": route_id,
            "attempt_id": f"r005-attempt-{identity_nonce_ns}",
            "session_id": session_id,
            "session_epoch": epoch,
            "runtime_protocol": RUNTIME_PROTOCOL,
            "runtime_digest_hi": runtime_hi,
            "runtime_digest_lo": runtime_lo,
            "triplet": triplet,
            "software_baseline_n": software_baseline["mean_wrench_n_nm"],
        }
        for filename, document in (
            ("script1_receipt.json", script1_receipt),
            ("controller_receipt.json", controller_receipt),
            ("software_baseline.json", software_baseline),
            ("launch_context.json", launch_context),
        ):
            _write_json(run_dir / filename, document)

        play = r005_writer.write("play")
        play_active = play.command_sent
        dashboard = _wait_dashboard(args.robot_host, R005_TARGET, running=True)
        snapshot: R004OutputSnapshot | None = None
        with RTDEClient(args.robot_host, timeout=3.0) as client:
            client.negotiate(version=2)
            recipe, types = client.setup_outputs(500.0, OUTPUT_FIELDS)
            client.start()
            deadline = time.monotonic() + 5.0
            while time.monotonic() < deadline:
                mapping = dict(
                    zip(
                        OUTPUT_FIELDS,
                        client.recv_recipe_sample(recipe, types),
                        strict=True,
                    )
                )
                candidate = R004OutputSnapshot.from_mapping(time.time(), mapping)
                if (
                    candidate.integer_echoes[26] == 78
                    and candidate.integer_echoes[32] == RUNTIME_PROTOCOL
                    and candidate.integer_echoes[33] == runtime_hi
                    and candidate.integer_echoes[34] == runtime_lo
                    and candidate.safety_normal
                    and candidate.stationary
                    and candidate.program_running
                ):
                    snapshot = candidate
                    break
        if snapshot is None:
            raise RuntimeError("r005 READY runtime identity was not observed")
        runtime_evidence = {
            "program": PROGRAM,
            "script_sha256": triplet["script"],
            "runtime_protocol": RUNTIME_PROTOCOL,
            "runtime_digest_hi": runtime_hi,
            "runtime_digest_lo": runtime_lo,
            "session_epoch": epoch,
            "resident_session_id": session_id,
            "program_running": True,
            "uninterrupted": True,
            "observed_at_s": snapshot.observed_at_s,
        }
        ready_evidence = {
            "dashboard": dashboard,
            "tp_state": snapshot.integer_echoes[26],
            "tp_epoch_prearm": snapshot.integer_echoes[24],
            "runtime_protocol": snapshot.integer_echoes[32],
            "runtime_digest_hi": snapshot.integer_echoes[33],
            "runtime_digest_lo": snapshot.integer_echoes[34],
            "tcp_pose_m_rad": list(snapshot.tcp_pose_m_rad),
            "q_rad": list(snapshot.q_rad),
            "stationary": snapshot.stationary,
            "safety_normal": snapshot.safety_normal,
            "play_response": play.response,
        }
        _write_json(run_dir / "runtime_evidence.json", runtime_evidence)
        _write_json(run_dir / "resident_ready_evidence.json", ready_evidence)
        ledger_path, queue_root = _initialize_r005_run_state(
            run_dir,
            campaign_fingerprint=contract.campaign_fingerprint,
            eoat_sha256=contract.raw["invariants"]["eoat_profile"]["sha256"],
        )
        print(
            json.dumps(
                {
                    "status": "r005_resident_ready_no_arm",
                    "launch_context": launch_context,
                    "software_baseline": software_baseline,
                    "ready": ready_evidence,
                    "host_state": {
                        "ledger_path": str(ledger_path),
                        "queue_root": str(queue_root),
                    },
                },
                sort_keys=True,
            )
        )
    except Exception:
        if play_active:
            try:
                r005_writer.write("stop")
            except Exception:
                pass
        raise


if __name__ == "__main__":
    main()
