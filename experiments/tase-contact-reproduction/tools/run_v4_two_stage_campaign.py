#!/usr/bin/env python3
"""Offline/resume entrypoint for the isolated V4 two-stage scheduler.

This command only creates or resumes the campaign ledger and emits typed
dispatch/proposal receipts.  It never opens Dashboard, RTDE, Kunwei, or a
robot writer; a live owner must consume the receipt through its own gates.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from step5d_autotune_v4_r013.v4_two_stage_campaign import (
    V4Stage,
    V4StageCampaignV1,
    load_three_stage_config,
)


HISTORICAL_COORDINATE = {
    "force_p_gain": 0.019027313840405524,
    "force_damping": 188.36079701683204,
    "force_i_gain": 0.0,
    "i_off": True,
    "normal_filter_tau_s": 0.04375,
    "orientation_ko": 0.05,
    "motion_kp": 2.5226892457611436,
    "target_force_n": 5.0,
}


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--stage", choices=[stage.value for stage in V4Stage], required=True)
    parser.add_argument("--ledger", type=Path, required=True)
    parser.add_argument("--json", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    raw = json.loads(args.config.read_text(encoding="utf-8"))
    stage_a, stage_b, stage_b2 = load_three_stage_config(raw)
    config = {
        V4Stage.FF_IOFF_100.value: stage_a,
        V4Stage.FF_ION_6D_100.value: stage_b,
        V4Stage.FF_ION_LIMIT_7D_100.value: stage_b2,
    }[args.stage]
    seed = dict(HISTORICAL_COORDINATE)
    if config.stage in {V4Stage.FF_ION_6D_100, V4Stage.FF_ION_LIMIT_7D_100}:
        seed.update({"force_i_gain": 0.008610779292198037, "i_off": False})
    campaign = V4StageCampaignV1(
        config=config,
        seed_candidate=seed,
        fingerprint_sha256=config.fingerprint_sha256,
        ledger_path=args.ledger,
    )
    dispatch = campaign.ask()
    value: dict[str, Any] = {
        "status": "dispatch_ready",
        "stage": config.stage.value,
        "fingerprint_sha256": config.fingerprint_sha256,
        "dispatch": dispatch.as_dict(),
        "campaign": campaign.status(),
        "live_authority": False,
    }
    print(json.dumps(value, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
