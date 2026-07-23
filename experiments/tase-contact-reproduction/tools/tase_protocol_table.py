#!/usr/bin/env python3
"""Load and resolve the paper-level TASE protocol table."""

from __future__ import annotations

import argparse
import json
import shlex
from pathlib import Path
from typing import Any


EXPERIMENT_ROOT = Path(__file__).resolve().parents[1]
PROTOCOL_TABLE_PATH = EXPERIMENT_ROOT / "config" / "tase_protocol_table.json"


class ProtocolTableError(RuntimeError):
    """Raised when the canonical protocol table is missing or inconsistent."""


def load_protocol_table(root: Path = EXPERIMENT_ROOT) -> dict[str, Any]:
    path = root / "config" / "tase_protocol_table.json"
    if not path.is_file():
        raise ProtocolTableError(f"missing TASE protocol table: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def _required_mapping(table: dict[str, Any], key: str) -> dict[str, Any]:
    value = table.get(key)
    if not isinstance(value, dict):
        raise ProtocolTableError(f"protocol table field must be an object: {key}")
    return value


def _required_named(mapping: dict[str, Any], key: str, label: str) -> dict[str, Any]:
    value = mapping.get(key)
    if not isinstance(value, dict):
        raise ProtocolTableError(f"unknown {label}: {key}")
    return value


def resolve_experiment_profile(profile_id: str, root: Path = EXPERIMENT_ROOT) -> dict[str, Any]:
    table = load_protocol_table(root)
    experiment = _required_named(_required_mapping(table, "experiment_profiles"), profile_id, "experiment profile")
    flow = _required_named(
        _required_mapping(_required_mapping(table, "paper_shared"), "flow_profiles"),
        str(experiment["flow_profile"]),
        "flow profile",
    )
    parameters = _required_named(
        _required_mapping(table, "parameter_profiles"),
        str(experiment["parameter_profile"]),
        "parameter profile",
    )
    safety = _required_named(
        _required_mapping(table, "safety_limits"),
        str(experiment["safety_limits"]),
        "safety limits",
    )
    evidence_key = experiment.get("evidence_profile")
    evidence = {}
    if evidence_key:
        evidence = _required_named(_required_mapping(table, "evidence"), str(evidence_key), "evidence profile")
    return {
        "id": profile_id,
        "step": experiment["step"],
        "task": experiment["task"],
        "flow": dict(flow),
        "parameters": dict(parameters),
        "safety_limits": dict(safety),
        "evidence": dict(evidence),
        "experiment": dict(experiment),
    }


def _profile_value(profile: dict[str, Any], section: str, key: str) -> Any:
    try:
        return profile[section][key]
    except KeyError as exc:
        raise ProtocolTableError(f"resolved profile {profile['id']} missing {section}.{key}") from exc


def operator_env(operator_id: str, root: Path = EXPERIMENT_ROOT) -> dict[str, str]:
    if operator_id == "step5b-contact":
        profile = resolve_experiment_profile("Step5.contact_cycloid", root)
        params = profile["parameters"]
        limits = profile["safety_limits"]
        return {
            "TASE_STEP5B_BRIDGE_DURATION_S": str(params["bridge_duration_s"]),
            "TASE_STEP5B_TARGET_FORCE_N": str(params["target_force_n"]),
            "TASE_STEP5B_NORMAL_FILTER_ALPHA": str(params["normal_filter_alpha"]),
            "TASE_STEP5B_NORMAL_MIN_FORCE_N": str(params["normal_filter_min_force_n"]),
            "TASE_STEP5B_FORCE_P_GAIN": str(params["force_p_gain"]),
            "TASE_STEP5B_FORCE_I_GAIN": str(params["force_i_gain"]),
            "TASE_STEP5B_FORCE_DAMPING": str(params["force_damping"]),
            "TASE_STEP5B_INTEGRAL_LIMIT_N_S": str(params["integral_limit_n_s"]),
            "TASE_STEP5B_MAX_NORMAL_FORCE_N": str(limits["max_normal_force_n"]),
            "TASE_STEP5B_MAX_FORCE_NORM_N": str(limits["force_norm_guard_n"]),
            "TASE_STEP5B_MAX_TORQUE_NORM_NM": str(limits["max_torque_norm_nm"]),
            "TASE_STEP5B_NORMAL_VELOCITY_LIMIT_M_S": str(limits["normal_velocity_limit_m_s"]),
            "TASE_STEP5B_TOTAL_LINEAR_LIMIT_M_S": str(limits["total_linear_limit_m_s"]),
            "TASE_STEP5B_ANGULAR_LIMIT_RAD_S": str(limits["angular_limit_rad_s"]),
            "TASE_STEP5B_REACQUIRE_VELOCITY_M_S": str(limits["reacquire_velocity_m_s"]),
        }
    if operator_id in {"step6b-contact", "step6b-v2-contact"}:
        profile_id = "Step6.contact_eight_v1" if operator_id == "step6b-contact" else "Step6.contact_eight"
        profile = resolve_experiment_profile(profile_id, root)
        params = profile["parameters"]
        limits = profile["safety_limits"]
        experiment = profile["experiment"]
        return {
            "TASE_STEP6B_BRIDGE_VERSION": str(experiment["bridge_version"]),
            "TASE_STEP6B_BRIDGE_DURATION_S": str(params["bridge_duration_s"]),
            "TASE_STEP6B_TARGET_FORCE_N": str(params["target_force_n"]),
            "TASE_STEP6B_NORMAL_FILTER_ALPHA": str(params["normal_filter_alpha"]),
            "TASE_STEP6B_NORMAL_MIN_FORCE_N": str(params["normal_filter_min_force_n"]),
            "TASE_STEP6B_MAX_NORMAL_FORCE_N": str(limits["max_normal_force_n"]),
            "TASE_STEP6B_MAX_FORCE_NORM_N": str(limits["force_norm_guard_n"]),
            "TASE_STEP6B_MAX_TORQUE_NORM_NM": str(limits["max_torque_norm_nm"]),
            "TASE_STEP6B_MOTION_LIMIT_M_S": str(limits["motion_limit_m_s"]),
            "TASE_STEP6B_TOTAL_LINEAR_LIMIT_M_S": str(limits["total_linear_limit_m_s"]),
            "TASE_STEP6B_NORMAL_VELOCITY_LIMIT_M_S": str(limits["normal_velocity_limit_m_s"]),
            "TASE_STEP6B_ANGULAR_LIMIT_RAD_S": str(limits["angular_limit_rad_s"]),
        }
    raise ProtocolTableError(f"unknown operator env profile: {operator_id}")


def shell_assignments(values: dict[str, str]) -> str:
    return "\n".join(f"{key}={shlex.quote(value)}" for key, value in sorted(values.items()))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=EXPERIMENT_ROOT)
    subparsers = parser.add_subparsers(dest="command", required=True)

    resolve_parser = subparsers.add_parser("resolve")
    resolve_parser.add_argument("profile_id")

    env_parser = subparsers.add_parser("operator-env")
    env_parser.add_argument("operator_id")

    args = parser.parse_args(argv)
    try:
        if args.command == "resolve":
            print(json.dumps(resolve_experiment_profile(args.profile_id, args.root), indent=2, sort_keys=True))
        elif args.command == "operator-env":
            print(shell_assignments(operator_env(args.operator_id, args.root)))
    except ProtocolTableError as exc:
        raise SystemExit(str(exc)) from exc
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
