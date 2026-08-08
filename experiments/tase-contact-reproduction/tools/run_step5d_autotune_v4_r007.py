#!/usr/bin/env python3
"""r007 host route: the r006 campaign with the two repaired seams.

r007 owns no campaign of its own.  It runs the frozen r006 release contract,
queue, writer, optimizer and TP triplet unchanged, and differs from the r006
host in exactly two additive places:

* the sealed observation rows are reconstructed with the r006 candidate
  decoder instead of the r005 bounded ``Candidate`` constructor, which is the
  seam that stopped the campaign after seq18 sealed a legitimate
  ``Ko = -1 quarter-octave`` candidate; and
* timing eligibility uses the V3 cadence predicate rather than r004's per-layer
  average-rate veto, which discarded an otherwise complete observation at
  459.7 Hz.

Both seams are refused unless ``config/step5d/autotune_v4_r007.json`` still
binds the exact r006 bytes they were reviewed against.  This process performs
no Dashboard Load or Play, and generates no new controller package: the
controller triplet is byte-identical to the one r006 already read back.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Mapping, Sequence

from step5d_autotune_v4_r005.observations import ObservationLedger

from step5d_autotune_v4_r006.contracts import load_contract
from step5d_autotune_v4_r006.live_adapter import (
    R006LiveAdapter,
    R006LiveAdapterError,
    R006ObservationLedger,
    R006ProductionOptimizer,
)
from step5d_autotune_v4_r006.parent import load_frozen_r005_contract
from step5d_autotune_v4_r006.queue import R006V3DurableQueueAdapter

import run_step5d_autotune_v4_r006 as r006_host

from step5d_autotune_v4_r007.native_ledger import R007NativeObservationLedger
from step5d_autotune_v4_r007.timing import r007_timing_scope


ROOT = Path(__file__).resolve().parents[1]
R007_CONTRACT_PATH = ROOT / "config/step5d/autotune_v4_r007.json"
R007_CONTRACT_VERSION = "r007-native-ledger-repair-v1"


class R007HostError(R006LiveAdapterError):
    """The r007 repair scope is not admissible for this process."""


def load_r007_activation(path: Path = R007_CONTRACT_PATH) -> Mapping[str, Any]:
    """Admit the r007 seams, or fail closed before any transport is opened.

    The contract names the r006 bytes this repair was reviewed against.  If any
    of them has moved, the review no longer covers what would run, so the host
    refuses rather than silently repairing a different r006.
    """

    try:
        contract = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise R007HostError("r007 activation contract is unreadable") from exc
    if not isinstance(contract, dict):
        raise R007HostError("r007 activation contract must be an object")
    if contract.get("version") != R007_CONTRACT_VERSION:
        raise R007HostError("r007 activation contract version differs")
    if contract.get("r006_mutation") is not False:
        raise R007HostError("r007 activation contract claims an r006 mutation")
    activation = contract.get("activation")
    if not isinstance(activation, dict) or activation.get("live_activation") is not True:
        raise R007HostError("r007 live activation is not enabled in the contract")
    parent_hashes = contract.get("parent_hashes")
    if not isinstance(parent_hashes, dict) or not parent_hashes:
        raise R007HostError("r007 activation contract declares no parent binding")
    for relative, expected in parent_hashes.items():
        if not isinstance(relative, str) or Path(relative).is_absolute():
            raise R007HostError("r007 parent binding path is invalid")
        if r006_host._sha256(ROOT / relative) != expected:
            raise R007HostError(f"r007 parent binding differs: {relative}")
    return contract


def run_live(args: argparse.Namespace) -> int:
    """The r006 live route, entered through the two repaired seams."""

    activation: Mapping[str, Any] = {}
    try:
        # Admission first: a stale review binding must stop the process before
        # a queue, ledger, writer or transport exists.
        activation = load_r007_activation()
        contract = load_contract()
        _published_parent, parent_contract = load_frozen_r005_contract()
        inputs = r006_host._build_inputs(args, parent_contract)
        inputs.validate(contract=contract, parent_contract=parent_contract)
        local_triplet = {"script": args.script, "txt": args.txt, "urp": args.urp}
        if any(
            r006_host._sha256(path) != inputs.expected_triplet[role]
            for role, path in local_triplet.items()
        ):
            raise R007HostError("r007 local triplet digest differs from owner admission")
        queue = R006V3DurableQueueAdapter(
            args.queue_root,
            campaign_id=contract.campaign_fingerprint,
            launch_profile_path=args.launch_profile,
            release_manifest_sha256=args.release_manifest_sha256,
        )
        # Only the sealed-row candidate constructor changes here; the hash
        # chain, raw-artifact binding and objective verification all stay with
        # the frozen r005 ledger this subclass inherits.
        ledger: ObservationLedger = R007NativeObservationLedger(
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
        with r007_timing_scope():
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
                "observation_ledger": "r007_native",
                "timing_eligibility": "r007_v3_cadence",
                "r007_activation_version": activation["version"],
                "stop_reason": adapter.last_stop_reason,
                "event_tail": list(adapter.last_events[-12:]),
            },
            sort_keys=True,
        )
    )
    return 0 if status == "complete" else 2


def main(argv: Sequence[str] | None = None) -> int:
    """Reuse the r006 CLI surface; only the live handler is r007's."""

    parser = r006_host.build_parser()
    parser.description = __doc__
    args = parser.parse_args(list(argv) if argv is not None else None)
    if args.mode != "live":
        return int(args.handler(args))
    return run_live(args)


if __name__ == "__main__":
    raise SystemExit(main())
