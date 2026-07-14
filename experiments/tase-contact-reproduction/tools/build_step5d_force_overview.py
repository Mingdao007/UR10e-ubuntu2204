#!/usr/bin/env python3
"""Build and optionally transfer the polished Step5d force overview."""

from __future__ import annotations

import argparse
import json
import math
import shutil
import subprocess
from pathlib import Path
from typing import Any

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402


DEFAULT_MAC_TARGET = "andyl@100.127.94.11:/Users/andyl/Downloads/ur10e_step5d_plots/"


def load_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def numeric(frame: pd.DataFrame, name: str) -> pd.Series:
    if name not in frame:
        return pd.Series(dtype=float)
    return pd.to_numeric(frame[name], errors="coerce")


def finite(values: pd.Series) -> pd.Series:
    return pd.to_numeric(values, errors="coerce").replace([np.inf, -np.inf], np.nan).dropna()


def stats(values: pd.Series) -> dict[str, float | int | None]:
    clean = finite(values)
    if clean.empty:
        return {"samples": 0, "mean": None, "std": None, "mae": None, "p95_abs": None, "max_abs": None}
    absolute = clean.abs()
    return {
        "samples": int(clean.size),
        "mean": float(clean.mean()),
        "std": float(clean.std(ddof=0)),
        "mae": float(absolute.mean()),
        "p95_abs": float(absolute.quantile(0.95)),
        "max_abs": float(absolute.max()),
    }


def display(value: Any, *, scale: float = 1.0, digits: int = 3, suffix: str = "") -> str:
    try:
        number = float(value) * scale
    except (TypeError, ValueError):
        return "n/a"
    return f"{number:.{digits}f}{suffix}" if math.isfinite(number) else "n/a"


def terminal_stop_reason(frame: pd.DataFrame) -> int | None:
    values = finite(numeric(frame, "ur_output_double_register_30"))
    nonzero = values[values.abs() > 0.05]
    return int(round(float(nonzero.iloc[-1]))) if not nonzero.empty else None


def completion_gate(run_dir: Path, frame: pd.DataFrame) -> dict[str, Any]:
    marker = load_json(run_dir / ".capture_complete.json")
    stage = numeric(frame, "ur_output_double_register_35")
    stage25 = frame.loc[(stage - 25.0).abs() < 0.05].copy()
    duration = 0.0
    if not stage25.empty:
        times = finite(numeric(stage25, "t_monotonic_s"))
        if len(times) >= 2:
            duration = float(times.iloc[-1] - times.iloc[0])
    exit_codes = marker.get("exit_codes") if isinstance(marker.get("exit_codes"), dict) else {}
    safety_mode = finite(numeric(stage25, "ur_safety_mode"))
    safety_state = finite(numeric(stage25, "_step5d_contact_safety_state"))
    terminal = terminal_stop_reason(frame)
    checks = {
        "immutable_capture_closed": marker.get("immutable") is True and marker.get("capture_closed") is True,
        "capture_succeeded": marker.get("capture_succeeded") is True,
        "bridge_and_monitor_exit_zero": bool(exit_codes) and all(int(value) == 0 for value in exit_codes.values()),
        "terminal_tp_stop_reason_complete": terminal == 1,
        "stage25_duration_approximately_60s": 59.0 <= duration <= 61.5,
        "ur_safety_normal": not safety_mode.empty and bool((safety_mode == 1.0).all()),
        "no_contact_safety_stop": not safety_state.empty and bool((safety_state == 0.0).all()),
    }
    return {
        "eligible": all(checks.values()),
        "checks": checks,
        "stage25_duration_s": duration,
        "terminal_tp_stop_reason": terminal,
        "stage25_rows": int(len(stage25)),
    }


