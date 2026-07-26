#!/usr/bin/env python3
"""Export stage-table values as shell env for operator wrappers."""

from __future__ import annotations

import argparse
from pathlib import Path

from step5d_runtime_interface import StageEnvError, build_stage_env


EXPERIMENT_ROOT = Path(__file__).resolve().parents[1]


def shell_lines(env: dict[str, str]) -> list[str]:
    return [f"{key}={value}" for key, value in env.items()]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("stage_id")
    parser.add_argument("--root", type=Path, default=EXPERIMENT_ROOT)
    args = parser.parse_args()
    try:
        env = build_stage_env(args.stage_id, args.root)
    except StageEnvError as exc:
        raise SystemExit(f"refusing to export stage env: {exc}") from exc
    print("\n".join(shell_lines(env)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
