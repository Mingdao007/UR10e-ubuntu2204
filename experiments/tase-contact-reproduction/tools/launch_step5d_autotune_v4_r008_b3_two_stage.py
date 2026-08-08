#!/usr/bin/env python3
"""Offline wiring helper for the r008 B3 FAR/NEAR canary.

Emits a launch recipe JSON binding the canary triplet, fingerprint, launch
profile, and prepare/host entrypoints. Does not touch the robot.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Sequence

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from step5d_autotune_v4_r008.b3_identity import load_b3_contract  # noqa: E402
from step5d_autotune_v4_r008.contact_search_schedule import (  # noqa: E402
    MAINLINE_FINGERPRINT,
    PROGRAM_B3,
)
from step5d_autotune_v4_r008.tp_two_stage_search import build_b3_triplet  # noqa: E402


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--run-dir",
        type=Path,
        required=True,
        help="Fresh empty run directory under runs/step5d_autotune_v4_r008/",
    )
    parser.add_argument(
        "--rebuild-triplet",
        action="store_true",
        help="Rebuild the B3 canary triplet before writing the recipe",
    )
    args = parser.parse_args(list(argv) if argv is not None else None)

    run_dir = args.run_dir.resolve()
    if "1113_stage_d" in str(run_dir):
        raise SystemExit("refusing mainline 1113_stage_d run directory")
    run_dir.mkdir(parents=True, exist_ok=True)

    contract = load_b3_contract()
    if contract.campaign_fingerprint == MAINLINE_FINGERPRINT:
        raise SystemExit("canary fingerprint collided with mainline")

    programs = ROOT / "programs/step5/step5d"
    if args.rebuild_triplet:
        build_b3_triplet(
            programs,
            contract_sha256=contract.sha256,
            campaign_fingerprint=contract.campaign_fingerprint,
            schedule=contract.schedule,
        )

    script = programs / f"{PROGRAM_B3}.script"
    txt = programs / f"{PROGRAM_B3}.txt"
    urp = programs / f"{PROGRAM_B3}.urp"
    for path in (script, txt, urp):
        if not path.is_file():
            raise SystemExit(f"missing B3 artifact: {path}")

    recipe = {
        "schema": "step5d.autotune-v4/r008-b3-two-stage-launch-recipe-v1",
        "program": PROGRAM_B3,
        "campaign_fingerprint": contract.campaign_fingerprint,
        "contract_sha256": contract.sha256,
        "parent_mainline_fingerprint": MAINLINE_FINGERPRINT,
        "run_dir": str(run_dir),
        "launch_profile": str(
            ROOT / "config/step5/step5d_autotune_v4_r008_b3_two_stage_launch_profile.json"
        ),
        "schedule": contract.schedule.as_dict(),
        "triplet": {
            "script": str(script),
            "txt": str(txt),
            "urp": str(urp),
            "script_sha256": _sha256(script),
            "txt_sha256": _sha256(txt),
            "urp_sha256": _sha256(urp),
        },
        "commands": {
            "upload": [
                "python3",
                "tools/upload_ur_tp_package.py",
                PROGRAM_B3,
                "--local-dir",
                "programs/step5/step5d",
            ],
            "prepare_module": "prepare_step5d_autotune_v4_r008_b3_two_stage",
            "host_module": "run_step5d_autotune_v4_r008_b3_two_stage",
        },
        "gates": {
            "stop_mainline_b1_first": True,
            "require_mainline_sealed_safe_return": True,
            "require_inflight_null": True,
            "never_resume_1113_stage_d": True,
        },
    }
    out = run_dir / "b3_launch_recipe.json"
    out.write_text(json.dumps(recipe, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({"status": "b3_launch_recipe_written", "path": str(out), **recipe}, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
