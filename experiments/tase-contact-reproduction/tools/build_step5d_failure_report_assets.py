#!/usr/bin/env python3
"""Build assets for the internal Step5d v1-v15a failure analysis report."""

from __future__ import annotations

import argparse
import csv
import json
import math
from collections import Counter
from pathlib import Path
from typing import Any

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402


EXPERIMENT_ROOT = Path(__file__).resolve().parents[1]
WORKSPACE_ROOT = EXPERIMENT_ROOT.parents[1]
REPORT_ROOT = WORKSPACE_ROOT / "report"
DEFAULT_ASSET_DIR = REPORT_ROOT / "assets" / "step5d-v1-v15-failure-analysis"
V15A_RUN = (
    EXPERIMENT_ROOT
    / "runs"
    / "bridge_step4e_line_outerloop_step5d_strict_rnn_liveprep_v15a_20260616_160355"
)
V15A_CSV = V15A_RUN / "bridge_rtde_500hz.csv"
V15A_SUMMARY = V15A_RUN / "summary.json"
V15A_STAGE_FREQ = V15A_RUN / "stage_frequency_summary.json"
V15A_OFFLINE = EXPERIMENT_ROOT / "runs" / "step5d_v15a_permissive_recovery_offline_20260616" / "summary.json"
V14_SUMMARY = (
    EXPERIMENT_ROOT
    / "runs"
    / "bridge_step4e_line_outerloop_step5d_strict_rnn_liveprep_v14_20260615_231805"
    / "summary.json"
)
STAGE_TABLE = EXPERIMENT_ROOT / "config" / "step5_stage_table.json"


VERSION_ROWS = [
    ("v1", "entered Stage25", "heartbeat stale", "约 0.106 s 后 TP heartbeat stale 停止"),
    ("v2", "entered Stage25", "force guard", "高预载进入，qdot 到 0.30 rad/s，normal force guard 停止"),
    ("v3", "before Stage25", "25.3 gate too narrow", "strict 5N force-settle gate 太窄"),
    ("v4", "entered Stage25", "semantic bug", "暴露 force/frame semantic bug"),
    ("v5", "Stage25 blocked", "engage gate too narrow", "2-15 N gate 拦住 17-21 N contact"),
    ("v6", "entered Stage25", "re-contact overpressure", "2-40 N gate 放过 20-22 N re-contact"),
    ("v7", "transition", "incomplete contact strategy", "保留过渡包，仍未解决主动稳定接触"),
    ("v8", "before Stage25", "low-load dropout", "25.3 low-load dropout 被当作 violation 停止"),
    ("v9", "before Stage25", "PID hunting", "direct force-PID under point contact hunting timeout"),
    ("v10", "before Stage25", "admittance hunting", "scalar admittance 饱和/翻符号，无法稳定 settle"),
    ("v11", "entered Stage25", "lost contact / E-stop", "deadband acquire 后失接触并触发 operator E-stop"),
    ("v12", "not live", "read-back only", "guarded read-back package，被 v13 planning supersede"),
    ("v13", "not live", "P1 safety gap", "first-sample actual-speed dwell gap，被 v14 supersede"),
    ("v14", "entered Stage25", "predicted speed watchdog", "predicted TCP speed watchdog 停止"),
    ("v15", "not live", "audit gap", "cage 未真正 online，bounded hold 不完整"),
    ("v15a", "entered Stage25", "hold duty limit", "zero-qdot hold 未恢复接触，hold_duty_limit 停止"),
]


def finite_float(value: Any) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return math.nan
    return parsed if math.isfinite(parsed) else math.nan


def percentile(values: list[float], p: float) -> float:
    finite = sorted(v for v in values if math.isfinite(v))
    if not finite:
        return math.nan
    idx = int(round((len(finite) - 1) * p))
    return finite[min(max(idx, 0), len(finite) - 1)]


