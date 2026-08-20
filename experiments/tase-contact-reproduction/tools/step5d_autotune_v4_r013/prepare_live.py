"""Prepare one truthful, fresh R013 resident session.

The preparation boundary owns only Remote Dashboard startup, read-only RTDE
and Kunwei observations, receipt construction, and fresh host genesis.  It
never asks the R013 campaign for a trial and never sends ARM.
"""

from __future__ import annotations

import argparse
from dataclasses import replace
import hashlib
import json
import math
import os
from pathlib import Path
import statistics
import time
import uuid
from types import SimpleNamespace
from typing import Any, Callable, Mapping

from .campaign import (
    Campaign,
    r012_seed_source_from_ledger,
    select_seed_template,
    strategy_canary_plan,
)
from .bounded_bo import BoundedBOSeedConfigV1
from .campaign_config import (
    DEFAULT_CONFIG_PATH,
    MANUAL_CANARY_ROLE,
    R013BudgetedFloorConfig,
    R013ManualCanaryPreparationProfileV1,
    _bind_materialized_r013_profiles,
    controller_source_identity_sha256,
    home_tare_procedure_identity,
    materialize_campaign_fingerprint,
    load_r013_budgeted_floor_config,
    r013_host_source_closure,
    require_campaign_config_binding,
)
from .controller_triplet import R013_PROGRAM, R013_RUNTIME_PROTOCOL
from .handoff import HandoffPolicy
from .identity import CampaignFingerprint, LEGACY_PROFILE_IDENTITY
from .runtime_strategy import (
    DISABLED_RUNTIME_STRATEGY,
    RuntimeStrategyReceiptLedger,
    runtime_strategy_sha256,
    validate_runtime_strategy,
)
from step5d_autotune_v4_r004.contracts import SCRIPT1_TARGET_POSE
from step5d_autotune_v4_r004.transport import OUTPUT_FIELDS, R004OutputSnapshot
from step5d_autotune_v4_r005.observations import ObservationLedger
from step5d_autotune_v4_r006.thresholds import SCHEMA as THRESHOLD_SCHEMA
from step5d_autotune_v4_r006.thresholds import VERSION as THRESHOLD_VERSION
from step5d_autotune_v4_r013.feedforward import FeedforwardMode, FeedforwardProfile
from step5d_autotune_v4_r012.path_cbf_live import (
    R012_HARD_TUBE_AXES_M,
    R012_SOFT_CBF_AXES_M,
)
from step5d_autotune_v3.dashboard import dashboard_exchange
from step5d_autotune_v3.rtde_client import RTDEClient
from step5d_remote_startup import RemoteDashboardWriter


ROOT = Path(__file__).resolve().parents[2]
SCRIPT1_TARGET = "/programs/andyl/kunwei/step5/step5d_autotune_start_hover_r001.urp"
R013_TARGET = f"/programs/andyl/kunwei/step5/{R013_PROGRAM}.urp"
SCRIPT1_SOURCE = ROOT / "programs/step5/step5d/step5d_autotune_start_hover_r001.script"
INT32_MAX = 2**31 - 1
R013_MOTION_PROTOCOL = 606006
R013_REVISION = 13
MANUAL_CANARY_PATH_ID = "r013_manual_canary_path_v1"
MANUAL_CANARY_METRIC_FINGERPRINT = "r013_manual_canary_nonformal_v1"
MANUAL_CANARY_CONTROLLER_LINEAGE = "step5d_strict_rnn_autotune_v4_r013_manual_canary"
BOUNDED_BO_CONTROLLER_LINEAGE = "step5d_strict_rnn_autotune_v4_r013_bounded_bo_v1"
BOUNDED_BO_ROLE = "bounded_bo_v1"

DASHBOARD_COMMANDS = (
    "is in remote control",
    "safetymode",
    "robotmode",
    "running",
    "programState",
    "get loaded program",
)


