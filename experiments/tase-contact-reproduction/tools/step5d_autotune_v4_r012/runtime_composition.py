"""Static R012 composition and fail-closed offline readiness report."""

from __future__ import annotations

import copy
import json
import socket
from pathlib import Path
from typing import Any, Mapping

from .common import R012ValueError, json_tree
from .descriptor import R012_MOTION_PROTOCOL, R012_REVISION, R012_RUNTIME_PROTOCOL, build_descriptor


RUNTIME_COMPOSITION_SCHEMA = "step5d.autotune-v4/r012-runtime-composition-v1"
READINESS_REPORT_SCHEMA = "step5d.autotune-v4/r012-readiness-report-v1"

BOUNDED_TUNING_BLOCKERS = (
    "controller_package_structurally_valid",
    "controller_readback_byte_equal",
    "runtime_protocol_observed",
    "home_stationary_safety_normal",
    "rtde_and_kunwei_fresh",
    "cbf_timing_physical_guards",
)


class R012RuntimeCompositionError(R012ValueError):
    """Static composition or activation request is invalid."""


_COMPOSITION = {
    "schema": RUNTIME_COMPOSITION_SCHEMA,
    "release_scope": "offline_candidate_live_blocked",
    "lifecycle": [
        "fresh_ledger",
        "three_reference_exact_repeats",
        "confirmed_incumbent",
        "r012_qlognei_q1_ask",
        "serial_dispatch",
        "w9c_contact_entry",
        "path_nominal_controller",
        "path_error_cbf_tube",
        "exact_completion_or_sequence_acked_censor",
        "safe_home_closure",
        "typed_ledger_update",
        "gp_noise_incumbent_update",
        "scheduler",
    ],
    "production_optimizer": "r012_owned_single_task_gp_qlognei",
    "gp": {
        "dimensions": [
            "log2_force_p_over_d",
            "log2_force_damping",
            "log2_normal_filter_tau_s",
            "log2_orientation_ko",
            "log2_motion_kp",
        ],
        "q": 1,
        "fixed_noise_train_Yvar": True,
        "repeated_rows_preserved": True,
        "censored_rows_trainable": False,
    },
    "theory_shadow": {"implementation": "async_ts", "production_state": "separate", "ledger_authority": False, "execution_authority": False},
    "censored_protocol": {
        "denominator_bins": 550,
        "bin_width_s": 0.1,
        "guard_bins": 55,
        "kappa": 2.0,
        "trigger": "causal_prefix_mean",
        "stored_lower_bound": "sum_over_550",
        "mode": "active_novel_bo_only",
        "sequence_matched_ack": True,
        "input_register": 35,
        "ack_register": 36,
        "terminal_reason_register": 28,
        "reason43_subtype_register": 35,
        "normal_completion_terminal_reason": 0,
        "normal_completion_reason43_subtype": 0,
    },
    "safety_filter": {
        "scope": "2-D_PATH_error_frame",
        "dt_s": 0.002,
        "axes_m": [0.025, 0.015],
        "tightened_axes_m": [0.022, 0.012],
        "alpha_s_inv": 2.0,
        "engage_deadband": 0.67,
        "hard_safety_independent": True,
        "force_cbf_claim": False,
    },
    "campaign_bound": {"runtime_s": 36000, "attempts": 320, "plateau_stop": False},
    "stars": {"sidecar": "offline_replay", "admission": "explicit_batch_idle_only", "campaign_member": False},
    "activation": {
        "network": False,
        "upload": False,
        "readback": False,
        "load": False,
        "play": False,
        "bridge": False,
        "live": False,
        "current_pointer_switch": False,
    },
}


def runtime_composition_manifest() -> dict[str, Any]:
    return copy.deepcopy(_COMPOSITION)


