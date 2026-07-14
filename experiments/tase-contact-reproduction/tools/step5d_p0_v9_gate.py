#!/usr/bin/env python3
"""Fail-closed authorization contract for the direct P0 v9 canary."""

from __future__ import annotations

import math
from pathlib import Path
from typing import Any, Mapping
import json


PROFILE = "step5d_strict_rnn_no_contact_p0_v9"
ROOT = Path(__file__).resolve().parents[1]


def configured_canary_duration() -> float:
    table = json.loads((ROOT / "config" / "step5_stage_table.json").read_text(encoding="utf-8"))
    row = next(item for item in table["stages"] if item.get("id") == PROFILE)
    return float(row["duration_s"])


def validate_canary_phase(profile: str, phase_s: float, *, allow_disabled: bool = True) -> float:
    phase = float(phase_s)
    if not math.isfinite(phase):
        raise ValueError("P0 v9 stop-register canary phase must be finite")
    if allow_disabled and phase == 0.0:
        return phase
    if profile != PROFILE:
        raise ValueError("--step5d-stop-register-canary-s is restricted to P0 v9")
    configured = configured_canary_duration()
    if not math.isclose(phase, configured, abs_tol=1e-9):
        raise ValueError(f"P0 v9 canary must match the frozen {configured:g}s duration")
    return phase


def authorize_canary(args: Any, current: Mapping[str, Any]) -> dict[str, Any]:
    candidate = current.get("p0_v9_candidate")
    capture = (current.get("bridge_trigger") or {}).get("no_contact_p0_v9_capture")
    if not isinstance(candidate, Mapping) or not isinstance(capture, Mapping):
        raise ValueError("P0 v9 remains local-only: canonical candidate/capture state is absent")
    if capture.get("profile") != PROFILE:
        raise ValueError("P0 v9 capture profile is not canonically bound")
    if capture.get("controller_readback_verified") is not True:
        raise ValueError("P0 v9 requires controller read-back before bridge start")
    if capture.get("capture_authorized") is not True:
        raise ValueError("P0 v9 requires explicit no-contact live-motion authorization")
    phase = validate_canary_phase(PROFILE, getattr(args, "step5d_stop_register_canary_s", 0.0), allow_disabled=False)
    return {"profile": PROFILE, "phase_s": phase, "package_sha256": capture.get("sha256")}
