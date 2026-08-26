#!/usr/bin/env python3
"""Run the independent, fixed-candidate V4 timing characterization lane.

Each supplied run directory must be a fresh manual-canary preparation.  The
R013 owner performs its fixed qualification lifecycle only; this entrypoint
never calls a V4 stage scheduler, opens BO, or tells an optimizer observation.
The lane is serial and holds the formal timing/throughput lease for its whole
duration.
"""

from __future__ import annotations

import argparse
from contextlib import nullcontext
import json
from pathlib import Path
import re
import time
from typing import Any, Mapping, Sequence

from step5d_autotune_v4_r013.campaign import Campaign
from step5d_autotune_v4_r013.live_owner import (
    R013PathProfileV1,
    build_r013_live_context,
)
from step5d_autotune_v4_r013.timing_characterization import (
    DEFAULT_REPEATS,
    _timing_from_mapping,
    build_receipt,
    classify_observations,
    load_run_observations,
)
from step5d_autotune_v4_r013.timing_scheduler import (
    LATE_CONTROL_FIFO_PROFILE,
    QUOTA_SAFE_OTHER_PROFILE,
    TimingSchedulerProfileV1,
)
from step5d_remote_startup import RemoteDashboardWriter
from step5d_autotune_v4_r012.register_transport import R012LiveRTDETransport
from ur10e_parallel import ResourceProfile, formal_timing_lease


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONTROLLER = "192.168.1.18"
DEFAULT_KUNWEI = "192.168.50.25"
DEFAULT_KUNWEI_PORT = 5152
SEED_I_GAIN = 0.008610779292198037
TIMING_DIAGNOSTICS_MARKER = "timing_diagnostics="
def _timing_process_receipt(
    observations: Sequence[Mapping[str, Any]],
    *,
    profile: TimingSchedulerProfileV1,
) -> dict[str, Any]:
    """Summarize per-attempt scheduler receipts for formal admission."""

    scheduler_rows = [
        row.get("timing_scheduler")
        for row in observations
        if isinstance(row.get("timing_scheduler"), Mapping)
    ]
    receipts = [dict(row) for row in scheduler_rows if isinstance(row, Mapping)]
    helper_counts = [
        int(row.get("helper_non_other_thread_count", 0))
        for row in receipts
        if isinstance(row.get("helper_non_other_thread_count", 0), int)
    ]
    gc_rows = [
        row.get("gc_window")
        for row in observations
        if isinstance(row.get("gc_window"), Mapping)
    ]
    return {
        "schema": "step5d.autotune-v4/r013-timing-process-receipt-v2",
        "version": 2,
        "scope": "per_attempt",
        "profile_id": profile.profile_id,
        "policy": "SCHED_FIFO" if profile.profile_id == LATE_CONTROL_FIFO_PROFILE else "SCHED_OTHER",
        "priority": profile.fifo_priority if profile.profile_id == LATE_CONTROL_FIFO_PROFILE else 0,
        "cpu_affinity": list(profile.control_cpu_affinity),
        "receipt_count": len(receipts),
        "helper_non_other_thread_count": max(helper_counts, default=-1),
        "restore_verified": bool(receipts) and all(row.get("restore_verified") is True for row in receipts),
        "kernel_rt_bandwidth_unchanged": bool(receipts)
        and all(row.get("kernel_rt_bandwidth_unchanged") is True for row in receipts),
        "early_process_promotion": False,
        "gc_window_receipt_count": len(gc_rows),
        "gc_window_entered": bool(gc_rows)
        and all(row.get("entered") is True for row in gc_rows),
        "gc_window_restored": bool(gc_rows)
        and all(
            row.get("restored") is True
            and row.get("restored_after_timing_lease") is True
            and row.get("pre_enabled") == row.get("post_enabled")
            for row in gc_rows
        ),
    }


def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", action="append", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--lane-id", default="V4_STAGE_A_TIMING_CHAR_10")
    parser.add_argument("--controller-host", default=DEFAULT_CONTROLLER)
    parser.add_argument("--kunwei-host", default=DEFAULT_KUNWEI)
    parser.add_argument("--kunwei-port", type=int, default=DEFAULT_KUNWEI_PORT)
    parser.add_argument("--target-observations", type=int, default=10)
    parser.add_argument(
        "--timing-scheduler-profile",
        choices=(LATE_CONTROL_FIFO_PROFILE, QUOTA_SAFE_OTHER_PROFILE),
        default=LATE_CONTROL_FIFO_PROFILE,
    )
    return parser.parse_args(argv)


def _qualification_candidate(run_dir: Path) -> dict[str, Any]:
    payload = json.loads((Path(run_dir) / "r012_seed_source.json").read_text(encoding="utf-8"))
    source = payload.get("confirmed_incumbent")
    if not isinstance(source, Mapping) or not isinstance(source.get("candidate"), Mapping):
        raise RuntimeError("timing characterization seed candidate is missing")
    candidate = dict(source["candidate"])
    candidate.update({"force_i_gain": SEED_I_GAIN, "i_off": False, "target_force_n": 5.0})
    return candidate


def _reset_resident(*, controller_host: str) -> None:
    """Restore neutral HOLD then resident READY_HOME_NEXT between lanes."""

    writer = RemoteDashboardWriter(
        controller_host,
        load_target="/programs/andyl/kunwei/step5/step5d/step5d_strict_rnn_autotune_v4_r013.urp",
    )
    try:
        writer.write("stop")
    except Exception:
        pass
    transport = R012LiveRTDETransport(controller_host)
    transport.open()
    try:
        doubles = [0.0] * 24
        doubles[-1] = 606.0
        transport.send_packet(doubles, [0] * 9)
    finally:
        transport.close()
    last_error: Exception | None = None
    for _ in range(4):
        try:
            writer.write("play")
            return
        except Exception as exc:
            last_error = exc
            time.sleep(0.5)
    raise RuntimeError(f"timing characterization could not restore resident READY: {last_error}")


