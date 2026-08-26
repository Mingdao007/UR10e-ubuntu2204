"""Compatibility adapter between the new V4 stage identity and R013 writer."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from typing import Any, Mapping

from step5d_autotune_v4_r013.campaign import Dispatch, candidate_token as r013_candidate_token
from step5d_autotune_v4_r013.v4_two_stage_campaign import (
    V4Stage,
    V4StageAttemptV1,
    V4StageError,
    candidate_token,
    validate_candidate,
)


ADAPTER_SCHEMA = "step5d.autotune-v4/v4-stage-r013-adapter-v1"


def materialize_runtime_candidate(
    candidate: Mapping[str, Any],
    stage: V4Stage | str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Map stage semantics to the mature R013 candidate DTO explicitly."""

    selected = stage if isinstance(stage, V4Stage) else V4Stage(stage)
    stage_candidate = validate_candidate(candidate, selected)
    runtime_candidate = dict(stage_candidate)
    if selected is V4Stage.FF_IOFF_100:
        # R006's native candidate DTO carries IMode.OFF.  Keep the boolean in
        # the runtime payload so the physical ledger and the stage ledger do
        # not silently alias two different identities.
        runtime_candidate["i_off"] = True
        runtime_candidate["force_i_gain"] = 0.0
    runtime_candidate = {
        key: runtime_candidate[key]
        for key in (
            "force_p_gain",
            "force_damping",
            "force_i_gain",
            "i_off",
            "normal_filter_tau_s",
            "orientation_ko",
            "motion_kp",
            "target_force_n",
            "integral_state_limit_n_s",
        )
    }
    runtime_token = r013_candidate_token(runtime_candidate)
    receipt_payload = {
        "schema": ADAPTER_SCHEMA,
        "version": 1,
        "stage": selected.value,
        "stage_candidate": stage_candidate,
        "stage_candidate_token": candidate_token(stage_candidate, selected),
        "runtime_candidate": runtime_candidate,
        "runtime_candidate_token": runtime_token,
        "materialization": "native_r006_i_mode_off_or_on",
    }
    receipt_payload["receipt_sha256"] = hashlib.sha256(
        json.dumps(receipt_payload, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    ).hexdigest()
    return runtime_candidate, receipt_payload


@dataclass(frozen=True)
class V4StageDispatchBindingV1:
    stage_attempt: V4StageAttemptV1
    runtime_candidate: Mapping[str, Any]
    materialization_receipt: Mapping[str, Any]
    dispatch: Dispatch

    @classmethod
    def from_attempt(
        cls,
        attempt: V4StageAttemptV1,
        *,
        stage: V4Stage | str,
        runtime_strategy_sha256: str,
        campaign_fingerprint: Mapping[str, Any],
    ) -> "V4StageDispatchBindingV1":
        runtime_candidate, receipt = materialize_runtime_candidate(attempt.candidate, stage)
        dispatch = Dispatch(
            dispatch_id=f"v4-stage-{attempt.ordinal:04d}-{receipt['runtime_candidate_token'][:12]}",
            kind="BO_TRIAL" if attempt.ordinal > 12 else "WARM_SOBOL",
            ordinal=attempt.ordinal,
            candidate=runtime_candidate,
            candidate_token=str(receipt["runtime_candidate_token"]),
            abort_allowed=attempt.ordinal > 12,
            runtime_strategy_sha256=str(runtime_strategy_sha256),
            campaign_fingerprint=dict(campaign_fingerprint),
        )
        return cls(attempt, runtime_candidate, receipt, dispatch)

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": ADAPTER_SCHEMA,
            "version": 1,
            "stage_attempt": self.stage_attempt.as_dict(),
            "runtime_candidate": dict(self.runtime_candidate),
            "materialization_receipt": dict(self.materialization_receipt),
            "dispatch": self.dispatch.as_dict(),
        }


__all__ = [
    "ADAPTER_SCHEMA",
    "V4StageDispatchBindingV1",
    "materialize_runtime_candidate",
]
