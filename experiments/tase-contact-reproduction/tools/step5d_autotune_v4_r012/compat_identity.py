"""Bind the accepted B3 motion basis to the R012 controller basename."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import Any

from .behavior import R012_PROGRAM


ROOT = Path(__file__).resolve().parents[2]
FROZEN_B3_CONTRACT = Path(
    "/home/andy/.codex-worktrees/step5d-r010-offline-finetune-20260809/"
    "experiments/tase-contact-reproduction/config/step5d/"
    "autotune_v4_r008_b3_two_stage.json"
)
B3_IDENTITY = ROOT / "programs/step5/step5d/step5d_strict_rnn_autotune_v4_r008_b3_two_stage.deploy-manifest.json"


def r012_compat_contract() -> Any:
    """Reuse the accepted B3 contract; change only its program identity."""

    from step5d_autotune_v4_r006.contracts import load_contract
    from step5d_autotune_v4_r008.b3_identity import R008B3Contract
    from step5d_autotune_v4_r008.contact_search_schedule import load_schedule

    identity = json.loads(B3_IDENTITY.read_text(encoding="utf-8"))
    parent = load_contract(verify_source_closure=False)
    schedule = load_schedule()
    contract = R008B3Contract(
        path=FROZEN_B3_CONTRACT,
        raw=json.loads(FROZEN_B3_CONTRACT.read_text(encoding="utf-8")),
        sha256=str(identity["contract_sha256"]),
        campaign_fingerprint=str(identity["campaign_fingerprint"]),
        runtime_manifest=parent.runtime_manifest,
        schedule=schedule,
        parent_r006=parent,
    )
    raw = json.loads(json.dumps(dict(contract.raw)))
    raw["program"] = R012_PROGRAM
    raw["lineage_id"] = R012_PROGRAM
    return replace(
        contract,
        path=FROZEN_B3_CONTRACT,
        raw=raw,
        sha256=str(identity["contract_sha256"]),
        campaign_fingerprint=str(identity["campaign_fingerprint"]),
    )


__all__ = ["r012_compat_contract"]
