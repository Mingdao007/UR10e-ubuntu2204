"""Fresh-ledger R013 warm start, serial ask/tell, and restart/resume."""

from __future__ import annotations

import hashlib
import json
import math
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from scipy.stats import qmc

from .domain import (
    BASELINE_KI_MAX,
    EXTENDED_KI_MAX,
    KI_LATTICE_ANCHOR,
    candidate_supports_fixed_ki_seed,
    candidate_to_log_features,
    normalized_to_candidate,
    physical_candidate_key,
)
from .gp import (
    CANDIDATE_POOL_SIZE,
    CandidateProposal,
    ProductionGPConfig,
    ask_qlognei,
    ask_core_qlognei,
    fit_core_production_gp,
    fit_production_gp,
)
from .handoff import HandoffPolicy, handoff_policy_identity, validate_handoff_policy
from .bounded_bo import (
    BOUNDED_BO_ATTEMPT_BUDGET,
    BOUNDED_BO_POLICY,
    BOUNDED_BO_SCHEMA,
)
from .floor_coordinator import (
    CORE_BO_NOVEL,
    CORE_PROPOSAL_CONTRACT,
    CORRECTION_PROPOSAL_CONTRACT,
    FloorCandidateProposal,
    FloorCoordinatorError,
    FloorDiscoveryCoordinator,
    FloorDiscoveryPolicyV1,
    FloorTrialRequest,
    FloorTrialSpec,
    RuntimePrimitiveNotInstalled,
)
from .identity import CampaignFingerprint, validate_campaign_fingerprint
from .ledger import Ledger
from .runtime_strategy import (
    DISABLED_RUNTIME_STRATEGY,
    runtime_strategy_sha256,
    validate_runtime_strategy,
)


WARM_START_SEED = 6013
FIXED_KI_SEEDS = (KI_LATTICE_ANCHOR, 0.00256)
FIXED_KI_REPEATS = 3
CONFIRMATION_TARGET_SCHEMA = "step5d.autotune-v4/r013-confirmation-target-v1"
CONFIRMATION_TARGET_VERSION = 1
CONFIRMATION_THRESHOLD_N = 0.35
CONFIRMATION_MIN_REPEATS = 3
CONFIRMATION_KIND = "CONFIRMATION"
ANCHOR_RETEST_KIND = "ANCHOR_RETEST"
ANCHOR_RETEST_PLAN_SCHEMA = "step5d.autotune-v4/r013-anchor-retest-plan-v1"
ANCHOR_RETEST_PLAN_VERSION = 1
LOCAL_REFINEMENT_KIND = "LOCAL_REFINEMENT"
LOCAL_REFINEMENT_PLAN_SCHEMA = "step5d.autotune-v4/r013-local-refinement-plan-v1"
LOCAL_REFINEMENT_PLAN_VERSION = 1
HIGH_KI_PROBE_KIND = "HIGH_KI_PROBE"
HIGH_KI_PROBE_PLAN_SCHEMA = "step5d.autotune-v4/r013-high-ki-probe-plan-v1"
HIGH_KI_PROBE_PLAN_VERSION = 1
NORMAL_VELOCITY_GAIN_PROBE_KIND = "NORMAL_VELOCITY_GAIN_PROBE"
NORMAL_VELOCITY_GAIN_PROBE_PLAN_SCHEMA = (
    "step5d.autotune-v4/r013-normal-velocity-gain-probe-plan-v1"
)
NORMAL_VELOCITY_GAIN_PROBE_PLAN_VERSION = 1
NORMAL_VELOCITY_GAIN_PROBE_STOP_SCHEMA = (
    "step5d.autotune-v4/r013-normal-velocity-gain-probe-stop-v1"
)
NORMAL_VELOCITY_GAIN_PROBE_STOP_VERSION = 1
LOWER_P_OVER_D_PROBE_KIND = "LOWER_P_OVER_D_PROBE"
LOWER_P_OVER_D_PROBE_PLAN_SCHEMA = "step5d.autotune-v4/r013-lower-p-over-d-probe-plan-v1"
LOWER_P_OVER_D_PROBE_PLAN_VERSION = 1
STRATEGY_CANARY_KIND = "PHASE_STRATEGY_CANARY"
STRATEGY_CANARY_PLAN_SCHEMA = "step5d.autotune-v4/r013-phase-strategy-canary-plan-v1"
STRATEGY_CANARY_PLAN_VERSION = 1
STRATEGY_CANARY_REQUIRED_ADMITTED = 3
STRATEGY_CANARY_MAX_PHYSICAL_ATTEMPTS = 6
SOBOL_WARM_ROWS = 6
MIN_EXACT_ROWS_FOR_BO = 12
CANDIDATE_POOL_MAX_ROUNDS = 64
CANDIDATE_POOL_STATE_SCHEMA = "step5d.autotune-v4/r013-sobol-pool-state-v1"
COMPLETION_POLICY_SCHEMA = "step5d.autotune-v4/r013-completion-policy-v1"
LEGACY_THRESHOLD_COMPLETION_POLICY = "threshold_terminal_v0"
BUDGETED_FLOOR_V1 = "budgeted_floor_v1"
BUDGETED_FLOOR_NOVEL_TARGET = 200
COMPLETION_POLICY_VERSION = 1
NOVEL_EXCLUDED_KINDS = frozenset({
    "HANDOFF_A", "HANDOFF_B", "SENTINEL", "REPEAT", CONFIRMATION_KIND,
    STRATEGY_CANARY_KIND,
})
TRIAL_ANTI_WINDUP_SCHEMA = "step5d.autotune-v4/r013-trial-anti-windup-v1"
PHYSICAL_ADMISSION_SCHEMA = "step5d.autotune-v4/r013-physical-admission-v1"
REJECTED_ADMISSION_SCHEMA = "step5d.autotune-v4/r013-rejected-admission-v1"
ANTI_WINDUP_CONTRACT = {
    "schema": "step5d.integral-anti-windup/conditional-double-clamp-v1",
    "integral_state_limit_n_s": 1.0,
    "i_term_authority_error_n": 0.5,
    "back_calculation": False,
    "leaky_integration": False,
    "reset_boundaries": [
        "candidate_dispatch", "path_entry", "contact_loss", "abort", "home",
        "control_mode_exit", "invalid_state",
    ],
}

# Coordinates only: historical objective values and eligibility are never
# imported.  These exact lattice points cover the best same-metric R013
# neighborhoods that deterministic 128-point Sobol pools otherwise expose
# only at substantially later BO rounds.
ANCHOR_RETEST_CANDIDATES = (
    {
        "force_p_gain": 0.008000000000152198,
        "force_damping": 79.19595949289332,
        "force_i_gain": 0.003620386719675124,
        "i_off": False,
        "normal_filter_tau_s": 0.05202781128136904,
        "orientation_ko": 0.2,
        "motion_kp": 1.7838106725040817,
        "target_force_n": 5.0,
    },
    {
        "force_p_gain": 0.019027313840405524,
        "force_damping": 158.39191898578665,
        "force_i_gain": 0.0030443702144069664,
        "i_off": False,
        "normal_filter_tau_s": 0.04375,
        "orientation_ko": 0.05,
        "motion_kp": 1.5,
        "target_force_n": 5.0,
    },
    {
        "force_p_gain": 0.016000000000304396,
        "force_damping": 133.19119688030474,
        "force_i_gain": 0.003620386719675124,
        "i_off": False,
        "normal_filter_tau_s": 0.04375,
        "orientation_ko": 0.05,
        "motion_kp": 2.5226892457611436,
        "target_force_n": 5.0,
    },
)

# Fresh-only exploitation around the observed R013 boundary optimum.  These
# coordinates keep Ko/tau at their empirically preferred lower bounds while
# resolving the local P/D, Ki/P, damping, and motion-Kp neighborhood.
LOCAL_REFINEMENT_CANDIDATES = (
    {"force_p_gain": 0.013454342644315396, "force_damping": 112.0,
     "force_i_gain": 0.003620386719675124, "i_off": False,
     "normal_filter_tau_s": 0.04375, "orientation_ko": 0.05,
     "motion_kp": 1.5, "target_force_n": 5.0},
    {"force_p_gain": 0.016000000000304396, "force_damping": 133.19119688030474,
     "force_i_gain": 0.0030443702144069664, "i_off": False,
     "normal_filter_tau_s": 0.04375, "orientation_ko": 0.05,
     "motion_kp": 1.5, "target_force_n": 5.0},
    {"force_p_gain": 0.019027313840405524, "force_damping": 158.39191898578665,
     "force_i_gain": 0.003620386719675124, "i_off": False,
     "normal_filter_tau_s": 0.04375, "orientation_ko": 0.05,
     "motion_kp": 2.5226892457611436, "target_force_n": 5.0},
    {"force_p_gain": 0.0226274169984, "force_damping": 188.36079701683204,
     "force_i_gain": 0.003620386719675124, "i_off": False,
     "normal_filter_tau_s": 0.04375, "orientation_ko": 0.05,
     "motion_kp": 1.5, "target_force_n": 5.0},
    {"force_p_gain": 0.0226274169984, "force_damping": 188.36079701683204,
     "force_i_gain": 0.003620386719675124, "i_off": False,
     "normal_filter_tau_s": 0.04375, "orientation_ko": 0.05,
     "motion_kp": 2.5226892457611436, "target_force_n": 5.0},
    {"force_p_gain": 0.013454342644315396, "force_damping": 133.19119688030474,
     "force_i_gain": 0.003620386719675124, "i_off": False,
     "normal_filter_tau_s": 0.04375, "orientation_ko": 0.05,
     "motion_kp": 1.5, "target_force_n": 5.0},
)

# Bounded continuation after the original Ki ceiling saturated in both
# directions on the best fresh rows.  Every point stays on the executable
# quarter-octave lattice and below the fixed 0.5 * P I-term authority.
HIGH_KI_PROBE_CANDIDATES = tuple(
    {
        "force_p_gain": p,
        "force_damping": damping,
        "force_i_gain": ki,
        "i_off": False,
        "normal_filter_tau_s": 0.04375,
        "orientation_ko": 0.05,
        "motion_kp": kp,
        "target_force_n": 5.0,
    }
    for p, damping, kp, levels in (
        (0.019027313840405524, 158.39191898578665, 2.5226892457611436,
         (0.004305389646099019, 0.005120000000000001, 0.006088740428813933,
          0.007240773439350248, 0.008610779292198037)),
        (0.013454342644315396, 112.0, 1.5,
         (0.004305389646099019, 0.005120000000000001, 0.006088740428813933)),
    )
    for ki in levels
)

NORMAL_VELOCITY_GAIN_PROBE_CANDIDATES = (
    # Same I/D as the admitted high-Ki incumbent neighborhood, increasing P/D.
    {"force_p_gain": 0.019027313840405524, "force_damping": 133.19119688030474,
     "force_i_gain": 0.005120000000000001, "i_off": False,
     "normal_filter_tau_s": 0.04375, "orientation_ko": 0.05,
     "motion_kp": 2.5226892457611436, "target_force_n": 5.0},
    {"force_p_gain": 0.019027313840405524, "force_damping": 112.0,
     "force_i_gain": 0.004305389646099019, "i_off": False,
     "normal_filter_tau_s": 0.04375, "orientation_ko": 0.05,
     "motion_kp": 2.5226892457611436, "target_force_n": 5.0},
    {"force_p_gain": 0.019027313840405524, "force_damping": 94.18039850841602,
     "force_i_gain": 0.003620386719675124, "i_off": False,
     "normal_filter_tau_s": 0.04375, "orientation_ko": 0.05,
     "motion_kp": 2.5226892457611436, "target_force_n": 5.0},
    # Paired higher I/D values at the two most useful P/D levels.
    {"force_p_gain": 0.019027313840405524, "force_damping": 133.19119688030474,
     "force_i_gain": 0.006088740428813933, "i_off": False,
     "normal_filter_tau_s": 0.04375, "orientation_ko": 0.05,
     "motion_kp": 2.5226892457611436, "target_force_n": 5.0},
    {"force_p_gain": 0.019027313840405524, "force_damping": 133.19119688030474,
     "force_i_gain": 0.007240773439350248, "i_off": False,
     "normal_filter_tau_s": 0.04375, "orientation_ko": 0.05,
     "motion_kp": 2.5226892457611436, "target_force_n": 5.0},
    {"force_p_gain": 0.019027313840405524, "force_damping": 112.0,
     "force_i_gain": 0.005120000000000001, "i_off": False,
     "normal_filter_tau_s": 0.04375, "orientation_ko": 0.05,
     "motion_kp": 2.5226892457611436, "target_force_n": 5.0},
)

LOWER_P_OVER_D_PROBE_CANDIDATES = (
    # Hold I/D at the admitted high-Ki best while reducing P/D by lattice steps.
    {"force_p_gain": 0.019027313840405524, "force_damping": 188.36079701683204,
     "force_i_gain": 0.007240773439350248, "i_off": False,
     "normal_filter_tau_s": 0.04375, "orientation_ko": 0.05,
     "motion_kp": 2.5226892457611436, "target_force_n": 5.0},
    {"force_p_gain": 0.019027313840405524, "force_damping": 224.0,
     "force_i_gain": 0.008610779292198037, "i_off": False,
     "normal_filter_tau_s": 0.04375, "orientation_ko": 0.05,
     "motion_kp": 2.5226892457611436, "target_force_n": 5.0},
    # At the nearer lower P/D, raise I/D by one quarter-octave.
    {"force_p_gain": 0.019027313840405524, "force_damping": 188.36079701683204,
     "force_i_gain": 0.008610779292198037, "i_off": False,
     "normal_filter_tau_s": 0.04375, "orientation_ko": 0.05,
     "motion_kp": 2.5226892457611436, "target_force_n": 5.0},
)

STRATEGY_CANARY_CANDIDATE = {
    "force_p_gain": 0.019027313840405524,
    "force_damping": 188.36079701683204,
    "force_i_gain": 0.008610779292198037,
    "i_off": False,
    "normal_filter_tau_s": 0.04375,
    "orientation_ko": 0.05,
    "motion_kp": 2.5226892457611436,
    "target_force_n": 5.0,
}


class R013CampaignError(ValueError):
    """R013 scheduling, lineage, or serial identity is invalid."""


