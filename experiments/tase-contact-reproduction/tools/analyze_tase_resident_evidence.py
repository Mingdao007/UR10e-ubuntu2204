"""Read-only reduction of sealed resident TASE attempts.

The output is a derived diagnostic. It never changes an attempt, its formal
5--60 s objective, or the original campaign ledger.
"""
from __future__ import annotations

import argparse
import bisect
import hashlib
import json
import math
from pathlib import Path
import statistics
from typing import Any

from tase_r013_timing_ledger import STAGES, TaseR013TimingLedger, TimingLedgerError
from tase_figure8_protocol import (
    BIN_WIDTH_S,
    DURATION_S,
    FORMAL_END_S,
    FORMAL_START_S,
    TARGET_FORCE_N,
)


SCHEMA = "tase.resident-evidence-diagnostic-v1"
CONTROLLER_TRACE_SCHEMAS = {
    "tase.qp-replay-command-timeline-v2",
    "tase.qp-replay-command-timeline-v3",
}
EXPECTED_PROTOCOLS = {
    "figure8_window60_r013_compat_v1",
    "figure8_window60_r013_rate400_v1",
    "figure8_window60_r013_rate460_v1",
}


def _rows(path: Path):
    with path.open(encoding="utf-8") as source:
        for line in source:
            if line.strip():
                yield json.loads(line)


def _verify_segments(attempt_dir: Path, item: dict[str, Any]) -> dict[str, Any]:
    seal = item.get("sealed_evidence") or {}
    segments = seal.get("segments") or {}
    disk_seal = json.loads((attempt_dir / "seal.json").read_text(encoding="utf-8"))
    if disk_seal != seal:
        raise ValueError(f"seal.json differs from attempt-result: {attempt_dir}")
    if not segments or seal.get("lifecycle", {}).get("sealed") is not True:
        raise ValueError(f"attempt lacks a complete segment seal: {attempt_dir}")
    receipts = dict(segments)
    if seal.get("service_observations"):
        receipts["service_observations"] = seal["service_observations"]
    for name, receipt in receipts.items():
        path = Path(receipt["path"])
        if not path.is_file() or path.is_symlink():
            raise ValueError(f"sealed segment missing: {name}: {path}")
        digest = hashlib.sha256()
        count = 0
        with path.open("rb") as source:
            while chunk := source.read(1024 * 1024):
                digest.update(chunk)
                count += chunk.count(b"\n")
        if digest.hexdigest() != receipt["sha256"] or count != receipt["count"]:
            raise ValueError(f"sealed segment digest/count differs: {name}: {path}")
    return segments


def _stages(path: Path, path_start: float | None,
            home_target: dict[str, Any] | None = None,
            service_path: Path | None = None) -> dict[str, float | None]:
    first: dict[int, float] = {}
    first_ready_after_return = None
    stationary_home_observed = None
    expected_q = (home_target or {}).get("final_q")
    expected_pose = (home_target or {}).get("final_pose")
    for row in _rows(path):
        timestamp = float(row["received_monotonic_s"])
        echoes = row.get("integer_echoes") or {}
        state = echoes.get("26", echoes.get(26))
        if state is None:
            continue
        state = int(state)
        if state in {20, 21, 25, 40} and state not in first:
            first[state] = timestamp
        if (state == 78 and first_ready_after_return is None
                and 40 in first and timestamp >= first[40]):
            first_ready_after_return = timestamp
    if first_ready_after_return is None and 40 in first and service_path is not None and service_path.is_file():
        for entry in _rows(service_path):
            if entry.get("label") != "robot_frames":
                continue
            row = entry.get("row") or {}
            echoes = row.get("integer_echoes") or {}
            state = int(echoes.get("26", echoes.get(26, -1)))
            timestamp = float(row.get("received_monotonic_s", -math.inf))
            if state == 78 and timestamp >= first[40]:
                first_ready_after_return = timestamp
                break
    if first_ready_after_return is not None and expected_q is not None and expected_pose is not None:
        home_frames = [
            row for row in _rows(path)
            if row.get("received_monotonic_s", -math.inf) >= first_ready_after_return
        ]
        if service_path is not None and service_path.is_file():
            home_frames.extend(
                entry["row"] for entry in _rows(service_path)
                if entry.get("label") == "robot_frames"
                and entry.get("row", {}).get("received_monotonic_s", -math.inf)
                >= first_ready_after_return
            )
        home_frames.sort(key=lambda row: float(row["received_monotonic_s"]))
        stable_since = None
        previous_stable_frame = None
        for row in home_frames:
            timestamp = float(row["received_monotonic_s"])
            echoes = row.get("integer_echoes") or {}
            state = int(echoes.get("26", echoes.get(26, -1)))
            q = row.get("q_rad") or ()
            qd = row.get("qd_rad_s") or ()
            pose = row.get("tcp_pose_m_rad") or ()
            speed = row.get("tcp_speed_m_s_rad_s") or ()
            still_home = bool(
                state == 78 and expected_q is not None and expected_pose is not None
                and len(q) == len(expected_q) == 6 and len(qd) == len(speed) == 6
                and len(pose) == len(expected_pose) == 6
                and row.get("safety_mode") in (1, "NORMAL")
                and max(abs(a - b) for a, b in zip(q, expected_q)) <= .02
                and math.dist(pose[:3], expected_pose[:3]) <= .0005
                and math.dist(pose[3:], expected_pose[3:]) <= .01
                and max(map(abs, qd)) < .001 and max(map(abs, speed)) < .001
            )
            if still_home and (previous_stable_frame is None
                               or 0 < timestamp - previous_stable_frame <= .020):
                stable_since = timestamp if stable_since is None else stable_since
                if timestamp - stable_since >= .5:
                    stationary_home_observed = timestamp
            elif still_home:
                stable_since = timestamp
            else:
                stable_since = None
            previous_stable_frame = timestamp if still_home else None
            if stationary_home_observed is not None:
                break
    return {
        "contact_search_s": first.get(20),
        "contact_latch_s": first.get(21),
        "entry_s": first.get(25),
        "path_start_s": path_start,
        "returning_s": first.get(40),
        "first_tp_ready_home_s": first_ready_after_return,
        "stationary_home_observed_s": stationary_home_observed,
    }


def _session_events(session_dir: Path) -> list[dict[str, Any]]:
    path = session_dir / "supervisor-result.json"
    if not path.is_file():
        return []
    result = json.loads(path.read_text(encoding="utf-8"))
    return ((result.get("body") or {}).get("session") or {}).get("lifecycle_events") or []


def _event_time(events: list[dict[str, Any]], stage: str, event: str,
                sequence: int, *, after: float | None = None,
                before: float | None = None) -> float | None:
    for row in events:
        stamp = row.get("monotonic_s")
        if (row.get("stage") != stage or row.get("event") != event
                or not isinstance(stamp, (int, float))):
            continue
        if row.get("sequence", sequence) != sequence:
            continue
        if after is not None and stamp < after:
            continue
        if before is not None and stamp > before:
            continue
        return float(stamp)
    return None