def transfer(paths: list[Path], target: str, timeout_s: float) -> dict[str, Any]:
    result: dict[str, Any] = {"attempted": True, "ok": False, "target": target}
    if ":" not in target:
        result["issue"] = "mac_target must be HOST:DIR"
        return result
    if shutil.which("ssh") is None or shutil.which("scp") is None:
        result["issue"] = "ssh or scp not found"
        return result
    host, remote_dir = target.split(":", 1)
    remote_dir = remote_dir.rstrip("/")
    try:
        subprocess.run(["ssh", host, "mkdir", "-p", remote_dir], check=True, capture_output=True, text=True, timeout=timeout_s)
        for path in paths:
            subprocess.run(["scp", str(path), f"{host}:{remote_dir}/{path.name}"], check=True, capture_output=True, text=True, timeout=timeout_s)
    except subprocess.CalledProcessError as exc:
        result["issue"] = f"{exc.cmd}: rc={exc.returncode} stderr={exc.stderr.strip()}"
        return result
    except subprocess.TimeoutExpired as exc:
        result["issue"] = f"timeout after {exc.timeout}s: {exc.cmd}"
        return result
    result["ok"] = True
    result["remote_paths"] = [f"{host}:{remote_dir}/{path.name}" for path in paths]
    return result


def build(run_dir: Path, output: Path, summary_output: Path, analysis_path: Path | None) -> dict[str, Any]:
    csv_path = run_dir / "bridge_rtde_500hz.csv"
    frame = pd.read_csv(csv_path, low_memory=False)
    gate = completion_gate(run_dir, frame)
    payload: dict[str, Any] = {
        "schema_version": "step5d_force_overview_v1",
        "claim_class": "diagnostic_only",
        "source_run": str(run_dir.resolve()),
        "source_csv": str(csv_path.resolve()),
        "automatic_generation_gate": gate,
        "generated": False,
        "mac_transfer": {"attempted": False, "ok": None},
    }
    summary_output.parent.mkdir(parents=True, exist_ok=True)
    if not gate["eligible"]:
        payload["skip_reason"] = "run_not_complete_successful_60s"
        summary_output.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        return payload

    stage = numeric(frame, "ur_output_double_register_35")
    active = frame.loc[(stage - 25.0).abs() < 0.05].copy()
    t = numeric(active, "t_monotonic_s")
    active["t_s"] = t - float(t.iloc[0])
    load = numeric(active, "_step4e_normal_load_n")
    target = numeric(active, "target_force_n")
    target_n = float(finite(target).median()) if not finite(target).empty else 12.0
    error = target_n - load
    active["force_error_n"] = error
    indexed = active.set_index(pd.to_timedelta(active["t_s"], unit="s"))
    binned = indexed["force_error_n"].resample("100ms").mean()
    trend = indexed["force_error_n"].rolling("5s", min_periods=1).mean()
    err_stats = stats(error)
    err_clean = finite(error)
    coverage = float((err_clean.abs() <= 1.0).mean()) if not err_clean.empty else None

    x_actual = numeric(active, "ur_actual_TCP_pose_0")
    y_actual = numeric(active, "ur_actual_TCP_pose_1")
    x_ref = numeric(active, "_step4e_desired_x_m")
    y_ref = numeric(active, "_step4e_desired_y_m")
    xy_error = np.sqrt((x_actual - x_ref) ** 2 + (y_actual - y_ref) ** 2) * 1000.0
    health = {
        "row_gap_s": stats(numeric(active, "_step5d_stage25_row_gap_s")),
        "feedback_age_s": stats(numeric(active, "rtde_feedback_age_s")),
        "sent_echo_heartbeat_gap": stats(numeric(active, "rtde_sent_echo_heartbeat_gap")),
        "rnn_oracle_qdot_difference_rad_s": stats(numeric(active, "_step5d_rnn_vs_oracle_qdot_norm")),
        "xy_error_mm": stats(pd.Series(xy_error)),
    }
    if analysis_path is not None and analysis_path.is_file():
        analysis = load_json(analysis_path)
        health["analysis"] = {
            "path": str(analysis_path.resolve()),
            "ok": analysis.get("ok"),
            "analysis_status": analysis.get("analysis_status"),
        }

    figure, axes = plt.subplots(2, 2, figsize=(15, 10), dpi=160)
    figure.suptitle(f"Step5d force overview — {run_dir.name}", fontsize=14)
    axes[0, 0].plot(active["t_s"], load, color="#155e75", linewidth=0.8, label="normal load")
    axes[0, 0].axhline(target_n, color="#b91c1c", linewidth=1.2, label=f"target {target_n:.1f} N")
    axes[0, 0].axhspan(target_n - 1.0, target_n + 1.0, color="#86efac", alpha=0.25, label="±1 N")
    axes[0, 0].set(title="Normal load", xlabel="Stage25 time (s)", ylabel="N")
    axes[0, 0].legend(fontsize=8)

    axes[0, 1].plot(binned.index.total_seconds(), binned, color="#7c3aed", linewidth=0.8, label="100 ms mean")
    axes[0, 1].plot(trend.index.total_seconds(), trend, color="#ea580c", linewidth=1.3, label="5 s trend")
    axes[0, 1].axhspan(-1.0, 1.0, color="#86efac", alpha=0.25)
    axes[0, 1].axhline(0.0, color="black", linewidth=0.6)
    axes[0, 1].set(title="Force error: target − load", xlabel="Stage25 time (s)", ylabel="N")
    axes[0, 1].legend(fontsize=8)

    axes[1, 0].plot(x_ref * 1000.0, y_ref * 1000.0, color="#dc2626", linewidth=1.2, label="reference")
    axes[1, 0].plot(x_actual * 1000.0, y_actual * 1000.0, color="#0369a1", linewidth=0.8, label="actual")
    axes[1, 0].set(title="XY tracking", xlabel="X (mm)", ylabel="Y (mm)")
    axes[1, 0].axis("equal")
    axes[1, 0].legend(fontsize=8)

    axes[1, 1].axis("off")
    health_text = [
        f"Force bias: {display(err_stats['mean'], suffix=' N')}",
        f"Force std: {display(err_stats['std'], suffix=' N')}",
        f"Force MAE: {display(err_stats['mae'], suffix=' N')}",
        f"Within ±1 N: {display(coverage, scale=100.0, digits=1, suffix='%')}",
        f"XY p95/max: {display(health['xy_error_mm']['p95_abs'])}/{display(health['xy_error_mm']['max_abs'])} mm",
        f"Feedback age p95/max: {display(health['feedback_age_s']['p95_abs'], scale=1000.0, digits=2)}/{display(health['feedback_age_s']['max_abs'], scale=1000.0, digits=2)} ms",
        f"Row gap max: {display(health['row_gap_s']['max_abs'], scale=1000.0, digits=2)} ms",
        f"Heartbeat gap max: {display(health['sent_echo_heartbeat_gap']['max_abs'], digits=1)}",
        f"RNN–oracle max: {display(health['rnn_oracle_qdot_difference_rad_s']['max_abs'], digits=6)} rad/s",
        f"Terminal: TP reason {gate['terminal_tp_stop_reason']}; Safety NORMAL",
    ]
    axes[1, 1].text(0.02, 0.98, "\n".join(health_text), va="top", family="monospace", fontsize=10)
    for axis in axes.ravel()[:3]:
        axis.grid(True, alpha=0.25)
    figure.tight_layout(rect=(0, 0, 1, 0.96))
    output.parent.mkdir(parents=True, exist_ok=True)
    figure.savefig(output)
    plt.close(figure)

    payload.update({
        "generated": True,
        "paths": {"png": str(output.resolve()), "summary": str(summary_output.resolve())},
        "force_error_n": {**err_stats, "within_1n_fraction": coverage, "target_n": target_n},
        "health": health,
    })
    summary_output.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return payload


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_dir", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--summary-output", type=Path, required=True)
    parser.add_argument("--analysis", type=Path)
    parser.add_argument("--mac-target", default=DEFAULT_MAC_TARGET)
    parser.add_argument("--no-transfer", action="store_true")
    parser.add_argument("--transfer-timeout-s", type=float, default=60.0)
    args = parser.parse_args(argv)
    payload = build(args.run_dir.resolve(), args.output.resolve(), args.summary_output.resolve(), args.analysis)
    if payload.get("generated") and not args.no_transfer:
        transfer_result = transfer([args.output.resolve()], args.mac_target, args.transfer_timeout_s)
        payload["mac_transfer"] = transfer_result
        args.summary_output.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        if transfer_result.get("ok"):
            summary_transfer = transfer([args.summary_output.resolve()], args.mac_target, args.transfer_timeout_s)
            if not summary_transfer.get("ok"):
                payload["mac_transfer"] = summary_transfer
                args.summary_output.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