def _strict_hash(value: Mapping[str, Any]) -> str:
    payload = json.dumps(dict(value), sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _fingerprint_payload(value: CampaignFingerprint | Mapping[str, Any] | None) -> dict[str, Any] | None:
    if value is None:
        return None
    return validate_campaign_fingerprint(value).as_dict()


def candidate_token(candidate: Mapping[str, Any]) -> str:
    physical_candidate_key(candidate)
    return _strict_hash({"schema": "r013-physical-candidate-v1", "candidate": dict(candidate)})


def anchor_retest_plan() -> dict[str, Any]:
    """Build the one reviewed, coordinates-only strategy extension."""

    for candidate in ANCHOR_RETEST_CANDIDATES:
        physical_candidate_key(candidate)
    return {
        "schema": ANCHOR_RETEST_PLAN_SCHEMA,
        "version": ANCHOR_RETEST_PLAN_VERSION,
        "policy": "one_fresh_admitted_observation_per_anchor_before_bo",
        "source_semantics": "coordinates_only_no_historical_objectives",
        "historical_objectives_imported": False,
        "candidates": [dict(candidate) for candidate in ANCHOR_RETEST_CANDIDATES],
    }


def _validated_anchor_retest_candidates(value: Mapping[str, Any]) -> tuple[dict[str, Any], ...]:
    expected = anchor_retest_plan()
    if not isinstance(value, Mapping) or set(value) != set(expected):
        raise R013CampaignError("R013 anchor retest plan fields differ")
    if value.get("schema") != ANCHOR_RETEST_PLAN_SCHEMA:
        raise R013CampaignError("R013 anchor retest plan schema differs")
    if value.get("version") != ANCHOR_RETEST_PLAN_VERSION:
        raise R013CampaignError("R013 anchor retest plan version differs")
    if value.get("historical_objectives_imported") is not False:
        raise R013CampaignError("R013 anchor plan must not import historical objectives")
    if value.get("policy") != expected["policy"] or value.get("source_semantics") != expected["source_semantics"]:
        raise R013CampaignError("R013 anchor retest semantics differ")
    candidates = value.get("candidates")
    if not isinstance(candidates, Sequence) or isinstance(candidates, (str, bytes)):
        raise R013CampaignError("R013 anchor retest candidates are invalid")
    parsed = tuple(dict(candidate) for candidate in candidates if isinstance(candidate, Mapping))
    if len(parsed) != len(candidates) or tuple(
        physical_candidate_key(candidate) for candidate in parsed
    ) != tuple(physical_candidate_key(candidate) for candidate in ANCHOR_RETEST_CANDIDATES):
        raise R013CampaignError("R013 anchor retest coordinates differ")
    return parsed


def local_refinement_plan() -> dict[str, Any]:
    for candidate in LOCAL_REFINEMENT_CANDIDATES:
        physical_candidate_key(candidate)
    return {
        "schema": LOCAL_REFINEMENT_PLAN_SCHEMA,
        "version": LOCAL_REFINEMENT_PLAN_VERSION,
        "policy": "one_fresh_admitted_observation_per_local_candidate_before_bo",
        "source_semantics": "current_campaign_coordinates_only_no_objective_import",
        "historical_objectives_imported": False,
        "candidates": [dict(candidate) for candidate in LOCAL_REFINEMENT_CANDIDATES],
    }


def _validated_local_refinement_candidates(
    value: Mapping[str, Any],
) -> tuple[dict[str, Any], ...]:
    expected = local_refinement_plan()
    if not isinstance(value, Mapping) or set(value) != set(expected):
        raise R013CampaignError("R013 local refinement plan fields differ")
    if value.get("schema") != LOCAL_REFINEMENT_PLAN_SCHEMA:
        raise R013CampaignError("R013 local refinement plan schema differs")
    if value.get("version") != LOCAL_REFINEMENT_PLAN_VERSION:
        raise R013CampaignError("R013 local refinement plan version differs")
    if value.get("historical_objectives_imported") is not False:
        raise R013CampaignError("R013 local refinement must not import objectives")
    if value.get("policy") != expected["policy"] or value.get("source_semantics") != expected["source_semantics"]:
        raise R013CampaignError("R013 local refinement semantics differ")
    candidates = value.get("candidates")
    if not isinstance(candidates, Sequence) or isinstance(candidates, (str, bytes)):
        raise R013CampaignError("R013 local refinement candidates are invalid")
    parsed = tuple(dict(candidate) for candidate in candidates if isinstance(candidate, Mapping))
    if len(parsed) != len(candidates) or tuple(
        physical_candidate_key(candidate) for candidate in parsed
    ) != tuple(physical_candidate_key(candidate) for candidate in LOCAL_REFINEMENT_CANDIDATES):
        raise R013CampaignError("R013 local refinement coordinates differ")
    return parsed


def high_ki_probe_plan() -> dict[str, Any]:
    for candidate in HIGH_KI_PROBE_CANDIDATES:
        physical_candidate_key(candidate)
        if float(candidate["force_i_gain"]) > 0.5 * float(candidate["force_p_gain"]):
            raise R013CampaignError("R013 high-Ki probe exceeds fixed I-term authority")
    return {
        "schema": HIGH_KI_PROBE_PLAN_SCHEMA,
        "version": HIGH_KI_PROBE_PLAN_VERSION,
        "policy": "one_fresh_admitted_observation_per_high_ki_candidate_before_bo",
        "source_semantics": "current_campaign_saturation_evidence_no_objective_import",
        "historical_objectives_imported": False,
        "baseline_ki_max": BASELINE_KI_MAX,
        "extended_ki_max": EXTENDED_KI_MAX,
        "i_term_authority_fraction_max": 0.5,
        "candidates": [dict(candidate) for candidate in HIGH_KI_PROBE_CANDIDATES],
    }


def _validated_high_ki_probe_candidates(
    value: Mapping[str, Any],
) -> tuple[dict[str, Any], ...]:
    expected = high_ki_probe_plan()
    if type(value) is not dict or set(value) != set(expected):
        raise R013CampaignError("R013 high-Ki probe plan fields differ")
    for key, expected_value in expected.items():
        if key == "candidates":
            continue
        actual_value = value.get(key)
        if type(actual_value) is not type(expected_value) or actual_value != expected_value:
            raise R013CampaignError(f"R013 high-Ki probe plan {key} differs")
    candidates = value.get("candidates")
    if type(candidates) is not list:
        raise R013CampaignError("R013 high-Ki probe candidates are invalid")
    if len(candidates) != len(HIGH_KI_PROBE_CANDIDATES):
        raise R013CampaignError("R013 high-Ki probe candidate count differs")
    parsed: list[dict[str, Any]] = []
    for candidate, expected_candidate in zip(
        candidates,
        HIGH_KI_PROBE_CANDIDATES,
        strict=True,
    ):
        if type(candidate) is not dict or set(candidate) != set(expected_candidate):
            raise R013CampaignError("R013 high-Ki probe candidate fields differ")
        for key, expected_value in expected_candidate.items():
            actual_value = candidate.get(key)
            if type(actual_value) is not type(expected_value) or actual_value != expected_value:
                raise R013CampaignError("R013 high-Ki probe candidate values differ")
        physical_candidate_key(candidate)
        parsed.append(dict(candidate))
    return tuple(parsed)


def normal_velocity_gain_probe_plan() -> dict[str, Any]:
    for candidate in NORMAL_VELOCITY_GAIN_PROBE_CANDIDATES:
        physical_candidate_key(candidate)
        if float(candidate["force_i_gain"]) > 0.5 * float(candidate["force_p_gain"]):
            raise R013CampaignError("R013 normal-velocity probe exceeds I-term authority")
    return {
        "schema": NORMAL_VELOCITY_GAIN_PROBE_PLAN_SCHEMA,
        "version": NORMAL_VELOCITY_GAIN_PROBE_PLAN_VERSION,
        "policy": "matched_i_over_d_then_paired_i_over_d_before_bo",
        "source_semantics": "current_campaign_phase_diagnosis_no_objective_import",
        "historical_objectives_imported": False,
        "fixed_tau_s": 0.04375,
        "fixed_orientation_ko": 0.05,
        "fixed_motion_kp": 2.5226892457611436,
        "i_term_authority_fraction_max": 0.5,
        "candidates": [dict(candidate) for candidate in NORMAL_VELOCITY_GAIN_PROBE_CANDIDATES],
    }


def _validated_normal_velocity_gain_probe_candidates(
    value: Mapping[str, Any],
) -> tuple[dict[str, Any], ...]:
    expected = normal_velocity_gain_probe_plan()
    if type(value) is not dict or set(value) != set(expected):
        raise R013CampaignError("R013 normal-velocity probe plan fields differ")
    for key, expected_value in expected.items():
        if key == "candidates":
            continue
        actual_value = value.get(key)
        if type(actual_value) is not type(expected_value) or actual_value != expected_value:
            raise R013CampaignError(f"R013 normal-velocity probe plan {key} differs")
    candidates = value.get("candidates")
    if type(candidates) is not list:
        raise R013CampaignError("R013 normal-velocity probe candidates are invalid")
    if len(candidates) != len(NORMAL_VELOCITY_GAIN_PROBE_CANDIDATES):
        raise R013CampaignError("R013 normal-velocity probe candidate count differs")
    parsed: list[dict[str, Any]] = []
    for candidate, expected_candidate in zip(
        candidates,
        NORMAL_VELOCITY_GAIN_PROBE_CANDIDATES,
        strict=True,
    ):
        if type(candidate) is not dict or set(candidate) != set(expected_candidate):
            raise R013CampaignError("R013 normal-velocity probe candidate fields differ")
        for key, expected_value in expected_candidate.items():
            actual_value = candidate.get(key)
            if type(actual_value) is not type(expected_value) or actual_value != expected_value:
                raise R013CampaignError("R013 normal-velocity probe candidate values differ")
        physical_candidate_key(candidate)
        parsed.append(dict(candidate))
    return tuple(parsed)


def lower_p_over_d_probe_plan() -> dict[str, Any]:
    for candidate in LOWER_P_OVER_D_PROBE_CANDIDATES:
        physical_candidate_key(candidate)
        if float(candidate["force_i_gain"]) > 0.5 * float(candidate["force_p_gain"]):
            raise R013CampaignError("R013 lower-P/D probe exceeds I-term authority")
    return {
        "schema": LOWER_P_OVER_D_PROBE_PLAN_SCHEMA,
        "version": LOWER_P_OVER_D_PROBE_PLAN_VERSION,
        "policy": "two_matched_i_over_d_lower_p_over_d_then_one_higher_i_over_d_before_bo",
        "source_semantics": "current_campaign_symmetric_velocity_gain_diagnosis_no_objective_import",
        "historical_objectives_imported": False,
        "fixed_force_p_gain": 0.019027313840405524,
        "fixed_tau_s": 0.04375,
        "fixed_orientation_ko": 0.05,
        "fixed_motion_kp": 2.5226892457611436,
        "i_term_authority_fraction_max": 0.5,
        "candidates": [dict(candidate) for candidate in LOWER_P_OVER_D_PROBE_CANDIDATES],
    }


def _validated_lower_p_over_d_probe_candidates(
    value: Mapping[str, Any],
) -> tuple[dict[str, Any], ...]:
    expected = lower_p_over_d_probe_plan()
    if type(value) is not dict or set(value) != set(expected):
        raise R013CampaignError("R013 lower-P/D probe plan fields differ")
    for key, expected_value in expected.items():
        if key == "candidates":
            continue
        actual_value = value.get(key)
        if type(actual_value) is not type(expected_value) or actual_value != expected_value:
            raise R013CampaignError(f"R013 lower-P/D probe plan {key} differs")
    candidates = value.get("candidates")
    if type(candidates) is not list:
        raise R013CampaignError("R013 lower-P/D probe candidates are invalid")
    if len(candidates) != len(LOWER_P_OVER_D_PROBE_CANDIDATES):
        raise R013CampaignError("R013 lower-P/D probe candidate count differs")
    parsed: list[dict[str, Any]] = []
    for candidate, expected_candidate in zip(
        candidates,
        LOWER_P_OVER_D_PROBE_CANDIDATES,
        strict=True,
    ):
        if type(candidate) is not dict or set(candidate) != set(expected_candidate):
            raise R013CampaignError("R013 lower-P/D probe candidate fields differ")
        for key, expected_value in expected_candidate.items():
            actual_value = candidate.get(key)
            if type(actual_value) is not type(expected_value) or actual_value != expected_value:
                raise R013CampaignError("R013 lower-P/D probe candidate values differ")
        physical_candidate_key(candidate)
        parsed.append(dict(candidate))
    return tuple(parsed)


def strategy_canary_plan(*, runtime_strategy_sha256_value: str) -> dict[str, Any]:
    if (
        type(runtime_strategy_sha256_value) is not str
        or len(runtime_strategy_sha256_value) != 64
        or any(c not in "0123456789abcdef" for c in runtime_strategy_sha256_value)
    ):
        raise R013CampaignError("R013 strategy canary runtime identity differs")
    physical_candidate_key(STRATEGY_CANARY_CANDIDATE)
    return {
        "schema": STRATEGY_CANARY_PLAN_SCHEMA,
        "version": STRATEGY_CANARY_PLAN_VERSION,
        "policy": "same_candidate_three_admitted_before_any_warm_or_bo",
        "runtime_strategy_sha256": runtime_strategy_sha256_value,
        "required_admitted_rows": STRATEGY_CANARY_REQUIRED_ADMITTED,
        "maximum_physical_attempts": STRATEGY_CANARY_MAX_PHYSICAL_ATTEMPTS,
        "rejected_attempts_consume_limit": True,
        "confirmation_attempts_consume_limit": True,
        "historical_objectives_imported": False,
        "candidate": dict(STRATEGY_CANARY_CANDIDATE),
    }


def _validated_strategy_canary_candidate(
    value: Mapping[str, Any],
    *,
    expected_strategy_sha256: str,
) -> dict[str, Any]:
    expected = strategy_canary_plan(
        runtime_strategy_sha256_value=expected_strategy_sha256
    )
    if type(value) is not dict or set(value) != set(expected):
        raise R013CampaignError("R013 strategy canary plan fields differ")
    for key, expected_value in expected.items():
        actual_value = value.get(key)
        if key == "candidate":
            if type(actual_value) is not dict or set(actual_value) != set(expected_value):
                raise R013CampaignError("R013 strategy canary candidate fields differ")
            if any(
                type(actual_value[name]) is not type(item)
                or actual_value[name] != item
                for name, item in expected_value.items()
            ):
                raise R013CampaignError("R013 strategy canary candidate values differ")
        elif type(actual_value) is not type(expected_value) or actual_value != expected_value:
            raise R013CampaignError(f"R013 strategy canary plan {key} differs")
    physical_candidate_key(value["candidate"])
    return dict(value["candidate"])


def _candidate_from_row(value: Mapping[str, Any]) -> Mapping[str, Any] | None:
    candidate = value.get("candidate")
    return candidate if isinstance(candidate, Mapping) else None


def _supports_warm_seeds(candidate: Mapping[str, Any]) -> bool:
    return all(candidate_supports_fixed_ki_seed(candidate, ki) for ki in FIXED_KI_SEEDS)


def select_seed_template(r012_snapshot: Mapping[str, Any]) -> dict[str, Any]:
    """Read only confirmed R012 candidate parameters; never import observations."""

    latest = r012_snapshot.get("confirmed_incumbent")
    if isinstance(latest, Mapping):
        candidate = _candidate_from_row(latest)
        confirmed = latest.get("confirmed", True) is True
        if candidate is not None and confirmed and _supports_warm_seeds(candidate):
            return dict(candidate)
    candidates = r012_snapshot.get("confirmed_candidates", ())
    if not isinstance(candidates, Sequence) or isinstance(candidates, (str, bytes)):
        raise R013CampaignError("R012 confirmed candidate fallback is invalid")
    eligible: list[tuple[float, Mapping[str, Any]]] = []
    for row in candidates:
        if not isinstance(row, Mapping) or row.get("confirmed") is not True:
            continue
        candidate = _candidate_from_row(row)
        if candidate is None or not _supports_warm_seeds(candidate):
            continue
        mean = row.get("arithmetic_mean_sealed_mae_n", row.get("mean_mae_n"))
        try:
            parsed_mean = float(mean)
        except (TypeError, ValueError):
            continue
        if math.isfinite(parsed_mean) and parsed_mean >= 0.0:
            eligible.append((parsed_mean, candidate))
    if not eligible:
        raise R013CampaignError(
            "no confirmed R012 candidate can host both fixed R013 Ki seeds"
        )
    return dict(min(eligible, key=lambda item: (item[0], repr(physical_candidate_key({**dict(item[1]), "force_i_gain": FIXED_KI_SEEDS[0], "i_off": False}))))[1])


def r012_seed_source_from_ledger(path: Path) -> dict[str, Any]:
    """Reduce a sealed R012 ledger to confirmed 5D candidate summaries only."""

    grouped: dict[tuple[Any, ...], list[float]] = defaultdict(list)
    candidates: dict[tuple[Any, ...], Mapping[str, Any]] = {}
    with Path(path).open("r", encoding="utf-8") as stream:
        for line_number, line in enumerate(stream, 1):
            row = json.loads(line)
            if row.get("schema") != "step5d.autotune-v4/r012-ledger-v2":
                raise R013CampaignError(
                    f"R013 seed source contains non-R012 ledger row {line_number}"
                )
            if row.get("record_type") != "observation":
                continue
            observation = row.get("observation")
            if not isinstance(observation, Mapping):
                raise R013CampaignError("R012 seed observation payload is invalid")
            if observation.get("schema") != "step5d.autotune-v4/r012-exact-observation-v1":
                continue
            if not all(
                observation.get(name) is True
                for name in ("completed", "sealed", "full_observation", "eligible")
            ):
                continue
            candidate = observation.get("candidate")
            if not isinstance(candidate, Mapping):
                raise R013CampaignError("R012 seed candidate is invalid")
            key = tuple(
                candidate.get(name)
                for name in (
                    "force_p_gain", "force_damping", "force_i_gain", "i_off",
                    "normal_filter_tau_s", "orientation_ko", "motion_kp", "target_force_n",
                )
            )
            objective = float(observation.get("objective_n"))
            if not math.isfinite(objective) or objective < 0.0:
                raise R013CampaignError("R012 seed objective is invalid")
            grouped[key].append(objective)
            candidates[key] = dict(candidate)
    confirmed = [
        {
            "candidate": dict(candidates[key]),
            "confirmed": True,
            "exact_count": len(values),
            "arithmetic_mean_sealed_mae_n": math.fsum(values) / len(values),
        }
        for key, values in grouped.items()
        if len(values) >= 3
    ]
    if not confirmed:
        raise R013CampaignError("R012 seed ledger has no confirmed candidate")
    incumbent = min(
        confirmed,
        key=lambda row: (
            float(row["arithmetic_mean_sealed_mae_n"]),
            repr(row["candidate"]),
        ),
    )
    return {
        "schema": "step5d.autotune-v4/r013-r012-seed-source-v1",
        "source_ledger": str(Path(path).resolve()),
        "confirmed_incumbent": incumbent,
        "confirmed_candidates": confirmed,
        "observation_rows_exported_to_r013": 0,
    }


def _sobol_candidates(*, seed: int, count: int) -> tuple[dict[str, Any], ...]:
    engine = qmc.Sobol(d=6, scramble=True, seed=seed)
    points = engine.random_base2(m=14)
    candidates: list[dict[str, Any]] = []
    keys: set[tuple[Any, ...]] = set()
    for point in points:
        try:
            candidate = normalized_to_candidate(tuple(float(value) for value in point), snap=True)
            key = physical_candidate_key(candidate)
        except ValueError:
            continue
        if key in keys:
            continue
        keys.add(key)
        candidates.append(candidate)
        if len(candidates) == count:
            return tuple(candidates)
    raise R013CampaignError(f"Sobol lattice projection produced only {len(candidates)} of {count} candidates")


def warm_start_candidates(seed_template: Mapping[str, Any]) -> tuple[dict[str, Any], ...]:
    fixed = tuple(
        {
            **dict(seed_template),
            "force_i_gain": float(ki),
            "i_off": False,
            "target_force_n": float(seed_template.get("target_force_n", 5.0)),
        }
        for ki in FIXED_KI_SEEDS
        for _repeat in range(FIXED_KI_REPEATS)
    )
    for candidate in fixed:
        physical_candidate_key(candidate)
    sobol = _sobol_candidates(seed=WARM_START_SEED, count=SOBOL_WARM_ROWS)
    result = fixed + sobol
    if len(result) != MIN_EXACT_ROWS_FOR_BO:
        raise R013CampaignError("R013 warm start must contain exactly 12 rows")
    return result


def candidate_pool(
    *,
    round_index: int,
    evaluated_keys: Sequence[tuple[Any, ...]] = (),
    pending_keys: Sequence[tuple[Any, ...]] = (),
) -> tuple[dict[str, Any], ...]:
    if isinstance(round_index, bool) or round_index < 0:
        raise R013CampaignError("R013 BO round index must be nonnegative")
    excluded = {tuple(key) for key in (*evaluated_keys, *pending_keys)}
    selected: list[dict[str, Any]] = []
    selected_keys: set[tuple[Any, ...]] = set(excluded)
    for offset in range(CANDIDATE_POOL_MAX_ROUNDS):
        candidates = _sobol_candidates(
            seed=WARM_START_SEED + 1 + int(round_index) + offset,
            count=CANDIDATE_POOL_SIZE,
        )
        for candidate in candidates:
            key = physical_candidate_key(candidate)
            if key in selected_keys:
                continue
            selected_keys.add(key)
            selected.append(candidate)
            if len(selected) == CANDIDATE_POOL_SIZE:
                return tuple(selected)
    raise R013CampaignError(
        "R013 candidate pool exhausted after "
        f"{CANDIDATE_POOL_MAX_ROUNDS} deterministic Sobol rounds; "
        f"found {len(selected)} of {CANDIDATE_POOL_SIZE} admissible candidates"
    )


@dataclass(frozen=True)
class ConfirmationTarget:
    """The persisted, typed R013 target and confirmation policy."""

    sealed_mae_threshold_n: float = CONFIRMATION_THRESHOLD_N
    minimum_admitted_repeats: int = CONFIRMATION_MIN_REPEATS
    schema: str = CONFIRMATION_TARGET_SCHEMA
    version: int = CONFIRMATION_TARGET_VERSION
    trigger_operator: str = "<="
    aggregation: str = "arithmetic_mean"
    confirmation_kind: str = CONFIRMATION_KIND
    target_status: str = "target_achieved"

    def __post_init__(self) -> None:
        if self.schema != CONFIRMATION_TARGET_SCHEMA:
            raise R013CampaignError("R013 confirmation target schema differs")
        if (
            isinstance(self.version, bool)
            or not isinstance(self.version, int)
            or self.version != CONFIRMATION_TARGET_VERSION
        ):
            raise R013CampaignError("R013 confirmation target version differs")
        if not isinstance(self.sealed_mae_threshold_n, (int, float)):
            raise R013CampaignError("R013 confirmation threshold must be numeric")
        try:
            threshold = float(self.sealed_mae_threshold_n)
        except (TypeError, ValueError) as exc:
            raise R013CampaignError("R013 confirmation threshold must be numeric") from exc
        if not math.isfinite(threshold) or not 0.0 <= threshold <= 1.0:
            raise R013CampaignError("R013 confirmation threshold is outside [0,1] N")
        if threshold != CONFIRMATION_THRESHOLD_N:
            raise R013CampaignError("R013 confirmation threshold must be exactly 0.35 N")
        if (
            isinstance(self.minimum_admitted_repeats, bool)
            or not isinstance(self.minimum_admitted_repeats, int)
            or not 1 <= self.minimum_admitted_repeats <= 32
        ):
            raise R013CampaignError("R013 confirmation repeat count is outside [1,32]")
        if self.minimum_admitted_repeats != CONFIRMATION_MIN_REPEATS:
            raise R013CampaignError("R013 confirmation repeat count must be exactly 3")
        if self.trigger_operator != "<=" or self.aggregation != "arithmetic_mean":
            raise R013CampaignError("R013 confirmation reduction semantics differ")
        if self.confirmation_kind != CONFIRMATION_KIND:
            raise R013CampaignError("R013 confirmation dispatch kind differs")
        if self.target_status != "target_achieved":
            raise R013CampaignError("R013 target status differs")
        object.__setattr__(self, "sealed_mae_threshold_n", threshold)

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any] | None) -> "ConfirmationTarget":
        if value is None:
            return cls()
        if not isinstance(value, Mapping):
            raise R013CampaignError("R013 confirmation target snapshot is invalid")
        required = {
            "schema", "version", "sealed_mae_threshold_n", "minimum_admitted_repeats",
            "trigger_operator", "aggregation", "confirmation_kind", "target_status",
        }
        if not required.issubset(value):
            raise R013CampaignError("R013 confirmation target snapshot is incomplete")
        return cls(
            sealed_mae_threshold_n=value["sealed_mae_threshold_n"],
            minimum_admitted_repeats=value["minimum_admitted_repeats"],
            schema=str(value["schema"]),
            version=value["version"],
            trigger_operator=str(value["trigger_operator"]),
            aggregation=str(value["aggregation"]),
            confirmation_kind=str(value["confirmation_kind"]),
            target_status=str(value["target_status"]),
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "version": self.version,
            "sealed_mae_threshold_n": self.sealed_mae_threshold_n,
            "minimum_admitted_repeats": self.minimum_admitted_repeats,
            "trigger_operator": self.trigger_operator,
            "aggregation": self.aggregation,
            "confirmation_kind": self.confirmation_kind,
            "target_status": self.target_status,
        }