def _force_window(values: list[tuple[float, float]]) -> dict[str, float | int | None]:
    if not values:
        return {"samples": 0, "sample_mae_n": None, "sample_rmse_n": None,
                "sample_error_p95_n": None, "sample_bias_measured_minus_target_n": None,
                "peak_normal_n": None,
                "below_1n_fraction": None, "below_1n_runs": 0,
                "longest_below_1n_run_s": None}
    signed_errors = [value - TARGET_FORCE_N for _, value in values]
    errors = [abs(error) for error in signed_errors]
    runs = 0
    longest = 0.0
    run_start = None
    run_end = None
    for stamp, value in values:
        if value < 1.0:
            if run_start is None:
                runs += 1
                run_start = stamp
            run_end = stamp
        elif run_start is not None:
            longest = max(longest, run_end - run_start)
            run_start = run_end = None
    if run_start is not None:
        longest = max(longest, run_end - run_start)
    return {
        "samples": len(values),
        "sample_mae_n": statistics.fmean(errors),
        "sample_rmse_n": math.sqrt(statistics.fmean(error * error for error in errors)),
        "sample_error_p95_n": sorted(errors)[math.ceil(.95 * len(errors)) - 1],
        "sample_bias_measured_minus_target_n": statistics.fmean(signed_errors),
        "peak_normal_n": max(value for _, value in values),
        "below_1n_fraction": sum(value < 1.0 for _, value in values) / len(values),
        "below_1n_runs": runs,
        "longest_below_1n_run_s": longest,
    }


def _bin_mean_force_window(
    values: list[tuple[float, float]], *, start_s: float, end_s: float,
    source_label: str,
) -> dict[str, Any]:
    """Equal-weight 100 ms bin-mean diagnostic for one named force signal."""
    bin_count_float = (end_s - start_s) / BIN_WIDTH_S
    bin_count = int(round(bin_count_float))
    if (
        not math.isfinite(bin_count_float)
        or bin_count <= 0
        or not math.isclose(bin_count_float, bin_count, rel_tol=0.0, abs_tol=1e-12)
    ):
        raise ValueError("raw sensor diagnostic window must contain whole R013 bins")
    totals = [0.0] * bin_count
    counts = [0] * bin_count
    for stamp, force in values:
        if not start_s <= stamp < end_s:
            continue
        index = int(math.floor((stamp - start_s) / BIN_WIDTH_S + 1e-9))
        if 0 <= index < bin_count:
            totals[index] += force
            counts[index] += 1
    means = [total / count for total, count in zip(totals, counts) if count]
    errors = [value - TARGET_FORCE_N for value in means]
    absolute = sorted(abs(error) for error in errors)
    p95 = absolute[math.ceil(.95 * len(absolute)) - 1] if absolute else None
    return {
        "metric_basis": f"equal-weight mean of nonempty 100 ms bin means from {source_label}",
        "window_s": [float(start_s), float(end_s)],
        "sample_count": sum(counts),
        "nonempty_bins": len(means),
        "required_bins": bin_count,
        "empty_bins": bin_count - len(means),
        "coverage_fraction": len(means) / bin_count,
        "complete_coverage": len(means) == bin_count,
        "mean_of_bin_means_n": statistics.fmean(means) if means else None,
        "bin_mean_mae_n": statistics.fmean(absolute) if absolute else None,
        "bin_mean_rmse_n": math.sqrt(statistics.fmean(error * error for error in errors))
            if errors else None,
        "bin_mean_bias_measured_minus_target_n": statistics.fmean(errors) if errors else None,
        "bin_mean_tail_abs_error_p95_n": p95,
    }


def _raw_sensor_bin_mean_window(
    values: list[tuple[float, float]], *, start_s: float, end_s: float,
) -> dict[str, Any]:
    """Equal-weight bin-mean diagnostic over retained raw Kunwei samples."""
    result = _bin_mean_force_window(
        values,
        start_s=start_s,
        end_s=end_s,
        source_label="sealed raw KWR75 corrected normal-load samples",
    )
    result["metric_basis"] += "; diagnostic only, not the synchronized official score"
    return result