def stats(values: list[float]) -> dict[str, float | int | None]:
    finite = [v for v in values if math.isfinite(v)]
    if not finite:
        return {"samples": 0, "min": None, "mean": None, "p50": None, "p95": None, "max": None}
    return {
        "samples": len(finite),
        "min": min(finite),
        "mean": sum(finite) / len(finite),
        "p50": percentile(finite, 0.50),
        "p95": percentile(finite, 0.95),
        "max": max(finite),
    }


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def stage25_rows(rows: list[dict[str, str]]) -> list[tuple[int, dict[str, str]]]:
    return [
        (idx, row)
        for idx, row in enumerate(rows)
        if abs(finite_float(row.get("ur_output_double_register_35")) - 25.0) < 0.05
    ]


def command_max(row: dict[str, str]) -> float:
    fields = [
        "step4e_cmd_vx_m_s",
        "step4e_cmd_vy_m_s",
        "step4e_cmd_vz_m_s",
        "step4e_cmd_wx_rad_s",
        "step4e_cmd_wy_rad_s",
        "step4e_cmd_wz_rad_s",
    ]
    values = [abs(finite_float(row.get(field))) for field in fields]
    finite = [value for value in values if math.isfinite(value)]
    return max(finite) if finite else math.nan


def contact_action(row: dict[str, str]) -> str:
    raw = row.get("_step5d_contact_safety_action", "")
    if raw == "1" or raw == "1.0":
        return "pass_solver"
    if raw == "2" or raw == "2.0":
        return "hold_zero_qdot"
    if raw == "3" or raw == "3.0":
        return "stop_zero_qdot"
    return raw


def extract_v15a_metrics(rows: list[dict[str, str]]) -> dict[str, Any]:
    s25 = stage25_rows(rows)
    if not s25:
        raise RuntimeError(f"no Stage25 rows found in {V15A_CSV}")
    t0 = finite_float(s25[0][1].get("t_monotonic_s"))
    t1 = finite_float(s25[-1][1].get("t_monotonic_s"))
    reasons = Counter(row.get("_step5d_contact_safety_reason", "") for _, row in s25)
    cage_reasons = Counter(row.get("_step5d_tcp_cage_reason", "") for _, row in s25)
    commands = [command_max(row) for _, row in s25]
    hold_rows = [(idx, row) for idx, row in s25 if contact_action(row) == "hold_zero_qdot"]
    pass_rows = [(idx, row) for idx, row in s25 if contact_action(row) == "pass_solver"]
    stop_rows = [(idx, row) for idx, row in s25 if contact_action(row) == "stop_zero_qdot"]
    return {
        "stage25_rows": len(s25),
        "stage25_index_start": s25[0][0],
        "stage25_index_end": s25[-1][0],
        "stage25_duration_s": t1 - t0,
        "stop_reason": read_json(V15A_SUMMARY).get("stop_reason"),
        "reason_counts": dict(reasons),
        "cage_reason_counts": dict(cage_reasons),
        "normal_load_n": stats([finite_float(row.get("_step4e_normal_load_n")) for _, row in s25]),
        "force_norm_n": stats([finite_float(row.get("force_norm_n")) for _, row in s25]),
        "actual_tcp_speed_m_s": stats([finite_float(row.get("_step5d_actual_tcp_speed_m_s")) for _, row in s25]),
        "predicted_tcp_speed_m_s": stats([finite_float(row.get("_step5d_predicted_tcp_speed_m_s")) for _, row in s25]),
        "tcp_cage_braking_margin_m": stats([finite_float(row.get("_step5d_tcp_cage_braking_margin_m")) for _, row in s25]),
        "hold_duty": stats([finite_float(row.get("_step5d_hold_duty")) for _, row in s25]),
        "consecutive_hold_s": stats([finite_float(row.get("_step5d_consecutive_hold_s")) for _, row in s25]),
        "command_max_abs": stats(commands),
        "hold_rows": len(hold_rows),
        "pass_solver_rows": len(pass_rows),
        "stop_rows": len(stop_rows),
        "hold_command_nonzero_rows": sum(command_max(row) > 1e-9 for _, row in hold_rows),
        "pass_command_nonzero_rows": sum(command_max(row) > 1e-9 for _, row in pass_rows),
        "final_row": {
            "normal_load_n": finite_float(s25[-1][1].get("_step4e_normal_load_n")),
            "force_norm_n": finite_float(s25[-1][1].get("force_norm_n")),
            "hold_duty": finite_float(s25[-1][1].get("_step5d_hold_duty")),
            "hold_event_count": finite_float(s25[-1][1].get("_step5d_hold_event_count")),
            "consecutive_hold_s": finite_float(s25[-1][1].get("_step5d_consecutive_hold_s")),
            "tcp_cage_braking_margin_m": finite_float(s25[-1][1].get("_step5d_tcp_cage_braking_margin_m")),
            "tcp_cage_reason": s25[-1][1].get("_step5d_tcp_cage_reason"),
            "contact_safety_reason": s25[-1][1].get("_step5d_contact_safety_reason"),
        },
    }


