"""Fixed R013 demo candidate and its comparison-only historical note."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from .campaign import candidate_token
from .feedforward import FeedforwardProfile


DEMO_PROFILE_SCHEMA = "step5d.autotune-v4/r013-demo-profile-v1"

# This is the candidate sealed at the historical R013 single-trial minimum.
# Keep it fixed for the manual demo; do not let the demo call the optimizer.
HISTORICAL_BEST_CANDIDATE: dict[str, Any] = {
    "force_damping": 188.36079701683204,
    "force_i_gain": 0.008610779292198037,
    "force_p_gain": 0.019027313840405524,
    "i_off": False,
    "motion_kp": 2.5226892457611436,
    "normal_filter_tau_s": 0.04375,
    "orientation_ko": 0.05,
    "target_force_n": 5.0,
}
HISTORICAL_BEST_CANDIDATE_TOKEN = (
    "fcf0c3255393f2ccc3474865568893556dc7ff3566d7bbca728b51b357aa8da2"
)
HISTORICAL_BEST_SEALED_MAE_N = 0.3162640181193824


def demo_candidate() -> dict[str, Any]:
    candidate = dict(HISTORICAL_BEST_CANDIDATE)
    observed = candidate_token(candidate)
    if observed != HISTORICAL_BEST_CANDIDATE_TOKEN:
        raise RuntimeError(
            "R013 fixed demo candidate token differs from the sealed incumbent"
        )
    return candidate


def demo_profile(feedforward_mode: Any = None) -> dict[str, Any]:
    profile = FeedforwardProfile.from_value(feedforward_mode)
    return {
        "schema": DEMO_PROFILE_SCHEMA,
        "profile_id": "r013_historical_best_manual_demo_v1",
        "candidate": demo_candidate(),
        "candidate_token": HISTORICAL_BEST_CANDIDATE_TOKEN,
        "historical_best_sealed_mae_n": HISTORICAL_BEST_SEALED_MAE_N,
        "historical_comparison_note": (
            "Operator recollection: feedforward ON reached about 0.31 N; "
            "feedforward OFF was about 0.49 N. This note is comparison-only, "
            "not a promotion or safety threshold."
        ),
        "feedforward": profile.as_dict(),
        "lifecycle": ["home", "start_movement", "safe_return_home"],
        "optimizer": "disabled_fixed_candidate",
    }


def demo_identity_sha256(
    *,
    feedforward_mode: Any = None,
    campaign_fingerprint_sha256: str,
) -> str:
    """Hash the demo limb without mutating the formal campaign fingerprint."""

    profile = FeedforwardProfile.from_value(feedforward_mode)
    payload = {
        "schema": DEMO_PROFILE_SCHEMA,
        "feedforward_mode": profile.mode.value,
        "candidate_token": HISTORICAL_BEST_CANDIDATE_TOKEN,
        "campaign_fingerprint_sha256": str(campaign_fingerprint_sha256),
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False).encode(
            "utf-8"
        )
    ).hexdigest()


__all__ = [
    "DEMO_PROFILE_SCHEMA",
    "HISTORICAL_BEST_CANDIDATE",
    "HISTORICAL_BEST_CANDIDATE_TOKEN",
    "HISTORICAL_BEST_SEALED_MAE_N",
    "demo_candidate",
    "demo_identity_sha256",
    "demo_profile",
]
