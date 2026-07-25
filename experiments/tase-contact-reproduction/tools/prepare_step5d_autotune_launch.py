#!/usr/bin/python3.10
"""Prepare one fingerprint-bound Step5d autotune runner launch."""

from __future__ import annotations

import argparse
import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from step5d_autotune_v3.campaign_basis import (
    atomic_json,
    campaign_spec,
    discover_campaign_epochs,
)
from step5d_autotune_v3.launch_basis import read_and_validate_launch_basis
from step5d_autotune_batch_plan import (
    initialize_plan,
    initialize_rolling_plan,
    load_plan,
)


@dataclass(frozen=True)
class LaunchPreparationRequest:
    experiment_root: Path
    campaign_root: Path
    binding_file: Path
    binding_source: str
    launch_profile_path: Path
    candidate_batch_size: int
    rolling_plan: bool
    campaign_fingerprint: str | None = None
    launch_basis_path: Path | None = None
    expected_basis_sha256: str | None = None
    owner_pid: int | None = None
    owner_starttime: int | None = None


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
    atomic_json(path, payload)
    return payload


def prepare(args: LaunchPreparationRequest) -> dict[str, object]:
    root = args.experiment_root.resolve()
    campaign_root = args.campaign_root.resolve()
    binding_file = args.binding_file.resolve()
    launch_profile_path = args.launch_profile_path.resolve()
    if campaign_root.is_symlink() or binding_file.is_symlink():
        raise RuntimeError("campaign/binding paths must not be symlinks")
    if not launch_profile_path.is_file() or launch_profile_path.is_symlink():
        raise RuntimeError("launch preparation requires an exact regular launch profile")
    if not args.binding_source.strip():
        raise RuntimeError("launch preparation requires a nonempty binding source")
    if (
        args.campaign_fingerprint is None
        or args.launch_basis_path is None
        or args.expected_basis_sha256 is None
        or args.owner_pid is None
        or args.owner_starttime is None
    ):
        raise RuntimeError("launch preparation requires the verified launch basis and campaign fingerprint")
    basis = read_and_validate_launch_basis(
        args.launch_basis_path,
        owner_pid=args.owner_pid,
        owner_starttime=args.owner_starttime,
        expected_basis_sha256=args.expected_basis_sha256,
    )
    if basis["campaign_fingerprint"] != args.campaign_fingerprint:
        raise RuntimeError("campaign fingerprint differs from the verified launch basis")
    campaign_root.mkdir(parents=True, exist_ok=True)
    chain = discover_campaign_epochs(campaign_root)
    fingerprint = args.campaign_fingerprint
    if chain:
        latest = chain[-1]
        retained = latest.manifest.get("frozen_fingerprint")
        if not isinstance(retained, dict):
            raise RuntimeError("latest campaign epoch lacks frozen fingerprint")
        epoch = (
            latest.epoch
            if retained.get("composite_fingerprint") == fingerprint
            else latest.epoch + 1
        )
        campaign_id = latest.campaign.campaign_id
    else:
        epoch = 1
        campaign_id = None
    campaign = campaign_spec(
        root,
        fingerprint,
        epoch,
        campaign_id=campaign_id,
    )
    plan_path = campaign_root / "control" / "candidate_plan.json"
    if plan_path.exists():
        load_plan(plan_path, campaign_id=campaign.campaign_id)
    else:
        if args.rolling_plan:
            initialize_rolling_plan(plan_path, campaign_id=campaign.campaign_id)
        else:
            initialize_plan(
                plan_path,
                campaign_id=campaign.campaign_id,
                batch_size=args.candidate_batch_size,
            )
    return {
        "ok": True,
        "campaign_id": campaign.campaign_id,
        "campaign_epoch": campaign.campaign_epoch,
        "campaign_fingerprint": campaign.campaign_fingerprint,
        "campaign_root": str(campaign_root),
        "campaign_binding_file": str(binding_file),
        "launch_profile_path": str(launch_profile_path),
        "launch_profile_sha256": _sha256_path(launch_profile_path),
        "machine_binding_status": "pending_exact_candidate_and_overlay_plans",
        "candidate_plan": str(plan_path),
    }


def parse_args() -> LaunchPreparationRequest:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--experiment-root",
        type=Path,
        default=Path(__file__).resolve().parents[1],
    )
    parser.add_argument("--campaign-root", type=Path, required=True)
    parser.add_argument("--binding-file", type=Path, required=True)
    parser.add_argument("--binding-source", required=True)
    parser.add_argument(
        "--launch-profile",
        type=Path,
        default=Path(__file__).resolve().parents[1]
        / "config/step5/step5d_autotune_v3_launch_profile.json",
    )
    parser.add_argument("--candidate-batch-size", type=int, choices=(5, 10), default=5)
    parser.add_argument("--campaign-fingerprint")
    parser.add_argument("--launch-basis", dest="launch_basis_path", type=Path, required=True)
    parser.add_argument("--launch-basis-sha256", dest="expected_basis_sha256", required=True)
    parser.add_argument("--owner-pid", type=int, required=True)
    parser.add_argument("--owner-starttime", type=int, required=True)
    args = parser.parse_args()
    return LaunchPreparationRequest(
        experiment_root=args.experiment_root,
        campaign_root=args.campaign_root,
        binding_file=args.binding_file,
        binding_source=args.binding_source,
        launch_profile_path=args.launch_profile,
        candidate_batch_size=args.candidate_batch_size,
        rolling_plan=False,
        campaign_fingerprint=args.campaign_fingerprint,
        launch_basis_path=args.launch_basis_path,
        expected_basis_sha256=args.expected_basis_sha256,
        owner_pid=args.owner_pid,
        owner_starttime=args.owner_starttime,
    )


if __name__ == "__main__":
    print(json.dumps(prepare(parse_args()), sort_keys=True))
