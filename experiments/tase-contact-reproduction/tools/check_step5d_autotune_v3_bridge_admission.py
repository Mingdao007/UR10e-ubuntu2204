#!/usr/bin/python3.10
"""Resolve delivery and check exact loaded/stopped state before live authority."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from step5d_autotune_v3.bridge_admission import (
    SCHEMA,
    observe_bridge_admission,
    write_indexed_bridge_admission,
)
from step5d_autotune_v3.state import atomic_json


ROOT = Path(__file__).resolve().parents[1]
ACTION_REQUIRED_EXIT = 75
OBSERVATION_FAILED_EXIT = 69


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--delivery-observation", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--robot-host")
    parser.add_argument("--timeout-s", type=float, default=3.0)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        payload = observe_bridge_admission(
            args.root,
            compatibility_delivery_observation=args.delivery_observation,
            robot_host=args.robot_host,
            timeout_s=args.timeout_s,
        )
        write_indexed_bridge_admission(args.root, payload)
        return_code = 0 if payload["ok"] is True else ACTION_REQUIRED_EXIT
    except Exception as exc:
        payload = {
            "schema": SCHEMA,
            "state": "UNPREPARED",
            "ok": False,
            "reason_code": "ADMISSION_OBSERVATION_FAILED",
            "detail": f"{type(exc).__name__}:{exc}",
            "authority_acquired": False,
            "attempt_created": False,
        }
        return_code = OBSERVATION_FAILED_EXIT
    if args.output is not None:
        atomic_json(args.output, payload)
    print(json.dumps(payload, sort_keys=True))
    return return_code


if __name__ == "__main__":
    raise SystemExit(main())
