"""Prepare one independent Figure-eight resident session at frozen Home.

This owner performs no ARM and no contact motion.  It cold-verifies the
already delivered package and no-contact canary, records a fresh read-only
Kunwei baseline, publishes one neutral HOLD image, then loads/plays the
resident Step6 program and proves READY/no-ARM through RTDE.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path
import time
from typing import Any, Mapping

from step5d_autotune_v3.dashboard import dashboard_exchange
from step5d_remote_startup import RemoteDashboardWriter
from step5d_autotune_v4_r004.contracts import runtime_identity_limbs
from step5d_autotune_v4_r004.transport import OUTPUT_FIELDS, R004OutputSnapshot
from step5d_autotune_v4_r006.thresholds import SCHEMA as THRESHOLD_SCHEMA
from step5d_autotune_v4_r006.thresholds import VERSION as THRESHOLD_VERSION
from step5d_autotune_v4_r013.live_owner import (
    R013PathProfileV1,
    _r013_compat_contract,
)
from step5d_autotune_v4_r013.prepare_live import (
    _capture_software_baseline,
    _dashboard_running,
    _loaded_target,
    _observe_dashboard,
    _publish_neutral_hold,
    _receipt_digest,
    _require_remote_normal,
    _snapshot_value,
    _state_sample,
    _validate_fresh_baseline,
    _wait_dashboard,
)
from step5d_autotune_v4_r013.runtime_strategy import (
    DISABLED_RUNTIME_STRATEGY,
    runtime_strategy_sha256,
)
from step5d_eoat_profiles import load_new_eoat_profile
from step5d_managed_runtime import (
    ManagedRuntimeError,
    require_execution_admission_receipt,
)
from step5d_autotune_v3.rtde_client import RTDEClient

from .core import FigureEightCampaignFingerprintV1
from .live_composition import derive_figure8_home_start_receipt
from .physical_ledger import FigureEightPhysicalLedgerV1


PROGRAM = "step6_figure8_autotune_v1"
CONTROLLER_TARGET = f"/programs/andyl/kunwei/step6/{PROGRAM}.urp"
RUNTIME_PROTOCOL = 607007
RUNTIME_REVISION = 5
RUNTIME_EXTENSION = 520607
INT32_MAX = 2**31 - 1
# The reused R006 prerequisite schema requires a finite positive application
# threshold even though the Figure-eight owner composes only the physical
# R005LiveRuntimePort and never the R006HostLoop completion policy.  Keep the
# compatibility value physically unattainable instead of carrying R013's old
# 0.35 N early-stop surface into this lineage.
FIGURE8_DISABLED_APPLICATION_THRESHOLD_N = 1e-12
DASHBOARD_COMMANDS = (
    "is in remote control",
    "safetymode",
    "robotmode",
    "running",
    "programState",
    "get loaded program",
)


class FigureEightPreparationError(RuntimeError):
    """The pre-ARM Figure-eight resident preparation failed closed."""


def _snapshot_register(snapshot: Any, register: int) -> int | None:
    echoes = _snapshot_value(snapshot, "integer_echoes", {})
    if not isinstance(echoes, Mapping):
        return None
    value = echoes.get(register, echoes.get(str(register)))
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _v5_ready_snapshot(snapshot: Any) -> bool:
    return (
        _snapshot_register(snapshot, 32) == RUNTIME_PROTOCOL
        and _snapshot_register(snapshot, 33) == RUNTIME_REVISION
        and _snapshot_register(snapshot, 34) == RUNTIME_EXTENSION
        and _snapshot_register(snapshot, 26) == 78
        and bool(_snapshot_value(snapshot, "safety_normal"))
        and bool(_snapshot_value(snapshot, "stationary"))
        and bool(_snapshot_value(snapshot, "program_running"))
    )


def _observe_v5_ready(
    host: str,
    _dashboard: Mapping[str, str],
    *,
    sleeper: Any,
    timeout_s: float = 5.0,
) -> R004OutputSnapshot:
    """Observe V5 READY on the unchanged mature output24..34 recipe."""

    with RTDEClient(host, timeout=3.0) as client:
        client.negotiate(version=2)
        recipe, types = client.setup_outputs(500.0, OUTPUT_FIELDS)
        client.start()
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            mapping = dict(
                zip(
                    OUTPUT_FIELDS,
                    client.recv_recipe_sample(recipe, types),
                    strict=True,
                )
            )
            snapshot = R004OutputSnapshot.from_mapping(time.time(), mapping)
            if _v5_ready_snapshot(snapshot):
                return snapshot
            sleeper(0.001)
    raise FigureEightPreparationError("V5 READY runtime identity was not observed")


def _sha256(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _write_json(path: Path, value: Mapping[str, Any]) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(target.name + f".tmp-{os.getpid()}")
    try:
        with temporary.open("x", encoding="utf-8") as stream:
            json.dump(dict(value), stream, sort_keys=True, indent=2, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
    finally:
        if temporary.exists():
            temporary.unlink()


def _read_json(path: Path, role: str) -> dict[str, Any]:
    target = Path(path)
    if target.is_symlink() or not target.is_file():
        raise FigureEightPreparationError(f"{role} is not a regular file")
    try:
        value = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError, TypeError) as exc:
        raise FigureEightPreparationError(f"{role} is unreadable") from exc
    if not isinstance(value, dict):
        raise FigureEightPreparationError(f"{role} is not an object")
    return value


def verify_controller_readback(
    *, root: Path, readback_dir: Path
) -> tuple[dict[str, Any], dict[str, str]]:
    """Cold-verify byte closure of the previously fetched Step6 triplet."""

    root = Path(root).resolve()
    directory = Path(readback_dir).resolve()
    manifest = _read_json(directory / "manifest.json", "Step6 read-back manifest")
    local_dir = root / "programs" / "step6"
    old_schema = manifest.get("schema") == "step6.figure8/controller-readback-v1"
    upload_schema = (
        manifest.get("status") == "controller read-back verified"
        and manifest.get("validation", {}).get("program") == PROGRAM
    )
    if not old_schema and not upload_schema:
        raise FigureEightPreparationError("Step6 controller read-back manifest is not passed")

    # The original Step6 delivery surface materialized a typed
    # ``step6.figure8/controller-readback-v1`` manifest.  The governed
    # controller uploader now emits its stronger, generic
    # ``ur10e_upload_result_v1`` result.  Accept the latter only after checking
    # every local/controller/fetched SHA and then normalize it in memory; the
    # raw uploader manifest remains the immutable source artifact.
    if old_schema:
        if (
            manifest.get("program") != PROGRAM
            or manifest.get("state") != "controller_readback_verified"
            or manifest.get("fresh_controller_readback") is not True
            or manifest.get("byte_for_byte_sha_closure") is not True
            or manifest.get("urp_internal_content_gate") is not True
        ):
            raise FigureEightPreparationError("Step6 controller read-back manifest is not passed")
        artifacts = manifest.get("artifacts")
        if not isinstance(artifacts, Mapping) or set(artifacts) != {"script", "txt", "urp"}:
            raise FigureEightPreparationError("Step6 read-back triplet is incomplete")
        triplet: dict[str, str] = {}
        for role in ("script", "txt", "urp"):
            row = artifacts[role]
            if not isinstance(row, Mapping):
                raise FigureEightPreparationError(f"Step6 read-back {role} row is invalid")
            local = local_dir / f"{PROGRAM}.{role}"
            fetched = directory / str(row.get("path", ""))
            expected = str(row.get("sha256", ""))
            if (
                any(path.is_symlink() or not path.is_file() for path in (local, fetched))
                or _sha256(local) != expected
                or _sha256(fetched) != expected
            ):
                raise FigureEightPreparationError(
                    f"Step6 controller read-back {role} differs from local bytes"
                )
            triplet[role] = expected
        return manifest, triplet

    validation = manifest.get("validation")
    hashes = manifest.get("sha256")
    target_dir = f"/programs/andyl/kunwei/step6"
    if (
        not isinstance(validation, Mapping)
        or validation.get("target_dir") != target_dir
        or manifest.get("target_dir") != target_dir
        or manifest.get("fresh_controller_sha_verified") is not True
        or manifest.get("readback_source") != "fresh_controller_get"
        or not isinstance(hashes, Mapping)
    ):
        raise FigureEightPreparationError("Step6 uploader read-back binding is incomplete")
    triplet = {}
    artifacts: dict[str, dict[str, Any]] = {}
    validation_keys = {"script": "script_sha256", "txt": "txt_sha256", "urp": "urp_sha256"}
    for role, validation_key in validation_keys.items():
        ext = f".{role}"
        local = local_dir / f"{PROGRAM}.{role}"
        fetched = directory / f"{PROGRAM}.{role}"
        local_hashes = hashes.get("local", {})
        controller_hashes = hashes.get("controller", {})
        readback_hashes = hashes.get("readback", {})
        expected = local_hashes.get(ext) if isinstance(local_hashes, Mapping) else None
        if (
            not isinstance(expected, str)
            or validation.get(validation_key) != expected
            or not isinstance(controller_hashes, Mapping)
            or not isinstance(readback_hashes, Mapping)
            or controller_hashes.get(ext) != expected
            or readback_hashes.get(ext) != expected
            or any(path.is_symlink() or not path.is_file() for path in (local, fetched))
            or _sha256(local) != expected
            or _sha256(fetched) != expected
        ):
            raise FigureEightPreparationError(
                f"Step6 uploader read-back {role} differs from local bytes"
            )
        triplet[role] = expected
        artifacts[role] = {"path": str(fetched), "sha256": expected}
    normalized = dict(manifest)
    normalized.update(
        {
            "schema": "step6.figure8/controller-readback-v1",
            "program": PROGRAM,
            "controller_directory": target_dir,
            "state": "controller_readback_verified",
            "fresh_controller_readback": True,
            "urp_internal_content_gate": True,
            "byte_for_byte_sha_closure": True,
            "artifacts": artifacts,
            "source_manifest_schema": "ur10e_upload_result_v1",
        }
    )
    return normalized, triplet


def _require_figure8_home(
    sample: Mapping[str, Any], *, expected_pose: tuple[float, ...], expected_q: tuple[float, ...]
) -> None:
    pose = tuple(float(value) for value in sample.get("actual_TCP_pose", ()))
    q = tuple(float(value) for value in sample.get("actual_q", ()))
    speed = tuple(float(value) for value in sample.get("actual_TCP_speed", ()))
    if len(pose) != 6 or len(q) != 6 or len(speed) != 6:
        raise FigureEightPreparationError("Figure-eight Home state is incomplete")
    if len(expected_pose) != 6:
        raise FigureEightPreparationError("Figure-eight expected Home pose is incomplete")
    position_error = math.dist(pose[:3], expected_pose[:3])
    orientation_error = math.dist(pose[3:], expected_pose[3:])
    q_error = max(abs(actual - expected) for actual, expected in zip(q, expected_q, strict=True))
    linear_speed = math.sqrt(sum(value * value for value in speed[:3]))
    angular_speed = math.sqrt(sum(value * value for value in speed[3:]))
    if (
        sample.get("safety_mode") not in (1, "NORMAL")
        or position_error > 0.001
        or orientation_error > 0.01
        or q_error > 0.02
        or linear_speed > 0.0005
        or angular_speed > 0.005
    ):
        raise FigureEightPreparationError(
            "Figure-eight current Home/IK/stationary/Safety gate failed: "
            f"position={position_error} orientation={orientation_error} "
            f"q={q_error} linear_speed={linear_speed} angular_speed={angular_speed}"
        )


def _ready_evidence(
    *,
    snapshot: Any,
    dashboard: Mapping[str, str],
    contract_sha256: str,
    compatibility_fingerprint: str,
    physical_fingerprint: str,
    route_id: str,
    attempt_id: str,
    session_id: str,
    session_epoch: int,
    triplet: Mapping[str, str],
) -> dict[str, Any]:
    echoes = dict(_snapshot_value(snapshot, "integer_echoes", {}))
    if (
        echoes.get(32) != RUNTIME_PROTOCOL
        or echoes.get(33) != RUNTIME_REVISION
        or echoes.get(34) != RUNTIME_EXTENSION
        or echoes.get(26) != 78
        or not bool(_snapshot_value(snapshot, "safety_normal"))
        or not bool(_snapshot_value(snapshot, "stationary"))
        or not bool(_snapshot_value(snapshot, "program_running"))
    ):
        raise FigureEightPreparationError("Step6 resident is not exact READY/no-ARM")
    return {
        "schema": "step5d.autotune-v4/r013-resident-ready-evidence-v2",
        "status": "resident_ready_no_arm",
        "observation_source": "fresh_rtde_observation",
        "observed_at_s": float(_snapshot_value(snapshot, "observed_at_s")),
        "contract_sha256": contract_sha256,
        "campaign_fingerprint": compatibility_fingerprint,
        "figure8_campaign_fingerprint_sha256": physical_fingerprint,
        "route_id": route_id,
        "attempt_id": attempt_id,
        "resident_session_id": session_id,
        "session_epoch": session_epoch,
        "program": PROGRAM,
        "controller_target": CONTROLLER_TARGET,
        "triplet_sha256": dict(triplet),
        "dashboard": dict(dashboard),
        "runtime_output_registers": {
            "32": RUNTIME_PROTOCOL,
            "33": RUNTIME_REVISION,
            "34": RUNTIME_EXTENSION,
        },
        "runtime_protocol": RUNTIME_PROTOCOL,
        "runtime_revision": RUNTIME_REVISION,
        "runtime_extension_protocol": RUNTIME_EXTENSION,
        "tp_state": 78,
        "program_running": True,
        "stationary": True,
        "safety_normal": True,
        "tcp_pose_m_rad": list(_snapshot_value(snapshot, "tcp_pose_m_rad", ())),
        "q_rad": list(_snapshot_value(snapshot, "q_rad", ())),
        "no_arm": True,
        "arm_dispatched": False,
        "trial_dispatched": False,
    }


def _threshold_receipt(
    *, contract_sha256: str, campaign_fingerprint: str, issued_at_s: float
) -> dict[str, Any]:
    body = {
        "schema": THRESHOLD_SCHEMA,
        "version": THRESHOLD_VERSION,
        "pac_epsilon_n": 0.05,
        "application_mae_threshold_n": FIGURE8_DISABLED_APPLICATION_THRESHOLD_N,
        "contract_sha256": contract_sha256,
        "campaign_fingerprint": campaign_fingerprint,
        "issued_at_unix_s": float(issued_at_s),
    }
    return {**body, "receipt_sha256": _receipt_digest(body)}


def prepare_figure8_live_run(
    run_dir: Path,
    *,
    root: Path,
    robot_host: str,
    kunwei_host: str,
    kunwei_port: int,
    controller_readback_dir: Path,
    canary_dir: Path,
    home_calibration_receipt_path: Path | None,
    fingerprint: FigureEightCampaignFingerprintV1,
    campaign_id: str,
    run_id: str,
    attempt_id: str,
) -> dict[str, Any]:
    """End at an uninterrupted resident READY state without dispatching ARM."""

    try:
        runtime_admission = require_execution_admission_receipt()
    except ManagedRuntimeError as exc:
        raise FigureEightPreparationError(str(exc)) from exc
    if runtime_admission.get("controller_calls_allowed") is not True:
        raise FigureEightPreparationError("runtime admission does not allow controller preparation")
    if runtime_admission.get("authority_granted") is not False:
        raise FigureEightPreparationError("runtime admission cannot grant authority")
    destination = Path(run_dir).resolve()
    if destination.exists():
        raise FigureEightPreparationError("fresh Figure-eight run directory already exists")
    root = Path(root).resolve()
    canary = Path(canary_dir).resolve()
    manifest, triplet = verify_controller_readback(
        root=root, readback_dir=controller_readback_dir
    )
    if dict(fingerprint.controller_triplet_sha256) != triplet:
        raise FigureEightPreparationError("Figure-eight fingerprint triplet differs")
    home_start = derive_figure8_home_start_receipt(
        canary_evidence_path=canary / "evidence_receipt_v2.json",
        frame_receipt_path=canary / "frame_receipt_v2.json",
        raw_trace_path=canary / "raw_rtde_trace.jsonl",
        home_calibration_receipt_path=home_calibration_receipt_path,
        calibration_only=home_calibration_receipt_path is None,
    )
    expected_pose = tuple(float(value) for value in home_start["home_pose"])
    if tuple(fingerprint.home_pose) != expected_pose:
        raise FigureEightPreparationError("Figure-eight fingerprint Home differs from canary Home")
    expected_q = tuple(float(value) for value in home_start["final_q"])
    dashboard = dict(_observe_dashboard(robot_host))
    _require_remote_normal(dashboard)
    if _dashboard_running(dashboard):
        raise FigureEightPreparationError(
            "an existing controller program is running; preparation will not stop an unknown writer"
        )
    home_sample = dict(_state_sample(robot_host))
    _require_figure8_home(
        home_sample,
        expected_pose=expected_pose,
        expected_q=expected_q,
    )

    identity_nonce = time.time_ns()
    epoch = int(time.time())
    if not 1 <= epoch <= INT32_MAX:
        raise FigureEightPreparationError("Figure-eight session epoch is outside TP INT32")
    route_id = f"r006-r013-figure8-{identity_nonce}"
    session_id = f"r006-r013-figure8-resident-{identity_nonce}"
    if "r006" not in attempt_id.lower():
        raise FigureEightPreparationError("Figure-eight attempt id must retain r006 compatibility")

    destination.mkdir(parents=True, exist_ok=False)
    (destination / "authority").mkdir()
    (destination / "r006-queue").mkdir()
    runtime_admission_path = destination / "runtime_admission_receipt.json"
    _write_json(runtime_admission_path, runtime_admission)
    play_sent = False
    writer: Any | None = None
    failure: Exception | None = None
    try:
        baseline = _validate_fresh_baseline(
            _capture_software_baseline(kunwei_host, int(kunwei_port)),
            after_s=float(home_start["observed_at_s"]),
        )
        baseline["campaign_fingerprint"] = fingerprint.sha256
        neutral_hold = dict(_publish_neutral_hold(robot_host))
        if (
            neutral_hold.get("layout_tag") != 606.0
            or neutral_hold.get("session_command") != "HOLD"
            or neutral_hold.get("arm_dispatched") is not False
        ):
            raise FigureEightPreparationError("Figure-eight neutral HOLD receipt differs")

        writer = RemoteDashboardWriter(
            robot_host, load_target=CONTROLLER_TARGET, timeout_s=3.0
        )
        writer.write(f"load {CONTROLLER_TARGET}")
        _wait_dashboard(
            robot_host,
            CONTROLLER_TARGET,
            running=False,
            observer=_observe_dashboard,
            sleeper=time.sleep,
        )
        outcome = writer.write("play")
        play_sent = bool(getattr(outcome, "command_sent", True))
        resident_dashboard = _wait_dashboard(
            robot_host,
            CONTROLLER_TARGET,
            running=True,
            observer=_observe_dashboard,
            sleeper=time.sleep,
        )
        controller_snapshot = _observe_v5_ready(
            robot_host, resident_dashboard, sleeper=time.sleep
        )
        runtime_snapshot = _observe_v5_ready(
            robot_host, resident_dashboard, sleeper=time.sleep
        )
        controller_observed = float(
            _snapshot_value(controller_snapshot, "observed_at_s")
        )
        runtime_observed = float(_snapshot_value(runtime_snapshot, "observed_at_s"))
        if runtime_observed <= controller_observed:
            raise FigureEightPreparationError(
                "Figure-eight READY runtime observation is not newer"
            )

        contract = _r013_compat_contract(R013PathProfileV1.figure8())
        compatibility_fingerprint = str(contract.campaign_fingerprint)
        contract_sha = str(contract.sha256)
        hi, lo = runtime_identity_limbs(
            PROGRAM, contract_sha, compatibility_fingerprint
        )
        ready_evidence = _ready_evidence(
            snapshot=runtime_snapshot,
            dashboard=resident_dashboard,
            contract_sha256=contract_sha,
            compatibility_fingerprint=compatibility_fingerprint,
            physical_fingerprint=fingerprint.sha256,
            route_id=route_id,
            attempt_id=attempt_id,
            session_id=session_id,
            session_epoch=epoch,
            triplet=triplet,
        )
        projection = {
            "kind": "v5_canonical_sha_limbs_out_of_band",
            "physically_read": False,
            "source_evidence": "resident_ready_evidence.json",
            "runtime_digest_hi": hi,
            "runtime_digest_lo": lo,
        }
        eoat = load_new_eoat_profile()
        controller_receipt = {
            "schema": "step6.autotune/figure8-controller-receipt-v1",
            "observation_source": "fresh_controller_readback_and_rtde",
            "program": PROGRAM,
            "controller_target": CONTROLLER_TARGET,
            **{f"{role}_sha256": digest for role, digest in triplet.items()},
            "controller_readback": manifest,
            "observed_at_s": controller_observed,
            "runtime_protocol": RUNTIME_PROTOCOL,
            "runtime_digest_hi": hi,
            "runtime_digest_lo": lo,
            "runtime_identity_projection": projection,
            "eoat_identity_sha256": eoat.profile_sha256,
            "readback": {
                "actual_tcp_speed_m_s_rad_s": list(
                    _snapshot_value(controller_snapshot, "tcp_speed_m_s_rad_s", ())
                ),
                "payload_kg": float(_snapshot_value(controller_snapshot, "payload_kg")),
                "payload_cog_m": list(
                    _snapshot_value(controller_snapshot, "payload_cog_m", ())
                ),
                "tcp_offset_m_rad": list(
                    _snapshot_value(controller_snapshot, "tcp_offset_m_rad", ())
                ),
            },
            "safety_mode": "NORMAL",
            "stationary": True,
            "route_id": route_id,
        }
        controller_receipt["receipt_sha256"] = _receipt_digest(controller_receipt)
        runtime_evidence = {
            "schema": "step6.autotune/figure8-runtime-evidence-v1",
            "observation_source": "resident_ready_evidence.json",
            "program": PROGRAM,
            "script_sha256": triplet["script"],
            "runtime_protocol": RUNTIME_PROTOCOL,
            "runtime_digest_hi": hi,
            "runtime_digest_lo": lo,
            "runtime_identity_projection": projection,
            "session_epoch": epoch,
            "resident_session_id": session_id,
            "program_running": True,
            "uninterrupted": True,
            "observed_at_s": runtime_observed,
        }
        strategy = dict(DISABLED_RUNTIME_STRATEGY)
        strategy_sha = runtime_strategy_sha256(strategy)
        physical_ledger_path = destination / "r006-physical-observations.jsonl"
        FigureEightPhysicalLedgerV1(
            physical_ledger_path,
            campaign_fingerprint=fingerprint.sha256,
            eoat_sha256=eoat.profile_sha256,
        )
        launch_profile = {
            "schema": "step5d.autotune-v3/launch-profile-v1",
            "tp_program_id": PROGRAM,
            "release_stage_id": "step6_figure8_direct_v1",
            "figure8_campaign_fingerprint_sha256": fingerprint.sha256,
            "campaign_target_stop": False,
            "trial_censor_active": True,
            "r006_host_loop_completion_policy_used": False,
            "application_threshold_reachable": False,
        }
        launch = {
            "schema": "step6.autotune/figure8-launch-context-v1",
            "campaign_id": campaign_id,
            "run_id": run_id,
            "campaign_fingerprint": compatibility_fingerprint,
            "figure8_campaign_fingerprint_sha256": fingerprint.sha256,
            "contract_sha256": contract_sha,
            "program": PROGRAM,
            "controller_target": CONTROLLER_TARGET,
            "v5_runtime_extension": RUNTIME_EXTENSION,
            "route_id": route_id,
            "attempt_id": attempt_id,
            "session_id": session_id,
            "session_epoch": epoch,
            "runtime_protocol": RUNTIME_PROTOCOL,
            "runtime_digest_hi": hi,
            "runtime_digest_lo": lo,
            "runtime_identity_projection": projection,
            "triplet": triplet,
            "resident_ready_evidence": str(destination / "resident_ready_evidence.json"),
            "software_baseline_n": baseline["mean_wrench_n_nm"],
            "runtime_strategy_sha256": strategy_sha,
            "runtime_strategy_enabled": False,
        }
        ready = {
            "schema": "step6.autotune/figure8-live-owner-ready-v1",
            "status": "resident_ready_no_arm",
            "program": PROGRAM,
            "controller_target": CONTROLLER_TARGET,
            "ledger_path": str(physical_ledger_path),
            "queue_root": str(destination / "r006-queue"),
            "authority_root": str(destination / "authority"),
            "route_id": route_id,
            "attempt_id": attempt_id,
            "resident_session_id": session_id,
            "session_epoch": epoch,
            "contract_sha256": contract_sha,
            "campaign_fingerprint": compatibility_fingerprint,
            "figure8_campaign_fingerprint_sha256": fingerprint.sha256,
            "runtime_protocol": RUNTIME_PROTOCOL,
            "runtime_digest_hi": hi,
            "runtime_digest_lo": lo,
            "runtime_identity_projection": projection,
            "triplet": triplet,
            "resident_ready_evidence": str(destination / "resident_ready_evidence.json"),
            "observed_at_s": runtime_observed,
            "arm_dispatched": False,
            "trial_dispatched": False,
            "runtime_strategy_sha256": strategy_sha,
            "runtime_strategy_enabled": False,
        }
        seed_candidate = {
            "force_p_gain": 0.019027313840405524,
            "force_damping": 188.36079701683204,
            "force_i_gain": 0.008610779292198037,
            "i_off": False,
            "normal_filter_tau_s": 0.04375,
            "orientation_ko": 0.05,
            "motion_kp": 2.5226892457611436,
            "target_force_n": 5.0,
        }
        seed = {
            "schema": "step6.autotune/figure8-provisional-cycloid-n3-seed-v1",
            "source_classification": "provisional_warm_start_not_confirmed_transfer",
            "confirmed_incumbent": {
                "candidate": seed_candidate,
                "confirmed": False,
                "source_single_minimum_mae_n": 0.3162640181193824,
                "source_repeat_mean_mae_n": 0.347410,
                "source_repeat_std_mae_n": 0.025409,
            },
        }
        preparation = {
            "schema": "step6.autotune/figure8-live-preparation-v1",
            "passed": True,
            "campaign_id": campaign_id,
            "run_id": run_id,
            "attempt_id": attempt_id,
            "program": PROGRAM,
            "controller_target": CONTROLLER_TARGET,
            "physical_fingerprint_sha256": fingerprint.sha256,
            "compatibility_contract_sha256": contract_sha,
            "compatibility_campaign_fingerprint": compatibility_fingerprint,
            "package_readback": str(Path(controller_readback_dir).resolve() / "manifest.json"),
            "no_contact_canary": str(canary / "evidence_receipt_v2.json"),
            "runtime_admission_receipt": str(runtime_admission_path),
            "home_start_receipt": str(destination / "home_start_receipt.json"),
            "resident_ready_evidence": str(destination / "resident_ready_evidence.json"),
            "arm_dispatched": False,
            "trial_dispatched": False,
            "contact_executed": False,
        }
        artifacts = {
            "home_start_receipt.json": home_start,
            # Compatibility filename; the content remains explicitly a
            # no-contact Figure-eight Home-start receipt, never old Script1.
            "script1_receipt.json": home_start,
            "controller_receipt.json": controller_receipt,
            "software_baseline.json": baseline,
            "neutral_hold_receipt.json": neutral_hold,
            "thresholds_receipt.json": _threshold_receipt(
                contract_sha256=contract_sha,
                campaign_fingerprint=compatibility_fingerprint,
                issued_at_s=float(baseline["observed_at_s"]),
            ),
            "runtime_evidence.json": runtime_evidence,
            "resident_ready_evidence.json": ready_evidence,
            "r013_live_owner_ready.json": ready,
            "launch_context.json": launch,
            "figure8_launch_profile.json": launch_profile,
            "r012_seed_source.json": seed,
            "optimizer_snapshot.json": {
                "schema": "step6.autotune/figure8-optimizer-snapshot-v1",
                "novel_budget": [200, 200],
                "campaign_target_stop": False,
                "trial_censor_active": True,
                "pool_size": 128,
                "q": 1,
            },
            "runtime_strategy.json": strategy,
            "figure8_fingerprint.json": fingerprint.as_dict(),
            "r013_live_preparation.json": preparation,
        }
        for name, value in artifacts.items():
            _write_json(destination / name, value)
        return preparation
    except Exception as exc:
        failure = exc
        try:
            _write_json(
                destination / "preparation_failure.json",
                {
                    "schema": "step6.autotune/figure8-preparation-failure-v1",
                    "passed": False,
                    "error": str(exc),
                    "play_sent": play_sent,
                    "arm_dispatched": False,
                    "trial_dispatched": False,
                    "captured_at_s": time.time(),
                },
            )
        except Exception:
            pass
        raise
    finally:
        if failure is not None and play_sent and writer is not None:
            try:
                writer.write("stop")
            except Exception:
                pass


__all__ = [
    "CONTROLLER_TARGET",
    "FigureEightPreparationError",
    "PROGRAM",
    "prepare_figure8_live_run",
    "verify_controller_readback",
]
