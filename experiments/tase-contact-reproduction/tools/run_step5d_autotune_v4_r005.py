#!/usr/bin/env python3
"""Entry point for the r005 host loop and its owner-gated live adapter.

The diagnostic stop exists only under the offline FakeRTDE mode.  The live
parser deliberately has no ordinal stop; its adapter composes the verified
r004 RTDE/Kunwei/controller stack and remains side-effect free until an owner
supplies the independent live receipts and invokes that boundary.
"""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from pathlib import Path

from step5d_autotune_v4_r005.contracts import load_contract
from step5d_autotune_v4_r005.observations import ObservationLedger
from step5d_autotune_v4_r005.optimizer import V4BoAdapter
from step5d_autotune_v4_r005.queue import OfflineDurableQueue, V3DurableQueueAdapter
from step5d_autotune_v4_r005.runtime import AttemptResult, HostLoop
from step5d_autotune_v4_r005.live_adapter import (
    R005LiveAdapter,
    R005LiveAdapterError,
    R005LiveInputs,
    build_verified_mature_r005_writer,
)
from step5d_force_objective import ForcePathSample


@dataclass(frozen=True)
class OfflineFakeSchedule:
    """Bounded, typed campaign schedule used only by the offline FakeRTDE CLI.

    The schedule controls gates and force values, but never supplies an
    objective.  PATH observations are always rebuilt by the production ledger
    seam from the raw samples below.  Keeping this fixture here, rather than
    adding a parser injection flag, leaves the live entrypoint free of test
    scheduling controls.
    """

    schema: str = "step5d.autotune-v4/r005-offline-complete-fixture-v1"
    safe_nontrainable_attempt_sequence: int = 14
    threshold_attempt_sequence: int = 15

    def __post_init__(self) -> None:
        if (
            isinstance(self.safe_nontrainable_attempt_sequence, bool)
            or not isinstance(self.safe_nontrainable_attempt_sequence, int)
            or self.safe_nontrainable_attempt_sequence <= 0
        ):
            raise ValueError("offline safe-nontrainable sequence must be positive")
        if (
            isinstance(self.threshold_attempt_sequence, bool)
            or not isinstance(self.threshold_attempt_sequence, int)
            or self.threshold_attempt_sequence != self.safe_nontrainable_attempt_sequence + 1
        ):
            raise ValueError("offline threshold must immediately follow safe-nontrainable attempt")

    def is_safe_nontrainable(self, attempt) -> bool:
        return bool(
            attempt.kind == "BO_TRIAL"
            and attempt.attempt_sequence == self.safe_nontrainable_attempt_sequence
        )

    def force_n(self, attempt) -> float | None:
        if attempt.kind == "QUALIFICATION":
            return None
        if attempt.kind == "RETEST" or attempt.attempt_sequence >= self.threshold_attempt_sequence:
            return 5.0
        return 5.25


_OFFLINE_FAKE_SCHEDULE = OfflineFakeSchedule()
_OFFLINE_LEGACY_SAMPLE_TIMES = tuple(0.05 + index * 0.1 for index in range(50))
_OFFLINE_FORMAL_SAMPLE_TIMES = tuple(5.05 + index * 0.1 for index in range(550))


def _offline_raw_path_samples(attempt, force_n: float) -> tuple[ForcePathSample, ...]:
    """Create complete [5,60) raw evidence plus the legacy shadow window."""

    samples: list[ForcePathSample] = []
    for index, path_time_s in enumerate(
        _OFFLINE_LEGACY_SAMPLE_TIMES + _OFFLINE_FORMAL_SAMPLE_TIMES
    ):
        # Each sample has a distinct monotonic source identity.  These arrays
        # stay in the attempt result only long enough for ObservationLedger to
        # persist and fresh-verify the immutable artifact; they never enter
        # bounded observation metrics.
        sequence = attempt.attempt_sequence * 1000 + index + 1
        samples.append(
            ForcePathSample(
                path_time_s=path_time_s,
                path_phase=25,
                filtered_normal_n=force_n,
                source_sequences={
                    "offline_packet": sequence,
                    "offline_rtde": sequence,
                },
                source_ages_s={
                    "offline_packet": 0.001,
                    "offline_rtde": 0.001,
                },
            )
        )
    return tuple(samples)


