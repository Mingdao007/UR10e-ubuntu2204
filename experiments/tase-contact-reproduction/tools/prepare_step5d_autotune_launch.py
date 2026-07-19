#!/usr/bin/env python3
"""Prepare one fingerprint-bound Step5d autotune runner launch."""

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
    binding_input = getattr(args, "binding_file", None)
    legacy_authorization = getattr(args, "authorization_file", None)
    binding_file = (binding_input or legacy_authorization).resolve()
    if campaign_root.is_symlink() or binding_file.is_symlink():
        raise RuntimeError("campaign/binding paths must not be symlinks")
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
        initialize_plan(
            plan_path,
            campaign_id=campaign.campaign_id,
            batch_size=getattr(args, "candidate_batch_size", 5),
        )
    if binding_input is not None:
        payload = {
            "schema_version": "step5d_autotune_campaign_binding_v2",
            "campaign_id": campaign.campaign_id,
            "campaign_epoch": campaign.campaign_epoch,
            "campaign_fingerprint": campaign.campaign_fingerprint,
            "bounded_baseline_and_loop": True,
            "controller_readback_verified": True,
            "binding_source": args.binding_source,
            "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
    else:
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
    _atomic_json(binding_file, payload)
    return {
        "ok": True,
        "campaign_id": campaign.campaign_id,
        "campaign_epoch": campaign.campaign_epoch,
        "campaign_fingerprint": campaign.campaign_fingerprint,
        "campaign_root": str(campaign_root),
        "campaign_binding_file": str(binding_file),
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
    binding = parser.add_mutually_exclusive_group(required=True)
    binding.add_argument("--binding-file", type=Path)
    binding.add_argument("--authorization-file", type=Path)
    parser.add_argument("--binding-source")
    parser.add_argument("--authorization-source")
    parser.add_argument("--candidate-batch-size", type=int, choices=(5, 10), default=5)
    args = parser.parse_args()
    if args.binding_file is not None and not args.binding_source:
        parser.error("--binding-source is required with --binding-file")
    if args.authorization_file is not None and not args.authorization_source:
        parser.error("--authorization-source is required with --authorization-file")
    return args


if __name__ == "__main__":
    print(json.dumps(prepare(parse_args()), sort_keys=True))
