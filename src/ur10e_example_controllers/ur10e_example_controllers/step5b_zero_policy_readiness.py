from __future__ import annotations

import argparse
import csv
import json
import math
import socket
import statistics
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from .kunwei_force_gate import FORCE_KG_TO_N, MOMENT_KG_M_TO_NM, START_STREAM, STOP_STREAM, parse_frame, pop_frames


WORKSPACE = Path(__file__).resolve().parents[3]
EXPERIMENT = WORKSPACE / "experiments" / "tase-contact-reproduction"
RUN_ROOT = EXPERIMENT / "runs"
TEXTBOOK_SPEC = EXPERIMENT / "config" / "local_control_textbook_spec.json"
CONTACT_CONTRACT = EXPERIMENT / "UR_FORCE_FRAME_CONTRACT.md"
FIELDS = ("fx", "fy", "fz", "mx", "my", "mz")
STEP5B_TARGET_FORCE_N = 5.0
STEP5B_CONTACT_FORCE_NORM_LATCH_N = 1.5
STEP5B_CONTACT_NORMAL_NEGATIVE_LATCH_N = -1.0
STEP5B_NORMAL_FILTER_MIN_FORCE_N = 2.0
STEP5B_RAW_NORMAL_GUARD_N = 50.0
STEP5B_FORCE_NORM_GUARD_N = 60.0
STEP5B_TORQUE_GUARD_NM = 3.0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the Step5b zero-policy/readiness gate without robot motion.")
    parser.add_argument("--sensor-ip", default="192.168.50.25")
    parser.add_argument("--sensor-port", type=int, default=5152)
    parser.add_argument("--sample-duration-s", type=float, default=2.0)
    parser.add_argument("--baseline-window-s", type=float, default=1.0)
    parser.add_argument("--validation-window-s", type=float, default=0.5)
    parser.add_argument("--min-samples", type=int, default=1000)
    parser.add_argument("--recent-rate-hz-min", type=float, default=200.0)
    parser.add_argument("--latest-sample-max-age-s", type=float, default=0.25)
    parser.add_argument("--force-norm-zeroed-mean-max-n", type=float, default=STEP5B_CONTACT_FORCE_NORM_LATCH_N)
    parser.add_argument("--force-norm-zeroed-max-n", type=float, default=STEP5B_CONTACT_FORCE_NORM_LATCH_N)
    parser.add_argument(
        "--normal-load-zeroed-negative-min-n",
        type=float,
        default=STEP5B_CONTACT_NORMAL_NEGATIVE_LATCH_N,
    )
    parser.add_argument(
        "--normal-load-zeroed-abs-max-n",
        type=float,
        default=abs(STEP5B_CONTACT_NORMAL_NEGATIVE_LATCH_N),
    )
    parser.add_argument("--normal-axis", choices=("fx", "fy", "fz"), default="fz")
    parser.add_argument("--normal-sign", choices=(-1.0, 1.0), type=float, default=1.0)
    parser.add_argument("--connect-timeout-s", type=float, default=3.0)
    parser.add_argument("--recv-timeout-s", type=float, default=0.25)
    parser.add_argument("--output-dir", type=Path, default=None)
    parser.add_argument("--no-start-command", action="store_true")
    parser.add_argument("--no-stop-command", action="store_true")
    args = parser.parse_args(argv)

    output_dir = args.output_dir or default_output_dir()
    output_dir.mkdir(parents=True, exist_ok=True)
    raw_frames_path = output_dir / "kunwei_raw_frames.bin"
    samples_csv_path = output_dir / "kunwei_samples.csv"
    summary_path = output_dir / "summary.json"

    failure_reason: str | None = None
    stream_metrics: dict[str, Any]
    samples: list[tuple[float, tuple[float, ...]]] = []
    try:
        samples, stream_metrics = collect_kunwei_samples(
            sensor_ip=args.sensor_ip,
            sensor_port=args.sensor_port,
            duration_s=args.sample_duration_s,
            connect_timeout_s=args.connect_timeout_s,
            recv_timeout_s=args.recv_timeout_s,
            raw_frames_path=raw_frames_path,
            send_start_command=not args.no_start_command,
            send_stop_command=not args.no_stop_command,
        )
    except OSError as exc:
        stream_metrics = base_stream_metrics(args, raw_frames_path)
        failure_reason = f"{type(exc).__name__}: {exc}"

    summary, rows = build_summary(
        samples=samples,
        stream_metrics=stream_metrics,
        output_dir=output_dir,
        raw_frames_path=raw_frames_path,
        samples_csv_path=samples_csv_path,
        args=args,
        collection_failure=failure_reason,
    )
    write_samples_csv(samples_csv_path, rows)
    write_json(summary_path, summary)
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0 if summary["ok"] else 3


