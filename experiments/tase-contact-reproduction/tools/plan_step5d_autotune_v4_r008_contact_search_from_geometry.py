#!/usr/bin/env python3
"""Offline: plan B3 contact-search schedule from geometry calibration or Δz demo."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "tools") not in sys.path:
    sys.path.insert(0, str(ROOT / "tools"))

from step5d_autotune_v4_r008.geometry_calibration import (  # noqa: E402
    load_geometry_calibration,
)
from step5d_autotune_v4_r008.schedule_planner import (  # noqa: E402
    V_NEAR_DEFAULT_M_S,
    WAVE4_V_FAR_M_S,
    SchedulePlannerError,
    plan_contact_search_schedule,
    plan_demo_from_delta_z,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument(
        "--calibration",
        type=Path,
        help="Path to sealed geometry calibration JSON",
    )
    group.add_argument(
        "--demo-delta-z-m",
        type=float,
        help="Formula demo only: synthesize operator_sealed contact at home_z - Δz",
    )
    parser.add_argument(
        "--write-schedule",
        type=Path,
        default=None,
        help="Optional path to write planned schedule JSON",
    )
    parser.add_argument(
        "--v-far-cap-m-s",
        type=float,
        default=WAVE4_V_FAR_M_S,
        help=f"Cap for planned v_far_m_s (default: Wave4 {WAVE4_V_FAR_M_S})",
    )
    parser.add_argument(
        "--v-near-m-s",
        type=float,
        default=V_NEAR_DEFAULT_M_S,
        help=f"Planned v_near_m_s (default: {V_NEAR_DEFAULT_M_S})",
    )
    args = parser.parse_args(argv)

    try:
        if args.demo_delta_z_m is not None:
            planned = plan_demo_from_delta_z(
                args.demo_delta_z_m,
                v_far_cap_m_s=float(args.v_far_cap_m_s),
                v_near_m_s=float(args.v_near_m_s),
            )
        else:
            calibration = load_geometry_calibration(args.calibration)
            planned = plan_contact_search_schedule(
                calibration,
                v_far_cap_m_s=float(args.v_far_cap_m_s),
                v_near_m_s=float(args.v_near_m_s),
            )
    except (SchedulePlannerError, OSError, ValueError) as exc:
        print(json.dumps({"status": "error", "reason": str(exc)}, sort_keys=True))
        return 2

    payload = planned.as_dict()
    if args.write_schedule is not None:
        args.write_schedule.parent.mkdir(parents=True, exist_ok=True)
        document = dict(planned.document)
        args.write_schedule.write_text(
            json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        payload["wrote_schedule"] = str(args.write_schedule)

    print(json.dumps({"status": "ok", "planned": payload}, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
