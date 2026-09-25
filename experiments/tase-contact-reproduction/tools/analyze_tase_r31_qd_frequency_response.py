#!/usr/bin/env python3
"""Describe the sealed R31 target_qd -> actual_qd frequency response.

This is an offline telemetry diagnostic on exact consumed-packet joins. Gaps
split the signal into contiguous runs; the analyzer does not interpolate them
and does not change the official all-sample motion gate.
"""
from __future__ import annotations

import argparse
import json
import math
import statistics
from pathlib import Path
from typing import Any, Sequence

import numpy as np
from scipy.signal import csd, welch

from analyze_tase_r31_cartesian_tracking import verify_sealed_sources


NOMINAL_DT_S = 0.002
CONTIGUOUS_DT_TOLERANCE_S = 0.0005
N_PER_SEG = 1024
OVERLAP = N_PER_SEG // 2
FREQUENCY_BANDS_HZ = {
    "0.5_to_5": (0.5, 5.0),
    "5_to_15": (5.0, 15.0),
    "15_to_50": (15.0, 50.0),
}


class SpectrumAnalysisError(ValueError):
    """Sealed RTDE sequence cannot support a valid spectral estimate."""


def _finite(value: Any, label: str) -> float:
    result = float(value)
    if not math.isfinite(result):
        raise SpectrumAnalysisError(f"{label} must be finite")
    return result


def _vector(value: Any, label: str) -> tuple[float, ...]:
    if not isinstance(value, (list, tuple)) or len(value) != 6:
        raise SpectrumAnalysisError(f"{label} must contain six values")
    return tuple(_finite(item, label) for item in value)


def split_contiguous_indices(
    packet_sequences: Sequence[int], timestamps_s: Sequence[float], *,
    nominal_dt_s: float = NOMINAL_DT_S,
) -> list[tuple[int, int]]:
    """Return all half-open spans with continuous RTDE time; packet gaps remain metadata."""
    if len(packet_sequences) != len(timestamps_s):
        raise SpectrumAnalysisError("sequence and timestamp lengths differ")
    if len(packet_sequences) < 2:
        raise SpectrumAnalysisError("at least two joined rows are required")
    if not math.isfinite(nominal_dt_s) or nominal_dt_s <= 0.0:
        raise SpectrumAnalysisError("nominal_dt_s must be positive and finite")
    stamps = [_finite(value, "controller timestamp") for value in timestamps_s]
    seqs = [int(value) for value in packet_sequences]
    if any(b <= a for a, b in zip(seqs, seqs[1:])):
        raise SpectrumAnalysisError("consumed packet sequences must strictly increase")
    if any(b <= a for a, b in zip(stamps, stamps[1:])):
        raise SpectrumAnalysisError("controller timestamps must strictly increase")

    spans: list[tuple[int, int]] = []
    start = 0
    for index in range(1, len(seqs)):
        dt = stamps[index] - stamps[index - 1]
        contiguous = abs(dt - nominal_dt_s) <= CONTIGUOUS_DT_TOLERANCE_S
        if not contiguous:
            if index > start:
                spans.append((start, index))
            start = index
    if len(seqs) > start:
        spans.append((start, len(seqs)))
    if not spans:
        raise SpectrumAnalysisError("no continuous RTDE spans were formed")
    return spans


