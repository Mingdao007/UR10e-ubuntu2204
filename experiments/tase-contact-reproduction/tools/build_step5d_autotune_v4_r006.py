#!/usr/bin/env python3
"""Build the local r006 TP triplet and numeric sanity manifest."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Iterable

from step5d_autotune_v4_r006.contracts import load_contract
from step5d_autotune_v4_r006.motion_profile import ACTIVE_MOTION_ENVELOPE_V2
from step5d_autotune_v4_r006.tp import build_triplet


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = ROOT / "programs/step5/step5d"


def build(output_dir: Path = DEFAULT_OUTPUT) -> dict[str, object]:
    contract = load_contract()
    paths = build_triplet(output_dir, contract=contract, envelope=ACTIVE_MOTION_ENVELOPE_V2)
    return {
        "program": contract.program,
        "output_dir": str(Path(output_dir)),
        "artifacts": {key: str(value) for key, value in paths.items()},
        "controller_upload": False,
        "controller_readback": False,
        "live_evidence": False,
    }


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args(list(argv) if argv is not None else None)
    print(json.dumps(build(args.output_dir), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
