#!/usr/bin/env python3
"""Build and publish one parameter-named PNG for a completed Step5d trial."""

from __future__ import annotations

import argparse
import csv
import json
import math
import shutil
import subprocess
from pathlib import Path
from typing import Any, Mapping

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402


DEFAULT_MAC_TARGET = (
    "andyl@100.127.94.11:/Users/andyl/Downloads/step5d_autotune/"
)


def _finite(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def _token(value: Any) -> str:
    parsed = _finite(value)
    if parsed is None:
        raise ValueError("plot filename parameter must be finite")
    return format(parsed, ".8g")


def parameter_png_name(bundle: Mapping[str, Any]) -> str:
    trial = bundle["trial"]
    candidate = trial["candidate"]
    profile = trial["execution_profile"]
    return (
        f"P={_token(candidate['force_p_gain'])}"
        f"_I={_token(candidate['force_i_gain'])}"
        f"_D={_token(candidate['force_damping'])}"
        f"_nf={_token(profile['normal_max_rate_rad_s'])}"
        f"_slew={_token(profile['host_qdot_slew_rad_s2'])}"
        f"_a={_token(profile['tp_speedj_accel_rad_s2'])}"
        f"_trial={int(trial['trial_id']):04d}.png"
    )


def completed_trial(bundle: Mapping[str, Any]) -> bool:
    capture = bundle.get("capture")
    evaluation = bundle.get("evaluation")
    trial = bundle.get("trial")
    if not all(isinstance(row, Mapping) for row in (capture, evaluation, trial)):
        return False
    campaign = trial.get("campaign")
    return bool(
        isinstance(campaign, Mapping)
        and capture.get("terminal_reason") == 1
        and capture.get("returned_safe") is True
        and capture.get("safe_closure_evidence", {}).get("trial_token_match") is True
        and float(capture.get("stage25_complete_s", 0.0)) >= 60.0
        and evaluation.get("complete_bins") == campaign.get("required_bins") == 550
    )


def _read_stage25(csv_path: Path) -> dict[str, list[float]]:
    fields = {
        "time": "t_monotonic_s",
        "stage": "ur_output_double_register_35",
        "load": "_step4e_normal_load_n",
        "orientation": "_step5d_contact_orientation_error_rad",
        "normal_limiter": "_step5d_normal_rate_limiter_active",
        "raw_qdot": "_step5d_rnn_qdot_max_abs_raw_rad_s",
        "sent_qdot": "_step5d_qdot_max_abs_after_guard_rad_s",
        "host_slew": "_step5d_qdot_slew_limiter_active",
        "qdd0": "ur_actual_qdd_0",
        "qdd1": "ur_actual_qdd_1",
        "qdd2": "ur_actual_qdd_2",
        "qdd3": "ur_actual_qdd_3",
        "qdd4": "ur_actual_qdd_4",
        "qdd5": "ur_actual_qdd_5",
    }
    result = {name: [] for name in fields if name not in {"stage"}}
    result["tp_qdd_max"] = []
    t0: float | None = None
    with csv_path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        missing = sorted(set(fields.values()) - set(reader.fieldnames or ()))
        if missing:
            raise ValueError(f"trial CSV lacks plot fields: {missing}")
        for row in reader:
            stage = _finite(row.get(fields["stage"]))
            timestamp = _finite(row.get(fields["time"]))
            if stage is None or abs(stage - 25.0) > 0.25 or timestamp is None:
                continue
            if t0 is None:
                t0 = timestamp
            result["time"].append(timestamp - t0)
            for name in (
                "load",
                "orientation",
                "normal_limiter",
                "raw_qdot",
                "sent_qdot",
                "host_slew",
            ):
                value = _finite(row.get(fields[name]))
                result[name].append(math.nan if value is None else value)
            qdd = [_finite(row.get(fields[f"qdd{index}"])) for index in range(6)]
            result["tp_qdd_max"].append(
                max(abs(value) for value in qdd if value is not None)
                if any(value is not None for value in qdd)
                else math.nan
            )
    if not result["time"]:
        raise ValueError("trial CSV has no Stage25 rows")
    return result


def build_plot(bundle_path: Path, output_dir: Path) -> Path:
    bundle = json.loads(bundle_path.read_text(encoding="utf-8"))
    if not completed_trial(bundle):
        raise ValueError("trial is not a complete reason-1 safe-closure 550-bin run")
    csv_path = Path(bundle["artifact_provenance"]["csv"]["path"])
    if not csv_path.is_file() or csv_path.is_symlink():
        raise ValueError("immutable trial CSV is missing or not a regular file")
    series = _read_stage25(csv_path)
    trial = bundle["trial"]
    candidate = trial["candidate"]
    profile = trial["execution_profile"]
    evaluation = bundle["evaluation"]
    name = parameter_png_name(bundle)
    output_dir.mkdir(parents=True, exist_ok=True)
    output = output_dir / name

    fig, axes = plt.subplots(4, 1, figsize=(12, 13), sharex=True)
    fig.suptitle(
        "Step5d autotune — "
        f"P={_token(candidate['force_p_gain'])}, "
        f"I={_token(candidate['force_i_gain'])}, "
        f"D={_token(candidate['force_damping'])}, "
        f"profile={profile['profile_id']}, trial={trial['trial_id']}\n"
        f"eligible={evaluation['eligible']}, "
        f"MAE={evaluation.get('objective_mae_n')}, "
        f"failures={evaluation.get('structural_failures', [])}",
        fontsize=11,
    )
    axes[0].plot(series["time"], series["load"], linewidth=0.8, color="#2563eb")
    axes[0].axhline(12.0, color="#dc2626", linestyle="--", label="target 12 N")
    axes[0].set_ylabel("normal load (N)")
    axes[0].legend(loc="best")

    axes[1].plot(series["time"], series["orientation"], linewidth=0.8, color="#7c3aed")
    axes[1].axhline(0.036, color="#f59e0b", linestyle="--", label="p95 qualification")
    axes[1].axhline(0.05, color="#dc2626", linestyle=":", label="absolute qualification")
    axes[1].set_ylabel("orientation error (rad)")
    axes[1].legend(loc="best")

    axes[2].plot(series["time"], series["raw_qdot"], linewidth=0.7, label="raw RNN qdot")
    axes[2].plot(series["time"], series["sent_qdot"], linewidth=0.7, label="sent qdot")
    qdot_values = [
        value
        for name in ("raw_qdot", "sent_qdot")
        for value in series[name]
        if math.isfinite(value)
    ]
    axes[2].set_ylim(0.0, max(0.02, 1.15 * max(qdot_values, default=0.0)))
    axes[2].set_title("qdot cap = 0.5 rad/s (off-scale)", fontsize=9)
    axes[2].set_ylabel("qdot max (rad/s)")
    axes[2].legend(loc="best")

    axes[3].plot(series["time"], series["normal_limiter"], linewidth=0.7, label="normal limiter")
    axes[3].plot(series["time"], series["host_slew"], linewidth=0.7, label="host slew limiter")
    accel = float(profile["tp_speedj_accel_rad_s2"])
    axes[3].plot(
        series["time"],
        [min(2.0, value / accel) for value in series["tp_qdd_max"]],
        linewidth=0.7,
        label="TP accel utilization (clipped at 2x)",
    )
    axes[3].axhline(1.0, color="#dc2626", linestyle="--")
    axes[3].set_ylim(-0.05, 2.05)
    axes[3].set_ylabel("activity / utilization")
    axes[3].set_xlabel("Stage25 time (s)")
    axes[3].legend(loc="best")
    for axis in axes:
        axis.grid(alpha=0.25)
    fig.tight_layout(rect=(0, 0, 1, 0.94))
    fig.savefig(output, dpi=150)
    plt.close(fig)
    return output


def publish(path: Path, mac_target: str, timeout_s: float) -> str:
    if ":" not in mac_target:
        raise ValueError("Mac target must be HOST:DIR")
    if shutil.which("ssh") is None or shutil.which("scp") is None:
        raise RuntimeError("ssh/scp is unavailable")
    host, remote_dir = mac_target.split(":", 1)
    remote_dir = remote_dir.rstrip("/")
    subprocess.run(
        ["ssh", host, "mkdir", "-p", remote_dir],
        check=True,
        timeout=timeout_s,
    )
    remote = f"{host}:{remote_dir}/{path.name}"
    subprocess.run(["scp", str(path), remote], check=True, timeout=timeout_s)
    return remote


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("bundle", type=Path)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--mac-target", default=DEFAULT_MAC_TARGET)
    parser.add_argument("--timeout-s", type=float, default=60.0)
    args = parser.parse_args()
    output = build_plot(args.bundle.resolve(), args.output_dir.resolve())
    remote = publish(output, args.mac_target, args.timeout_s)
    print(json.dumps({"ok": True, "local_png": str(output), "remote_png": remote}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