def _offline_ask_impl(_observations, choices, pending, _q, _seed):
    """Deterministic optimizer proposal for the offline FakeRTDE fixture."""

    if not choices:
        raise RuntimeError("offline fixture received no candidate choices")
    return choices[0], {
        "offline_fake_optimizer": _OFFLINE_FAKE_SCHEDULE.schema,
        "pending_count": len(pending),
    }


def _offline_result(attempt, timing) -> AttemptResult:
    force_n = _OFFLINE_FAKE_SCHEDULE.force_n(attempt)
    is_qualification = attempt.kind == "QUALIFICATION"
    safe_nontrainable = _OFFLINE_FAKE_SCHEDULE.is_safe_nontrainable(attempt)
    raw_samples = (
        ()
        if is_qualification or force_n is None
        else _offline_raw_path_samples(attempt, force_n)
    )

    return AttemptResult(
        epoch=attempt.epoch,
        attempt_sequence=attempt.attempt_sequence,
        kind=attempt.kind,
        candidate=attempt.candidate,
        safe_return=True,
        binding_ok=True,
        safety_gate=True,
        contact_gate=not safe_nontrainable,
        return_gate=True,
        motion_gate=not safe_nontrainable,
        timing_gate=timing.passes,
        identity_gate=True,
        qualification_passed=is_qualification,
        duration_s=60.0,
        # A caller scalar is deliberately never supplied.  The durable
        # observation ledger recomputes this from raw_path_samples in its
        # fresh subprocess verifier.
        force_objective=None,
        metrics={
            "offline_fake_rtde": True,
            "offline_schedule": _OFFLINE_FAKE_SCHEDULE.schema,
            "rates": dict(timing.rates),
        },
        raw_path_samples=raw_samples,
    )


