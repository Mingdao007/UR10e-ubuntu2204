#!/usr/bin/env python3
"""Prepare one externally gated Step5d autotune runner launch."""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

from run_step5d_autotune_campaign import (
    _atomic_json,
    _campaign_spec,
    discover_campaign_epochs,
    select_campaign_epoch,
)
from step5d_autotune_backend import Step5dV35Backend
from step5d_autotune_batch_plan import initialize_plan, load_plan


def prepare(args: argparse.Namespace) -> dict[str, object]:
    root = args.experiment_root.resolve()
    campaign_root = args.campaign_root.resolve()
    authorization_file = args.authorization_file.resolve()
    if campaign_root.is_symlink() or authorization_file.is_symlink():
        raise RuntimeError("campaign/authorization paths must not be symlinks")
    campaign_root.mkdir(parents=True, exist_ok=True)
    backend = Step5dV35Backend(root)
    frozen = backend.freeze_fingerprint()
    chain = discover_campaign_epochs(campaign_root)
    legacy_root: Path | None = None
    if chain:
        latest = chain[-1]
        retained = latest.manifest.get("frozen_fingerprint")
        if not isinstance(retained, dict):
            raise RuntimeError("latest campaign epoch lacks frozen fingerprint")
        epoch = (
            latest.epoch
            if retained.get("composite_fingerprint") == frozen.composite_fingerprint
            else latest.epoch + 1
        )
        campaign_id = latest.campaign.campaign_id
        if args.legacy_campaign_root is not None:
            raise RuntimeError("legacy root is forbidden after persistent campaign adoption")
    elif args.legacy_campaign_root is not None:
        legacy_root = args.legacy_campaign_root.resolve()
        legacy = select_campaign_epoch(
            legacy_root,
            campaign_epoch=getattr(args, "legacy_campaign_epoch", None),
        )
        epoch = legacy.epoch + 1
        campaign_id = legacy.campaign.campaign_id
    else:
        epoch = 1
        campaign_id = None
    campaign = _campaign_spec(
        root,
        frozen.composite_fingerprint,
        epoch,
        campaign_id=campaign_id,
    )
    plan_path = campaign_root / "control" / "candidate_plan.json"
    if plan_path.exists():
        load_plan(plan_path, campaign_id=campaign.campaign_id)
    else:
        initialize_plan(plan_path, campaign_id=campaign.campaign_id)
    payload = {
        "schema_version": "step5d_autotune_campaign_authorization_v1",
        "campaign_id": campaign.campaign_id,
        "campaign_epoch": campaign.campaign_epoch,
        "campaign_fingerprint": campaign.campaign_fingerprint,
        "bounded_baseline_and_loop": True,
        "live_authorized": True,
        "controller_readback_verified": True,
        "authorization_source": args.authorization_source,
        "authorized_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    _atomic_json(authorization_file, payload)
    return {
        "ok": True,
        "campaign_id": campaign.campaign_id,
        "campaign_epoch": campaign.campaign_epoch,
        "campaign_fingerprint": campaign.campaign_fingerprint,
        "campaign_root": str(campaign_root),
        "authorization_file": str(authorization_file),
        "legacy_campaign_root": None if legacy_root is None else str(legacy_root),
        "candidate_plan": str(plan_path),
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--experiment-root",
        type=Path,
        default=Path(__file__).resolve().parents[1],
    )
    parser.add_argument("--campaign-root", type=Path, required=True)
    parser.add_argument("--legacy-campaign-root", type=Path)
    parser.add_argument("--legacy-campaign-epoch", type=int)
    parser.add_argument("--authorization-file", type=Path, required=True)
    parser.add_argument("--authorization-source", required=True)
    return parser.parse_args()


if __name__ == "__main__":
    print(json.dumps(prepare(parse_args()), sort_keys=True))