def default_output_dir() -> Path:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return RUN_ROOT / f"step5b_zero_policy_readiness_{stamp}"


def collect_kunwei_samples(
    *,
    sensor_ip: str,
    sensor_port: int,
    duration_s: float,
    connect_timeout_s: float,
    recv_timeout_s: float,
    raw_frames_path: Path,
    send_start_command: bool,
    send_stop_command: bool,
) -> tuple[list[tuple[float, tuple[float, ...]]], dict[str, Any]]:
    metrics = {
        "sensor_endpoint": f"{sensor_ip}:{sensor_port}",
        "stream_start_command_sent": False,
        "stream_stop_command_sent": False,
        "bytes_received": 0,
        "packets_received": 0,
        "dropped_sync_bytes": 0,
        "parse_errors": 0,
    }
    samples: list[tuple[float, tuple[float, ...]]] = []
    buffer = bytearray()
    sock: socket.socket | None = None
    raw_handle = raw_frames_path.open("wb")
    try:
        sock = socket.create_connection((sensor_ip, sensor_port), timeout=connect_timeout_s)
        sock.settimeout(recv_timeout_s)
        if send_start_command:
            sock.sendall(START_STREAM)
            metrics["stream_start_command_sent"] = True
        collection_start_mono = time.monotonic()
        deadline = collection_start_mono + duration_s
        while time.monotonic() < deadline:
            try:
                chunk = sock.recv(8192)
            except socket.timeout:
                continue
            if not chunk:
                metrics["socket_closed"] = True
                break
            metrics["bytes_received"] += len(chunk)
            metrics["packets_received"] += 1
            buffer.extend(chunk)
            frames, dropped = pop_frames(buffer)
            metrics["dropped_sync_bytes"] += dropped
            for frame in frames:
                try:
                    values = parse_frame(frame)
                except ValueError:
                    metrics["parse_errors"] += 1
                    continue
                raw_handle.write(frame)
                samples.append((time.monotonic() - collection_start_mono, values))
        metrics["collection_elapsed_s"] = time.monotonic() - collection_start_mono
    finally:
        raw_handle.close()
        if sock is not None:
            if send_stop_command:
                try:
                    sock.sendall(STOP_STREAM)
                    metrics["stream_stop_command_sent"] = True
                except OSError:
                    pass
            try:
                sock.close()
            except OSError:
                pass
    return samples, metrics


def base_stream_metrics(args: argparse.Namespace, raw_frames_path: Path) -> dict[str, Any]:
    return {
        "sensor_endpoint": f"{args.sensor_ip}:{args.sensor_port}",
        "stream_start_command_sent": False,
        "stream_stop_command_sent": False,
        "bytes_received": 0,
        "packets_received": 0,
        "dropped_sync_bytes": 0,
        "parse_errors": 0,
        "raw_frames_path": str(raw_frames_path),
        "collection_elapsed_s": 0.0,
    }


