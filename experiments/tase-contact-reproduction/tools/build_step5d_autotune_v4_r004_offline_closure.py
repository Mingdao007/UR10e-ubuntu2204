#!/usr/bin/env python3
"""Create the V4-specific r004 offline source closure.

The closure is content-addressed and deliberately excludes the pointer that
references it, avoiding a circular digest.  It records frozen V3/r034 and
V4/r003 evidence so a local audit can prove isolation without contacting any
external system.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Iterable

from step5d_autotune_v4_r004.contracts import PROGRAM, load_contract


ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "config/step5d/autotune_v4_r004_offline_closure.json"

FROZEN_V3 = {
    "config/step5d/current.json": "646edefaf6fbbd76b0cbfe86bc00b5d8ac26b787231f9ac345425256bbc78f5e",
    "programs/step5/step5d/step5d_strict_rnn_autotune_v3_r034.script": "6a6af44ebdb79553307c2acb45ba6a2914c26f6dd7b05f393a6c1eb2c2d11f1d",
    "programs/step5/step5d/step5d_strict_rnn_autotune_v3_r034.txt": "19b77977e08d759359e5c9f5b372a32e198eb6ac71200680d29bf7a17ba62785",
    "programs/step5/step5d/step5d_strict_rnn_autotune_v3_r034.urp": "10f482d8c8e305b1aea979ceefb2cd24ef1fbc79e5e22235dae8e4a2b9093f66",
    "tools/step5d_autotune_contract.py": "973adc8c73e0187f8b4a5ef8dc6520890530b436b0eddaad02f41444bf5cafd6",
}

FROZEN_R003 = {
    "programs/step5/step5d/step5d_strict_rnn_autotune_v4_r003.script": "b0898df98966a1103f1dff67f0cf0542b57bddfcc26fe47ca04a50edc082fe1d",
    "programs/step5/step5d/step5d_strict_rnn_autotune_v4_r003.txt": "6840e2707b8241c08c3028d4f19569f3f507ac9771516708fe615755ab36f819",
    "programs/step5/step5d/step5d_strict_rnn_autotune_v4_r003.urp": "ddfcc945dff17d423a4423fb43a8e4e8fe6ff4782267a13a38029a692221133a",
    "config/step5d/autotune_v4_r003.json": "88b2236818457594dcbd7cb8989191745483a0b6d66c693d3e2794edfebb51b3",
    "config/step5d/autotune_v4_live_writer_r003.json": "7585c0b1eeec730d3ad3dcdcc24e3649bf48868f0f8e4488d88007a717beba62",
}


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _assert_frozen(bindings: dict[str, str]) -> None:
    for relative, expected in bindings.items():
        path = ROOT / relative
        if not path.is_file() or path.is_symlink() or sha256(path) != expected:
            raise RuntimeError(f"frozen evidence changed: {relative}")


def _source_paths() -> tuple[Path, ...]:
    paths = [
        ROOT / "STEP5D_FLOW.md",
        ROOT / "config/step5_stage_table.json",
        ROOT / "config/step5d/autotune_v4_r004.json",
        ROOT / "config/step5d/autotune_v4_live_writer_r004.json",
        ROOT / "config/step5d/autotune_v4_r004_baseline_ledger_genesis.json",
        ROOT / "config/step5d/eoat_profile_new_v4.json",
        ROOT / "tools/build_step5d_autotune_v4_r004.py",
        ROOT / "tools/build_step5d_autotune_v4_r004_offline_closure.py",
        ROOT / "tools/step5d_autotune_v4_r004_live_writer.py",
        ROOT / "tools/run_step5d_autotune_v4_r004_campaign.py",
        ROOT / "programs/step5/step5d/step5d_autotune_start_hover_r001.script",
        ROOT / "programs/step5/step5d/step5d_autotune_start_hover_r001.txt",
        ROOT / "programs/step5/step5d/step5d_autotune_start_hover_r001.urp",
    ]
    paths.extend(sorted((ROOT / "tools/step5d_autotune_v4_r004").glob("*.py")))
    paths.extend(
        sorted(
            ROOT.glob(
                "programs/step5/step5d/step5d_strict_rnn_autotune_v4_r004.*"
            )
        )
    )
    return tuple(dict.fromkeys(paths))


def build_closure() -> dict[str, object]:
    _assert_frozen(FROZEN_V3)
    _assert_frozen(FROZEN_R003)
    contract = load_contract()
    source_closure: dict[str, str] = {}
    for path in _source_paths():
        if not path.is_file() or path.is_symlink():
            raise RuntimeError(f"source closure path is not a regular file: {path}")
        source_closure[str(path.relative_to(ROOT))] = sha256(path)
    return {
        "schema": "step5d.autotune-v4/r004-offline-closure-v1",
        "lineage": "step5d_strict_rnn_autotune_v4",
        "program": PROGRAM,
        "release_contract_sha256": contract.sha256,
        "campaign_fingerprint": contract.campaign_fingerprint,
        "wire_layout": 606,
        "target_force_n": 5.0,
        "damping": 28.0,
        "campaign": {
            "qualification": 3,
            "batch_a": 5,
            "batch_b": 5,
            "retest": 3,
            "total_logical_attempts": 16,
            "standalone_search_or_canary": False,
        },
        "acceptance": {
            "path_coverage_bins": 550,
            "nominal_rate_hz": 500,
            "minimum_rate_hz_inclusive": 460,
            "distinct_rate_layers": [
                "writer_publish",
                "rtde_distinct",
                "kunwei_distinct",
                "tp_consumed_distinct",
            ],
            "feedback_age_p99_max_s": 0.010,
            "fresh_frame_gap_lt_s": 0.020,
            "runtime_stale_stop_s": 0.080,
            "retest_passes_min": 2,
            "retest_count": 3,
            "mae_max_n": 0.30,
            "candidate_median_objective_ratio_max": 0.95,
            "anchor_mutated_in_place": False,
        },
        "safety_boundary": [
            "offline deterministic construction only",
            "no controller network, Dashboard, RTDE hardware, Kunwei, SSH, or SFTP",
            "no upload, read-back, Load, Play, contact, motion, sensor writes, or promotion",
        ],
        "source_closure": source_closure,
        "frozen_v3_isolation": {
            "sha256": FROZEN_V3,
            "verified_unchanged": True,
            "current_pointer_changed": False,
            "observations_or_gp_imported": False,
        },
        "frozen_v4_r003_isolation": {
            "sha256": FROZEN_R003,
            "verified_unchanged": True,
            "bytes_reused_as_r004": False,
        },
        "pointer_policy": {
            "v3_current_pointer": "config/step5d/current.json",
            "v4_pointer": "config/step5d/lineage_selector_v4_r004.json",
            "v4_pointer_is_not_canonical_active_selector": True,
            "controller_delivery": False,
        },
    }


def write_closure(path: Path = OUTPUT, *, replace_existing: bool = False) -> dict[str, object]:
    if (path.exists() or path.is_symlink()) and not replace_existing:
        raise FileExistsError(f"r004 closure is immutable: {path}")
    if path.exists() or path.is_symlink():
        if path.is_symlink() or not path.is_file():
            raise FileExistsError(f"r004 closure replacement target is invalid: {path}")
        path.unlink()
    document = build_closure()
    path.write_text(
        json.dumps(document, indent=2, sort_keys=True, allow_nan=False) + "\n",
        encoding="utf-8",
    )
    document["closure_sha256"] = sha256(path)
    return document


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=OUTPUT)
    parser.add_argument("--replace-existing", action="store_true")
    args = parser.parse_args(argv)
    print(
        json.dumps(
            write_closure(args.output, replace_existing=args.replace_existing),
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
