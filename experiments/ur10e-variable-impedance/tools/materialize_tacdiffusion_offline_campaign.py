"""Materialize and verify the tracked TacDiffusion offline fixture bundle."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys


EXPERIMENT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(EXPERIMENT_ROOT))

from ur10e_vic.tacdiffusion.offline_campaign import (  # noqa: E402
    materialize_offline_campaign_bundle,
    validate_offline_campaign_bundle,
)


DEFAULT_OUTPUT = EXPERIMENT_ROOT / "evidence" / "tacdiffusion_offline_fixture_campaign_v1"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="materialize-tacdiffusion-offline-campaign")
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT))
    parser.add_argument("--validate-only", action="store_true")
    args = parser.parse_args(argv)
    if args.validate_only:
        manifest = validate_offline_campaign_bundle(args.output)
        result = {
            "validation_status": "verified",
            "artifact_root": str(Path(args.output)),
            "artifact_root_digest": manifest["bundle_digest_sha256"],
        }
    else:
        materialized = materialize_offline_campaign_bundle(args.output)
        result = {
            "validation_status": "materialized_and_verified",
            "artifact_root": str(materialized.root),
            "artifact_root_digest": materialized.artifact_root_digest,
            "campaign_counters": materialized.campaign_receipt["counters"],
            "dataset_shape": {
                "observations": materialized.dataset_manifest["observation_shape"],
                "actions": materialized.dataset_manifest["action_shape"],
            },
            "split_episode_counts": materialized.dataset_manifest["split_episode_counts"],
            "fixture_only": True,
            "production_promotion_allowed": False,
        }
    print(json.dumps(result, indent=2, sort_keys=True, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
