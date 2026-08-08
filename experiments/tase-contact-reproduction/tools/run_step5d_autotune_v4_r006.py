#!/usr/bin/env python3
"""r006 continuous host route plus explicitly bounded offline diagnostics.

The ``live`` parser has no ordinal stop.  It performs owner admission and
then delegates physical lifecycle work to the r005 HostLoop/mature r004
writer stack; this process never performs Dashboard Load or Play.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Sequence

from step5d_autotune_v4_r005.contracts import R005Contract
from step5d_autotune_v4_r005.live_adapter import R005LiveInputs
from step5d_autotune_v4_r005.observations import ObservationLedger

from step5d_autotune_v4_r006.contracts import load_contract
from step5d_autotune_v4_r006.live_adapter import (
    R006LiveAdapter,
    R006LiveAdapterError,
    R006LiveInputs,
    R006ObservationLedger,
    R006ProductionOptimizer,
)
from step5d_autotune_v4_r006.runtime import R006RuntimeError, run_fake_campaign
from step5d_autotune_v4_r006.parent import load_frozen_r005_contract
from step5d_autotune_v4_r006.queue import R006V3DurableQueueAdapter


def _sha256(path: Path) -> str:
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"triplet path is not a regular file: {path}")
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _build_inputs(args: argparse.Namespace, parent_contract: R005Contract) -> R006LiveInputs:
    del parent_contract
    parent = R005LiveInputs(
        controller_receipt=args.controller_receipt,
        script1_receipt=args.script1_receipt,
        runtime_evidence=args.runtime_evidence,
        # These three r005-only fields are deliberately not admission gates in
        # r006.  The compatibility dataclass still requires values; bind them
        # to already admitted r006 artifacts rather than inventing stale V3
        # dependencies.
        runtime_attestation=args.runtime_evidence,
        optimizer_pointer=args.runtime_evidence,
        launch_profile_path=args.launch_profile,
        release_manifest_sha256=args.release_manifest_sha256,
        baseline_ledger=args.software_baseline_receipt,
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
        expected_triplet={
            "script": args.expected_script_sha256,
            "txt": args.expected_txt_sha256,
            "urp": args.expected_urp_sha256,
        },
        eoat_sha256=args.eoat_sha256,
        campaign_fingerprint=args.campaign_fingerprint,
        contract_sha256=args.contract_sha256,
    )
    return R006LiveInputs(
        parent=parent,
        thresholds_receipt=args.thresholds_receipt,
        route_id=args.route_id,
        attempt_id=args.attempt_id,
        contract_sha256=args.contract_sha256,
        campaign_fingerprint=args.campaign_fingerprint,
        expected_triplet={
            "script": args.expected_script_sha256,
            "txt": args.expected_txt_sha256,
            "urp": args.expected_urp_sha256,
        },
    )


def run_offline(args: argparse.Namespace) -> int:
    try:
        result = run_fake_campaign(
            args.root,
            diagnostic_limit=args.stop_after_ordinal,
        )
    except R006RuntimeError as exc:
        print(
            json.dumps(
                {
                    "status": "INCOMPLETE_STOPPED",
                    "reason": str(exc),
                    "offline_diagnostic_limit": args.stop_after_ordinal,
                    "live_evidence": False,
                },
                sort_keys=True,
            )
        )
        return 2
    print(
        json.dumps(
            {
                "status": result.status,
                "attempts": len(result.attempts),
                "qualifications": result.qualification_execution_ids,
                "first_warm_start_trials": len(result.first_warm_start),
                "second_warm_start_trials": len(result.second_warm_start),
                "hyperparameters_frozen": result.hyperparameters_frozen,
                "scheduler_q_history": result.scheduler_q_history,
                "timing_passed": result.timing.passes,
                "echo_backlog": result.echo_backlog,
                "pending_cancelled": result.pending_cancelled,
                "application_accepted": result.completion.application_accepted,
                "local_pac_accepted": result.completion.local_pac_accepted,
                "live_evidence": result.live_evidence,
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0 if result.status == "COMPLETE" else 2


def run_live(args: argparse.Namespace) -> int:
    contract = load_contract()
    _published_parent, parent_contract = load_frozen_r005_contract()
    inputs = _build_inputs(args, parent_contract)
    try:
        # Complete all read-only receipt and threshold gates before queue,
        # ledger, writer, or transport construction.  Missing thresholds hold
        # at Home and can never reach ARM.
        inputs.validate(contract=contract, parent_contract=parent_contract)
        local_triplet = {"script": args.script, "txt": args.txt, "urp": args.urp}
        if any(_sha256(path) != inputs.expected_triplet[role] for role, path in local_triplet.items()):
            raise R006LiveAdapterError("r006 local triplet digest differs from owner admission")
        queue = R006V3DurableQueueAdapter(
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
        r006_ledger = R006ObservationLedger(
            ledger,
            campaign_fingerprint=contract.campaign_fingerprint,
        )
        optimizer = R006ProductionOptimizer(
            contract=contract,
            ledger=r006_ledger,
            seed=6006,
        )
        adapter = R006LiveAdapter(contract=contract, parent_contract=parent_contract)
        status = adapter.run_forever(
            inputs=inputs,
            queue=queue,
            ledger=ledger,
            optimizer=optimizer,
        )
    except (R006LiveAdapterError, OSError, ValueError, RuntimeError) as exc:
        print(json.dumps({"status": "incomplete_stopped", "reason": str(exc)}, sort_keys=True))
        return 2
    print(
        json.dumps(
            {
                "status": status,
                "formal_live_complete": status == "complete",
                "live_evidence_owner": "r005_observation_ledger_plus_r006_raw_sidecar",
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
    offline = subparsers.add_parser("offline", help="FakeRTDE diagnostic fixture")
    offline.add_argument("--root", type=Path, required=True)
    offline.add_argument("--stop-after-ordinal", type=int, help="offline diagnostic limit only")
    # The fixture is intentionally bounded by its own run function in this
    # release; retaining the option in the offline namespace does not expose
    # it on the live parser.
    offline.set_defaults(handler=run_offline)

    live = subparsers.add_parser("live", help="owner-gated continuous Remote route")
    for flag in (
        "controller-host", "kunwei-host", "controller-receipt", "script1-receipt",
        "runtime-evidence", "software-baseline-receipt",
        "ledger", "queue-root", "authority-root", "thresholds-receipt", "launch-profile",
        "script", "txt", "urp", "route-id", "attempt-id", "resident-session-id",
    ):
        live.add_argument(f"--{flag}", type=Path if flag.endswith(("receipt", "evidence", "pointer", "ledger", "root", "profile", "script", "txt", "urp")) else str, required=True)
    live.add_argument("--kunwei-port", type=int, required=True)
    live.add_argument("--session-epoch", type=int, required=True)
    for flag in (
        "expected-script-sha256", "expected-txt-sha256", "expected-urp-sha256",
        "release-manifest-sha256", "eoat-sha256", "campaign-fingerprint", "contract-sha256",
    ):
        live.add_argument(f"--{flag}", required=True)
    live.add_argument("--optimizer-profile", default="optimizer")
    live.set_defaults(handler=run_live)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(list(argv) if argv is not None else None)
    return int(args.handler(args))


if __name__ == "__main__":
    raise SystemExit(main())
