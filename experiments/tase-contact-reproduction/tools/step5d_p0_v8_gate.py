#!/usr/bin/env python3
"""Dependency-light, fail-closed authorization contract for P0 v8 canaries."""

from __future__ import annotations

import math
import re
from typing import Any, Mapping


PROFILE = "step5d_strict_rnn_no_contact_p0_v8"
CANARY_PHASES_S = (2.0, 10.0, 60.0)


def validate_canary_phase(profile: str, phase_s: float, *, allow_disabled: bool = True) -> float:
    try:
        phase = float(phase_s)
    except (TypeError, ValueError) as exc:
        raise ValueError("P0 v8 stop-register canary phase must be finite") from exc
    if not math.isfinite(phase):
        raise ValueError("P0 v8 stop-register canary phase must be finite")
    if allow_disabled and phase == 0.0:
        return phase
    if profile != PROFILE:
        raise ValueError("--step5d-stop-register-canary-s is restricted to P0 v8")
    if not any(math.isclose(phase, allowed, abs_tol=1e-9) for allowed in CANARY_PHASES_S):
        raise ValueError("P0 v8 stop-register canary phase must be exactly 2, 10, or 60 seconds")
    return phase


def authorize_canary(args: Any, current: Mapping[str, Any]) -> dict[str, Any]:
    """Validate frozen review/readback state and sequential same-fingerprint phases."""

    candidate = current.get("p0_v8_candidate")
    capture = (current.get("bridge_trigger") or {}).get("no_contact_p0_v8_capture")
    if not isinstance(candidate, Mapping) or not isinstance(capture, Mapping):
        raise ValueError("P0 v8 raw bridge requires canonical p0_v8_candidate and capture state")
    if capture.get("profile") != PROFILE:
        raise ValueError("P0 v8 capture profile is not canonically bound")
    if capture.get("controller_readback_verified") is not True:
        raise ValueError("P0 v8 requires manifest-bound controller readback before bridge start")
    if capture.get("capture_authorized") is not True:
        raise ValueError("P0 v8 requires explicit no-contact live-motion authorization")
    review = candidate.get("review_v2")
    if not isinstance(review, Mapping) or review.get("status") != "accepted":
        raise ValueError("P0 v8 requires an accepted Review v2 1+1 gate")
    fingerprint = str(candidate.get("composite_fingerprint") or "")
    if re.fullmatch(r"[0-9a-f]{64}", fingerprint) is None:
        raise ValueError("P0 v8 composite fingerprint is missing or invalid")
    if review.get("composite_fingerprint") != fingerprint:
        raise ValueError("P0 v8 review fingerprint does not match current evidence")
    if candidate.get("evidence_frozen") is not True:
        raise ValueError("P0 v8 review may run only after evidence freeze")
    phase = validate_canary_phase(PROFILE, getattr(args, "step5d_stop_register_canary_s", 0.0), allow_disabled=False)
    completed = candidate.get("completed_canaries") or []
    required_previous = () if phase == 2.0 else (2.0,) if phase == 10.0 else (2.0, 10.0)
    for required_phase in required_previous:
        if not any(
            isinstance(item, Mapping)
            and math.isclose(float(item.get("phase_s", -1.0)), required_phase, abs_tol=1e-9)
            and item.get("composite_fingerprint") == fingerprint
            and item.get("canary_passed") is True
            for item in completed
        ):
            raise ValueError(
                f"P0 v8 {phase:g}s canary requires prior {required_phase:g}s pass on the same fingerprint"
            )
    return {
        "profile": PROFILE,
        "phase_s": phase,
        "composite_fingerprint": fingerprint,
        "package_sha256": capture.get("sha256"),
        "review_manifest": review.get("manifest"),
    }
