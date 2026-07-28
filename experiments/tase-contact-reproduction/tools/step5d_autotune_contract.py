#!/usr/bin/python3.10
"""Pure contracts for the Step5d-native force autotune campaign.

This module deliberately contains no RTDE, Dashboard, controller, or process
side effects.  It binds the log2 search space, native Step5d parameter mapping,
execution profiles, immutable trial identity, and evaluation wire shapes.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Mapping

from ur10e_artifact_store import ArtifactRef, ArtifactStoreError
from ur10e_experiment_runtime.return_route import (
    RETURN_ANGULAR_ACCELERATION_GUARD_RAD_S2,
    RETURN_ANGULAR_SPEED_GUARD_RAD_S,
    RETURN_CONTROLLER_MAX_SAMPLE_GAP_S,
)


SCHEMA_VERSION = "step5d.autotune/v1"
CAMPAIGN_STAGE_ID = "step5d_strict_rnn_autotune_v1"
SOURCE_STAGE_ID = "step5d_strict_rnn_ablation_v35"
TARGET_FORCE_N = 12.0
SEED_FORCE_P_GAIN = 0.001
SEED_FORCE_I_GAIN = 0.00001
SEED_FORCE_DAMPING = 7.0
LOG2_LATTICE_OCTAVE = 0.25
CODEX_I_SCALE_MULTIPLIERS = (10.0, 50.0, 100.0, 500.0, 1000.0)
QDOT_CAP_RAD_S = 0.5
ROTATIONAL_DYNAMICS_X5_PROFILE_ID = "nf500-slew250-a250"
ROTATIONAL_DYNAMICS_X10_PROFILE_ID = "nf1000-slew250-a250"
ROTATIONAL_DYNAMICS_X20_PROFILE_ID = "nf2000-slew250-a250"
NORMAL_FILTER_TAU_S = 0.35
NORMAL_FILTER_DT_MODE = "fixed_0.002s"
EXACT_REPLAY_ENGINE_ID = "step5d_v35_candidate_bound_exact_replay_v1"


class _FrozenJsonDict(dict[str, Any]):
    """JSON-compatible mapping whose nested payload cannot be mutated."""

    @staticmethod
    def _immutable(*_args: Any, **_kwargs: Any) -> None:
        raise TypeError("canonical evidence payload is immutable")

    __setitem__ = _immutable
    __delitem__ = _immutable
    clear = _immutable
    pop = _immutable
    popitem = _immutable
    setdefault = _immutable
    update = _immutable
    __ior__ = _immutable

    def __deepcopy__(self, _memo: dict[int, Any]) -> "_FrozenJsonDict":
        return self


def _freeze_json_value(value: Any) -> Any:
    if isinstance(value, dict):
        frozen = _FrozenJsonDict()
        dict.update(
            frozen,
            {str(key): _freeze_json_value(item) for key, item in value.items()},
        )
        return frozen
    if isinstance(value, list):
        return tuple(_freeze_json_value(item) for item in value)
    return value


def _canonical_frozen_mapping(name: str, value: Any) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{name} must be a mapping")
    try:
        detached = json.loads(canonical_json_bytes(dict(value)).decode("utf-8"))
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError(f"{name} must be a canonical JSON mapping") from exc
    return _freeze_json_value(detached)


def canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("utf-8")


def sha256_json(value: Any) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def require_sha256(name: str, value: Any) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ValueError(f"{name} must be 64 lowercase hexadecimal characters")
    return value


def _finite(name: str, value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a finite float")
    parsed = float(value)
    if not math.isfinite(parsed):
        raise ValueError(f"{name} must be finite")
    return parsed


def _positive_int(name: str, value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{name} must be a positive integer")
    return value


def _non_negative_int(name: str, value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{name} must be a non-negative integer")
    return value


def _strict_bool(name: str, value: Any) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"{name} must be a boolean")
    return value


def _on_lattice(value: float, *, step: float = LOG2_LATTICE_OCTAVE) -> bool:
    return math.isclose(value / step, round(value / step), abs_tol=1e-9)


class SearchTier(str, Enum):
    T1 = "T1"
    T2 = "T2"
    T3 = "T3"

    @property
    def p_d_radius_octaves(self) -> float:
        return {SearchTier.T1: 1.0, SearchTier.T2: 1.5, SearchTier.T3: 2.0}[self]

    @property
    def positive_i_radius_octaves(self) -> float:
        return {SearchTier.T1: 0.0, SearchTier.T2: 1.0, SearchTier.T3: 2.0}[self]


class TrialDisposition(str, Enum):
    OBJECTIVE = "objective"
    WAIT_INFRA_READY = "wait_infra_ready"
    PARAMETER_EVENT = "parameter_event"
    OPERATOR_STOP = "operator_stop"
    CODE_CONTRACT_BUG = "code_contract_bug"
    SAFETY_STOP = "safety_stop"
    MANUAL_RECOVERY = "manual_recovery"
    FAIL_CLOSED = "fail_closed"


@dataclass(frozen=True)
class ForceCandidate:
    """Step5d force-loop candidate expressed in canonical and log2 coordinates."""

    force_p_gain: float = SEED_FORCE_P_GAIN
    force_i_gain: float = SEED_FORCE_I_GAIN
    force_damping: float = SEED_FORCE_DAMPING
    normal_filter_tau_s: float = NORMAL_FILTER_TAU_S
    target_force_n: float = TARGET_FORCE_N

    def __post_init__(self) -> None:
        p = _finite("force_p_gain", self.force_p_gain)
        i = _finite("force_i_gain", self.force_i_gain)
        damping = _finite("force_damping", self.force_damping)
        filter_tau = _finite("normal_filter_tau_s", self.normal_filter_tau_s)
        target = _finite("target_force_n", self.target_force_n)
        if p <= 0.0 or damping <= 0.0 or filter_tau <= 0.0 or i < 0.0:
            raise ValueError(
                "force P/damping/filter tau must be positive and I must be non-negative"
            )
        if not math.isclose(target, TARGET_FORCE_N, abs_tol=1e-12):
            raise ValueError("Step5d autotune target_force_n is fixed at 12 N")
        for name, coordinate in (
            ("log2_p", self.log2_p),
            ("log2_damping", self.log2_damping),
            ("log2_filter_tau", self.log2_filter_tau),
        ):
            if not _on_lattice(coordinate):
                raise ValueError(f"{name} must be on the 0.25-octave lattice")
        if (
            i > 0.0
            and not _on_lattice(self.log2_i)
            and not any(
                math.isclose(i / SEED_FORCE_I_GAIN, multiplier, abs_tol=1e-9)
                for multiplier in CODEX_I_SCALE_MULTIPLIERS
            )
        ):
            raise ValueError(
                "I must use the 0.25-octave lattice or an approved scale multiplier"
            )
        native_values = (1.0 / p, i / p, damping / p)
        if not all(math.isfinite(value) for value in native_values):
            raise ValueError("native Md/kf/Bd mapping must remain finite")
        object.__setattr__(self, "force_p_gain", p)
        object.__setattr__(self, "force_i_gain", i)
        object.__setattr__(self, "force_damping", damping)
        object.__setattr__(self, "normal_filter_tau_s", filter_tau)
        object.__setattr__(self, "target_force_n", target)

    @property
    def log2_p(self) -> float:
        return math.log2(self.force_p_gain / SEED_FORCE_P_GAIN)

    @property
    def log2_damping(self) -> float:
        return math.log2(self.force_damping / SEED_FORCE_DAMPING)

    @property
    def log2_i(self) -> float:
        if self.force_i_gain == 0.0:
            raise ValueError("I=0 is categorical and has no log2 coordinate")
        return math.log2(self.force_i_gain / SEED_FORCE_I_GAIN)

    @property
    def log2_filter_tau(self) -> float:
        return math.log2(self.normal_filter_tau_s / NORMAL_FILTER_TAU_S)

    @property
    def i_mode(self) -> str:
        return "off" if self.force_i_gain == 0.0 else "positive"

    @property
    def approved_i_scale_multiplier(self) -> float | None:
        if self.force_i_gain <= 0.0:
            return None
        actual = self.force_i_gain / SEED_FORCE_I_GAIN
        return next(
            (
                multiplier
                for multiplier in CODEX_I_SCALE_MULTIPLIERS
                if math.isclose(actual, multiplier, abs_tol=1e-9)
            ),
            None,
        )

    @property
    def native_mapping(self) -> dict[str, float]:
        return {
            "Md": 1.0 / self.force_p_gain,
            "kf": self.force_i_gain / self.force_p_gain,
            "Bd": self.force_damping / self.force_p_gain,
        }

    @property
    def candidate_uid(self) -> str:
        return sha256_json(self.payload())

    def payload(self) -> dict[str, Any]:
        payload = {
            "target_force_n": self.target_force_n,
            "force_p_gain": self.force_p_gain,
            "force_i_gain": self.force_i_gain,
            "force_damping": self.force_damping,
            "log2_coordinates": {
                "p": self.log2_p,
                "damping": self.log2_damping,
                "i": None if self.force_i_gain == 0.0 else self.log2_i,
                "i_mode": self.i_mode,
            },
            "native_mapping": self.native_mapping,
        }
        # Preserve the exact historical payload/UID shape for trials 1-50.
        # New non-default filter candidates carry their tau explicitly.
        if not math.isclose(
            self.normal_filter_tau_s, NORMAL_FILTER_TAU_S, abs_tol=1e-12
        ):
            payload["normal_filter_tau_s"] = self.normal_filter_tau_s
            payload["log2_coordinates"]["filter_tau"] = self.log2_filter_tau
        return payload

    def within_tier(self, tier: SearchTier) -> bool:
        if abs(self.log2_p) > tier.p_d_radius_octaves + 1e-9:
            return False
        if abs(self.log2_damping) > tier.p_d_radius_octaves + 1e-9:
            return False
        if abs(self.log2_filter_tau) > 1.0 + 1e-9:
            return False
        if tier is SearchTier.T1:
            return math.isclose(self.force_i_gain, SEED_FORCE_I_GAIN, abs_tol=1e-15)
        return self.force_i_gain == 0.0 or abs(self.log2_i) <= tier.positive_i_radius_octaves + 1e-9

    def within_codex_hybrid_i_envelope(self) -> bool:
        """Bound P/D normally while allowing the approved log10-like I scale probes."""

        if (
            abs(self.log2_p) > SearchTier.T1.p_d_radius_octaves + 1e-9
            or abs(self.log2_damping) > SearchTier.T1.p_d_radius_octaves + 1e-9
            or abs(self.log2_filter_tau) > 1.0 + 1e-9
            or self.force_i_gain <= 0.0
        ):
            return False
        return (
            abs(self.log2_i) <= SearchTier.T2.positive_i_radius_octaves + 1e-9
            or self.approved_i_scale_multiplier is not None
        )

    @classmethod
    def from_log2(
        cls,
        *,
        p: float,
        damping: float,
        filter_tau: float = 0.0,
        i: float | None = 0.0,
        i_off: bool = False,
    ) -> "ForceCandidate":
        for name, coordinate in (
            ("p", p),
            ("damping", damping),
            ("filter_tau", filter_tau),
        ):
            coordinate = _finite(name, coordinate)
            if not _on_lattice(coordinate):
                raise ValueError(f"{name} must be on the 0.25-octave lattice")
        if i_off:
            force_i_gain = 0.0
        else:
            i_coordinate = _finite("i", 0.0 if i is None else i)
            if not _on_lattice(i_coordinate):
                raise ValueError("i must be on the 0.25-octave lattice")
            force_i_gain = SEED_FORCE_I_GAIN * (2.0**i_coordinate)
        return cls(
            force_p_gain=SEED_FORCE_P_GAIN * (2.0**float(p)),
            force_i_gain=force_i_gain,
            force_damping=SEED_FORCE_DAMPING * (2.0**float(damping)),
            normal_filter_tau_s=NORMAL_FILTER_TAU_S * (2.0**float(filter_tau)),
        )

    @classmethod
    def from_i_multiplier(
        cls,
        *,
        p: float,
        damping: float,
        i_multiplier: float,
        filter_tau: float = 0.0,
    ) -> "ForceCandidate":
        multiplier = _finite("i_multiplier", i_multiplier)
        if not any(
            math.isclose(multiplier, approved, abs_tol=1e-9)
            for approved in CODEX_I_SCALE_MULTIPLIERS
        ):
            raise ValueError("i_multiplier is not an approved I scale probe")
        return cls(
            force_p_gain=SEED_FORCE_P_GAIN * (2.0**_finite("p", p)),
            force_i_gain=SEED_FORCE_I_GAIN * multiplier,
            force_damping=SEED_FORCE_DAMPING * (2.0**_finite("damping", damping)),
            normal_filter_tau_s=NORMAL_FILTER_TAU_S
            * (2.0**_finite("filter_tau", filter_tau)),
        )

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "ForceCandidate":
        if "normal_filter_alpha" in payload:
            raise ValueError("normal_filter_alpha is not an active Step5d autotune parameter")
        allowed = {
            "target_force_n",
            "force_p_gain",
            "force_i_gain",
            "force_damping",
            "normal_filter_tau_s",
        }
        unknown = set(payload) - allowed
        if unknown:
            raise ValueError(f"unknown force candidate fields: {sorted(unknown)}")
        return cls(**dict(payload))


@dataclass(frozen=True)
class ExecutionProfile:
    profile_id: str
    normal_max_rate_rad_s: float
    host_qdot_slew_rad_s2: float = 0.1
    tp_speedj_accel_rad_s2: float = 0.1
    qdot_cap_rad_s: float = QDOT_CAP_RAD_S
    normal_filter_tau_s: float = NORMAL_FILTER_TAU_S
    normal_filter_dt_mode: str = NORMAL_FILTER_DT_MODE
    live_eligible: bool = True
    bridge_angular_limit_rad_s: float = 0.05

    def __post_init__(self) -> None:
        if not isinstance(self.profile_id, str) or not self.profile_id.strip():
            raise ValueError("profile_id must be a non-empty string")
        normal_rate = _finite("normal_max_rate_rad_s", self.normal_max_rate_rad_s)
        host_slew = _finite("host_qdot_slew_rad_s2", self.host_qdot_slew_rad_s2)
        tp_accel = _finite("tp_speedj_accel_rad_s2", self.tp_speedj_accel_rad_s2)
        qdot_cap = _finite("qdot_cap_rad_s", self.qdot_cap_rad_s)
        filter_tau = _finite("normal_filter_tau_s", self.normal_filter_tau_s)
        bridge_angular_limit = _finite(
            "bridge_angular_limit_rad_s", self.bridge_angular_limit_rad_s
        )
        _strict_bool("live_eligible", self.live_eligible)
        if normal_rate not in {
            0.010,
            0.015,
            0.020,
            0.030,
            0.050,
            0.100,
            0.500,
            1.000,
            2.000,
        }:
            raise ValueError(
                "normal max-rate must be one of .010/.015/.020/.030/.050/.100/.500/1.000/2.000 rad/s"
            )
        if host_slew not in {0.1, 0.2, 0.5, 2.5}:
            raise ValueError("host qdot slew must be one of .1/.2/.5/2.5 rad/s^2")
        if tp_accel not in {0.1, 0.2, 0.5, 2.5}:
            raise ValueError("TP speedj acceleration must be one of .1/.2/.5/2.5 rad/s^2")
        if qdot_cap not in {0.5, 2.5}:
            raise ValueError("qdot cap must be one of .5/2.5 rad/s")
        if bridge_angular_limit not in {0.05, 0.25}:
            raise ValueError("bridge angular limit must be one of .05/.25 rad/s")
        if filter_tau <= 0.0:
            raise ValueError("normal filter tau must be positive")
        if self.normal_filter_dt_mode != NORMAL_FILTER_DT_MODE:
            raise ValueError("normal filter dt_mode is fixed_0.002s")
        if math.isclose(normal_rate, 0.030, abs_tol=1e-12) and self.live_eligible:
            raise ValueError(".030 rad/s normal-follow profile is offline_only")
        expected_profile_id = (
            "nf030-offline"
            if math.isclose(normal_rate, 0.030, abs_tol=1e-12)
            else (
                f"nf{round(normal_rate * 1000):03d}"
                f"-slew{round(host_slew * 100):03d}"
                f"-a{round(tp_accel * 100):03d}"
            )
        )
        if self.profile_id != expected_profile_id:
            raise ValueError(
                "profile_id must canonically encode normal-rate/host-slew/TP-accel: "
                f"expected {expected_profile_id!r}"
            )
        if expected_profile_id == "nf030-offline" and (
            not math.isclose(host_slew, 0.1, abs_tol=1e-12)
            or not math.isclose(tp_accel, 0.1, abs_tol=1e-12)
        ):
            raise ValueError("offline .030 profile is fixed to host-slew=.1 and TP-accel=.1")
        if any(
            math.isclose(normal_rate, value, abs_tol=1e-12)
            for value in (0.500, 1.000, 2.000)
        ):
            if not (
                math.isclose(host_slew, 2.5, abs_tol=1e-12)
                and math.isclose(tp_accel, 2.5, abs_tol=1e-12)
                and math.isclose(qdot_cap, 2.5, abs_tol=1e-12)
                and math.isclose(bridge_angular_limit, 0.25, abs_tol=1e-12)
            ):
                raise ValueError(
                    "high-dynamics profiles must bind bridge-angular=.25, "
                    "qdot=2.5, host-slew=2.5, and TP-accel=2.5"
                )
        elif not (
            math.isclose(qdot_cap, 0.5, abs_tol=1e-12)
            and math.isclose(bridge_angular_limit, 0.05, abs_tol=1e-12)
        ):
            raise ValueError("historical profiles must retain qdot=.5 and bridge-angular=.05")
        object.__setattr__(self, "normal_max_rate_rad_s", normal_rate)
        object.__setattr__(self, "host_qdot_slew_rad_s2", host_slew)
        object.__setattr__(self, "tp_speedj_accel_rad_s2", tp_accel)
        object.__setattr__(self, "qdot_cap_rad_s", qdot_cap)
        object.__setattr__(self, "normal_filter_tau_s", filter_tau)
        object.__setattr__(self, "bridge_angular_limit_rad_s", bridge_angular_limit)

    def payload(self) -> dict[str, Any]:
        return asdict(self)


NORMAL_FILTER_PROFILES: tuple[ExecutionProfile, ...] = (
    ExecutionProfile("nf010-slew010-a010", 0.010),
    ExecutionProfile("nf015-slew010-a010", 0.015),
    ExecutionProfile("nf020-slew010-a010", 0.020),
    ExecutionProfile("nf030-offline", 0.030, live_eligible=False),
    ExecutionProfile("nf050-slew050-a050", 0.050, 0.5, 0.5),
    ExecutionProfile("nf100-slew050-a050", 0.100, 0.5, 0.5),
    ExecutionProfile(
        ROTATIONAL_DYNAMICS_X5_PROFILE_ID,
        0.500,
        2.5,
        2.5,
        qdot_cap_rad_s=2.5,
        bridge_angular_limit_rad_s=0.25,
    ),
    ExecutionProfile(
        ROTATIONAL_DYNAMICS_X10_PROFILE_ID,
        1.000,
        2.5,
        2.5,
        qdot_cap_rad_s=2.5,
        bridge_angular_limit_rad_s=0.25,
    ),
    ExecutionProfile(
        ROTATIONAL_DYNAMICS_X20_PROFILE_ID,
        2.000,
        2.5,
        2.5,
        qdot_cap_rad_s=2.5,
        bridge_angular_limit_rad_s=0.25,
    ),
)


@dataclass(frozen=True)
class CampaignSpec:
    campaign_id: str
    campaign_epoch: int
    campaign_fingerprint: str
    source_stage_id: str = SOURCE_STAGE_ID
    stage_id: str = CAMPAIGN_STAGE_ID
    target_force_n: float = TARGET_FORCE_N
    objective_window_start_s: float = 5.0
    objective_window_end_s: float = 60.0
    objective_bin_s: float = 0.1
    required_bins: int = 550
    success_mae_n: float = 0.30
    confirmation_relative_delta_max: float = 0.15
    f0_shadow_reaction_normal_base: tuple[float, float, float] | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.campaign_id, str) or not self.campaign_id.strip():
            raise ValueError("campaign_id must be a non-empty string")
        _positive_int("campaign_epoch", self.campaign_epoch)
        require_sha256("campaign_fingerprint", self.campaign_fingerprint)
        if self.source_stage_id != SOURCE_STAGE_ID or self.stage_id != CAMPAIGN_STAGE_ID:
            raise ValueError("Step5d autotune stage/source identities are fixed")
        target_force_n = _finite("target_force_n", self.target_force_n)
        window_start_s = _finite("objective_window_start_s", self.objective_window_start_s)
        window_end_s = _finite("objective_window_end_s", self.objective_window_end_s)
        bin_s = _finite("objective_bin_s", self.objective_bin_s)
        success_mae_n = _finite("success_mae_n", self.success_mae_n)
        repeatability = _finite(
            "confirmation_relative_delta_max", self.confirmation_relative_delta_max
        )
        _positive_int("required_bins", self.required_bins)
        if not math.isclose(target_force_n, TARGET_FORCE_N, rel_tol=0.0, abs_tol=1e-12):
            raise ValueError("Step5d autotune target_force_n is fixed at 12 N")
        if not (
            math.isclose(window_start_s, 5.0, rel_tol=0.0, abs_tol=1e-12)
            and math.isclose(window_end_s, 60.0, rel_tol=0.0, abs_tol=1e-12)
            and math.isclose(bin_s, 0.1, rel_tol=0.0, abs_tol=1e-12)
            and self.required_bins == 550
        ):
            raise ValueError("objective contract is fixed to Stage25 [5,60)s in 550 100 ms bins")
        if not (
            math.isclose(success_mae_n, 0.30, rel_tol=0.0, abs_tol=1e-12)
            and math.isclose(repeatability, 0.15, rel_tol=0.0, abs_tol=1e-12)
        ):
            raise ValueError("success MAE and confirmation repeatability thresholds are fixed")
        expected = round((window_end_s - window_start_s) / bin_s)
        if expected != self.required_bins:
            raise ValueError("objective window must contain exactly 550 fixed 100 ms bins")
        if self.f0_shadow_reaction_normal_base is not None:
            if len(self.f0_shadow_reaction_normal_base) != 3:
                raise ValueError("F0 shadow reaction normal must contain exactly three values")
            normal = tuple(_finite("f0 shadow normal", value) for value in self.f0_shadow_reaction_normal_base)
            norm = math.sqrt(sum(value * value for value in normal))
            if not math.isclose(norm, 1.0, rel_tol=0.0, abs_tol=1e-6):
                raise ValueError("F0 shadow reaction normal must be unit length")
            object.__setattr__(self, "f0_shadow_reaction_normal_base", normal)
        object.__setattr__(self, "target_force_n", target_force_n)
        object.__setattr__(self, "objective_window_start_s", window_start_s)
        object.__setattr__(self, "objective_window_end_s", window_end_s)
        object.__setattr__(self, "objective_bin_s", bin_s)
        object.__setattr__(self, "success_mae_n", success_mae_n)
        object.__setattr__(self, "confirmation_relative_delta_max", repeatability)


@dataclass(frozen=True)
class CandidateReplayEvidence:
    """Measured production-replay evidence; pass/fail is derived, never asserted."""

    engine_id: str
    replayed_rows: int
    accepted_rows: int
    active_bounds_rows: int
    structural_failure_rows: int
    rnn_residual_max: float
    rnn_oracle_qdot_delta_max_rad_s: float
    qdot_max_abs_rad_s: float
    slew_violation_max_rad_s: float
    trace_artifact_ref: ArtifactRef | Mapping[str, Any]
    report_artifact_ref: ArtifactRef | Mapping[str, Any]

    def __post_init__(self) -> None:
        if self.engine_id != EXACT_REPLAY_ENGINE_ID:
            raise ValueError("candidate replay engine identity is fixed")
        _positive_int("replayed_rows", self.replayed_rows)
        for name in (
            "accepted_rows",
            "active_bounds_rows",
            "structural_failure_rows",
        ):
            _non_negative_int(name, getattr(self, name))
        if self.accepted_rows > self.replayed_rows:
            raise ValueError("accepted_rows cannot exceed replayed_rows")
        for name in (
            "rnn_residual_max",
            "rnn_oracle_qdot_delta_max_rad_s",
            "qdot_max_abs_rad_s",
            "slew_violation_max_rad_s",
        ):
            value = _finite(name, getattr(self, name))
            if value < 0.0:
                raise ValueError(f"{name} must be non-negative")
            object.__setattr__(self, name, value)
        for name in ("trace_artifact_ref", "report_artifact_ref"):
            value = getattr(self, name)
            if isinstance(value, Mapping):
                try:
                    value = ArtifactRef.from_mapping(dict(value))
                except ArtifactStoreError as exc:
                    raise ValueError(f"{name} is invalid: {exc}") from exc
            if not isinstance(value, ArtifactRef):
                raise ValueError(f"{name} must be an ArtifactRef")
            object.__setattr__(self, name, value)

    @property
    def accepted_ratio(self) -> float:
        return self.accepted_rows / self.replayed_rows

    @property
    def passed(self) -> bool:
        return bool(
            self.replayed_rows >= 27_500
            and self.accepted_ratio >= 0.98
            and self.active_bounds_rows == 0
            and self.structural_failure_rows == 0
            and self.rnn_residual_max <= 1e-3
            and self.rnn_oracle_qdot_delta_max_rad_s <= 1e-6
            and self.qdot_max_abs_rad_s <= QDOT_CAP_RAD_S + 1e-12
            and self.slew_violation_max_rad_s <= 1e-12
        )

    def metrics_payload(self) -> dict[str, Any]:
        return {
            "engine_id": self.engine_id,
            "replayed_rows": self.replayed_rows,
            "accepted_rows": self.accepted_rows,
            "accepted_ratio": self.accepted_ratio,
            "active_bounds_rows": self.active_bounds_rows,
            "structural_failure_rows": self.structural_failure_rows,
            "rnn_residual_max": self.rnn_residual_max,
            "rnn_oracle_qdot_delta_max_rad_s": (
                self.rnn_oracle_qdot_delta_max_rad_s
            ),
            "qdot_max_abs_rad_s": self.qdot_max_abs_rad_s,
            "slew_violation_max_rad_s": self.slew_violation_max_rad_s,
        }

    def payload(self) -> dict[str, Any]:
        return {
            **self.metrics_payload(),
            "trace_artifact_ref": self.trace_artifact_ref.as_dict(),
            "report_artifact_ref": self.report_artifact_ref.as_dict(),
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "CandidateReplayEvidence":
        required = {
            "engine_id",
            "replayed_rows",
            "accepted_rows",
            "accepted_ratio",
            "active_bounds_rows",
            "structural_failure_rows",
            "rnn_residual_max",
            "rnn_oracle_qdot_delta_max_rad_s",
            "qdot_max_abs_rad_s",
            "slew_violation_max_rad_s",
            "trace_artifact_ref",
            "report_artifact_ref",
        }
        if not isinstance(payload, Mapping) or set(payload) != required:
            raise ValueError("candidate replay evidence payload fields are invalid")
        evidence = cls(
            engine_id=payload["engine_id"],
            replayed_rows=payload["replayed_rows"],
            accepted_rows=payload["accepted_rows"],
            active_bounds_rows=payload["active_bounds_rows"],
            structural_failure_rows=payload["structural_failure_rows"],
            rnn_residual_max=payload["rnn_residual_max"],
            rnn_oracle_qdot_delta_max_rad_s=payload[
                "rnn_oracle_qdot_delta_max_rad_s"
            ],
            qdot_max_abs_rad_s=payload["qdot_max_abs_rad_s"],
            slew_violation_max_rad_s=payload["slew_violation_max_rad_s"],
            trace_artifact_ref=payload["trace_artifact_ref"],
            report_artifact_ref=payload["report_artifact_ref"],
        )
        if not math.isclose(
            float(payload["accepted_ratio"]),
            evidence.accepted_ratio,
            rel_tol=0.0,
            abs_tol=1e-12,
        ):
            raise ValueError("candidate replay accepted_ratio is not derived")
        return evidence


@dataclass(frozen=True)
class SearchAttestation:
    """Artifact-backed exact replay proof for one trace and next candidate."""

    profile_id: str
    plant_epoch: int
    source_trial_uid: str
    latest_trace_sha256: str
    replay_source_fingerprint: str
    replay_config_fingerprint: str
    from_candidate: ForceCandidate
    to_candidate: ForceCandidate
    next_candidate_uid: str
    outward_axis: str
    outward_direction: int
    replay_evidence: CandidateReplayEvidence

    def __post_init__(self) -> None:
        if not isinstance(self.profile_id, str) or not self.profile_id.strip():
            raise ValueError("profile_id must be a non-empty string")
        _positive_int("plant_epoch", self.plant_epoch)
        require_sha256("source_trial_uid", self.source_trial_uid)
        require_sha256("latest_trace_sha256", self.latest_trace_sha256)
        require_sha256("replay_source_fingerprint", self.replay_source_fingerprint)
        require_sha256("replay_config_fingerprint", self.replay_config_fingerprint)
        require_sha256("next_candidate_uid", self.next_candidate_uid)
        if not isinstance(self.from_candidate, ForceCandidate) or not isinstance(
            self.to_candidate, ForceCandidate
        ):
            raise ValueError("from_candidate and to_candidate must be ForceCandidate values")
        if self.next_candidate_uid != self.to_candidate.candidate_uid:
            raise ValueError("next_candidate_uid must identify to_candidate exactly")
        if self.outward_axis not in {"p", "damping", "i", "filter_tau"}:
            raise ValueError("outward_axis must be p, damping, i, or filter_tau")
        if self.outward_direction not in {-1, 1}:
            raise ValueError("outward_direction must be -1 or 1")
        if not isinstance(self.replay_evidence, CandidateReplayEvidence):
            raise ValueError("replay_evidence must be CandidateReplayEvidence")
        if self.replay_evidence.trace_artifact_ref.sha256 != self.latest_trace_sha256:
            raise ValueError("replay trace artifact must match latest_trace_sha256")
        deltas = {
            "p": self.to_candidate.log2_p - self.from_candidate.log2_p,
            "damping": (
                self.to_candidate.log2_damping - self.from_candidate.log2_damping
            ),
            "filter_tau": (
                self.to_candidate.log2_filter_tau
                - self.from_candidate.log2_filter_tau
            ),
        }
        if (
            self.from_candidate.i_mode == "positive"
            and self.to_candidate.i_mode == "positive"
        ):
            deltas["i"] = self.to_candidate.log2_i - self.from_candidate.log2_i
        elif self.from_candidate.i_mode != self.to_candidate.i_mode:
            deltas["i_mode"] = 1.0
        changed = tuple(
            name for name, value in deltas.items() if not math.isclose(value, 0.0, abs_tol=1e-9)
        )
        if changed != (self.outward_axis,):
            raise ValueError("attested candidates must change exactly the outward continuous axis")
        delta = deltas[self.outward_axis]
        if not math.isclose(abs(delta), LOG2_LATTICE_OCTAVE, abs_tol=1e-9):
            raise ValueError("attested candidates must be one exact quarter-octave step")
        if int(math.copysign(1, delta)) != self.outward_direction:
            raise ValueError("outward_direction does not match the attested candidate step")

    @property
    def exact_replay_passed(self) -> bool:
        return self.replay_evidence.passed

    def payload(self) -> dict[str, Any]:
        return {
            "profile_id": self.profile_id,
            "plant_epoch": self.plant_epoch,
            "source_trial_uid": self.source_trial_uid,
            "latest_trace_sha256": self.latest_trace_sha256,
            "replay_source_fingerprint": self.replay_source_fingerprint,
            "replay_config_fingerprint": self.replay_config_fingerprint,
            "from_candidate": self.from_candidate.payload(),
            "to_candidate": self.to_candidate.payload(),
            "next_candidate_uid": self.next_candidate_uid,
            "outward_axis": self.outward_axis,
            "outward_direction": self.outward_direction,
            "replay_evidence": self.replay_evidence.payload(),
        }

    @property
    def attestation_uid(self) -> str:
        return sha256_json(self.payload())

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "SearchAttestation":
        required = {
            "profile_id",
            "plant_epoch",
            "source_trial_uid",
            "latest_trace_sha256",
            "replay_source_fingerprint",
            "replay_config_fingerprint",
            "from_candidate",
            "to_candidate",
            "next_candidate_uid",
            "outward_axis",
            "outward_direction",
            "replay_evidence",
        }
        if not isinstance(payload, Mapping) or set(payload) != required:
            raise ValueError("search attestation payload fields are invalid")

        def candidate_from_payload(value: Any) -> ForceCandidate:
            if not isinstance(value, Mapping):
                raise ValueError("search attestation candidate payload is invalid")
            candidate = ForceCandidate(
                target_force_n=value.get("target_force_n"),
                force_p_gain=value.get("force_p_gain"),
                force_i_gain=value.get("force_i_gain"),
                force_damping=value.get("force_damping"),
                normal_filter_tau_s=value.get(
                    "normal_filter_tau_s", NORMAL_FILTER_TAU_S
                ),
            )
            if canonical_json_bytes(candidate.payload()) != canonical_json_bytes(dict(value)):
                raise ValueError("search attestation candidate payload is not canonical")
            return candidate

        return cls(
            profile_id=payload["profile_id"],
            plant_epoch=payload["plant_epoch"],
            source_trial_uid=payload["source_trial_uid"],
            latest_trace_sha256=payload["latest_trace_sha256"],
            replay_source_fingerprint=payload["replay_source_fingerprint"],
            replay_config_fingerprint=payload["replay_config_fingerprint"],
            from_candidate=candidate_from_payload(payload["from_candidate"]),
            to_candidate=candidate_from_payload(payload["to_candidate"]),
            next_candidate_uid=payload["next_candidate_uid"],
            outward_axis=payload["outward_axis"],
            outward_direction=payload["outward_direction"],
            replay_evidence=CandidateReplayEvidence.from_payload(
                payload["replay_evidence"]
            ),
        )


class TrialTransitionKind(str, Enum):
    BASELINE = "baseline"
    BATCH_BOOTSTRAP = "batch_bootstrap"
    FORCE_SEARCH = "force_search"
    REPLICATION = "replication"
    RETRY = "retry"
    GOVERNOR_PROBE = "governor_probe"
    PLANT_EPOCH_ANCHOR = "plant_epoch_anchor"
    CODE_EPOCH_SEARCH = "code_epoch_search"
    I_SCALE_PROBE = "i_scale_probe"


class TrialTransitionSourceScope(str, Enum):
    """Where a transition source must already exist when history is read."""

    NONE = "none"
    CURRENT_HISTORY = "current_history"
    ARCHIVED_HISTORY = "archived_history"


@dataclass(frozen=True)
class TrialTransitionPolicy:
    requires_source: bool
    campaign_start_only: bool
    source_scope: TrialTransitionSourceScope


_SOURCE_FREE_TRANSITIONS = frozenset(
    {
        TrialTransitionKind.BASELINE,
        TrialTransitionKind.BATCH_BOOTSTRAP,
    }
)
_ARCHIVED_SOURCE_TRANSITIONS = frozenset({TrialTransitionKind.CODE_EPOCH_SEARCH})


def trial_transition_policy(
    kind: TrialTransitionKind,
    *,
    retry_kind: str | None = None,
) -> TrialTransitionPolicy:
    """Return the single authoritative source/history policy for every kind."""

    if not isinstance(kind, TrialTransitionKind):
        raise ValueError("trial transition kind must be TrialTransitionKind")
    if kind in _SOURCE_FREE_TRANSITIONS:
        return TrialTransitionPolicy(
            requires_source=False,
            campaign_start_only=True,
            source_scope=TrialTransitionSourceScope.NONE,
        )
    if kind in _ARCHIVED_SOURCE_TRANSITIONS or (
        kind is TrialTransitionKind.RETRY and retry_kind == "code_fix"
    ):
        return TrialTransitionPolicy(
            requires_source=True,
            campaign_start_only=False,
            source_scope=TrialTransitionSourceScope.ARCHIVED_HISTORY,
        )
    return TrialTransitionPolicy(
        requires_source=True,
        campaign_start_only=False,
        source_scope=TrialTransitionSourceScope.CURRENT_HISTORY,
    )


@dataclass(frozen=True)
class TrialSource:
    """Immutable identity of the executed trial that authorizes a transition."""

    trial_uid: str
    candidate: ForceCandidate
    profile_id: str
    plant_epoch: int
    campaign_id: str
    campaign_epoch: int
    campaign_fingerprint: str
    backend_id: str
    source_fingerprint: str
    config_fingerprint: str

    def __post_init__(self) -> None:
        require_sha256("trial source trial_uid", self.trial_uid)
        if not isinstance(self.candidate, ForceCandidate):
            raise ValueError("trial source candidate must be ForceCandidate")
        for name in ("profile_id", "campaign_id", "backend_id"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"trial source {name} must be non-empty")
        _positive_int("trial source plant_epoch", self.plant_epoch)
        _positive_int("trial source campaign_epoch", self.campaign_epoch)
        for name in (
            "campaign_fingerprint",
            "source_fingerprint",
            "config_fingerprint",
        ):
            require_sha256(f"trial source {name}", getattr(self, name))

    def payload(self) -> dict[str, Any]:
        return {
            "trial_uid": self.trial_uid,
            "candidate": self.candidate.payload(),
            "profile_id": self.profile_id,
            "plant_epoch": self.plant_epoch,
            "campaign_id": self.campaign_id,
            "campaign_epoch": self.campaign_epoch,
            "campaign_fingerprint": self.campaign_fingerprint,
            "backend_id": self.backend_id,
            "source_fingerprint": self.source_fingerprint,
            "config_fingerprint": self.config_fingerprint,
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "TrialSource":
        required = {
            "trial_uid",
            "candidate",
            "profile_id",
            "plant_epoch",
            "campaign_id",
            "campaign_epoch",
            "campaign_fingerprint",
            "backend_id",
            "source_fingerprint",
            "config_fingerprint",
        }
        if not isinstance(payload, Mapping) or set(payload) != required:
            raise ValueError("trial source payload fields are invalid")
        candidate_payload = payload["candidate"]
        if not isinstance(candidate_payload, Mapping):
            raise ValueError("trial source candidate payload is invalid")
        candidate = ForceCandidate(
            target_force_n=candidate_payload.get("target_force_n"),
            force_p_gain=candidate_payload.get("force_p_gain"),
            force_i_gain=candidate_payload.get("force_i_gain"),
            force_damping=candidate_payload.get("force_damping"),
            normal_filter_tau_s=candidate_payload.get(
                "normal_filter_tau_s", NORMAL_FILTER_TAU_S
            ),
        )
        if canonical_json_bytes(candidate.payload()) != canonical_json_bytes(
            dict(candidate_payload)
        ):
            raise ValueError("trial source candidate payload is not canonical")
        return cls(
            trial_uid=payload["trial_uid"],
            candidate=candidate,
            profile_id=payload["profile_id"],
            plant_epoch=payload["plant_epoch"],
            campaign_id=payload["campaign_id"],
            campaign_epoch=payload["campaign_epoch"],
            campaign_fingerprint=payload["campaign_fingerprint"],
            backend_id=payload["backend_id"],
            source_fingerprint=payload["source_fingerprint"],
            config_fingerprint=payload["config_fingerprint"],
        )


@dataclass(frozen=True)
class TrialTransition:
    """Why this trial may follow its exact persisted source trial."""

    kind: TrialTransitionKind
    source: TrialSource | None = None
    retry_kind: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.kind, TrialTransitionKind):
            raise ValueError("trial transition kind must be TrialTransitionKind")
        policy = trial_transition_policy(self.kind, retry_kind=self.retry_kind)
        if not policy.requires_source:
            if self.source is not None or self.retry_kind is not None:
                raise ValueError(
                    "source-free transition cannot name a source or retry"
                )
            return
        if not isinstance(self.source, TrialSource):
            raise ValueError("non-baseline transition requires an exact TrialSource")
        if self.kind is TrialTransitionKind.RETRY:
            if self.retry_kind not in {"infrastructure", "evidence", "code_fix"}:
                raise ValueError("retry transition kind is invalid")
        elif self.retry_kind is not None:
            raise ValueError("retry_kind is valid only for retry transitions")

    @property
    def policy(self) -> TrialTransitionPolicy:
        return trial_transition_policy(self.kind, retry_kind=self.retry_kind)

    def payload(self) -> dict[str, Any]:
        return {
            "kind": self.kind.value,
            "source": None if self.source is None else self.source.payload(),
            "retry_kind": self.retry_kind,
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "TrialTransition":
        if not isinstance(payload, Mapping) or set(payload) != {
            "kind",
            "source",
            "retry_kind",
        }:
            raise ValueError("trial transition payload fields are invalid")
        try:
            kind = TrialTransitionKind(payload["kind"])
        except (TypeError, ValueError) as exc:
            raise ValueError("trial transition kind is invalid") from exc
        source_payload = payload["source"]
        return cls(
            kind=kind,
            source=(
                None
                if source_payload is None
                else TrialSource.from_payload(source_payload)
            ),
            retry_kind=payload["retry_kind"],
        )


def trial_source_from_trial(trial: "TrialSpec") -> TrialSource:
    return TrialSource(
        trial_uid=trial.trial_uid,
        candidate=trial.candidate,
        profile_id=trial.execution_profile.profile_id,
        plant_epoch=trial.plant_epoch,
        campaign_id=trial.campaign.campaign_id,
        campaign_epoch=trial.campaign.campaign_epoch,
        campaign_fingerprint=trial.campaign.campaign_fingerprint,
        backend_id=trial.backend_id,
        source_fingerprint=trial.source_fingerprint,
        config_fingerprint=trial.config_fingerprint,
    )


def _trial_candidate_step(
    source: ForceCandidate, target: ForceCandidate
) -> tuple[str, float] | None:
    deltas: list[tuple[str, float]] = []
    for axis, before, after in (
        ("p", source.log2_p, target.log2_p),
        ("damping", source.log2_damping, target.log2_damping),
        ("filter_tau", source.log2_filter_tau, target.log2_filter_tau),
    ):
        if not math.isclose(before, after, abs_tol=1e-9):
            deltas.append((axis, after - before))
    if source.i_mode != target.i_mode:
        deltas.append(("i_mode", 1.0))
    elif source.i_mode == "positive" and not math.isclose(
        source.log2_i, target.log2_i, abs_tol=1e-9
    ):
        deltas.append(("i", target.log2_i - source.log2_i))
    if len(deltas) != 1:
        return None
    axis, delta = deltas[0]
    if axis != "i_mode" and not math.isclose(
        abs(delta), LOG2_LATTICE_OCTAVE, abs_tol=1e-9
    ):
        return None
    return axis, delta


def codex_i_scale_probe_transition(
    source: ForceCandidate,
    target: ForceCandidate,
) -> bool:
    """Return true for one-axis probes on the approved coarse I scale grid."""

    if source.i_mode != "positive" or target.i_mode != "positive":
        return False
    return (
        math.isclose(source.log2_p, target.log2_p, abs_tol=1e-9)
        and math.isclose(source.log2_damping, target.log2_damping, abs_tol=1e-9)
        and math.isclose(
            source.log2_filter_tau, target.log2_filter_tau, abs_tol=1e-9
        )
        and not math.isclose(source.log2_i, target.log2_i, abs_tol=1e-9)
        and target.approved_i_scale_multiplier is not None
    )


@dataclass(frozen=True)
class TrialSpec:
    campaign: CampaignSpec
    trial_id: int
    candidate_token: int
    command_seq: int
    plant_epoch: int
    candidate: ForceCandidate
    execution_profile: ExecutionProfile
    backend_id: str
    source_fingerprint: str
    config_fingerprint: str
    transition: TrialTransition
    search_attestation: SearchAttestation | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.campaign, CampaignSpec):
            raise ValueError("campaign must be a CampaignSpec")
        if not isinstance(self.candidate, ForceCandidate):
            raise ValueError("candidate must be a ForceCandidate")
        if not isinstance(self.execution_profile, ExecutionProfile):
            raise ValueError("execution_profile must be an ExecutionProfile")
        for name, value in (
            ("trial_id", self.trial_id),
            ("candidate_token", self.candidate_token),
            ("command_seq", self.command_seq),
            ("plant_epoch", self.plant_epoch),
        ):
            _positive_int(name, value)
        for name, value in (
            ("source_fingerprint", self.source_fingerprint),
            ("config_fingerprint", self.config_fingerprint),
        ):
            require_sha256(name, value)
        if not isinstance(self.backend_id, str) or not self.backend_id.strip():
            raise ValueError("backend_id must be a non-empty string")
        if not (
            self.candidate.within_tier(SearchTier.T3)
            or self.candidate.within_codex_hybrid_i_envelope()
        ):
            raise ValueError("trial candidate is outside the frozen T3 envelope")
        if not isinstance(self.transition, TrialTransition):
            raise ValueError("transition must be TrialTransition")
        transition = self.transition
        source = transition.source
        requires_search_attestation = False
        if transition.kind is TrialTransitionKind.BASELINE:
            if self.candidate != ForceCandidate():
                raise ValueError("baseline transition is fixed to the exact v35 seed")
        elif transition.kind is TrialTransitionKind.BATCH_BOOTSTRAP:
            if source is not None:
                raise ValueError("batch bootstrap cannot name a source trial")
        else:
            assert source is not None
            if (
                source.backend_id != self.backend_id
                or source.campaign_id != self.campaign.campaign_id
            ):
                raise ValueError("trial transition source backend/campaign is inconsistent")
            same_candidate = source.candidate == self.candidate
            same_profile = source.profile_id == self.execution_profile.profile_id
            same_plant_epoch = source.plant_epoch == self.plant_epoch
            same_campaign_epoch = (
                source.campaign_epoch == self.campaign.campaign_epoch
                and source.campaign_fingerprint == self.campaign.campaign_fingerprint
            )
            same_fingerprints = (
                source.source_fingerprint == self.source_fingerprint
                and source.config_fingerprint == self.config_fingerprint
            )
            if transition.kind is TrialTransitionKind.I_SCALE_PROBE:
                same_or_new_code_epoch = (
                    same_campaign_epoch and same_fingerprints
                ) or (
                    self.campaign.campaign_epoch > source.campaign_epoch
                    and self.campaign.campaign_fingerprint
                    != source.campaign_fingerprint
                )
                if (
                    not codex_i_scale_probe_transition(
                        source.candidate,
                        self.candidate,
                    )
                    or same_candidate
                    or not same_profile
                    or not same_plant_epoch
                    or not same_or_new_code_epoch
                ):
                    raise ValueError(
                        "i_scale_probe requires one approved same-P/D I scale step"
                    )
            elif transition.kind is TrialTransitionKind.FORCE_SEARCH:
                step = _trial_candidate_step(source.candidate, self.candidate)
                if (
                    step is None
                    or same_candidate
                    or not same_profile
                    or not same_plant_epoch
                    or not same_campaign_epoch
                    or not same_fingerprints
                ):
                    raise ValueError(
                        "force_search requires one exact same-context candidate step"
                    )
                axis, _delta = step
                if axis == "i_mode" and not self.candidate.within_tier(SearchTier.T2):
                    raise ValueError("outside-T2 categorical I-mode search is prohibited")
                if not self.candidate.within_tier(SearchTier.T2):
                    if axis == "p":
                        before = source.candidate.log2_p
                        after = self.candidate.log2_p
                    elif axis == "damping":
                        before = source.candidate.log2_damping
                        after = self.candidate.log2_damping
                    elif axis == "filter_tau":
                        before = source.candidate.log2_filter_tau
                        after = self.candidate.log2_filter_tau
                    else:
                        before = source.candidate.log2_i
                        after = self.candidate.log2_i
                    requires_search_attestation = abs(after) > abs(before) + 1e-9
            elif transition.kind is TrialTransitionKind.REPLICATION:
                if not (
                    same_candidate
                    and same_profile
                    and same_plant_epoch
                    and same_campaign_epoch
                    and same_fingerprints
                ):
                    raise ValueError("replication must preserve candidate and full context")
            elif transition.kind is TrialTransitionKind.RETRY:
                if not (same_candidate and same_profile and same_plant_epoch):
                    raise ValueError("retry must preserve candidate/profile/plant epoch")
                if transition.retry_kind == "code_fix":
                    if not (
                        self.campaign.campaign_epoch > source.campaign_epoch
                        and self.campaign.campaign_fingerprint
                        != source.campaign_fingerprint
                    ):
                        raise ValueError(
                            "code-fix retry requires a newer fingerprinted campaign epoch"
                        )
                elif not (same_campaign_epoch and same_fingerprints):
                    raise ValueError(
                        "infrastructure/evidence retry must preserve campaign fingerprints"
                    )
            elif transition.kind is TrialTransitionKind.GOVERNOR_PROBE:
                if not (
                    same_candidate
                    and not same_profile
                    and same_plant_epoch
                    and same_campaign_epoch
                    and same_fingerprints
                ):
                    raise ValueError(
                        "governor probe must change only profile for one force candidate"
                    )
            elif transition.kind is TrialTransitionKind.PLANT_EPOCH_ANCHOR:
                if not (
                    same_candidate
                    and self.plant_epoch == source.plant_epoch + 1
                    and same_campaign_epoch
                    and same_fingerprints
                ):
                    raise ValueError(
                        "plant epoch anchor must replicate candidate into the next epoch"
                    )
            elif transition.kind is TrialTransitionKind.CODE_EPOCH_SEARCH:
                if (
                    (
                        _trial_candidate_step(source.candidate, self.candidate) is None
                        and not codex_i_scale_probe_transition(
                            source.candidate,
                            self.candidate,
                        )
                    )
                    or same_candidate
                    or not same_profile
                    or not same_plant_epoch
                    or self.campaign.campaign_epoch <= source.campaign_epoch
                    or self.campaign.campaign_fingerprint
                    == source.campaign_fingerprint
                ):
                    raise ValueError(
                        "code_epoch_search requires one new adjacent or approved I-scale "
                        "candidate in a newer fingerprinted epoch"
                    )
            else:  # pragma: no cover - Enum exhaustiveness guard.
                raise ValueError("unsupported trial transition kind")
        if requires_search_attestation and self.search_attestation is None:
            raise ValueError(
                "outward T3 force_search requires a candidate-bound search attestation"
            )
        if not requires_search_attestation and self.search_attestation is not None:
            raise ValueError(
                "search attestation is permitted only for an outward T3 force_search"
            )
        if self.search_attestation is not None:
            attestation = self.search_attestation
            if not isinstance(attestation, SearchAttestation):
                raise ValueError("search_attestation must be SearchAttestation or None")
            if not attestation.exact_replay_passed:
                raise ValueError("search attestation exact RNN/oracle/slew replay must pass")
            if (
                attestation.to_candidate != self.candidate
                or attestation.next_candidate_uid != self.candidate.candidate_uid
                or source is None
                or attestation.source_trial_uid != source.trial_uid
                or attestation.from_candidate != source.candidate
                or attestation.profile_id != self.execution_profile.profile_id
                or attestation.plant_epoch != self.plant_epoch
                or attestation.replay_source_fingerprint != self.source_fingerprint
                or attestation.replay_config_fingerprint != self.config_fingerprint
            ):
                raise ValueError("search attestation does not bind this exact trial")

    @property
    def trial_uid(self) -> str:
        """Stable identity; intentionally excludes run/output directory paths."""

        return sha256_json(
            {
                "schema": SCHEMA_VERSION,
                "campaign_id": self.campaign.campaign_id,
                "campaign_epoch": self.campaign.campaign_epoch,
                "campaign_fingerprint": self.campaign.campaign_fingerprint,
                "trial_id": self.trial_id,
                "candidate_token": self.candidate_token,
                "command_seq": self.command_seq,
                "plant_epoch": self.plant_epoch,
                "candidate": self.candidate.payload(),
                "execution_profile": self.execution_profile.payload(),
                "backend_id": self.backend_id,
                "source_fingerprint": self.source_fingerprint,
                "config_fingerprint": self.config_fingerprint,
                "transition": self.transition.payload(),
                "search_attestation": (
                    None
                    if self.search_attestation is None
                    else self.search_attestation.payload()
                ),
            }
        )

    @property
    def comparison_key(self) -> str:
        return sha256_json(
            {
                "candidate": self.candidate.payload(),
                "execution_profile": self.execution_profile.payload(),
                "plant_epoch": self.plant_epoch,
                "campaign_epoch": self.campaign.campaign_epoch,
            }
        )

    def payload(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "trial_uid": self.trial_uid,
            "candidate_uid": self.candidate.candidate_uid,
            "comparison_key": self.comparison_key,
            "campaign": asdict(self.campaign),
            "trial_id": self.trial_id,
            "candidate_token": self.candidate_token,
            "command_seq": self.command_seq,
            "plant_epoch": self.plant_epoch,
            "candidate": self.candidate.payload(),
            "execution_profile": self.execution_profile.payload(),
            "backend_id": self.backend_id,
            "source_fingerprint": self.source_fingerprint,
            "config_fingerprint": self.config_fingerprint,
            "transition": self.transition.payload(),
            "search_attestation": (
                None
                if self.search_attestation is None
                else self.search_attestation.payload()
            ),
        }


@dataclass(frozen=True)
class SafeClosureEvidence:
    """Exact TP + host proof required before an immutable-bundle ACK."""

    tp_position_error_m: float
    tp_orientation_error_rad: float
    tp_joint_error_max_rad: float
    host_position_error_m: float
    host_orientation_error_rad: float
    host_joint_error_max_rad: float
    host_tcp_linear_speed_m_s: float
    host_tcp_angular_speed_rad_s: float
    host_qd_max_rad_s: float
    host_safety_mode: str
    host_dwell_s: float
    trial_token_match: bool
    capture_hashes_complete: bool
    terminal_manifest_complete: bool
    fingerprint_closed: bool

    def __post_init__(self) -> None:
        for name in (
            "tp_position_error_m",
            "tp_orientation_error_rad",
            "tp_joint_error_max_rad",
            "host_position_error_m",
            "host_orientation_error_rad",
            "host_joint_error_max_rad",
            "host_tcp_linear_speed_m_s",
            "host_tcp_angular_speed_rad_s",
            "host_qd_max_rad_s",
            "host_dwell_s",
        ):
            value = _finite(name, getattr(self, name))
            if value < 0.0:
                raise ValueError(f"{name} must be non-negative")
            object.__setattr__(self, name, value)
        if not isinstance(self.host_safety_mode, str) or not self.host_safety_mode:
            raise ValueError("host_safety_mode must be a non-empty string")
        for name in (
            "trial_token_match",
            "capture_hashes_complete",
            "terminal_manifest_complete",
            "fingerprint_closed",
        ):
            _strict_bool(name, getattr(self, name))

    def failures(self) -> tuple[str, ...]:
        limits = (
            ("tp_position", self.tp_position_error_m, 0.003),
            ("tp_orientation", self.tp_orientation_error_rad, 0.05),
            ("tp_joint", self.tp_joint_error_max_rad, 0.01),
            ("host_position", self.host_position_error_m, 0.003),
            ("host_orientation", self.host_orientation_error_rad, 0.05),
            ("host_joint", self.host_joint_error_max_rad, 0.01),
            ("host_tcp_linear_speed", self.host_tcp_linear_speed_m_s, 0.001),
            ("host_tcp_angular_speed", self.host_tcp_angular_speed_rad_s, 0.01),
            ("host_qd", self.host_qd_max_rad_s, 0.01),
        )
        failures = [
            f"{name}_outside_limit" for name, value, limit in limits if value > limit
        ]
        if self.host_safety_mode != "NORMAL":
            failures.append("host_safety_not_normal")
        if self.host_dwell_s < 0.5:
            failures.append("host_safe_dwell_short")
        for name, value in (
            ("trial_token", self.trial_token_match),
            ("capture_hashes", self.capture_hashes_complete),
            ("terminal_manifest", self.terminal_manifest_complete),
            ("fingerprint", self.fingerprint_closed),
        ):
            if not value:
                failures.append(f"{name}_not_closed")
        return tuple(failures)

    @property
    def returned_safe(self) -> bool:
        return not self.failures()

    def payload(self) -> dict[str, Any]:
        return asdict(self)


_TYPED_RETURN_GUARDS = frozenset(
    {
        "force",
        "torque",
        "joints",
        "sensor_freshness",
        "heartbeat",
        "contact_loss",
        "route_workspace",
    }
)


@dataclass(frozen=True)
class TypedSafeClosureEvidence:
    """Exact pre-ACK closure at a sealed near-ready or campaign-home target."""

    return_reference_uid: str
    return_reference_kind: str
    batch_row_index: int
    tp_position_error_m: float
    tp_orientation_error_rad: float
    tp_qd_max_rad_s: float
    return_phase_echo: float
    return_segment_id: int
    return_current_angular_speed_rad_s: float
    return_current_angular_acceleration_rad_s2: float
    return_max_angular_speed_rad_s: float
    return_max_angular_acceleration_rad_s2: float
    return_max_sample_gap_s: float
    host_position_error_m: float
    host_orientation_error_rad: float
    host_tcp_linear_speed_m_s: float
    host_tcp_angular_speed_rad_s: float
    host_qd_max_rad_s: float
    return_guard_mask: int
    safety_guards: Mapping[str, bool]
    host_safety_mode: str
    host_dwell_s: float
    trial_token_match: bool
    capture_hashes_complete: bool
    terminal_manifest_complete: bool
    fingerprint_closed: bool
    protocol: str = "v3_direct_arm_v1"
    logical_batch_sequence: int = 0
    schema: str = field(
        default="step5d.autotune/typed-safe-closure-v2",
        init=False,
    )

    def __post_init__(self) -> None:
        require_sha256("return_reference_uid", self.return_reference_uid)
        _positive_int("batch_row_index", self.batch_row_index)
        rolling = self.protocol == "v3_full_home_rolling_arm_v1"
        if self.protocol not in {"v3_direct_arm_v1", "v3_full_home_rolling_arm_v1"}:
            raise ValueError("typed closure protocol is not recognized")
        max_rows = 5 if rolling else 10
        if self.batch_row_index > max_rows:
            raise ValueError("batch_row_index exceeds the protocol batch size")
        if (
            isinstance(self.logical_batch_sequence, bool)
            or not isinstance(self.logical_batch_sequence, int)
            or self.logical_batch_sequence < 0
            or rolling != (self.logical_batch_sequence > 0)
        ):
            raise ValueError("typed closure logical batch sequence differs from protocol")
        expected_kind = (
            "campaign_home"
            if rolling or self.batch_row_index == 10
            else "near_ready"
        )
        if self.return_reference_kind != expected_kind:
            raise ValueError("typed closure reference differs from exact batch row")
        for name in (
            "tp_position_error_m",
            "tp_orientation_error_rad",
            "tp_qd_max_rad_s",
            "return_phase_echo",
            "return_current_angular_speed_rad_s",
            "return_current_angular_acceleration_rad_s2",
            "return_max_angular_speed_rad_s",
            "return_max_angular_acceleration_rad_s2",
            "return_max_sample_gap_s",
            "host_position_error_m",
            "host_orientation_error_rad",
            "host_tcp_linear_speed_m_s",
            "host_tcp_angular_speed_rad_s",
            "host_qd_max_rad_s",
            "host_dwell_s",
        ):
            value = _finite(name, getattr(self, name))
            if value < 0.0:
                raise ValueError(f"{name} must be non-negative")
            object.__setattr__(self, name, value)
        if not math.isclose(self.return_phase_echo, 40.3, abs_tol=1e-9):
            raise ValueError("typed closure does not prove completion of return segment 3")
        if type(self.return_segment_id) is not int or self.return_segment_id != 3:
            raise ValueError("typed closure return segment identity is incomplete")
        if any(
            (
                self.return_current_angular_speed_rad_s
                > RETURN_ANGULAR_SPEED_GUARD_RAD_S,
                self.return_current_angular_acceleration_rad_s2
                > RETURN_ANGULAR_ACCELERATION_GUARD_RAD_S2,
                self.return_max_angular_speed_rad_s
                > RETURN_ANGULAR_SPEED_GUARD_RAD_S,
                self.return_max_angular_acceleration_rad_s2
                > RETURN_ANGULAR_ACCELERATION_GUARD_RAD_S2,
                self.return_max_sample_gap_s <= 0.0,
                self.return_max_sample_gap_s > RETURN_CONTROLLER_MAX_SAMPLE_GAP_S,
            )
        ):
            raise ValueError("typed closure return angular envelope is incomplete or breached")
        if self.return_guard_mask != 0x7F:
            raise ValueError("typed closure return guard mask is incomplete")
        guards = _canonical_frozen_mapping("safety_guards", self.safety_guards)
        if set(guards) != _TYPED_RETURN_GUARDS or not all(
            type(value) is bool and value for value in guards.values()
        ):
            raise ValueError("typed closure safety guards are incomplete or failed")
        object.__setattr__(self, "safety_guards", guards)
        if self.host_safety_mode != "NORMAL":
            raise ValueError("typed closure host safety mode is not NORMAL")
        for name in (
            "trial_token_match",
            "capture_hashes_complete",
            "terminal_manifest_complete",
            "fingerprint_closed",
        ):
            _strict_bool(name, getattr(self, name))

    def failures(self) -> tuple[str, ...]:
        limits = (
            ("tp_position", self.tp_position_error_m, 0.003),
            ("tp_orientation", self.tp_orientation_error_rad, 0.05),
            ("tp_qd", self.tp_qd_max_rad_s, 0.01),
            ("host_position", self.host_position_error_m, 0.003),
            ("host_orientation", self.host_orientation_error_rad, 0.05),
            ("host_tcp_linear_speed", self.host_tcp_linear_speed_m_s, 0.001),
            ("host_tcp_angular_speed", self.host_tcp_angular_speed_rad_s, 0.01),
            ("host_qd", self.host_qd_max_rad_s, 0.01),
        )
        failures = [
            f"{name}_outside_limit" for name, value, limit in limits if value > limit
        ]
        if self.host_dwell_s < 0.5:
            failures.append("host_safe_dwell_short")
        for name, value in (
            ("trial_token", self.trial_token_match),
            ("capture_hashes", self.capture_hashes_complete),
            ("terminal_manifest", self.terminal_manifest_complete),
            ("fingerprint", self.fingerprint_closed),
        ):
            if not value:
                failures.append(f"{name}_not_closed")
        return tuple(failures)

    @property
    def returned_safe(self) -> bool:
        return not self.failures()

    def payload(self) -> dict[str, Any]:
        payload = {
            "schema": self.schema,
            "return_reference_uid": self.return_reference_uid,
            "return_reference_kind": self.return_reference_kind,
            "batch_row_index": self.batch_row_index,
            "tp_position_error_m": self.tp_position_error_m,
            "tp_orientation_error_rad": self.tp_orientation_error_rad,
            "tp_qd_max_rad_s": self.tp_qd_max_rad_s,
            "return_phase_echo": self.return_phase_echo,
            "return_segment_id": self.return_segment_id,
            "return_current_angular_speed_rad_s": self.return_current_angular_speed_rad_s,
            "return_current_angular_acceleration_rad_s2": self.return_current_angular_acceleration_rad_s2,
            "return_max_angular_speed_rad_s": self.return_max_angular_speed_rad_s,
            "return_max_angular_acceleration_rad_s2": self.return_max_angular_acceleration_rad_s2,
            "return_max_sample_gap_s": self.return_max_sample_gap_s,
            "host_position_error_m": self.host_position_error_m,
            "host_orientation_error_rad": self.host_orientation_error_rad,
            "host_tcp_linear_speed_m_s": self.host_tcp_linear_speed_m_s,
            "host_tcp_angular_speed_rad_s": self.host_tcp_angular_speed_rad_s,
            "host_qd_max_rad_s": self.host_qd_max_rad_s,
            "return_guard_mask": self.return_guard_mask,
            "safety_guards": dict(sorted(self.safety_guards.items())),
            "host_safety_mode": self.host_safety_mode,
            "host_dwell_s": self.host_dwell_s,
            "trial_token_match": self.trial_token_match,
            "capture_hashes_complete": self.capture_hashes_complete,
            "terminal_manifest_complete": self.terminal_manifest_complete,
            "fingerprint_closed": self.fingerprint_closed,
        }
        if self.protocol == "v3_full_home_rolling_arm_v1":
            payload["protocol"] = self.protocol
            payload["logical_batch_sequence"] = self.logical_batch_sequence
        return payload


@dataclass(frozen=True)
class DirectReadyClosureEvidence(TypedSafeClosureEvidence):
    """r006 exact terminal-ready closure without a post-return dwell gate."""

    schema: str = field(
        default="step5d.autotune/direct-ready-closure-v3",
        init=False,
    )

    def failures(self) -> tuple[str, ...]:
        return tuple(
            failure
            for failure in super().failures()
            if failure != "host_safe_dwell_short"
        )


ClosureEvidence = (
    SafeClosureEvidence | TypedSafeClosureEvidence | DirectReadyClosureEvidence
)


@dataclass(frozen=True)
class CaptureArtifactPaths:
    """Filesystem provenance kept outside every stable trial/capture identity."""

    csv_path: Path
    metadata_path: Path
    terminal_manifest_path: Path

    def __post_init__(self) -> None:
        for name in ("csv_path", "metadata_path", "terminal_manifest_path"):
            value = getattr(self, name)
            if not isinstance(value, Path):
                raise ValueError(f"{name} must be a pathlib.Path")

    def by_role(self) -> dict[str, Path]:
        return {
            "csv": self.csv_path,
            "metadata": self.metadata_path,
            "terminal_manifest": self.terminal_manifest_path,
        }


@dataclass(frozen=True)
class CaptureManifest:
    trial_uid: str
    backend_id: str
    source_fingerprint_pre: str
    source_fingerprint_post: str
    config_fingerprint_pre: str
    config_fingerprint_post: str
    candidate_token: int
    terminal_reason: int
    host_cause: str | None
    csv_sha256: str
    metadata_sha256: str
    terminal_manifest_sha256: str
    completion_marker: bool
    cadence_ok: bool
    feedback_fresh: bool
    rnn_oracle_aligned: bool
    safety_normal: bool
    returned_safe: bool
    immutable_bundle_written: bool
    stage25_complete_s: float
    safe_closure_evidence: ClosureEvidence | Mapping[str, Any]
    evidence: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for name, value in (
            ("trial_uid", self.trial_uid),
            ("source_fingerprint_pre", self.source_fingerprint_pre),
            ("source_fingerprint_post", self.source_fingerprint_post),
            ("config_fingerprint_pre", self.config_fingerprint_pre),
            ("config_fingerprint_post", self.config_fingerprint_post),
            ("csv_sha256", self.csv_sha256),
            ("metadata_sha256", self.metadata_sha256),
            ("terminal_manifest_sha256", self.terminal_manifest_sha256),
        ):
            require_sha256(name, value)
        if not isinstance(self.backend_id, str) or not self.backend_id.strip():
            raise ValueError("backend_id must be a non-empty string")
        _positive_int("candidate_token", self.candidate_token)
        _positive_int("terminal_reason", self.terminal_reason)
        if self.host_cause is not None and not isinstance(self.host_cause, str):
            raise ValueError("host_cause must be a string or None")
        for name in (
            "completion_marker",
            "cadence_ok",
            "feedback_fresh",
            "rnn_oracle_aligned",
            "safety_normal",
            "returned_safe",
            "immutable_bundle_written",
        ):
            _strict_bool(name, getattr(self, name))
        stage25_complete_s = _finite("stage25_complete_s", self.stage25_complete_s)
        if stage25_complete_s < 0.0:
            raise ValueError("stage25_complete_s must be non-negative")
        frozen_evidence = _canonical_frozen_mapping("evidence", self.evidence)
        closure = self.safe_closure_evidence
        if isinstance(closure, Mapping):
            try:
                closure_payload = dict(closure)
                closure_schema = closure_payload.pop("schema", None)
                if closure_schema == "step5d.autotune/typed-safe-closure-v2":
                    closure = TypedSafeClosureEvidence(**closure_payload)
                elif closure_schema == "step5d.autotune/direct-ready-closure-v3":
                    closure = DirectReadyClosureEvidence(**closure_payload)
                elif closure_schema is None:
                    closure = SafeClosureEvidence(**closure_payload)
                elif closure_schema == "step5d.autotune/typed-safe-closure-v1":
                    raise ValueError(
                        "typed-safe-closure-v1 lacks the required return telemetry"
                    )
                else:
                    raise ValueError(
                        f"unsupported safe closure schema: {closure_schema}"
                    )
            except (TypeError, ValueError) as exc:
                raise ValueError(f"safe_closure_evidence is invalid: {exc}") from exc
        if not isinstance(
            closure,
            (
                SafeClosureEvidence,
                TypedSafeClosureEvidence,
                DirectReadyClosureEvidence,
            ),
        ):
            raise ValueError("safe_closure_evidence has an unsupported closure schema")
        if self.returned_safe is not closure.returned_safe:
            raise ValueError("returned_safe must equal the detailed safe closure proof")
        if self.safety_normal is not (closure.host_safety_mode == "NORMAL"):
            raise ValueError("safety_normal must equal the host safety-mode proof")
        if self.fingerprint_closed is not closure.fingerprint_closed:
            raise ValueError("fingerprint closure claim differs from safe closure proof")
        if self.hashes_complete is not closure.capture_hashes_complete:
            raise ValueError("capture hash closure claim differs from safe closure proof")
        object.__setattr__(self, "safe_closure_evidence", closure)
        object.__setattr__(self, "stage25_complete_s", stage25_complete_s)
        object.__setattr__(self, "evidence", frozen_evidence)

    @property
    def fingerprint_closed(self) -> bool:
        return (
            self.source_fingerprint_pre == self.source_fingerprint_post
            and self.config_fingerprint_pre == self.config_fingerprint_post
        )

    @property
    def hashes_complete(self) -> bool:
        try:
            for name, value in (
                ("csv_sha256", self.csv_sha256),
                ("metadata_sha256", self.metadata_sha256),
                ("terminal_manifest_sha256", self.terminal_manifest_sha256),
            ):
                require_sha256(name, value)
        except ValueError:
            return False
        return True


@dataclass(frozen=True)
class Evaluation:
    trial_uid: str
    backend_id: str
    eligible: bool
    disposition: TrialDisposition
    objective_mae_n: float | None
    force_bias_n: float | None
    force_std_n: float | None
    coverage_12_plus_minus_1_ratio: float | None
    complete_bins: int
    safe_closure: bool
    structural_failures: tuple[str, ...] = ()
    metrics: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        require_sha256("trial_uid", self.trial_uid)
        if not isinstance(self.backend_id, str) or not self.backend_id.strip():
            raise ValueError("backend_id must be a non-empty string")
        _strict_bool("eligible", self.eligible)
        _strict_bool("safe_closure", self.safe_closure)
        if not isinstance(self.disposition, TrialDisposition):
            raise ValueError("disposition must be a TrialDisposition")
        _non_negative_int("complete_bins", self.complete_bins)
        if not isinstance(self.structural_failures, tuple) or any(
            not isinstance(item, str) or not item for item in self.structural_failures
        ):
            raise ValueError("structural_failures must be a tuple of non-empty strings")
        frozen_metrics = _canonical_frozen_mapping("metrics", self.metrics)

        finite_values: dict[str, float | None] = {}
        for name, value in (
            ("objective_mae_n", self.objective_mae_n),
            ("force_bias_n", self.force_bias_n),
            ("force_std_n", self.force_std_n),
            ("coverage_12_plus_minus_1_ratio", self.coverage_12_plus_minus_1_ratio),
        ):
            finite_values[name] = None if value is None else _finite(name, value)
        if finite_values["objective_mae_n"] is not None and finite_values["objective_mae_n"] < 0.0:
            raise ValueError("objective_mae_n must be non-negative")
        if finite_values["force_std_n"] is not None and finite_values["force_std_n"] < 0.0:
            raise ValueError("force_std_n must be non-negative")
        coverage = finite_values["coverage_12_plus_minus_1_ratio"]
        if coverage is not None and not 0.0 <= coverage <= 1.0:
            raise ValueError("coverage ratio must be in [0, 1]")
        if self.eligible:
            if any(value is None for value in finite_values.values()):
                raise ValueError("eligible evaluation requires complete finite objective metrics")
            if self.disposition is not TrialDisposition.OBJECTIVE:
                raise ValueError("eligible evaluation disposition must be objective")
            if self.complete_bins != 550:
                raise ValueError("eligible evaluation requires exactly 550 bins")
            if not self.safe_closure or self.structural_failures:
                raise ValueError("eligible evaluation requires safe closure and no structural failures")
        elif any(value is not None for value in finite_values.values()):
            raise ValueError("ineligible evaluation must not expose trainable objective metrics")
        for name, value in finite_values.items():
            object.__setattr__(self, name, value)
        object.__setattr__(self, "metrics", frozen_metrics)

    def history_payload(self) -> dict[str, Any]:
        payload = {
            "schema_version": SCHEMA_VERSION,
            "trial_uid": self.trial_uid,
            "backend_id": self.backend_id,
            "eligible": self.eligible,
            "disposition": self.disposition.value,
            "objective_mae_n": self.objective_mae_n,
            "force_bias_n": self.force_bias_n,
            "force_std_n": self.force_std_n,
            "coverage_12_plus_minus_1_ratio": self.coverage_12_plus_minus_1_ratio,
            "complete_bins": self.complete_bins,
            "safe_closure": self.safe_closure,
            "structural_failures": list(self.structural_failures),
            "metrics": dict(self.metrics),
        }
        canonical_json_bytes(payload)
        return payload


@dataclass(frozen=True)
class GovernorDecision:
    action: str
    layer: str
    from_profile_id: str
    to_profile_id: str | None
    keep: bool | None
    reason: str
    plant_epoch_before: int
    plant_epoch_after: int
    cooldown_eligible_trials: int = 3
    evidence: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for name in ("action", "layer", "from_profile_id", "reason"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} must be a non-empty string")
        if self.to_profile_id is not None and (
            not isinstance(self.to_profile_id, str) or not self.to_profile_id.strip()
        ):
            raise ValueError("to_profile_id must be a non-empty string or None")
        if self.keep is not None:
            _strict_bool("keep", self.keep)
        _positive_int("plant_epoch_before", self.plant_epoch_before)
        _positive_int("plant_epoch_after", self.plant_epoch_after)
        _non_negative_int("cooldown_eligible_trials", self.cooldown_eligible_trials)
        if not isinstance(self.evidence, Mapping):
            raise ValueError("governor evidence must be a mapping")
        canonical_json_bytes(dict(self.evidence))
