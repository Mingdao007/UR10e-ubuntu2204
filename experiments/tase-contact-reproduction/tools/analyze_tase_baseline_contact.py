#!/usr/bin/env python3
"""Summarize recorded baseline-contact commands and force gate traces.

This is a read-only replay of already sealed packet traces. It does not
reconstruct missing controller inputs, access devices, or predict plant
response under a changed command.
"""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
import statistics
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
GAIN_M_S_PER_N = 0.0001767766953
SPEED_CAP_M_S = 0.00025
FORCE_ONLY_GATE_SCOPE = (
    "descriptive per-packet force/torque threshold fraction only; not the official "
    "continuous release gate and excludes dwell, freshness, timing, stationarity, "
    "and velocity predicates"
)

DEFAULT_RUNS = {
    "probe_r013_60_ramp8": "runs/contact-ramp-probe-ladder-20260924-r2-retry3/ramp-08s/initial/attempts/0001",
    "tase_a_before_release_fix": "runs/tase-a-four-unit-engineering-20260924T0515Z/attempts/0001",
    "tase_a_after_release_fix": "runs/tase-a-four-unit-engineering-fixed-20260924T0600HKT/attempts/0001",
}


def _read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object: {path}")
    return value


def _range(values: list[float]) -> dict[str, float] | None:
    if not values:
        return None
    return {
        "min": min(values),
        "median": statistics.median(values),
        "max": max(values),
    }