def relative_stage25_series(rows: list[dict[str, str]]) -> dict[str, list[float | str]]:
    s25 = stage25_rows(rows)
    t0 = finite_float(s25[0][1].get("t_monotonic_s"))
    return {
        "t": [finite_float(row.get("t_monotonic_s")) - t0 for _, row in s25],
        "normal_load": [finite_float(row.get("_step4e_normal_load_n")) for _, row in s25],
        "force_error": [finite_float(row.get("step4e_force_error_n")) for _, row in s25],
        "hold_duty": [finite_float(row.get("_step5d_hold_duty")) for _, row in s25],
        "actual_speed": [finite_float(row.get("_step5d_actual_tcp_speed_m_s")) for _, row in s25],
        "predicted_speed": [finite_float(row.get("_step5d_predicted_tcp_speed_m_s")) for _, row in s25],
        "cage_margin": [finite_float(row.get("_step5d_tcp_cage_braking_margin_m")) for _, row in s25],
        "command_max": [command_max(row) for _, row in s25],
        "path_time": [finite_float(row.get("_step4e_path_time_s")) for _, row in s25],
        "stage_s": [finite_float(row.get("_step4e_line_stage_s")) for _, row in s25],
        "reason": [row.get("_step5d_contact_safety_reason", "") for _, row in s25],
    }


def plot_v15a_load_hold(series: dict[str, list[float | str]], out: Path) -> None:
    fig, ax1 = plt.subplots(figsize=(10, 4.8))
    t = series["t"]
    ax1.plot(t, series["normal_load"], color="#2563eb", linewidth=1.6, label="normal_load_n")
    ax1.axhline(5.0, color="#111827", linestyle="--", linewidth=1.0, label="target 5 N")
    ax1.set_xlabel("Stage25 time (s)")
    ax1.set_ylabel("Normal load (N)")
    ax1.grid(True, alpha=0.25)
    ax2 = ax1.twinx()
    ax2.plot(t, series["hold_duty"], color="#dc2626", linewidth=1.5, label="hold duty")
    ax2.axhline(0.40, color="#dc2626", linestyle=":", linewidth=1.0, label="hold duty limit")
    ax2.set_ylabel("Hold duty")
    fig.suptitle("v15a Stage25: contact load collapses while hold duty accumulates")
    lines1, labels1 = ax1.get_legend_handles_labels()
    lines2, labels2 = ax2.get_legend_handles_labels()
    ax1.legend(lines1 + lines2, labels1 + labels2, loc="upper right", fontsize=8)
    fig.tight_layout()
    fig.savefig(out, dpi=180)
    plt.close(fig)


def plot_v15a_command_freeze(series: dict[str, list[float | str]], out: Path) -> None:
    fig, ax1 = plt.subplots(figsize=(10, 4.8))
    t = series["t"]
    ax1.plot(t, series["command_max"], color="#7c3aed", linewidth=1.4, label="max |qdot command|")
    ax1.set_xlabel("Stage25 time (s)")
    ax1.set_ylabel("Max |command|")
    ax1.grid(True, alpha=0.25)
    ax2 = ax1.twinx()
    ax2.plot(t, series["path_time"], color="#059669", linewidth=1.4, label="path_time_s")
    ax2.plot(t, series["stage_s"], color="#f59e0b", linewidth=1.2, linestyle="--", label="line_stage_s")
    ax2.set_ylabel("Virtual/path time (s)")
    fig.suptitle("v15a Stage25: low-load recovery freezes time and sends zero qdot")
    lines1, labels1 = ax1.get_legend_handles_labels()
    lines2, labels2 = ax2.get_legend_handles_labels()
    ax1.legend(lines1 + lines2, labels1 + labels2, loc="upper right", fontsize=8)
    fig.tight_layout()
    fig.savefig(out, dpi=180)
    plt.close(fig)