def build_summary(
    *,
    samples: list[tuple[float, tuple[float, ...]]],
    stream_metrics: dict[str, Any],
    output_dir: Path,
    raw_frames_path: Path,
    samples_csv_path: Path,
    args: argparse.Namespace,
    collection_failure: str | None = None,
) -> tuple[dict[str, Any], list[dict[str, float]]]:
    thresholds = {
        "force_norm_zeroed_mean_max_n": float(args.force_norm_zeroed_mean_max_n),
        "force_norm_zeroed_max_n": float(args.force_norm_zeroed_max_n),
        "normal_load_zeroed_negative_min_n": float(args.normal_load_zeroed_negative_min_n),
        "normal_load_zeroed_abs_max_n": float(args.normal_load_zeroed_abs_max_n),
    }
    rows: list[dict[str, float]] = []
    sample_times = [stamp for stamp, _ in samples]
    latest_age_s = None
    sample_window_s = 0.0
    recent_rate_hz = None
    collection_elapsed_s = float(stream_metrics.get("collection_elapsed_s") or args.sample_duration_s)
    if sample_times:
        sample_window_s = sample_times[-1] - sample_times[0] if len(sample_times) > 1 else 0.0
        recent_rate_hz = (len(sample_times) - 1) / sample_window_s if sample_window_s > 0.0 and len(sample_times) > 1 else None
        latest_age_s = max(0.0, collection_elapsed_s - sample_times[-1])

    baseline_samples = [sample for stamp, sample in samples if stamp <= args.baseline_window_s]
    validation_start = max(0.0, float(args.sample_duration_s) - float(args.validation_window_s))
    validation_samples = [sample for stamp, sample in samples if stamp >= validation_start]
    baseline = mean_tuple(baseline_samples) if baseline_samples else None
    zeroed_force_norms: list[float] = []
    zeroed_normal_loads: list[float] = []
    raw_force_norms: list[float] = []

    for stamp, sample in samples:
        raw_si = raw_to_si(sample)
        raw_force_norm = force_norm(raw_si)
        raw_force_norms.append(raw_force_norm)
        if baseline is None:
            zeroed_si = (0.0, 0.0, 0.0, 0.0, 0.0, 0.0)
        else:
            zeroed_si = tuple(value - base for value, base in zip(raw_si, raw_to_si(baseline)))
        normal_load = normal_component(zeroed_si, args.normal_axis, args.normal_sign)
        zeroed_force_norm = force_norm(zeroed_si)
        if stamp >= validation_start:
            zeroed_force_norms.append(zeroed_force_norm)
            zeroed_normal_loads.append(normal_load)
        rows.append(
            {
                "t_s": stamp,
                "fx_raw_n": raw_si[0],
                "fy_raw_n": raw_si[1],
                "fz_raw_n": raw_si[2],
                "mx_raw_nm": raw_si[3],
                "my_raw_nm": raw_si[4],
                "mz_raw_nm": raw_si[5],
                "force_norm_raw_n": raw_force_norm,
                "fx_zeroed_n": zeroed_si[0],
                "fy_zeroed_n": zeroed_si[1],
                "fz_zeroed_n": zeroed_si[2],
                "normal_load_zeroed_n": normal_load,
                "force_norm_zeroed_n": zeroed_force_norm,
            }
        )

    force_norm_zeroed_mean = statistics.fmean(zeroed_force_norms) if zeroed_force_norms else None
    force_norm_zeroed_max = max(zeroed_force_norms) if zeroed_force_norms else None
    normal_load_min = min(zeroed_normal_loads) if zeroed_normal_loads else None
    normal_load_abs_max = max((abs(value) for value in zeroed_normal_loads), default=None)
    failures = readiness_failures(
        collection_failure=collection_failure,
        samples=len(samples),
        min_samples=int(args.min_samples),
        recent_rate_hz=recent_rate_hz,
        recent_rate_hz_min=float(args.recent_rate_hz_min),
        latest_age_s=latest_age_s,
        latest_sample_max_age_s=float(args.latest_sample_max_age_s),
        parse_errors=int(stream_metrics.get("parse_errors", 0)),
        dropped_sync_bytes=int(stream_metrics.get("dropped_sync_bytes", 0)),
        baseline_samples=len(baseline_samples),
        validation_samples=len(validation_samples),
        force_norm_zeroed_mean=force_norm_zeroed_mean,
        force_norm_zeroed_max=force_norm_zeroed_max,
        normal_load_min=normal_load_min,
        normal_load_abs_max=normal_load_abs_max,
        thresholds=thresholds,
    )
    ok = not failures
    summary = {
        "ok": ok,
        "role": "step5b_zero_policy_readiness_gate",
        "stage_id": "step5_contact_cycloid_baseline_v1",
        "motion_authorized": False,
        "contact_search_authorized": False,
        "bridge_start_authorized": False,
        "tp_play_authorized": False,
        "force_source": "kunwei_software_baselined_stream",
        "zero_policy": {
            "ur_zero_ftsensor_called": False,
            "kunwei_hardware_tare_or_config_written": False,
            "software_baseline_subtraction": True,
            "baseline_timing": "before_contact_search",
            "manual_no_preload_required": True,
        },
        "kunwei_stream": {
            "sensor_endpoint": stream_metrics.get("sensor_endpoint"),
            "sample_duration_s": float(args.sample_duration_s),
            "collection_elapsed_s": collection_elapsed_s,
            "min_samples": int(args.min_samples),
            "samples": len(samples),
            "recent_rate_hz": recent_rate_hz,
            "recent_rate_hz_min": float(args.recent_rate_hz_min),
            "latest_sample_age_s": latest_age_s,
            "latest_sample_max_age_s": float(args.latest_sample_max_age_s),
            "parse_errors": int(stream_metrics.get("parse_errors", 0)),
            "dropped_sync_bytes": int(stream_metrics.get("dropped_sync_bytes", 0)),
            "stream_start_command_sent": bool(stream_metrics.get("stream_start_command_sent", False)),
            "stream_stop_command_sent": bool(stream_metrics.get("stream_stop_command_sent", False)),
            "bytes_received": int(stream_metrics.get("bytes_received", 0)),
            "packets_received": int(stream_metrics.get("packets_received", 0)),
        },
        "baseline_window": {
            "duration_s": float(args.baseline_window_s),
            "samples": len(baseline_samples),
            "force_norm_raw_mean_n": statistics.fmean(raw_force_norms[: len(baseline_samples)]) if baseline_samples else None,
            "force_norm_raw_max_n": max(raw_force_norms[: len(baseline_samples)]) if baseline_samples else None,
        },
        "zeroed_validation_window": {
            "duration_s": float(args.validation_window_s),
            "samples": len(validation_samples),
            "force_norm_zeroed_mean_n": force_norm_zeroed_mean,
            "force_norm_zeroed_max_n": force_norm_zeroed_max,
            "normal_load_zeroed_min_n": normal_load_min,
            "normal_load_zeroed_abs_max_n": normal_load_abs_max,
        },
        "thresholds": thresholds,
        "step5b_textbook_parameters": {
            "stage_id": "step5_contact_cycloid_baseline_v1",
            "target_force_n": STEP5B_TARGET_FORCE_N,
            "contact_latch": {
                "force_norm_gt_n": STEP5B_CONTACT_FORCE_NORM_LATCH_N,
                "normal_force_lte_n": STEP5B_CONTACT_NORMAL_NEGATIVE_LATCH_N,
            },
            "normal_filter": {
                "mode": "filtered_live",
                "min_force_n": STEP5B_NORMAL_FILTER_MIN_FORCE_N,
            },
            "hard_guards": {
                "raw_normal_guard_n": STEP5B_RAW_NORMAL_GUARD_N,
                "force_norm_guard_n": STEP5B_FORCE_NORM_GUARD_N,
                "torque_guard_nm": STEP5B_TORQUE_GUARD_NM,
            },
            "sources": [
                str(TEXTBOOK_SPEC),
                str(EXPERIMENT / "programs" / "step5" / "step5b_contact_cycloid_baseline_v1.script"),
                str(EXPERIMENT / "programs" / "step5" / "step5b_contact_cycloid_baseline_v1.txt"),
                str(EXPERIMENT / "config" / "step5_stage_table.json"),
            ],
        },
        "threshold_provenance": {
            "force_norm_zeroed_mean_max_n": "readiness-only bound using Step5b contact latch force_norm_gt_n=1.5",
            "force_norm_zeroed_max_n": "Step5b contact latch force_norm_gt_n=1.5",
            "normal_load_zeroed_negative_min_n": "Step5b contact latch normal_force_lte_n=-1.0",
            "normal_load_zeroed_abs_max_n": "symmetric no-preload proxy derived from abs(Step5b normal_force_lte_n=-1.0)",
            "normal_filter_min_force_n": "Step5b filtered-live normal activation parameter, recorded but not used as zero-baseline pass threshold",
        },
        "normal_axis_policy": {
            "pre_contact_reaction_normal_latched": False,
            "normal_load_zeroed_n": "configured_axis_no_preload_proxy_before_contact_search",
            "normal_axis": args.normal_axis,
            "normal_sign": args.normal_sign,
        },
        "contact_force_frame_contract": {
            "contract_path": str(CONTACT_CONTRACT),
            "reaction_normal_for_load": True,
            "approach_normal_for_posture_and_press": True,
            "raw_force_orientation_target_forbidden": True,
        },
        "textbook_alignment": {
            "local_control_textbook_spec": str(TEXTBOOK_SPEC),
            "zero_policy_classification": "preserved",
        },
        "artifacts": {
            "run_dir": str(output_dir),
            "raw_frames_path": str(raw_frames_path),
            "samples_csv_path": str(samples_csv_path),
            "summary_path": str(output_dir / "summary.json"),
        },
        "failure_reason": None if ok else ";".join(failures),
    }
    return summary, rows


