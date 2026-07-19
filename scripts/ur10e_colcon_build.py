#!/usr/bin/env python3
"""Serialize one colcon tree while parallelizing its two UR10e packages."""

from __future__ import annotations

import argparse
import fcntl
import os
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
ALLOWED_PACKAGES = {"ur10e_bringup", "ur10e_example_controllers"}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("packages", nargs="+", choices=sorted(ALLOWED_PACKAGES))
    args = parser.parse_args()
    packages = list(dict.fromkeys(args.packages))
    lock_path = ROOT / ".ur10e_colcon_build.lock"
    with lock_path.open("a+", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        environment = os.environ.copy()
        environment.update(
            {
                "CMAKE_BUILD_PARALLEL_LEVEL": "8",
                "OMP_NUM_THREADS": "1",
                "OPENBLAS_NUM_THREADS": "1",
                "MKL_NUM_THREADS": "1",
                "NUMEXPR_NUM_THREADS": "1",
                "UR10E_CONCURRENCY_CONTRACT": "ur10e_concurrency_contract_v1",
            }
        )
        completed = subprocess.run(
            [
                "colcon",
                "build",
                "--executor",
                "parallel",
                "--parallel-workers",
                str(min(2, len(packages))),
                "--packages-select",
                *packages,
                "--symlink-install",
            ],
            cwd=ROOT,
            env=environment,
            check=False,
        )
    return completed.returncode


if __name__ == "__main__":
    raise SystemExit(main())
