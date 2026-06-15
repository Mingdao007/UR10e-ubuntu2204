#!/usr/bin/env python3
from __future__ import annotations

import argparse
import datetime as dt
import json
import pathlib

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import mujoco
import numpy as np
import yaml

ROOT = pathlib.Path(__file__).resolve().parents[1]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="configs/mujoco_ur10e.yaml")
    parser.add_argument("--output-dir", default=None)
    parser.add_argument("--smoke", action="store_true")
    args = parser.parse_args()

    config_path = (ROOT / args.config).resolve()
    with config_path.open("r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)

    model_path = (ROOT / cfg["ur10e_mujoco"]["mjcf_path"]).resolve()
    model = mujoco.MjModel.from_xml_path(str(model_path))
    data = mujoco.MjData(model)

    duration_s = float(cfg["ur10e_mujoco"]["smoke_duration_s"])
    if args.smoke:
        duration_s = min(duration_s, 2.0)
    steps = int(round(duration_s / model.opt.timestep))

    run_id = dt.datetime.now().strftime("%Y%m%dT%H%M%S")
    out_dir = pathlib.Path(args.output_dir) if args.output_dir else ROOT / "runs" / "ur10e_smoke" / run_id
    out_dir.mkdir(parents=True, exist_ok=True)

    qpos_hist = np.empty((steps, model.nq))
    qvel_hist = np.empty((steps, model.nv))
    tcp_hist = np.empty((steps, 3))
    contact_hist = np.empty(steps, dtype=int)
    normal_force_hist = np.empty(steps)

    site_id = mujoco.mj_name2id(model, mujoco.mjtObj.mjOBJ_SITE, "tcp_site_unverified_85mm")
    for i in range(steps):
        mujoco.mj_step(model, data)
        qpos_hist[i] = data.qpos
        qvel_hist[i] = data.qvel
        tcp_hist[i] = data.site_xpos[site_id]
        contact_hist[i] = data.ncon
        total_normal = 0.0
        for c in range(data.ncon):
            wrench = np.zeros(6)
            mujoco.mj_contactForce(model, data, c, wrench)
            total_normal += max(0.0, float(wrench[0]))
        normal_force_hist[i] = total_normal

    np.savez(
        out_dir / "ur10e-smoke_raw-state.npz",
        qpos=qpos_hist,
        qvel=qvel_hist,
        tcp=tcp_hist,
        contact_count=contact_hist,
        normal_force=normal_force_hist,
    )

    metrics = {
        "config": str(config_path),
        "model": str(model_path),
        "run_id": run_id,
        "v1_model_status": cfg.get("v1", {}).get("model_status"),
        "nq": int(model.nq),
        "nv": int(model.nv),
        "nu": int(model.nu),
        "nbody": int(model.nbody),
        "ngeom": int(model.ngeom),
        "duration_s": duration_s,
        "steps": steps,
        "max_abs_qvel_rad_s": float(np.max(np.abs(qvel_hist))),
        "max_contact_count": int(np.max(contact_hist)),
        "mean_contact_count": float(np.mean(contact_hist)),
        "max_contact_normal_force_N": float(np.max(normal_force_hist)),
        "mean_contact_normal_force_N": float(np.mean(normal_force_hist)),
        "final_tcp_xyz_m": [float(x) for x in tcp_hist[-1]],
        "warnings": [
            "v1 approximate MJCF, not calibrated",
            "85 mm TCP is unverified CAD guess",
            "smoke run is not controller validation",
        ],
    }
    with (out_dir / "metrics.yaml").open("w", encoding="utf-8") as f:
        yaml.safe_dump(metrics, f, sort_keys=False, allow_unicode=True)
    with (out_dir / "metrics.json").open("w", encoding="utf-8") as f:
        json.dump(metrics, f, indent=2)

    t = np.arange(steps) * model.opt.timestep
    plt.figure(figsize=(8, 4))
    plt.plot(t, normal_force_hist)
    plt.xlabel("time [s]")
    plt.ylabel("contact normal force [N]")
    plt.title("UR10e V1 smoke contact force")
    plt.grid(True, alpha=0.3)
    plt.tight_layout()
    plt.savefig(out_dir / f"ur10e-smoke_contact-force_{run_id}.png", dpi=160)

    plt.figure(figsize=(8, 4))
    plt.plot(t, tcp_hist[:, 2])
    plt.xlabel("time [s]")
    plt.ylabel("tcp z [m]")
    plt.title("UR10e V1 smoke TCP height")
    plt.grid(True, alpha=0.3)
    plt.tight_layout()
    plt.savefig(out_dir / f"ur10e-smoke_tcp-z_{run_id}.png", dpi=160)

    print(out_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

