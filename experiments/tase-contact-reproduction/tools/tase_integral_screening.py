"""Build the randomized, offline four-arm integral screening schedule.

This module only creates identities and parameter files. It never opens a
device or dispatches a Figure-eight attempt.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import numpy as np


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG = ROOT / "config" / "tase_integral_screening_v1.json"


def load_screening_config(path: Path = DEFAULT_CONFIG) -> dict[str, Any]:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if payload.get("schema") != "tase.integral-screening-v1":
        raise ValueError("screening schema differs")
    if payload.get("method") != "TASE_RNN_MATURE":
        raise ValueError("screening method must be TASE_RNN_MATURE")
    if payload.get("protocol_id") != "figure8_window60_r013_compat_v1":
        raise ValueError("screening must use the 60 s R013-compatible protocol")
    if int(payload.get("repeats_per_arm", 0)) != 5:
        raise ValueError("screening requires five repeats per arm")
    arms = payload.get("arms")
    if not isinstance(arms, list) or [a.get("arm_id") for a in arms] != ["A", "B", "C", "D"]:
        raise ValueError("screening arms must be ordered A, B, C, D")
    seen = set()
    for arm in arms:
        required = {"arm_id", "label", "force_integral_limit_n_s", "force_integral_policy", "force_integral_authority_error_n"}
        if set(arm) != required:
            raise ValueError("screening arm fields differ")
        if arm["arm_id"] in seen:
            raise ValueError("screening arm ids must be unique")
        seen.add(arm["arm_id"])
        if float(arm["force_integral_limit_n_s"]) not in {0.1, 0.5, 1.0}:
            raise ValueError("screening integral limit is outside the approved set")
        if arm["force_integral_policy"] not in {"legacy-clamp-v1", "conditional-double-clamp-v1"}:
            raise ValueError("screening integral policy is unknown")
    return payload


def build_schedule(config: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    config = load_screening_config() if config is None else config
    rng = np.random.default_rng(int(config["seed"]))
    arms = config["arms"]
    rows: list[dict[str, Any]] = []
    for block in range(int(config["repeats_per_arm"])):
        order = [arms[int(i)] for i in rng.permutation(len(arms))]
        for position, arm in enumerate(order):
            rows.append({
                "schema": "tase.integral-screening-attempt-v1",
                "protocol_id": config["protocol_id"],
                "duration_token": config["duration_token"],
                "method": config["method"],
                "block": block,
                "position": position,
                "arm_id": arm["arm_id"],
                "candidate_id": f"screen-{arm['arm_id']}-{block:02d}",
                "Md_scalar": float(config["Md_scalar"]),
                "Bd_scalar": float(config["Bd_scalar"]),
                "frozen": {
                    "kp": 4.0,
                    "ko": 5.0,
                    "kf": 1.0,
                    "force_target_n": float(config["target_force_n"]),
                    "force_integral_limit_n_s": float(arm["force_integral_limit_n_s"]),
                    "force_integral_policy": arm["force_integral_policy"],
                    "force_integral_authority_error_n": float(arm["force_integral_authority_error_n"]),
                    "force_sign_convention": "step5_step6_positive_normal_load",
                },
                "status": "planned",
            })
    return rows


def write_schedule(output: Path, config_path: Path = DEFAULT_CONFIG) -> Path:
    config = load_screening_config(config_path)
    rows = build_schedule(config)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps({"schema": "tase.integral-screening-schedule-v1", "rows": rows}, indent=2) + "\n", encoding="utf-8")
    return output


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    print(write_schedule(args.output, args.config))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
