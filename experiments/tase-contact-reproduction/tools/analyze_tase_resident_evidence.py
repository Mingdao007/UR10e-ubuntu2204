"""Read-only reduction of sealed resident TASE attempts.

The output is a derived diagnostic. It never changes an attempt, its formal
5--60 s objective, or the original campaign ledger.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path
import statistics
from typing import Any

from tase_r013_timing_ledger import STAGES, TaseR013TimingLedger, TimingLedgerError


SCHEMA = "tase.resident-evidence-diagnostic-v1"
EXPECTED_PROTOCOLS = {
    "figure8_window60_r013_compat_v1",
    "figure8_window60_r013_rate400_v1",
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
                "sample_error_p95_n": None, "peak_normal_n": None,
                "below_1n_fraction": None, "below_1n_runs": 0,
                "longest_below_1n_run_s": None}
    errors = [abs(value - 5.0) for _, value in values]
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
        "peak_normal_n": max(value for _, value in values),
        "below_1n_fraction": sum(value < 1.0 for _, value in values) / len(values),
        "below_1n_runs": runs,
        "longest_below_1n_run_s": longest,
    }


def _force_diagnostics(path: Path, path_start: float | None) -> dict[str, Any]:
    windows: list[list[tuple[float, float]]] = [[], []]
    if path_start is not None:
        for row in _rows(path):
            wrench = row.get("corrected_wrench_n_nm")
            t = float(row.get("host_use_monotonic_s", -math.inf)) - path_start
            if wrench is not None and 0 <= t < 60.0:
                windows[0 if t < 5.0 else 1].append((t, -float(wrench[2])))
    return {"window_0_5_s": _force_window(windows[0]),
            "window_5_60_s": _force_window(windows[1]),
            "interpretation": "raw corrected samples; below 1 N is a contact-loss proxy, not visual proof; independent of formal bin-mean MAE"}


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
    receipt_home = timing.get("home_verified_monotonic_s")
    home_path = attempt_dir.parent.parent / "home_start_receipt.json"
    home_target = (json.loads(home_path.read_text(encoding="utf-8"))
                   if home_path.is_file() else None)
    service_info = seal.get("service_observations") or {}
    service_path = Path(service_info["path"]) if service_info.get("path") else None
    stages = _stages(Path(segments["robot_frames"]["path"]), path_start,
                     home_target, service_path)
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
    return {
        "schema": SCHEMA,
        "campaign": attempt_dir.parent.parent.parent.name,
        "session": attempt_dir.parent.parent.name,
        "sequence": sequence,
        "candidate_id": binding.get("candidate_id"),
        "protocol_id": protocol,
        "physical_dispatched": item.get("physical_dispatched") is True,
        "path_complete": item.get("lifecycle", {}).get("path_complete") is True,
        "evidence_eligible": item.get("evidence_eligible") is True,
        "home_verified": item.get("lifecycle", {}).get("home_verified") is True,
        "seal_verified": bool(verify_seal),
        "top_sealed_matches_seal": item.get("lifecycle", {}).get("sealed") is seal.get("lifecycle", {}).get("sealed"),
        "formal_bin_mean_mae_n": metrics.get("normal_force_mae_n"),
        "formal_bins": metrics.get("complete_bins"),
        "timing_ledger": timing_ledger,
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
                    "provider_compute_summary": timing_attribution.get("provider_compute"),
                    "host_interpublish_summary": timing_attribution.get("host_interpublish"),
                    **_host_intervals(Path(segments["published_packets"]["path"]), path_start)},
        "force": _force_diagnostics(Path(segments["raw_sensor"]["path"]), path_start),
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
                            *, events: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """Write the post-stop resident sidecar; original attempt files stay intact."""
    session_dir = Path(session_dir)
    attempts_dir = session_dir / "attempts"
    rows = [
        reduce_attempt(path, events=events)
        for path in sorted(attempts_dir.glob("[0-9][0-9][0-9][0-9]"))
        if (path / "attempt-result.json").is_file()
    ]
    if not rows:
        raise ValueError("resident rate400 has no sealed physical attempt")
    output = session_dir / "resident-evidence-diagnostic.json"
    temporary = output.with_suffix(".tmp")
    temporary.write_text(json.dumps({
        "schema": "tase.resident-session-evidence-diagnostic-v1",
        "attempts": rows,
    }, indent=2, sort_keys=True, allow_nan=False) + "\n", encoding="utf-8")
    temporary.replace(output)
    return {
        "schema": "tase.resident-timing-ledger-bundle-v1",
        "protocol_id": "figure8_window60_r013_rate400_v1",
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
