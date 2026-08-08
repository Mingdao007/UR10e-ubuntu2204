#!/usr/bin/env python3
"""Run or inspect the deterministic offline V4 r004 campaign ledger.

The live mode is an explicit receipt- and acknowledgement-gated boundary. It
assumes the owner separately delivered/loaded/played the resident TP route;
this entrypoint never performs Dashboard actions.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "tools") not in sys.path:
    sys.path.insert(0, str(ROOT / "tools"))

from step5d_autotune_v4_r004.campaign import (  # noqa: E402
    CampaignRunner,
    build_campaign_plan,
    promotion_decision,
)
from step5d_autotune_v4_r004.contracts import load_contract  # noqa: E402
from step5d_autotune_v4_r004.ledger import DurableCampaignLedger  # noqa: E402
from step5d_autotune_v4_r004.live_campaign import LiveCampaignRunner  # noqa: E402
from step5d_autotune_v4_r004.prerequisites import load_prerequisites  # noqa: E402
from step5d_autotune_v4_r004_live_writer import LIVE_ACK, LiveR004Writer  # noqa: E402


DEFAULT_LEDGER = ROOT / "artifacts/step5d_autotune_v4_r004_campaign.jsonl"


def _plan_json() -> list[dict[str, object]]:
    return [
        {
            "ordinal": attempt.ordinal,
            "phase": attempt.phase,
            "label": attempt.label,
            "kind": attempt.kind.name,
            "logical_attempt_id": attempt.logical_attempt_id,
            "candidate_uid": attempt.candidate.uid,
            "target_force_n": attempt.candidate.target_force_n,
        }
        for attempt in build_campaign_plan(load_contract())
    ]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", action="store_true", help="print the exact 16-slot plan")
    parser.add_argument("--run-offline", action="store_true", help="append deterministic offline rows")
    parser.add_argument("--run-live", action="store_true", help="run the explicit-ack r004 live campaign boundary")
    parser.add_argument("--live-ack")
    parser.add_argument("--controller-host")
    parser.add_argument("--kunwei-host")
    parser.add_argument("--kunwei-port", type=int)
    parser.add_argument("--controller-receipt", type=Path)
    parser.add_argument("--script1-receipt", type=Path)
    parser.add_argument("--runtime-evidence", type=Path)
    parser.add_argument("--authority-root", type=Path)
    parser.add_argument("--route-id")
    parser.add_argument("--attempt-id")
    parser.add_argument("--session-id")
    parser.add_argument("--input-baseline-ledger-sha256")
    parser.add_argument("--software-baseline-n", nargs=6, type=float)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--resume-new-epoch", type=int)
    parser.add_argument("--resume-script1-receipt-sha256")
    parser.add_argument("--stop-after-ordinal", type=int, default=16)
    parser.add_argument("--ledger", type=Path, default=DEFAULT_LEDGER)
    parser.add_argument("--supersedes-ledger", type=Path)
    parser.add_argument("--session-epoch", type=int)
    parser.add_argument("--controller-receipt-sha256", default="1" * 64)
    parser.add_argument("--script1-receipt-sha256", default="2" * 64)
    args = parser.parse_args(argv)
    if not args.plan and not args.run_offline and not args.run_live:
        parser.error("choose --plan, --run-offline, or --run-live")
    if args.plan:
        print(json.dumps({"offline": True, "campaign": _plan_json()}, indent=2, sort_keys=True))
    if args.run_offline:
        contract = load_contract()
        ledger = DurableCampaignLedger(args.ledger)
        runner = CampaignRunner(contract, ledger)
        rows = runner.run(
            session_epoch=args.session_epoch or 1,
            controller_receipt_sha256=args.controller_receipt_sha256,
            script1_receipt_sha256=args.script1_receipt_sha256,
            baseline_ledger_sha256=args.input_baseline_ledger_sha256 or "3" * 64,
        )
        anchor_uid = build_campaign_plan(contract)[0].candidate.uid
        decision = promotion_decision(rows, anchor_uid=anchor_uid)
        print(
            json.dumps(
                {
                    "offline": True,
                    "ledger": str(args.ledger),
                    "rows": len(rows),
                    "promotion": decision,
                    "delivery": {"controller": False, "kunwei": False, "promotion": False},
                },
                indent=2,
                sort_keys=True,
            )
        )
    if args.run_live:
        if args.live_ack != LIVE_ACK:
            parser.error(f"live mode requires --live-ack {LIVE_ACK}")
        required = {
            "--controller-host": args.controller_host,
            "--kunwei-host": args.kunwei_host,
            "--kunwei-port": args.kunwei_port,
            "--controller-receipt": args.controller_receipt,
            "--script1-receipt": args.script1_receipt,
            "--runtime-evidence": args.runtime_evidence,
            "--authority-root": args.authority_root,
            "--route-id": args.route_id,
            "--attempt-id": args.attempt_id,
            "--session-id": args.session_id,
            "--session-epoch": args.session_epoch,
            "--input-baseline-ledger-sha256": args.input_baseline_ledger_sha256,
            "--software-baseline-n": args.software_baseline_n,
        }
        missing = [name for name, value in required.items() if value is None]
        if missing:
            parser.error("live mode requires explicit " + ", ".join(missing))
        contract = load_contract()
        triplet = {
            suffix: hashlib.sha256(
                (ROOT / f"programs/step5/step5d/step5d_strict_rnn_autotune_v4_r004{suffix}").read_bytes()
            ).hexdigest()
            for suffix in (".script", ".txt", ".urp")
        }
        prerequisites = load_prerequisites(
            contract,
            controller_path=args.controller_receipt,
            script1_path=args.script1_receipt,
            runtime_path=args.runtime_evidence,
            expected_triplet={"script": triplet[".script"], "txt": triplet[".txt"], "urp": triplet[".urp"]},
            route_id=args.route_id,
            session_epoch=args.session_epoch,
            resident_session_id=args.session_id,
            input_baseline_ledger_sha256=args.input_baseline_ledger_sha256,
        )
        writer = LiveR004Writer(
            prerequisites,
            authority_root=args.authority_root,
            route_id=args.route_id,
            attempt_id=args.attempt_id,
            controller_host=args.controller_host,
            kunwei_host=args.kunwei_host,
            kunwei_port=args.kunwei_port,
            software_baseline_n=args.software_baseline_n,
        )
        try:
            writer.open(live_ack=args.live_ack)
            ledger = (
                DurableCampaignLedger(args.ledger)
                if args.ledger.exists()
                else DurableCampaignLedger.create_new(
                    args.ledger,
                    supersedes=args.supersedes_ledger,
                    reason=(
                        "supersede false-positive r004 ordinal-4 evidence after fixed-Home, "
                        "500 Hz layered timing, and physical XY proof restoration"
                        if args.supersedes_ledger is not None
                        else "new r004 campaign ledger"
                    ),
                )
            )
            runner = LiveCampaignRunner(contract, ledger, writer)
            if args.resume:
                if args.resume_new_epoch is None or args.resume_script1_receipt_sha256 is None:
                    parser.error("--resume requires --resume-new-epoch and --resume-script1-receipt-sha256")
                result = runner.resume(
                    new_epoch=args.resume_new_epoch,
                    new_script1_receipt_sha256=args.resume_script1_receipt_sha256,
                    stop_after_ordinal=args.stop_after_ordinal,
                )
            else:
                result = runner.run(stop_after_ordinal=args.stop_after_ordinal)
            print(
                json.dumps(
                    {
                        "live": True,
                        "rows": len(result.rows),
                        "qualification_only": result.qualification_only,
                        "qualification_complete": result.qualification_complete,
                        "full_campaign_blocked": result.full_campaign_blocked,
                        "promotion": result.promotion,
                        "resumed": result.resumed,
                        "dashboard_actions": False,
                        "delivery": {"upload": False, "load": False, "play": False},
                    },
                    indent=2,
                    sort_keys=True,
                )
            )
        finally:
            writer.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
