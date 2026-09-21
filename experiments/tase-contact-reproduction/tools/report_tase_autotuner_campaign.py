"""Build a reproducible TASE-RNN campaign/confirmation report.

The live owner's formal MAE/RMSE and timing gates remain authoritative.  The
packet diagnostics in this report are a separate signed-normal diagnostic
view, computed from the published packets in the formal PATH window; they are
not an independent force truth source.
"""
from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
from typing import Any, Iterable


PERIOD_S = 2.0 * math.pi / 0.1
TARGET_FORCE_N = 5.0
QDOT_CAP_RAD_S = 0.05


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _latest_by(rows: Iterable[dict[str, Any]], keys: tuple[str, ...]) -> list[dict[str, Any]]:
    latest: dict[tuple[Any, ...], dict[str, Any]] = {}
    for row in rows:
        latest[tuple(row.get(key) for key in keys)] = row
    return [latest[key] for key in sorted(latest)]


def _percentile(values: list[float], quantile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = (len(ordered) - 1) * quantile
    low = math.floor(index)
    high = math.ceil(index)
    if low == high:
        return float(ordered[low])
    weight = index - low
    return float(ordered[low] * (1.0 - weight) + ordered[high] * weight)


def _formal_packet_diagnostics(run_dir: Path) -> dict[str, Any]:
    frames = _read_jsonl(run_dir / "robot_frames.jsonl")
    packets: list[tuple[float, dict[str, Any]]] = []
    for row in _read_jsonl(run_dir / "published_packets.jsonl"):
        # Published packet logs are [host_monotonic_s, packet].
        if isinstance(row, list) and len(row) == 2 and isinstance(row[1], dict):
            packets.append((float(row[0]), row[1]))
    state25 = [row for row in frames if row.get("integer_echoes", {}).get("26") == 25]
    if not state25 or not packets:
        return {
            "available": False,
            "reason": "formal packet/robot trace is missing",
        }
    first_state25_host = float(state25[0]["received_monotonic_s"])
    formal_start = first_state25_host + 1.0
    formal_end = formal_start + PERIOD_S
    selected = [
        packet
        for host_time, packet in packets
        if packet.get("command_mode") == 2 and formal_start <= host_time < formal_end
    ]
    normal = [float(packet["double_values"][0]) for packet in selected]
    force_norm = [float(packet["double_values"][1]) for packet in selected]
    errors = [value - TARGET_FORCE_N for value in normal]
    abs_errors = [abs(value) for value in errors]
    qdot = [
        max(abs(float(value)) for value in packet["double_values"][13:19])
        for packet in selected
    ]
    saturated = sum(value >= QDOT_CAP_RAD_S - 1e-12 for value in qdot)
    return {
        "available": True,
        "sample_count": len(selected),
        "formal_window_s": [formal_start, formal_end],
        "packet_signed_normal_bias_n": (sum(errors) / len(errors)) if errors else None,
        "packet_signed_normal_rmse_n": (
            math.sqrt(sum(error * error for error in errors) / len(errors)) if errors else None
        ),
        "packet_signed_normal_mae_n": (sum(abs_errors) / len(abs_errors)) if abs_errors else None,
        "packet_signed_normal_tail_abs_p95_n": _percentile(abs_errors, 0.95),
        "low_contact_fraction": (
            sum(value < 1.0 for value in normal) / len(normal) if normal else None
        ),
        "force_norm_max_n": max(force_norm) if force_norm else None,
        "qdot_saturation_fraction": saturated / len(qdot) if qdot else None,
        "qdot_cap_rad_s": QDOT_CAP_RAD_S,
        "diagnostic_scope": "published corrected signed-normal packet channel; not independent task-force truth",
    }


def _attempt_report(row: dict[str, Any]) -> dict[str, Any]:
    metrics = row.get("metrics") if isinstance(row.get("metrics"), dict) else {}
    run_dir = Path(str(row.get("run_dir", "")))
    timing = metrics.get("timing_evidence") if isinstance(metrics.get("timing_evidence"), dict) else {}
    result = {
        "ordinal": row.get("ordinal"),
        "candidate_id": row.get("candidate_id"),
        "stage": row.get("stage"),
        "Md_scalar": row.get("Md_scalar"),
        "Bd_scalar": row.get("Bd_scalar"),
        "status": row.get("status"),
        "mae_n": row.get("mae_n"),
        "rmse_n": metrics.get("full_force_rmse_n"),
        "formal_metric_duration_s": metrics.get("full_force_metric_duration_s"),
        "full_path_bin_count": metrics.get("full_path_bin_count"),
        "required_full_path_bin_count": metrics.get("required_full_path_bin_count"),
        "timing_gate_passed": metrics.get("timing_gate_passed"),
        "timing_rate_hz": timing.get("rtde_frames", timing.get("layer_rates_hz", {}).get("rtde_frames")),
        "timing_max_fresh_gap_s": timing.get("max_fresh_gap_s"),
        "home_verified": row.get("home_verified"),
        "failure": row.get("failure"),
        "packet_diagnostics": _formal_packet_diagnostics(run_dir) if row.get("status") == "complete" else None,
    }
    return result


def build_report(campaign_dir: Path) -> dict[str, Any]:
    campaign_dir = campaign_dir.expanduser().resolve()
    ledger_rows = _latest_by(_read_jsonl(campaign_dir / "ledger.jsonl"), ("ordinal",))
    confirmation_rows = _latest_by(
        _read_jsonl(campaign_dir / "confirmation" / "ledger.jsonl"), ("round", "arm")
    )
    campaign_summary = json.loads((campaign_dir / "summary.json").read_text(encoding="utf-8"))
    confirmation_summary_path = campaign_dir / "confirmation" / "summary.json"
    confirmation_summary = (
        json.loads(confirmation_summary_path.read_text(encoding="utf-8"))
        if confirmation_summary_path.is_file()
        else None
    )
    report = {
        "schema": "tase.autotuner-report-v1",
        "campaign_dir": str(campaign_dir),
        "method": "TASE_RNN_MATURE",
        "formal_metric": "full formal PATH measured filtered normal load; target 5 N",
        "campaign": {
            "budget": campaign_summary.get("budget"),
            "attempts": campaign_summary.get("attempts"),
            "complete_paths": campaign_summary.get("complete_paths"),
            "failed_attempts": campaign_summary.get("failed_attempts"),
            "failure_denominator_included": True,
            "best": campaign_summary.get("best"),
            "attempts_detail": [_attempt_report(row) for row in ledger_rows],
        },
        "confirmation": {
            "attempted_cells": len(confirmation_rows),
            "complete_cells": sum(row.get("status") == "complete" for row in confirmation_rows),
            "failed_cells": sum(row.get("status") == "failed" for row in confirmation_rows),
            "summary": confirmation_summary,
        },
        "claim_scope": (
            "campaign and confirmation evidence only; packet diagnostics are not independent task-force truth"
        ),
    }
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--campaign-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    report = build_report(args.campaign_dir)
    output = args.output or args.campaign_dir / "tase-autotuner-report.json"
    output = output.expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2, sort_keys=True, allow_nan=False) + "\n", encoding="utf-8")
    print(json.dumps({"output": str(output), "campaign_attempts": report["campaign"]["attempts"], "confirmation_cells": report["confirmation"]["attempted_cells"]}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
