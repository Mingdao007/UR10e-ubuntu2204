#!/usr/bin/env python3
from __future__ import annotations

import argparse
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
sys.path.insert(0, str(ROOT / "src"))

from tase_repro.finite_time import simulate_scalar_convergence


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="configs/paper_truth.yaml")
    parser.add_argument("--output-dir", default=None)
    parser.add_argument("--smoke", action="store_true")
    args = parser.parse_args()

    config_path = (ROOT / args.config).resolve()
    with config_path.open("r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)

    pending = cfg["paper"]["section_v"].get("pending_pdf_verify", [])
    if pending:
        if cfg.get("v1", {}).get("allow_pending_pdf_verify", False):
            print(f"WARNING: v1 running with pending_pdf_verify fields: {', '.join(pending)}", file=sys.stderr)
        else:
            raise SystemExit(f"pending_pdf_verify fields require resolution: {pending}")

    fig_cfg = cfg["fig5"].copy()
    if args.smoke:
        fig_cfg["duration_s"] = min(float(fig_cfg["duration_s"]), 0.4)

    run_id = dt.datetime.now().strftime("%Y%m%dT%H%M%S")
    out_dir = pathlib.Path(args.output_dir) if args.output_dir else ROOT / "runs" / "fig5_r_sweep" / run_id
    out_dir.mkdir(parents=True, exist_ok=True)

    results = {}
    metrics = {"config": str(config_path), "run_id": run_id, "pending_pdf_verify": pending, "r": {}}
    for r in cfg["paper"]["section_v"]["r_sweep"]:
        sim = simulate_scalar_convergence(r=float(r), **fig_cfg)
        results[f"t_r_{r}"] = sim["t"]
        results[f"error_r_{r}"] = sim["error"]
        metrics["r"][str(r)] = {
            "convergence_time_s": sim["convergence_time_s"],
            "tail_abs_error": sim["tail_abs_error"],
            "max_abs_error": sim["max_abs_error"],
        }

    np.savez(out_dir / "fig5_r-sweep_raw.npz", **results)
    with (out_dir / "metrics.yaml").open("w", encoding="utf-8") as f:
        yaml.safe_dump(metrics, f, sort_keys=False, allow_unicode=True)
    with (out_dir / "metrics.json").open("w", encoding="utf-8") as f:
        json.dump(metrics, f, indent=2, allow_nan=True)

    plt.figure(figsize=(8, 5))
    for r in cfg["paper"]["section_v"]["r_sweep"]:
        plt.plot(results[f"t_r_{r}"], np.abs(results[f"error_r_{r}"]), label=f"r={r}")
    plt.yscale("log")
    plt.xlabel("time [s]")
    plt.ylabel("|error|")
    plt.title("Fig.5 V1 finite-time r sweep")
    plt.grid(True, which="both", alpha=0.3)
    plt.legend()
    plt.tight_layout()
    plt.savefig(out_dir / f"fig5_r-sweep_{run_id}.png", dpi=160)
    print(out_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