def run_offline(args: argparse.Namespace) -> int:
    from step5d_autotune_v4_r005.fake_rtde import FakeAttemptRuntime

    contract = load_contract()
    root = Path(args.root)
    root.mkdir(parents=True, exist_ok=True)
    ledger = ObservationLedger(
        root / "r005-observations.jsonl",
        campaign_fingerprint=contract.campaign_fingerprint,
        eoat_sha256="0" * 64,
    )
    queue = OfflineDurableQueue(root / "r005-queue")
    runtime = FakeAttemptRuntime(result_factory=_offline_result)
    loop = HostLoop(
        contract=contract,
        queue=queue,
        ledger=ledger,
        optimizer=V4BoAdapter(
            ask_impl=_offline_ask_impl,
            ledger=ledger,
            runtime_manifest=contract.runtime_manifest,
        ),
        runtime=runtime,
    )
    status = loop.run_until_terminal(diagnostic_limit=args.stop_after_ordinal)
    print(
        json.dumps(
            {
                "status": status,
                "records": len(ledger.records),
                "attempts": runtime.rtde.tp.arm_history,
                "hardware_calls": runtime.rtde.hardware_calls,
                "live_evidence": False,
                "stop_reason": loop.stop_reason,
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0 if status == "complete" else 2


def run_live(args: argparse.Namespace) -> int:
    contract = load_contract()
    expected_triplet = {
        "script": args.expected_script_sha256,
        "txt": args.expected_txt_sha256,
        "urp": args.expected_urp_sha256,
    }
    inputs = R005LiveInputs(
        controller_receipt=args.controller_receipt,
        script1_receipt=args.script1_receipt,
        runtime_evidence=args.runtime_evidence,
        runtime_attestation=args.runtime_attestation,
        optimizer_pointer=args.optimizer_pointer,
        launch_profile_path=args.launch_profile,
        release_manifest_sha256=args.release_manifest_sha256,
        baseline_ledger=args.baseline_ledger,
        software_baseline_receipt=args.software_baseline_receipt,
        ledger_path=args.ledger,
        queue_root=args.queue_root,
        authority_root=args.authority_root,
        controller_host=args.controller_host,
        kunwei_host=args.kunwei_host,
        kunwei_port=args.kunwei_port,
        route_id=args.route_id,
        attempt_id=args.attempt_id,
        resident_session_id=args.resident_session_id,
        session_epoch=args.session_epoch,
        expected_triplet=expected_triplet,
        eoat_sha256=args.eoat_sha256,
        campaign_fingerprint=args.campaign_fingerprint,
        contract_sha256=args.contract_sha256,
    )
    try:
        # This is the complete read-only admission set.  No transport factory
        # is constructed until every item, including the exact r005 triplet,
        # has passed validation.
        inputs.validate(contract=contract)
        if not inputs.expected_triplet_matches(
            {"script": args.script, "txt": args.txt, "urp": args.urp}
        ):
            raise R005LiveAdapterError("r005 program triplet digest differs")
        queue = V3DurableQueueAdapter(
            args.queue_root,
            campaign_id=contract.campaign_fingerprint,
            launch_profile_path=args.launch_profile,
            release_manifest_sha256=args.release_manifest_sha256,
        )
        ledger = ObservationLedger(
            args.ledger,
            campaign_fingerprint=contract.campaign_fingerprint,
            eoat_sha256=args.eoat_sha256,
        )
        optimizer = V4BoAdapter(
            ledger=ledger,
            optimizer_profile=args.optimizer_profile,
            runtime_manifest=contract.runtime_manifest,
        )
        adapter = R005LiveAdapter(
            contract=contract,
            writer_factory=lambda admitted, sink: build_verified_mature_r005_writer(
                admitted, contract=contract, path_sample_sink=sink
            ),
        )
        # The owner-gated factory is now a real composition over the mature
        # r004 writer/RTDE/Kunwei stack.  It is still constructed only after
        # every read-only admission check; this CLI never performs Dashboard
        # Load/Play and never fabricates live receipts.
        status = adapter.run_forever(
            inputs=inputs,
            queue=queue,
            ledger=ledger,
            optimizer=optimizer,
        )
    except (R005LiveAdapterError, OSError, ValueError, RuntimeError) as exc:
        print(json.dumps({"status": "incomplete_stopped", "reason": str(exc)}, sort_keys=True))
        return 2
    # Do not repeat the offline descriptor's claim boundary after a real live
    # run.  The durable ledger remains the evidence owner; this summary only
    # reports whether that run reached the formal terminal condition.
    print(
        json.dumps(
            {
                "status": status,
                "formal_live_complete": status == "complete",
                "live_evidence_owner": "r005_observation_ledger",
                "stop_reason": adapter.last_stop_reason,
                "event_tail": list(adapter.last_events[-12:]),
            },
            sort_keys=True,
        )
    )
    return 0 if status == "complete" else 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="mode", required=True)
    offline = subparsers.add_parser("offline", help="FakeRTDE-only diagnostic mode")
    offline.add_argument("--root", type=Path, required=True)
    offline.add_argument(
        "--stop-after-ordinal",
        type=int,
        help="diagnostic stop, permitted only for offline FakeRTDE mode",
    )
    offline.set_defaults(handler=run_offline)
    live = subparsers.add_parser("live", help="owner-gated r005 long-lived host loop")
    live.add_argument("--controller-host", required=True)
    live.add_argument("--kunwei-host", required=True)
    live.add_argument("--kunwei-port", type=int, required=True)
    live.add_argument("--controller-receipt", type=Path, required=True)
    live.add_argument("--script1-receipt", type=Path, required=True)
    live.add_argument("--runtime-evidence", type=Path, required=True)
    live.add_argument("--runtime-attestation", type=Path, required=True)
    live.add_argument("--optimizer-pointer", type=Path, required=True)
    live.add_argument("--baseline-ledger", type=Path, required=True)
    live.add_argument("--software-baseline-receipt", type=Path, required=True)
    live.add_argument("--ledger", type=Path, required=True)
    live.add_argument("--queue-root", type=Path, required=True)
    live.add_argument("--authority-root", type=Path, required=True)
    live.add_argument("--launch-profile", type=Path, required=True)
    live.add_argument("--script", type=Path, required=True)
    live.add_argument("--txt", type=Path, required=True)
    live.add_argument("--urp", type=Path, required=True)
    live.add_argument("--expected-script-sha256", required=True)
    live.add_argument("--expected-txt-sha256", required=True)
    live.add_argument("--expected-urp-sha256", required=True)
    live.add_argument("--release-manifest-sha256", required=True)
    live.add_argument("--eoat-sha256", required=True)
    live.add_argument("--campaign-fingerprint", required=True)
    live.add_argument("--contract-sha256", required=True)
    live.add_argument("--route-id", required=True)
    live.add_argument("--attempt-id", required=True)
    live.add_argument("--resident-session-id", required=True)
    live.add_argument("--session-epoch", type=int, required=True)
    live.add_argument("--optimizer-profile", default="optimizer")
    live.set_defaults(handler=run_live)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.handler(args))


if __name__ == "__main__":
    raise SystemExit(main())