def _controller_path_force_diagnostics(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Compare per-frame measured, filtered, and control signals on PATH time."""
    channels = {
        "normal_force_measured_n": "KWR75 corrected normal load at controller observation",
        "normal_force_filtered_n": "controller low-pass filtered KWR75 normal load",
        "normal_force_control_n": "normal load supplied to the TASE outer loop",
    }
    values: dict[str, list[tuple[float, float]]] = {name: [] for name in channels}
    invalid_rows = 0
    for row in rows:
        if row.get("reference_phase") != "path":
            continue
        try:
            reference_time = float(row.get("reference_time_s", math.nan))
        except (TypeError, ValueError):
            reference_time = math.nan
        if not math.isfinite(reference_time) or not 0.0 <= reference_time < DURATION_S:
            invalid_rows += 1
            continue
        for name in channels:
            try:
                force = float(row.get(name, math.nan))
            except (TypeError, ValueError):
                force = math.nan
            if not math.isfinite(force):
                invalid_rows += 1
                continue
            values[name].append((reference_time, force))

    windows = {
        "window_0_60_s": (0.0, DURATION_S),
        "window_0_5_s": (0.0, FORMAL_START_S),
        "window_5_60_s": (FORMAL_START_S, FORMAL_END_S),
    }
    result: dict[str, Any] = {}
    for name, source_label in channels.items():
        channel_windows: dict[str, Any] = {}
        for window_name, (start_s, end_s) in windows.items():
            selected = [
                (stamp, force) for stamp, force in values[name]
                if start_s <= stamp < end_s
            ]
            channel_windows[window_name] = {
                "per_sample": _force_window(selected),
                "equal_weight_100ms_bins": _bin_mean_force_window(
                    selected,
                    start_s=start_s,
                    end_s=end_s,
                    source_label=source_label,
                ),
            }
        result[name] = {
            "source": source_label,
            "path_time_source": "command_timeline.reference_time_s",
            "windows": channel_windows,
        }
    return {
        "available": any(values.values()),
        "invalid_sample_or_channel_count": invalid_rows,
        "signals": result,
    }


def _synchronized_path_force_diagnostics(
    timeline_path: Path,
    robot_frames_path: Path,
    *,
    metrics: dict[str, Any],
    receipt_path_sample_count: int | None,
) -> dict[str, Any]:
    """Rebuild the accepted RTDE/TP-joined force samples used by the R013 score.

    ``command_timeline`` contains every successful writer tick. The live
    ``PathEvidenceCollector`` scores only fresh, unique RTDE timestamp / TP
    consumed-sequence pairs. Reconstruct those pairs from the sealed robot
    frames before computing the 550-bin score; using all writer ticks as a
    cross-check can disagree slightly because it includes cached joins.
    """
    echo_summary = metrics.get("raw_rtde_tp_echo_summary") or {}
    try:
        boundary_timestamp = float(echo_summary["first_controller_timestamp_s"])
        boundary_sequence = int(echo_summary["first_consumed_sequence"])
    except (KeyError, TypeError, ValueError):
        return {
            "available": False,
            "reason": "sealed metrics lack the RTDE/TP PATH boundary identity",
            "force_source": "Kunwei KWR75 values from sealed command timeline",
        }
    if not math.isfinite(boundary_timestamp) or boundary_sequence < 0:
        return {
            "available": False,
            "reason": "sealed RTDE/TP PATH boundary identity is invalid",
            "force_source": "Kunwei KWR75 values from sealed command timeline",
        }

    frames = list(_rows(robot_frames_path))
    frames.sort(key=lambda row: float(row["received_monotonic_s"]))
    frame_times = [float(row["received_monotonic_s"]) for row in frames]
    timeline_rows = [
        row for row in _rows(timeline_path)
        if row.get("reference_phase") == "path"
    ]
    matched: list[tuple[dict[str, Any], dict[str, Any], float]] = []
    unmatched = 0
    ambiguous = 0
    for row in timeline_rows:
        try:
            host_time = float(row["host_monotonic_s"])
            q = tuple(float(value) for value in row["actual_q_rad"])
            qd = tuple(float(value) for value in row["actual_qd_rad_s"])
        except (KeyError, TypeError, ValueError):
            unmatched += 1
            continue
        if len(q) != 6 or len(qd) != 6 or not math.isfinite(host_time):
            unmatched += 1
            continue
        insertion = bisect.bisect_right(frame_times, host_time)
        candidate_indices = range(max(0, insertion - 4), min(len(frames), insertion + 2))
        candidates = []
        for index in candidate_indices:
            frame = frames[index]
            try:
                frame_q = tuple(float(value) for value in frame.get("q_rad", ()))
                frame_qd = tuple(float(value) for value in frame.get("qd_rad_s", ()))
                received = float(frame["received_monotonic_s"])
            except (KeyError, TypeError, ValueError):
                continue
            if frame_q == q and frame_qd == qd:
                candidates.append((abs(received - host_time), frame))
        if not candidates:
            unmatched += 1
            continue
        if len(candidates) > 1:
            ambiguous += 1
        age, frame = min(candidates, key=lambda item: item[0])
        matched.append((row, frame, age))

    accepted: list[tuple[float, int, float, dict[str, Any]]] = []
    seen: set[tuple[float, int]] = set()
    previous: tuple[float, int] | None = None
    rejected_state = 0
    before_boundary = 0
    duplicate_clock = 0
    invalid_force = 0
    for row, frame, _age in matched:
        try:
            echoes = frame.get("integer_echoes") or {}
            state = int(echoes.get("26", echoes.get(26, -1)))
            timestamp = float(frame["timestamp"])
            sequence = int(frame["consumed_packet_sequence"])
        except (KeyError, TypeError, ValueError):
            unmatched += 1
            continue
        if state != 25:
            rejected_state += 1
            continue
        if timestamp < boundary_timestamp or sequence < boundary_sequence:
            before_boundary += 1
            continue
        path_time = timestamp - boundary_timestamp
        if not math.isfinite(path_time) or not 0.0 <= path_time < DURATION_S:
            before_boundary += 1
            continue
        identity = (timestamp, sequence)
        if identity in seen:
            duplicate_clock += 1
            continue
        if previous is not None:
            if timestamp < previous[0] or sequence < previous[1]:
                return {
                    "available": False,
                    "reason": "sealed RTDE/TP PATH identities regress in time",
                    "matched_timeline_rows": len(matched),
                    "unmatched_timeline_rows": unmatched,
                }
            if timestamp == previous[0] or sequence == previous[1]:
                duplicate_clock += 1
                continue
        elif identity != (boundary_timestamp, boundary_sequence) and (
            timestamp == boundary_timestamp or sequence == boundary_sequence
        ):
            duplicate_clock += 1
            continue
        try:
            filtered = float(row["normal_force_filtered_n"])
            measured = float(row["normal_force_measured_n"])
            control = float(row["normal_force_control_n"])
        except (KeyError, TypeError, ValueError):
            invalid_force += 1
            continue
        if not all(math.isfinite(value) for value in (filtered, measured, control)):
            invalid_force += 1
            continue
        accepted.append((timestamp, sequence, path_time, {
            "normal_force_filtered_n": filtered,
            "normal_force_measured_n": measured,
            "normal_force_control_n": control,
        }))
        seen.add(identity)
        previous = identity

    windows: dict[str, Any] = {}
    for window_name, start_s, end_s in (
        ("window_0_60_s", 0.0, DURATION_S),
        ("window_5_60_s", FORMAL_START_S, FORMAL_END_S),
    ):
        signal_results: dict[str, Any] = {}
        for signal, source in (
            ("normal_force_filtered_n", "accepted RTDE/TP-joined KWR75 filtered normal load"),
            ("normal_force_measured_n", "accepted RTDE/TP-joined corrected KWR75 normal load"),
            ("normal_force_control_n", "accepted RTDE/TP-joined normal load supplied to TASE"),
        ):
            samples = [
                (path_time, values[signal])
                for _timestamp, _sequence, path_time, values in accepted
                if start_s <= path_time < end_s
            ]
            signal_results[signal] = {
                "per_sample": _force_window(samples),
                "equal_weight_100ms_bins": _bin_mean_force_window(
                    samples, start_s=start_s, end_s=end_s, source_label=source,
                ),
            }
        windows[window_name] = {
            "window_s": [start_s, end_s],
            "required_bins": int(round((end_s - start_s) / BIN_WIDTH_S)),
            "sample_count": sum(start_s <= item[2] < end_s for item in accepted),
            "signals": signal_results,
        }

    receipt_mae = metrics.get("normal_force_mae_n")
    reconstructed = (
        windows["window_5_60_s"]["signals"]["normal_force_filtered_n"]
        ["equal_weight_100ms_bins"].get("bin_mean_mae_n")
    )
    delta = (
        None if not isinstance(receipt_mae, (int, float))
        or not isinstance(reconstructed, (int, float))
        else float(reconstructed) - float(receipt_mae)
    )
    return {
        "available": True,
        "force_source": "Kunwei KWR75; not an independent task-force reference",
        "path_time_source": "matched RTDE controller timestamp minus first consumed PATH RTDE timestamp",
        "sample_identity": "unique ordered (RTDE controller timestamp, TP consumed packet sequence) pairs",
        "matched_timeline_rows": len(matched),
        "unmatched_timeline_rows": unmatched,
        "ambiguous_nearest_frame_matches": ambiguous,
        "rejected_non_path_state_rows": rejected_state,
        "rejected_before_or_outside_path_boundary_rows": before_boundary,
        "duplicate_rtde_or_tp_clock_rows": duplicate_clock,
        "invalid_force_rows": invalid_force,
        "reconstructed_accepted_path_sample_count": len(accepted),
        "receipt_path_sample_count": receipt_path_sample_count,
        "accepted_sample_count_delta_from_receipt": (
            None if receipt_path_sample_count is None
            else len(accepted) - int(receipt_path_sample_count)
        ),
        "windows": windows,
        "official_score_crosscheck": {
            "receipt_formal_550_bin_mae_n": receipt_mae,
            "accepted_join_reconstructed_550_bin_mae_n": reconstructed,
            "reconstruction_minus_receipt_n": delta,
            "exact_match_within_1e-9_n": (
                None if delta is None else abs(delta) <= 1e-9
            ),
            "metric_identity": "filtered KWR75 normal load, equal-weight 100 ms means, [5,60) bins",
        },
    }


def _force_diagnostics(path: Path, path_start: float | None) -> dict[str, Any]:
    windows: list[list[tuple[float, float]]] = [[], []]
    full_path: list[tuple[float, float]] = []
    if path_start is not None:
        for row in _rows(path):
            wrench = row.get("corrected_wrench_n_nm")
            t = float(row.get("host_use_monotonic_s", -math.inf)) - path_start
            if wrench is not None and 0 <= t < 60.0:
                full_path.append((t, -float(wrench[2])))
                windows[0 if t < 5.0 else 1].append((t, -float(wrench[2])))
    return {"window_0_60_s": _force_window(full_path),
            "window_0_5_s": _force_window(windows[0]),
            "window_5_60_s": _force_window(windows[1]),
            "raw_sensor_bin_mean_diagnostics": {
                "window_0_60_s": _raw_sensor_bin_mean_window(
                    full_path, start_s=0.0, end_s=DURATION_S,
                ),
                "window_0_5_s": _raw_sensor_bin_mean_window(
                    full_path, start_s=0.0, end_s=FORMAL_START_S,
                ),
            "window_5_60_s": _raw_sensor_bin_mean_window(
                    full_path, start_s=FORMAL_START_S, end_s=FORMAL_END_S,
                ),
                "official_window_s": [FORMAL_START_S, FORMAL_END_S],
            },
            "interpretation": "raw corrected samples; below 1 N is a contact-loss proxy, not visual proof; independent of formal bin-mean MAE"}


def _distribution(values: list[float]) -> dict[str, float | int | None]:
    finite = sorted(float(value) for value in values if math.isfinite(float(value)))
    if not finite:
        return {"count": 0, "mean": None, "rms": None, "p95": None,
                "min": None, "max": None}
    p95 = finite[math.ceil(.95 * len(finite)) - 1]
    return {
        "count": len(finite),
        "mean": statistics.fmean(finite),
        "rms": math.sqrt(statistics.fmean(value * value for value in finite)),
        "p95": p95,
        "min": finite[0],
        "max": finite[-1],
    }


def _pre_path_force_diagnostics(
    sensor_path: Path,
    robot_frames_path: Path,
    stages: dict[str, float | None],
) -> dict[str, Any]:
    """Describe search, contact ramp, and entry without a fixed-5 N MAE.

    State 20 is contact search, state 21 includes the one-newton latch and
    force ramp, and state 25 is the separate PATH entry. Those intervals do
    not all hold a constant 5 N reference, so this reports measured channels
    and timing instead of treating them as force-tracking error.
    """
    intervals = {
        "state20_contact_search": (
            stages.get("contact_search_s"), stages.get("contact_latch_s"),
        ),
        "state21_latch_and_ramp": (
            stages.get("contact_latch_s"), stages.get("entry_s"),
        ),
        "entry": (stages.get("entry_s"), stages.get("path_start_s")),
    }
    samples: dict[str, list[tuple[float, list[float], bool | None]]] = {
        name: [] for name in intervals
    }
    for row in _rows(sensor_path):
        try:
            timestamp = float(row["host_use_monotonic_s"])
            wrench = [float(value) for value in row["corrected_wrench_n_nm"]]
        except (KeyError, TypeError, ValueError):
            continue
        if len(wrench) != 6 or not all(math.isfinite(value) for value in wrench):
            continue
        if not math.isfinite(timestamp):
            continue
        fresh = row.get("sensor_fresh")
        for name, (start_s, end_s) in intervals.items():
            if start_s is not None and end_s is not None and start_s <= timestamp < end_s:
                samples[name].append((timestamp, wrench, fresh if type(fresh) is bool else None))
                break

    frame_bounds: dict[str, list[list[float]]] = {name: [] for name in intervals}
    for row in _rows(robot_frames_path):
        try:
            timestamp = float(row["received_monotonic_s"])
            pose = [float(value) for value in row.get("tcp_pose_m_rad", [])]
        except (KeyError, TypeError, ValueError):
            continue
        if len(pose) != 6 or not all(math.isfinite(value) for value in pose):
            continue
        for name, (start_s, end_s) in intervals.items():
            if start_s is not None and end_s is not None and start_s <= timestamp < end_s:
                bounds = frame_bounds[name]
                if not bounds:
                    bounds.append(pose)
                elif len(bounds) == 1:
                    bounds.append(pose)
                else:
                    bounds[1] = pose
                break

    result: dict[str, Any] = {}
    for name, (start_s, end_s) in intervals.items():
        rows = samples[name]
        normal = [-wrench[2] for _, wrench, _ in rows]
        force_norm = [math.sqrt(sum(value * value for value in wrench[:3]))
                      for _, wrench, _ in rows]
        torque_norm = [math.sqrt(sum(value * value for value in wrench[3:]))
                       for _, wrench, _ in rows]
        fresh_values = [fresh for _, _, fresh in rows if fresh is not None]
        pose_rows = frame_bounds[name]
        result[name] = {
            "stage_interval_s": [start_s, end_s],
            "stage_duration_s": (
                None if start_s is None or end_s is None else max(0.0, end_s - start_s)
            ),
            "sensor_sample_count": len(rows),
            "sensor_observed_span_s": (
                None if len(rows) < 2 else rows[-1][0] - rows[0][0]
            ),
            "sensor_fresh_fraction": (
                None if not fresh_values else sum(fresh_values) / len(fresh_values)
            ),
            "normal_load_n": _distribution(normal),
            "force_norm_n": _distribution(force_norm),
            "torque_norm_nm": _distribution(torque_norm),
            "below_1n_fraction": (
                None if not normal else sum(value < 1.0 for value in normal) / len(normal)
            ),
            "tcp_z_start_m": None if not pose_rows else pose_rows[0][2],
            "tcp_z_end_m": None if not pose_rows else pose_rows[-1][2],
            "tcp_z_change_m": (
                None if len(pose_rows) < 2 else pose_rows[-1][2] - pose_rows[0][2]
            ),
            "interpretation": "KWR75 corrected feedback; stage may have a changing target, so no constant-5 N MAE is assigned",
        }
    return {
        "force_source": "Kunwei KWR75 corrected_wrench_n_nm",
        "normal_load_definition": "-corrected_wrench_n_nm[2]",
        "stages": result,
    }


def _controller_trace_diagnostics(path: Path) -> dict[str, Any]:
    rows = list(_rows(path))
    if not rows:
        return {"available": False, "reason": "sealed command timeline is empty"}
    schema = rows[0].get("schema")
    if schema not in CONTROLLER_TRACE_SCHEMAS:
        return {
            "available": True,
            "schema": schema,
            "per_frame_diagnostics_complete": False,
            "record_count": len(rows),
            "reason": "trace predates per-frame controller diagnostics",
        }
    boolean_fields = (
        "integral_state_clamped", "integral_authority_clamped",
        "integral_conditional_frozen", "integral_final_output_frozen",
        "integral_velocity_saturated", "integral_saturated",
        "integral_contact_gated", "integral_explicit_velocity_limit_active",
        "force_rise_guard", "force_preempt_warm_start",
        "force_preempt_direction_retry", "baseline_normal_projection_applied",
        "force_rise_inward_cap_applied", "host_slew_limited",
        "published_feedback_present", "published_task_space_saturated",
        "published_realization_saturated", "published_explicit_realization_limit",
    )
    numeric_fields = (
        "normal_force_measured_n", "normal_force_filtered_n",
        "normal_force_control_n", "force_target_n", "force_error_n",
        "integral_state_n_s", "integral_raw_state_n_s",
        "integral_effective_limit_n_s", "integral_i_term",
        "integral_raw_normal_velocity_m_s", "integral_applied_normal_velocity_m_s",
        "measured_force_norm_n", "published_same_direction_freeze_sign",
        "published_total_directional_shortfall_m_s", "published_requested_normal_m_s",
        "published_task_normal_m_s", "published_realized_normal_m_s",
        "publish_latency_s", "actual_dt_s", "solver_elapsed_s", "provider_elapsed_s",
    )
    missing = 0
    numeric: dict[str, list[float]] = {name: [] for name in numeric_fields}
    flag_counts = {name: 0 for name in boolean_fields}
    integral_at_limit_count = 0
    host_gaps: list[float] = []
    publish_gaps: list[float] = []
    largest_gap_rows: list[dict[str, Any]] = []
    previous_host: float | None = None
    previous_publish: float | None = None
    actual_qd_max_abs = 0.0
    actual_qd_max_norm = 0.0
    published_qdot_max_abs = 0.0
    actual_q_min = [math.inf] * 6
    actual_q_max = [-math.inf] * 6
    actual_qd_min = [math.inf] * 6
    actual_qd_max = [-math.inf] * 6
    for row in rows:
        row_numeric: dict[str, float] = {}
        if row.get("schema") != schema:
            missing += 1
        for name in boolean_fields:
            value = row.get(name)
            if type(value) is not bool:
                missing += 1
            elif value:
                flag_counts[name] += 1
        for name in numeric_fields:
            try:
                value = float(row.get(name, math.nan))
            except (TypeError, ValueError):
                value = math.nan
            if not math.isfinite(value):
                missing += 1
            else:
                numeric[name].append(value)
                row_numeric[name] = value
        limit = row_numeric.get("integral_effective_limit_n_s", math.nan)
        integral = row_numeric.get("integral_state_n_s", math.nan)
        if math.isfinite(limit) and limit > 0.0 and math.isfinite(integral) \
                and abs(integral) >= 0.98 * limit:
            integral_at_limit_count += 1
        try:
            q = [float(value) for value in row.get("actual_q_rad", [])]
            qd = [float(value) for value in row.get("actual_qd_rad_s", [])]
            qdot = [float(value) for value in row.get("published_packet_qdot_rad_s", [])]
        except (TypeError, ValueError):
            q, qd, qdot = [], [], []
        if len(q) != 6 or not all(math.isfinite(value) for value in q):
            missing += 1
        else:
            for index, value in enumerate(q):
                actual_q_min[index] = min(actual_q_min[index], value)
                actual_q_max[index] = max(actual_q_max[index], value)
        if len(qd) != 6 or not all(math.isfinite(value) for value in qd):
            missing += 1
        else:
            actual_qd_max_abs = max(actual_qd_max_abs, *(abs(value) for value in qd))
            actual_qd_max_norm = max(actual_qd_max_norm, math.sqrt(sum(value * value for value in qd)))
            for index, value in enumerate(qd):
                actual_qd_min[index] = min(actual_qd_min[index], value)
                actual_qd_max[index] = max(actual_qd_max[index], value)
        if len(qdot) != 6 or not all(math.isfinite(value) for value in qdot):
            missing += 1
        else:
            published_qdot_max_abs = max(published_qdot_max_abs, *(abs(value) for value in qdot))
        try:
            host_time = float(row.get("host_monotonic_s", math.nan))
            publish_time = float(row.get("published_monotonic_s", math.nan))
        except (TypeError, ValueError):
            host_time = publish_time = math.nan
        if not math.isfinite(host_time) or not math.isfinite(publish_time):
            missing += 1
        else:
            host_gap = None if previous_host is None else host_time - previous_host
            publish_gap = None if previous_publish is None else publish_time - previous_publish
            if host_gap is not None and host_gap > 0.0:
                host_gaps.append(host_gap)
            if publish_gap is not None and publish_gap > 0.0:
                publish_gaps.append(publish_gap)
            previous_host, previous_publish = host_time, publish_time
            if host_gap is not None and host_gap > 0.0:
                active_flags = [name for name in boolean_fields if row.get(name) is True]
                largest_gap_rows.append({
                    "packet_sequence": row.get("packet_sequence"),
                    "reference_time_s": row.get("reference_time_s"),
                    "host_gap_s": host_gap,
                    "publish_gap_s": publish_gap,
                    "actual_dt_s": row_numeric.get("actual_dt_s"),
                    "provider_elapsed_s": row_numeric.get("provider_elapsed_s"),
                    "solver_elapsed_s": row_numeric.get("solver_elapsed_s"),
                    "publish_latency_s": row_numeric.get("publish_latency_s"),
                    "normal_force_measured_n": row_numeric.get("normal_force_measured_n"),
                    "force_error_n": row_numeric.get("force_error_n"),
                    "integral_state_n_s": row_numeric.get("integral_state_n_s"),
                    "integral_effective_limit_n_s": row_numeric.get(
                        "integral_effective_limit_n_s"
                    ),
                    "host_slew_scale": row.get("host_slew_scale"),
                    "active_guards_and_saturation_flags": active_flags,
                    "max_abs_actual_qd_rad_s": (
                        max((abs(value) for value in qd), default=None)
                    ),
                    "max_abs_published_qdot_rad_s": (
                        max((abs(value) for value in qdot), default=None)
                    ),
                })
    force_target = numeric["force_target_n"]
    measured = numeric["normal_force_measured_n"]
    filtered = numeric["normal_force_filtered_n"]
    target_minus_measured = [target - value for target, value in zip(force_target, measured)]
    target_minus_filtered = [target - value for target, value in zip(force_target, filtered)]
    return {
        "available": True,
        "schema": schema,
        "record_count": len(rows),
        "per_frame_diagnostics_complete": missing == 0,
        "missing_or_invalid_field_count": missing,
        "normal_force_target_minus_measured_n": _distribution(target_minus_measured),
        "normal_force_target_minus_filtered_n": _distribution(target_minus_filtered),
        "controller_force_error_n": _distribution(numeric["force_error_n"]),
        "force_norm_n": _distribution(numeric["measured_force_norm_n"]),
        "integral_state_n_s": _distribution(numeric["integral_state_n_s"]),
        "integral_raw_state_n_s": _distribution(numeric["integral_raw_state_n_s"]),
        "integral_effective_limit_n_s": _distribution(numeric["integral_effective_limit_n_s"]),
        "integral_i_term": _distribution(numeric["integral_i_term"]),
        "integral_at_or_above_98pct_limit_ticks": integral_at_limit_count,
        "saturation_and_transition_ticks": flag_counts,
        "published_shortfall_m_s": _distribution(
            numeric["published_total_directional_shortfall_m_s"]
        ),
        "timing_s": {
            name: _distribution(numeric[name])
            for name in ("actual_dt_s", "solver_elapsed_s", "provider_elapsed_s", "publish_latency_s")
        },
        "inter_tick_timing_s": {
            "host_observation_gap": _distribution(host_gaps),
            "successful_publish_gap": _distribution(publish_gaps),
            "largest_host_gaps": sorted(
                largest_gap_rows, key=lambda value: value["host_gap_s"], reverse=True
            )[:10],
        },
        "path_force_signal_diagnostics": _controller_path_force_diagnostics(rows),
        "actual_qd_max_abs_rad_s": actual_qd_max_abs,
        "actual_qd_max_norm_rad_s": actual_qd_max_norm,
        "published_qdot_max_abs_rad_s": published_qdot_max_abs,
        "actual_q_joint_range_rad": [
            {"min": low, "max": high}
            if math.isfinite(low) and math.isfinite(high) else {"min": None, "max": None}
            for low, high in zip(actual_q_min, actual_q_max)
        ],
        "actual_qd_joint_range_rad_s": [
            {"min": low, "max": high}
            if math.isfinite(low) and math.isfinite(high) else {"min": None, "max": None}
            for low, high in zip(actual_qd_min, actual_qd_max)
        ],
        "limitations": "KWR75 is feedback, not an independent task-force reference; joint position and velocity vectors remain in the sealed per-frame rows.",
    }


def _host_intervals(path: Path, path_start: float | None) -> dict[str, float | int | None]:
    intervals: list[float] = []
    previous = None
    if path_start is not None:
        for value in _rows(path):
            if not isinstance(value, list) or len(value) != 2:
                continue
            stamp = float(value[0])
            if path_start <= stamp < path_start + 60.0:
                if previous is not None and stamp > previous:
                    intervals.append(stamp - previous)
                previous = stamp
    return {"observed_intervals": len(intervals),
            "p99_interval_s": (sorted(intervals)[math.ceil(.99 * len(intervals)) - 1]
                               if intervals else None),
            "max_interval_s": max(intervals) if intervals else None}


def _interval_summary(values: list[float]) -> dict[str, float | int | None]:
    ordered = sorted(float(value) for value in values if math.isfinite(float(value)) and value > 0.0)
    if not ordered:
        return {"interval_count": 0, "p99_interval_s": None, "max_interval_s": None}
    return {
        "interval_count": len(ordered),
        "p99_interval_s": ordered[math.ceil(.99 * len(ordered)) - 1],
        "max_interval_s": ordered[-1],
    }


def _raw_path_layers(
    segments: dict[str, Any],
    *,
    duration_s: float,
    protocol_id: str,
    attempt: dict[str, Any],
) -> dict[str, Any]:
    """Separate raw interface rates from the synchronized acceptance samples.

    TimingEvidence intentionally counts identities represented in fully
    accepted PathSample rows. This helper independently counts all formal
    host PATH references, raw RTDE frames, and distinct TP-consumed echoes in
    the controller-clock [0,duration) window.
    """
    timeline_receipt = segments.get("command_timeline") or {}
    frames_receipt = segments.get("robot_frames") or {}
    timeline_path = Path(timeline_receipt.get("path", ""))
    frames_path = Path(frames_receipt.get("path", ""))
    if not timeline_path.is_file() or not frames_path.is_file():
        return {"available": False, "reason": "sealed command timeline or RTDE segment is missing"}

    formal_refs: dict[int, float] = {}
    for row in _rows(timeline_path):
        if row.get("reference_phase") != "path":
            continue
        reference = row.get("reference_time_s")
        sequence = row.get("packet_sequence")
        if (isinstance(reference, (int, float)) and not isinstance(reference, bool)
                and math.isfinite(float(reference)) and 0.0 <= float(reference) < duration_s
                and isinstance(sequence, int) and not isinstance(sequence, bool)):
            formal_refs[int(sequence)] = float(reference)
    if not formal_refs:
        return {"available": False, "reason": "no formal PATH reference packets were retained"}

    rtde_path_frames: list[dict[str, Any]] = []
    path_origin_timestamp = None
    echo_first_time: dict[int, float] = {}
    for row in _rows(frames_path):
        echoes = row.get("integer_echoes") or {}
        state = int(echoes.get("26", echoes.get(26, -1)))
        if state != 25:
            continue
        timestamp = float(row.get("timestamp", math.nan))
        sequence = int(row.get("consumed_packet_sequence", -1))
        if not math.isfinite(timestamp):
            continue
        if sequence in formal_refs:
            if path_origin_timestamp is None:
                path_origin_timestamp = timestamp
            echo_first_time.setdefault(sequence, timestamp)
        rtde_path_frames.append({"timestamp": timestamp, "sequence": sequence})
    if path_origin_timestamp is None:
        return {"available": False, "reason": "no RTDE frame echoed a formal PATH reference"}

    stop_timestamp = path_origin_timestamp + duration_s
    window_frames = [
        row for row in rtde_path_frames
        if path_origin_timestamp <= row["timestamp"] < stop_timestamp
    ]
    unique_rtde_timestamps = sorted({row["timestamp"] for row in window_frames})
    echoes_in_window = sorted({
        row["sequence"] for row in window_frames
        if row["sequence"] in formal_refs
    })
    echo_times = sorted({
        sequence: echo_first_time[sequence]
        for sequence in echoes_in_window
        if sequence in echo_first_time
    }.values())
    rtde_intervals = [right - left for left, right in zip(
        unique_rtde_timestamps, unique_rtde_timestamps[1:]
    )]
    echo_intervals = [right - left for left, right in zip(echo_times, echo_times[1:])]
    skipped_sequence_steps = sum(
        max(0, right - left - 1)
        for left, right in zip(echoes_in_window, echoes_in_window[1:])
    )
    timing_evidence = ((attempt.get("evidence") or {}).get("metrics") or {}).get(
        "timing_evidence"
    ) or {}
    minimum_rate_hz = float(timing_evidence.get("minimum_rate_hz", 460.0))
    timing_attribution = attempt.get("timing_attribution") or {}
    host_summary = timing_attribution.get("host_interpublish") or {}
    host_count = host_summary.get("path_publishes")
    host_rate = timing_attribution.get("host_path_publish_rate_hz")
    if isinstance(host_count, int) and not isinstance(host_count, bool):
        host_rate = host_count / duration_s
    kunwei_count = timing_evidence.get("distinct_kunwei_frames")
    layer_counts = {
        "host_successful_path_publishes": host_count,
        "formal_host_path_reference_sequences": len(formal_refs),
        "raw_rtde_frames": len(unique_rtde_timestamps),
        "tp_consumed_formal_echoes": len(echoes_in_window),
        "kunwei_distinct_frames": kunwei_count,
    }
    layer_rates = {
        "host_successful_path_publishes": host_rate,
        "formal_host_path_reference_sequences": len(formal_refs) / duration_s,
        "raw_rtde_frames": len(unique_rtde_timestamps) / duration_s,
        "tp_consumed_formal_echoes": len(echoes_in_window) / duration_s,
        "kunwei_distinct_frames": (
            None if not isinstance(kunwei_count, int) else kunwei_count / duration_s
        ),
    }
    return {
        "available": True,
        "protocol_id": protocol_id,
        "formal_window_controller_timestamp_s": [path_origin_timestamp, stop_timestamp],
        "duration_s": duration_s,
        "minimum_layer_rate_hz": minimum_rate_hz,
        "counts": layer_counts,
        "rates_hz": layer_rates,
        "passes_minimum_rate": {
            name: (None if rate is None else rate >= minimum_rate_hz)
            for name, rate in layer_rates.items()
        },
        "formal_host_sequences_not_echoed_in_window": len(
            set(formal_refs).difference(echoes_in_window)
        ),
        "consumed_sequence_skips": skipped_sequence_steps,
        "raw_rtde_intervals": _interval_summary(rtde_intervals),
        "tp_echo_intervals": _interval_summary(echo_intervals),
        "synchronized_timing_evidence": {
            "scope": "distinct identities represented in accepted, fully synchronized PathSample rows; not a raw callback count",
            "layer_rates_hz": timing_evidence.get("layer_rates_hz"),
            "writer_sequences": timing_evidence.get("successful_writer_publishes"),
            "rtde_frames": timing_evidence.get("distinct_rtde_frames"),
            "tp_consumed_echoes": timing_evidence.get("distinct_tp_consumed_packet_echoes"),
            "kunwei_frames": timing_evidence.get("distinct_kunwei_frames"),
        },
        "interpretation": "Raw per-layer rates and synchronized accepted-sample rate are separate measurements; do not rename the latter as raw RTDE or host-send cadence.",
    }


def _attempt_timing_ledger(*, attempt_id: str, protocol: str,
                           stages: dict[str, float | None],
                           stationary_home: float | None,
                           started: float | None,
                           events: list[dict[str, Any]],
                           sequence: int, eligible: bool) -> dict[str, Any]:
    ledger = TaseR013TimingLedger(
        attempt_id=attempt_id, protocol_id=protocol, source_success=eligible,
    )
    readiness_start = _event_time(
        events, "READINESS_HOLD", "start", sequence,
        after=started, before=stages["path_start_s"],
    )
    readiness_end = _event_time(
        events, "READINESS_HOLD", "end", sequence,
        after=readiness_start, before=stages["path_start_s"],
    ) if readiness_start is not None else None
    candidates = {
        "HOME_CHECK": (started, "verified_preflight"),
        "CONTACT_SEARCH": (stages["contact_search_s"], "tp_state_20"),
        "CONTACT_LATCH": (stages["contact_latch_s"], "tp_state_21"),
        "QUALIFICATION": (stages["contact_latch_s"], "tp_state_21"),
        "READINESS_HOLD": (readiness_start, "start"),
        "ENTRY": (stages["entry_s"], "tp_state_25"),
        "PATH": (stages["path_start_s"], "first_consumed_path_command"),
        "STOP": (stages["returning_s"], "tp_state_40"),
        "UNLOAD_RELIEF": (stages["returning_s"], "tp_state_40"),
        "CLEARANCE": (stages["first_tp_ready_home_s"], "tp_state_78"),
        "HOME": (stationary_home, "verified"),
    }
    for stage in STAGES:
        stamp, event = candidates[stage]
        if stamp is None:
            ledger.missing_events.append({"source": "sealed_attempt", "stage": stage,
                                          "event": event, "timestamp_s": None})
            continue
        try:
            ledger.mark(stage, stamp, event=event)
            if stage == "READINESS_HOLD" and readiness_end is not None:
                ledger.mark(stage, readiness_end, event="end")
        except TimingLedgerError:
            ledger.missing_events.append({"source": "sealed_attempt", "stage": stage,
                                          "event": event, "timestamp_s": None,
                                          "reason": "timestamp_regressed"})
    return ledger.as_dict()


def _infer_path_start_from_command_timeline(path: Path) -> float | None:
    """Recover a missing PATH origin from the sealed v2 reference trace.

    This is diagnostic-only timestamp recovery. It never changes lifecycle
    completeness or attempt eligibility.
    """
    for row in _rows(path):
        if row.get("reference_phase") != "path":
            continue
        try:
            host_time = float(row["host_monotonic_s"])
            reference_time = float(row["reference_time_s"])
        except (KeyError, TypeError, ValueError):
            continue
        if (math.isfinite(host_time) and host_time >= 0.0
                and math.isfinite(reference_time) and reference_time >= 0.0):
            inferred = host_time - reference_time
            if math.isfinite(inferred) and inferred >= 0.0:
                return inferred
    return None


def reduce_attempt(attempt_dir: Path, *, verify_seal: bool = True,
                   events: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    attempt_dir = Path(attempt_dir)
    item = json.loads((attempt_dir / "attempt-result.json").read_text(encoding="utf-8"))
    seal = item.get("sealed_evidence") or {}
    segments = (_verify_segments(attempt_dir, item) if verify_seal
                else seal.get("segments") or {})
    binding = item.get("parameter_binding") or (item.get("state") or {}).get("parameter_binding") or {}
    protocol = binding.get("protocol_id")
    if protocol not in EXPECTED_PROTOCOLS:
        raise ValueError(f"unsupported attempt protocol: {protocol!r}: {attempt_dir}")
    sequence = int(item["sequence"])
    timing = item.get("timing") or {}
    start = timing.get("started_monotonic_s")
    path_start = timing.get("path_started_monotonic_s")
    path_start_source = "attempt_timing_receipt" if path_start is not None else "missing"
    if path_start is None:
        path_start = _infer_path_start_from_command_timeline(
            Path(segments["command_timeline"]["path"])
        )
        if path_start is not None:
            path_start_source = "sealed_command_timeline"
    receipt_home = timing.get("home_verified_monotonic_s")
    home_path = attempt_dir.parent.parent / "home_start_receipt.json"
    home_target = (json.loads(home_path.read_text(encoding="utf-8"))
                   if home_path.is_file() else None)
    service_info = seal.get("service_observations") or {}
    service_path = Path(service_info["path"]) if service_info.get("path") else None
    stages = _stages(Path(segments["robot_frames"]["path"]), path_start,
                     home_target, service_path)
    pre_path_contact = _pre_path_force_diagnostics(
        Path(segments["raw_sensor"]["path"]),
        Path(segments["robot_frames"]["path"]),
        stages,
    )
    events = events if events is not None else _session_events(attempt_dir.parent.parent)
    first_ready = stages["first_tp_ready_home_s"]
    stationary_home = timing.get("stationary_home_verified_monotonic_s")
    if stationary_home is None:
        stationary_home = _event_time(events, "HOME_SETTLE", "verified", sequence,
                                      after=first_ready)
    if stationary_home is None:
        stationary_home = stages["stationary_home_observed_s"]
    seal_complete = _event_time(events, "SEAL", "complete", sequence,
                                after=first_ready)
    finalize_start = timing.get("terminal_finalize_start_monotonic_s")
    finalize_end = timing.get("terminal_finalize_end_monotonic_s")
    if finalize_start is None:
        finalize_start = _event_time(events, "SERVICE", "start", sequence,
                                     after=stationary_home, before=receipt_home)
    if finalize_end is None and finalize_start is not None:
        finalize_end = _event_time(events, "ATTEMPT", "path_returned", sequence,
                                   after=finalize_start)
    metrics = ((item.get("evidence") or {}).get("metrics") or {})
    cadence = metrics.get("timing_evidence") or {}
    rates = cadence.get("layer_rates_hz") or {}
    timing_attribution = item.get("timing_attribution") or {}
    host_rate = timing_attribution.get("host_path_publish_rate_hz")
    rtde_rate = rates.get("rtde_frames")
    tp_rate = rates.get("tp_consumed_packet_echoes")
    classification = (
        "no_complete_path" if metrics.get("complete_bins") != 550
        else "host_publish_below_460" if isinstance(host_rate, (int, float)) and host_rate < 460
        else "rtde_below_460" if isinstance(rtde_rate, (int, float)) and rtde_rate < 460
        else "tp_echo_below_460" if isinstance(tp_rate, (int, float)) and tp_rate < 460
        else "observed_layers_at_least_460"
    )
    def delta(end, begin):
        return None if end is None or begin is None else float(end) - float(begin)
    timing_ledger = _attempt_timing_ledger(
        attempt_id=f"{attempt_dir.parent.parent.name}/{sequence:04d}",
        protocol=protocol, stages=stages, stationary_home=stationary_home,
        started=start, events=events, sequence=sequence,
        eligible=item.get("evidence_eligible") is True,
    )
    force_diagnostics = _force_diagnostics(
        Path(segments["raw_sensor"]["path"]), path_start
    )
    raw_formal_bins = force_diagnostics["raw_sensor_bin_mean_diagnostics"][
        "window_5_60_s"
    ]
    receipt_mae = metrics.get("normal_force_mae_n")
    force_diagnostics["raw_sensor_bin_mean_diagnostics"].update({
        "receipt_normal_force_mae_n": receipt_mae,
        "receipt_metric_eligible": item.get("evidence_eligible") is True,
        "raw_formal_bin_mean_minus_receipt_mae_n": (
            None if raw_formal_bins.get("bin_mean_mae_n") is None
            or not isinstance(receipt_mae, (int, float))
            else raw_formal_bins["bin_mean_mae_n"] - float(receipt_mae)
        ),
    })
    controller_trace = _controller_trace_diagnostics(
        Path(segments["command_timeline"]["path"])
    )
    filtered_formal = (
        controller_trace.get("path_force_signal_diagnostics", {})
        .get("signals", {})
        .get("normal_force_filtered_n", {})
        .get("windows", {})
        .get("window_5_60_s", {})
        .get("equal_weight_100ms_bins", {})
    )
    timeline_diagnostic = filtered_formal.get("bin_mean_mae_n")
    accepted_path_force = _synchronized_path_force_diagnostics(
        Path(segments["command_timeline"]["path"]),
        Path(segments["robot_frames"]["path"]),
        metrics=metrics,
        receipt_path_sample_count=(item.get("evidence") or {}).get("path_samples"),
    )
    controller_trace["all_writer_tick_score_diagnostic"] = {
        "all_tick_filtered_550_bin_mae_n": timeline_diagnostic,
        "receipt_formal_550_bin_mae_n": receipt_mae,
        "interpretation": (
            "This diagnostic bins every command_timeline control tick. It includes "
            "repeated/cached RTDE or TP joins and is not the accepted PathSample score."
        ),
    }
    controller_trace["official_score_crosscheck"] = (
        accepted_path_force.get("official_score_crosscheck")
        if accepted_path_force.get("available")
        else {
            "receipt_formal_550_bin_mae_n": receipt_mae,
            "accepted_join_reconstruction_available": False,
            "reason": accepted_path_force.get("reason"),
            "receipt_remains_authoritative": True,
        }
    )
    return {
        "schema": SCHEMA,
        "campaign": attempt_dir.parent.parent.parent.name,
        "session": attempt_dir.parent.parent.name,
        "sequence": sequence,
        "candidate_id": binding.get("candidate_id"),
        "protocol_id": protocol,
        "physical_dispatched": item.get("physical_dispatched") is True,
        "path_start_source": path_start_source,
        "path_complete": item.get("lifecycle", {}).get("path_complete") is True,
        "evidence_eligible": item.get("evidence_eligible") is True,
        "home_verified": item.get("lifecycle", {}).get("home_verified") is True,
        "seal_verified": bool(verify_seal),
        "top_sealed_matches_seal": item.get("lifecycle", {}).get("sealed") is seal.get("lifecycle", {}).get("sealed"),
        "formal_bin_mean_mae_n": metrics.get("normal_force_mae_n"),
        "formal_bins": metrics.get("complete_bins"),
        "timing_ledger": timing_ledger,
        "pre_path_contact_diagnostics": pre_path_contact,
        "timing": {**stages,
                   "stationary_home_verified_s": stationary_home,
                   "receipt_home_verified_s": receipt_home,
                   "terminal_finalize_start_s": finalize_start,
                   "terminal_finalize_end_s": finalize_end,
                   "seal_complete_s": seal_complete,
                   "pre_path_s": delta(path_start, start),
                   "path_to_returning_s": delta(stages["returning_s"], path_start),
                   "physical_return_s": delta(first_ready, stages["returning_s"]),
                   "ready_home_to_receipt_s": delta(receipt_home, first_ready),
                   "start_to_home_receipt_s": delta(receipt_home, start),
                   "start_to_seal_s": delta(seal_complete, start)},
        "cadence": {"host_publish_hz": host_rate,
                    "rtde_hz": rtde_rate,
                    "tp_echo_hz": tp_rate,
                    "kunwei_hz": rates.get("kunwei_frames"),
                    "max_fresh_gap_s": cadence.get("max_fresh_gap_s"),
                    "feedback_age_p99_s": cadence.get("feedback_age_p99_s"),
                    "classification": classification,
                    "rate_scope": "rtde_hz/tp_echo_hz above are unique identities in accepted synchronized PathSample rows; use raw_layer_measurements below for all raw interfaces",
                    "provider_compute_summary": timing_attribution.get("provider_compute"),
                    "host_interpublish_summary": timing_attribution.get("host_interpublish"),
                    **_host_intervals(Path(segments["published_packets"]["path"]), path_start)},
        "raw_layer_measurements": _raw_path_layers(
            segments,
            duration_s=float(metrics.get("path_duration_s", 60.0)),
            protocol_id=protocol,
            attempt=item,
        ),
        "synchronized_path_force_diagnostics": accepted_path_force,
        "controller_trace": controller_trace,
        "force": force_diagnostics,
        "solver_timing_aggregate_available": bool(
            (timing_attribution.get("provider_compute") or {}).get("solver_timed_calls", 0)
        ),
    }


def reduce_campaigns(roots: list[Path], *, verify_seal: bool = True) -> dict[str, Any]:
    rows = []
    sessions = {}
    for root in roots:
        for session_dir in sorted(Path(root).glob("session-[0-9][0-9]")):
            events = _session_events(session_dir)
            supervisor_path = session_dir / "supervisor-result.json"
            supervisor = (json.loads(supervisor_path.read_text(encoding="utf-8"))
                          if supervisor_path.is_file() else {})
            sessions[f"{Path(root).name}/{session_dir.name}"] = {
                "video_available": (supervisor.get("video_evidence") or {}).get("available") is True,
                "timing_ledger_error": supervisor.get("timing_ledger_error"),
                "program_stopped": supervisor.get("program_stopped"),
            }
            for attempt_dir in sorted((session_dir / "attempts").glob("[0-9][0-9][0-9][0-9]")):
                if (attempt_dir / "attempt-result.json").is_file():
                    rows.append(reduce_attempt(attempt_dir, verify_seal=verify_seal, events=events))
    def median(field):
        values = [row["timing"][field] for row in rows if row["timing"][field] is not None]
        return statistics.median(values) if values else None
    return {
        "schema": SCHEMA,
        "source_campaigns": [str(Path(root).resolve()) for root in roots],
        "summary": {
            "attempts": len(rows),
            "complete_paths": sum(row["path_complete"] for row in rows),
            "eligible": sum(row["evidence_eligible"] for row in rows),
            "below_460_rtde": sum(isinstance(row["cadence"]["rtde_hz"], (int, float))
                                  and row["cadence"]["rtde_hz"] < 460 for row in rows),
            "below_400_rtde": sum(isinstance(row["cadence"]["rtde_hz"], (int, float))
                                  and row["cadence"]["rtde_hz"] < 400 for row in rows),
            "host_publish_below_460": sum(
                row["cadence"]["classification"] == "host_publish_below_460" for row in rows
            ),
            "rtde_or_echo_below_460_with_host_at_least_460": sum(
                row["cadence"]["classification"] in {"rtde_below_460", "tp_echo_below_460"}
                for row in rows
            ),
            "tp_echo_below_460_with_host_and_rtde_at_least_460": sum(
                row["cadence"]["classification"] == "tp_echo_below_460" for row in rows
            ),
            "seal_field_mismatches": sum(not row["top_sealed_matches_seal"] for row in rows),
            "median_pre_path_s": median("pre_path_s"),
            "median_physical_return_s": median("physical_return_s"),
            "median_ready_home_to_receipt_s": median("ready_home_to_receipt_s"),
        },
        "sessions": sessions,
        "attempts": rows,
    }


def seal_session_diagnostic(session_dir: Path,
                            *, events: list[dict[str, Any]] | None = None,
                            protocol_id: str = "figure8_window60_r013_rate400_v1") -> dict[str, Any]:
    """Write the post-stop resident sidecar; original attempt files stay intact."""
    if protocol_id not in EXPECTED_PROTOCOLS:
        raise ValueError(f"unsupported resident timing protocol: {protocol_id}")
    session_dir = Path(session_dir)
    attempts_dir = session_dir / "attempts"
    rows = [
        reduce_attempt(path, events=events)
        for path in sorted(attempts_dir.glob("[0-9][0-9][0-9][0-9]"))
        if (path / "attempt-result.json").is_file()
    ]
    if not rows:
        raise ValueError("resident session has no sealed physical attempt")
    if any(row.get("protocol_id") != protocol_id for row in rows):
        raise ValueError("resident session attempts do not share the declared protocol")
    output = session_dir / "resident-evidence-diagnostic.json"
    temporary = output.with_suffix(".tmp")
    temporary.write_text(json.dumps({
        "schema": "tase.resident-session-evidence-diagnostic-v1",
        "attempts": rows,
    }, indent=2, sort_keys=True, allow_nan=False) + "\n", encoding="utf-8")
    temporary.replace(output)
    return {
        "schema": "tase.resident-timing-ledger-bundle-v1",
        "protocol_id": protocol_id,
        "attempt_count": len(rows),
        "home_to_home_count": sum(
            row["timing_ledger"]["durations_s"]["home_to_home_s"] is not None
            for row in rows
        ),
        "path": str(output),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--campaign-root", type=Path, action="append", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = reduce_campaigns(args.campaign_root)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x", encoding="utf-8") as target:
        json.dump(result, target, indent=2, sort_keys=True, allow_nan=False)
        target.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
