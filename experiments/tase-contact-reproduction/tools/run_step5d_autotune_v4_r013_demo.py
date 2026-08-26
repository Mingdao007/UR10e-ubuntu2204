#!/usr/bin/env python3
"""Run the fixed R013 V4 manual demo.

The demo has one deliberate choice and two deliberate actions:

    --feedforward on|off  -> select the comparable runtime limb
    home                  -> return to verified Home and stop there
    start                 -> Home, run the fixed best candidate, safe-return Home

This is a demo runner, not an optimizer and not a new qualification campaign.
Use a fresh, dedicated R013 prepared run directory; it refuses to append to a
campaign that already contains optimizer observations.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
import time
from typing import Any, Mapping

from step5d_autotune_v4_r013.campaign import Campaign, Dispatch, candidate_token
from step5d_autotune_v4_r013.campaign_config import (
    R013ManualCanaryPreparationProfileV1,
)
from step5d_autotune_v4_r013.demo_profile import (
    HISTORICAL_BEST_CANDIDATE_TOKEN,
    demo_candidate,
    demo_identity_sha256,
    demo_profile,
)
from step5d_autotune_v4_r013.feedforward import FeedforwardProfile
from step5d_autotune_v4_r013.live_owner import build_r013_live_context


DEFAULT_CONTROLLER = "192.168.1.18"
DEFAULT_KUNWEI = "192.168.50.25"
DEFAULT_KUNWEI_PORT = 5152


def _json_value(value: Any) -> Any:
    if hasattr(value, "as_dict"):
        return _json_value(value.as_dict())
    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_json_value(item) for item in value]
    return value


def _write_json(path: Path, value: Mapping[str, Any]) -> None:
    destination = Path(path).resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(destination.name + ".tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        json.dump(_json_value(value), stream, sort_keys=True, indent=2, allow_nan=False)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, destination)


def _read_object(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise SystemExit(f"invalid JSON artifact: {path}") from exc
    if not isinstance(value, dict):
        raise SystemExit(f"JSON artifact must be an object: {path}")
    return value


def _candidate_from_artifact(path: Path | None) -> dict[str, Any]:
    if path is None:
        return demo_candidate()
    value = _read_object(path)
    candidate = value.get("candidate")
    dispatch = value.get("dispatch")
    if not isinstance(candidate, Mapping) and isinstance(dispatch, Mapping):
        candidate = dispatch.get("candidate")
    if not isinstance(candidate, Mapping):
        raise SystemExit("candidate artifact has no candidate object")
    candidate_value = dict(candidate)
    token = candidate_token(candidate_value)
    if token != HISTORICAL_BEST_CANDIDATE_TOKEN:
        raise SystemExit(
            "demo is locked to the historical R013 best candidate; "
            f"observed token={token}"
        )
    return candidate_value


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("profile", "home", "start"))
    parser.add_argument("--feedforward", choices=("on", "off"), required=False)
    parser.add_argument("--run-dir", type=Path)
    parser.add_argument("--launch-profile", type=Path)
    parser.add_argument("--candidate-json", type=Path)
    parser.add_argument("--runtime-strategy-json", type=Path)
    parser.add_argument("--controller-host", default=DEFAULT_CONTROLLER)
    parser.add_argument("--kunwei-host", default=DEFAULT_KUNWEI)
    parser.add_argument("--kunwei-port", type=int, default=DEFAULT_KUNWEI_PORT)
    parser.add_argument("--output", type=Path)
    return parser.parse_args()


def _require_live_args(args: argparse.Namespace) -> tuple[Path, Path, FeedforwardProfile]:
    if args.feedforward is None:
        raise SystemExit("home/start requires an explicit --feedforward on|off")
    if args.run_dir is None:
        raise SystemExit("home/start requires --run-dir")
    if args.launch_profile is None:
        raise SystemExit("home/start requires --launch-profile")
    return (
        Path(args.run_dir).resolve(),
        Path(args.launch_profile).resolve(),
        FeedforwardProfile.from_value(args.feedforward),
    )


def _fixed_dispatch(campaign: Campaign, candidate: Mapping[str, Any], mode: str) -> Dispatch:
    token = candidate_token(candidate)
    ordinal = max((int(item.ordinal) for item in campaign.dispatches), default=0) + 1
    return Dispatch(
        dispatch_id=f"r013-demo-{mode}-{token[:12]}",
        kind="PHASE_STRATEGY_CANARY",
        ordinal=ordinal,
        candidate=dict(candidate),
        candidate_token=token,
        abort_allowed=False,
        runtime_strategy_sha256=campaign.runtime_strategy_sha256,
        campaign_fingerprint=campaign.campaign_fingerprint.as_dict(),
    )


def _require_dedicated_demo_campaign(
    campaign: Campaign,
    feedforward_profile: FeedforwardProfile,
) -> None:
    expected_role = R013ManualCanaryPreparationProfileV1(
        feedforward_profile
    ).as_dict()
    if campaign.snapshot.get("campaign_role") != expected_role:
        raise SystemExit(
            "demo requires an exact manual-canary preparation role and feedforward mode"
        )
    if campaign.in_flight is not None:
        raise SystemExit(
            "demo run directory has an in-flight campaign dispatch; use a fresh prepared run"
        )
    if campaign.observations or campaign.rejected_admissions:
        raise SystemExit(
            "demo refuses a campaign with optimizer observations; use a fresh dedicated run"
        )


def _bind_demo_identity(
    *,
    path: Path,
    receipt: Mapping[str, Any],
    action: str,
) -> dict[str, Any]:
    """Bind one mode to one prepared run directory, never mix A/B rows."""

    identity = {
        "schema": "step5d.autotune-v4/r013-manual-demo-identity-v1",
        "demo_identity_sha256": receipt["demo_identity_sha256"],
        "campaign_id": receipt["campaign_id"],
        "run_id": receipt["run_id"],
        "attempt_id": receipt["attempt_id"],
        "campaign_fingerprint_sha256": receipt["campaign_fingerprint_sha256"],
        "feedforward_mode": receipt["feedforward_mode"],
        "candidate_token": receipt["candidate_token"],
        "state": "selection_bound",
        "start_count": 0,
    }
    identity_path = Path(path)
    if identity_path.is_file():
        existing = _read_object(identity_path)
        if existing.get("demo_identity_sha256") != identity["demo_identity_sha256"]:
            raise SystemExit(
                "demo run directory is already bound to the other feedforward mode; "
                "prepare a separate run directory for the A/B limb"
            )
        if action == "start" and int(existing.get("start_count", 0)) >= 1:
            raise SystemExit(
                "demo run directory already completed one start; prepare a fresh run for another demo"
            )
        if existing.get("state") == "failed":
            raise SystemExit(
                "demo run directory contains a failed demo attempt; prepare a fresh run"
            )
        identity.update(existing)
    _write_json(identity_path, identity)
    return identity


def _base_receipt(
    *,
    action: str,
    run_dir: Path,
    profile: Mapping[str, Any],
    campaign: Campaign,
) -> dict[str, Any]:
    return {
        "schema": "step5d.autotune-v4/r013-manual-demo-receipt-v1",
        "created_at_unix_s": time.time(),
        "action": action,
        "run_dir": str(run_dir),
        "campaign_id": str(campaign.ledger.header["campaign_id"]),
        "run_id": str(campaign.ledger.header["run_id"]),
        "attempt_id": str(campaign.ledger.header["attempt_id"]),
        "campaign_fingerprint_sha256": campaign.campaign_fingerprint.sha256,
        "demo_identity_sha256": demo_identity_sha256(
            feedforward_mode=profile["feedforward"]["mode"],
            campaign_fingerprint_sha256=campaign.campaign_fingerprint.sha256,
        ),
        "demo_profile": dict(profile),
        "formal_campaign_tell_exact": False,
        "note": "Demo-only fixed candidate run; not an optimizer admission or promotion record.",
    }


def main() -> int:
    args = _parse_args()
    if args.action == "profile":
        mode = args.feedforward or "on"
        print(json.dumps(demo_profile(mode), sort_keys=True, indent=2))
        return 0

    run_dir, launch_profile, feedforward = _require_live_args(args)
    ledger_path = run_dir / "r013_ledger.jsonl"
    if not ledger_path.is_file():
        raise SystemExit(f"fresh R013 campaign ledger is missing: {ledger_path}")
    campaign = Campaign.resume(ledger_path)
    _require_dedicated_demo_campaign(campaign, feedforward)
    candidate = _candidate_from_artifact(args.candidate_json)
    profile = demo_profile(feedforward.mode)
    runtime_strategy_path = (
        Path(args.runtime_strategy_json).resolve()
        if args.runtime_strategy_json is not None
        else run_dir / "runtime_strategy.json"
    )
    runtime_strategy = _read_object(runtime_strategy_path)
    dispatch = _fixed_dispatch(campaign, candidate, feedforward.mode.value)
    output = (
        Path(args.output).resolve()
        if args.output is not None
        else run_dir / f"r013_demo_{feedforward.mode.value}_{args.action}.json"
    )
    receipt = _base_receipt(
        action=args.action,
        run_dir=run_dir,
        profile=profile,
        campaign=campaign,
    )
    receipt.update(
        {
            "feedforward_mode": feedforward.mode.value,
            "candidate_token": dispatch.candidate_token,
            "candidate": dict(candidate),
            "dispatch": dispatch.as_dict(),
            "state": "selection_bound",
        }
    )
    identity_path = run_dir / "r013_demo_identity.json"
    identity = _bind_demo_identity(path=identity_path, receipt=receipt, action=args.action)
    _write_json(output, receipt)

    context = None
    try:
        context = build_r013_live_context(
            run_dir=run_dir,
            controller_host=str(args.controller_host),
            kunwei_host=str(args.kunwei_host),
            kunwei_port=int(args.kunwei_port),
            launch_profile=launch_profile,
            campaign_id=str(campaign.ledger.header["campaign_id"]),
            run_id=str(campaign.ledger.header["run_id"]),
            attempt_id=str(campaign.ledger.header["attempt_id"]),
            runtime_strategy=runtime_strategy,
            campaign=campaign,
            feedforward_mode=feedforward.mode,
        )
        # This is an explicit first action even though run_trial repeats the
        # Home boundary before ARM.  It makes the demo operator flow visible.
        context.runtime.home()
        receipt.update(
            {
                "state": "home_verified",
                "home": True,
                "feedforward_profile": context.feedforward_profile.as_dict(),
            }
        )
        identity.update({"state": "home_verified"})
        _write_json(identity_path, identity)
        _write_json(output, receipt)
        if args.action == "home":
            return 0

        result = context.run_trial(dispatch)
        receipt.update(
            {
                "state": "safe_return_home",
                "home": bool(result.get("home", False)),
                "safe_return": bool(result.get("safe_return", False)),
                "result": _json_value(result),
            }
        )
        identity.update({"state": "safe_return_home", "start_count": 1})
        _write_json(identity_path, identity)
        _write_json(output, receipt)
        if not receipt["home"] or not receipt["safe_return"]:
            raise RuntimeError("R013 demo did not finish at verified safe-return Home")
        return 0
    except Exception as exc:
        receipt.update(
            {
                "state": "failed",
                "error": f"{type(exc).__name__}: {exc}",
            }
        )
        identity.update({"state": "failed"})
        _write_json(identity_path, identity)
        _write_json(output, receipt)
        raise
    finally:
        if context is not None:
            context.close()


if __name__ == "__main__":
    sys.exit(main())