def plot_version_timeline(out: Path) -> None:
    fig, ax = plt.subplots(figsize=(11, 5.8))
    labels = [row[0] for row in VERSION_ROWS]
    colors = []
    for _, stage, cause, _ in VERSION_ROWS:
        if "not live" in stage or "transition" in stage:
            colors.append("#94a3b8")
        elif "before Stage25" in stage or "blocked" in stage:
            colors.append("#f59e0b")
        elif cause in {"semantic bug", "force guard", "predicted speed watchdog", "hold duty limit"}:
            colors.append("#dc2626")
        else:
            colors.append("#2563eb")
    y = list(range(len(VERSION_ROWS)))
    ax.barh(y, [1] * len(y), color=colors, alpha=0.88)
    ax.set_yticks(y)
    ax.set_yticklabels(labels)
    ax.set_xticks([])
    ax.invert_yaxis()
    for idx, (_, stage, cause, _) in enumerate(VERSION_ROWS):
        ax.text(0.03, idx, f"{stage} | {cause}", va="center", ha="left", fontsize=8.5, color="white")
    ax.set_title("Step5d v1-v15a failure chain")
    for spine in ax.spines.values():
        spine.set_visible(False)
    fig.tight_layout()
    fig.savefig(out, dpi=180)
    plt.close(fig)


def build_assets(asset_dir: Path) -> dict[str, Any]:
    asset_dir.mkdir(parents=True, exist_ok=True)
    rows = read_csv(V15A_CSV)
    series = relative_stage25_series(rows)
    v15a_metrics = extract_v15a_metrics(rows)
    offline = read_json(V15A_OFFLINE)
    stage_freq = read_json(V15A_STAGE_FREQ)
    summary = read_json(V15A_SUMMARY)
    metrics = {
        "sources": {
            "v15a_run": str(V15A_RUN),
            "v15a_csv": str(V15A_CSV),
            "v15a_summary": str(V15A_SUMMARY),
            "v15a_offline_summary": str(V15A_OFFLINE),
            "stage_table": str(STAGE_TABLE),
        },
        "v15a": v15a_metrics,
        "offline_gate": {
            "success_hold_duty_max_observed": offline["candidate_parameters"]["bounded_hold_policy"][
                "success_hold_duty_max_observed"
            ],
            "hold_duty_limit": offline["candidate_parameters"]["bounded_hold_policy"]["hold_duty_limit"],
            "success_hold_event_count_max_observed": offline["candidate_parameters"]["bounded_hold_policy"][
                "success_hold_event_count_max_observed"
            ],
            "hold_event_limit": offline["candidate_parameters"]["bounded_hold_policy"]["hold_event_limit"],
            "acceptance": offline["acceptance"],
        },
        "runtime": {
            "bridge_write_rate_hz": summary["bridge_write_timing"]["rate_hz"],
            "rtde_output_rate_hz": summary["rtde_output_timing"]["rate_hz"],
            "rtde_reconnect_event_count": summary.get("rtde_reconnect_event_count", 0),
            "stage25_echo_rate_hz": stage_freq["stage25_ft_line_control_echo_rate"]["echo_rate_hz"],
        },
        "version_rows": [
            {"version": version, "stage": stage, "cause": cause, "summary": text}
            for version, stage, cause, text in VERSION_ROWS
        ],
    }
    (asset_dir / "metrics.json").write_text(json.dumps(metrics, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    plot_v15a_load_hold(series, asset_dir / "v15a-stage25-load-hold-duty.png")
    plot_v15a_command_freeze(series, asset_dir / "v15a-command-freeze.png")
    plot_version_timeline(asset_dir / "step5d-version-timeline.png")
    return metrics


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--asset-dir", type=Path, default=DEFAULT_ASSET_DIR)
    args = parser.parse_args()
    metrics = build_assets(args.asset_dir)
    print(json.dumps({"ok": True, "asset_dir": str(args.asset_dir), "stop_reason": metrics["v15a"]["stop_reason"]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