DEFAULT_CONFIRMATION_TARGET = ConfirmationTarget()


@dataclass(frozen=True)
class CompletionPolicy:
    """Typed terminal policy; the threshold policy remains legacy-only."""

    policy: str = LEGACY_THRESHOLD_COMPLETION_POLICY
    version: int = COMPLETION_POLICY_VERSION
    schema: str = COMPLETION_POLICY_SCHEMA
    target_mae_n: float = CONFIRMATION_THRESHOLD_N
    novel_exact_candidate_target: int = BUDGETED_FLOOR_NOVEL_TARGET
    checkpoint_only_target: bool = False

    def __post_init__(self) -> None:
        if (
            self.schema != COMPLETION_POLICY_SCHEMA
            or type(self.version) is not int
            or self.version != COMPLETION_POLICY_VERSION
        ):
            raise R013CampaignError("R013 completion policy schema/version differs")
        if self.policy not in {LEGACY_THRESHOLD_COMPLETION_POLICY, BUDGETED_FLOOR_V1}:
            raise R013CampaignError("R013 completion policy differs")
        try:
            target = float(self.target_mae_n)
        except (TypeError, ValueError) as exc:
            raise R013CampaignError("R013 completion target is invalid") from exc
        if not math.isfinite(target) or target != CONFIRMATION_THRESHOLD_N:
            raise R013CampaignError("R013 completion target must be exactly 0.35 N")
        if (
            isinstance(self.novel_exact_candidate_target, bool)
            or not isinstance(self.novel_exact_candidate_target, int)
            or self.novel_exact_candidate_target <= 0
        ):
            raise R013CampaignError("R013 novel exact target is invalid")
        if not isinstance(self.checkpoint_only_target, bool):
            raise R013CampaignError("R013 completion checkpoint flag is invalid")
        if self.policy == BUDGETED_FLOOR_V1 and not self.checkpoint_only_target:
            raise R013CampaignError("R013 budgeted floor target must be checkpoint-only")
        if self.policy == LEGACY_THRESHOLD_COMPLETION_POLICY and self.checkpoint_only_target:
            raise R013CampaignError("R013 legacy threshold target cannot be checkpoint-only")

    @classmethod
    def from_value(cls, value: Mapping[str, Any] | str | None) -> "CompletionPolicy":
        if value is None:
            return cls()
        if isinstance(value, CompletionPolicy):
            return value
        if isinstance(value, str):
            if value == BUDGETED_FLOOR_V1:
                return cls(policy=value, checkpoint_only_target=True)
            return cls(policy=value)
        if not isinstance(value, Mapping):
            raise R013CampaignError("R013 completion policy must be typed")
        required = {
            "schema", "version", "policy", "target_mae_n",
            "novel_exact_candidate_target", "checkpoint_only_target",
        }
        if set(value) != required:
            raise R013CampaignError("R013 completion policy fields differ")
        return cls(
            policy=value["policy"],
            version=value["version"],
            schema=value["schema"],
            target_mae_n=value["target_mae_n"],
            novel_exact_candidate_target=value["novel_exact_candidate_target"],
            checkpoint_only_target=value["checkpoint_only_target"],
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "version": self.version,
            "policy": self.policy,
            "target_mae_n": self.target_mae_n,
            "novel_exact_candidate_target": self.novel_exact_candidate_target,
            "checkpoint_only_target": self.checkpoint_only_target,
        }


def _validate_bounded_bo_profile(
    value: Mapping[str, Any] | None,
) -> dict[str, Any] | None:
    """Validate the persisted bounded policy without changing legacy semantics."""

    if value is None:
        return None
    required = {
        "schema", "version", "policy", "physical_attempt_budget", "target_mae_n",
        "checkpoint_only_target", "seed_receipt_sha256", "runtime_strategy_sha256",
        "source_closure_sha256", "controller_triplet_sha256",
    }
    if not isinstance(value, Mapping) or set(value) != required:
        raise R013CampaignError("R013 bounded BO profile fields differ")
    if value.get("schema") != BOUNDED_BO_SCHEMA or value.get("version") != 1:
        raise R013CampaignError("R013 bounded BO profile schema/version differs")
    if value.get("policy") != BOUNDED_BO_POLICY:
        raise R013CampaignError("R013 bounded BO policy differs")
    budget = value.get("physical_attempt_budget")
    if budget != BOUNDED_BO_ATTEMPT_BUDGET:
        raise R013CampaignError(
            "R013 bounded BO physical attempt budget must be exactly 100"
        )
    try:
        target = float(value.get("target_mae_n"))
    except (TypeError, ValueError) as exc:
        raise R013CampaignError("R013 bounded BO target is invalid") from exc
    if not math.isfinite(target) or target != CONFIRMATION_THRESHOLD_N:
        raise R013CampaignError("R013 bounded BO target must be exactly 0.35 N")
    if value.get("checkpoint_only_target") is not True:
        raise R013CampaignError("R013 bounded BO target must be checkpoint-only")
    try:
        seed_sha = str(value["seed_receipt_sha256"])
        strategy_sha = str(value["runtime_strategy_sha256"])
        source_sha = str(value["source_closure_sha256"])
    except (KeyError, TypeError) as exc:
        raise R013CampaignError("R013 bounded BO identity is incomplete") from exc
    for role, digest in (
        ("seed receipt", seed_sha),
        ("runtime strategy", strategy_sha),
        ("source closure", source_sha),
    ):
        if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
            raise R013CampaignError(f"R013 bounded BO {role} identity is invalid")
    triplet = value.get("controller_triplet_sha256")
    if not isinstance(triplet, Mapping) or set(triplet) != {"script", "txt", "urp"}:
        raise R013CampaignError("R013 bounded BO controller triplet is incomplete")
    for role in ("script", "txt", "urp"):
        digest = triplet.get(role)
        if type(digest) is not str or len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
            raise R013CampaignError(f"R013 bounded BO controller {role} identity is invalid")
    return {
        "schema": BOUNDED_BO_SCHEMA,
        "version": 1,
        "policy": BOUNDED_BO_POLICY,
        "physical_attempt_budget": budget,
        "target_mae_n": target,
        "checkpoint_only_target": True,
        "seed_receipt_sha256": seed_sha,
        "runtime_strategy_sha256": strategy_sha,
        "source_closure_sha256": source_sha,
        "controller_triplet_sha256": {role: str(triplet[role]) for role in ("script", "txt", "urp")},
    }


def optimizer_snapshot(
    *,
    noise_floor_n2: float,
    seed_template: Mapping[str, Any],
    confirmation_target: ConfirmationTarget | None = None,
    runtime_strategy: Mapping[str, Any] | None = None,
    completion_policy: CompletionPolicy | Mapping[str, Any] | str | None = None,
    handoff_policy: HandoffPolicy | Mapping[str, Any] | str | None = None,
    campaign_fingerprint: CampaignFingerprint | Mapping[str, Any] | None = None,
    budgeted_floor_config: Mapping[str, Any] | None = None,
    campaign_role: Mapping[str, Any] | None = None,
    bounded_bo_profile: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    try:
        noise = float(noise_floor_n2)
    except (TypeError, ValueError) as exc:
        raise R013CampaignError("R013 noise floor must be numeric") from exc
    if not math.isfinite(noise) or noise <= 0.0:
        raise R013CampaignError("R013 noise floor must be positive and finite")
    for ki in FIXED_KI_SEEDS:
        physical_candidate_key({**dict(seed_template), "force_i_gain": ki, "i_off": False})
    target = confirmation_target or DEFAULT_CONFIRMATION_TARGET
    if not isinstance(target, ConfirmationTarget):
        raise R013CampaignError("R013 confirmation target must be typed")
    strategy = validate_runtime_strategy(runtime_strategy)
    bounded_profile = _validate_bounded_bo_profile(bounded_bo_profile)
    if bounded_profile is not None and bounded_profile["runtime_strategy_sha256"] != runtime_strategy_sha256(strategy):
        raise R013CampaignError("R013 bounded BO strategy identity differs from snapshot strategy")
    parsed_completion = CompletionPolicy.from_value(completion_policy)
    if handoff_policy is None:
        if parsed_completion.policy == BUDGETED_FLOOR_V1:
            raise R013CampaignError(
                "R013 budgeted floor requires a completed handoff-selection receipt"
            )
        # Retained pre-budgeted campaigns had an explicit fixed default in
        # their snapshot contract.  Keep that compatibility path explicit;
        # validate_handoff_policy(None) itself is intentionally fail-closed.
        parsed_handoff_policy = HandoffPolicy()
    else:
        parsed_handoff_policy = validate_handoff_policy(handoff_policy)
    floor_policy: FloorDiscoveryPolicyV1 | None = None
    if budgeted_floor_config is not None and parsed_completion.policy == BUDGETED_FLOOR_V1:
        if not isinstance(budgeted_floor_config, Mapping):
            raise R013CampaignError("R013 budgeted floor config is not typed")
        if "floor_discovery_policy" not in budgeted_floor_config:
            raise R013CampaignError("R013 budgeted floor config lacks floor discovery policy")
        floor_policy = FloorDiscoveryPolicyV1.from_mapping(
            budgeted_floor_config.get("floor_discovery_policy")
        )
    if campaign_fingerprint is None:
        if parsed_completion.policy == BUDGETED_FLOOR_V1:
            raise R013CampaignError("R013 budgeted floor requires a campaign fingerprint")
        fingerprint = CampaignFingerprint.legacy_default(
            handoff_policy=parsed_handoff_policy.policy,
            runtime_strategy_identity=runtime_strategy_sha256(strategy),
        )
    else:
        fingerprint = validate_campaign_fingerprint(campaign_fingerprint)
        if handoff_policy_identity(fingerprint.handoff_policy) != parsed_handoff_policy.policy:
            raise R013CampaignError("R013 fingerprint handoff policy differs")
    if (
        parsed_completion.policy == BUDGETED_FLOOR_V1
        and budgeted_floor_config is not None
        and (
            any(
                type(getattr(fingerprint, name)) is not str
                or len(getattr(fingerprint, name)) != 64
                or any(
                    character not in "0123456789abcdef"
                    for character in getattr(fingerprint, name)
                )
                for name in (
                    "correction_runtime_strategy_identity",
                    "source_identity",
                    "eoat_identity",
                )
            )
            or not fingerprint.home_tare_identity.startswith(
                "step5d.autotune-v4/r013-home-tare-procedure-v1|"
            )
        )
    ):
        raise R013CampaignError(
            "R013 budgeted-floor campaign fingerprint must be materialized"
        )
    snapshot = {
        "schema": "step5d.autotune-v4/r013-optimizer-snapshot-v1",
        "noise_floor_n2": noise,
        "noise_semantics": "gp_uncertainty_only_sealed_mae_identity",
        "model_dimensions": 6,
        "q": 1,
        "candidate_pool_size": CANDIDATE_POOL_SIZE,
        "warm_start_seed": WARM_START_SEED,
        "minimum_exact_rows_for_bo": MIN_EXACT_ROWS_FOR_BO,
        "seed_template": dict(seed_template),
        "confirmation_target": target.as_dict(),
        "anti_windup": dict(ANTI_WINDUP_CONTRACT),
        "runtime_strategy": strategy,
        "runtime_strategy_sha256": runtime_strategy_sha256(strategy),
        "handoff_policy": parsed_handoff_policy.as_dict(),
        "campaign_fingerprint": fingerprint.as_dict(),
        "campaign_fingerprint_sha256": fingerprint.sha256,
        "completion_policy": parsed_completion.as_dict(),
        "candidate_pool_state": {
            "schema": CANDIDATE_POOL_STATE_SCHEMA,
            "version": 1,
            "sobol_seed": WARM_START_SEED + 1,
            "round_index": 0,
            "cursor": 0,
            "pool_size": CANDIDATE_POOL_SIZE,
        },
        "training_lineage": "fresh_r013_only",
    }
    if floor_policy is not None:
        snapshot["floor_discovery_policy"] = floor_policy.as_dict()
        snapshot["floor_discovery_state"] = {
            "schema": "step5d.autotune-v4/r013-floor-coordinator-snapshot-v1",
            "version": 1,
            "state": "running",
            "novel_count": 0,
            "candidate_pool_size": floor_policy.candidate_pool_size,
            "sobol_cursor": 0,
            "sobol_round_index": 0,
        }
    if budgeted_floor_config is not None:
        snapshot["budgeted_floor_config"] = dict(budgeted_floor_config)
        if parsed_completion.policy == BUDGETED_FLOOR_V1:
            snapshot["materialized_campaign_fingerprint"] = fingerprint.as_dict()
            snapshot["materialized_campaign_fingerprint_sha256"] = fingerprint.sha256
    if bounded_profile is not None:
        snapshot["bounded_bo"] = dict(bounded_profile)
        snapshot["bounded_bo_seed_template"] = dict(seed_template)
    if campaign_role is not None:
        snapshot["campaign_role"] = dict(campaign_role)
        snapshot["formal_campaign_tell_exact"] = campaign_role.get(
            "formal_campaign_tell_exact"
        )
        snapshot["launch_ready"] = campaign_role.get("launch_ready")
    return snapshot


@dataclass(frozen=True)
class Dispatch:
    dispatch_id: str
    kind: str
    ordinal: int
    candidate: Mapping[str, Any]
    candidate_token: str
    abort_allowed: bool
    runtime_strategy_sha256: str
    campaign_fingerprint: Mapping[str, Any] | None = None
    floor_trial: Mapping[str, Any] | None = None

    @property
    def floor_trial_token(self) -> str | None:
        return None if self.floor_trial is None else str(self.floor_trial.get("canonical_token", ""))

    @property
    def floor_trial_key(self) -> tuple[Any, ...] | None:
        if self.floor_trial is None:
            return None
        return tuple(self.floor_trial.get("canonical_key", ()))

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": "step5d.autotune-v4/r013-dispatch-v1",
            "dispatch_id": self.dispatch_id,
            "kind": self.kind,
            "ordinal": self.ordinal,
            "candidate": dict(self.candidate),
            "candidate_token": self.candidate_token,
            "abort_allowed": self.abort_allowed,
            "runtime_strategy_sha256": self.runtime_strategy_sha256,
            "campaign_fingerprint": (
                _fingerprint_payload(self.campaign_fingerprint)
            ),
            "campaign_fingerprint_sha256": (
                None
                if self.campaign_fingerprint is None
                else validate_campaign_fingerprint(self.campaign_fingerprint).sha256
            ),
            "physical_key": list(physical_candidate_key(self.candidate)),
            "floor_trial": None if self.floor_trial is None else dict(self.floor_trial),
            "floor_trial_token": (
                None
                if self.floor_trial is None
                else str(self.floor_trial.get("canonical_token", ""))
            ),
        }


