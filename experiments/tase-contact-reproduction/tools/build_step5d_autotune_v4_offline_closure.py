#!/usr/bin/env python3
"""Build immutable V4 source/bootstrap/replay closure without live endpoints."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path
from typing import Any

from step5d_autotune_v4 import bo, contracts, replay


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = ROOT / "config/step5d/autotune_v4_r003_offline_closure.json"
SOURCE_PATHS = (
    "config/step5d/autotune_v4_r003.json",
    "config/step5d/autotune_v4_live_writer_r003.json",
    "config/step5d/autotune_v4_r003_baseline_ledger_genesis.json",
    "config/step5d/autotune_v4_wire_v2.json",
    "config/step5d/new_eoat_calibration.json",
    "config/step5d/old_eoat_controller_readback_v1.json",
    "config/step5d/eoat_profile_old_v3.json",
    "config/step5d/eoat_profile_new_v4.json",
    "config/step5_stage_table.json",
    "STEP5D_FLOW.md",
    "tools/step5d_autotune_v4/__init__.py",
    "tools/step5d_autotune_v4/contracts.py",
    "tools/step5d_autotune_v4/entry.py",
    "tools/step5d_autotune_v4/baseline.py",
    "tools/step5d_autotune_v4/baseline_ledger.py",
    "tools/step5d_autotune_v4/policies.py",
    "tools/step5d_autotune_v4/runtime.py",
    "tools/step5d_autotune_v4/control.py",
    "tools/step5d_autotune_v4/adapter.py",
    "tools/step5d_autotune_v4/live_attempt.py",
    "tools/step5d_autotune_v4/path_controller.py",
    "tools/step5d_autotune_v4/calibrated_runtime.py",
    "tools/step5d_autotune_v4/bo.py",
    "tools/step5d_autotune_v4/eligibility.py",
    "tools/step5d_autotune_v4/replay.py",
    "tools/step5d_autotune_v4/wire.py",
    "tools/step5d_autotune_v4/tp.py",
    "tools/step5d_force_search_core.py",
    "tools/step5d_new_eoat.py",
    "tools/build_step5d_autotune_start_hover_r001.py",
    "tools/step5d_eoat_profiles.py",
    "tools/build_step5d_autotune_v4_r003.py",
    "tools/step5d_autotune_v4_live_writer.py",
    "tools/run_step5d_autotune_v4_campaign.py",
    "tools/build_step5d_autotune_v4_offline_closure.py",
    "tools/transition_step5d_lineage.py",
    "tests/test_step5d_autotune_v4.py",
    "tests/test_step5d_autotune_v4_architecture.py",
    "tests/test_step5d_autotune_v4_r003_campaign.py",
    "programs/step5/step5d/step5d_strict_rnn_autotune_v4_r003.script",
    "programs/step5/step5d/step5d_strict_rnn_autotune_v4_r003.txt",
    "programs/step5/step5d/step5d_strict_rnn_autotune_v4_r003.urp",
    "programs/step5/step5d/step5d_strict_rnn_autotune_v4_r003.controller-deploy-manifest.json",
)
V3_UNTOUCHED = {
    "tools/step5d_autotune_contract.py": "973adc8c73e0187f8b4a5ef8dc6520890530b436b0eddaad02f41444bf5cafd6",
    "config/step5d/current.json": "646edefaf6fbbd76b0cbfe86bc00b5d8ac26b787231f9ac345425256bbc78f5e",
    "programs/step5/step5d/step5d_strict_rnn_autotune_v3_r034.script": "6a6af44ebdb79553307c2acb45ba6a2914c26f6dd7b05f393a6c1eb2c2d11f1d",
    "programs/step5/step5d/step5d_strict_rnn_autotune_v3_r034.txt": "19b77977e08d759359e5c9f5b372a32e198eb6ac71200680d29bf7a17ba62785",
    "programs/step5/step5d/step5d_strict_rnn_autotune_v3_r034.urp": "10f482d8c8e305b1aea979ceefb2cd24ef1fbc79e5e22235dae8e4a2b9093f66",
}


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _closed_sha(relative_path: str) -> str:
    path = (ROOT / relative_path).resolve()
    if ROOT not in path.parents or path.is_symlink() or not path.is_file():
        raise RuntimeError(f"offline closure source is not a local regular file: {relative_path}")
    return _sha(path)


def _candidate(value: contracts.V4Candidate) -> dict[str, Any]:
    return {
        "physical": value.canonical_physical,
        "named7d": dict(zip(contracts.NAMED_DIMENSIONS, contracts.encode_named7d(value), strict=True)),
        "candidate_uid": value.candidate_uid,
    }


def build_closure() -> dict[str, Any]:
    contract = contracts.load_contract()
    source_closure = {path: _closed_sha(path) for path in SOURCE_PATHS}
    v3_actual = {path: _sha(ROOT / path) for path in V3_UNTOUCHED}
    if v3_actual != V3_UNTOUCHED:
        raise RuntimeError("V3 source/current/triplet bytes changed during V4 build")
    batch_a, batch_b = bo.initial_pd_batches()
    tau = bo.tau_qualification_batch(bo.anchor())
    i_path = bo.i_qualification_path(bo.anchor())
    synthetic = [
        replay.ReplayRow(
            monotonic_s=index * 0.01,
            filtered_normal_n=5.0 + 0.1 * math.sin(index * 0.01),
            raw_normal_n=5.0 + 0.15 * math.sin(index * 0.01),
            force_norm_n=5.1,
            torque_norm_nm=0.1,
            sensor_fresh=True,
        )
        for index in range(6001)
    ]
    replay_result = replay.replay(synthetic)
    if replay_result.complete_bins != 550 or not replay_result.eligible_shape:
        raise RuntimeError("V4 exact-550-bin offline replay closure failed")
    return {
        "schema": "step5d.autotune-v4/offline-closure-v3",
        "lineage": contracts.LINEAGE,
        "program": contracts.PROGRAM,
        "contract_sha256": contract.sha256,
        "campaign_fingerprint": contract.campaign_fingerprint,
        "eoat_sha256": contract.eoat_sha256,
        "v3_isolation": {
            "verified_unchanged": True,
            "sha256": v3_actual,
            "v3_observations_or_gp_imported": False,
            "v3_current_pointer_changed": False,
        },
        "source_closure": source_closure,
        "force_search_shared_core": {
            "core_path": "tools/step5d_force_search_core.py",
            "core_sha256": source_closure["tools/step5d_force_search_core.py"],
            "integrated_contact_acquisition": True,
            "standalone_canary_prerequisite": False,
        },
        "bootstrap": {
            "batch_a": [_candidate(value) for value in batch_a],
            "batch_b": [_candidate(value) for value in batch_b],
            "tau": [_candidate(value) for value in tau],
            "i_path": [_candidate(value) for value in i_path],
            "stage_order": ["pd", "tau", "i", "ko_kp"],
        },
        "replay": {
            "required_bins": replay_result.required_bins,
            "complete_bins": replay_result.complete_bins,
            "mae_n": replay_result.mae_n,
            "bias_n": replay_result.bias_n,
            "std_n": replay_result.std_n,
            "p99_normal_n": replay_result.p99_normal_n,
            "max_force_norm_n": replay_result.max_force_norm_n,
            "max_torque_norm_nm": replay_result.max_torque_norm_nm,
            "timing": replay_result.timing,
            "eligible_shape": replay_result.eligible_shape,
            "synthetic_evidence_only": True,
        },
        "live_status": "BLOCKED_PENDING_CONTROLLER_UPLOAD_READBACK_THREE_5N_BASELINES_FORMAL_REVIEW_AND_CANONICAL_TRANSITION",
        "safety_boundary": [
            "offline deterministic construction only",
            "no controller, Dashboard, RTDE, Kunwei, Load, Play, motion, contact, zero, tare, or pointer mutation",
        ],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args(argv)
    payload = build_closure()
    encoded = (
        json.dumps(payload, indent=2, sort_keys=True, allow_nan=False) + "\n"
    ).encode("utf-8")
    if args.output.exists() or args.output.is_symlink():
        if args.output.is_symlink() or not args.output.is_file() or args.output.read_bytes() != encoded:
            raise RuntimeError("immutable V4 offline closure differs")
    else:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_bytes(encoded)
    print(
        json.dumps(
            {
                "output": str(args.output),
                "sha256": _sha(args.output),
                "campaign_fingerprint": payload["campaign_fingerprint"],
                "complete_bins": payload["replay"]["complete_bins"],
                "v3_verified_unchanged": payload["v3_isolation"]["verified_unchanged"],
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
