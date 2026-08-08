#!/usr/bin/env python3
"""Build the r008 B3 FAR/NEAR contact-search canary triplet (offline)."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Sequence

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from step5d_autotune_v4_r008.b3_identity import load_b3_contract  # noqa: E402
from step5d_autotune_v4_r008.contact_search_schedule import PROGRAM_B3  # noqa: E402
from step5d_autotune_v4_r008.tp_two_stage_search import build_b3_triplet  # noqa: E402


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=ROOT / "programs/step5/step5d",
        help="Directory for the B3 canary triplet (default: programs/step5/step5d)",
    )
    args = parser.parse_args(list(argv) if argv is not None else None)
    contract = load_b3_contract()
    if contract.campaign_fingerprint.startswith("1db4f9bf"):
        raise SystemExit("refusing to emit mainline fingerprint")
    paths = build_b3_triplet(
        args.output_dir.resolve(),
        contract_sha256=contract.sha256,
        campaign_fingerprint=contract.campaign_fingerprint,
        schedule=contract.schedule,
    )
    # Guard: mainline r006 files must remain untouched by this builder.
    mainline = args.output_dir.resolve() / "step5d_strict_rnn_autotune_v4_r006.script"
    canary = paths[".script"]
    if canary.name == mainline.name:
        raise SystemExit("builder attempted to overwrite mainline r006")
    summary = {
        "status": "b3_two_stage_triplet_built",
        "program": PROGRAM_B3,
        "campaign_fingerprint": contract.campaign_fingerprint,
        "contract_sha256": contract.sha256,
        "parent_mainline_fingerprint": contract.schedule.parent_campaign_fingerprint,
        "artifacts": {key: str(path) for key, path in paths.items()},
        "schedule": contract.schedule.as_dict(),
    }
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
