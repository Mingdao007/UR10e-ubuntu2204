#!/usr/bin/env python3
"""r008 host route: domain-repaired search over an unmodified r006 release.

r008 reuses r006's release contract, campaign fingerprint and controller
triplet.  It changes only host-side concerns: the domain design binding, the
phase plan (staircase + Sobol + BO), and the controller seams expressed in
``step5d_autotune_v4_r008``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any, Mapping, Sequence

from step5d_autotune_v4_r005.observations import ObservationLedger

from step5d_autotune_v4_r006.contracts import load_contract
from step5d_autotune_v4_r006.live_adapter import R006LiveAdapterError
from step5d_autotune_v4_r006.parent import load_frozen_r005_contract

import run_step5d_autotune_v4_r006 as r006_host
import run_step5d_autotune_v4_r007 as r007_host

from step5d_autotune_v4_r008.design_binding import load_domain_design
from step5d_autotune_v4_r008.bounded_sidecar_verify import r008_bounded_sidecar_scope
from step5d_autotune_v4_r008.fresh_verify import r008_fresh_verify_scope
from step5d_autotune_v4_r008.live_adapter import (
    R008LiveAdapter,
    R008ObservationLedger,
    R008ProductionOptimizer,
)
from step5d_autotune_v4_r008.optimizer_keepalive import r008_optimizer_keepalive_scope
from step5d_autotune_v4_r008.optimizer_timeout import r008_optimizer_timeout_scope
from step5d_autotune_v4_r008.queue import R008DurableQueueAdapter
from step5d_autotune_v4_r008.bounded_resume_ledger import R008BoundedResumeObservationLedger
from step5d_autotune_v4_r008.timing import r008_timing_scope


ROOT = Path(__file__).resolve().parents[1]
R008_CONTRACT_PATH = ROOT / "config/step5d/autotune_v4_r008.json"
R008_CONTRACT_VERSION = "r008-domain-repair-v1"
R008_DOMAIN_PATH = ROOT / "config/step5d/autotune_v4_r008_domain.json"


class R008HostError(R006LiveAdapterError):
    """The r008 activation scope is not admissible for this process."""


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def load_r008_activation(path: Path = R008_CONTRACT_PATH) -> Mapping[str, Any]:
    try:
        contract = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise R008HostError("r008 activation contract is unreadable") from exc
    if not isinstance(contract, dict):
        raise R008HostError("r008 activation contract must be an object")
    if contract.get("version") != R008_CONTRACT_VERSION:
        raise R008HostError("r008 activation contract version differs")
    if contract.get("r006_mutation") is not False:
        raise R008HostError("r008 activation contract claims an r006 mutation")
    activation = contract.get("activation")
    if not isinstance(activation, dict) or activation.get("live_activation") is not True:
        raise R008HostError("r008 live activation is not enabled in the contract")
    live_route = activation.get("live_route")
    if not isinstance(live_route, dict) or live_route.get("controller_package_rotation") is not False:
        raise R008HostError("r008 must keep controller_package_rotation false")
    parent_hashes = contract.get("parent_hashes")
    if not isinstance(parent_hashes, dict) or not parent_hashes:
        raise R008HostError("r008 activation contract declares no parent binding")
    for relative, expected in parent_hashes.items():
        actual = _sha256_file(ROOT / relative)
        if actual != expected:
            raise R008HostError(f"r008 parent hash mismatch: {relative}")
    domain_binding = contract.get("domain_binding")
    if not isinstance(domain_binding, dict):
        raise R008HostError("r008 domain_binding missing")
    domain_path = ROOT / str(domain_binding["path"])
    domain = load_domain_design(domain_path)
    if domain.get("artifact_sha256") != domain_binding.get("artifact_sha256"):
        raise R008HostError("r008 domain artifact_sha256 mismatch")
    # Fail-closed r007 parent seam as well: r008 builds on the repaired ledger.
    r007_host.load_r007_activation()
    return contract


def run_live(args: argparse.Namespace) -> int:
    activation: Mapping[str, Any] = {}
    try:
        activation = load_r008_activation()
        contract = load_contract()
        _published_parent, parent_contract = load_frozen_r005_contract()
        inputs = r006_host._build_inputs(args, parent_contract)
        inputs.validate(contract=contract, parent_contract=parent_contract)
        local_triplet = {"script": args.script, "txt": args.txt, "urp": args.urp}
        if any(
            r006_host._sha256(path) != inputs.expected_triplet[role]
            for role, path in local_triplet.items()
        ):
            raise R008HostError("r008 local triplet digest differs from owner admission")
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
            contract=contract,
            parent_contract=parent_contract,
            domain_path=R008_DOMAIN_PATH,
        )
        # Optimizer/sidecar construction must sit inside bounded_sidecar_scope:
        # R006ObjectiveSidecar.__init__ cold-reads every historical artifact
        # unless the r008 overlay is already patched (otherwise ~168×subprocess
        # stalls resume before authority activates).
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
                contract=contract,
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
                "timing_eligibility": "r008_tp_ratio_rescue_over_r007",
                "fresh_verify_timeout": "r008_600s_plus_sidecar_hot_path",
                "r008_activation_version": activation["version"],
                "application_mae_threshold_n": float(
                    activation["domain_binding"]["application_mae_threshold_n"]
                ),
                "stop_reason": adapter.last_stop_reason,
                "event_tail": list(adapter.last_events[-16:]),
            },
            sort_keys=True,
        )
    )
    return 0 if status == "complete" else 2


def build_parser() -> argparse.ArgumentParser:
    parser = r006_host.build_parser()
    parser.prog = "run_step5d_autotune_v4_r008"
    parser.description = __doc__
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    if args.mode != "live":
        return int(args.handler(args))
    return run_live(args)


if __name__ == "__main__":
    raise SystemExit(main())