@dataclass(frozen=True)
class PhysicalAdmissionReceipt:
    """Typed truth returned by the sealed physical observation ledger."""

    dispatch_id: str
    candidate_token: str
    candidate_key: tuple[Any, ...]
    attempt_sequence: int
    execution_id: str
    sealed_mae_n: float
    physical_eligible: bool = False
    timing_gate: bool = False
    motion_gate: bool = False
    qualification_passed: bool = False
    observation_uid: str = ""
    sealed: bool = True
    schema: str = PHYSICAL_ADMISSION_SCHEMA
    epoch_qualification_passed: bool | None = None
    trial_admission_passed: bool | None = None
    campaign_fingerprint: Mapping[str, Any] | None = None

    @property
    def admitted_exact(self) -> bool:
        """Per-trial physical truth required before an exact GP row exists."""

        trial_passed = (
            self.physical_eligible
            if self.trial_admission_passed is None
            else self.trial_admission_passed
        )
        return trial_passed and self.timing_gate and self.motion_gate and self.sealed

    @property
    def strict_admitted_exact(self) -> bool:
        """New-campaign admission; legacy aliases are deliberately insufficient."""

        return (
            self.epoch_qualification_passed is True
            and self.trial_admission_passed is True
            and self.motion_gate is True
            and self.timing_gate is True
            and self.sealed is True
        )

    def __post_init__(self) -> None:
        if self.schema != PHYSICAL_ADMISSION_SCHEMA:
            raise R013CampaignError("R013 physical admission schema differs")
        if not self.dispatch_id or not self.candidate_token or not self.execution_id:
            raise R013CampaignError("R013 physical admission identity is incomplete")
        if (
            isinstance(self.attempt_sequence, bool)
            or not isinstance(self.attempt_sequence, int)
            or self.attempt_sequence <= 0
        ):
            raise R013CampaignError("R013 physical admission attempt sequence is invalid")
        if not self.candidate_key:
            raise R013CampaignError("R013 physical admission candidate key is missing")
        try:
            mae_n = float(self.sealed_mae_n)
        except (TypeError, ValueError) as exc:
            raise R013CampaignError("R013 physical admission MAE is invalid") from exc
        if not math.isfinite(mae_n) or mae_n < 0.0:
            raise R013CampaignError("R013 physical admission MAE is invalid")
        object.__setattr__(self, "sealed_mae_n", mae_n)
        for name in (
            "physical_eligible", "timing_gate", "motion_gate", "qualification_passed", "sealed",
        ):
            if not isinstance(getattr(self, name), bool):
                raise R013CampaignError(f"R013 physical admission {name} is not bool")
        if not self.sealed:
            raise R013CampaignError("R013 physical admission is not sealed")
        if not self.observation_uid:
            raise R013CampaignError("R013 physical admission observation identity is missing")
        for name in ("epoch_qualification_passed", "trial_admission_passed"):
            value = getattr(self, name)
            if value is not None and not isinstance(value, bool):
                raise R013CampaignError(f"R013 physical admission {name} is not bool")
        if self.campaign_fingerprint is not None:
            validate_campaign_fingerprint(self.campaign_fingerprint)

    @classmethod
    def from_ledger_record(
        cls,
        *,
        dispatch: Dispatch,
        record: Any,
        epoch_qualification_passed: bool | None = None,
        trial_admission_passed: bool | None = None,
        campaign_fingerprint: CampaignFingerprint | Mapping[str, Any] | None = None,
    ) -> "PhysicalAdmissionReceipt":
        candidate = getattr(record, "candidate", None)
        candidate_payload = getattr(candidate, "canonical", candidate)
        if not isinstance(candidate_payload, Mapping):
            raise R013CampaignError("R013 physical ledger candidate is invalid")
        if physical_candidate_key(candidate_payload) != physical_candidate_key(dispatch.candidate):
            raise R013CampaignError("R013 physical ledger candidate identity differs")
        metrics = getattr(record, "metrics", {})
        execution_id = metrics.get("execution_id") if isinstance(metrics, Mapping) else None
        if not isinstance(execution_id, str) or not execution_id:
            raise R013CampaignError("R013 physical ledger execution identity is missing")
        record_fingerprint = getattr(record, "campaign_fingerprint", None)
        if not isinstance(record_fingerprint, (CampaignFingerprint, Mapping)):
            record_fingerprint = None
        return cls(
            dispatch_id=dispatch.dispatch_id,
            candidate_token=dispatch.candidate_token,
            candidate_key=physical_candidate_key(candidate_payload),
            attempt_sequence=int(getattr(record, "attempt_sequence", 0)),
            execution_id=execution_id,
            sealed_mae_n=float(getattr(record, "mae_n", float("nan"))),
            physical_eligible=bool(getattr(record, "eligible", False)),
            timing_gate=bool(getattr(record, "timing_gate", False)),
            motion_gate=bool(getattr(record, "motion_gate", False)),
            qualification_passed=bool(getattr(record, "qualification_passed", False)),
            observation_uid=str(getattr(record, "observation_uid", "")),
            sealed=bool(getattr(record, "sealed", False)),
            epoch_qualification_passed=(
                epoch_qualification_passed
                if epoch_qualification_passed is not None
                else getattr(record, "epoch_qualification_passed", None)
            ),
            trial_admission_passed=(
                trial_admission_passed
                if trial_admission_passed is not None
                else getattr(record, "trial_admission_passed", None)
            ),
            campaign_fingerprint=(
                campaign_fingerprint
                if campaign_fingerprint is not None
                else record_fingerprint
            ),
        )

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "PhysicalAdmissionReceipt":
        if not isinstance(value, Mapping) or value.get("schema") != PHYSICAL_ADMISSION_SCHEMA:
            raise R013CampaignError("R013 physical admission evidence schema differs")
        candidate_key = value.get("candidate_key")
        if not isinstance(candidate_key, Sequence) or isinstance(candidate_key, (str, bytes)):
            raise R013CampaignError("R013 physical admission candidate key is invalid")
        return cls(
            dispatch_id=str(value.get("dispatch_id", "")),
            candidate_token=str(value.get("candidate_token", "")),
            candidate_key=tuple(candidate_key),
            attempt_sequence=value.get("attempt_sequence"),
            execution_id=str(value.get("execution_id", "")),
            sealed_mae_n=value.get("sealed_mae_n"),
            physical_eligible=value.get("physical_eligible"),
            timing_gate=value.get("timing_gate"),
            motion_gate=value.get("motion_gate"),
            qualification_passed=value.get("qualification_passed"),
            observation_uid=str(value.get("observation_uid", "")),
            sealed=value.get("sealed"),
            schema=str(value.get("schema", "")),
            epoch_qualification_passed=value.get("epoch_qualification_passed"),
            trial_admission_passed=value.get("trial_admission_passed"),
            campaign_fingerprint=value.get("campaign_fingerprint"),
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "dispatch_id": self.dispatch_id,
            "candidate_token": self.candidate_token,
            "candidate_key": list(self.candidate_key),
            "attempt_sequence": self.attempt_sequence,
            "execution_id": self.execution_id,
            "sealed_mae_n": self.sealed_mae_n,
            "physical_eligible": self.physical_eligible,
            "timing_gate": self.timing_gate,
            "motion_gate": self.motion_gate,
            "qualification_passed": self.qualification_passed,
            "epoch_qualification_passed": self.epoch_qualification_passed,
            "trial_admission_passed": self.trial_admission_passed,
            "campaign_fingerprint": (
                _fingerprint_payload(self.campaign_fingerprint)
            ),
            "observation_uid": self.observation_uid,
            "sealed": self.sealed,
        }


@dataclass(frozen=True)
class CandidateRepeatSummary:
    """Readable admitted-repeat summary for one physical candidate."""

    candidate: Mapping[str, Any]
    physical_key: tuple[Any, ...]
    admitted_exact_count: int
    minimum_sealed_mae_n: float
    arithmetic_mean_sealed_mae_n: float
    sealed_mae_n_values: tuple[float, ...]
    admitted_dispatch_ids: tuple[str, ...]
    admitted_observation_uids: tuple[str, ...]
    trigger_dispatch_id: str | None
    confirmation_complete: bool
    confirmed: bool

    @property
    def target_achieved(self) -> bool:
        return self.confirmed

    @property
    def confirmation_failed(self) -> bool:
        return self.confirmation_complete and not self.confirmed

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": "step5d.autotune-v4/r013-candidate-repeat-summary-v1",
            "candidate": dict(self.candidate),
            "physical_key": list(self.physical_key),
            "admitted_exact_count": self.admitted_exact_count,
            "minimum_sealed_mae_n": self.minimum_sealed_mae_n,
            "arithmetic_mean_sealed_mae_n": self.arithmetic_mean_sealed_mae_n,
            "sealed_mae_n_values": list(self.sealed_mae_n_values),
            "admitted_dispatch_ids": list(self.admitted_dispatch_ids),
            "admitted_observation_uids": list(self.admitted_observation_uids),
            "trigger_dispatch_id": self.trigger_dispatch_id,
            "confirmation_complete": self.confirmation_complete,
            "confirmed": self.confirmed,
            "target_achieved": self.target_achieved,
            "confirmation_failed": self.confirmation_failed,
        }


@dataclass(frozen=True)
class ConfirmationSummary:
    """Typed incumbent summary separating single minima from confirmation."""

    target: ConfirmationTarget
    candidate_summaries: tuple[CandidateRepeatSummary, ...]
    single_trial_minimum_sealed_mae_n: float | None
    pending_confirmation: CandidateRepeatSummary | None
    confirmed_incumbent: CandidateRepeatSummary | None
    schema: str = "step5d.autotune-v4/r013-confirmation-summary-v1"

    @property
    def target_achieved(self) -> bool:
        return self.confirmed_incumbent is not None

    @property
    def success(self) -> bool:
        return self.target_achieved

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "target": self.target.as_dict(),
            "single_trial_minimum_sealed_mae_n": self.single_trial_minimum_sealed_mae_n,
            "candidate_summaries": [row.as_dict() for row in self.candidate_summaries],
            "pending_confirmation": (
                None if self.pending_confirmation is None else self.pending_confirmation.as_dict()
            ),
            "confirmed_incumbent": (
                None if self.confirmed_incumbent is None else self.confirmed_incumbent.as_dict()
            ),
            "target_achieved": self.target_achieved,
        }


def exact_observation(
    dispatch: Dispatch,
    *,
    admission: PhysicalAdmissionReceipt,
    observation_variance_n2: float,
    anti_windup_metrics: Mapping[str, Any],
    runtime_strategy_receipt: Mapping[str, Any] | None = None,
    runtime_strategy_sidecar_sha256: str | None = None,
    expected_fingerprint: CampaignFingerprint | None = None,
    strict: bool = False,
) -> dict[str, Any]:
    _validate_admission(
        dispatch,
        admission,
        expected_fingerprint=expected_fingerprint,
        strict=strict,
    )
    admitted_exact = admission.strict_admitted_exact if strict else admission.admitted_exact
    if not admitted_exact:
        raise R013CampaignError("R013 exact observation requires physical motion/timing admission")
    objective = float(admission.sealed_mae_n)
    variance = float(observation_variance_n2)
    if not math.isfinite(objective) or objective < 0.0 or not math.isfinite(variance) or variance <= 0.0:
        raise R013CampaignError("R013 exact objective/variance is invalid")
    anti_windup = validate_trial_anti_windup_metrics(
        anti_windup_metrics,
        candidate=dispatch.candidate,
    )
    result = {
        "schema": "step5d.autotune-v4/r013-exact-observation-v1",
        "dispatch_id": dispatch.dispatch_id,
        "candidate": dict(dispatch.candidate),
        "candidate_token": dispatch.candidate_token,
        "objective_n": objective,
        "observation_variance_n2": variance,
        "completed": True,
        "sealed": True,
        "full_observation": True,
        "eligible": admitted_exact,
        "censored": False,
        "kind": dispatch.kind,
        "campaign_epoch": "r013",
        "anti_windup": anti_windup,
        "physical_admission": admission.as_dict(),
        "campaign_fingerprint": (
            _fingerprint_payload(dispatch.campaign_fingerprint)
        ),
    }
    if runtime_strategy_receipt is not None:
        result["runtime_strategy_receipt"] = dict(runtime_strategy_receipt)
        result["runtime_strategy_sidecar_sha256"] = str(runtime_strategy_sidecar_sha256)
    return result


def _validated_runtime_strategy_receipt(
    value: Mapping[str, Any],
    *,
    expected_sha256: str,
) -> dict[str, Any]:
    required = {
        "schema", "runtime_strategy_sha256", "enabled", "path_clock",
        "path_sample_count", "formal_sample_count", "minimum_effective_target_n",
        "maximum_applied_correction_n", "violation_count",
        "first_path_time_s", "last_path_time_s", "maximum_path_clock_gap_s",
        "exit_restored_to_unmodified_target", "safe_return_transition_observed",
        "exit_mode",
    }
    if type(value) is not dict or set(value) != required:
        raise R013CampaignError("R013 runtime strategy receipt fields differ")
    if (
        value.get("schema") != "step5d.autotune-v4/r013-runtime-strategy-receipt-v1"
        or value.get("runtime_strategy_sha256") != expected_sha256
        or value.get("enabled") is not True
        or value.get("path_clock") != "runtime_desired_twist_path_time_s"
        or type(value.get("path_sample_count")) is not int
        or value["path_sample_count"] <= 0
        or type(value.get("formal_sample_count")) is not int
        or value["formal_sample_count"] <= 0
        or value["formal_sample_count"] > value["path_sample_count"]
        or type(value.get("violation_count")) is not int
        or value["violation_count"] != 0
        or value.get("exit_restored_to_unmodified_target") is not True
        or value.get("safe_return_transition_observed") is not True
        or value.get("exit_mode") != "safe_return"
    ):
        raise R013CampaignError("R013 runtime strategy receipt contract differs")
    minimum = value.get("minimum_effective_target_n")
    maximum = value.get("maximum_applied_correction_n")
    first = value.get("first_path_time_s")
    last = value.get("last_path_time_s")
    gap = value.get("maximum_path_clock_gap_s")
    if (
        type(minimum) not in (int, float)
        or type(maximum) not in (int, float)
        or type(first) not in (int, float)
        or type(last) not in (int, float)
        or type(gap) not in (int, float)
        or not 3.75 <= float(minimum) <= 5.0
        or not 0.0 <= float(maximum) <= 1.25
        or not 0.0 <= float(first) <= 0.1
        or float(last) < 59.9
        or not 0.0 <= float(gap) <= 0.08
    ):
        raise R013CampaignError("R013 runtime strategy receipt bounds differ")
    return dict(value)


def _validate_admission(
    dispatch: Dispatch,
    admission: PhysicalAdmissionReceipt,
    *,
    expected_fingerprint: CampaignFingerprint | None = None,
    strict: bool = False,
) -> None:
    if not isinstance(admission, PhysicalAdmissionReceipt):
        raise R013CampaignError("R013 tell requires typed physical admission evidence")
    if admission.dispatch_id != dispatch.dispatch_id:
        raise R013CampaignError("R013 physical admission dispatch identity differs")
    if admission.candidate_token != dispatch.candidate_token:
        raise R013CampaignError("R013 physical admission candidate identity differs")
    if admission.candidate_key != physical_candidate_key(dispatch.candidate):
        raise R013CampaignError("R013 physical admission physical key differs")
    if dispatch.campaign_fingerprint is not None:
        if admission.campaign_fingerprint is not None:
            if validate_campaign_fingerprint(admission.campaign_fingerprint) != validate_campaign_fingerprint(
                dispatch.campaign_fingerprint
            ):
                raise R013CampaignError("R013 physical admission campaign fingerprint differs")
        elif strict:
            raise R013CampaignError("R013 physical admission lacks campaign fingerprint")
    if strict:
        if expected_fingerprint is None:
            raise R013CampaignError("R013 strict admission fingerprint is not bound")
        if admission.campaign_fingerprint is None:
            raise R013CampaignError("R013 physical admission lacks campaign fingerprint")
        if validate_campaign_fingerprint(admission.campaign_fingerprint) != expected_fingerprint:
            raise R013CampaignError("R013 exact observation campaign fingerprint differs")
        if admission.epoch_qualification_passed is None or admission.trial_admission_passed is None:
            raise R013CampaignError("R013 physical admission has ambiguous qualification fields")


def _admission_rejection_reasons(
    admission: PhysicalAdmissionReceipt,
    *,
    strict: bool,
) -> list[str]:
    reasons: list[str] = []
    if strict:
        if admission.epoch_qualification_passed is False:
            reasons.append("epoch_qualification_passed=false")
        if admission.trial_admission_passed is False:
            reasons.append("trial_admission_passed=false")
    elif not admission.physical_eligible:
        reasons.append("physical_eligible=false")
    if not admission.timing_gate:
        reasons.append("timing_gate=false")
    if not admission.motion_gate:
        reasons.append("motion_gate=false")
    return reasons


def validate_trial_anti_windup_metrics(
    metrics: Mapping[str, Any],
    *,
    candidate: Mapping[str, Any],
) -> dict[str, Any]:
    required = {
        "schema", "policy", "max_abs_integral_n_s", "max_abs_i_term",
        "saturation_duty", "freeze_duty", "invariant_violation_count",
        "reset_reasons", "path_gain_hot_switch",
    }
    if not isinstance(metrics, Mapping) or set(metrics) != required:
        raise R013CampaignError("R013 trial anti-windup metric fields differ")
    if (
        metrics.get("schema") != TRIAL_ANTI_WINDUP_SCHEMA
        or metrics.get("policy") != ANTI_WINDUP_CONTRACT["schema"].split("/")[-1]
        or metrics.get("path_gain_hot_switch") is not False
    ):
        raise R013CampaignError("R013 trial anti-windup policy differs")
    numeric = {
        name: float(metrics[name])
        for name in (
            "max_abs_integral_n_s", "max_abs_i_term", "saturation_duty", "freeze_duty",
        )
    }
    if not all(math.isfinite(value) and value >= 0.0 for value in numeric.values()):
        raise R013CampaignError("R013 trial anti-windup metrics are invalid")
    if numeric["max_abs_integral_n_s"] > 1.0 + 1e-12:
        raise R013CampaignError("R013 trial exceeded integral state limit")
    if numeric["max_abs_i_term"] > 0.5 * float(candidate["force_p_gain"]) + 1e-12:
        raise R013CampaignError("R013 trial exceeded I-term authority")
    if numeric["saturation_duty"] > 1.0 or numeric["freeze_duty"] > 1.0:
        raise R013CampaignError("R013 trial duty must be within [0,1]")
    violations = metrics.get("invariant_violation_count")
    if isinstance(violations, bool) or not isinstance(violations, int) or violations != 0:
        raise R013CampaignError("R013 trial has an anti-windup invariant violation")
    reasons = metrics.get("reset_reasons")
    if not isinstance(reasons, Sequence) or isinstance(reasons, (str, bytes)) or any(
        not isinstance(reason, str) or not reason for reason in reasons
    ):
        raise R013CampaignError("R013 trial reset reasons are invalid")
    reason_set = set(reasons)
    if not {"candidate_dispatch", "path_entry"}.issubset(reason_set) or not (
        {"home", "mode_exit_or_home"} & reason_set
    ):
        raise R013CampaignError("R013 exact trial lacks required reset boundaries")
    return {
        **dict(metrics),
        **numeric,
        "reset_reasons": list(reasons),
    }


