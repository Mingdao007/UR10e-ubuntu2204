#!/usr/bin/env python3
"""Run exactly one guarded Step6 Figure-eight no-contact canary with raw RTDE evidence."""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import hashlib
import json
import math
from pathlib import Path
import time
from typing import Any, Callable, Mapping, Sequence

try:
    from build_step6_figure8_no_contact_canary_v1 import (
        ALONG,
        DURATION_S,
        FIGURE8_BIN_WIDTH_S,
        FIGURE8_FULL_BIN_COUNT,
        CONTROLLER_DIR,
        HARD_ELLIPSE_M,
        HOME_POSE,
        LATERAL,
        PROGRAM_NAME,
        SOFT_ELLIPSE_M,
        WORKSPACE,
        X_GUARD_M,
    )
    from step5d_autotune_v3.rtde_client import RTDEClient, dashboard_exchange
except ModuleNotFoundError:  # pragma: no cover - repository-root import
    from tools.build_step6_figure8_no_contact_canary_v1 import (
        ALONG,
        DURATION_S,
        FIGURE8_BIN_WIDTH_S,
        FIGURE8_FULL_BIN_COUNT,
        CONTROLLER_DIR,
        HARD_ELLIPSE_M,
        HOME_POSE,
        LATERAL,
        PROGRAM_NAME,
        SOFT_ELLIPSE_M,
        WORKSPACE,
        X_GUARD_M,
    )
    from tools.step5d_autotune_v3.rtde_client import RTDEClient, dashboard_exchange


ROOT = Path(__file__).resolve().parents[1]
ROBOT_HOST = "192.168.1.18"
CONTROLLER_URP = f"{CONTROLLER_DIR}/{PROGRAM_NAME}.urp"
TRACE_FIELDS = (
    "timestamp",
    "actual_TCP_pose",
    "actual_TCP_speed",
    "actual_q",
    "actual_qd",
    "actual_TCP_force",
    "runtime_state",
    "robot_status_bits",
    "safety_status_bits",
    "output_int_register_40",
    "output_int_register_41",
    *(f"output_double_register_{index}" for index in range(40, 48)),
)


class FigureEightNoContactLiveError(RuntimeError):
    """The one-shot canary could not preserve its live safety/evidence contract."""


def _hkt_now() -> datetime:
    return datetime.now(timezone.utc).astimezone(timezone(timedelta(hours=8)))


def _dashboard(host: str, commands: Sequence[str]) -> dict[str, str]:
    return dashboard_exchange(host, commands, port=29999, timeout=3.0)


def _dashboard_snapshot(host: str) -> dict[str, str]:
    return _dashboard(
        host,
        (
            "is in remote control",
            "safetymode",
            "robotmode",
            "running",
            "programState",
            "get loaded program",
        ),
    )


