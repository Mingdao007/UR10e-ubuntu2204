#!/usr/bin/env python3
"""Prepare one fresh r008 B3 two-stage canary resident session (no ARM).

Uploads the B3 canary triplet (never overwrites mainline r006 basename on disk).
Reuses Script1/Home and Kunwei baseline primitives from the r006 prepare path.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from pathlib import Path
from typing import Any, Mapping, Sequence

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from step5d_autotune_v3.rtde_client import RTDEClient  # noqa: E402
from step5d_autotune_v4_r004.contracts import (  # noqa: E402
    load_contract as load_r004_contract,
    runtime_identity_limbs,
)
from step5d_autotune_v4_r004.transport import OUTPUT_FIELDS, R004OutputSnapshot  # noqa: E402
from step5d_autotune_v4_r005.observations import ObservationLedger  # noqa: E402
from step5d_autotune_v4_r006.tp import CONTROLLER_DIRECTORY, RUNTIME_PROTOCOL  # noqa: E402
from step5d_autotune_v4_r008.b3_identity import load_b3_contract  # noqa: E402
from step5d_autotune_v4_r008.contact_search_schedule import (  # noqa: E402
    MAINLINE_FINGERPRINT,
    PROGRAM_B3,
)
from step5d_autotune_v4_r008.tp_two_stage_search import build_b3_triplet  # noqa: E402
from step5d_remote_startup import RemoteDashboardWriter  # noqa: E402
from prepare_step5d_autotune_v4_r005_live import (  # noqa: E402
    SCRIPT1_TARGET,
    _capture_software_baseline,
    _dashboard_running,
    _loaded_target,
    _observe_dashboard,
    _receipt_digest,
    _require_remote_normal,
    _state_sample,
    _stationary_normal,
    _wait_dashboard,
    _wire_epoch,
    _write_json,
)

B3_TARGET = f"{CONTROLLER_DIRECTORY}/{PROGRAM_B3}.urp"
LEDGER_FILENAME = "r006-observations.jsonl"
QUEUE_DIRNAME = "r006-queue"
AUTHORITY_DIRNAME = "authority"


def _sha256(path: Path) -> str:
    if path.is_symlink() or not path.is_file():
        raise RuntimeError(f"B3 artifact missing or unsafe: {path}")
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _verify_controller_readback(
    root: Path,
    readback_dir: Path,
    result_path: Path,
    *,
    controller_host: str,
    program: str,
) -> dict[str, str]:
    try:
        result = json.loads(result_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("B3 controller read-back result is not strict JSON") from exc
    if (
        not isinstance(result, Mapping)
        or result.get("ok") is not True
        or result.get("operation") != "readback"
        or result.get("host") != controller_host
        or not isinstance(result.get("files"), list)
    ):
        raise RuntimeError("B3 controller read-back result is not a passing fresh readback")
    receipt_rows = {
        str(row.get("filename")): str(row.get("sha256"))
        for row in result["files"]
        if isinstance(row, Mapping)
    }
    local_stem = root / "programs/step5/step5d" / program
    hashes: dict[str, str] = {}
    for role, suffix in (("script", ".script"), ("txt", ".txt"), ("urp", ".urp")):
        filename = f"{program}{suffix}"
        local_path = local_stem.with_suffix(suffix)
        fetched_path = readback_dir / filename
        local_hash = _sha256(local_path)
        if _sha256(fetched_path) != local_hash or receipt_rows.get(filename) != local_hash:
            raise RuntimeError(f"B3 controller read-back {role} differs from local bytes")
        hashes[role] = local_hash
    if set(receipt_rows) != {f"{program}{suffix}" for suffix in (".script", ".txt", ".urp")}:
        raise RuntimeError("B3 controller read-back result contains an unexpected file set")
    return hashes


def _initialize_run_state(
    run_dir: Path,
    *,
    campaign_fingerprint: str,
    eoat_sha256: str,
) -> tuple[Path, Path, Path]:
    if campaign_fingerprint == MAINLINE_FINGERPRINT:
        raise RuntimeError("B3 prepare refuses mainline campaign fingerprint")
    ledger_path = run_dir / LEDGER_FILENAME
    queue_root = run_dir / QUEUE_DIRNAME
    authority_root = run_dir / AUTHORITY_DIRNAME
    for role, directory in (("queue", queue_root), ("authority", authority_root)):
        if directory.is_symlink() or directory.exists() and not directory.is_dir():
            raise RuntimeError(f"B3 {role} root is not a regular directory")
        if directory.exists() and any(directory.iterdir()):
            raise RuntimeError(f"B3 {role} root must be empty before host admission")
        directory.mkdir(exist_ok=True)
    ledger = ObservationLedger(
        ledger_path,
        campaign_fingerprint=campaign_fingerprint,
        eoat_sha256=eoat_sha256,
    )
    if ledger.rows:
        raise RuntimeError("B3 startup ledger must contain only its genesis header")
    ledger.fresh_process_verify()
    return ledger_path, queue_root, authority_root


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--controller-readback-dir", type=Path, required=True)
    parser.add_argument("--controller-readback-result", type=Path, required=True)
    parser.add_argument("--robot-host", required=True)
    parser.add_argument("--kunwei-host", required=True)
    parser.add_argument("--kunwei-port", type=int, required=True)
    parser.add_argument(
        "--skip-rebuild-triplet",
        action="store_true",
        help="Use existing on-disk B3 triplet instead of rebuilding",
    )
    args = parser.parse_args(list(argv) if argv is not None else None)

    root = args.root.resolve()
    run_dir = args.run_dir.resolve()
    run_dir.mkdir(parents=True, exist_ok=True)
    if any(run_dir.iterdir()):
        raise RuntimeError("B3 live run directory must be empty")
    if "1113_stage_d" in str(run_dir):
        raise RuntimeError("B3 must not use the mainline 1113_stage_d run directory")

    contract = load_b3_contract()
    if contract.campaign_fingerprint == MAINLINE_FINGERPRINT:
        raise RuntimeError("B3 fingerprint must differ from mainline")
    if contract.program != PROGRAM_B3:
        raise RuntimeError("B3 program identity differs")

    programs_dir = root / "programs/step5/step5d"
    if not args.skip_rebuild_triplet:
        build_b3_triplet(
            programs_dir,
            contract_sha256=contract.sha256,
            campaign_fingerprint=contract.campaign_fingerprint,
            schedule=contract.schedule,
        )

    triplet = _verify_controller_readback(
        root,
        args.controller_readback_dir.resolve(),
        args.controller_readback_result.resolve(),
        controller_host=args.robot_host,
        program=PROGRAM_B3,
    )
    runtime_hi, runtime_lo = runtime_identity_limbs(
        PROGRAM_B3, contract.sha256, contract.campaign_fingerprint
    )
    r004_parent = load_r004_contract(root / "config/step5d/autotune_v4_r004.json")
    identity_nonce_ns = time.time_ns()
    epoch = _wire_epoch(identity_nonce_ns)
    # route/attempt must contain "r006" for R006LiveInputs.validate.
    route_id = f"r006-r008-b3-two-stage-{identity_nonce_ns}"
    session_id = f"r006-r008-b3-resident-{identity_nonce_ns}"
    b3_writer = RemoteDashboardWriter(
        args.robot_host, load_target=B3_TARGET, timeout_s=3.0
    )
    play_active = False
    try:
        current = _observe_dashboard(args.robot_host)
        _require_remote_normal(current)
        if _dashboard_running(current):
            current_target = _loaded_target(current)
            RemoteDashboardWriter(
                args.robot_host, load_target=current_target, timeout_s=3.0
            ).write("stop")
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
        _stationary_normal(home, "B3 Script1 final state")
        script1_receipt: dict[str, Any] = {
            "script_sha256": r004_parent.script1_sha256["script"],
            "observed_at_s": time.time(),
            "final_pose": home["actual_TCP_pose"],
            "final_q": home["actual_q"],
            "stationary": True,
            "safety_mode": "NORMAL",
            "eoat_identity_sha256": r004_parent.eoat_sha256,
        }
        script1_receipt["receipt_sha256"] = _receipt_digest(script1_receipt)

        b3_writer.write(f"load {B3_TARGET}")
        _wait_dashboard(args.robot_host, B3_TARGET, running=False)
        loaded = _state_sample(args.robot_host)
        _stationary_normal(loaded, "B3 loaded state")
        controller_receipt: dict[str, Any] = {
            "program": PROGRAM_B3,
            "controller_target": B3_TARGET,
            **{f"{role}_sha256": digest for role, digest in triplet.items()},
            "observed_at_s": time.time(),
            "runtime_protocol": RUNTIME_PROTOCOL,
            "runtime_digest_hi": runtime_hi,
            "runtime_digest_lo": runtime_lo,
            "eoat_identity_sha256": r004_parent.eoat_sha256,
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
            "attempt_id": f"r006-r008-b3-attempt-{identity_nonce_ns}",
            "session_id": session_id,
            "session_epoch": epoch,
            "runtime_protocol": RUNTIME_PROTOCOL,
            "runtime_digest_hi": runtime_hi,
            "runtime_digest_lo": runtime_lo,
            "triplet": triplet,
            "software_baseline_n": software_baseline["mean_wrench_n_nm"],
            "contract_sha256": contract.sha256,
            "campaign_fingerprint": contract.campaign_fingerprint,
            "program": PROGRAM_B3,
            "b3_two_stage": True,
            "parent_mainline_fingerprint": MAINLINE_FINGERPRINT,
        }
        for filename, document in (
            ("script1_receipt.json", script1_receipt),
            ("controller_receipt.json", controller_receipt),
            ("software_baseline.json", software_baseline),
            ("launch_context.json", launch_context),
        ):
            _write_json(run_dir / filename, document)

        play = b3_writer.write("play")
        play_active = play.command_sent
        dashboard = _wait_dashboard(args.robot_host, B3_TARGET, running=True)
        snapshot: R004OutputSnapshot | None = None
        with RTDEClient(args.robot_host, timeout=3.0) as client:
            client.negotiate(version=2)
            recipe, types = client.setup_outputs(500.0, OUTPUT_FIELDS)
            client.start()
            deadline = time.monotonic() + 5.0
            while time.monotonic() < deadline:
                mapping = dict(
                    zip(OUTPUT_FIELDS, client.recv_recipe_sample(recipe, types), strict=True)
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
            raise RuntimeError("B3 READY runtime identity was not observed")
        runtime_evidence = {
            "program": PROGRAM_B3,
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
        ledger_path, queue_root, authority_root = _initialize_run_state(
            run_dir,
            campaign_fingerprint=contract.campaign_fingerprint,
            eoat_sha256=r004_parent.eoat_sha256,
        )
        print(
            json.dumps(
                {
                    "status": "r008_b3_two_stage_resident_ready_no_arm",
                    "launch_context": launch_context,
                    "software_baseline": software_baseline,
                    "ready": ready_evidence,
                    "host_state": {
                        "ledger_path": str(ledger_path),
                        "queue_root": str(queue_root),
                        "authority_root": str(authority_root),
                    },
                    "schedule": contract.schedule.as_dict(),
                },
                sort_keys=True,
            )
        )
        return 0
    except Exception:
        if play_active:
            try:
                b3_writer.write("stop")
            except Exception:
                pass
        raise


if __name__ == "__main__":
    raise SystemExit(main())