class Campaign:
    """One serial writer over a fresh R013 ledger."""

    def __init__(self, ledger: Ledger) -> None:
        self.ledger = ledger
        snapshot = ledger.header["optimizer_snapshot"]
        if not isinstance(snapshot, Mapping):
            raise R013CampaignError("R013 ledger optimizer snapshot is invalid")
        if snapshot.get("anti_windup") != ANTI_WINDUP_CONTRACT:
            raise R013CampaignError("R013 fixed anti-windup contract differs")
        self.confirmation_target = ConfirmationTarget.from_mapping(
            snapshot.get("confirmation_target")
        )
        self.completion_policy = CompletionPolicy.from_value(snapshot.get("completion_policy"))
        self.bounded_bo_profile = _validate_bounded_bo_profile(snapshot.get("bounded_bo"))
        if self.bounded_bo_profile is not None:
            seed_template = snapshot.get("bounded_bo_seed_template")
            if not isinstance(seed_template, Mapping):
                raise R013CampaignError("R013 bounded BO seed template is missing")
            if dict(seed_template) != dict(snapshot.get("seed_template", {})):
                raise R013CampaignError("R013 bounded BO seed template differs from optimizer seed")
            persisted_strategy_sha = self.bounded_bo_profile["runtime_strategy_sha256"]
            if snapshot.get("runtime_strategy_sha256") != persisted_strategy_sha:
                raise R013CampaignError("R013 bounded BO strategy snapshot identity differs")
        self.strict_admission = self.completion_policy.policy == BUDGETED_FLOOR_V1
        self.floor_discovery_policy = (
            FloorDiscoveryPolicyV1.from_mapping(snapshot.get("floor_discovery_policy"))
            if self.completion_policy.policy == BUDGETED_FLOOR_V1
            and snapshot.get("floor_discovery_policy") is not None
            else None
        )
        persisted_handoff = snapshot.get("handoff_policy")
        self.handoff_policy = (
            HandoffPolicy()
            if persisted_handoff is None
            else validate_handoff_policy(persisted_handoff)
        )
        persisted_fingerprint = snapshot.get("campaign_fingerprint")
        self.campaign_fingerprint = (
            validate_campaign_fingerprint(persisted_fingerprint)
            if persisted_fingerprint is not None
            else CampaignFingerprint.legacy_default(
                handoff_policy=self.handoff_policy.policy,
                runtime_strategy_identity="legacy",
            )
        )
        persisted_fingerprint_sha = snapshot.get("campaign_fingerprint_sha256")
        if (
            persisted_fingerprint_sha is not None
            and persisted_fingerprint_sha != self.campaign_fingerprint.sha256
        ):
            raise R013CampaignError("R013 campaign fingerprint snapshot identity differs")
        self.snapshot = dict(snapshot)
        # Older local R013 ledgers did not carry this field.  Keep their
        # replay deterministic by materializing the fixed default in memory;
        # every newly created ledger persists it in the header above.
        self.snapshot["confirmation_target"] = self.confirmation_target.as_dict()
        self.runtime_strategy = validate_runtime_strategy(
            snapshot.get("runtime_strategy", DISABLED_RUNTIME_STRATEGY)
        )
        self.runtime_strategy_sha256 = runtime_strategy_sha256(self.runtime_strategy)
        persisted_strategy_hash = snapshot.get("runtime_strategy_sha256")
        if (
            persisted_strategy_hash is not None
            and persisted_strategy_hash != self.runtime_strategy_sha256
        ):
            raise R013CampaignError("R013 runtime strategy snapshot identity differs")
        if self.runtime_strategy.get("enabled") is True and persisted_strategy_hash is None:
            raise R013CampaignError("R013 enabled runtime strategy snapshot lacks its identity")
        self.snapshot["runtime_strategy"] = dict(self.runtime_strategy)
        self.snapshot["runtime_strategy_sha256"] = self.runtime_strategy_sha256
        self.snapshot["completion_policy"] = self.completion_policy.as_dict()
        self.snapshot["handoff_policy"] = self.handoff_policy.as_dict()
        self.snapshot["campaign_fingerprint"] = self.campaign_fingerprint.as_dict()
        self.snapshot["campaign_fingerprint_sha256"] = self.campaign_fingerprint.sha256
        self.snapshot.setdefault(
            "candidate_pool_state",
            {
                "schema": CANDIDATE_POOL_STATE_SCHEMA,
                "version": 1,
                "sobol_seed": WARM_START_SEED + 1,
                "round_index": 0,
                "cursor": 0,
                "pool_size": CANDIDATE_POOL_SIZE,
            },
        )
        self.candidate_pool_state = dict(self.snapshot["candidate_pool_state"])
        self.gp_config = ProductionGPConfig.from_snapshot(snapshot)
        self.warm = warm_start_candidates(snapshot["seed_template"])
        self.observations: list[Mapping[str, Any]] = []
        self.observation_ledger_sequences: list[int] = []
        self.rejected_admissions: list[Mapping[str, Any]] = []
        self.dispatches: list[Dispatch] = []
        self.in_flight: Dispatch | None = None
        self.stopped = False
        self.fit_receipts: list[Mapping[str, Any]] = []
        self.anchor_plan: Mapping[str, Any] | None = None
        self.anchor_candidates: tuple[dict[str, Any], ...] = ()
        self.local_refinement_plan: Mapping[str, Any] | None = None
        self.local_refinement_plan_sequence: int | None = None
        self.local_refinement_candidates: tuple[dict[str, Any], ...] = ()
        self.high_ki_probe_plan: Mapping[str, Any] | None = None
        self.high_ki_probe_plan_sequence: int | None = None
        self.high_ki_probe_candidates: tuple[dict[str, Any], ...] = ()
        self.normal_velocity_gain_probe_plan: Mapping[str, Any] | None = None
        self.normal_velocity_gain_probe_plan_sequence: int | None = None
        self.normal_velocity_gain_probe_candidates: tuple[dict[str, Any], ...] = ()
        self.normal_velocity_gain_probe_stop: Mapping[str, Any] | None = None
        self.lower_p_over_d_probe_plan: Mapping[str, Any] | None = None
        self.lower_p_over_d_probe_plan_sequence: int | None = None
        self.lower_p_over_d_probe_candidates: tuple[dict[str, Any], ...] = ()
        self.strategy_canary_plan: Mapping[str, Any] | None = None
        self.strategy_canary_plan_sequence: int | None = None
        self.strategy_canary_candidate: dict[str, Any] | None = None
        self.floor_coordinator: FloorDiscoveryCoordinator | None = None
        self._replay()
        if self.floor_discovery_policy is not None:
            self.floor_coordinator = FloorDiscoveryCoordinator.from_records(
                self.floor_discovery_policy,
                (
                    record["payload"]
                    for record in self.ledger.records
                    if record.get("record_type") == "floor_coordinator"
                ),
                fingerprint=self.campaign_fingerprint.as_dict(),
                seed_candidate=self.warm[0],
                core_proposal_provider=self._floor_core_proposal,
            )

    @classmethod
    def create(
        cls,
        path: Path,
        *,
        campaign_id: str,
        run_id: str,
        attempt_id: str,
        noise_floor_n2: float,
        r012_seed_source: Mapping[str, Any],
        runtime_strategy: Mapping[str, Any] | None = None,
        completion_policy: CompletionPolicy | Mapping[str, Any] | str | None = None,
        handoff_policy: HandoffPolicy | Mapping[str, Any] | str | None = None,
        campaign_fingerprint: CampaignFingerprint | Mapping[str, Any] | None = None,
        budgeted_floor_config: Mapping[str, Any] | None = None,
        campaign_role: Mapping[str, Any] | None = None,
        seed_template: Mapping[str, Any] | None = None,
        bounded_bo_profile: Mapping[str, Any] | None = None,
    ) -> "Campaign":
        seed = (
            select_seed_template(r012_seed_source)
            if seed_template is None
            else dict(seed_template)
        )
        if seed_template is not None:
            if not isinstance(seed_template, Mapping):
                raise R013CampaignError("R013 bounded BO seed template must be an object")
            for ki in FIXED_KI_SEEDS:
                physical_candidate_key({**seed, "force_i_gain": ki, "i_off": False})
        snapshot = optimizer_snapshot(
            noise_floor_n2=noise_floor_n2,
            seed_template=seed,
            completion_policy=completion_policy,
            handoff_policy=handoff_policy,
            campaign_fingerprint=campaign_fingerprint,
            runtime_strategy=runtime_strategy,
            budgeted_floor_config=budgeted_floor_config,
            campaign_role=campaign_role,
            bounded_bo_profile=bounded_bo_profile,
        )
        return cls(
            Ledger.create(
                path,
                campaign_id=campaign_id,
                run_id=run_id,
                attempt_id=attempt_id,
                optimizer_snapshot=snapshot,
            )
        )

    @classmethod
    def resume(cls, path: Path) -> "Campaign":
        return cls(Ledger.load(path))

    def _replay(self) -> None:
        dispatch_by_id: dict[str, Dispatch] = {}
        completed: set[str] = set()
        for record in self.ledger.records[1:]:
            kind = record["record_type"]
            payload = record["payload"]
            if kind == "dispatch":
                if any(dispatch_id not in completed for dispatch_id in dispatch_by_id):
                    raise R013CampaignError(
                        "R013 serial q=1 ledger dispatches overlap"
                    )
                candidate = payload.get("candidate")
                if not isinstance(candidate, Mapping):
                    raise R013CampaignError("R013 persisted dispatch candidate is invalid")
                dispatch = Dispatch(
                    str(payload["dispatch_id"]), str(payload["kind"]), int(payload["ordinal"]),
                    dict(candidate), str(payload["candidate_token"]), bool(payload["abort_allowed"]),
                    str(payload.get("runtime_strategy_sha256", self.runtime_strategy_sha256)),
                    (
                        self.campaign_fingerprint.as_dict()
                        if payload.get("campaign_fingerprint") is None
                        else dict(payload["campaign_fingerprint"])
                    ),
                    None if payload.get("floor_trial") is None else dict(payload["floor_trial"]),
                )
                if dispatch.runtime_strategy_sha256 != self.runtime_strategy_sha256:
                    raise R013CampaignError("R013 dispatch runtime strategy identity differs")
                if (
                    self.runtime_strategy.get("enabled") is True
                    and "runtime_strategy_sha256" not in payload
                ):
                    raise R013CampaignError("R013 enabled-strategy dispatch lacks its identity")
                if dispatch.candidate_token != candidate_token(dispatch.candidate):
                    raise R013CampaignError("R013 persisted physical identity differs")
                if dispatch.floor_trial is not None:
                    FloorTrialSpec.from_mapping(dispatch.floor_trial)
                if validate_campaign_fingerprint(dispatch.campaign_fingerprint) != self.campaign_fingerprint:
                    raise R013CampaignError("R013 dispatch campaign fingerprint differs")
                if self.strategy_canary_plan is not None and not self.strategy_canary_terminal:
                    if self.strategy_canary_physical_attempt_count >= int(
                        self.strategy_canary_plan["maximum_physical_attempts"]
                    ):
                        raise R013CampaignError("R013 strategy canary exceeds its physical-attempt limit")
                    if dispatch.kind not in {STRATEGY_CANARY_KIND, CONFIRMATION_KIND}:
                        raise R013CampaignError("R013 strategy canary was bypassed by another dispatch")
                    assert self.strategy_canary_candidate is not None
                    if physical_candidate_key(dispatch.candidate) != physical_candidate_key(
                        self.strategy_canary_candidate
                    ):
                        raise R013CampaignError("R013 strategy canary dispatch candidate differs")
                    if dispatch.abort_allowed:
                        raise R013CampaignError("R013 strategy canary dispatch cannot be abortable")
                if dispatch.kind == LOCAL_REFINEMENT_KIND:
                    if self.local_refinement_plan is None:
                        raise R013CampaignError(
                            "R013 local refinement dispatch precedes its plan"
                        )
                    if dispatch.abort_allowed:
                        raise R013CampaignError(
                            "R013 local refinement dispatch cannot be abortable"
                        )
                    refinement_slot = self.local_refinement_slot_cursor
                    if refinement_slot >= len(self.local_refinement_candidates):
                        raise R013CampaignError(
                            "R013 local refinement dispatch exceeds its plan"
                        )
                    expected = self.local_refinement_candidates[refinement_slot]
                    if physical_candidate_key(dispatch.candidate) != physical_candidate_key(expected):
                        raise R013CampaignError(
                            "R013 local refinement dispatch differs from plan order"
                        )
                if dispatch.kind == HIGH_KI_PROBE_KIND:
                    if self.high_ki_probe_plan is None:
                        raise R013CampaignError("R013 high-Ki dispatch precedes its plan")
                    if dispatch.abort_allowed:
                        raise R013CampaignError("R013 high-Ki dispatch cannot be abortable")
                    high_ki_slot = self.high_ki_probe_slot_cursor
                    if high_ki_slot >= len(self.high_ki_probe_candidates):
                        raise R013CampaignError("R013 high-Ki dispatch exceeds its plan")
                    expected = self.high_ki_probe_candidates[high_ki_slot]
                    if physical_candidate_key(dispatch.candidate) != physical_candidate_key(expected):
                        raise R013CampaignError("R013 high-Ki dispatch differs from plan order")
                if dispatch.kind == NORMAL_VELOCITY_GAIN_PROBE_KIND:
                    if self.normal_velocity_gain_probe_plan is None:
                        raise R013CampaignError("R013 normal-velocity dispatch precedes its plan")
                    if self.normal_velocity_gain_probe_stop is not None:
                        raise R013CampaignError("R013 normal-velocity dispatch follows its stop")
                    if dispatch.abort_allowed:
                        raise R013CampaignError("R013 normal-velocity dispatch cannot be abortable")
                    slot = self.normal_velocity_gain_probe_slot_cursor
                    if slot >= len(self.normal_velocity_gain_probe_candidates):
                        raise R013CampaignError("R013 normal-velocity dispatch exceeds its plan")
                    expected = self.normal_velocity_gain_probe_candidates[slot]
                    if physical_candidate_key(dispatch.candidate) != physical_candidate_key(expected):
                        raise R013CampaignError("R013 normal-velocity dispatch differs from plan order")
                if dispatch.kind == LOWER_P_OVER_D_PROBE_KIND:
                    if self.lower_p_over_d_probe_plan is None:
                        raise R013CampaignError("R013 lower-P/D dispatch precedes its plan")
                    if dispatch.abort_allowed:
                        raise R013CampaignError("R013 lower-P/D dispatch cannot be abortable")
                    slot = self.lower_p_over_d_probe_slot_cursor
                    if slot >= len(self.lower_p_over_d_probe_candidates):
                        raise R013CampaignError("R013 lower-P/D dispatch exceeds its plan")
                    expected = self.lower_p_over_d_probe_candidates[slot]
                    if physical_candidate_key(dispatch.candidate) != physical_candidate_key(expected):
                        raise R013CampaignError("R013 lower-P/D dispatch differs from plan order")
                dispatch_by_id[dispatch.dispatch_id] = dispatch
                self.dispatches.append(dispatch)
            elif kind == "observation":
                dispatch_id = str(payload.get("dispatch_id", ""))
                if dispatch_id not in dispatch_by_id or dispatch_id in completed:
                    raise R013CampaignError("R013 persisted observation dispatch differs")
                dispatch = dispatch_by_id[dispatch_id]
                expected_fields = {
                    "schema", "dispatch_id", "candidate", "candidate_token",
                    "objective_n", "observation_variance_n2", "completed", "sealed",
                    "full_observation", "eligible", "censored", "kind",
                    "campaign_epoch", "anti_windup", "physical_admission",
                    "campaign_fingerprint",
                }
                if self.runtime_strategy.get("enabled") is True:
                    expected_fields |= {
                        "runtime_strategy_receipt",
                        "runtime_strategy_sidecar_sha256",
                    }
                if dispatch.floor_trial is not None:
                    expected_fields |= {"floor_trial", "floor_trial_token", "floor_trial_key"}
                if set(payload) != expected_fields and not (
                    not self.strict_admission
                    and set(payload) == expected_fields - {"campaign_fingerprint"}
                ):
                    raise R013CampaignError("R013 persisted observation fields differ")
                if (
                    payload.get("schema")
                    != "step5d.autotune-v4/r013-exact-observation-v1"
                    or payload.get("completed") is not True
                    or payload.get("sealed") is not True
                    or payload.get("full_observation") is not True
                    or payload.get("eligible") is not True
                    or payload.get("censored") is not False
                    or payload.get("campaign_epoch") != "r013"
                ):
                    raise R013CampaignError("R013 persisted observation contract differs")
                if payload.get("candidate_token") != dispatch.candidate_token:
                    raise R013CampaignError("R013 persisted observation identity differs")
                if payload.get("campaign_fingerprint") is not None and validate_campaign_fingerprint(
                    payload["campaign_fingerprint"]
                ) != self.campaign_fingerprint:
                    raise R013CampaignError("R013 persisted observation campaign fingerprint differs")
                if self.strict_admission and payload.get("campaign_fingerprint") is None:
                    raise R013CampaignError("R013 persisted observation lacks campaign fingerprint")
                if payload.get("kind") != dispatch.kind:
                    raise R013CampaignError(
                        "R013 persisted observation kind differs from dispatch"
                    )
                if dispatch.floor_trial is not None:
                    if (
                        payload.get("floor_trial") != dispatch.floor_trial
                        or payload.get("floor_trial_token") != dispatch.floor_trial.get("canonical_token")
                        or tuple(payload.get("floor_trial_key", ()))
                        != tuple(dispatch.floor_trial.get("canonical_key", ()))
                    ):
                        raise R013CampaignError("R013 persisted floor trial identity differs")
                admission = PhysicalAdmissionReceipt.from_mapping(payload.get("physical_admission", {}))
                _validate_admission(
                    dispatch,
                    admission,
                    expected_fingerprint=self.campaign_fingerprint,
                    strict=self.strict_admission,
                )
                if not (admission.strict_admitted_exact if self.strict_admission else admission.admitted_exact):
                    raise R013CampaignError(
                        "R013 persisted exact observation lacks physical motion/timing admission"
                    )
                if (
                    type(payload.get("objective_n")) not in (int, float)
                    or not math.isfinite(float(payload["objective_n"]))
                    or float(payload["objective_n"]) != float(admission.sealed_mae_n)
                    or type(payload.get("observation_variance_n2")) not in (int, float)
                    or float(payload["observation_variance_n2"])
                    != float(self.gp_config.noise_floor_n2)
                ):
                    raise R013CampaignError("R013 persisted observation objective/noise differs")
                if physical_candidate_key(payload["candidate"]) != physical_candidate_key(dispatch.candidate):
                    raise R013CampaignError("R013 persisted observation candidate differs")
                validate_trial_anti_windup_metrics(
                    payload.get("anti_windup", {}),
                    candidate=payload["candidate"],
                )
                if self.runtime_strategy.get("enabled") is True:
                    _validated_runtime_strategy_receipt(
                        payload.get("runtime_strategy_receipt", {}),
                        expected_sha256=self.runtime_strategy_sha256,
                    )
                    sidecar_sha = payload.get("runtime_strategy_sidecar_sha256")
                    if (
                        type(sidecar_sha) is not str
                        or len(sidecar_sha) != 64
                        or any(c not in "0123456789abcdef" for c in sidecar_sha)
                    ):
                        raise R013CampaignError("R013 runtime strategy sidecar identity differs")
                self.observations.append(payload)
                self.observation_ledger_sequences.append(int(record["sequence"]))
                completed.add(dispatch_id)
            elif kind == "rejected":
                dispatch_id = str(payload.get("dispatch_id", ""))
                if dispatch_id not in dispatch_by_id or dispatch_id in completed:
                    raise R013CampaignError("R013 persisted rejection dispatch differs")
                dispatch = dispatch_by_id[dispatch_id]
                expected_fields = {
                    "schema", "dispatch_id", "candidate", "candidate_token",
                    "physical_admission", "status", "reasons",
                    "exact_observation_added", "campaign_fingerprint",
                }
                if self.runtime_strategy.get("enabled") is True:
                    expected_fields |= {
                        "runtime_strategy_receipt",
                        "runtime_strategy_sidecar_sha256",
                    }
                if set(payload) != expected_fields and not (
                    not self.strict_admission
                    and set(payload) == expected_fields - {"campaign_fingerprint"}
                ):
                    raise R013CampaignError("R013 persisted rejection fields differ")
                if payload.get("schema") != REJECTED_ADMISSION_SCHEMA:
                    raise R013CampaignError("R013 persisted rejection schema differs")
                admission = PhysicalAdmissionReceipt.from_mapping(payload.get("physical_admission", {}))
                _validate_admission(
                    dispatch,
                    admission,
                    expected_fingerprint=self.campaign_fingerprint,
                    strict=self.strict_admission,
                )
                if (
                    admission.strict_admitted_exact
                    if self.strict_admission
                    else admission.admitted_exact
                ):
                    raise R013CampaignError("R013 persisted rejection is physically admissible")
                if payload.get("campaign_fingerprint") is not None and validate_campaign_fingerprint(
                    payload["campaign_fingerprint"]
                ) != self.campaign_fingerprint:
                    raise R013CampaignError("R013 persisted rejection campaign fingerprint differs")
                if self.strict_admission and payload.get("campaign_fingerprint") is None:
                    raise R013CampaignError("R013 persisted rejection lacks campaign fingerprint")
                expected_reasons = _admission_rejection_reasons(
                    admission,
                    strict=self.strict_admission,
                )
                if (
                    payload.get("candidate_token") != dispatch.candidate_token
                    or physical_candidate_key(payload.get("candidate", {}))
                    != physical_candidate_key(dispatch.candidate)
                    or payload.get("status") != "rejected_ineligible"
                    or payload.get("exact_observation_added") is not False
                    or payload.get("reasons") != expected_reasons
                ):
                    raise R013CampaignError("R013 persisted rejection contract differs")
                if self.runtime_strategy.get("enabled") is True:
                    _validated_runtime_strategy_receipt(
                        payload.get("runtime_strategy_receipt", {}),
                        expected_sha256=self.runtime_strategy_sha256,
                    )
                    sidecar_sha = payload.get("runtime_strategy_sidecar_sha256")
                    if (
                        type(sidecar_sha) is not str
                        or len(sidecar_sha) != 64
                        or any(c not in "0123456789abcdef" for c in sidecar_sha)
                    ):
                        raise R013CampaignError("R013 runtime strategy sidecar identity differs")
                self.rejected_admissions.append(payload)
                completed.add(dispatch_id)
            elif kind == "hard_guard":
                self.stopped = True
                completed.add(str(payload.get("dispatch_id", "")))
            elif kind == "fit_receipt":
                self.fit_receipts.append(payload)
                pool_state = payload.get("candidate_pool_state")
                if pool_state is not None:
                    if (
                        not isinstance(pool_state, Mapping)
                        or pool_state.get("schema") != CANDIDATE_POOL_STATE_SCHEMA
                        or pool_state.get("version") != 1
                        or pool_state.get("pool_size") != CANDIDATE_POOL_SIZE
                    ):
                        raise R013CampaignError("R013 persisted Sobol pool state differs")
                    self.candidate_pool_state = dict(pool_state)
                    self.snapshot["candidate_pool_state"] = dict(pool_state)
            elif kind == "floor_coordinator":
                if self.floor_discovery_policy is None:
                    raise R013CampaignError(
                        "R013 floor coordinator record lacks its budgeted policy"
                    )
                if (
                    payload.get("campaign_fingerprint") is None
                    or validate_campaign_fingerprint(payload["campaign_fingerprint"])
                    != self.campaign_fingerprint
                    or payload.get("campaign_fingerprint_sha256")
                    != self.campaign_fingerprint.sha256
                ):
                    raise R013CampaignError(
                        "R013 floor coordinator campaign fingerprint differs"
                    )
                state = payload.get("coordinator_state")
                if not isinstance(state, Mapping) or state.get("schema") != "step5d.autotune-v4/r013-floor-coordinator-v1":
                    raise R013CampaignError("R013 floor coordinator state receipt differs")
            elif kind == "anchor_retest_plan":
                if self.anchor_plan is not None:
                    raise R013CampaignError("R013 anchor retest plan was installed more than once")
                self.anchor_candidates = _validated_anchor_retest_candidates(payload)
                self.anchor_plan = dict(payload)
            elif kind == "local_refinement_plan":
                if self.local_refinement_plan is not None:
                    raise R013CampaignError("R013 local refinement plan was installed more than once")
                if any(dispatch_id not in completed for dispatch_id in dispatch_by_id):
                    raise R013CampaignError(
                        "R013 local refinement plan was installed with an in-flight dispatch"
                    )
                if self.stopped or self.target_achieved:
                    raise R013CampaignError(
                        "R013 local refinement plan extends a terminal campaign"
                    )
                self.local_refinement_candidates = _validated_local_refinement_candidates(payload)
                self.local_refinement_plan = dict(payload)
                self.local_refinement_plan_sequence = int(record["sequence"])
            elif kind == "high_ki_probe_plan":
                if self.high_ki_probe_plan is not None:
                    raise R013CampaignError("R013 high-Ki probe plan was installed more than once")
                if any(dispatch_id not in completed for dispatch_id in dispatch_by_id):
                    raise R013CampaignError(
                        "R013 high-Ki probe plan was installed with an in-flight dispatch"
                    )
                if self.stopped or self.target_achieved:
                    raise R013CampaignError("R013 high-Ki probe plan extends a terminal campaign")
                self.high_ki_probe_candidates = _validated_high_ki_probe_candidates(payload)
                self.high_ki_probe_plan = dict(payload)
                self.high_ki_probe_plan_sequence = int(record["sequence"])
            elif kind == "normal_velocity_gain_probe_plan":
                if self.normal_velocity_gain_probe_plan is not None:
                    raise R013CampaignError("R013 normal-velocity probe plan installed more than once")
                if any(dispatch_id not in completed for dispatch_id in dispatch_by_id):
                    raise R013CampaignError(
                        "R013 normal-velocity probe plan installed with an in-flight dispatch"
                    )
                if self.stopped or self.target_achieved:
                    raise R013CampaignError("R013 normal-velocity probe plan extends a terminal campaign")
                self.normal_velocity_gain_probe_candidates = (
                    _validated_normal_velocity_gain_probe_candidates(payload)
                )
                self.normal_velocity_gain_probe_plan = dict(payload)
                self.normal_velocity_gain_probe_plan_sequence = int(record["sequence"])
            elif kind == "normal_velocity_gain_probe_stop":
                if self.normal_velocity_gain_probe_stop is not None:
                    raise R013CampaignError("R013 normal-velocity probe stopped more than once")
                if any(dispatch_id not in completed for dispatch_id in dispatch_by_id):
                    raise R013CampaignError(
                        "R013 normal-velocity probe stopped with an in-flight dispatch"
                    )
                expected = self._normal_velocity_gain_probe_stop_payload()
                if payload != expected or any(
                    type(payload.get(key)) is not type(expected_value)
                    for key, expected_value in expected.items()
                ):
                    raise R013CampaignError("R013 normal-velocity probe stop differs")
                for key in ("admitted_dispatch_ids", "admitted_objective_n"):
                    actual_items = payload.get(key)
                    expected_items = expected[key]
                    if any(
                        type(actual_item) is not type(expected_item)
                        for actual_item, expected_item in zip(
                            actual_items,
                            expected_items,
                            strict=True,
                        )
                    ):
                        raise R013CampaignError("R013 normal-velocity probe stop differs")
                self.normal_velocity_gain_probe_stop = dict(payload)
            elif kind == "lower_p_over_d_probe_plan":
                if self.lower_p_over_d_probe_plan is not None:
                    raise R013CampaignError("R013 lower-P/D probe plan installed more than once")
                if any(dispatch_id not in completed for dispatch_id in dispatch_by_id):
                    raise R013CampaignError(
                        "R013 lower-P/D probe plan installed with an in-flight dispatch"
                    )
                if self.stopped or self.target_achieved:
                    raise R013CampaignError("R013 lower-P/D probe plan extends a terminal campaign")
                self.lower_p_over_d_probe_candidates = (
                    _validated_lower_p_over_d_probe_candidates(payload)
                )
                self.lower_p_over_d_probe_plan = dict(payload)
                self.lower_p_over_d_probe_plan_sequence = int(record["sequence"])
            elif kind == "strategy_canary_plan":
                if self.strategy_canary_plan is not None:
                    raise R013CampaignError("R013 strategy canary plan was installed more than once")
                if self.runtime_strategy.get("enabled") is not True:
                    raise R013CampaignError("R013 strategy canary requires an enabled strategy")
                if (
                    int(record["sequence"]) != 1
                    or self.dispatches
                    or self.observations
                    or self.rejected_admissions
                ):
                    raise R013CampaignError("R013 strategy canary requires a fresh campaign")
                self.strategy_canary_candidate = _validated_strategy_canary_candidate(
                    payload,
                    expected_strategy_sha256=self.runtime_strategy_sha256,
                )
                self.strategy_canary_plan = dict(payload)
                self.strategy_canary_plan_sequence = int(record["sequence"])
        outstanding = [dispatch for dispatch in self.dispatches if dispatch.dispatch_id not in completed]
        if len(outstanding) > 1:
            raise R013CampaignError("R013 serial q=1 ledger has multiple in-flight dispatches")
        self.in_flight = outstanding[0] if outstanding else None
        if len(self.observations) > len(self.dispatches):
            raise R013CampaignError("R013 observation count exceeds dispatch count")

    @property
    def exact_row_count(self) -> int:
        return len(self.observations)

    @property
    def manual_canary(self) -> bool:
        role = self.snapshot.get("campaign_role")
        return (
            isinstance(role, Mapping)
            and role.get("role") == "manual_canary"
            and role.get("formal_campaign_tell_exact") is False
        )

    @property
    def warm_slot_cursor(self) -> int:
        """Derive the next warm slot from admitted ledger rows only.

        The ordered warm plan deliberately contains repeated fixed-Ki physical
        keys and unique Sobol keys.  A persisted admitted row for a fixed key
        can therefore consume another planned repeat, while extra confirmation
        rows for a unique Sobol key are capped at its one planned slot.
        """

        plan_keys = tuple(physical_candidate_key(candidate) for candidate in self.warm)
        plan_multiplicity = Counter(plan_keys)
        admitted_by_key = Counter(
            physical_candidate_key(row["candidate"])
            for row in self.observations
            if physical_candidate_key(row["candidate"]) in plan_multiplicity
        )
        consumed_by_key = {
            key: min(count, plan_multiplicity[key])
            for key, count in admitted_by_key.items()
        }
        seen_by_key: Counter[tuple[Any, ...]] = Counter()
        for index, key in enumerate(plan_keys):
            if seen_by_key[key] >= consumed_by_key.get(key, 0):
                return index
            seen_by_key[key] += 1
        return len(plan_keys)

    @property
    def warm_plan_satisfied(self) -> bool:
        return self.warm_slot_cursor >= len(self.warm)

    @property
    def bo_trial_count(self) -> int:
        """Count only admitted BO_TRIAL observations for deterministic rounds."""

        return sum(1 for row in self.observations if row.get("kind") == "BO_TRIAL")

    @property
    def anchor_slot_cursor(self) -> int:
        """Consume an anchor only after any fresh admitted exact row at its key."""

        evaluated = set(self.evaluated_keys)
        for index, candidate in enumerate(self.anchor_candidates):
            if physical_candidate_key(candidate) not in evaluated:
                return index
        return len(self.anchor_candidates)

    @property
    def anchor_retest_summary(self) -> Mapping[str, Any]:
        return {
            "installed": self.anchor_plan is not None,
            "schema": None if self.anchor_plan is None else self.anchor_plan["schema"],
            "planned_count": len(self.anchor_candidates),
            "admitted_count": self.anchor_slot_cursor,
            "complete": self.anchor_plan is not None and self.anchor_slot_cursor == len(self.anchor_candidates),
            "historical_objectives_imported": False,
        }

    def install_anchor_retest_plan(self, value: Mapping[str, Any]) -> None:
        """Persist an explicit future-only strategy extension in the hash chain."""

        if self.anchor_plan is not None:
            raise R013CampaignError("R013 anchor retest plan is already installed")
        if self.stopped or self.target_achieved:
            raise R013CampaignError("R013 anchor retest plan cannot extend a terminal campaign")
        candidates = _validated_anchor_retest_candidates(value)
        payload = {**dict(value), "candidates": [dict(candidate) for candidate in candidates]}
        self.ledger.append("anchor_retest_plan", payload)
        self.anchor_plan = payload
        self.anchor_candidates = candidates

    @property
    def local_refinement_slot_cursor(self) -> int:
        if self.local_refinement_plan_sequence is None:
            return 0
        cursor = 0
        for row, sequence in zip(
            self.observations,
            self.observation_ledger_sequences,
            strict=True,
        ):
            if sequence <= self.local_refinement_plan_sequence:
                continue
            if row.get("kind") != LOCAL_REFINEMENT_KIND:
                continue
            if cursor >= len(self.local_refinement_candidates):
                raise R013CampaignError(
                    "R013 admitted local refinement rows exceed the plan"
                )
            expected = self.local_refinement_candidates[cursor]
            if physical_candidate_key(row["candidate"]) != physical_candidate_key(expected):
                raise R013CampaignError(
                    "R013 admitted local refinement row differs from plan order"
                )
            cursor += 1
        return cursor

    @property
    def local_refinement_summary(self) -> Mapping[str, Any]:
        return {
            "installed": self.local_refinement_plan is not None,
            "schema": None if self.local_refinement_plan is None else self.local_refinement_plan["schema"],
            "planned_count": len(self.local_refinement_candidates),
            "admitted_count": self.local_refinement_slot_cursor,
            "complete": (
                self.local_refinement_plan is not None
                and self.local_refinement_slot_cursor == len(self.local_refinement_candidates)
            ),
            "historical_objectives_imported": False,
        }

    def install_local_refinement_plan(self, value: Mapping[str, Any]) -> None:
        if self.local_refinement_plan is not None:
            raise R013CampaignError("R013 local refinement plan is already installed")
        if self.in_flight is not None:
            raise R013CampaignError("R013 local refinement requires no in-flight dispatch")
        if self.stopped or self.target_achieved:
            raise R013CampaignError("R013 local refinement cannot extend a terminal campaign")
        candidates = _validated_local_refinement_candidates(value)
        payload = {**dict(value), "candidates": [dict(candidate) for candidate in candidates]}
        record = self.ledger.append("local_refinement_plan", payload)
        self.local_refinement_plan = payload
        self.local_refinement_candidates = candidates
        self.local_refinement_plan_sequence = int(record["sequence"])

    @property
    def high_ki_probe_slot_cursor(self) -> int:
        if self.high_ki_probe_plan_sequence is None:
            return 0
        cursor = 0
        for row, sequence in zip(
            self.observations,
            self.observation_ledger_sequences,
            strict=True,
        ):
            if sequence <= self.high_ki_probe_plan_sequence or row.get("kind") != HIGH_KI_PROBE_KIND:
                continue
            if cursor >= len(self.high_ki_probe_candidates):
                raise R013CampaignError("R013 admitted high-Ki rows exceed the plan")
            expected = self.high_ki_probe_candidates[cursor]
            if physical_candidate_key(row["candidate"]) != physical_candidate_key(expected):
                raise R013CampaignError("R013 admitted high-Ki row differs from plan order")
            cursor += 1
        return cursor

    @property
    def high_ki_probe_summary(self) -> Mapping[str, Any]:
        return {
            "installed": self.high_ki_probe_plan is not None,
            "schema": None if self.high_ki_probe_plan is None else self.high_ki_probe_plan["schema"],
            "planned_count": len(self.high_ki_probe_candidates),
            "admitted_count": self.high_ki_probe_slot_cursor,
            "complete": (
                self.high_ki_probe_plan is not None
                and self.high_ki_probe_slot_cursor == len(self.high_ki_probe_candidates)
            ),
            "baseline_ki_max": BASELINE_KI_MAX,
            "extended_ki_max": EXTENDED_KI_MAX,
            "i_term_authority_fraction_max": 0.5,
            "historical_objectives_imported": False,
        }

    def install_high_ki_probe_plan(self, value: Mapping[str, Any]) -> None:
        if self.high_ki_probe_plan is not None:
            raise R013CampaignError("R013 high-Ki probe plan is already installed")
        if self.in_flight is not None:
            raise R013CampaignError("R013 high-Ki probe requires no in-flight dispatch")
        if self.stopped or self.target_achieved:
            raise R013CampaignError("R013 high-Ki probe cannot extend a terminal campaign")
        candidates = _validated_high_ki_probe_candidates(value)
        payload = {**dict(value), "candidates": [dict(candidate) for candidate in candidates]}
        record = self.ledger.append("high_ki_probe_plan", payload)
        self.high_ki_probe_plan = payload
        self.high_ki_probe_candidates = candidates
        self.high_ki_probe_plan_sequence = int(record["sequence"])

    @property
    def normal_velocity_gain_probe_slot_cursor(self) -> int:
        if self.normal_velocity_gain_probe_plan_sequence is None:
            return 0
        cursor = 0
        for row, sequence in zip(
            self.observations,
            self.observation_ledger_sequences,
            strict=True,
        ):
            if (
                sequence <= self.normal_velocity_gain_probe_plan_sequence
                or row.get("kind") != NORMAL_VELOCITY_GAIN_PROBE_KIND
            ):
                continue
            if cursor >= len(self.normal_velocity_gain_probe_candidates):
                raise R013CampaignError("R013 admitted normal-velocity rows exceed the plan")
            expected = self.normal_velocity_gain_probe_candidates[cursor]
            if physical_candidate_key(row["candidate"]) != physical_candidate_key(expected):
                raise R013CampaignError("R013 admitted normal-velocity row differs from plan order")
            cursor += 1
        return cursor

    def _normal_velocity_gain_probe_rows(self) -> tuple[Mapping[str, Any], ...]:
        if self.normal_velocity_gain_probe_plan_sequence is None:
            return ()
        return tuple(
            row
            for row, sequence in zip(
                self.observations,
                self.observation_ledger_sequences,
                strict=True,
            )
            if sequence > self.normal_velocity_gain_probe_plan_sequence
            and row.get("kind") == NORMAL_VELOCITY_GAIN_PROBE_KIND
        )

    def _normal_velocity_gain_probe_stop_payload(self) -> dict[str, Any]:
        if self.normal_velocity_gain_probe_plan is None:
            raise R013CampaignError("R013 normal-velocity probe stop requires its plan")
        rows = self._normal_velocity_gain_probe_rows()
        if len(rows) < 2:
            raise R013CampaignError("R013 normal-velocity probe stop requires two admitted rows")
        objectives = [float(row["objective_n"]) for row in rows]
        if objectives[1] <= objectives[0] or objectives[1] - objectives[0] < 0.5:
            raise R013CampaignError("R013 normal-velocity probe stop rule is not satisfied")
        return {
            "schema": NORMAL_VELOCITY_GAIN_PROBE_STOP_SCHEMA,
            "version": NORMAL_VELOCITY_GAIN_PROBE_STOP_VERSION,
            "plan_schema": NORMAL_VELOCITY_GAIN_PROBE_PLAN_SCHEMA,
            "action": "skip_remaining_plan_return_to_scheduler",
            "reason": "matched_i_over_d_p_over_d_monotonic_worsening",
            "minimum_admitted_rows": 2,
            "minimum_last_minus_first_n": 0.5,
            "admitted_count_at_stop": len(rows),
            "admitted_dispatch_ids": [str(row["dispatch_id"]) for row in rows],
            "admitted_objective_n": objectives,
        }

    @property
    def normal_velocity_gain_probe_summary(self) -> Mapping[str, Any]:
        return {
            "installed": self.normal_velocity_gain_probe_plan is not None,
            "schema": (
                None
                if self.normal_velocity_gain_probe_plan is None
                else self.normal_velocity_gain_probe_plan["schema"]
            ),
            "planned_count": len(self.normal_velocity_gain_probe_candidates),
            "admitted_count": self.normal_velocity_gain_probe_slot_cursor,
            "complete": (
                self.normal_velocity_gain_probe_plan is not None
                and self.normal_velocity_gain_probe_slot_cursor
                == len(self.normal_velocity_gain_probe_candidates)
            ),
            "stopped": self.normal_velocity_gain_probe_stop is not None,
            "stop_reason": (
                None
                if self.normal_velocity_gain_probe_stop is None
                else self.normal_velocity_gain_probe_stop["reason"]
            ),
            "historical_objectives_imported": False,
        }

    def install_normal_velocity_gain_probe_plan(self, value: Mapping[str, Any]) -> None:
        if self.normal_velocity_gain_probe_plan is not None:
            raise R013CampaignError("R013 normal-velocity probe plan is already installed")
        if self.in_flight is not None:
            raise R013CampaignError("R013 normal-velocity probe requires no in-flight dispatch")
        if self.stopped or self.target_achieved:
            raise R013CampaignError("R013 normal-velocity probe cannot extend a terminal campaign")
        candidates = _validated_normal_velocity_gain_probe_candidates(value)
        payload = {**dict(value), "candidates": [dict(candidate) for candidate in candidates]}
        record = self.ledger.append("normal_velocity_gain_probe_plan", payload)
        self.normal_velocity_gain_probe_plan = payload
        self.normal_velocity_gain_probe_candidates = candidates
        self.normal_velocity_gain_probe_plan_sequence = int(record["sequence"])

    def stop_normal_velocity_gain_probe(self) -> Mapping[str, Any]:
        if self.normal_velocity_gain_probe_stop is not None:
            raise R013CampaignError("R013 normal-velocity probe is already stopped")
        if self.in_flight is not None:
            raise R013CampaignError("R013 normal-velocity probe stop requires no in-flight dispatch")
        if self.stopped or self.target_achieved:
            raise R013CampaignError("R013 normal-velocity probe stop cannot alter a terminal campaign")
        payload = self._normal_velocity_gain_probe_stop_payload()
        self.ledger.append("normal_velocity_gain_probe_stop", payload)
        self.normal_velocity_gain_probe_stop = payload
        return payload

    @property
    def lower_p_over_d_probe_slot_cursor(self) -> int:
        if self.lower_p_over_d_probe_plan_sequence is None:
            return 0
        cursor = 0
        for row, sequence in zip(
            self.observations,
            self.observation_ledger_sequences,
            strict=True,
        ):
            if (
                sequence <= self.lower_p_over_d_probe_plan_sequence
                or row.get("kind") != LOWER_P_OVER_D_PROBE_KIND
            ):
                continue
            if cursor >= len(self.lower_p_over_d_probe_candidates):
                raise R013CampaignError("R013 admitted lower-P/D rows exceed the plan")
            expected = self.lower_p_over_d_probe_candidates[cursor]
            if physical_candidate_key(row["candidate"]) != physical_candidate_key(expected):
                raise R013CampaignError("R013 admitted lower-P/D row differs from plan order")
            cursor += 1
        return cursor

    @property
    def lower_p_over_d_probe_summary(self) -> Mapping[str, Any]:
        return {
            "installed": self.lower_p_over_d_probe_plan is not None,
            "schema": (
                None
                if self.lower_p_over_d_probe_plan is None
                else self.lower_p_over_d_probe_plan["schema"]
            ),
            "planned_count": len(self.lower_p_over_d_probe_candidates),
            "admitted_count": self.lower_p_over_d_probe_slot_cursor,
            "complete": (
                self.lower_p_over_d_probe_plan is not None
                and self.lower_p_over_d_probe_slot_cursor
                == len(self.lower_p_over_d_probe_candidates)
            ),
            "historical_objectives_imported": False,
        }

    def install_lower_p_over_d_probe_plan(self, value: Mapping[str, Any]) -> None:
        if self.lower_p_over_d_probe_plan is not None:
            raise R013CampaignError("R013 lower-P/D probe plan is already installed")
        if self.in_flight is not None:
            raise R013CampaignError("R013 lower-P/D probe requires no in-flight dispatch")
        if self.stopped or self.target_achieved:
            raise R013CampaignError("R013 lower-P/D probe cannot extend a terminal campaign")
        candidates = _validated_lower_p_over_d_probe_candidates(value)
        payload = {**dict(value), "candidates": [dict(candidate) for candidate in candidates]}
        record = self.ledger.append("lower_p_over_d_probe_plan", payload)
        self.lower_p_over_d_probe_plan = payload
        self.lower_p_over_d_probe_candidates = candidates
        self.lower_p_over_d_probe_plan_sequence = int(record["sequence"])

    def install_strategy_canary_plan(self, value: Mapping[str, Any]) -> None:
        if self.strategy_canary_plan is not None:
            raise R013CampaignError("R013 strategy canary plan is already installed")
        if self.runtime_strategy.get("enabled") is not True:
            raise R013CampaignError("R013 strategy canary requires an enabled strategy")
        if (
            len(self.ledger.records) != 1
            or self.in_flight is not None
            or self.dispatches
            or self.observations
            or self.rejected_admissions
        ):
            raise R013CampaignError("R013 strategy canary requires a fresh campaign")
        candidate = _validated_strategy_canary_candidate(
            value,
            expected_strategy_sha256=self.runtime_strategy_sha256,
        )
        payload = {**dict(value), "candidate": dict(candidate)}
        record = self.ledger.append("strategy_canary_plan", payload)
        self.strategy_canary_plan = payload
        self.strategy_canary_candidate = candidate
        self.strategy_canary_plan_sequence = int(record["sequence"])

    @property
    def strategy_canary_admitted_count(self) -> int:
        if self.strategy_canary_candidate is None:
            return 0
        key = physical_candidate_key(self.strategy_canary_candidate)
        return sum(
            1
            for row in self.observations
            if row.get("kind") in {STRATEGY_CANARY_KIND, CONFIRMATION_KIND}
            and physical_candidate_key(row["candidate"]) == key
        )

    @property
    def strategy_canary_physical_attempt_count(self) -> int:
        if self.strategy_canary_candidate is None:
            return 0
        key = physical_candidate_key(self.strategy_canary_candidate)
        return sum(
            1
            for dispatch in self.dispatches
            if dispatch.kind in {STRATEGY_CANARY_KIND, CONFIRMATION_KIND}
            and physical_candidate_key(dispatch.candidate) == key
        )

    @property
    def strategy_canary_terminal(self) -> bool:
        if self.strategy_canary_plan is None:
            return False
        return (
            self.strategy_canary_admitted_count
            >= int(self.strategy_canary_plan["required_admitted_rows"])
            or self.strategy_canary_physical_attempt_count
            >= int(self.strategy_canary_plan["maximum_physical_attempts"])
        )

    @property
    def strategy_canary_summary(self) -> Mapping[str, Any]:
        installed = self.strategy_canary_plan is not None
        required = (
            0 if not installed else int(self.strategy_canary_plan["required_admitted_rows"])
        )
        maximum = (
            0 if not installed else int(self.strategy_canary_plan["maximum_physical_attempts"])
        )
        admitted = self.strategy_canary_admitted_count
        attempts = self.strategy_canary_physical_attempt_count
        rejected = 0
        if self.strategy_canary_candidate is not None:
            key = physical_candidate_key(self.strategy_canary_candidate)
            rejected = sum(
                1
                for row in self.rejected_admissions
                if physical_candidate_key(row["candidate"]) == key
                and row.get("physical_admission", {}).get("dispatch_id")
            )
        return {
            "installed": installed,
            "schema": None if not installed else self.strategy_canary_plan["schema"],
            "runtime_strategy_sha256": (
                None if not installed else self.runtime_strategy_sha256
            ),
            "candidate": (
                None if self.strategy_canary_candidate is None else dict(self.strategy_canary_candidate)
            ),
            "required_admitted_rows": required,
            "maximum_physical_attempts": maximum,
            "admitted_count": admitted,
            "physical_attempt_count": attempts,
            "rejected_attempt_count": rejected,
            "complete": installed and admitted >= required,
            "attempt_limit_exhausted": installed and attempts >= maximum and admitted < required,
            "terminal": self.strategy_canary_terminal,
            "target_achieved": self.target_achieved,
            "historical_objectives_imported": False,
        }

    @property
    def evaluated_keys(self) -> tuple[tuple[Any, ...], ...]:
        return tuple(physical_candidate_key(row["candidate"]) for row in self.observations)

    def confirmation_summary(self) -> ConfirmationSummary:
        grouped: dict[tuple[Any, ...], list[Mapping[str, Any]]] = defaultdict(list)
        trigger_order: dict[tuple[Any, ...], int] = {}
        trigger_rows: dict[tuple[Any, ...], Mapping[str, Any]] = {}
        for observation_index, row in enumerate(self.observations):
            key = physical_candidate_key(row["candidate"])
            grouped[key].append(row)
            if (
                float(row["objective_n"]) <= self.confirmation_target.sealed_mae_threshold_n
                and len(grouped[key]) < self.confirmation_target.minimum_admitted_repeats
            ):
                trigger_order.setdefault(key, observation_index)
                trigger_rows.setdefault(key, row)
        summaries: list[CandidateRepeatSummary] = []
        for key, rows in grouped.items():
            objectives = [float(row["objective_n"]) for row in rows]
            arithmetic_mean = math.fsum(objectives) / len(objectives)
            candidate = dict(rows[0]["candidate"])
            trigger_row = trigger_rows.get(key)
            admitted_uids = tuple(
                str(row.get("physical_admission", {}).get("observation_uid", ""))
                for row in rows
            )
            confirmation_complete = (
                len(rows) >= self.confirmation_target.minimum_admitted_repeats
            )
            summaries.append(
                CandidateRepeatSummary(
                    candidate=candidate,
                    physical_key=key,
                    admitted_exact_count=len(rows),
                    minimum_sealed_mae_n=min(objectives),
                    arithmetic_mean_sealed_mae_n=arithmetic_mean,
                    sealed_mae_n_values=tuple(objectives),
                    admitted_dispatch_ids=tuple(str(row["dispatch_id"]) for row in rows),
                    admitted_observation_uids=admitted_uids,
                    trigger_dispatch_id=(
                        None if trigger_row is None else str(trigger_row["dispatch_id"])
                    ),
                    confirmation_complete=confirmation_complete,
                    confirmed=(
                        confirmation_complete
                        and arithmetic_mean
                        <= self.confirmation_target.sealed_mae_threshold_n
                    ),
                )
            )
        ordered = tuple(sorted(summaries, key=lambda row: (row.arithmetic_mean_sealed_mae_n, repr(row.physical_key))))
        pending = next(
            (
                row for row in sorted(
                    ordered,
                    key=lambda value: (
                        trigger_order.get(value.physical_key, len(self.observations)),
                        repr(value.physical_key),
                    ),
                )
                if row.physical_key in trigger_order
                and not row.confirmation_complete
            ),
            None,
        )
        confirmed = next((row for row in ordered if row.target_achieved), None)
        minimum = min(
            (float(row["objective_n"]) for row in self.observations),
            default=None,
        )
        return ConfirmationSummary(
            target=self.confirmation_target,
            candidate_summaries=ordered,
            single_trial_minimum_sealed_mae_n=minimum,
            pending_confirmation=pending,
            confirmed_incumbent=confirmed,
        )

    @property
    def target_achieved(self) -> bool:
        """Typed success predicate derived only from admitted ledger rows."""

        # bounded_bo_v1 deliberately treats the historical 0.35 N threshold as
        # a checkpoint.  It must never terminate the fresh 100-attempt BO lane.
        if self.bounded_bo_profile is not None:
            return False

        if self.floor_coordinator is not None:
            return self.floor_coordinator.complete
        if self.completion_policy.policy == BUDGETED_FLOOR_V1:
            return self.novel_count >= self.completion_policy.novel_exact_candidate_target
        return self.confirmation_summary().target_achieved

    @property
    def physical_attempt_count(self) -> int:
        """Count every serial dispatch, including rejected/ineligible receipts."""

        return len(self.dispatches)

    @property
    def bounded_bo_budget_exhausted(self) -> bool:
        profile = self.bounded_bo_profile
        return profile is not None and self.physical_attempt_count >= int(
            profile["physical_attempt_budget"]
        )

    @property
    def target_checkpoint(self) -> bool:
        return any(
            float(row["objective_n"]) <= self.completion_policy.target_mae_n
            for row in self.observations
        )

    @property
    def novel_count(self) -> int:
        if self.floor_coordinator is not None:
            return self.floor_coordinator.novel_count
        keys = {
            physical_candidate_key(row["candidate"])
            for row in self.observations
            if row.get("kind") not in NOVEL_EXCLUDED_KINDS
        }
        return len(keys)

    @property
    def completion_status(self) -> Mapping[str, Any]:
        if self.bounded_bo_profile is not None:
            return {
                "schema": COMPLETION_POLICY_SCHEMA,
                "version": COMPLETION_POLICY_VERSION,
                "policy": BOUNDED_BO_POLICY,
                "target_mae_n": CONFIRMATION_THRESHOLD_N,
                "target_checkpoint": self.target_checkpoint,
                "novel_count": self.novel_count,
                "physical_attempt_count": self.physical_attempt_count,
                "physical_attempt_budget": self.bounded_bo_profile["physical_attempt_budget"],
                "budget_exhausted": self.bounded_bo_budget_exhausted,
                "checkpoint_only_target": True,
                "complete": self.bounded_bo_budget_exhausted,
            }
        if self.floor_coordinator is not None:
            return {
                **self.floor_coordinator.snapshot(),
                "schema": COMPLETION_POLICY_SCHEMA,
                "version": COMPLETION_POLICY_VERSION,
                "policy": self.completion_policy.policy,
                "target_mae_n": self.completion_policy.target_mae_n,
                "target_checkpoint": self.floor_coordinator.target_checkpoint,
                "novel_count": self.floor_coordinator.novel_count,
                "novel_exact_candidate_target": self.completion_policy.novel_exact_candidate_target,
                "complete": self.floor_coordinator.complete,
            }
        return {
            "schema": COMPLETION_POLICY_SCHEMA,
            "version": COMPLETION_POLICY_VERSION,
            "policy": self.completion_policy.policy,
            "target_mae_n": self.completion_policy.target_mae_n,
            "target_checkpoint": self.target_checkpoint,
            "novel_count": self.novel_count,
            "novel_exact_candidate_target": self.completion_policy.novel_exact_candidate_target,
            "complete": self.target_achieved,
        }

    @property
    def floor_discovery_status(self) -> Mapping[str, Any] | None:
        return None if self.floor_coordinator is None else self.floor_coordinator.snapshot()

    def _persist_floor_coordinator_events(self) -> None:
        if self.floor_coordinator is None:
            return
        for event in self.floor_coordinator.drain_events():
            self.ledger.append(
                "floor_coordinator",
                {
                    **dict(event),
                    "campaign_fingerprint": self.campaign_fingerprint.as_dict(),
                    "campaign_fingerprint_sha256": self.campaign_fingerprint.sha256,
                    "coordinator_state": self.floor_coordinator.snapshot(),
                },
            )
        self.snapshot["floor_discovery_state"] = self.floor_coordinator.snapshot()

    def _floor_core_proposal(
        self,
        pool: Sequence[dict[str, Any]],
    ) -> Mapping[str, Any] | FloorCandidateProposal:
        fit = fit_core_production_gp(self.observations, config=self.gp_config)
        proposal = ask_core_qlognei(fit, pool)
        return FloorCandidateProposal(
            candidate=proposal.candidate,
            block_input=tuple(
                candidate_to_log_features(proposal.candidate)[index]
                for index in (0, 1, 2, 5)
            ),
            contract=CORE_PROPOSAL_CONTRACT,
            acquisition_value=proposal.acquisition_value,
            posterior_beating_probability=proposal.posterior_beating_probability,
            incumbent_threshold_n=proposal.incumbent_threshold_n,
            fit_receipt=proposal.as_dict(),
        )

    def _persist_dispatch(
        self,
        candidate: Mapping[str, Any],
        *,
        kind: str,
        abort_allowed: bool,
        floor_trial: Mapping[str, Any] | None = None,
    ) -> Dispatch:
        if self.in_flight is not None:
            raise R013CampaignError("R013 serial q=1 already has an in-flight dispatch")
        ordinal = len(self.dispatches) + 1
        token = candidate_token(candidate)
        dispatch = Dispatch(
            f"r013-{ordinal:04d}-{token[:12]}", kind, ordinal, dict(candidate), token,
            abort_allowed, self.runtime_strategy_sha256, self.campaign_fingerprint.as_dict(),
            None if floor_trial is None else dict(floor_trial),
        )
        self.ledger.append("dispatch", dispatch.as_dict())
        self.dispatches.append(dispatch)
        self.in_flight = dispatch
        return dispatch

    def ask(self) -> tuple[Dispatch, CandidateProposal | None]:
        if self.manual_canary:
            raise R013CampaignError(
                "R013 manual canary cannot enter optimizer ask/tell"
            )
        if self.stopped:
            raise R013CampaignError("R013 campaign stopped after a hard guard")
        if self.bounded_bo_budget_exhausted:
            raise R013CampaignError(
                "R013 bounded BO physical-attempt budget exhausted: "
                f"{self.physical_attempt_count}/{self.bounded_bo_profile['physical_attempt_budget']}"
            )
        summary = self.confirmation_summary()
        if self.target_achieved:
            if self.completion_policy.policy == BUDGETED_FLOOR_V1:
                raise R013CampaignError(
                    "R013 budgeted_floor_v1 complete: "
                    f"novel_count={self.novel_count}"
                )
            evidence = summary.confirmed_incumbent
            raise R013CampaignError(
                "R013 target_achieved: no further ask is permitted; "
                f"candidate={evidence.physical_key!r} "
                f"admitted={evidence.admitted_exact_count} "
                f"mean={evidence.arithmetic_mean_sealed_mae_n:.6f} N"
            )
        if self.in_flight is not None:
            raise R013CampaignError("R013 serial q=1 dispatch remains in flight")
        if self.runtime_strategy.get("enabled") is True and self.strategy_canary_plan is None:
            raise R013CampaignError("R013 enabled strategy lacks its fresh canary plan")
        if self.strategy_canary_terminal and not (
            self.bounded_bo_profile is not None
            and not self.strategy_canary_summary["attempt_limit_exhausted"]
        ):
            raise R013CampaignError("R013 strategy canary is terminal; no warm or BO dispatch is permitted")
        if (
            self.completion_policy.policy != BUDGETED_FLOOR_V1
            and self.bounded_bo_profile is None
            and summary.pending_confirmation is not None
        ):
            return self._persist_dispatch(
                summary.pending_confirmation.candidate,
                kind=self.confirmation_target.confirmation_kind,
                abort_allowed=False,
            ), None
        if self.strategy_canary_plan is not None and not (
            self.bounded_bo_profile is not None
            and self.strategy_canary_terminal
            and not self.strategy_canary_summary["attempt_limit_exhausted"]
        ):
            assert self.strategy_canary_candidate is not None
            dispatch = self._persist_dispatch(
                self.strategy_canary_candidate,
                kind=STRATEGY_CANARY_KIND,
                abort_allowed=False,
            )
            return dispatch, None
        if self.floor_coordinator is not None:
            try:
                request = self.floor_coordinator.next_request()
            except RuntimePrimitiveNotInstalled as exc:
                self._persist_floor_coordinator_events()
                raise R013CampaignError(str(exc)) from exc
            self._persist_floor_coordinator_events()
            if not request.executable or request.runtime_candidate is None:
                self.floor_coordinator.mark_runtime_primitive_not_installed(request)
                self._persist_floor_coordinator_events()
                raise R013CampaignError(
                    "runtime_primitive_not_installed: "
                    f"floor role {request.role} requires its Outcome-4 runtime primitive"
                )
            proposal: FloorCandidateProposal | None = None
            if request.proposal_contract.block == "controller_core":
                receipt = request.proposal_receipt or {}
                proposal = FloorCandidateProposal(
                    candidate=request.runtime_candidate,
                    block_input=tuple(
                        candidate_to_log_features(request.runtime_candidate)[index]
                        for index in (0, 1, 2, 5)
                    ),
                    contract=request.proposal_contract,
                    acquisition_value=float(receipt.get("acquisition_value", 0.0)),
                    posterior_beating_probability=float(
                        receipt.get("posterior_beating_probability", request.posterior_beating_probability)
                    ),
                    incumbent_threshold_n=receipt.get("incumbent_threshold_n"),
                    fit_receipt=receipt.get("fit_receipt"),
                )
            dispatch = self._persist_dispatch(
                request.runtime_candidate,
                kind=request.role,
                abort_allowed=request.role == CORE_BO_NOVEL,
                floor_trial=request.trial.as_dict(),
            )
            return dispatch, proposal
        warm_slot = self.warm_slot_cursor
        if warm_slot < len(self.warm):
            candidate = self.warm[warm_slot]
            fixed_warm_slots = len(FIXED_KI_SEEDS) * FIXED_KI_REPEATS
            kind = "WARM_FIXED_KI" if warm_slot < fixed_warm_slots else "WARM_SOBOL"
            return self._persist_dispatch(candidate, kind=kind, abort_allowed=False), None
        anchor_slot = self.anchor_slot_cursor
        if anchor_slot < len(self.anchor_candidates):
            return self._persist_dispatch(
                self.anchor_candidates[anchor_slot],
                kind=ANCHOR_RETEST_KIND,
                abort_allowed=False,
            ), None
        refinement_slot = self.local_refinement_slot_cursor
        if refinement_slot < len(self.local_refinement_candidates):
            return self._persist_dispatch(
                self.local_refinement_candidates[refinement_slot],
                kind=LOCAL_REFINEMENT_KIND,
                abort_allowed=False,
            ), None
        high_ki_slot = self.high_ki_probe_slot_cursor
        if high_ki_slot < len(self.high_ki_probe_candidates):
            return self._persist_dispatch(
                self.high_ki_probe_candidates[high_ki_slot],
                kind=HIGH_KI_PROBE_KIND,
                abort_allowed=False,
            ), None
        velocity_slot = self.normal_velocity_gain_probe_slot_cursor
        if (
            self.normal_velocity_gain_probe_stop is None
            and velocity_slot < len(self.normal_velocity_gain_probe_candidates)
        ):
            return self._persist_dispatch(
                self.normal_velocity_gain_probe_candidates[velocity_slot],
                kind=NORMAL_VELOCITY_GAIN_PROBE_KIND,
                abort_allowed=False,
            ), None
        lower_p_over_d_slot = self.lower_p_over_d_probe_slot_cursor
        if lower_p_over_d_slot < len(self.lower_p_over_d_probe_candidates):
            return self._persist_dispatch(
                self.lower_p_over_d_probe_candidates[lower_p_over_d_slot],
                kind=LOWER_P_OVER_D_PROBE_KIND,
                abort_allowed=False,
            ), None
        fit = fit_production_gp(self.observations, config=self.gp_config)
        pool_state = {
            "schema": CANDIDATE_POOL_STATE_SCHEMA,
            "version": 1,
            "sobol_seed": WARM_START_SEED + 1,
            "round_index": self.bo_trial_count,
            "cursor": self.bo_trial_count * CANDIDATE_POOL_SIZE,
            "pool_size": CANDIDATE_POOL_SIZE,
        }
        fit_receipt = {**dict(fit.fit_receipt), "candidate_pool_state": pool_state}
        self.ledger.append("fit_receipt", fit_receipt)
        self.fit_receipts.append(fit_receipt)
        self.candidate_pool_state = dict(pool_state)
        self.snapshot["candidate_pool_state"] = dict(pool_state)
        proposal = ask_qlognei(
            fit,
            candidate_pool(
                round_index=self.bo_trial_count,
                evaluated_keys=self.evaluated_keys,
                pending_keys=(
                    ()
                    if self.in_flight is None
                    else (physical_candidate_key(self.in_flight.candidate),)
                ),
            ),
            evaluated_keys=self.evaluated_keys,
        )
        return self._persist_dispatch(
            proposal.candidate,
            kind="BO_TRIAL",
            abort_allowed=True,
        ), proposal

    def tell_exact(
        self,
        *,
        admission: PhysicalAdmissionReceipt,
        anti_windup_metrics: Mapping[str, Any],
        runtime_strategy_receipt: Mapping[str, Any] | None = None,
        runtime_strategy_sidecar_sha256: str | None = None,
    ) -> Mapping[str, Any]:
        if self.manual_canary:
            raise R013CampaignError(
                "R013 manual canary cannot enter optimizer ask/tell"
            )
        if self.in_flight is None:
            raise R013CampaignError("R013 tell requires an in-flight dispatch")
        _validate_admission(
            self.in_flight,
            admission,
            expected_fingerprint=self.campaign_fingerprint,
            strict=self.strict_admission,
        )
        parsed_strategy_receipt: Mapping[str, Any] | None = None
        parsed_sidecar_sha: str | None = None
        if self.runtime_strategy.get("enabled") is True:
            parsed_strategy_receipt = _validated_runtime_strategy_receipt(
                runtime_strategy_receipt or {},
                expected_sha256=self.runtime_strategy_sha256,
            )
            if (
                type(runtime_strategy_sidecar_sha256) is not str
                or len(runtime_strategy_sidecar_sha256) != 64
                or any(c not in "0123456789abcdef" for c in runtime_strategy_sidecar_sha256)
            ):
                raise R013CampaignError("R013 runtime strategy sidecar identity differs")
            parsed_sidecar_sha = runtime_strategy_sidecar_sha256
        admitted_exact = admission.strict_admitted_exact if self.strict_admission else admission.admitted_exact
        if not admitted_exact:
            reasons = _admission_rejection_reasons(
                admission,
                strict=self.strict_admission,
            )
            rejected = {
                "schema": REJECTED_ADMISSION_SCHEMA,
                "dispatch_id": self.in_flight.dispatch_id,
                "candidate": dict(self.in_flight.candidate),
                "candidate_token": self.in_flight.candidate_token,
                "physical_admission": admission.as_dict(),
                "campaign_fingerprint": self.campaign_fingerprint.as_dict(),
                **(
                    {}
                    if parsed_strategy_receipt is None
                    else {
                        "runtime_strategy_receipt": dict(parsed_strategy_receipt),
                        "runtime_strategy_sidecar_sha256": parsed_sidecar_sha,
                    }
                ),
                "status": "rejected_ineligible",
                "reasons": reasons,
                "exact_observation_added": False,
            }
            self.ledger.append("rejected", rejected)
            self.rejected_admissions.append(rejected)
            if self.floor_coordinator is not None and self.in_flight.floor_trial is not None:
                self.floor_coordinator.record_result(
                    admitted=False,
                    sealed_mae_n=float(admission.sealed_mae_n),
                    complete=False,
                    sealed=admission.sealed,
                    failure_signature="|".join(reasons) or "rejected_ineligible",
                )
                self._persist_floor_coordinator_events()
            self.in_flight = None
            return rejected
        row = exact_observation(
            self.in_flight,
            admission=admission,
            observation_variance_n2=self.gp_config.noise_floor_n2,
            anti_windup_metrics=anti_windup_metrics,
            runtime_strategy_receipt=parsed_strategy_receipt,
            runtime_strategy_sidecar_sha256=parsed_sidecar_sha,
            expected_fingerprint=self.campaign_fingerprint,
            strict=self.strict_admission,
        )
        if self.in_flight.floor_trial is not None:
            row["floor_trial"] = dict(self.in_flight.floor_trial)
            row["floor_trial_token"] = str(
                self.in_flight.floor_trial.get("canonical_token", "")
            )
            row["floor_trial_key"] = list(
                self.in_flight.floor_trial.get("canonical_key", ())
            )
        record = self.ledger.append("observation", row)
        self.observations.append(row)
        self.observation_ledger_sequences.append(int(record["sequence"]))
        if self.floor_coordinator is not None and self.in_flight.floor_trial is not None:
            self.floor_coordinator.record_result(
                admitted=True,
                sealed_mae_n=float(admission.sealed_mae_n),
                complete=True,
                sealed=admission.sealed,
                receipt_id=admission.observation_uid,
            )
            self._persist_floor_coordinator_events()
        self.in_flight = None
        return row

    def tell_hard_guard(self, *, reason: str) -> Mapping[str, Any]:
        if self.in_flight is None:
            raise R013CampaignError("R013 hard guard requires an in-flight dispatch")
        if not str(reason).strip():
            raise R013CampaignError("R013 hard guard requires a reason")
        payload = {
            "schema": "step5d.autotune-v4/r013-hard-guard-v1",
            "dispatch_id": self.in_flight.dispatch_id,
            "candidate_token": self.in_flight.candidate_token,
            "reason": str(reason),
            "action": "stop_no_retry",
        }
        self.ledger.append("hard_guard", payload)
        self.in_flight = None
        self.stopped = True
        return payload


