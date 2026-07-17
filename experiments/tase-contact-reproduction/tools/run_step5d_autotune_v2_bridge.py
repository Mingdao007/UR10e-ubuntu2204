#!/usr/bin/env python3
"""Same-PID launcher for the frozen Step5d autotune v2 live bridge."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
STABLE_PYTHON_RUNTIME = Path("/home/andy/.codex-python/ur10e-digital-twin-20260711")
ROS_PYTHON_PATHS = (
    Path("/opt/ros/humble/lib/python3.10/site-packages"),
    Path("/opt/ros/humble/local/lib/python3.10/dist-packages"),
)
RUNTIME_LIBRARY_PATHS = (
    STABLE_PYTHON_RUNTIME / "nvidia/cuda_nvrtc/lib",
    STABLE_PYTHON_RUNTIME / "nvidia/nvjitlink/lib",
    STABLE_PYTHON_RUNTIME / "nvidia/cuda_runtime/lib",
    Path("/opt/ros/humble/lib"),
)


def _prepend_paths(current: str | None, paths: tuple[Path, ...]) -> str:
    values = [str(path) for path in paths if path.is_dir()]
    if current:
        values.append(current)
    return os.pathsep.join(values)


def bridge_environment(base: dict[str, str] | None = None) -> dict[str, str]:
    environment = dict(os.environ if base is None else base)
    for name, value in {
        "AMENT_PREFIX_PATH": "/opt/ros/humble",
        "CMAKE_PREFIX_PATH": "/opt/ros/humble",
        "ROS_DISTRO": "humble",
        "ROS_VERSION": "2",
        "ROS_PYTHON_VERSION": "3",
    }.items():
        environment.setdefault(name, value)
    environment["PYTHONPATH"] = _prepend_paths(
        environment.get("PYTHONPATH"),
        (STABLE_PYTHON_RUNTIME, *ROS_PYTHON_PATHS),
    )
    environment["LD_LIBRARY_PATH"] = _prepend_paths(
        environment.get("LD_LIBRARY_PATH"),
        RUNTIME_LIBRARY_PATHS,
    )
    return environment


def bridge_argv(root: Path, runtime_root: Path) -> list[str]:
    mailbox = runtime_root / "command.json"
    output = runtime_root / "bridge"
    return [
        sys.executable, str(root / "tools/kunwei_rtde_bridge.py"),
        "--allow-kunwei-stream-command", "--write-rtde-inputs",
        "--baseline-s", "5", "--rezero-s", "1", "--duration-s", "180",
        "--rtde-hz", "500", "--socket-timeout-s", "0", "--sensor-stale-s", "0.10",
        "--target-force-n", "12", "--normal-axis", "fz", "--normal-sign", "1",
        "--max-normal-force-n", "60", "--max-force-norm-n", "100",
        "--max-torque-norm-nm", "3",
        "--bridge-mode", "line", "--bridge-profile", "step5d_strict_rnn_autotune_v1",
        "--bridge-path-shape", "cycloid", "--bridge-line-speed-m-s", "0.003",
        "--bridge-line-settle-s", "0", "--bridge-path-p-gain", "1.5",
        "--bridge-motion-limit-m-s", "0.004", "--bridge-total-linear-limit-m-s", "0.006",
        "--bridge-normal-velocity-limit-m-s", "0.003",
        "--bridge-force-p-gain", "0.001", "--bridge-force-i-gain", "0.00001",
        "--bridge-force-damping", "7", "--bridge-normal-command-sign", "1",
        "--bridge-integral-limit-n-s", "10", "--bridge-min-force-for-control-n", "1",
        "--bridge-acquire-grace-s", "0.25", "--bridge-reacquire-velocity-m-s", "0.001",
        "--bridge-orientation-gain", "0.20", "--bridge-orientation-wx-sign", "1",
        "--bridge-orientation-wy-sign", "1", "--bridge-angular-limit-rad-s", "0.015",
        "--bridge-contact-offset-min-fz-n", "1", "--bridge-normal-follow-mode", "filtered_live",
        "--bridge-normal-filter-tau-s", "0.35", "--bridge-normal-max-rate-rad-s", "0.05",
        "--bridge-normal-min-force-n", "2", "--bridge-normal-max-angle-from-latch-deg", "20",
        "--bridge-normal-friction-projection", "on",
        "--step5c-qdot-limit-rad-s", "0.15", "--step5c-joint-damping", "0.0001",
        "--step5d-stage25-control-mode", "speedj_rnn_live",
        "--step5d-qdot-limit-rad-s", "0.5", "--step5d-epsilon", "0.022",
        "--step5d-sigr-exponent-r", "1", "--step5d-rnn-inner-iterations", "1",
        "--step5d-rnn-backend", "cupy",
        "--step5d-tcp-offset-tool0-m", "0", "0", "0.1221",
        "--step5d-preload-filtered-min-n", "5", "--step5d-preload-filtered-max-n", "22",
        "--step5d-preload-raw-min-n", "3", "--step5d-preload-raw-max-n", "25",
        "--step5d-preload-force-norm-max-n", "100", "--step5d-preload-hold-s", "0.1",
        "--step5d-preload-timeout-s", "10",
        "--step5d-autotune-force-p", "0.001", "--step5d-autotune-force-i", "0.00001",
        "--step5d-autotune-force-damping", "7",
        "--step5d-autotune-normal-rate-rad-s", "0.05",
        "--step5d-autotune-host-slew-rad-s2", "0.5",
        "--step5d-autotune-speedj-acceleration-rad-s2", "0.5",
        "--step5d-autotune-campaign-epoch", "0", "--step5d-autotune-trial-id", "0",
        "--step5d-autotune-command", "0", "--step5d-autotune-candidate-token", "0",
        "--step5d-autotune-execution-profile-id", "0",
        "--step5d-autotune-command-sequence", "0",
        "--step5d-autotune-command-mailbox", str(mailbox),
        "--output-dir", str(output),
    ]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--runtime-root", type=Path)
    args = parser.parse_args(argv)
    runtime_value = args.runtime_root or (
        Path(os.environ["STEP5D_AUTOTUNE_V2_RUNTIME_ROOT"])
        if os.environ.get("STEP5D_AUTOTUNE_V2_RUNTIME_ROOT")
        else None
    )
    if runtime_value is None or not runtime_value.is_absolute():
        raise SystemExit("STEP5D_AUTOTUNE_V2_RUNTIME_ROOT must be an absolute path")
    command = bridge_argv(ROOT, runtime_value.resolve())
    environment = bridge_environment()
    if args.check:
        print(json.dumps({
            "schema": "step5d.autotune.bridge-launch/v2",
            "argv": command,
            "pythonpath": environment["PYTHONPATH"],
            "ld_library_path": environment["LD_LIBRARY_PATH"],
            "ament_prefix_path": environment["AMENT_PREFIX_PATH"],
        }, indent=2))
        return 0
    required = (
        "STEP5D_AUTOTUNE_V2_ADAPTER",
        "STEP5D_AUTOTUNE_V2_DEPLOYMENT_ID",
        "STEP5D_AUTOTUNE_V2_LAUNCH_NONCE",
    )
    missing = [name for name in required if not os.environ.get(name)]
    if missing or os.environ.get("STEP5D_AUTOTUNE_V2_ADAPTER") != "1":
        raise SystemExit("v2 bridge launcher environment is incomplete")
    os.execvpe(command[0], command, environment)
    raise AssertionError("execvpe returned unexpectedly")


if __name__ == "__main__":
    raise SystemExit(main())
