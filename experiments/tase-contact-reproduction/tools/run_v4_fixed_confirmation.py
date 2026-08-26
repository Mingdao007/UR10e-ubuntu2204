#!/usr/bin/env python3
"""Run one fixed, non-optimizer V4 confirmation attempt.

This is used only after a V4 search closes but its winner confirmation is
interrupted by a terminal safety/runtime failure.  The candidate is explicit,
I-off, and never enters BO or ``tell_exact`` through this compatibility
manual-canary ledger.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Mapping

from step5d_autotune_v4_r013.campaign import Campaign, Dispatch, candidate_token
from step5d_autotune_v4_r013.live_owner import (
    R013PathProfileV1,
    build_r013_live_context,
)
from step5d_autotune_v4_r013.feedforward import FeedforwardMode


def _json_value(value: Any) -> Any:
    if hasattr(value, "as_dict"):
        return _json_value(value.as_dict())
    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_json_value(item) for item in value]
    return value


def _write(path: Path, value: Mapping[str, Any]) -> None:
    destination = Path(path).resolve()
    temporary = destination.with_suffix(destination.suffix + ".tmp")
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary.write_text(
        json.dumps(_json_value(value), sort_keys=True, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )
    temporary.replace(destination)


def _args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--candidate-json", type=Path, required=True)
    parser.add_argument("--launch-profile", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--controller-host", default="192.168.1.18")
    parser.add_argument("--kunwei-host", default="192.168.50.25")
    parser.add_argument("--kunwei-port", type=int, default=5152)
    return parser.parse_args()


def _candidate(path: Path) -> dict[str, Any]:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    value = raw.get("candidate") if isinstance(raw, Mapping) else None
    if not isinstance(value, Mapping):
        raise RuntimeError("fixed confirmation candidate artifact is missing candidate")
    candidate = dict(value)
    if candidate.get("i_off") is not True or float(candidate.get("force_i_gain", 1.0)) != 0.0:
        raise RuntimeError("fixed confirmation candidate must be Stage-A I-off")
    if float(candidate.get("target_force_n", float("nan"))) != 5.0:
        raise RuntimeError("fixed confirmation candidate target must remain 5 N")
    return candidate


def main() -> int:
    args = _args()
    run_dir = Path(args.run_dir).resolve()
    campaign = Campaign.resume(run_dir / "r013_ledger.jsonl")
    if campaign.snapshot.get("campaign_role", {}).get("role") != "manual_canary":
        raise RuntimeError("fixed confirmation requires a manual-canary compatibility root")
    if campaign.observations or campaign.rejected_admissions:
        raise RuntimeError("fixed confirmation root must not contain optimizer observations")
    candidate = _candidate(args.candidate_json)
    token = candidate_token(candidate)
    dispatch = Dispatch(
        dispatch_id=f"v4-fixed-confirmation-{token[:16]}",
        kind="CONFIRMATION",
        ordinal=1,
        candidate=candidate,
        candidate_token=token,
        abort_allowed=False,
        runtime_strategy_sha256=campaign.runtime_strategy_sha256,
        campaign_fingerprint=campaign.campaign_fingerprint.as_dict(),
    )
    receipt: dict[str, Any] = {
        "schema": "step5d.autotune-v4/v4-fixed-confirmation-receipt-v1",
        "run_dir": str(run_dir),
        "candidate": candidate,
        "candidate_token": token,
        "campaign_fingerprint_sha256": campaign.campaign_fingerprint.sha256,
        "formal_campaign_tell_exact": False,
        "state": "selection_bound",
    }
    context = None
    try:
        context = build_r013_live_context(
            run_dir=run_dir,
            controller_host=str(args.controller_host),
            kunwei_host=str(args.kunwei_host),
            kunwei_port=int(args.kunwei_port),
            launch_profile=Path(args.launch_profile).resolve(),
            campaign_id=str(campaign.ledger.header["campaign_id"]),
            run_id=str(campaign.ledger.header["run_id"]),
            attempt_id=str(campaign.ledger.header["attempt_id"]),
            runtime_strategy=campaign.runtime_strategy,
            campaign=campaign,
            path_profile=R013PathProfileV1.cycloid(),
            feedforward_mode=FeedforwardMode.ON,
        )
        context.runtime.home()
        result = context.run_trial(dispatch)
        admission = result.get("physical_admission")
        if not isinstance(admission, Mapping) or not bool(
            admission.get("physical_eligible", False)
        ):
            raise RuntimeError(
                "fixed confirmation physical admission failed: "
                + json.dumps(_json_value(admission or {}), sort_keys=True)
            )
        receipt.update(
            {
                "state": "safe_return_home",
                "home": bool(result.get("home", False)),
                "safe_return": bool(result.get("safe_return", False)),
                "result": result,
            }
        )
        if not receipt["home"] or not receipt["safe_return"]:
            raise RuntimeError("fixed confirmation did not finish at Home")
        return 0
    except Exception as exc:
        receipt.update({"state": "failed", "error": f"{type(exc).__name__}: {exc}"})
        raise
    finally:
        _write(Path(args.output), receipt)
        if context is not None:
            try:
                context.stop("fixed_confirmation_complete")
            finally:
                context.close()


if __name__ == "__main__":
    raise SystemExit(main())