__all__ = [
    "ANCHOR_RETEST_CANDIDATES", "ANCHOR_RETEST_KIND", "ANCHOR_RETEST_PLAN_SCHEMA",
    "LOCAL_REFINEMENT_CANDIDATES", "LOCAL_REFINEMENT_KIND", "LOCAL_REFINEMENT_PLAN_SCHEMA",
    "HIGH_KI_PROBE_CANDIDATES", "HIGH_KI_PROBE_KIND", "HIGH_KI_PROBE_PLAN_SCHEMA",
    "NORMAL_VELOCITY_GAIN_PROBE_CANDIDATES", "NORMAL_VELOCITY_GAIN_PROBE_KIND",
    "NORMAL_VELOCITY_GAIN_PROBE_PLAN_SCHEMA",
    "LOWER_P_OVER_D_PROBE_CANDIDATES", "LOWER_P_OVER_D_PROBE_KIND",
    "LOWER_P_OVER_D_PROBE_PLAN_SCHEMA",
    "STRATEGY_CANARY_CANDIDATE", "STRATEGY_CANARY_KIND",
    "STRATEGY_CANARY_MAX_PHYSICAL_ATTEMPTS", "STRATEGY_CANARY_PLAN_SCHEMA",
    "STRATEGY_CANARY_REQUIRED_ADMITTED",
    "ANCHOR_RETEST_PLAN_VERSION", "ANTI_WINDUP_CONTRACT", "CANDIDATE_POOL_MAX_ROUNDS", "Campaign",
    "BUDGETED_FLOOR_V1", "BUDGETED_FLOOR_NOVEL_TARGET", "CANDIDATE_POOL_STATE_SCHEMA",
    "COMPLETION_POLICY_SCHEMA", "COMPLETION_POLICY_VERSION", "CompletionPolicy",
    "CandidateRepeatSummary", "ConfirmationSummary", "ConfirmationTarget",
    "CONFIRMATION_KIND", "CONFIRMATION_MIN_REPEATS", "CONFIRMATION_TARGET_SCHEMA",
    "CONFIRMATION_TARGET_VERSION", "CONFIRMATION_THRESHOLD_N", "DEFAULT_CONFIRMATION_TARGET",
    "Dispatch", "FIXED_KI_REPEATS", "FIXED_KI_SEEDS", "LEGACY_THRESHOLD_COMPLETION_POLICY",
    "MIN_EXACT_ROWS_FOR_BO", "CampaignFingerprint",
    "PhysicalAdmissionReceipt", "PHYSICAL_ADMISSION_SCHEMA", "R013CampaignError",
    "REJECTED_ADMISSION_SCHEMA", "SOBOL_WARM_ROWS", "WARM_START_SEED",
    "anchor_retest_plan", "candidate_pool", "candidate_token", "local_refinement_plan",
    "high_ki_probe_plan",
    "normal_velocity_gain_probe_plan",
    "lower_p_over_d_probe_plan",
    "strategy_canary_plan",
    "TRIAL_ANTI_WINDUP_SCHEMA", "exact_observation",
    "optimizer_snapshot", "r012_seed_source_from_ledger", "select_seed_template",
    "validate_trial_anti_windup_metrics", "warm_start_candidates",
    "FloorDiscoveryCoordinator", "FloorDiscoveryPolicyV1", "FloorTrialRequest", "FloorTrialSpec",
    "FloorCandidateProposal", "FloorCoordinatorError", "RuntimePrimitiveNotInstalled",
]
