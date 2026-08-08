"""Smoke dry-run of the r008 phase plan without robot I/O."""

from __future__ import annotations

from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from step5d_autotune_v4_r008.design_binding import (  # noqa: E402
    anchor_from_document,
    box_from_document,
    load_domain_design,
)
from step5d_autotune_v4_r008.lattice import assert_r006_accepts, scrambled_sobol  # noqa: E402
from step5d_autotune_v4_r008.live_adapter import DEFAULT_PHASE_PLAN  # noqa: E402
from step5d_autotune_v4_r008.staircase import build_staircase  # noqa: E402


def main() -> int:
    domain = load_domain_design(ROOT / "config/step5d/autotune_v4_r008_domain.json")
    box = box_from_document(domain)
    anchor = anchor_from_document(domain)
    stair = build_staircase(anchor)
    space = scrambled_sobol(box, count=24, seed=8)
    assert_r006_accepts(tuple(level.point for level in stair) + space)
    plan = list(DEFAULT_PHASE_PLAN)
    total = sum(spec.count for spec in plan if spec.count > 0)
    print(
        {
            "ok": True,
            "phase_plan": [(spec.kind, spec.count) for spec in plan],
            "staircase": len(stair),
            "spacefill": len(space),
            "offline_attempt_budget": total,
            "mae_threshold_n": domain["application_mae_threshold_n"],
            "anchor_pd": anchor.pd_ratio,
            "predicted_anchor_mae_n": domain["scan_summary"].get("survivor_mae_at_anchor"),
            "predicted_survivor_mae_min": domain["scan_summary"].get("survivor_mae_min"),
        }
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
