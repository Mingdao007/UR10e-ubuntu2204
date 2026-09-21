"""Live PATH duration plan.

Short rungs are diagnostic; the explicit ``r013_compat_60`` request is the
historical 60 s measurement window; the 62.831853 s full period remains a
separate identity.

This module does not open devices.  Native seeds are not physical qualification.
"""
from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Any

# Keep duration parsing transport-free.  Importing the full live method
# registry pulls calibration/Pinocchio dependencies into otherwise pure
# request validation and made offline admission tests environment-sensitive.
DIAGNOSTIC_DURATIONS_S = (2.0, 10.0)
from contact_yield_protocol import PERIOD_S
from tase_figure8_protocol import DURATION_S as R013_COMPAT60_DURATION_S
from tase_figure8_protocol import PROTOCOL_ID as R013_COMPAT60_PROTOCOL_ID


DIAGNOSTIC_CLAIM = (
    "diagnostic short rung; not full-cycle acceptance, not physical qualification, "
    "and not a formal PATH period"
)
FULL_PERIOD_CLAIM = (
    "requested formal PATH period; not physical qualification and not a declared "
    "full-cycle acceptance"
)
R013_COMPAT60_CLAIM = (
    "historical R013-compatible 60 s measurement window; formal metric is "
    "[5,60) with 550 bins; not the 62.831853 s full-period protocol and not "
    "physical qualification by itself"
)


class LivePathRequestError(ValueError):
    """Live duration or claim labeling failed closed."""


@dataclass(frozen=True)
class LivePathRequest:
    kind: str
    path_duration_s: float
    claim_scope: str
    formally_qualified: bool
    full_cycle_acceptance: bool
    protocol_id: str = "contact_yield_full_period_v1"

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "path_duration_s": self.path_duration_s,
            "claim_scope": self.claim_scope,
            "formally_qualified": self.formally_qualified,
            "full_cycle_acceptance": self.full_cycle_acceptance,
            "protocol_id": self.protocol_id,
        }


def _finite_duration(value: Any) -> float:
    try:
        duration = float(value)
    except (TypeError, ValueError) as exc:
        raise LivePathRequestError("live duration is not a finite number") from exc
    if not math.isfinite(duration) or duration <= 0.0:
        raise LivePathRequestError("live duration is not a finite positive duration")
    return duration


def require_live_path_request(value: Any) -> LivePathRequest:
    if not isinstance(value, LivePathRequest):
        raise LivePathRequestError("live path request is not typed")
    if value.formally_qualified is not False or value.full_cycle_acceptance is not False:
        raise LivePathRequestError("live path request cannot declare formal qualification")
    if value.kind == "diagnostic":
        if value.path_duration_s not in DIAGNOSTIC_DURATIONS_S:
            raise LivePathRequestError("diagnostic duration is not 2 s or 10 s")
        if value.path_duration_s >= PERIOD_S:
            raise LivePathRequestError("diagnostic duration cannot cover the formal period")
        if value.claim_scope != DIAGNOSTIC_CLAIM:
            raise LivePathRequestError("diagnostic claim scope differs")
        return value
    if value.kind == "full_period":
        if not math.isclose(value.path_duration_s, PERIOD_S, rel_tol=0.0, abs_tol=1e-12):
            raise LivePathRequestError("full-period request is not the formal PATH period")
        if value.claim_scope != FULL_PERIOD_CLAIM:
            raise LivePathRequestError("full-period claim scope differs")
        if value.protocol_id != "contact_yield_full_period_v1":
            raise LivePathRequestError("full-period protocol identity differs")
        return value
    if value.kind == "r013_compat_60":
        if not math.isclose(
            value.path_duration_s,
            R013_COMPAT60_DURATION_S,
            rel_tol=0.0,
            abs_tol=1e-12,
        ):
            raise LivePathRequestError("R013-compatible request is not exactly 60 s")
        if value.claim_scope != R013_COMPAT60_CLAIM:
            raise LivePathRequestError("R013-compatible claim scope differs")
        if value.protocol_id != R013_COMPAT60_PROTOCOL_ID:
            raise LivePathRequestError("R013-compatible protocol identity differs")
        return value
    raise LivePathRequestError(f"unknown live path kind {value.kind!r}")


def parse_live_duration(value: Any) -> LivePathRequest:
    if isinstance(value, LivePathRequest):
        return require_live_path_request(value)
    if isinstance(value, str):
        token = value.strip().lower()
        if token.endswith("s") and token not in {"full"}:
            token = token[:-1]
        if token in {"full", "full_period", "period"}:
            return LivePathRequest(
                kind="full_period",
                path_duration_s=PERIOD_S,
                claim_scope=FULL_PERIOD_CLAIM,
                formally_qualified=False,
                full_cycle_acceptance=False,
                protocol_id="contact_yield_full_period_v1",
            )
        if token in {"compat60", "r013_60", "r013_compat_60", "window60", "60_r013"}:
            return LivePathRequest(
                kind="r013_compat_60",
                path_duration_s=R013_COMPAT60_DURATION_S,
                claim_scope=R013_COMPAT60_CLAIM,
                formally_qualified=False,
                full_cycle_acceptance=False,
                protocol_id=R013_COMPAT60_PROTOCOL_ID,
            )
        value = token
    duration = _finite_duration(value)
    for diagnostic in DIAGNOSTIC_DURATIONS_S:
        if math.isclose(duration, diagnostic, rel_tol=0.0, abs_tol=1e-12):
            return LivePathRequest(
                kind="diagnostic",
                path_duration_s=diagnostic,
                claim_scope=DIAGNOSTIC_CLAIM,
                formally_qualified=False,
                full_cycle_acceptance=False,
                protocol_id="contact_yield_full_period_v1",
            )
    if math.isclose(duration, PERIOD_S, rel_tol=0.0, abs_tol=1e-12):
        return LivePathRequest(
            kind="full_period",
            path_duration_s=PERIOD_S,
            claim_scope=FULL_PERIOD_CLAIM,
            formally_qualified=False,
            full_cycle_acceptance=False,
            protocol_id="contact_yield_full_period_v1",
        )
    raise LivePathRequestError(
        "live duration must be 2 s, 10 s, compat60/r013_60, or the formal "
        "PATH period; a shortened time cannot be labeled full-period"
    )
