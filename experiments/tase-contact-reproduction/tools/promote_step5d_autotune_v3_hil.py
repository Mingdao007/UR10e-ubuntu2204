#!/usr/bin/env python3
"""Promote one successful HIL HOLD result into separate V3 acceptance evidence."""

from __future__ import annotations

import argparse
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

import verify_step5d_autotune_v3_execution_readiness as readiness
from step5d_autotune_v3.state import ORCHESTRATION_RELATIVE_PATHS, atomic_json


ROOT = Path(__file__).resolve().parents[1]
EVIDENCE = ROOT / "evidence/step5d_autotune_v3/hil_acceptance.json"
PROMOTION = ROOT / "config/step5/step5d_autotune_v3_live_promotion.json"


class PromotionError(RuntimeError):
    pass


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _load(path: Path, role: str) -> Mapping[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise PromotionError(f"{role} must be a real regular file")
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, Mapping):
        raise PromotionError(f"{role} must be a JSON object")
    return payload


def promote(result_path: Path) -> Mapping[str, Any]:
    current = readiness.verify(ROOT)
    result_path = result_path.expanduser().absolute()
    result = _load(result_path, "HIL HOLD result")
    expected = {
        "schema": "step5d.autotune-v3/hil-full-bridge-hold-result-v1",
        "ok": True,
        "claim": "target_controller_full_bridge_hold_no_motion",
        "identity": current["identity"],
        "zero_events": [],
    }
    for key, value in expected.items():
        if result.get(key) != value:
            raise PromotionError(f"HIL HOLD result differs at {key}")
    stationary = result.get("stationary") or {}
    if float(stationary.get("ready_home_dwell_s", 0.0)) < 5.0:
        raise PromotionError("HIL HOLD lacks the required READY_HOME dwell")
    program_stop = result.get("program_stop") or {}
    if program_stop.get("ok") is not True:
        raise PromotionError("HIL HOLD cleanup does not prove the V3 program stopped")
    startup_baseline = result.get("startup_software_baseline") or {}
    if (
        startup_baseline.get("baseline_epoch") != 0
        or result.get("hardware_zero_or_tare_count") != 0
    ):
        raise PromotionError("HIL HOLD startup software-baseline evidence differs")
    evidence = {
        "schema": "step5d.autotune-v3/hil-acceptance-v1",
        "ok": True,
        "claim": expected["claim"],
        "accepted_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "identity": current["identity"],
        "source_result_sha256": _sha256(result_path),
        "controller_identity_sha256": result.get("controller_identity_sha256"),
        "launch_profile_fingerprint": result.get("launch_profile_fingerprint"),
        "trial_overlay_fingerprint": result.get("trial_overlay_fingerprint"),
        "stationary": stationary,
        "startup_software_baseline": startup_baseline,
        "hardware_zero_or_tare_count": 0,
        "zero_events": [],
        "program_stop": program_stop,
    }
    atomic_json(EVIDENCE, evidence)
    promotion = {
        "schema": "step5d.autotune-v3/live-promotion-v1",
        "candidate_stage_id": readiness.V3_STAGE_ID,
        "control_profile_id": readiness.V1_STAGE_ID,
        "current_selector": readiness.V1_STAGE_ID,
        "identity": current["identity"],
        "hil_acceptance": {
            "path": str(EVIDENCE.relative_to(ROOT)),
            "sha256": _sha256(EVIDENCE),
        },
        "same_process_startup_gate": True,
        "live_runtime_promoted": True,
    }
    atomic_json(PROMOTION, promotion)
    readiness.verify(ROOT, require_live=True)
    return promotion


def carry_forward(prior_path: Path) -> Mapping[str, Any]:
    """Carry target HIL evidence only across an exact nonphysical startup delta."""

    current = readiness.verify(ROOT)
    prior_path = prior_path.expanduser().absolute()
    prior = _load(prior_path, "prior HIL acceptance")
    if any(
        (
            prior.get("schema") != "step5d.autotune-v3/hil-acceptance-v1",
            prior.get("ok") is not True,
            prior.get("claim") != "target_controller_full_bridge_hold_no_motion",
        )
    ):
        raise PromotionError("prior HIL acceptance contract differs")
    prior_identity = prior.get("identity") or {}
    current_identity = current["identity"]
    for field in ("contract_sha256", "control_fingerprint"):
        if prior_identity.get(field) != current_identity.get(field):
            raise PromotionError(f"HIL physical identity changed at {field}")
    if prior_identity.get("orchestration_fingerprint") == current_identity.get(
        "orchestration_fingerprint"
    ):
        raise PromotionError("HIL carry-forward requires a real orchestration delta")
    accepted_at = datetime.fromisoformat(str(prior.get("accepted_at"))).timestamp()
    changed = sorted(
        relative
        for relative in ORCHESTRATION_RELATIVE_PATHS
        if (ROOT / relative).stat().st_mtime > accepted_at
    )
    if not set(changed).issubset(readiness.HIL_CARRYFORWARD_NONPHYSICAL_PATHS):
        raise PromotionError(
            "HIL carry-forward includes a physical/runtime source: "
            + ",".join(sorted(set(changed) - readiness.HIL_CARRYFORWARD_NONPHYSICAL_PATHS))
        )
    if not {"tools/run_step5d_autotune_campaign.py", "tools/run_step5d_autotune_v3_live.py"}.issubset(changed):
        raise PromotionError("HIL carry-forward does not match the startup-only incident delta")
    evidence = {
        **dict(prior),
        "accepted_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "identity": current_identity,
        "carryforward": {
            "schema": "step5d.autotune-v3/hil-nonphysical-carryforward-v1",
            "prior_acceptance_path": str(prior_path.relative_to(ROOT)),
            "prior_acceptance_sha256": _sha256(prior_path),
            "prior_identity": prior_identity,
            "changed_orchestration_paths": changed,
            "policy": "no_new_tp_play_only_nonphysical_startup_sources_changed",
        },
    }
    atomic_json(EVIDENCE, evidence)
    promotion = {
        "schema": "step5d.autotune-v3/live-promotion-v1",
        "candidate_stage_id": readiness.V3_STAGE_ID,
        "control_profile_id": readiness.V1_STAGE_ID,
        "current_selector": readiness.V1_STAGE_ID,
        "identity": current_identity,
        "hil_acceptance": {
            "path": str(EVIDENCE.relative_to(ROOT)),
            "sha256": _sha256(EVIDENCE),
        },
        "same_process_startup_gate": True,
        "live_runtime_promoted": True,
    }
    atomic_json(PROMOTION, promotion)
    readiness.verify(ROOT, require_live=True)
    return promotion


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--result", type=Path)
    source.add_argument("--carry-forward-existing", type=Path)
    args = parser.parse_args()
    try:
        report = (
            promote(args.result)
            if args.result is not None
            else carry_forward(args.carry_forward_existing)
        )
    except Exception as exc:
        print(json.dumps({"ok": False, "blocker": str(exc)}, indent=2, sort_keys=True))
        return 2
    print(json.dumps({"ok": True, "promotion": report}, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
