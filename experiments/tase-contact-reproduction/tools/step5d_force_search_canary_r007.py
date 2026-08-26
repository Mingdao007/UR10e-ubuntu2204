"""Independent Kunwei-only 1 N force-search canary r007 primitive."""

from __future__ import annotations

from pathlib import Path

from step5d_force_search_canary_shared import (
    ForceSearchCanaryAxisReturnPolicy,
    ForceSearchCanaryContract,
    ForceSearchCanaryContractError,
    canary_stop_reason,
    load_force_search_canary_contract,
    render_force_search_canary_script,
)

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONTRACT = ROOT / "config/step5d/force_search_canary_r007.json"
SCHEMA = "step5d.force-search-canary/contract-v2"
ARTIFACT_ID = "new-eoat-kunwei-1n-canary-r007"
PROGRAM_NAME = "step5d_force_search_canary_r007"
CONTROLLER_DIR = "/programs/andyl/kunwei/step5"

CanaryR007Contract = ForceSearchCanaryContract
CanaryR007Error = ForceSearchCanaryContractError


stop_reason = canary_stop_reason


def load_contract(path: Path = DEFAULT_CONTRACT) -> CanaryR007Contract:
    result = load_force_search_canary_contract(
        path=path, schema=SCHEMA, artifact_id=ARTIFACT_ID
    )
    exact = {
        "axis": ForceSearchCanaryAxisReturnPolicy.NEGATIVE_Z_SEARCH_EXTERNAL_SCRIPT1_HOME,
        "search_speed_m_s": 0.0002,
        "search_acceleration_m_s2": 0.005,
        "cycle_s": 0.008,
        "max_travel_m": 0.018,
        "runtime_limit_s": 90.0,
        "search_stopl_acceleration_m_s2": 0.01,
        "startup_increment_count": 3,
        "heartbeat_gap_s": 0.08,
        "contact_positive_normal_n": 0.5,
        "contact_force_norm_n": 0.7,
        "hard_abs_normal_n": 3.0,
        "hard_force_norm_n": 3.0,
        "hard_torque_norm_nm": 0.2,
        "stationary_dwell_s": 0.25,
        "retract_distance_m": 0.0,
        "retract_speed_m_s": 0.0,
        "retract_acceleration_m_s2": 0.0,
    }
    mismatches = {
        name: {"expected": expected, "actual": getattr(result, name)}
        for name, expected in exact.items()
        if getattr(result, name) != expected
    }
    if mismatches:
        raise CanaryR007Error(f"r007 fixed invariants differ: {mismatches}")
    if result.runtime_limit_s * result.search_speed_m_s < result.max_travel_m:
        raise CanaryR007Error("runtime cannot cover maximum travel")
    if not (
        0.0 < result.contact_positive_normal_n < result.hard_abs_normal_n
        and 0.0 < result.contact_force_norm_n < result.hard_force_norm_n
    ):
        raise CanaryR007Error("contact and hard guards are not nested")
    if result.retract_distance_m < 0.0:
        raise CanaryR007Error("retract distance must be non-negative")
    return result


def render_script(
    contract: CanaryR007Contract | None = None,
    *,
    program_name: str = PROGRAM_NAME,
) -> str:
    return render_force_search_canary_script(
        contract or load_contract(), program_name=program_name
    )


__all__ = [
    "ARTIFACT_ID",
    "CONTROLLER_DIR",
    "DEFAULT_CONTRACT",
    "PROGRAM_NAME",
    "CanaryR007Contract",
    "CanaryR007Error",
    "load_contract",
    "render_script",
    "stop_reason",
]