def validate_runtime_composition(value: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(value, Mapping) or json_tree(value) != _COMPOSITION:
        raise R012RuntimeCompositionError("R012 runtime composition differs")
    return json_tree(value)


def build_offline_composition(*, campaign_id: str, run_id: str, attempt_id: str):
    from .scheduler import ConfirmedIncumbentScheduler

    return ConfirmedIncumbentScheduler(campaign_id=campaign_id, run_id=run_id, attempt_id=attempt_id)


def _live_host_module_present() -> bool:
    try:
        from . import live_host  # noqa: F401

        return True
    except ImportError:
        return False


def build_readiness_report(snapshot: Any, *, evidence: Mapping[str, Any] | None = None) -> dict[str, Any]:
    if not hasattr(snapshot, "as_dict") or not hasattr(snapshot, "behavior_config"):
        raise R012RuntimeCompositionError("readiness requires a validated R012 live snapshot")
    boundary = snapshot.behavior_config.runtime.get("activation")
    if not isinstance(boundary, Mapping) or any(boundary.values()):
        raise R012RuntimeCompositionError("R012 offline boundary is not fail-closed")
    supplied = dict(evidence or {})
    host_wired = _live_host_module_present()
    gates = {name: supplied.get(name) is True for name in BOUNDED_TUNING_BLOCKERS}
    # Offline evidence remains useful context, but it is not a live identity.
    gates.update({
        "production_gp_fit": supplied.get("production_gp_fit") is True,
        "historical_gp_calibration": supplied.get("historical_gp_calibration") is True,
        "early_end_backtest": supplied.get("early_end_backtest") is True,
        "snapshot_and_focused_tests": supplied.get("snapshot_and_focused_tests") is True,
    })
    blockers = [key for key in BOUNDED_TUNING_BLOCKERS if not gates[key]]
    blockers.extend(["current_pointer_switch_forbidden", "theory_shadow_has_no_execution_authority"])
    offline_ready = all(gates[key] for key in ("production_gp_fit", "historical_gp_calibration", "early_end_backtest", "snapshot_and_focused_tests"))
    controller_delivery_ready = gates["controller_package_structurally_valid"]
    bench_launch_ready = controller_delivery_ready and gates["controller_readback_byte_equal"] and gates["cbf_timing_physical_guards"]
    live_ready = all(gates[key] for key in BOUNDED_TUNING_BLOCKERS)
    report = {
        "schema": READINESS_REPORT_SCHEMA,
        "campaign_id": snapshot.campaign_id,
        "run_id": snapshot.run_id,
        "attempt_id": snapshot.attempt_id,
        "runtime_composition": runtime_composition_manifest(),
        "gates": gates,
        "offline_analysis_ready": offline_ready,
        "patch_ready": offline_ready,
        "controller_delivery_ready": controller_delivery_ready,
        "bench_launch_ready": bench_launch_ready,
        "bounded_tuning_ready": live_ready,
        "live_ready": live_ready,
        "release_live_ready": False,
        "bo_dispatch_allowed": live_ready,
        "blockers": blockers,
        "host_modules_present": host_wired,
    }
    return report


def require_live_ready(report: Mapping[str, Any]) -> None:
    if not isinstance(report, Mapping) or report.get("schema") != READINESS_REPORT_SCHEMA:
        raise R012RuntimeCompositionError("R012 readiness report differs")
    if report.get("live_ready") is True:
        return
    blockers = report.get("blockers")
    detail = blockers if isinstance(blockers, list) else []
    raise R012RuntimeCompositionError(f"R012 live activation is not ready: {detail}")


def require_bench_launch_ready(report: Mapping[str, Any]) -> None:
    """Gate a first canary after controller delivery/read-back, before motion proof."""

    if not isinstance(report, Mapping) or report.get("schema") != READINESS_REPORT_SCHEMA:
        raise R012RuntimeCompositionError("R012 readiness report differs")
    if report.get("bench_launch_ready") is True:
        return
    raise R012RuntimeCompositionError(
        f"R012 bench launch is not ready: {report.get('blockers', [])}"
    )


def verify_local_controller_triplet(snapshot: Any) -> bool:
    programs = getattr(snapshot, "controller_programs", None)
    if not isinstance(programs, Mapping):
        return False
    paths: dict[str, Path] = {}
    for role in ("script", "txt", "urp"):
        value = programs.get(role)
        if not isinstance(value, Mapping):
            return False
        path = Path(str(value.get("path", "")))
        if not path.is_file() or path.name != value.get("basename") or path.stat().st_size <= 0:
            return False
        paths[role] = path
    try:
        from .controller_triplet import R012_PROGRAM, validate_controller_triplet
        validate_controller_triplet(
            paths["script"].read_text(encoding="utf-8"),
            paths["txt"].read_text(encoding="utf-8"),
            paths["urp"].read_bytes(),
        )
    except (OSError, KeyError, TypeError, ValueError, RuntimeError, json.JSONDecodeError):
        return False
    return True


def verify_controller_readback(snapshot: Any, receipt_path: Path) -> bool:
    """Verify a future UR10e owner's fresh GET receipt against local R012 bytes."""

    try:
        value = json.loads(Path(receipt_path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    if not isinstance(value, Mapping) or value.get("schema") != "step5d.autotune-v4/r012-controller-readback-v2" or value.get("fresh_get") is not True:
        return False
    if value.get("program") != "step5d_strict_rnn_autotune_v4_r012" or value.get("revision") != R012_REVISION:
        return False
    if value.get("controller_target") != "/programs/andyl/kunwei/step5/step5d_strict_rnn_autotune_v4_r012.urp":
        return False
    if (
        value.get("motion_protocol") != R012_MOTION_PROTOCOL
        or value.get("extension_protocol") != R012_RUNTIME_PROTOCOL
        or value.get("safety_mode") != "NORMAL"
        or value.get("stationary") is not True
        or value.get("home") is not True
        or value.get("byte_equal") is not True
        or not isinstance(value.get("timestamp"), str) or not value.get("timestamp")
        or not isinstance(value.get("state"), str) or not value.get("state")
    ):
        return False
    programs = getattr(snapshot, "controller_programs", None)
    readback = value.get("readback_files")
    basenames = value.get("basenames")
    if not isinstance(readback, Mapping) or not isinstance(basenames, Mapping) or not isinstance(programs, Mapping):
        return False
    for role in ("script", "txt", "urp"):
        item = programs.get(role)
        if not isinstance(item, Mapping) or basenames.get(role) != item.get("basename"):
            return False
        try:
            local_bytes = Path(str(item["path"])).read_bytes()
            remote_bytes = Path(str(readback[role])).read_bytes()
        except OSError:
            return False
        if local_bytes != remote_bytes:
            return False
    descriptor = build_descriptor(
        campaign_id=snapshot.campaign_id, run_id=snapshot.run_id, attempt_id=snapshot.attempt_id,
        route_id=str(value.get("route_id", f"r012-route-{snapshot.run_id}")),
        session_id=str(value.get("session_id", f"r012-session-{snapshot.run_id}")),
        session_epoch=int(value.get("session_epoch", 1)),
    )
    return value.get("descriptor") == descriptor.as_dict()


def require_live_admission(snapshot: Any, facts: Mapping[str, Any]) -> None:
    """Admit only readable identity plus current physical/runtime facts."""

    descriptor = build_descriptor(
        campaign_id=snapshot.campaign_id, run_id=snapshot.run_id, attempt_id=snapshot.attempt_id,
        route_id=str(facts.get("route_id", f"r012-route-{snapshot.run_id}")),
        session_id=str(facts.get("session_id", f"r012-session-{snapshot.run_id}")),
        session_epoch=int(facts.get("session_epoch", 1)),
    )
    expected = {
        "program": descriptor.program, "revision": descriptor.revision,
        "motion_protocol": descriptor.motion_protocol, "extension_protocol": descriptor.extension_protocol,
        "route_id": descriptor.route_id, "session_id": descriptor.session_id,
        "session_epoch": descriptor.session_epoch,
        "controller_target": f"/programs/andyl/kunwei/step5/{descriptor.program}.urp",
        "controller_basename": f"{descriptor.program}.urp",
        "txt_basename": f"{descriptor.program}.txt",
        "urp_basename": f"{descriptor.program}.urp",
    }
    if any(facts.get(key) != value for key, value in expected.items()):
        raise R012RuntimeCompositionError("R012 readable live descriptor differs")
    required_true = (
        "controller_package_structurally_valid", "controller_readback_byte_equal", "runtime_protocol_observed",
        "home_stationary_safety_normal", "rtde_and_kunwei_fresh", "cbf_timing_physical_guards",
        "payload_cog_tcp_valid", "protective_stop_clear", "force_torque_velocity_limits",
    )
    if any(facts.get(key) is not True for key in required_true):
        raise R012RuntimeCompositionError("R012 current live safety/runtime facts are not admitted")


def verify_cbf_timing_receipt(receipt_path: Path) -> bool:
    try:
        value = json.loads(Path(receipt_path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    timing = value.get("timing") if isinstance(value, Mapping) else None
    return bool(
        isinstance(value, Mapping)
        and value.get("schema") == "step5d.autotune-v4/r012-cbf-launch-host-timing-v1"
        and value.get("host") == socket.gethostname()
        and isinstance(timing, Mapping)
        and timing.get("qualified") is True
        and timing.get("algorithm_bound_ok") is True
    )


__all__ = [
    "BOUNDED_TUNING_BLOCKERS",
    "READINESS_REPORT_SCHEMA",
    "R012RuntimeCompositionError",
    "RUNTIME_COMPOSITION_SCHEMA",
    "build_offline_composition",
    "build_readiness_report",
    "require_live_ready",
    "require_bench_launch_ready",
    "runtime_composition_manifest",
    "verify_controller_readback",
    "verify_cbf_timing_receipt",
    "verify_local_controller_triplet",
    "validate_runtime_composition",
]