def _packet_rows(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as stream:
        for line_number, line in enumerate(stream, 1):
            outer = json.loads(line)
            if not isinstance(outer, list) or len(outer) != 2 or not isinstance(outer[1], dict):
                raise ValueError(f"invalid packet row at {path}:{line_number}")
            at_s, packet = float(outer[0]), outer[1]
            doubles = packet.get("double_values")
            if not isinstance(doubles, list) or len(doubles) != 24:
                raise ValueError(f"invalid 606 double payload at {path}:{line_number}")
            # Layout 606 uses logical registers 24..47. Keep the field names
            # local to this read-only tool to avoid importing the live stack.
            rows.append({
                "time_s": at_s,
                "sequence": int(packet["sequence"]),
                "command_mode": int(packet["command_mode"]),
                "reason": str(packet.get("reason", "")),
                "normal_n": float(doubles[0]),
                "force_norm_n": float(doubles[1]),
                "torque_norm_nm": float(doubles[6]),
                "qdot_rad_s": [float(v) for v in doubles[13:19]],
                "cmd_valid": float(doubles[19]),
                "setpoint_n": float(doubles[20]),
                "filtered_normal_n": float(doubles[21]),
            })
    if any(not math.isfinite(row["time_s"]) for row in rows):
        raise ValueError(f"nonfinite packet time in {path}")
    return rows


def _summarize_run(name: str, run_dir: Path) -> dict[str, Any]:
    attempt = _read_json(run_dir / "attempt-result.json")
    packets = _packet_rows(run_dir / "published_packets.jsonl")
    baseline = [row for row in packets if row["command_mode"] == 1]
    if not baseline:
        raise ValueError(f"no BASELINE packets in {run_dir}")
    target_rows = [row for row in baseline if row["setpoint_n"] >= 5.0 - 1e-9]
    target = target_rows[0] if target_rows else None
    windows: dict[str, Any] = {}
    if target is not None:
        for duration_s in (0.5, 1.0, 2.0):
            rows = [
                row for row in baseline
                if target["time_s"] <= row["time_s"] < target["time_s"] + duration_s
            ]
            qdot_norm = [math.sqrt(sum(v * v for v in row["qdot_rad_s"])) for row in rows]
            force_gate = [
                4.0 <= row["filtered_normal_n"] <= 5.5
                and 3.0 <= row["normal_n"] <= 7.0
                and row["force_norm_n"] <= 7.0
                and row["torque_norm_nm"] <= 0.30
                for row in rows
            ]
            windows[f"{duration_s:g}s"] = {
                "sample_count": len(rows),
                "normal_n": _range([row["normal_n"] for row in rows]),
                "filtered_normal_n": _range([row["filtered_normal_n"] for row in rows]),
                "force_norm_n": _range([row["force_norm_n"] for row in rows]),
                "force_only_gate_pass_fraction": (
                    sum(force_gate) / len(force_gate) if force_gate else None
                ),
                "command_qdot_norm_rad_s": _range(qdot_norm),
                "provider_baseline_speed_reconstructed_m_s": _range([
                    max(-SPEED_CAP_M_S, min(
                        SPEED_CAP_M_S,
                        GAIN_M_S_PER_N * (
                            row["setpoint_n"] - max(row["filtered_normal_n"], row["normal_n"])
                        ),
                    ))
                    for row in rows
                ]),
            }
    first = baseline[0]
    outer = attempt.get("evidence", {})
    failure = outer.get("failure")
    tp_timing = attempt.get("timing", {}).get("tp_stage_observations", {}).get("21")
    provider_state = attempt.get("state", {}).get("last_result", {})
    return {
        "run_id": name,
        "run_dir": str(run_dir),
        "metric_scope": {"force_only_gate_pass_fraction": FORCE_ONLY_GATE_SCOPE},
        "controller": "TASE_RNN_MATURE",
        "attempt_failure": failure,
        "tp_state21_observation": tp_timing,
        "baseline_packet_count": len(baseline),
        "baseline_duration_s": baseline[-1]["time_s"] - baseline[0]["time_s"],
        "first_baseline_packet": {
            "time_s": first["time_s"],
            "sequence": first["sequence"],
            "normal_n": first["normal_n"],
            "filtered_normal_n": first["filtered_normal_n"],
            "force_norm_n": first["force_norm_n"],
            "setpoint_n": first["setpoint_n"],
            "command_qdot_rad_s": first["qdot_rad_s"],
            "provider_speed_reconstructed_m_s": max(-SPEED_CAP_M_S, min(
                SPEED_CAP_M_S,
                GAIN_M_S_PER_N * (
                    first["setpoint_n"] - max(first["filtered_normal_n"], first["normal_n"])
                ),
            )),
        },
        "first_target_packet": None if target is None else {
            "time_s": target["time_s"],
            "sequence": target["sequence"],
            "normal_n": target["normal_n"],
            "filtered_normal_n": target["filtered_normal_n"],
            "force_norm_n": target["force_norm_n"],
            "command_qdot_rad_s": target["qdot_rad_s"],
        },
        "target_windows": windows,
        "last_provider_result": {
            key: provider_state.get(key)
            for key in (
                "sample_time_s", "measured_normal_n", "filtered_normal_n",
                "measured_force_norm_n", "baseline_primitive_speed_m_s",
                "provider_qdot_rad_s", "phase", "last_pause",
            )
            if key in provider_state
        },
    }


def analyze(runs: dict[str, str], repo_root: Path = ROOT) -> dict[str, Any]:
    results = {
        name: _summarize_run(name, (repo_root / relative).resolve())
        for name, relative in runs.items()
    }
    return {
        "schema": "tase.baseline-contact-gate-timeline-v1",
        "evidence_class": "recorded TASE command and sensor feedback timeline; descriptive, not a changed-plant prediction",
        "source_fields": {
            "normal_n": "layout-606 packet double register 24",
            "force_norm_n": "layout-606 packet double register 25",
            "qdot_rad_s": "layout-606 packet double registers 37-42",
            "setpoint_n": "layout-606 packet double register 44",
            "filtered_normal_n": "layout-606 packet double register 45",
        },
        "provider_baseline_law": {
            "gain_m_s_per_n": GAIN_M_S_PER_N,
            "normal_speed_cap_m_s": SPEED_CAP_M_S,
            "formula": "clip(gain * (setpoint - max(filtered_normal, raw_normal)), -cap, +cap)",
            "rnn_and_integral_state": "frozen until PATH",
        },
        "metric_scope": {
            "force_only_gate_pass_fraction": FORCE_ONLY_GATE_SCOPE,
        },
        "runs": results,
        "interpretation_boundary": [
            "The 0515 packet shows zero qdot while path-entry release dwell is pending; that recorded run predates the later fix that keeps the TASE baseline provider active in this branch.",
            "The 0600 packet shows active baseline commands that reverse direction as raw and filtered normal load move around the setpoint; it still fails the release dwell.",
            "The isolated 8 s diagnostic probe exits to retract after its 0.5 s release gate and therefore does not test the longer TASE baseline hold or a formal Figure-eight path.",
            "The force values are Kunwei feedback, not independent task-force truth. The command replay does not predict a changed physical response.",
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "reports/tase_contact_gate_timeline_20260924.json")
    parser.add_argument(
        "--run", action="append", default=None,
        help="NAME=relative-or-absolute-attempt-directory; may be repeated",
    )
    args = parser.parse_args()
    runs = dict(DEFAULT_RUNS)
    if args.run:
        runs = {}
        for value in args.run:
            if "=" not in value:
                parser.error("--run must be NAME=PATH")
            name, path = value.split("=", 1)
            if not name or not path or name in runs:
                parser.error("--run names must be unique and paths nonempty")
            runs[name] = path
    result = analyze(runs)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"output": str(args.output), "runs": list(result["runs"])}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
