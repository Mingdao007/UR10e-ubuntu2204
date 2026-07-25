#!/usr/bin/python3.10
"""Prepare one fingerprint-bound Step5d autotune runner launch."""

from __future__ import annotations

import argparse
import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from step5d_autotune_v3.state import atomic_json
from step5d_autotune_v3.release_identity import (
    load_current_release,
    release_payload_path,
)
from step5d_campaign_identity import campaign_spec, discover_campaign_epochs


INITIAL_MANIFEST_PATH = "config/step5d/parameter_receiver_initial.json"
RECEIVER_PLAN_SCHEMA = "step5d.parameter-receiver/launch-plan-v1"
RECEIVER_SOURCE_SCHEMA = "step5d.parameter-receiver/source-binding-v1"


@dataclass(frozen=True)
class LaunchPreparationRequest:
    experiment_root: Path
    campaign_root: Path
    binding_file: Path
    binding_source: str
    launch_profile_path: Path
    candidate_batch_size: int
    rolling_plan: bool


def _sha256_path(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _campaign_fingerprint(
    *,
    release_manifest_sha256: str,
    launch_profile_sha256: str,
) -> str:
    encoded = json.dumps(
        {
            "schema": "step5d.parameter-receiver/campaign-fingerprint-v1",
            "release_manifest_sha256": release_manifest_sha256,
            "launch_profile_sha256": launch_profile_sha256,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("ascii")
    return hashlib.sha256(encoded).hexdigest()


def _receiver_documents(
    *,
    campaign_id: str,
    release_manifest_sha256: str,
    launch_profile_path: Path,
    initial_manifest_path: Path,
) -> tuple[dict[str, object], dict[str, object]]:
    plan = {
        "schema": RECEIVER_PLAN_SCHEMA,
        "campaign_id": campaign_id,
        "revision": 1,
        "protocol": "v3_full_home_parameter_receiver_v1",
        "unbounded": True,
        "one_inflight": True,
        "optimizer_required": False,
    }
    source = {
        "schema": RECEIVER_SOURCE_SCHEMA,
        "release_manifest_sha256": release_manifest_sha256,
        "launch_profile_sha256": _sha256_path(launch_profile_path),
        "initial_manifest_sha256": _sha256_path(initial_manifest_path),
    }
    return plan, source


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
    if candidate_plan_path.is_symlink() or not candidate_plan_path.is_file():
        raise RuntimeError("parameter receiver plan must be a real file")
    plan = json.loads(candidate_plan_path.read_text(encoding="utf-8"))
    revision = plan.get("revision") if isinstance(plan, dict) else None
    if (
        not isinstance(plan, dict)
        or plan.get("schema") != RECEIVER_PLAN_SCHEMA
        or plan.get("campaign_id") != campaign_id
        or isinstance(revision, bool)
        or not isinstance(revision, int)
        or revision < 1
        or trial_overlay_plan_path.is_symlink()
        or not trial_overlay_plan_path.is_file()
    ):
        raise RuntimeError("machine campaign binding requires exact receiver plans")
    payload = {
        "schema_version": "step5d_autotune_campaign_binding_v3",
        "campaign_id": campaign_id,
        "campaign_epoch": campaign_epoch,
        "campaign_fingerprint": campaign_fingerprint,
        "candidate_plan_revision": revision,
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
    campaign_root.mkdir(parents=True, exist_ok=True)
    release = load_current_release(root)
    initial_manifest_path = release_payload_path(
        root,
        release,
        INITIAL_MANIFEST_PATH,
    )
    fingerprint = _campaign_fingerprint(
        release_manifest_sha256=release.manifest_sha256,
        launch_profile_sha256=_sha256_path(launch_profile_path),
    )
    chain = discover_campaign_epochs(campaign_root)
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
    legacy_binding_root = (
        campaign_root
        / "control"
        / "parameter_receiver_bindings"
        / release.manifest_sha256
    )
    plan_path = legacy_binding_root / "plan.json"
    source_path = legacy_binding_root / "source.json"
    receiver_root = campaign_root / "control" / "parameter_receiver"
    plan, source = _receiver_documents(
        campaign_id=campaign.campaign_id,
        release_manifest_sha256=release.manifest_sha256,
        launch_profile_path=launch_profile_path,
        initial_manifest_path=initial_manifest_path,
    )
    for path, payload in ((plan_path, plan), (source_path, source)):
        if path.exists() or path.is_symlink():
            if path.is_symlink() or json.loads(
                path.read_text(encoding="utf-8")
            ) != payload:
                raise RuntimeError(f"existing receiver binding differs: {path.name}")
        else:
            atomic_json(path, payload)
    return {
        "ok": True,
        "campaign_id": campaign.campaign_id,
        "campaign_epoch": campaign.campaign_epoch,
        "campaign_fingerprint": campaign.campaign_fingerprint,
        "campaign_root": str(campaign_root),
        "campaign_binding_file": str(binding_file),
        "launch_profile_path": str(launch_profile_path),
        "launch_profile_sha256": _sha256_path(launch_profile_path),
        "machine_binding_status": "ready_parameter_receiver",
        "candidate_plan": str(plan_path),
        "trial_overlay_plan": str(source_path),
        "initial_manifest": str(initial_manifest_path),
        "receiver_root": str(receiver_root),
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
    args = parser.parse_args()
    return LaunchPreparationRequest(
        experiment_root=args.experiment_root,
        campaign_root=args.campaign_root,
        binding_file=args.binding_file,
        binding_source=args.binding_source,
        launch_profile_path=args.launch_profile,
        candidate_batch_size=args.candidate_batch_size,
        rolling_plan=False,
    )


if __name__ == "__main__":
    print(json.dumps(prepare(parse_args()), sort_keys=True))