def _sha(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _controller_triplet_from_receipt(value: Mapping[str, Any]) -> dict[str, str]:
    readback = value.get("controller_readback")
    if isinstance(readback, Mapping) and isinstance(readback.get("triplet_sha256"), Mapping):
        triplet = readback["triplet_sha256"]
    else:
        triplet = {
            role: value.get(f"{role}_sha256")
            for role in ("script", "txt", "urp")
        }
    if not isinstance(triplet, Mapping):
        raise RuntimeError("R013 persisted controller source identity is missing")
    return {role: str(triplet.get(role, "")) for role in ("script", "txt", "urp")}


def _verify_local_controller_triplet(
    root: Path,
    *,
    triplet: Mapping[str, str],
) -> None:
    package_dir = Path(root) / "programs" / "step5" / "step5d"
    for role in ("script", "txt", "urp"):
        path = package_dir / f"{R013_PROGRAM}.{role}"
        if path.is_symlink() or not path.is_file() or _sha(path) != triplet.get(role):
            raise RuntimeError("R013 local controller source identity differs")


def _receipt_digest(value: Mapping[str, Any]) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _write_json(path: Path, value: Mapping[str, Any]) -> None:
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(destination.name + ".tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        json.dump(dict(value), stream, sort_keys=True, indent=2, allow_nan=False)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, destination)


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
    host: str,
    target: str,
    *,
    running: bool,
    observer: Callable[[str], Mapping[str, str]],
    sleeper: Callable[[float], None],
    timeout_s: float = 15.0,
) -> dict[str, str]:
    deadline = time.monotonic() + timeout_s
    last: dict[str, str] = {}
    while time.monotonic() < deadline:
        last = dict(observer(host))
        _require_remote_normal(last)
        stopped = last.get("programState", "").startswith("STOPPED")
        if _loaded_target(last) == target and _dashboard_running(last) is running and (
            running or stopped
        ):
            return last
        sleeper(0.02)
    raise RuntimeError(f"Dashboard target state was not reached: {last}")


def _state_sample(host: str) -> dict[str, Any]:
    fields = (
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
    with RTDEClient(host, timeout=3.0) as client:
        client.negotiate(version=2)
        recipe, types = client.setup_outputs(500.0, fields)
        client.start()
        observed_at_s = time.time()
        values = client.recv_recipe_sample(recipe, types)
    return {**dict(zip(fields, values, strict=True)), "observed_at_s": observed_at_s}


def _stationary_normal(sample: Mapping[str, Any], role: str) -> None:
    speed = tuple(float(value) for value in sample["actual_TCP_speed"])
    linear = math.sqrt(sum(value * value for value in speed[:3]))
    angular = math.sqrt(sum(value * value for value in speed[3:]))
    if (
        sample.get("safety_mode") not in (1, "NORMAL")
        or linear > 0.0005
        or angular > 0.005
    ):
        raise RuntimeError(f"{role} is not stationary Safety NORMAL")


def _fixed_home(sample: Mapping[str, Any]) -> None:
    pose = tuple(float(value) for value in sample["actual_TCP_pose"])
    if math.dist(pose, SCRIPT1_TARGET_POSE) > 0.003:
        raise RuntimeError("Script1 final state is not the fixed Home pose")


def _wait_fixed_home(
    host: str,
    *,
    sampler: Callable[[str], Mapping[str, Any]],
    sleeper: Callable[[float], None],
    timeout_s: float = 3.0,
) -> dict[str, Any]:
    """Wait for the post-Script1 RTDE state to settle without relaxing gates."""

    deadline = time.monotonic() + timeout_s
    last_error: RuntimeError | None = None
    while time.monotonic() < deadline:
        sample = dict(sampler(host))
        try:
            _stationary_normal(sample, "Script1 final state")
            _fixed_home(sample)
        except RuntimeError as exc:
            last_error = exc
            sleeper(0.02)
            continue
        return sample
    if last_error is not None:
        raise last_error
    raise RuntimeError("Script1 final state was not observed")


def _capture_software_baseline(host: str, port: int) -> dict[str, Any]:
    """Capture a read-only Kunwei stream baseline; no zero/tare/config write."""

    import step5d_autotune_v4_live_writer as canonical_v4

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
        "observation_source": "fresh_kunwei_live_stream",
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


def _publish_neutral_hold(host: str) -> dict[str, Any]:
    """Replace an arbitrary stale RTDE input image with layout-606 HOLD."""

    from step5d_autotune_v4_r012.register_transport import R012LiveRTDETransport

    transport = R012LiveRTDETransport(host)
    doubles = [0.0] * 24
    doubles[-1] = 606.0
    integers = [0] * 9
    transport.open()
    try:
        transport.send_packet(doubles, integers)
    finally:
        transport.close()
    return {
        "schema": "step5d.autotune-v4/r013-neutral-hold-v1",
        "observation_source": "fresh_rtde_input_write",
        "observed_at_s": time.time(),
        "layout_tag": 606.0,
        "session_command": "HOLD",
        "session_command_value": 0,
        "arm_dispatched": False,
        "trial_dispatched": False,
    }


def _read_controller_triplet(root: Path, readback_dir: Path, *, observed_at_s: float) -> dict[str, Any]:
    """Validate an injected read-back fixture; production uses a fresh GET."""

    local_dir = Path(root) / "programs/step5/step5d"
    readback = Path(readback_dir)
    if readback.is_symlink() or not readback.is_dir():
        raise RuntimeError("R013 controller read-back directory must be a real directory")
    hashes: dict[str, str] = {}
    for role in ("script", "txt", "urp"):
        local = local_dir / f"{R013_PROGRAM}.{role}"
        actual = readback / f"{R013_PROGRAM}.{role}"
        if any(path.is_symlink() or not path.is_file() for path in (local, actual)):
            raise RuntimeError(f"R013 controller read-back {role} is missing")
        local_hash = _sha(local)
        readback_hash = _sha(actual)
        if readback_hash != local_hash:
            raise RuntimeError(f"R013 controller read-back {role} differs from the local triplet")
        hashes[role] = readback_hash
    return {
        "source": "injected_controller_readback_fixture",
        "directory": str(readback.resolve()),
        "observed_at_s": float(observed_at_s),
        "triplet_sha256": hashes,
    }


def _capture_controller_triplet(
    root: Path,
    readback_dir: Path,
    *,
    controller_helper: Path,
    controller_helper_sha256: str,
) -> dict[str, Any]:
    """Freshly GET and byte-close the exact resident R013 triplet."""

    from upload_ur_tp_package import DEFAULT_CONTROLLER, readback_only_existing

    local_dir = Path(root) / "programs/step5/step5d"
    files = {
        f".{role}": local_dir / f"{R013_PROGRAM}.{role}"
        for role in ("script", "txt", "urp")
    }
    closure = readback_only_existing(
        files,
        R013_PROGRAM,
        DEFAULT_CONTROLLER,
        "/programs/andyl/kunwei/step5",
        Path(readback_dir),
        helper=Path(controller_helper),
        helper_sha256=controller_helper_sha256,
    )
    triplet = {
        role: str(closure["readback"][f".{role}"])
        for role in ("script", "txt", "urp")
    }
    if not all(
        triplet[role]
        == closure["local"][f".{role}"]
        == closure["controller"][f".{role}"]
        for role in triplet
    ):
        raise RuntimeError("R013 fresh controller GET byte closure differs")
    return {
        "source": "fresh_controller_get",
        "operation": "readback",
        "directory": str(Path(readback_dir).resolve()),
        "observed_at_s": time.time(),
        "controller_helper_sha256": controller_helper_sha256,
        "triplet_sha256": triplet,
        "byte_closure": {
            family: {role.removeprefix("."): digest for role, digest in values.items()}
            for family, values in closure.items()
        },
    }


def _runtime_identity_limbs(contract_sha: str, campaign_fingerprint: str) -> tuple[int, int]:
    from step5d_autotune_v4_r004.contracts import runtime_identity_limbs

    return runtime_identity_limbs(R013_PROGRAM, contract_sha, campaign_fingerprint)


def _r013_identity() -> tuple[Any, str, str, str]:
    from step5d_autotune_v4_r012.compat_identity import r012_compat_contract
    from step5d_eoat_profiles import load_new_eoat_profile

    contract = r012_compat_contract()
    eoat = load_new_eoat_profile().profile_sha256
    return contract, str(contract.sha256), str(contract.campaign_fingerprint), eoat


def _materialize_manual_canary_fingerprint(
    *,
    triplet: Mapping[str, str],
    runtime_strategy_sha256_value: str,
    eoat_identity_sha256: str,
    script1_source_sha256: str,
    feedforward_profile: FeedforwardProfile,
) -> CampaignFingerprint:
    base = CampaignFingerprint.legacy_default(
        handoff_policy=HandoffPolicy().policy,
        runtime_strategy_identity=runtime_strategy_sha256_value,
    )
    materialized = replace(
        base,
        path_id=MANUAL_CANARY_PATH_ID,
        metric_fingerprint=MANUAL_CANARY_METRIC_FINGERPRINT,
        correction_runtime_strategy_identity=runtime_strategy_sha256_value,
        source_identity=controller_source_identity_sha256(triplet),
        eoat_identity=eoat_identity_sha256,
        home_tare_identity=home_tare_procedure_identity(script1_source_sha256),
        controller_lineage=MANUAL_CANARY_CONTROLLER_LINEAGE,
    )
    return _bind_materialized_r013_profiles(materialized, feedforward_profile)


def _materialize_bounded_bo_fingerprint(
    *,
    triplet: Mapping[str, str],
    runtime_strategy_sha256_value: str,
    eoat_identity_sha256: str,
    script1_source_sha256: str,
    feedforward_profile: FeedforwardProfile,
) -> CampaignFingerprint:
    """Bind a fresh bounded BO campaign to current package/read-back identity."""

    base = CampaignFingerprint.legacy_default(
        handoff_policy=HandoffPolicy().policy,
        runtime_strategy_identity=runtime_strategy_sha256_value,
    )
    materialized = replace(
        base,
        path_id="r013_bounded_bo_path_v1",
        metric_fingerprint="force-mae-v2-sealed|formal=[5,60)|bin=0.1s|target=5N",
        correction_runtime_strategy_identity=runtime_strategy_sha256_value,
        source_identity=controller_source_identity_sha256(triplet),
        eoat_identity=eoat_identity_sha256,
        home_tare_identity=home_tare_procedure_identity(script1_source_sha256),
        controller_lineage=BOUNDED_BO_CONTROLLER_LINEAGE,
    )
    return _bind_materialized_r013_profiles(materialized, feedforward_profile)


def _snapshot_value(snapshot: Any, name: str, fallback: Any = None) -> Any:
    if isinstance(snapshot, Mapping):
        return snapshot.get(name, fallback)
    return getattr(snapshot, name, fallback)


def _snapshot_register(snapshot: Any, register: int) -> int | None:
    echoes = _snapshot_value(snapshot, "integer_echoes", {})
    if not isinstance(echoes, Mapping):
        return None
    value = echoes.get(register, echoes.get(str(register)))
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _ready_snapshot(snapshot: Any) -> bool:
    return (
        _snapshot_register(snapshot, 32) == R013_MOTION_PROTOCOL
        and _snapshot_register(snapshot, 33) == R013_REVISION
        and _snapshot_register(snapshot, 34) == R013_RUNTIME_PROTOCOL
        and _snapshot_register(snapshot, 26) == 78
        and bool(_snapshot_value(snapshot, "safety_normal"))
        and bool(_snapshot_value(snapshot, "stationary"))
        and bool(_snapshot_value(snapshot, "program_running"))
    )


def _observe_r013_ready(
    host: str,
    _dashboard: Mapping[str, str],
    *,
    sleeper: Callable[[float], None],
    timeout_s: float = 5.0,
) -> Any:
    with RTDEClient(host, timeout=3.0) as client:
        client.negotiate(version=2)
        recipe, types = client.setup_outputs(500.0, OUTPUT_FIELDS)
        client.start()
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            mapping = dict(zip(OUTPUT_FIELDS, client.recv_recipe_sample(recipe, types), strict=True))
            snapshot = R004OutputSnapshot.from_mapping(time.time(), mapping)
            if _ready_snapshot(snapshot):
                return snapshot
            sleeper(0.001)
    raise RuntimeError("R013 READY runtime identity was not observed")


def _build_threshold_receipt(*, contract_sha: str, campaign_fingerprint: str, issued_at_s: float) -> dict[str, Any]:
    unsigned = {
        "schema": THRESHOLD_SCHEMA,
        "version": THRESHOLD_VERSION,
        "pac_epsilon_n": 0.05,
        "application_mae_threshold_n": 0.35,
        "contract_sha256": contract_sha,
        "campaign_fingerprint": campaign_fingerprint,
        "issued_at_unix_s": float(issued_at_s),
    }
    return {**unsigned, "receipt_sha256": _receipt_digest(unsigned)}


def _validate_fresh_baseline(baseline: Mapping[str, Any], *, after_s: float) -> dict[str, Any]:
    result = dict(baseline)
    if result.get("schema") != "step5d.autotune-v4/r005-software-baseline-v1":
        raise RuntimeError("fresh Kunwei baseline schema differs")
    if result.get("observation_source") != "fresh_kunwei_live_stream":
        raise RuntimeError("Kunwei baseline is not marked as a fresh live observation")
    if result.get("zero_tare_config_write") is not False:
        raise RuntimeError("Kunwei baseline must not write zero/tare configuration")
    if result.get("parse_errors") != 0 or result.get("dropped_bytes") != 0:
        raise RuntimeError("Kunwei baseline transport evidence is not clean")
    if isinstance(result.get("sample_count"), bool) or not isinstance(result.get("sample_count"), int) or result["sample_count"] < 900:
        raise RuntimeError("fresh Kunwei baseline has too few physical frames")
    observed = float(result.get("observed_at_s"))
    if not math.isfinite(observed) or observed < after_s:
        raise RuntimeError("fresh Kunwei baseline timestamp is invalid or out of order")
    result["observed_at_s"] = observed
    return result


def _fresh_resident_evidence(
    *,
    snapshot: Any,
    dashboard: Mapping[str, str],
    contract_sha: str,
    campaign_fingerprint: str,
    route_id: str,
    attempt_id: str,
    session_id: str,
    session_epoch: int,
    triplet: Mapping[str, str],
    controller_target: str,
) -> dict[str, Any]:
    if not _ready_snapshot(snapshot):
        raise RuntimeError("R013 resident snapshot is not exact READY/stationary/running")
    return {
        "schema": "step5d.autotune-v4/r013-resident-ready-evidence-v2",
        "status": "resident_ready_no_arm",
        "observation_source": "fresh_rtde_observation",
        "observed_at_s": float(_snapshot_value(snapshot, "observed_at_s")),
        "contract_sha256": contract_sha,
        "campaign_fingerprint": campaign_fingerprint,
        "route_id": route_id,
        "attempt_id": attempt_id,
        "resident_session_id": session_id,
        "session_epoch": session_epoch,
        "program": R013_PROGRAM,
        "controller_target": controller_target,
        "triplet_sha256": dict(triplet),
        "dashboard": dict(dashboard),
        "runtime_output_registers": {
            "32": _snapshot_register(snapshot, 32),
            "33": _snapshot_register(snapshot, 33),
            "34": _snapshot_register(snapshot, 34),
        },
        "runtime_protocol": R013_MOTION_PROTOCOL,
        "runtime_revision": R013_REVISION,
        "runtime_extension_protocol": R013_RUNTIME_PROTOCOL,
        "tp_state": _snapshot_register(snapshot, 26),
        "program_running": True,
        "stationary": True,
        "safety_normal": True,
        "tcp_pose_m_rad": list(_snapshot_value(snapshot, "tcp_pose_m_rad", ())),
        "q_rad": list(_snapshot_value(snapshot, "q_rad", ())),
        "no_arm": True,
        "arm_dispatched": False,
        "trial_dispatched": False,
    }


def create_live_run(
    run_dir: Path,
    *,
    robot_host: str,
    kunwei_host: str,
    kunwei_port: int,
    controller_readback_dir: Path,
    r012_ledger: Path,
    root: Path = ROOT,
    noise_floor_n2: float = 0.01,
    resume_existing: bool = False,
    runtime_strategy_profile: Path | None = None,
    campaign_config: Path | R013BudgetedFloorConfig | None = None,
    feedforward_mode: FeedforwardMode | str | None = "on",
    preparation_role: str = "formal",
    bounded_bo_seed: Path | BoundedBOSeedConfigV1 | None = None,
    dashboard_observer: Callable[[str], Mapping[str, str]] = _observe_dashboard,
    dashboard_writer_factory: Callable[..., Any] = RemoteDashboardWriter,
    state_sampler: Callable[[str], Mapping[str, Any]] = _state_sample,
    baseline_capture: Callable[[str, int], Mapping[str, Any]] = _capture_software_baseline,
    neutral_hold_publisher: Callable[[str], Mapping[str, Any]] = _publish_neutral_hold,
    readback_loader: Callable[..., Mapping[str, Any]] | None = None,
    controller_helper: Path | None = None,
    controller_helper_sha256: str | None = None,
    runtime_observer: Callable[..., Any] | None = None,
    sleeper: Callable[[float], None] = time.sleep,
    now: Callable[[], float] = time.time,
) -> dict[str, Any]:
    """Run the complete R013 preparation, ending at READY/no-ARM.

    A fresh run creates its ledgers only after resident READY.  An explicit
    resume preserves existing hash-chained campaign/physical observations and
    refreshes only controller/session readiness receipts.
    """

    if readback_loader is None:
        from upload_ur_tp_package import resolve_live_controller_helper

        controller_helper, controller_helper_sha256 = resolve_live_controller_helper(
            controller_helper,
            controller_helper_sha256,
        )
    run_dir = Path(run_dir).resolve()
    if not isinstance(resume_existing, bool):
        raise RuntimeError("R013 resume_existing must be bool")
    feedforward_profile = FeedforwardProfile.from_value(feedforward_mode)
    if preparation_role not in {"formal", MANUAL_CANARY_ROLE}:
        raise RuntimeError("R013 preparation role differs")
    bounded_bo_seed_config = (
        bounded_bo_seed
        if isinstance(bounded_bo_seed, BoundedBOSeedConfigV1)
        else None
        if bounded_bo_seed is None
        else BoundedBOSeedConfigV1.load(Path(bounded_bo_seed))
    )
    if bounded_bo_seed_config is not None and preparation_role != "formal":
        raise RuntimeError("bounded BO preparation must use the formal preparation role")
    manual_canary_profile = (
        R013ManualCanaryPreparationProfileV1(feedforward_profile)
        if preparation_role == MANUAL_CANARY_ROLE
        else None
    )
    if manual_canary_profile is None and bounded_bo_seed_config is None:
        loaded_campaign_config = (
            campaign_config
            if isinstance(campaign_config, R013BudgetedFloorConfig)
            else load_r013_budgeted_floor_config(campaign_config)
        )
        try:
            loaded_campaign_config.require_launch_ready()
        except ValueError as exc:
            # This gate is intentionally before any dashboard, controller,
            # Kunwei, run-directory, or ledger side effect.
            raise RuntimeError(str(exc)) from exc
        campaign_config_path = (
            "<typed-live-ready-config>"
            if isinstance(campaign_config, R013BudgetedFloorConfig)
            else str(
                (DEFAULT_CONFIG_PATH if campaign_config is None else Path(campaign_config)).resolve()
            )
        )
    else:
        loaded_campaign_config = None
        campaign_config_path = (
            "<manual-canary-preparation-v1>"
            if manual_canary_profile is not None
            else "<bounded-bo-seed-v1>"
        )
    root = Path(root).resolve()
    if SCRIPT1_SOURCE.is_symlink() or not SCRIPT1_SOURCE.is_file():
        raise RuntimeError(f"Script1 source is missing: {SCRIPT1_SOURCE}")
    requested_strategy = dict(DISABLED_RUNTIME_STRATEGY)
    requested_strategy_profile_sha256: str | None = None
    if runtime_strategy_profile is not None:
        strategy_path = Path(runtime_strategy_profile).resolve()
        if not strategy_path.is_file() or strategy_path.is_symlink():
            raise RuntimeError("R013 runtime strategy profile must be a regular file")
        try:
            raw_strategy = json.loads(strategy_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise RuntimeError("R013 runtime strategy profile is unreadable") from exc
        requested_strategy = validate_runtime_strategy(raw_strategy)
        requested_strategy_profile_sha256 = _sha(strategy_path)
    if bounded_bo_seed_config is not None:
        if runtime_strategy_sha256(requested_strategy) != runtime_strategy_sha256(
            bounded_bo_seed_config.runtime_strategy
        ) and runtime_strategy_profile is not None:
            raise RuntimeError("bounded BO runtime strategy profile differs from seed receipt")
        requested_strategy = dict(bounded_bo_seed_config.runtime_strategy)
        if isinstance(bounded_bo_seed, Path):
            requested_strategy_profile_sha256 = bounded_bo_seed_config.receipt_sha256
    campaign: Campaign | None = None
    existing_seed_source: dict[str, Any] | None = None
    existing_optimizer_snapshot: dict[str, Any] | None = None
    resumed_physical_records: tuple[Any, ...] = ()
    resumed_strategy_sidecar: RuntimeStrategyReceiptLedger | None = None
    prior_session_epoch = 0
    materialized_campaign_fingerprint = None
    if resume_existing:
        if run_dir.is_symlink() or not run_dir.is_dir():
            raise RuntimeError("resumed R013 run directory must be a real directory")
        campaign = Campaign.resume(run_dir / "r013_ledger.jsonl")
        persisted_role = campaign.snapshot.get("campaign_role")
        if bounded_bo_seed_config is not None:
            expected_role = {
                "schema": "step5d.autotune-v4/r013-bounded-bo-role-v1",
                "version": 1,
                "role": BOUNDED_BO_ROLE,
                "formal_campaign_tell_exact": True,
                "launch_ready": True,
            }
            if persisted_role != expected_role:
                raise RuntimeError("resumed R013 bounded BO role differs")
            bounded_bo_seed_config.verify_profile(
                campaign.bounded_bo_profile or {}, bounded_bo_seed_config
            )
        elif manual_canary_profile is not None:
            if persisted_role != manual_canary_profile.as_dict():
                raise RuntimeError("resumed R013 manual canary role or mode differs")
            if campaign.completion_policy.policy == "budgeted_floor_v1":
                raise RuntimeError("manual canary cannot resume a formal campaign")
        elif persisted_role is not None:
            raise RuntimeError("formal preparation cannot resume a manual canary")
        if loaded_campaign_config is not None and campaign.completion_policy.policy == "budgeted_floor_v1":
            try:
                require_campaign_config_binding(
                    config=loaded_campaign_config,
                    snapshot=campaign.snapshot,
                )
            except ValueError as exc:
                raise RuntimeError(str(exc)) from exc
        if runtime_strategy_profile is not None and (
            runtime_strategy_sha256(requested_strategy)
            != campaign.runtime_strategy_sha256
        ):
            raise RuntimeError("resumed R013 runtime strategy profile differs from its ledger")
        requested_strategy = dict(campaign.runtime_strategy)
        if manual_canary_profile is not None:
            controller_receipt_path = run_dir / "controller_receipt.json"
            if controller_receipt_path.is_symlink() or not controller_receipt_path.is_file():
                raise RuntimeError("manual canary persisted controller source receipt is missing")
            try:
                controller_receipt = json.loads(
                    controller_receipt_path.read_text(encoding="utf-8")
                )
            except (OSError, UnicodeError, json.JSONDecodeError) as exc:
                raise RuntimeError("manual canary controller source receipt is unreadable") from exc
            if not isinstance(controller_receipt, Mapping):
                raise RuntimeError("manual canary controller source receipt is invalid")
            controller_triplet = _controller_triplet_from_receipt(controller_receipt)
            _verify_local_controller_triplet(root, triplet=controller_triplet)
            _contract, _contract_sha, _contract_fingerprint, eoat_sha = _r013_identity()
            expected_canary_fingerprint = _materialize_manual_canary_fingerprint(
                triplet=controller_triplet,
                runtime_strategy_sha256_value=runtime_strategy_sha256(requested_strategy),
                eoat_identity_sha256=eoat_sha,
                script1_source_sha256=_sha(SCRIPT1_SOURCE),
                feedforward_profile=manual_canary_profile.feedforward_profile,
            )
            if expected_canary_fingerprint != campaign.campaign_fingerprint:
                raise RuntimeError("resumed manual canary fingerprint differs")
        if loaded_campaign_config is not None and campaign.completion_policy.policy == "budgeted_floor_v1":
            controller_receipt_path = run_dir / "controller_receipt.json"
            if controller_receipt_path.is_symlink() or not controller_receipt_path.is_file():
                raise RuntimeError("R013 persisted controller source receipt is missing")
            try:
                controller_receipt = json.loads(
                    controller_receipt_path.read_text(encoding="utf-8")
                )
            except (OSError, UnicodeError, json.JSONDecodeError) as exc:
                raise RuntimeError("R013 persisted controller source receipt is unreadable") from exc
            if not isinstance(controller_receipt, Mapping):
                raise RuntimeError("R013 persisted controller source receipt is invalid")
            controller_triplet = _controller_triplet_from_receipt(controller_receipt)
            _verify_local_controller_triplet(root, triplet=controller_triplet)
            _contract, _contract_sha, _contract_fingerprint, eoat_sha = _r013_identity()
            materialized_campaign_fingerprint = materialize_campaign_fingerprint(
                loaded_campaign_config,
                runtime_strategy_sha256_value=runtime_strategy_sha256(requested_strategy),
                controller_triplet_sha256=controller_triplet,
                eoat_identity_sha256=eoat_sha,
                script1_source_sha256=_sha(SCRIPT1_SOURCE),
                feedforward_profile=(
                    None
                    if feedforward_profile.mode is FeedforwardMode.ON
                    and all(
                        value == LEGACY_PROFILE_IDENTITY
                        for value in (
                            campaign.campaign_fingerprint.feedforward_profile_identity,
                            campaign.campaign_fingerprint.motion_admission_profile_identity,
                            campaign.campaign_fingerprint.baseline_transition_profile_identity,
                            campaign.campaign_fingerprint.baseline_residual_policy_identity,
                        )
                    )
                    else feedforward_profile
                ),
            )
            if materialized_campaign_fingerprint != campaign.campaign_fingerprint:
                raise RuntimeError("resumed R013 materialized campaign fingerprint differs")
            if (
                campaign.snapshot.get("materialized_campaign_fingerprint")
                != materialized_campaign_fingerprint.as_dict()
                or campaign.snapshot.get("materialized_campaign_fingerprint_sha256")
                != materialized_campaign_fingerprint.sha256
            ):
                raise RuntimeError("resumed R013 materialized fingerprint receipt differs")
        if runtime_strategy_profile is None:
            prior_preparation_path = run_dir / "r013_live_preparation.json"
            if prior_preparation_path.is_file():
                prior_preparation = json.loads(
                    prior_preparation_path.read_text(encoding="utf-8")
                )
                prior_profile_sha = prior_preparation.get(
                    "runtime_strategy_profile_sha256"
                )
                if prior_profile_sha is not None and (
                    type(prior_profile_sha) is not str
                    or len(prior_profile_sha) != 64
                    or any(c not in "0123456789abcdef" for c in prior_profile_sha)
                ):
                    raise RuntimeError("resumed R013 runtime strategy profile identity differs")
                requested_strategy_profile_sha256 = prior_profile_sha
        if campaign.runtime_strategy.get("enabled") is True:
            resumed_strategy_sidecar = RuntimeStrategyReceiptLedger(
                run_dir / "r013-runtime-strategy-receipts.jsonl",
                campaign_id=str(campaign.ledger.header["campaign_id"]),
                run_id=str(campaign.ledger.header["run_id"]),
                attempt_id=str(campaign.ledger.header["attempt_id"]),
                strategy=campaign.runtime_strategy,
                must_exist=True,
            )
            resumed_strategy_sidecar.validate_campaign_records(campaign.ledger.records)
        if campaign.in_flight is not None:
            raise RuntimeError("resumed R013 campaign has an in-flight dispatch")
        if campaign.stopped or campaign.target_achieved or campaign.strategy_canary_terminal:
            raise RuntimeError("resumed R013 campaign is terminal")
        if not (run_dir / "r006-physical-observations.jsonl").is_file():
            raise RuntimeError("resumed R013 physical ledger is missing")
        for directory in (run_dir / "authority", run_dir / "r006-queue"):
            if directory.is_symlink() or not directory.is_dir():
                raise RuntimeError("resumed R013 owner directory is invalid")
        if any((run_dir / "r006-queue").iterdir()):
            raise RuntimeError("resumed R013 owner queue is not empty")
        authority_path = run_dir / "authority" / "owner-authority.json"
        if not authority_path.is_file():
            raise RuntimeError("resumed R013 owner authority is missing")
        from step5d_bridge_authority import require_revoked

        require_revoked(
            run_dir / "authority",
            attempt_id=str(campaign.ledger.header["attempt_id"]),
            minimum_sequence=1,
        )
        previous_launch = json.loads(
            (run_dir / "launch_context.json").read_text(encoding="utf-8")
        )
        prior_session_epoch = int(previous_launch["session_epoch"])
        existing_seed_source = json.loads(
            (run_dir / "r012_seed_source.json").read_text(encoding="utf-8")
        )
        existing_optimizer_snapshot = json.loads(
            (run_dir / "optimizer_snapshot.json").read_text(encoding="utf-8")
        )
        if not isinstance(existing_seed_source, dict):
            raise RuntimeError("resumed R013 seed source is invalid")
        if existing_optimizer_snapshot != campaign.ledger.header["optimizer_snapshot"]:
            raise RuntimeError("resumed R013 optimizer snapshot differs from its ledger")
        if select_seed_template(existing_seed_source) != campaign.snapshot["seed_template"]:
            raise RuntimeError("resumed R013 seed source differs from its campaign template")
        from .live_owner import (
            _open_physical_ledger,
            _validate_strategy_physical_bindings,
        )

        _contract, _contract_sha, campaign_fingerprint, eoat_sha = _r013_identity()
        resumed_physical_records = tuple(
            _open_physical_ledger(
                run_dir / "r006-physical-observations.jsonl",
                campaign_fingerprint=campaign_fingerprint,
                eoat_sha256=eoat_sha,
            ).records
        )
        if resumed_strategy_sidecar is not None:
            _validate_strategy_physical_bindings(
                strategy_sidecar=resumed_strategy_sidecar,
                campaign=campaign,
                physical_records=resumed_physical_records,
            )
    elif run_dir.exists():
        raise RuntimeError("fresh R013 run directory must not already exist")
    r013_play_sent = False
    r013_writer: Any = None
    try:
        current = dict(dashboard_observer(robot_host))
        _require_remote_normal(current)
        identity_nonce_ns = time.time_ns()
        epoch = max(
            int(identity_nonce_ns // 1_000_000_000),
            prior_session_epoch + 1,
            max((int(record.epoch) for record in resumed_physical_records), default=0) + 1,
        )
        if not 1 <= epoch <= INT32_MAX:
            raise RuntimeError("current Unix time cannot be represented by the TP INT32 epoch")
        route_id = f"r006-r013-{identity_nonce_ns}"
        attempt_id = (
            str(campaign.ledger.header["attempt_id"])
            if campaign is not None
            else f"r006-r013-attempt-{identity_nonce_ns}"
        )
        session_id = f"r006-r013-resident-{identity_nonce_ns}"

        if _dashboard_running(current):
            current_target = _loaded_target(current)
            stopper = dashboard_writer_factory(
                robot_host, load_target=current_target, timeout_s=3.0
            )
            stopper.write("stop")
            _wait_dashboard(
                robot_host,
                current_target,
                running=False,
                observer=dashboard_observer,
                sleeper=sleeper,
            )

        script1_writer = dashboard_writer_factory(
            robot_host, load_target=SCRIPT1_TARGET, timeout_s=3.0
        )
        script1_writer.write(f"load {SCRIPT1_TARGET}")
        _wait_dashboard(
            robot_host,
            SCRIPT1_TARGET,
            running=False,
            observer=dashboard_observer,
            sleeper=sleeper,
        )
        script1_writer.write("play")
        script1_dashboard = _wait_dashboard(
            robot_host,
            SCRIPT1_TARGET,
            running=False,
            observer=dashboard_observer,
            sleeper=sleeper,
        )
        home = _wait_fixed_home(
            robot_host,
            sampler=state_sampler,
            sleeper=sleeper,
        )
        script1_observed_at = float(home["observed_at_s"])
        script1_receipt = {
            "schema": "step5d.autotune-v4/r013-script1-receipt-v3",
            "observation_source": "fresh_script1_dashboard_and_rtde",
            "program": SCRIPT1_TARGET,
            "script_sha256": _sha(SCRIPT1_SOURCE),
            "observed_at_s": script1_observed_at,
            "final_pose": list(home["actual_TCP_pose"]),
            "final_q": list(home["actual_q"]),
            "runtime_state": home.get("runtime_state"),
            "stationary": True,
            "safety_mode": "NORMAL",
            "dashboard": script1_dashboard,
            "eoat_identity_sha256": _r013_identity()[3],
        }
        script1_receipt["receipt_sha256"] = _receipt_digest(script1_receipt)

        if readback_loader is None:
            controller_helper, controller_helper_sha256 = resolve_live_controller_helper(
                controller_helper,
                controller_helper_sha256,
            )
            controller_readback = _capture_controller_triplet(
                root,
                Path(controller_readback_dir).resolve(),
                controller_helper=controller_helper,
                controller_helper_sha256=controller_helper_sha256,
            )
        else:
            controller_readback = dict(
                readback_loader(
                    root,
                    Path(controller_readback_dir).resolve(),
                    observed_at_s=float(now()),
                )
            )
        triplet = dict(controller_readback["triplet_sha256"])
        if set(triplet) != {"script", "txt", "urp"}:
            raise RuntimeError("R013 controller read-back triplet is incomplete")
        if bounded_bo_seed_config is not None:
            if triplet != dict(bounded_bo_seed_config.controller_triplet_sha256):
                raise RuntimeError("bounded BO controller read-back triplet differs from seed receipt")
            current_source_closure = r013_host_source_closure(source_root=root)
            if current_source_closure.get("sha256") != bounded_bo_seed_config.source_closure_sha256:
                raise RuntimeError("bounded BO host source closure differs from seed receipt")

        baseline = _validate_fresh_baseline(
            baseline_capture(kunwei_host, int(kunwei_port)),
            after_s=script1_observed_at,
        )
        contract, contract_sha, campaign_fingerprint, eoat_sha = _r013_identity()
        if bounded_bo_seed_config is not None:
            materialized_campaign_fingerprint = _materialize_bounded_bo_fingerprint(
                triplet=triplet,
                runtime_strategy_sha256_value=runtime_strategy_sha256(requested_strategy),
                eoat_identity_sha256=eoat_sha,
                script1_source_sha256=_sha(SCRIPT1_SOURCE),
                feedforward_profile=feedforward_profile,
            )
        elif manual_canary_profile is not None:
            materialized_campaign_fingerprint = _materialize_manual_canary_fingerprint(
                triplet=triplet,
                runtime_strategy_sha256_value=runtime_strategy_sha256(requested_strategy),
                eoat_identity_sha256=eoat_sha,
                script1_source_sha256=_sha(SCRIPT1_SOURCE),
                feedforward_profile=manual_canary_profile.feedforward_profile,
            )
        else:
            assert loaded_campaign_config is not None
            materialized_campaign_fingerprint = materialize_campaign_fingerprint(
                loaded_campaign_config,
                runtime_strategy_sha256_value=runtime_strategy_sha256(requested_strategy),
                controller_triplet_sha256=triplet,
                eoat_identity_sha256=eoat_sha,
                script1_source_sha256=_sha(SCRIPT1_SOURCE),
                feedforward_profile=feedforward_profile,
            )
        baseline["campaign_fingerprint"] = campaign_fingerprint
        hi, lo = _runtime_identity_limbs(contract_sha, campaign_fingerprint)
        neutral_hold = dict(neutral_hold_publisher(robot_host))
        if (
            neutral_hold.get("schema") != "step5d.autotune-v4/r013-neutral-hold-v1"
            or neutral_hold.get("observation_source") != "fresh_rtde_input_write"
            or neutral_hold.get("layout_tag") != 606.0
            or neutral_hold.get("session_command") != "HOLD"
            or neutral_hold.get("session_command_value") != 0
            or neutral_hold.get("arm_dispatched") is not False
            or neutral_hold.get("trial_dispatched") is not False
        ):
            raise RuntimeError("R013 neutral HOLD receipt is incomplete")

        r013_writer = dashboard_writer_factory(
            robot_host, load_target=R013_TARGET, timeout_s=3.0
        )
        r013_writer.write(f"load {R013_TARGET}")
        _wait_dashboard(
            robot_host,
            R013_TARGET,
            running=False,
            observer=dashboard_observer,
            sleeper=sleeper,
        )
        try:
            play_outcome = r013_writer.write("play")
            r013_play_sent = bool(getattr(play_outcome, "command_sent", True))
        except Exception as exc:
            r013_play_sent = bool(getattr(exc, "command_sent", False))
            raise
        resident_dashboard = _wait_dashboard(
            robot_host,
            R013_TARGET,
            running=True,
            observer=dashboard_observer,
            sleeper=sleeper,
        )
        observe_runtime = runtime_observer or _observe_r013_ready
        controller_snapshot = observe_runtime(
            robot_host,
            resident_dashboard,
            sleeper=sleeper,
        )
        snapshot = observe_runtime(
            robot_host,
            resident_dashboard,
            sleeper=sleeper,
        )
        controller_observed_at = float(
            _snapshot_value(controller_snapshot, "observed_at_s")
        )
        ready_evidence = _fresh_resident_evidence(
            snapshot=snapshot,
            dashboard=resident_dashboard,
            contract_sha=contract_sha,
            campaign_fingerprint=campaign_fingerprint,
            route_id=route_id,
            attempt_id=attempt_id,
            session_id=session_id,
            session_epoch=epoch,
            triplet=triplet,
            controller_target=R013_TARGET,
        )
        resident_observed_at = float(ready_evidence["observed_at_s"])
        if resident_observed_at <= controller_observed_at:
            raise RuntimeError("R013 runtime READY observation is not newer than controller state")

        # The compatibility carrier remains required by the mature R006
        # adapter.  Its large limbs are a projection, never a claim about
        # physical registers; the physical tuple is only in ready_evidence.
        projection = {
            "kind": "r013_legacy_runtime_limbs",
            "physically_read": False,
            "source_evidence": "resident_ready_evidence.json",
            "runtime_digest_hi": hi,
            "runtime_digest_lo": lo,
        }
        controller_state = {
            "actual_tcp_speed_m_s_rad_s": list(_snapshot_value(controller_snapshot, "tcp_speed_m_s_rad_s", ())),
            "payload_kg": float(_snapshot_value(controller_snapshot, "payload_kg")),
            "payload_cog_m": list(_snapshot_value(controller_snapshot, "payload_cog_m", ())),
            "tcp_offset_m_rad": list(_snapshot_value(controller_snapshot, "tcp_offset_m_rad", ())),
        }
        controller_receipt = {
            "schema": "step5d.autotune-v4/r013-controller-receipt-v3",
            "observation_source": "fresh_controller_readback_and_rtde",
            "program": R013_PROGRAM,
            "controller_target": R013_TARGET,
            **{f"{role}_sha256": digest for role, digest in triplet.items()},
            "controller_readback": controller_readback,
            "observed_at_s": controller_observed_at,
            "runtime_protocol": R013_MOTION_PROTOCOL,
            "runtime_digest_hi": hi,
            "runtime_digest_lo": lo,
            "runtime_identity_projection": projection,
            "eoat_identity_sha256": eoat_sha,
            "readback": controller_state,
            "safety_mode": "NORMAL",
            "stationary": True,
            "route_id": route_id,
        }
        controller_receipt["receipt_sha256"] = _receipt_digest(controller_receipt)
        runtime_evidence = {
            "schema": "step5d.autotune-v4/r013-runtime-evidence-v3",
            "observation_source": "resident_ready_evidence.json",
            "program": R013_PROGRAM,
            "script_sha256": triplet["script"],
            "runtime_protocol": R013_MOTION_PROTOCOL,
            "runtime_digest_hi": hi,
            "runtime_digest_lo": lo,
            "runtime_identity_projection": projection,
            "session_epoch": epoch,
            "resident_session_id": session_id,
            "program_running": True,
            "uninterrupted": True,
            "observed_at_s": resident_observed_at,
        }
        thresholds = _build_threshold_receipt(
            contract_sha=contract_sha,
            campaign_fingerprint=campaign_fingerprint,
            issued_at_s=float(baseline["observed_at_s"]),
        )

        # Only now create fresh append-only host state, or cold-validate the
        # explicitly resumed stores before refreshing readiness receipts.
        if campaign is None:
            run_dir.mkdir(parents=False, exist_ok=False)
        authority_root = run_dir / "authority"
        queue_root = run_dir / "r006-queue"
        if campaign is None:
            authority_root.mkdir()
            queue_root.mkdir()
        physical_ledger = run_dir / "r006-physical-observations.jsonl"
        if campaign is None:
            ObservationLedger(
                physical_ledger,
                campaign_fingerprint=campaign_fingerprint,
                eoat_sha256=eoat_sha,
            )
        else:
            # Existing live rows use the R008RAW2 slim-json + binary sidecar
            # contract.  Resume must use the same bounded cold verifier as the
            # physical owner, not the legacy JSON-samples verifier.
            from .live_owner import _open_physical_ledger

            _open_physical_ledger(
                physical_ledger,
                campaign_fingerprint=campaign_fingerprint,
                eoat_sha256=eoat_sha,
            )
        campaign_ledger = run_dir / "r013_ledger.jsonl"
        if campaign is None:
            seed_source = r012_seed_source_from_ledger(Path(r012_ledger))
            campaign_id = f"r013-live-{uuid.uuid4().hex[:16]}"
            run_id = f"r013-run-{uuid.uuid4().hex[:16]}"
            if bounded_bo_seed_config is not None:
                campaign = Campaign.create(
                    campaign_ledger,
                    campaign_id=campaign_id,
                    run_id=run_id,
                    attempt_id=attempt_id,
                    noise_floor_n2=noise_floor_n2,
                    r012_seed_source=seed_source,
                    seed_template=bounded_bo_seed_config.candidate,
                    runtime_strategy=requested_strategy,
                    handoff_policy=HandoffPolicy(),
                    campaign_fingerprint=materialized_campaign_fingerprint,
                    bounded_bo_profile=bounded_bo_seed_config.campaign_profile(),
                    campaign_role={
                        "schema": "step5d.autotune-v4/r013-bounded-bo-role-v1",
                        "version": 1,
                        "role": BOUNDED_BO_ROLE,
                        "formal_campaign_tell_exact": True,
                        "launch_ready": True,
                    },
                )
            elif manual_canary_profile is not None:
                campaign = Campaign.create(
                    campaign_ledger,
                    campaign_id=campaign_id,
                    run_id=run_id,
                    attempt_id=attempt_id,
                    noise_floor_n2=noise_floor_n2,
                    r012_seed_source=seed_source,
                    runtime_strategy=requested_strategy,
                    handoff_policy=HandoffPolicy(),
                    campaign_fingerprint=materialized_campaign_fingerprint,
                    campaign_role=manual_canary_profile.as_dict(),
                )
            else:
                assert loaded_campaign_config is not None
                campaign = Campaign.create(
                    campaign_ledger,
                    campaign_id=campaign_id,
                    run_id=run_id,
                    attempt_id=attempt_id,
                    noise_floor_n2=noise_floor_n2,
                    r012_seed_source=seed_source,
                    runtime_strategy=requested_strategy,
                    completion_policy=loaded_campaign_config.completion_policy,
                    handoff_policy=loaded_campaign_config.handoff_policy,
                    campaign_fingerprint=materialized_campaign_fingerprint,
                    budgeted_floor_config=loaded_campaign_config.as_dict(),
                )
            if campaign.runtime_strategy.get("enabled") is True:
                campaign.install_strategy_canary_plan(
                    strategy_canary_plan(
                        runtime_strategy_sha256_value=campaign.runtime_strategy_sha256
                    )
                )
        else:
            assert existing_seed_source is not None
            seed_source = existing_seed_source
            campaign_id = str(campaign.ledger.header["campaign_id"])
            run_id = str(campaign.ledger.header["run_id"])
            if not math.isclose(
                float(campaign.snapshot["noise_floor_n2"]),
                float(noise_floor_n2),
                rel_tol=0.0,
                abs_tol=0.0,
            ):
                raise RuntimeError("resumed R013 noise snapshot differs")
        launch = {
            "schema": "step5d.autotune-v4/r013-launch-context-v2",
            "campaign_id": campaign_id,
            "run_id": run_id,
            "campaign_fingerprint": campaign_fingerprint,
            "contract_sha256": contract_sha,
            "program": R013_PROGRAM,
            "r013_extension_protocol": R013_RUNTIME_PROTOCOL,
            "route_id": route_id,
            "attempt_id": attempt_id,
            "session_id": session_id,
            "session_epoch": epoch,
            "runtime_protocol": R013_MOTION_PROTOCOL,
            "runtime_digest_hi": hi,
            "runtime_digest_lo": lo,
            "runtime_identity_projection": projection,
            "triplet": triplet,
            "resident_ready_evidence": str(run_dir / "resident_ready_evidence.json"),
            "software_baseline_n": baseline["mean_wrench_n_nm"],
            "runtime_strategy_sha256": campaign.runtime_strategy_sha256,
            "runtime_strategy_enabled": bool(campaign.runtime_strategy["enabled"]),
        }
        ready = {
            "schema": "step5d.autotune-v4/r013-live-owner-ready-v2",
            "status": "resident_ready_no_arm",
            "program": R013_PROGRAM,
            "controller_target": R013_TARGET,
            "ledger_path": str(physical_ledger),
            "queue_root": str(queue_root),
            "authority_root": str(authority_root),
            "route_id": route_id,
            "attempt_id": attempt_id,
            "resident_session_id": session_id,
            "session_epoch": epoch,
            "contract_sha256": contract_sha,
            "campaign_fingerprint": campaign_fingerprint,
            "runtime_protocol": R013_MOTION_PROTOCOL,
            "runtime_digest_hi": hi,
            "runtime_digest_lo": lo,
            "runtime_identity_projection": projection,
            "triplet": triplet,
            "resident_ready_evidence": str(run_dir / "resident_ready_evidence.json"),
            "observed_at_s": resident_observed_at,
            "arm_dispatched": False,
            "trial_dispatched": False,
            "runtime_strategy_sha256": campaign.runtime_strategy_sha256,
            "runtime_strategy_enabled": bool(campaign.runtime_strategy["enabled"]),
        }
        preparation = {
            "schema": "step5d.autotune-v4/r013-live-preparation-v2",
            "campaign_id": campaign_id,
            "run_id": run_id,
            "attempt_id": attempt_id,
            "training_lineage": "fresh_r013_only",
            "noise_floor_n2": noise_floor_n2,
            "model_dimensions": 6,
            "q": 1,
            "candidate_pool_size": 128,
            "r012_seed_rows_exported": 0,
            "r012_seed_source": str(Path(r012_ledger).resolve()),
            "fresh_campaign_ledger": str(campaign_ledger),
            "fresh_physical_ledger": str(physical_ledger),
            "package_triplet": triplet,
            "controller_readback_verified": True,
            "resident_ready_evidence": str(run_dir / "resident_ready_evidence.json"),
            "preparation_only": True,
            "arm_dispatched": False,
            "trial_dispatched": False,
            "resume_existing_campaign": resume_existing,
            "runtime_strategy": dict(campaign.runtime_strategy),
            "runtime_strategy_sha256": campaign.runtime_strategy_sha256,
            "runtime_strategy_profile_sha256": (
                requested_strategy_profile_sha256
                if bounded_bo_seed_config is None
                else bounded_bo_seed_config.runtime_strategy_sha256
            ),
            "campaign_config": campaign_config_path,
            "completion_policy": campaign.completion_policy.as_dict(),
            "novel_exact_candidate_target": campaign.completion_policy.novel_exact_candidate_target,
            "target_checkpoint_only": campaign.completion_policy.checkpoint_only_target,
            "target_mae_n": campaign.completion_policy.target_mae_n,
            "handoff_policy": campaign.handoff_policy.as_dict(),
            "floor_discovery_policy": campaign.snapshot.get("floor_discovery_policy"),
            "floor_discovery": campaign.floor_discovery_status,
            "campaign_fingerprint_identity": campaign.campaign_fingerprint.as_dict(),
            "campaign_fingerprint_sha256": campaign.campaign_fingerprint.sha256,
            "campaign_fingerprint_template": (
                None
                if manual_canary_profile is not None or bounded_bo_seed_config is not None
                else loaded_campaign_config.campaign_fingerprint.as_dict()
            ),
            "materialized_campaign_fingerprint": campaign.campaign_fingerprint.as_dict(),
            "materialized_campaign_fingerprint_sha256": campaign.campaign_fingerprint.sha256,
            "budgeted_floor_config": (
                None
                if manual_canary_profile is not None or bounded_bo_seed_config is not None
                else loaded_campaign_config.as_dict()
            ),
            "bounded_bo": (
                None
                if bounded_bo_seed_config is None
                else {
                    **bounded_bo_seed_config.campaign_profile(),
                    "seed_receipt": bounded_bo_seed_config.as_dict(),
                }
            ),
            "strategy_canary": dict(campaign.strategy_canary_summary),
            "soft_tube": {"mode": "active", "semi_axes_m": list(R012_SOFT_CBF_AXES_M)},
            "hard_tube": {
                "enabled": True,
                "independent": True,
                "checked_first": True,
                "axes_m": list(R012_HARD_TUBE_AXES_M),
            },
        }
        if manual_canary_profile is not None:
            preparation.update(
                {
                    "preparation_role": manual_canary_profile.role,
                    "manual_canary_profile": manual_canary_profile.as_dict(),
                    "formal_campaign_tell_exact": False,
                    "launch_ready": False,
                }
            )
        elif bounded_bo_seed_config is not None:
            preparation.update(
                {
                    "preparation_role": BOUNDED_BO_ROLE,
                    "bounded_bo_seed_receipt_sha256": bounded_bo_seed_config.receipt_sha256,
                    "formal_campaign_tell_exact": True,
                    "launch_ready": True,
                }
            )
        refreshed_artifacts = {
            "script1_receipt.json": script1_receipt,
            "controller_receipt.json": controller_receipt,
            "software_baseline.json": baseline,
            "neutral_hold_receipt.json": neutral_hold,
            "thresholds_receipt.json": thresholds,
            "runtime_evidence.json": runtime_evidence,
            "resident_ready_evidence.json": ready_evidence,
            "r013_live_owner_ready.json": ready,
            "launch_context.json": launch,
            "r013_live_preparation.json": preparation,
        }
        if not resume_existing:
            refreshed_artifacts.update(
                {
                    "r012_seed_source.json": seed_source,
                    "optimizer_snapshot.json": campaign.ledger.header["optimizer_snapshot"],
                    "runtime_strategy.json": dict(campaign.runtime_strategy),
                }
            )
            if bounded_bo_seed_config is not None:
                refreshed_artifacts["bounded_bo_seed.json"] = bounded_bo_seed_config.as_dict()
        for name, value in refreshed_artifacts.items():
            _write_json(run_dir / name, value)
        return preparation
    except Exception:
        if r013_play_sent and r013_writer is not None:
            try:
                r013_writer.write("stop")
            except Exception:
                pass
        raise


def _noise_floor_arg(value: str) -> float:
    try:
        parsed = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("noise floor must be a number") from exc
    if not math.isfinite(parsed) or parsed <= 0.0:
        raise argparse.ArgumentTypeError("noise floor must be positive and finite")
    return parsed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--controller-readback-dir", type=Path, required=True)
    parser.add_argument("--controller-helper", type=Path)
    parser.add_argument("--controller-helper-sha256")
    parser.add_argument("--r012-ledger", type=Path, required=True)
    parser.add_argument("--robot-host", required=True)
    parser.add_argument("--kunwei-host", required=True)
    parser.add_argument("--kunwei-port", type=int, required=True)
    parser.add_argument("--resume-existing", action="store_true")
    parser.add_argument(
        "--feedforward",
        choices=("on", "off"),
        default="on",
        help="Immutable run-level PATH velocity feedforward mode.",
    )
    parser.add_argument(
        "--preparation-role",
        choices=("formal", MANUAL_CANARY_ROLE),
        default="formal",
        help="Formal floor preparation or one non-formal fixed-candidate manual canary.",
    )
    parser.add_argument("--runtime-strategy-profile", type=Path)
    parser.add_argument(
        "--bounded-bo-seed",
        type=Path,
        help="Immutable bounded_bo_v1 seed/control-law receipt.",
    )
    parser.add_argument(
        "--campaign-config",
        type=Path,
        default=DEFAULT_CONFIG_PATH,
    )
    parser.add_argument(
        "--noise-floor-n2",
        type=_noise_floor_arg,
        default=0.01,
    )
    args = parser.parse_args(argv)
    result = create_live_run(
        args.run_dir,
        robot_host=args.robot_host,
        kunwei_host=args.kunwei_host,
        kunwei_port=args.kunwei_port,
        controller_readback_dir=args.controller_readback_dir,
        controller_helper=args.controller_helper,
        controller_helper_sha256=args.controller_helper_sha256,
        r012_ledger=args.r012_ledger,
        noise_floor_n2=args.noise_floor_n2,
        resume_existing=args.resume_existing,
        runtime_strategy_profile=args.runtime_strategy_profile,
        campaign_config=args.campaign_config,
        feedforward_mode=args.feedforward,
        preparation_role=args.preparation_role,
        bounded_bo_seed=args.bounded_bo_seed,
    )
    print(json.dumps(result, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["create_live_run", "main"]