def _timing_diagnostics(error: BaseException) -> Mapping[str, Any] | None:
    text = str(error)
    marker = text.rfind(TIMING_DIAGNOSTICS_MARKER)
    if marker < 0:
        return None
    raw = text[marker + len(TIMING_DIAGNOSTICS_MARKER) :].strip()
    try:
        value = json.loads(raw)
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    return value if isinstance(value, Mapping) else None


def _run_one(
    run_dir: Path,
    *,
    controller_host: str,
    kunwei_host: str,
    kunwei_port: int,
    timing_scheduler_profile: str,
) -> dict[str, Any]:
    run_dir = Path(run_dir).resolve()
    ledger = run_dir / "r013_ledger.jsonl"
    campaign = Campaign.resume(ledger)
    context: Any | None = None
    error: str | None = None
    timing_failure: Mapping[str, Any] | None = None
    before = len(load_run_observations(run_dir))
    try:
        context = build_r013_live_context(
            run_dir=run_dir,
            controller_host=controller_host,
            kunwei_host=kunwei_host,
            kunwei_port=kunwei_port,
            launch_profile=(ROOT / "config/step5/step5d_autotune_v4_r013_launch_profile.json"),
            campaign_id=str(campaign.ledger.header["campaign_id"]),
            run_id=str(campaign.ledger.header["run_id"]),
            attempt_id=str(campaign.ledger.header["attempt_id"]),
            runtime_strategy=campaign.runtime_strategy,
            campaign=campaign,
            path_profile=R013PathProfileV1.cycloid(),
            feedforward_mode="on",
            timing_scheduler_profile=timing_scheduler_profile,
        )
    except BaseException as exc:
        error = f"{type(exc).__name__}: {exc}"
        timing_failure = _timing_diagnostics(exc)
    finally:
        if context is not None:
            try:
                context.stop("timing_characterization_complete")
            finally:
                context.close()
        _reset_resident(controller_host=controller_host)
    observations = load_run_observations(run_dir)
    result: dict[str, Any] = {
        "run_dir": str(run_dir),
        "campaign_fingerprint_sha256": campaign.campaign_fingerprint.sha256,
        "source_identity": json.loads(
            (run_dir / "r013_live_preparation.json").read_text(encoding="utf-8")
        ).get("materialized_campaign_fingerprint", {}).get("source_identity"),
        "candidate": _qualification_candidate(run_dir),
        "observations_before": before,
        "observations_after": len(observations),
        "error": error,
        "timing_failure": dict(timing_failure) if timing_failure is not None else None,
        "observations": observations,
    }
    if timing_failure is not None and isinstance(timing_failure.get("timing_evidence"), Mapping):
        result["timing_failure_observation"] = {
            "schema": "step5d.autotune-v4/v4-stage-a-timing-characterization-observation-v1",
            "run_dir": str(run_dir),
            "attempt_sequence": len(observations) + 1,
            "candidate": result["candidate"],
            "timing": _timing_from_mapping(timing_failure["timing_evidence"]),
            "timing_decision": dict(timing_failure.get("r008_timing_decision", {})),
            "status": "timing_failure",
        }
    return result


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    if args.target_observations < 1:
        raise SystemExit("--target-observations must be positive")
    profile = ResourceProfile.from_env()
    run_results: list[dict[str, Any]] = []
    all_run_dirs: list[Path] = []
    all_observations: list[dict[str, Any]] = []
    timing_profile = TimingSchedulerProfileV1.from_id(args.timing_scheduler_profile)
    with formal_timing_lease(profile, task=args.lane_id, blocking=False):
        for run_dir in args.run_dir:
            result = _run_one(
                run_dir,
                controller_host=args.controller_host,
                kunwei_host=args.kunwei_host,
                kunwei_port=args.kunwei_port,
                timing_scheduler_profile=timing_profile.profile_id,
            )
            run_results.append(result)
            all_run_dirs.append(Path(run_dir))
            all_observations.extend(result["observations"])
            failure_observation = result.get("timing_failure_observation")
            if isinstance(failure_observation, Mapping):
                all_observations.append(dict(failure_observation))
            if len(all_observations) >= args.target_observations:
                break
    receipt = build_receipt(all_run_dirs, lane_id=args.lane_id)
    receipt["run_results"] = run_results
    receipt["timing_process"] = _timing_process_receipt(
        all_observations,
        profile=timing_profile,
    )
    receipt["observations"] = all_observations
    receipt["observation_count"] = len(all_observations)
    receipt["timing_observation_count"] = sum(
        isinstance(row.get("timing"), Mapping) for row in all_observations
    )
    receipt.update(
        classify_observations(
            all_observations,
            minimum_observations=max(3, min(DEFAULT_REPEATS, int(args.target_observations))),
        )
    )
    receipt["target_observations"] = int(args.target_observations)
    receipt["target_met"] = receipt["timing_observation_count"] >= args.target_observations
    receipt["lane_id"] = str(args.lane_id)
    output = Path(args.output).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(receipt, sort_keys=True, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    print(json.dumps({"output": str(output), "classification": receipt["classification"], "timing_observation_count": receipt["timing_observation_count"]}, sort_keys=True))
    return 0 if receipt["target_met"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