def _require_preflight(snapshot: Mapping[str, str], *, require_loaded: bool) -> None:
    failures: list[str] = []
    if snapshot.get("is in remote control") != "true":
        failures.append("remote_control_not_true")
    if snapshot.get("safetymode") != "Safetymode: NORMAL":
        failures.append("safety_not_normal")
    if snapshot.get("robotmode") != "Robotmode: RUNNING":
        failures.append("robotmode_not_running")
    if snapshot.get("running") != "Program running: false":
        failures.append("program_already_running")
    if not str(snapshot.get("programState", "")).startswith("STOPPED"):
        failures.append("program_state_not_stopped")
    if require_loaded and snapshot.get("get loaded program") != f"Loaded program: {CONTROLLER_URP}":
        failures.append("loaded_program_identity_differs")
    if failures:
        raise FigureEightNoContactLiveError("preflight failed: " + ",".join(failures))


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _canonical_sha256(value: Mapping[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(dict(value), sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    ).hexdigest()


def _finite_vector(value: Any, length: int, role: str) -> tuple[float, ...]:
    if not isinstance(value, (tuple, list)) or len(value) != length:
        raise FigureEightNoContactLiveError(f"{role} is incomplete")
    result = tuple(float(item) for item in value)
    if not all(math.isfinite(item) for item in result):
        raise FigureEightNoContactLiveError(f"{role} is non-finite")
    return result


def _path_rows(rows: Sequence[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    return [row for row in rows if int(row["output_int_register_40"]) == 2]


def _orientation_error(
    pose: Sequence[float], home_pose: Sequence[float]
) -> float:
    return math.dist(
        tuple(float(value) for value in pose[3:6]),
        tuple(float(value) for value in home_pose[3:6]),
    )


def summarize_trace(
    rows: Sequence[Mapping[str, Any]],
    *,
    preflight: Mapping[str, str],
    loaded: Mapping[str, str],
    postflight: Mapping[str, str],
    home_pose: Sequence[float] = HOME_POSE,
) -> dict[str, Any]:
    if not rows:
        raise FigureEightNoContactLiveError("RTDE trace is empty")
    normalized_rows = [dict(row) for row in rows]
    path_rows = _path_rows(normalized_rows)
    final = normalized_rows[-1]
    path_times = [float(row["output_double_register_40"]) for row in path_rows]
    bins = {
        min(FIGURE8_FULL_BIN_COUNT - 1, max(0, int(math.floor(value / FIGURE8_BIN_WIDTH_S + 1e-9))))
        for value in path_times
        if 0.0 <= value < DURATION_S
    }
    host_times = [float(row["host_monotonic_s"]) for row in normalized_rows]
    host_gaps = [right - left for left, right in zip(host_times, host_times[1:])]
    actual_tracking: list[dict[str, float]] = []
    for row in path_rows:
        pose = _finite_vector(row["actual_TCP_pose"], 6, "actual_TCP_pose")
        desired_x = float(row["output_double_register_41"])
        desired_y = float(row["output_double_register_42"])
        error_x = pose[0] - desired_x
        error_y = pose[1] - desired_y
        error_along = error_x * ALONG[0] + error_y * ALONG[1]
        error_lateral = error_x * LATERAL[0] + error_y * LATERAL[1]
        actual_tracking.append(
            {
                "along_m": error_along,
                "lateral_m": error_lateral,
                "hard_rho": (error_along / HARD_ELLIPSE_M[0]) ** 2 + (error_lateral / HARD_ELLIPSE_M[1]) ** 2,
                "soft_rho": (error_along / SOFT_ELLIPSE_M[0]) ** 2 + (error_lateral / SOFT_ELLIPSE_M[1]) ** 2,
            }
        )
    final_pose = _finite_vector(final["actual_TCP_pose"], 6, "final TCP pose")
    final_speed = _finite_vector(final["actual_TCP_speed"], 6, "final TCP speed")
    all_poses = [_finite_vector(row["actual_TCP_pose"], 6, "TCP pose") for row in normalized_rows]
    all_forces = [_finite_vector(row["actual_TCP_force"], 6, "TCP force") for row in normalized_rows]
    states = [int(row["output_int_register_40"]) for row in normalized_rows]
    reasons = [int(row["output_int_register_41"]) for row in normalized_rows]
    terminal_state = states[-1]
    terminal_reason = reasons[-1]
    path_coverage_passed = bool(path_times) and max(path_times) >= DURATION_S - FIGURE8_BIN_WIDTH_S and len(bins) == FIGURE8_FULL_BIN_COUNT
    max_hard_rho = max((row["hard_rho"] for row in actual_tracking), default=math.inf)
    max_soft_rho = max((row["soft_rho"] for row in actual_tracking), default=math.inf)
    all_actual_speeds = [
        math.sqrt(sum(float(value) * float(value) for value in row["actual_TCP_speed"][:3]))
        for row in normalized_rows
    ]
    path_actual_speeds = [
        math.sqrt(sum(float(value) * float(value) for value in row["actual_TCP_speed"][:3]))
        for row in path_rows
    ]
    max_transfer_speed = max(all_actual_speeds)
    max_path_speed = max(path_actual_speeds, default=math.inf)
    max_force_norm = max(math.sqrt(sum(value * value for value in force[:3])) for force in all_forces)
    max_abs_fz = max(abs(force[2]) for force in all_forces)
    expected_home = tuple(float(value) for value in home_pose)
    if len(expected_home) != 6:
        raise FigureEightNoContactLiveError("expected Home pose is incomplete")
    final_position_error = math.dist(final_pose[:3], expected_home[:3])
    final_orientation_error = _orientation_error(final_pose, expected_home)
    final_speed_norm = math.sqrt(sum(value * value for value in final_speed[:3]))
    workspace_observed = {
        "x_m": [min(pose[0] for pose in all_poses), max(pose[0] for pose in all_poses)],
        "y_m": [min(pose[1] for pose in all_poses), max(pose[1] for pose in all_poses)],
        "z_m": [min(pose[2] for pose in all_poses), max(pose[2] for pose in all_poses)],
    }
    checks = {
        "preflight_remote_normal_stopped": (
            preflight.get("is in remote control") == "true"
            and preflight.get("safetymode") == "Safetymode: NORMAL"
            and preflight.get("running") == "Program running: false"
        ),
        "loaded_exact_program": loaded.get("get loaded program") == f"Loaded program: {CONTROLLER_URP}",
        "path_state_seen": 2 in states,
        "path_600_bins_complete": path_coverage_passed,
        "terminal_home_state": terminal_state == 4,
        "terminal_reason_zero": terminal_reason == 0,
        "hard_tracking_guard_passed": max_hard_rho <= 1.0,
        "x_guard_passed": workspace_observed["x_m"][1] <= X_GUARD_M,
        "workspace_passed": (
            workspace_observed["x_m"][0] >= WORKSPACE["x_m"][0]
            and workspace_observed["x_m"][1] <= WORKSPACE["x_m"][1]
            and workspace_observed["y_m"][0] >= WORKSPACE["y_m"][0]
            and workspace_observed["y_m"][1] <= WORKSPACE["y_m"][1]
            and workspace_observed["z_m"][0] >= WORKSPACE["z_m"][0]
            and workspace_observed["z_m"][1] <= WORKSPACE["z_m"][1]
        ),
        "speed_guard_passed": max_path_speed <= 0.015,
        "force_no_contact_guard_passed": max_force_norm <= 20.0 and max_abs_fz <= 15.0,
        "final_home_position_passed": final_position_error <= 0.001,
        "final_home_orientation_passed": final_orientation_error <= 0.010,
        "final_static_passed": final_speed_norm <= 0.001,
        "postflight_safety_normal": postflight.get("safetymode") == "Safetymode: NORMAL",
        "postflight_program_stopped": postflight.get("running") == "Program running: false",
        "sample_gap_passed": not host_gaps or max(host_gaps) <= 0.1,
    }
    summary = {
        "schema": "step6.figure8/no-contact-live-evidence-v1",
        "program": PROGRAM_NAME,
        "controller_urp": CONTROLLER_URP,
        "motion_class": "one_shot_no_contact",
        "preflight": dict(preflight),
        "loaded": dict(loaded),
        "postflight": dict(postflight),
        "trace": {
            "sample_count": len(normalized_rows),
            "path_sample_count": len(path_rows),
            "path_bin_count": len(bins),
            "path_time_range_s": [min(path_times), max(path_times)] if path_times else None,
            "max_host_gap_s": max(host_gaps) if host_gaps else 0.0,
            "states_seen": sorted(set(states)),
            "terminal_state": terminal_state,
            "terminal_reason": terminal_reason,
        },
        "tracking": {
            "max_abs_along_error_m": max((abs(row["along_m"]) for row in actual_tracking), default=None),
            "max_abs_lateral_error_m": max((abs(row["lateral_m"]) for row in actual_tracking), default=None),
            "max_hard_rho": max_hard_rho,
            "max_soft_rho": max_soft_rho,
        },
        "workspace_observed": workspace_observed,
        "dynamics": {
            "max_path_tcp_linear_speed_m_s": max_path_speed,
            "max_transfer_tcp_linear_speed_m_s": max_transfer_speed,
            "path_speed_guard_m_s": 0.015,
            "transfer_movel_command_speed_m_s": 0.020,
            "max_force_norm_n": max_force_norm,
            "max_abs_fz_n": max_abs_fz,
        },
        "final_home": {
            "actual_pose": list(final_pose),
            "expected_pose": list(expected_home),
            "position_error_m": final_position_error,
            "orientation_error_rad": final_orientation_error,
            "linear_speed_m_s": final_speed_norm,
        },
        "checks": checks,
        "passed": all(checks.values()),
        "contact_executed": False,
        "force_control_executed": False,
        "campaign_live_acceptance": False,
    }
    return summary


def _write_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, indent=2, sort_keys=True, allow_nan=False) + "\n", encoding="utf-8")


def _write_trace(path: Path, rows: Sequence[Mapping[str, Any]]) -> None:
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(dict(row), sort_keys=True, allow_nan=False) + "\n")


def cold_recompute_run(run_dir: Path) -> dict[str, Any]:
    """Recompute the immutable trace without replaying robot motion."""

    run_dir = run_dir.expanduser().resolve()
    source_path = run_dir / "evidence.json"
    trace_path = run_dir / "raw_rtde_trace.jsonl"
    source = json.loads(source_path.read_text(encoding="utf-8"))
    expected_home = tuple(source.get("final_home", {}).get("expected_pose", HOME_POSE))
    rows = [json.loads(line) for line in trace_path.read_text(encoding="utf-8").splitlines()]
    summary = summarize_trace(
        rows,
        preflight=source["preflight"],
        loaded=source["loaded"],
        postflight=source["postflight"],
        home_pose=expected_home,
    )
    derivation = {
        "schema": "step6.figure8/no-contact-receipt-recompute-v1",
        "reason": "path_speed_guard_must_exclude_entry_and_return_movel",
        "robot_motion_replayed": False,
        "source_evidence": "evidence.json",
        "source_evidence_sha256": _sha256(source_path),
        "raw_trace": "raw_rtde_trace.jsonl",
        "raw_trace_sha256": _sha256(trace_path),
        "derivation_source_sha256": _sha256(Path(__file__)),
        "derived_at": _hkt_now().isoformat(),
    }
    evidence = {
        **summary,
        "captured_at": source["captured_at"],
        "package_sha256": source["package_sha256"],
        "raw_trace": "raw_rtde_trace.jsonl",
        "frame_receipt": "frame_receipt_v2.json",
        "derivation": derivation,
    }
    frame_payload = {
        "schema": "step6.figure8/frozen-home-frame-v1",
        "home_pose": list(expected_home),
        "along_base": list(ALONG),
        "lateral_base": list(LATERAL),
        "source_program": PROGRAM_NAME,
        "source_package_sha256": source["package_sha256"],
        "source_live_evidence": "evidence_receipt_v2.json",
        "source_raw_trace_sha256": derivation["raw_trace_sha256"],
        "live_canary_passed": summary["passed"],
        "home_calibration_receipt_sha256": source.get(
            "home_calibration_receipt_sha256"
        ),
    }
    frame_receipt = {**frame_payload, "frame_sha256": _canonical_sha256(frame_payload)}
    _write_json(run_dir / "evidence_receipt_v2.json", evidence)
    _write_json(run_dir / "frame_receipt_v2.json", frame_receipt)
    return evidence


def execute_live(
    *,
    host: str,
    output_root: Path,
    frequency_hz: float = 125.0,
    timeout_s: float = 70.0,
    home_pose: Sequence[float] = HOME_POSE,
    home_calibration_receipt_sha256: str | None = None,
    dashboard: Callable[[str, Sequence[str]], dict[str, str]] = _dashboard,
) -> tuple[Path, dict[str, Any]]:
    expected_home = tuple(float(value) for value in home_pose)
    if len(expected_home) != 6:
        raise FigureEightNoContactLiveError("expected Home pose is incomplete")
    local_script = ROOT / "programs" / "step6" / f"{PROGRAM_NAME}.script"
    script_text = local_script.read_text(encoding="utf-8")
    if home_calibration_receipt_sha256 is None:
        expected_authority = "HOME_CALIBRATION_RECEIPT_SHA256: pending"
    else:
        expected_authority = (
            "HOME_CALIBRATION_RECEIPT_SHA256: "
            + home_calibration_receipt_sha256
        )
    exact_home = "p[" + ", ".join(f"{value:.10f}" for value in expected_home) + "]"
    if expected_authority not in script_text or exact_home not in script_text:
        raise FigureEightNoContactLiveError(
            "local no-contact package Home authority differs from requested run"
        )
    preflight = _dashboard_snapshot(host)
    _require_preflight(preflight, require_loaded=False)
    load_response = dashboard(host, (f"load {CONTROLLER_URP}",))
    if "Loading program:" not in load_response.get(f"load {CONTROLLER_URP}", ""):
        raise FigureEightNoContactLiveError(f"Dashboard load failed: {load_response}")
    load_deadline = time.monotonic() + 5.0
    loaded = _dashboard_snapshot(host)
    while (
        loaded.get("get loaded program") != f"Loaded program: {CONTROLLER_URP}"
        and time.monotonic() < load_deadline
    ):
        time.sleep(0.1)
        loaded = _dashboard_snapshot(host)
    _require_preflight(loaded, require_loaded=True)

    run_stamp = _hkt_now().strftime("%Y%m%d_%H%M%S")
    run_dir = output_root / f"step6_figure8_no_contact_canary_v1_{run_stamp}"
    run_dir.mkdir(parents=True, exist_ok=False)
    package_dir = ROOT / "programs" / "step6"
    package_sha = {
        suffix: _sha256(package_dir / f"{PROGRAM_NAME}.{suffix}")
        for suffix in ("script", "txt", "urp")
    }
    rows: list[dict[str, Any]] = []
    start_monotonic = time.monotonic()
    play_sent = False
    postflight: dict[str, str] = {}
    try:
        with RTDEClient(host, port=30004, timeout=3.0) as client:
            client.negotiate()
            recipe_id, type_names = client.setup_outputs(frequency_hz, TRACE_FIELDS)
            client.start()
            values = client.recv_recipe_sample(recipe_id, type_names)
            first = dict(zip(TRACE_FIELDS, values))
            first["host_monotonic_s"] = time.monotonic() - start_monotonic
            rows.append(first)
            start_pose = _finite_vector(first["actual_TCP_pose"], 6, "start TCP pose")
            if not (
                WORKSPACE["x_m"][0] <= start_pose[0] <= WORKSPACE["x_m"][1]
                and WORKSPACE["y_m"][0] <= start_pose[1] <= WORKSPACE["y_m"][1]
                and WORKSPACE["z_m"][0] <= start_pose[2] <= WORKSPACE["z_m"][1]
                and _orientation_error(start_pose, expected_home) <= 0.020
            ):
                raise FigureEightNoContactLiveError("start pose is outside the transfer envelope")
            play_response = dashboard(host, ("play",))
            if "Starting program" not in play_response.get("play", ""):
                raise FigureEightNoContactLiveError(f"Dashboard play failed: {play_response}")
            play_sent = True
            terminal_seen_at: float | None = None
            next_dashboard_poll = time.monotonic() + 0.5
            initial_state = int(first["output_int_register_40"])
            initial_reason = int(first["output_int_register_41"])
            path_seen = False
            while True:
                values = client.recv_recipe_sample(recipe_id, type_names)
                now = time.monotonic()
                row = dict(zip(TRACE_FIELDS, values))
                row["host_monotonic_s"] = now - start_monotonic
                rows.append(row)
                state = int(row["output_int_register_40"])
                reason = int(row["output_int_register_41"])
                path_seen = path_seen or state == 2
                terminal_is_fresh = (
                    state == 4 and path_seen
                ) or (
                    state == 90
                    and (
                        state != initial_state
                        or reason != initial_reason
                        or now - start_monotonic >= 0.5
                    )
                )
                if terminal_is_fresh and terminal_seen_at is None:
                    terminal_seen_at = now
                if terminal_seen_at is not None and now - terminal_seen_at >= 0.25:
                    break
                if now >= next_dashboard_poll:
                    live_dashboard = _dashboard_snapshot(host)
                    if live_dashboard.get("safetymode") != "Safetymode: NORMAL":
                        raise FigureEightNoContactLiveError("Safety left NORMAL during canary")
                    if live_dashboard.get("running") == "Program running: false" and state not in (4, 90):
                        raise FigureEightNoContactLiveError("program stopped before terminal receipt")
                    next_dashboard_poll = now + 0.5
                if now - start_monotonic > timeout_s:
                    raise FigureEightNoContactLiveError("canary timeout before terminal receipt")
        postflight = _dashboard_snapshot(host)
    except BaseException as exc:
        if play_sent:
            try:
                dashboard(host, ("stop",))
            except Exception:
                pass
        if not postflight:
            try:
                postflight = _dashboard_snapshot(host)
            except Exception as exc:
                postflight = {"snapshot_error": f"{type(exc).__name__}: {exc}"}
        _write_trace(run_dir / "raw_rtde_trace.jsonl", rows)
        _write_json(
            run_dir / "failure.json",
            {
                "schema": "step6.figure8/no-contact-live-failure-v1",
                "captured_at": _hkt_now().isoformat(),
                "preflight": preflight,
                "loaded": loaded,
                "postflight": postflight,
                "package_sha256": package_sha,
                "trace_sample_count": len(rows),
                "error": f"{type(exc).__name__}: {exc}",
            },
        )
        raise

    summary = summarize_trace(
        rows,
        preflight=preflight,
        loaded=loaded,
        postflight=postflight,
        home_pose=expected_home,
    )
    frame_payload = {
        "schema": "step6.figure8/frozen-home-frame-v1",
        "home_pose": list(expected_home),
        "along_base": list(ALONG),
        "lateral_base": list(LATERAL),
        "source_program": PROGRAM_NAME,
        "source_package_sha256": package_sha,
        "source_live_evidence": "evidence.json",
        "live_canary_passed": summary["passed"],
        "home_calibration_receipt_sha256": home_calibration_receipt_sha256,
    }
    frame_receipt = {**frame_payload, "frame_sha256": _canonical_sha256(frame_payload)}
    evidence = {
        **summary,
        "captured_at": _hkt_now().isoformat(),
        "package_sha256": package_sha,
        "raw_trace": "raw_rtde_trace.jsonl",
        "frame_receipt": "frame_receipt.json",
        "home_calibration_receipt_sha256": home_calibration_receipt_sha256,
    }
    _write_trace(run_dir / "raw_rtde_trace.jsonl", rows)
    _write_json(run_dir / "evidence.json", evidence)
    _write_json(run_dir / "frame_receipt.json", frame_receipt)
    return run_dir, evidence


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default=ROBOT_HOST)
    parser.add_argument("--output-root", type=Path, default=ROOT / "runs")
    parser.add_argument("--frequency-hz", type=float, default=125.0)
    parser.add_argument("--timeout-s", type=float, default=70.0)
    parser.add_argument("--home-calibration-receipt", type=Path)
    parser.add_argument("--recompute-run", type=Path)
    parser.add_argument("--execute-live", action="store_true")
    parser.add_argument("--confirm-no-contact-motion", action="store_true")
    args = parser.parse_args()
    if args.recompute_run is not None:
        evidence = cold_recompute_run(args.recompute_run)
        print(
            json.dumps(
                {
                    "run_dir": str(args.recompute_run.expanduser().resolve()),
                    "passed": evidence["passed"],
                    "robot_motion_replayed": False,
                    "evidence": evidence,
                },
                indent=2,
                sort_keys=True,
            )
        )
        return 0 if evidence["passed"] else 2
    if not args.execute_live or not args.confirm_no_contact_motion:
        print(
            json.dumps(
                {
                    "live_executed": False,
                    "program": PROGRAM_NAME,
                    "controller_urp": CONTROLLER_URP,
                    "motion_class": "one_shot_no_contact",
                    "required_flags": ["--execute-live", "--confirm-no-contact-motion"],
                },
                indent=2,
                sort_keys=True,
            )
        )
        return 0
    expected_home = HOME_POSE
    home_receipt_sha = None
    if args.home_calibration_receipt is not None:
        try:
            from step6_figure8_autotune_v1.live_composition import (
                load_figure8_home_calibration_receipt,
            )
        except ModuleNotFoundError:  # pragma: no cover - repository-root import
            from tools.step6_figure8_autotune_v1.live_composition import (
                load_figure8_home_calibration_receipt,
            )
        home_receipt = load_figure8_home_calibration_receipt(
            args.home_calibration_receipt
        )
        expected_home = tuple(home_receipt["final_home_pose"])
        home_receipt_sha = str(home_receipt["receipt_sha256"])
    run_dir, evidence = execute_live(
        host=args.host,
        output_root=args.output_root,
        frequency_hz=args.frequency_hz,
        timeout_s=args.timeout_s,
        home_pose=expected_home,
        home_calibration_receipt_sha256=home_receipt_sha,
    )
    print(json.dumps({"run_dir": str(run_dir), "passed": evidence["passed"], "evidence": evidence}, indent=2, sort_keys=True))
    return 0 if evidence["passed"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
