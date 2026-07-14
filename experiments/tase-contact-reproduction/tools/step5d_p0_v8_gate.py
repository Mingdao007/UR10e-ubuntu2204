#!/usr/bin/env python3
"""Dependency-light, fail-closed authorization contract for P0 v8 canaries."""

from __future__ import annotations

import math
import hashlib
import json
import re
from pathlib import Path
from typing import Any, Mapping

from ur10e_decision_manifest import p0_duration


PROFILE = "step5d_strict_rnn_no_contact_p0_v8"
ROOT = Path(__file__).resolve().parents[1]


def configured_canary_duration(current: Mapping[str, Any] | None = None) -> float:
    if current is None:
        current = json.loads((ROOT / "config/current_stage.json").read_text(encoding="utf-8"))
    table = json.loads((ROOT / "config/step5_stage_table.json").read_text(encoding="utf-8"))
    return p0_duration(dict(current), table)


def composite_fingerprint(candidate: Mapping[str, Any]) -> str:
    """Return the canonical fingerprint for the decision/package/canary binding."""

    binding = candidate.get("composite_binding")
    if not isinstance(binding, Mapping):
        raise ValueError("P0 v8 composite binding is missing")
    payload = {
        "decision_source_digest": binding.get("decision_source_digest"),
        "package_sha256": binding.get("package_sha256"),
        "semantic_fingerprint": binding.get("semantic_fingerprint"),
        "canary_policy": binding.get("canary_policy"),
    }
    if payload["package_sha256"] != candidate.get("package_sha256"):
        raise ValueError("P0 v8 composite package binding is stale")
    if payload["semantic_fingerprint"] != candidate.get("semantic_fingerprint"):
        raise ValueError("P0 v8 composite semantic binding is stale")
    if payload["canary_policy"] != candidate.get("canary_policy"):
        raise ValueError("P0 v8 composite canary-policy binding is stale")
    decision_digest = payload["decision_source_digest"]
    if re.fullmatch(r"[0-9a-f]{64}", str(decision_digest or "")) is None:
        raise ValueError("P0 v8 composite decision digest is invalid")
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def review_authorized(review: Mapping[str, Any], fingerprint: str) -> bool:
    """P0 no-contact is Review v3 0+0; deterministic gates remain mandatory."""

    return bool(
        review.get("policy_id") == "ur10e_review_policy_v3"
        and review.get("required_stack") == "0+0"
        and review.get("status") == "not_required"
        and review.get("composite_fingerprint") == fingerprint
        and (
            review.get("deterministic_canaries_still_required") is True
            or review.get("deterministic_gates_still_required") is True
        )
    )


def validate_canary_phase(profile: str, phase_s: float, *, allow_disabled: bool = True,
                          current: Mapping[str, Any] | None = None) -> float:
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
    configured = configured_canary_duration(current)
    if not math.isclose(phase, configured, abs_tol=1e-9):
        raise ValueError(
            f"P0 v8 stop-register canary must match frozen current-stage duration ({configured:g}s)"
        )
    return phase


def authorize_canary(args: Any, current: Mapping[str, Any]) -> dict[str, Any]:
    """Validate the one direct canary duration on the frozen fingerprint."""

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
    review = candidate.get("review_v3")
    fingerprint = str(candidate.get("composite_fingerprint") or "")
    if re.fullmatch(r"[0-9a-f]{64}", fingerprint) is None:
        raise ValueError("P0 v8 composite fingerprint is missing or invalid")
    if composite_fingerprint(candidate) != fingerprint:
        raise ValueError("P0 v8 composite fingerprint does not match its canonical binding")
    if not isinstance(review, Mapping) or not review_authorized(review, fingerprint):
        raise ValueError(
            "P0 v8 no-contact requires the bound Review v3 0+0 policy record"
        )
    if candidate.get("evidence_frozen") is not True:
        raise ValueError("P0 v8 canaries may run only after evidence freeze")
    phase = validate_canary_phase(
        PROFILE, getattr(args, "step5d_stop_register_canary_s", 0.0),
        allow_disabled=False, current=current,
    )
    return {
        "profile": PROFILE,
        "phase_s": phase,
        "composite_fingerprint": fingerprint,
        "package_sha256": capture.get("sha256"),
        "review_manifest": review.get("manifest"),
    }
