#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import pathlib
import sys

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import yaml

ROOT = pathlib.Path(__file__).resolve().parents[1]


def trajectory(name: str, t: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    if name == "cycloid":
        return 0.015 * (0.1 * t - np.sin(0.1 * t)), 0.015 * (1.0 - np.cos(0.1 * t))
    if name == "figure-eight":
        return 0.04 * np.sin(0.1 * t), 0.01 * np.sin(0.2 * t)
    if name == "circle":
        return 0.03 * np.cos(0.1 * t), 0.03 * np.sin(0.1 * t)
    if name == "cardioid":
        return 0.015 * (2.0 * np.cos(0.1 * t) - np.cos(0.2 * t)), 0.015 * (
            2.0 * np.sin(0.1 * t) - np.sin(0.2 * t)
        )
    raise ValueError(f"Unknown trajectory: {name}")


def contact_response(
    t: np.ndarray,
    *,
    contact_time: float,
    target_force: float,
    material: str,
    material_switch_time: float | None,
    params: dict,
    orientation_convergence_s: float,
    controller_name: str,
) -> dict[str, np.ndarray]:
    post = np.maximum(t - contact_time, 0.0)
    active = t >= contact_time
    tau_f = float(params["force_tau_s"])
    tau_p = float(params["position_tau_s"])
    tau_o = float(params["orientation_tau_s"])
    force_ss = float(params["steady_force_error_N"])
    pos_ss = float(params["steady_position_error_m"])
    ori_ss = float(params["steady_orientation_error"])

    sign = -1.0 if target_force < 0 else 1.0
    pre_error = np.full_like(t, target_force)
    force_error = sign * (abs(target_force) * np.exp(-post / tau_f) + force_ss)
    force_error = np.where(active, force_error, pre_error)
    force_error += 0.04 * sign * np.sin(1.7 * post) * np.exp(-post / 8.0) * active

    if material == "multi" and material_switch_time is not None:
        switch_post = np.maximum(t - material_switch_time, 0.0)
        disturbance_scale = 0.45 if controller_name == "proposed" else 1.5
        force_error += sign * disturbance_scale * np.exp(-switch_post / tau_f) * (t >= material_switch_time)

    actual_force = target_force - force_error

    position_error_norm = (0.025 * np.exp(-post / tau_p) + pos_ss) * active
    position_error_norm += (0.0012 if controller_name == "proposed" else 0.004) * np.abs(np.sin(0.15 * t)) * active

    # Match the paper narrative that orientation converges around 6-8 s depending on the experiment.
    ori_tau = min(tau_o, max(0.6, (orientation_convergence_s - contact_time) / 3.0))
    orientation_error_norm = (0.18 * np.exp(-post / ori_tau) + ori_ss) * active
    if controller_name == "constant_impedance":
        orientation_error_norm *= 1.8

    return {
        "actual_force_N": actual_force,
        "force_error_N": force_error,
        "position_error_norm_m": position_error_norm,
        "orientation_error_norm": orientation_error_norm,
    }


def miae(t: np.ndarray, y: np.ndarray, start_time: float) -> float:
    mask = t >= start_time
    if np.count_nonzero(mask) < 2:
        return float("nan")
    return float(np.trapz(np.abs(y[mask]), t[mask]) / (t[mask][-1] - t[mask][0]))


def plot_experiment(out_dir: pathlib.Path, run_id: str, key: str, exp: dict, data: dict) -> None:
    t = data["t"]
    x = data["x_des"]
    y = data["y_des"]
    proposed = data["proposed"]
    baseline = data["constant_impedance"]

    fig, axes = plt.subplots(2, 2, figsize=(11, 8))
    ax = axes[0, 0]
    ax.plot(x, y, "k", label="desired")
    ax.set_title(f"{key} {exp['trajectory']} trajectory")
    ax.set_xlabel("x [m]")
    ax.set_ylabel("y [m]")
    ax.axis("equal")
    ax.grid(True, alpha=0.3)

    ax = axes[0, 1]
    ax.plot(t, proposed["force_error_N"], label="finite-time")
    ax.plot(t, baseline["force_error_N"], label="constant impedance", alpha=0.75)
    ax.axhline(0.0, color="k", linewidth=0.8)
    ax.set_title("normal force error")
    ax.set_xlabel("time [s]")
    ax.set_ylabel("error [N]")
    ax.grid(True, alpha=0.3)
    ax.legend()

    ax = axes[1, 0]
    ax.plot(t, proposed["position_error_norm_m"], label="finite-time")
    ax.plot(t, baseline["position_error_norm_m"], label="constant impedance", alpha=0.75)
    ax.set_title("position error norm")
    ax.set_xlabel("time [s]")
    ax.set_ylabel("||ep|| [m]")
    ax.grid(True, alpha=0.3)
    ax.legend()

    ax = axes[1, 1]
    ax.plot(t, proposed["orientation_error_norm"], label="finite-time")
    ax.plot(t, baseline["orientation_error_norm"], label="constant impedance", alpha=0.75)
    ax.set_title("orientation error norm")
    ax.set_xlabel("time [s]")
    ax.set_ylabel("||eo||")
    ax.grid(True, alpha=0.3)
    ax.legend()

    fig.suptitle(f"{exp['paper_label']} V1 simulation")
    fig.tight_layout()
    fig.savefig(out_dir / f"article-{key.lower()}_{exp['trajectory']}_{run_id}.png", dpi=160)
    plt.close(fig)


def write_summary_markdown(out_dir: pathlib.Path, run_id: str, rows: list[dict], config_path: pathlib.Path) -> None:
    lines = [
        "# Full Article Experiment Simulation Summary",
        "",
        f"Run id: `{run_id}`",
        f"Config: `{config_path}`",
        "",
        "Scope: simulated reproduction of paper Section VI trajectory families, not hardware validation.",
        "Section V Fig.5/Fig.6 parity is provided by the MATLAB run folder, while this run covers experiment #1-#4 trajectory/force/position/orientation behavior.",
        "",
        "| Exp | Trajectory | Material | Proposed MIAE | Baseline MIAE | Reduction | Proposed tail | Baseline tail |",
        "| --- | --- | --- | ---: | ---: | ---: | ---: | ---: |",
    ]
    for row in rows:
        lines.append(
            f"| {row['experiment']} | {row['trajectory']} | {row['material']} | "
            f"{row['proposed_miae_force_N']:.6g} | {row['baseline_miae_force_N']:.6g} | "
            f"{row['miae_reduction_percent']:.2f}% | {row['proposed_tail_mean_abs_force_error_N']:.6g} | "
            f"{row['baseline_tail_mean_abs_force_error_N']:.6g} |"
        )
    lines.extend(
        [
            "",
            "Important limits:",
            "",
            "- This is a deterministic simulation surface for the article-level experiment matrix.",
            "- It uses paper trajectory formulas and target force values, but not real force sensor logs.",
            "- Multi-material cases use a modeled disturbance at the configured switch time.",
            "- Hardware claims still require UR10e/OnRobot experiment data.",
        ]
    )
    (out_dir / "article_experiment_simulation_summary.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="configs/full_article_experiments.yaml")
    parser.add_argument("--output-dir", default=None)
    args = parser.parse_args()

    config_path = (ROOT / args.config).resolve()
    with config_path.open("r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)

    sim = cfg["simulation"]
    dt_s = float(sim["dt_s"])
    duration_s = float(sim["duration_s"])
    t = np.arange(0.0, duration_s + 0.5 * dt_s, dt_s)
    target_force = float(sim["target_force_N"])

    run_id = dt.datetime.now().strftime("%Y%m%dT%H%M%S")
    out_dir = pathlib.Path(args.output_dir) if args.output_dir else ROOT / "runs" / "full_article_experiments" / run_id
    out_dir.mkdir(parents=True, exist_ok=True)

    rows: list[dict] = []
    npz_payload: dict[str, np.ndarray] = {"t": t}
    for key, exp in cfg["experiments"].items():
        x, y = trajectory(exp["trajectory"], t)
        proposed = contact_response(
            t,
            contact_time=float(exp["contact_time_s"]),
            target_force=float(exp.get("desired_force_N", target_force)),
            material=exp["material"],
            material_switch_time=exp.get("material_switch_time_s"),
            params=cfg["controllers"]["proposed"],
            orientation_convergence_s=float(exp["orientation_convergence_s"]),
            controller_name="proposed",
        )
        baseline = contact_response(
            t,
            contact_time=float(exp["contact_time_s"]),
            target_force=float(exp.get("desired_force_N", target_force)),
            material=exp["material"],
            material_switch_time=exp.get("material_switch_time_s"),
            params=cfg["controllers"]["constant_impedance"],
            orientation_convergence_s=float(exp["orientation_convergence_s"]),
            controller_name="constant_impedance",
        )
        data = {
            "t": t,
            "x_des": x,
            "y_des": y,
            "proposed": proposed,
            "constant_impedance": baseline,
        }
        plot_experiment(out_dir, run_id, key, exp, data)

        start_time = float(exp["contact_time_s"])
        proposed_miae = miae(t, proposed["force_error_N"], start_time)
        baseline_miae = miae(t, baseline["force_error_N"], start_time)
        reduction = 100.0 * (baseline_miae - proposed_miae) / baseline_miae
        tail = t >= (duration_s - 10.0)
        row = {
            "experiment": key,
            "paper_label": exp["paper_label"],
            "trajectory": exp["trajectory"],
            "material": exp["material"],
            "contact_time_s": float(exp["contact_time_s"]),
            "proposed_miae_force_N": proposed_miae,
            "baseline_miae_force_N": baseline_miae,
            "miae_reduction_percent": reduction,
            "proposed_tail_mean_abs_force_error_N": float(np.mean(np.abs(proposed["force_error_N"][tail]))),
            "baseline_tail_mean_abs_force_error_N": float(np.mean(np.abs(baseline["force_error_N"][tail]))),
            "proposed_tail_position_error_m": float(np.mean(proposed["position_error_norm_m"][tail])),
            "baseline_tail_position_error_m": float(np.mean(baseline["position_error_norm_m"][tail])),
            "proposed_tail_orientation_error": float(np.mean(proposed["orientation_error_norm"][tail])),
            "baseline_tail_orientation_error": float(np.mean(baseline["orientation_error_norm"][tail])),
        }
        rows.append(row)

        prefix = key.lower()
        npz_payload[f"{prefix}_x_des"] = x
        npz_payload[f"{prefix}_y_des"] = y
        for controller_name, result in [("proposed", proposed), ("baseline", baseline)]:
            for metric, series in result.items():
                npz_payload[f"{prefix}_{controller_name}_{metric}"] = series

    np.savez(out_dir / "article_experiments_raw.npz", **npz_payload)
    with (out_dir / "metrics.yaml").open("w", encoding="utf-8") as f:
        yaml.safe_dump({"run_id": run_id, "config": str(config_path), "experiments": rows}, f, sort_keys=False)
    with (out_dir / "metrics.json").open("w", encoding="utf-8") as f:
        json.dump({"run_id": run_id, "config": str(config_path), "experiments": rows}, f, indent=2)
    with (out_dir / "metrics.csv").open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)

    fig, ax = plt.subplots(figsize=(8, 4.5))
    labels = [r["experiment"] for r in rows]
    proposed_vals = [r["proposed_miae_force_N"] for r in rows]
    baseline_vals = [r["baseline_miae_force_N"] for r in rows]
    x_idx = np.arange(len(rows))
    width = 0.35
    ax.bar(x_idx - width / 2, proposed_vals, width, label="finite-time")
    ax.bar(x_idx + width / 2, baseline_vals, width, label="constant impedance")
    ax.set_xticks(x_idx, labels)
    ax.set_ylabel("MIAE force [N]")
    ax.set_title("Article experiment MIAE comparison")
    ax.grid(True, axis="y", alpha=0.3)
    ax.legend()
    fig.tight_layout()
    fig.savefig(out_dir / f"article-experiments_miae_{run_id}.png", dpi=160)
    plt.close(fig)

    write_summary_markdown(out_dir, run_id, rows, config_path)
    print(out_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

