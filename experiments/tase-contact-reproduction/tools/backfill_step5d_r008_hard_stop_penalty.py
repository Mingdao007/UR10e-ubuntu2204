#!/usr/bin/env python3
"""Backfill r008-hard-stop-penalty.jsonl from a queue request + stop-dominant dump.

Example (far005 disp42)::

  python3 tools/backfill_step5d_r008_hard_stop_penalty.py \\
    --run-dir runs/.../live_20260805_120727_b3_wave4_far005_autotune \\
    --dispatch-sequence 42
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from step5d_autotune_v4_r008.hard_stop_penalty import (  # noqa: E402
    candidate_fields_from_overlay,
    has_penalty_for_dispatch,
    historical_max_mae_n,
    record_hard_stop_penalty_for_run,
)
from step5d_autotune_v4_r008.lattice import point_from_physical  # noqa: E402


def _load_request_overlay(run_dir: Path, dispatch_sequence: int) -> dict[str, Any]:
    path = run_dir / "r006-queue" / "requests" / f"{int(dispatch_sequence):012d}.json"
    if not path.is_file():
        raise SystemExit(f"queue request missing: {path}")
    payload = json.loads(path.read_text(encoding="utf-8"))
    overlay = payload.get("overlay")
    if not isinstance(overlay, dict):
        raise SystemExit(f"queue request lacks overlay: {path}")
    return overlay


def _point_key_from_overlay(overlay: dict[str, Any]) -> list[Any]:
    fields = candidate_fields_from_overlay(overlay)
    required = (
        "force_p_gain",
        "force_damping",
        "normal_filter_tau_s",
        "orientation_ko",
        "motion_kp",
    )
    missing = [k for k in required if k not in fields]
    if missing:
        raise SystemExit(f"overlay missing fields: {missing}")
    point = point_from_physical(
        force_p_gain=fields["force_p_gain"],
        force_damping=fields["force_damping"],
        normal_filter_tau_s=fields["normal_filter_tau_s"],
        force_i_gain=float(fields.get("force_i_gain") or 0.0),
        orientation_ko=fields["orientation_ko"],
        motion_kp=fields["motion_kp"],
    )
    return list(point.to_parameter_point().key)


def _attempt_sequence_for_dispatch(run_dir: Path, dispatch_sequence: int) -> int | None:
    executions = run_dir / "r006-queue" / "r005-executions.jsonl"
    if not executions.is_file():
        return None
    # Prefer matching via request file enqueue; fall back to dispatch==attempt heuristic.
    for line in executions.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if int(row.get("attempt_sequence", -1)) == int(dispatch_sequence):
            return int(dispatch_sequence)
    return int(dispatch_sequence)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--dispatch-sequence", type=int, required=True)
    parser.add_argument(
        "--kind",
        default="BO_TRIAL",
        help="attempt kind label stored on the penalty row",
    )
    args = parser.parse_args(list(argv) if argv is not None else None)
    run_dir = args.run_dir.resolve()
    dispatch = int(args.dispatch_sequence)
    if has_penalty_for_dispatch(run_dir, dispatch):
        print(
            json.dumps(
                {
                    "status": "already_present",
                    "run_dir": str(run_dir),
                    "dispatch_sequence": dispatch,
                },
                indent=2,
                sort_keys=True,
            )
        )
        return 0
    overlay = _load_request_overlay(run_dir, dispatch)
    point_key = _point_key_from_overlay(overlay)
    candidate = candidate_fields_from_overlay(overlay)
    # Ensure orientation/motion present for the stored candidate snapshot.
    for key in ("orientation_ko", "motion_kp"):
        if key in overlay and key not in candidate:
            try:
                candidate[key] = float(overlay[key])
            except (TypeError, ValueError):
                pass
    hist = historical_max_mae_n(run_dir)
    row = record_hard_stop_penalty_for_run(
        run_dir,
        dispatch_sequence=dispatch,
        attempt_sequence=_attempt_sequence_for_dispatch(run_dir, dispatch),
        kind=str(args.kind),
        point_key=point_key,
        candidate=candidate,
    )
    print(
        json.dumps(
            {
                "status": "appended" if row is not None else "skipped",
                "run_dir": str(run_dir),
                "dispatch_sequence": dispatch,
                "historical_max_mae_n": hist,
                "penalty_mae_n": None if row is None else row.get("penalty_mae_n"),
                "point_key": point_key,
                "reason_code": None if row is None else row.get("reason_code"),
                "reason": None if row is None else row.get("reason"),
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
