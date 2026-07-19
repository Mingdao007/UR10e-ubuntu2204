#!/usr/bin/env python3
"""Prepare one fingerprint-bound Step5d autotune runner launch."""

from __future__ import annotations

import argparse
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

from run_step5d_autotune_campaign import (
    _atomic_json,
    _campaign_spec,
    discover_campaign_epochs,
)
from step5d_autotune_backend import Step5dV35Backend
from step5d_autotune_batch_plan import initialize_plan, load_plan


def _sha256_path(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_machine_campaign_binding(
    path: Path,
    *,
    campaign_id: str,
    campaign_epoch: int,
    campaign_fingerprint: str,
    candidate_plan_path: Path,
    trial_overlay_plan_path: Path,
    binding_source: str,
) -> dict[str, object]:
    plan = load_plan(candidate_plan_path, campaign_id=campaign_id)
    if plan.revision < 1 or not trial_overlay_plan_path.is_file():
        raise RuntimeError("machine campaign binding requires finalized exact plans")
    payload = {
        "schema_version": "step5d_autotune_campaign_binding_v3",
        "campaign_id": campaign_id,
        "campaign_epoch": campaign_epoch,
        "campaign_fingerprint": campaign_fingerprint,
        "candidate_plan_revision": plan.revision,
        "candidate_plan_sha256": _sha256_path(candidate_plan_path),
        "trial_overlay_plan_sha256": _sha256_path(trial_overlay_plan_path),
        "binding_source": binding_source,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    _atomic_json(path, payload)
    return payload


def prepare(args: argparse.Namespace) -> dict[str, object]:
    root = args.experiment_root.resolve()
    campaign_root = args.campaign_root.resolve()
    binding_file = args.binding_file.resolve()
    if campaign_root.is_symlink() or binding_file.is_symlink():
        raise RuntimeError("campaign/binding paths must not be symlinks")
    campaign_root.mkdir(parents=True, exist_ok=True)
    backend = Step5dV35Backend(root)
    frozen = backend.freeze_fingerprint()
    chain = discover_campaign_epochs(campaign_root)
    if getattr(args, "legacy_campaign_root", None) is not None:
        raise RuntimeError("V3 launch preparation forbids legacy campaign adoption")
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
    return {
        "ok": True,
        "campaign_id": campaign.campaign_id,
        "campaign_epoch": campaign.campaign_epoch,
        "campaign_fingerprint": campaign.campaign_fingerprint,
        "campaign_root": str(campaign_root),
        "campaign_binding_file": str(binding_file),
        "machine_binding_status": "pending_exact_candidate_and_overlay_plans",
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
    parser.add_argument("--binding-file", type=Path, required=True)
    parser.add_argument("--binding-source", required=True)
    parser.add_argument("--candidate-batch-size", type=int, choices=(5, 10), default=5)
    args = parser.parse_args()
    return args


if __name__ == "__main__":
    print(json.dumps(prepare(parse_args()), sort_keys=True))