def _welch_pair(
    target: np.ndarray,
    actual: np.ndarray,
    spans: Sequence[tuple[int, int]],
    *,
    sample_rate_hz: float,
) -> dict[str, Any]:
    if target.ndim != 2 or actual.shape != target.shape or target.shape[1] != 6:
        raise SpectrumAnalysisError("target_qd and actual_qd must have matching N x 6 shapes")
    if len(target) < N_PER_SEG:
        raise SpectrumAnalysisError("not enough samples for spectral estimate")
    frequency_reference: np.ndarray | None = None
    pxx_acc = np.zeros((N_PER_SEG // 2 + 1, 6), dtype=float)
    pyy_acc = np.zeros_like(pxx_acc)
    pxy_acc = np.zeros((N_PER_SEG // 2 + 1, 6), dtype=complex)
    total_windows = 0

    for start, stop in spans:
        if stop - start < N_PER_SEG:
            continue
        window_count = 1 + (stop - start - N_PER_SEG) // (N_PER_SEG - OVERLAP)
        for joint in range(6):
            f, pxx = welch(
                target[start:stop, joint], fs=sample_rate_hz,
                nperseg=N_PER_SEG, noverlap=OVERLAP, detrend="constant",
            )
            _, pyy = welch(
                actual[start:stop, joint], fs=sample_rate_hz,
                nperseg=N_PER_SEG, noverlap=OVERLAP, detrend="constant",
            )
            _, pxy = csd(
                target[start:stop, joint], actual[start:stop, joint],
                fs=sample_rate_hz, nperseg=N_PER_SEG, noverlap=OVERLAP,
                detrend="constant",
            )
            if frequency_reference is None:
                frequency_reference = f
            elif not np.array_equal(frequency_reference, f):
                raise SpectrumAnalysisError("Welch frequency grids differ")
            pxx_acc[:, joint] += pxx * window_count
            pyy_acc[:, joint] += pyy * window_count
            pxy_acc[:, joint] += pxy * window_count
        total_windows += window_count
    if total_windows == 0 or frequency_reference is None:
        raise SpectrumAnalysisError("no Welch windows were accumulated")

    pxx_acc /= total_windows
    pyy_acc /= total_windows
    pxy_acc /= total_windows
    per_joint: list[dict[str, Any]] = []
    for joint in range(6):
        pxx = pxx_acc[:, joint]
        pyy = pyy_acc[:, joint]
        pxy = pxy_acc[:, joint]
        coherence = np.clip(np.abs(pxy) ** 2 / np.maximum(pxx * pyy, np.finfo(float).tiny), 0.0, 1.0)
        transfer = pxy / np.maximum(pxx, np.finfo(float).tiny)
        bands: dict[str, Any] = {}
        for name, (low_hz, high_hz) in FREQUENCY_BANDS_HZ.items():
            mask = (
                (frequency_reference >= low_hz)
                & (frequency_reference < high_hz)
                & (pxx > np.finfo(float).tiny)
            )
            if not np.any(mask):
                bands[name] = {"frequency_bins": 0, "gain_db_median": None, "coherence_median": None, "phase_deg_median": None}
                continue
            gain_db = 20.0 * np.log10(np.maximum(np.abs(transfer[mask]), np.finfo(float).tiny))
            phase_deg = np.rad2deg(np.angle(transfer[mask]))
            bands[name] = {
                "frequency_bins": int(mask.sum()),
                "gain_db_median": float(np.median(gain_db)),
                "coherence_median": float(np.median(coherence[mask])),
                "phase_deg_median": float(np.median(phase_deg)),
            }
        usable = (frequency_reference >= 0.5) & (frequency_reference < 50.0)
        total_power = float(np.sum(pxx[usable]))
        high_mask = usable & (frequency_reference >= 15.0)
        per_joint.append({
            "joint_1_based": joint + 1,
            "target_qd_std_rad_s": float(np.std(target[:, joint])),
            "actual_qd_std_rad_s": float(np.std(actual[:, joint])),
            "target_power_fraction_15_to_50_hz": (
                float(np.sum(pxx[high_mask]) / total_power) if total_power > 0.0 else None
            ),
            "bands": bands,
        })
    return {
        "sample_rate_hz": sample_rate_hz,
        "nperseg": N_PER_SEG,
        "overlap": OVERLAP,
        "welch_windows": total_windows,
        "per_joint": per_joint,
    }


def analyze_attempt(label: str, attempt_dir: Path) -> dict[str, Any]:
    attempt_dir = Path(attempt_dir).expanduser().resolve(strict=True)
    sealed_hashes = verify_sealed_sources(attempt_dir)
    attempt = json.loads((attempt_dir / "attempt-result.json").read_text(encoding="utf-8"))
    commands: dict[int, dict[str, float]] = {}
    with (attempt_dir / "command_timeline.jsonl").open(encoding="utf-8") as stream:
        for line_number, line in enumerate(stream, 1):
            if not line.strip():
                continue
            row = json.loads(line)
            reference_time = _finite(row.get("reference_time_s"), f"reference_time line {line_number}")
            if row.get("reference_phase") != "path" or not 0.0 <= reference_time < 60.0:
                continue
            sequence = int(row.get("packet_sequence", -1))
            if sequence < 0:
                raise SpectrumAnalysisError(f"invalid packet sequence on line {line_number}")
            commands.setdefault(sequence, {
                "reference_time_s": reference_time,
                "published_monotonic_s": _finite(row.get("published_monotonic_s"), "published monotonic time"),
            })

    joined: dict[int, dict[str, Any]] = {}
    with (attempt_dir / "robot_frames.jsonl").open(encoding="utf-8") as stream:
        for line_number, line in enumerate(stream, 1):
            if not line.strip():
                continue
            frame = json.loads(line)
            sequence = int(frame.get("consumed_packet_sequence", -1))
            command = commands.get(sequence)
            if command is None or sequence in joined:
                continue
            received = _finite(frame.get("received_monotonic_s"), "RTDE receive time")
            if received - command["published_monotonic_s"] < -0.001:
                continue
            joined[sequence] = {
                "sequence": sequence,
                "reference_time_s": command["reference_time_s"],
                "timestamp_s": _finite(frame.get("timestamp"), "RTDE timestamp"),
                "target_qd": _vector(frame.get("target_qd_rad_s"), "target_qd_rad_s"),
                "actual_qd": _vector(frame.get("qd_rad_s"), "qd_rad_s"),
            }
    rows = sorted(joined.values(), key=lambda row: (row["reference_time_s"], row["sequence"]))
    if len(rows) < N_PER_SEG:
        raise SpectrumAnalysisError("too few exact-consumed-sequence rows for spectral analysis")
    if any(b["sequence"] <= a["sequence"] for a, b in zip(rows, rows[1:])):
        raise SpectrumAnalysisError("consumed sequence regressed after join")
    timestamps = [row["timestamp_s"] for row in rows]
    intervals = [b - a for a, b in zip(timestamps, timestamps[1:])]
    median_dt = statistics.median(intervals)
    if abs(median_dt - NOMINAL_DT_S) > CONTIGUOUS_DT_TOLERANCE_S:
        raise SpectrumAnalysisError(f"median controller interval {median_dt} differs from nominal 2 ms")
    all_spans = split_contiguous_indices(
        [row["sequence"] for row in rows], timestamps,
    )
    eligible_spans = [(start, stop) for start, stop in all_spans if stop - start >= N_PER_SEG]
    if eligible_spans:
        analysis = {
            "status": "estimated",
            **_welch_pair(
                np.asarray([row["target_qd"] for row in rows], dtype=float),
                np.asarray([row["actual_qd"] for row in rows], dtype=float),
                eligible_spans,
                sample_rate_hz=1.0 / NOMINAL_DT_S,
            ),
        }
    else:
        analysis = {
            "status": "ineligible_no_contiguous_span_long_enough",
            "sample_rate_hz": 1.0 / NOMINAL_DT_S,
            "required_contiguous_rows": N_PER_SEG,
            "largest_contiguous_span_rows": max(stop - start for start, stop in all_spans),
            "welch_windows": 0,
            "per_joint": [],
        }
    span_lengths = [stop - start for start, stop in all_spans]
    return {
        "label": label,
        "attempt_dir": str(attempt_dir),
        "sealed_source_sha256": sealed_hashes,
        "path_complete": (attempt.get("evidence") or {}).get("path_complete"),
        "official_qd_gate": (attempt.get("evidence") or {}).get("metrics", {}).get("qd_correlation"),
        "joined_rows": len(rows),
        "contiguous_span_count": len(all_spans),
        "discontinuous_interval_count": max(0, len(all_spans) - 1),
        "contiguous_span_rows": {
            "min": min(span_lengths),
            "median": statistics.median(span_lengths),
            "p90": float(np.quantile(span_lengths, 0.90)),
            "p99": float(np.quantile(span_lengths, 0.99)),
            "max": max(span_lengths),
        },
        "eligible_span_count": len(eligible_spans),
        "rows_in_short_spans": sum(length for length in span_lengths if length < N_PER_SEG),
        "median_controller_interval_s": median_dt,
        "target_actual_same_row_pearson": [
            float(np.corrcoef(np.asarray([r["target_qd"][j] for r in rows]), np.asarray([r["actual_qd"][j] for r in rows]))[0, 1])
            if np.std([r["target_qd"][j] for r in rows]) > 0 and np.std([r["actual_qd"][j] for r in rows]) > 0 else None
            for j in range(6)
        ],
        "frequency_response": analysis,
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", action="append", required=True, help="LABEL=ATTEMPT_DIR; repeat per attempt")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    attempts = []
    seen: set[str] = set()
    for token in args.input:
        label, sep, raw_path = token.partition("=")
        if not sep or not label or not raw_path:
            parser.error("each --input must be LABEL=ATTEMPT_DIR")
        if label in seen:
            parser.error("input labels must be unique")
        seen.add(label)
        attempts.append(analyze_attempt(label, Path(raw_path)))
    output = args.output.expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    document = {
        "schema": "tase.r31-qd-target-actual-frequency-response-v1",
        "claim_class": "offline sealed-trace telemetry diagnostic; no live tuning or gate changes",
        "protocol_id": "figure8_window60_r013_rate460_v1",
        "join": "first RTDE frame echoing the exact consumed packet sequence, received no earlier than publish time minus 1 ms",
        "spectral_method": "Welch CSD on contiguous exact-sequence 2 ms RTDE spans; gaps are split, never interpolated",
        "frequency_bands_hz": FREQUENCY_BANDS_HZ,
        "attempts": attempts,
        "limitations": [
            "Target-to-actual spectral response is observational and not a causal servo or physical-plant model.",
            "The analysis excludes intervals around missing/held RTDE echoes and reports retained spans.",
            "Joint 5 has low command variance; its coherence and gain are not treated as decisive evidence.",
            "The official all-sample published-qdot/actual-qdot gate remains unchanged.",
        ],
    }
    output.write_text(json.dumps(document, indent=2, sort_keys=True, allow_nan=False) + "\n", encoding="utf-8")
    print(json.dumps({"output": str(output), "attempts": len(attempts), "joined_rows": [x["joined_rows"] for x in attempts], "offline_only": True}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
