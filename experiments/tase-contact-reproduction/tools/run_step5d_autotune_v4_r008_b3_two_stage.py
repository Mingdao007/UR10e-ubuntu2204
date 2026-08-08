#!/usr/bin/env python3
"""r008 B3 host route: FAR/NEAR contact-search canary over a new fingerprint.

Does not call the mainline r008 activation gate that forbids controller package
rotation. Never resumes into live_20260803_1113_stage_d / 1db4f9bf.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Sequence

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from step5d_autotune_v4_r005.observations import ObservationLedger  # noqa: E402
from step5d_autotune_v4_r006.live_adapter import R006LiveAdapterError  # noqa: E402
from step5d_autotune_v4_r006.parent import load_frozen_r005_contract  # noqa: E402
import run_step5d_autotune_v4_r006 as r006_host  # noqa: E402

from step5d_autotune_v4_r009.quarantine import reject_r008_formal_resume  # noqa: E402
from step5d_autotune_v4_r008.b3_identity import load_b3_contract  # noqa: E402
from step5d_autotune_v4_r008.bounded_resume_ledger import (  # noqa: E402
    R008BoundedResumeObservationLedger,
)
from step5d_autotune_v4_r008.bounded_sidecar_verify import r008_bounded_sidecar_scope  # noqa: E402
from step5d_autotune_v4_r008.contact_search_schedule import MAINLINE_FINGERPRINT  # noqa: E402
from step5d_autotune_v4_r008.fresh_verify import r008_fresh_verify_scope  # noqa: E402
from step5d_autotune_v4_r008.live_adapter import (  # noqa: E402
    R008LiveAdapter,
    R008ObservationLedger,
    R008ProductionOptimizer,
)
from step5d_autotune_v4_r008.optimizer_keepalive import r008_optimizer_keepalive_scope  # noqa: E402
from step5d_autotune_v4_r008.optimizer_timeout import r008_optimizer_timeout_scope  # noqa: E402
from step5d_autotune_v4_r008.queue import R008DurableQueueAdapter  # noqa: E402
from step5d_autotune_v4_r008.timing import r008_timing_scope  # noqa: E402

R008_DOMAIN_PATH = ROOT / "config/step5d/autotune_v4_r008_domain.json"


class R008B3HostError(R006LiveAdapterError):
    """B3 canary host refused an unsafe activation."""


def run_live(args: argparse.Namespace) -> int:
    reject_r008_formal_resume("run_step5d_autotune_v4_r008_b3_two_stage.live")
    try:
        contract = load_b3_contract()
        if contract.campaign_fingerprint == MAINLINE_FINGERPRINT:
            raise R008B3HostError("B3 host refuses mainline fingerprint")
        if args.campaign_fingerprint != contract.campaign_fingerprint:
            raise R008B3HostError("launch campaign fingerprint differs from B3 contract")
        if args.contract_sha256 != contract.sha256:
            raise R008B3HostError("launch contract digest differs from B3 contract")
        if "1113_stage_d" in str(args.ledger) or "1113_stage_d" in str(args.queue_root):
            raise R008B3HostError("B3 must not resume into 1113_stage_d")

        _published_parent, parent_contract = load_frozen_r005_contract()
        inputs = r006_host._build_inputs(args, parent_contract)
        inputs.validate(contract=contract, parent_contract=parent_contract)  # type: ignore[arg-type]
        local_triplet = {"script": args.script, "txt": args.txt, "urp": args.urp}
        if any(
            r006_host._sha256(path) != inputs.expected_triplet[role]
            for role, path in local_triplet.items()
        ):
            raise R008B3HostError("B3 local triplet digest differs from owner admission")
        for path in local_triplet.values():
            if Path(path).name.startswith("step5d_strict_rnn_autotune_v4_r006."):
                raise R008B3HostError("B3 host refuses mainline r006 triplet paths")

        queue = R008DurableQueueAdapter(
            args.queue_root,
            campaign_id=contract.campaign_fingerprint,
            launch_profile_path=args.launch_profile,
            release_manifest_sha256=args.release_manifest_sha256,
        )
        ledger: ObservationLedger = R008BoundedResumeObservationLedger(
            args.ledger,
            campaign_fingerprint=contract.campaign_fingerprint,
            eoat_sha256=args.eoat_sha256,
        )
        adapter = R008LiveAdapter(
            contract=contract,  # type: ignore[arg-type]
            parent_contract=parent_contract,
            domain_path=R008_DOMAIN_PATH,
            timing_canary=bool(getattr(args, "timing_canary", False)),
        )
        with (
            r008_timing_scope(),
            r008_fresh_verify_scope(),
            r008_bounded_sidecar_scope(),
            r008_optimizer_timeout_scope(),
            r008_optimizer_keepalive_scope(),
        ):
            r006_ledger = R008ObservationLedger(
                ledger,
                campaign_fingerprint=contract.campaign_fingerprint,
            )
            optimizer = R008ProductionOptimizer(
                contract=contract,  # type: ignore[arg-type]
                ledger=r006_ledger,
                seed=6008,
                domain_path=R008_DOMAIN_PATH,
            )
            status = adapter.run_forever(
                inputs=inputs,
                queue=queue,
                ledger=ledger,
                optimizer=optimizer,
            )
            stop_reason = getattr(adapter, "last_stop_reason", None)
            events = list(getattr(adapter, "last_events", ()) or ())[-20:]
    except (R006LiveAdapterError, OSError, ValueError, RuntimeError) as exc:
        print(
            json.dumps(
                {"status": "incomplete_stopped", "reason": str(exc), "b3_two_stage": True},
                sort_keys=True,
            )
        )
        return 2
    print(
        json.dumps(
            {
                "status": status,
                "formal_live_complete": status == "complete",
                "b3_two_stage": True,
                "campaign_fingerprint": args.campaign_fingerprint,
                "stop_reason": stop_reason,
                "events_tail": events,
            },
            sort_keys=True,
        )
    )
    return 0 if status == "complete" else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    live = sub.add_parser("live", help="Run B3 two-stage canary host")
    # Reuse the same flag surface as r006/r008 live.
    for name, kwargs in [
        ("--controller-host", {"required": True}),
        ("--kunwei-host", {"required": True}),
        ("--kunwei-port", {"required": True, "type": int}),
        ("--controller-receipt", {"required": True, "type": Path}),
        ("--script1-receipt", {"required": True, "type": Path}),
        ("--runtime-evidence", {"required": True, "type": Path}),
        ("--software-baseline-receipt", {"required": True, "type": Path}),
        ("--ledger", {"required": True, "type": Path}),
        ("--queue-root", {"required": True, "type": Path}),
        ("--authority-root", {"required": True, "type": Path}),
        ("--thresholds-receipt", {"required": True, "type": Path}),
        ("--launch-profile", {"required": True, "type": Path}),
        ("--script", {"required": True, "type": Path}),
        ("--txt", {"required": True, "type": Path}),
        ("--urp", {"required": True, "type": Path}),
        ("--route-id", {"required": True}),
        ("--attempt-id", {"required": True}),
        ("--resident-session-id", {"required": True}),
        ("--session-epoch", {"required": True, "type": int}),
        ("--expected-script-sha256", {"required": True}),
        ("--expected-txt-sha256", {"required": True}),
        ("--expected-urp-sha256", {"required": True}),
        ("--release-manifest-sha256", {"required": True}),
        ("--eoat-sha256", {"required": True}),
        ("--campaign-fingerprint", {"required": True}),
        ("--contract-sha256", {"required": True}),
    ]:
        live.add_argument(name, **kwargs)
    live.add_argument(
        "--timing-canary",
        action="store_true",
        help="Skip QUAL (0→ANCHOR) for search/return timing canaries; formal autotune must omit this",
    )
    live.set_defaults(func=run_live)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