def readiness_failures(
    *,
    collection_failure: str | None,
    samples: int,
    min_samples: int,
    recent_rate_hz: float | None,
    recent_rate_hz_min: float,
    latest_age_s: float | None,
    latest_sample_max_age_s: float,
    parse_errors: int,
    dropped_sync_bytes: int,
    baseline_samples: int,
    validation_samples: int,
    force_norm_zeroed_mean: float | None,
    force_norm_zeroed_max: float | None,
    normal_load_min: float | None,
    normal_load_abs_max: float | None,
    thresholds: dict[str, float],
) -> list[str]:
    failures: list[str] = []
    if collection_failure:
        failures.append(f"collection_failure:{collection_failure}")
    if samples < min_samples:
        failures.append("insufficient_samples")
    if recent_rate_hz is None or recent_rate_hz < recent_rate_hz_min:
        failures.append("recent_rate_below_min")
    if latest_age_s is None or latest_age_s > latest_sample_max_age_s:
        failures.append("latest_sample_stale")
    if parse_errors != 0:
        failures.append("parse_errors")
    if dropped_sync_bytes != 0:
        failures.append("dropped_sync_bytes")
    if baseline_samples <= 0:
        failures.append("empty_baseline_window")
    if validation_samples <= 0:
        failures.append("empty_zeroed_validation_window")
    if force_norm_zeroed_mean is None or force_norm_zeroed_mean > thresholds["force_norm_zeroed_mean_max_n"]:
        failures.append("force_norm_zeroed_mean_threshold")
    if force_norm_zeroed_max is None or force_norm_zeroed_max > thresholds["force_norm_zeroed_max_n"]:
        failures.append("force_norm_zeroed_max_threshold")
    if normal_load_min is None or normal_load_min <= thresholds["normal_load_zeroed_negative_min_n"]:
        failures.append("normal_load_zeroed_negative_latch_threshold")
    if normal_load_abs_max is None or normal_load_abs_max > thresholds["normal_load_zeroed_abs_max_n"]:
        failures.append("normal_load_zeroed_abs_threshold")
    return failures


def mean_tuple(samples: list[tuple[float, ...]]) -> tuple[float, ...]:
    width = float(len(samples))
    return tuple(sum(sample[index] for sample in samples) / width for index in range(len(FIELDS)))


def raw_to_si(sample: tuple[float, ...]) -> tuple[float, ...]:
    return (
        sample[0] * FORCE_KG_TO_N,
        sample[1] * FORCE_KG_TO_N,
        sample[2] * FORCE_KG_TO_N,
        sample[3] * MOMENT_KG_M_TO_NM,
        sample[4] * MOMENT_KG_M_TO_NM,
        sample[5] * MOMENT_KG_M_TO_NM,
    )


def force_norm(values_si: tuple[float, ...]) -> float:
    return math.sqrt(sum(value * value for value in values_si[:3]))


def normal_component(values_si: tuple[float, ...], axis: str, sign: float) -> float:
    index = {"fx": 0, "fy": 1, "fz": 2}[axis]
    return sign * values_si[index]


def write_samples_csv(path: Path, rows: list[dict[str, float]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = [
        "t_s",
        "fx_raw_n",
        "fy_raw_n",
        "fz_raw_n",
        "mx_raw_nm",
        "my_raw_nm",
        "mz_raw_nm",
        "force_norm_raw_n",
        "fx_zeroed_n",
        "fy_zeroed_n",
        "fz_zeroed_n",
        "normal_load_zeroed_n",
        "force_norm_zeroed_n",
    ]
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())
