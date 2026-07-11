#!/usr/bin/env python3
"""Build a deterministic, offline-only cross-engine falsification plan.

This tool reads a MuJoCo model bundle manifest and the Gazebo v2 static lane
contract.  It does not import either simulator and never turns static contract
agreement into a contact, timing, physics, or robustness pass.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path
from typing import Any, Iterable, Mapping


EXPERIMENT_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = EXPERIMENT_ROOT.parents[1]
DEFAULT_CONFIG = EXPERIMENT_ROOT / "config" / "cross_engine_domain_randomization_v1.json"
DEFAULT_GAZEBO_CONTRACT = EXPERIMENT_ROOT / "config" / "gazebo_v2_lane_contract.json"
SCENARIO_SCHEMA = "ur10e_seeded_domain_randomization_scenarios_v1"
EVIDENCE_SCHEMA = "ur10e_cross_engine_falsification_evidence_v1"
SHA256_LENGTH = 64
OUTPUT_KEYS = ("no_contact_velocity", "contact_velocity", "torque_surrogate")
REQUIRED_PARAMETER_GROUPS = (
    "surface_geometry",
    "surface_friction",
    "contact_compliance",
    "tcp_extrinsics",
    "eoat_dynamics",
    "sensor_bias",
    "sensor_noise",
    "sensor_delay",
    "servo_lag",
    "command_drop",
)
RUNTIME_BLOCKERS = (
    "mujoco_runtime_evidence_not_supplied_static_model_manifest_only",
    "gazebo_runtime_evidence_missing",
    "gazebo_native_contact_ft_same_run_evidence_missing",
    "gazebo_runtime_timing_evidence_missing",
    "cross_engine_contact_equivalence_not_proven",
    "cross_engine_timing_equivalence_not_proven",
    "domain_randomization_scenarios_not_executed",
    "randomization_parameters_uncalibrated_falsification_only",
    "gazebo_eoat_mass_cog_inertia_not_bound_in_static_contract",
)


def canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("utf-8")


def sha256_value(value: Any) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_object(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object: {path}")
    return value


def write_object(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _finite(value: Any) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(float(value))
    )


def _sha256(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == SHA256_LENGTH
        and all(character in "0123456789abcdef" for character in value)
    )


def _dedupe(values: Iterable[str]) -> list[str]:
    return list(dict.fromkeys(values))


def repo_locator(path: Path) -> str:
    resolved = path.resolve()
    try:
        return str(resolved.relative_to(REPO_ROOT.resolve()))
    except ValueError:
        return str(resolved)


def source_binding(path: Path, *, role: str) -> dict[str, Any]:
    return {
        "role": role,
        "path": repo_locator(path),
        "size_bytes": path.stat().st_size,
        "sha256": sha256_file(path),
    }


def resolve_binding_path(locator: str, *, base_dir: Path) -> Path:
    path = Path(locator)
    if path.is_absolute():
        return path
    repo_candidate = REPO_ROOT / path
    if repo_candidate.exists():
        return repo_candidate
    return base_dir / path


def validate_config(config: Mapping[str, Any]) -> list[str]:
    issues: list[str] = []
    if config.get("schema") != "ur10e_cross_engine_domain_randomization_config_v1":
        issues.append("config_schema_invalid")
    if not isinstance(config.get("seed"), int) or isinstance(config.get("seed"), bool):
        issues.append("config_seed_invalid")
    if not isinstance(config.get("combined_scenario_count"), int) or int(
        config.get("combined_scenario_count") or 0
    ) < 1:
        issues.append("combined_scenario_count_invalid")
    groups = config.get("parameter_groups")
    if not isinstance(groups, Mapping):
        return issues + ["parameter_groups_invalid"]
    missing = sorted(set(REQUIRED_PARAMETER_GROUPS) - set(groups))
    extra = sorted(set(groups) - set(REQUIRED_PARAMETER_GROUPS))
    issues.extend(f"parameter_group_missing:{name}" for name in missing)
    issues.extend(f"parameter_group_unexpected:{name}" for name in extra)
    parameter_ids: set[str] = set()
    for group_name, group in groups.items():
        parameters = group.get("parameters") if isinstance(group, Mapping) else None
        if not isinstance(parameters, Mapping) or not parameters:
            issues.append(f"parameter_group_empty:{group_name}")
            continue
        for parameter_id, bounds in parameters.items():
            if parameter_id in parameter_ids:
                issues.append(f"parameter_duplicate:{parameter_id}")
            parameter_ids.add(str(parameter_id))
            if not isinstance(bounds, Mapping):
                issues.append(f"parameter_bounds_invalid:{parameter_id}")
                continue
            minimum = bounds.get("minimum")
            default = bounds.get("default")
            maximum = bounds.get("maximum")
            if not all(_finite(value) for value in (minimum, default, maximum)):
                issues.append(f"parameter_nonfinite:{parameter_id}")
            elif not float(minimum) <= float(default) <= float(maximum):
                issues.append(f"parameter_order_invalid:{parameter_id}")
            if not str(bounds.get("unit") or ""):
                issues.append(f"parameter_unit_missing:{parameter_id}")
    boundary = config.get("claim_boundary")
    if not isinstance(boundary, Mapping):
        issues.append("config_claim_boundary_invalid")
    else:
        for key in (
            "parameters_are_calibrated",
            "scenario_execution_authorized",
            "robustness_pass_allowed_from_plan",
            "physics_pass_allowed_from_plan",
            "live_motion_authorized",
            "package_accepted",
            "live_accepted",
            "reproduction_complete",
        ):
            if boundary.get(key) is not False:
                issues.append(f"config_claim_boundary_not_false:{key}")
    return issues


def _validate_bound_file(
    binding: Any,
    *,
    base_dir: Path,
    label: str,
) -> list[str]:
    if not isinstance(binding, Mapping):
        return [f"{label}:binding_invalid"]
    locator = binding.get("path")
    expected_hash = binding.get("sha256")
    if not isinstance(locator, str) or not locator:
        return [f"{label}:path_invalid"]
    if not _sha256(expected_hash):
        return [f"{label}:sha256_invalid"]
    path = resolve_binding_path(locator, base_dir=base_dir)
    if not path.is_file():
        return [f"{label}:file_missing"]
    if sha256_file(path) != expected_hash:
        return [f"{label}:hash_mismatch"]
    if binding.get("size_bytes") != path.stat().st_size:
        return [f"{label}:size_mismatch"]
    return []


def validate_mujoco_manifest(
    manifest: Mapping[str, Any],
    *,
    manifest_dir: Path,
) -> list[str]:
    issues: list[str] = []
    if manifest.get("schema") != "ur10e_mujoco_model_bundle_v1":
        issues.append("mujoco_manifest_schema_invalid")
    rates = manifest.get("rates_hz")
    if rates != {"physics": 2000, "control": 500, "dbil": 200}:
        issues.append("mujoco_rates_invalid")
    schedule = manifest.get("integer_schedule")
    if schedule != {"physics_per_control": 4, "physics_per_dbil": 10}:
        issues.append("mujoco_integer_schedule_invalid")
    outputs = manifest.get("outputs")
    if not isinstance(outputs, Mapping):
        issues.append("mujoco_outputs_invalid")
    else:
        for key in OUTPUT_KEYS:
            issues.extend(
                _validate_bound_file(
                    outputs.get(key),
                    base_dir=manifest_dir,
                    label=f"mujoco_output:{key}",
                )
            )
    tcp = manifest.get("active_tcp_offset_tool0_m")
    if not isinstance(tcp, list) or len(tcp) != 3 or not all(_finite(value) for value in tcp):
        issues.append("mujoco_active_tcp_invalid")
    no_contact = manifest.get("no_contact_scene")
    if not isinstance(no_contact, Mapping) or no_contact.get("native_contact_enabled") is not True:
        issues.append("mujoco_no_contact_native_collision_not_enabled")
    boundary = manifest.get("claim_boundary")
    if not isinstance(boundary, Mapping):
        issues.append("mujoco_claim_boundary_invalid")
    else:
        for key in (
            "calibrated_physics_claim_allowed",
            "p0_sim_physics_pass_allowed",
            "live_motion_authorized",
            "package_accepted",
            "live_accepted",
            "reproduction_complete",
        ):
            if boundary.get(key) is not False:
                issues.append(f"mujoco_claim_boundary_not_false:{key}")
    blockers = manifest.get("blockers")
    if not isinstance(blockers, list) or not blockers:
        issues.append("mujoco_geometry_provisional_blockers_missing")
    return issues


def validate_gazebo_contract(contract: Mapping[str, Any]) -> list[str]:
    issues: list[str] = []
    if contract.get("schema") != "ur10e_gazebo_v2_lane_contract_v1":
        issues.append("gazebo_contract_schema_invalid")
    engine = contract.get("engine")
    if not isinstance(engine, Mapping):
        issues.append("gazebo_engine_contract_invalid")
    else:
        if engine.get("family") != "Gazebo Fortress" or engine.get("required_major") != 6:
            issues.append("gazebo_fortress_lane_invalid")
        if engine.get("ros2_control_plugin") != "libign_ros2_control-system.so":
            issues.append("gazebo_ros2_control_plugin_invalid")
        if "gz sim" not in (engine.get("forbidden_runtime_tokens") or []):
            issues.append("gazebo_harmonic_mixing_guard_missing")
    rates = contract.get("rates_hz")
    if not isinstance(rates, Mapping) or rates.get("physics") != 2000 or rates.get("controller") != 500:
        issues.append("gazebo_rates_invalid")
    exclusivity = contract.get("backend_exclusivity")
    if not isinstance(exclusivity, Mapping) or exclusivity.get("simultaneous_activation_allowed") is not False:
        issues.append("gazebo_backend_exclusivity_invalid")
    attachment = contract.get("sensor_attachment")
    if not isinstance(attachment, Mapping):
        issues.append("gazebo_sensor_attachment_invalid")
    else:
        if attachment.get("same_run_required") is not True:
            issues.append("gazebo_same_run_requirement_missing")
        if attachment.get("virtual_surface_force_allowed") is not False:
            issues.append("gazebo_virtual_surface_force_not_forbidden")
    lineage = contract.get("frame_lineage")
    entities = lineage.get("required_runtime_entities") if isinstance(lineage, Mapping) else None
    if not isinstance(entities, list) or "active_tcp" not in entities:
        issues.append("gazebo_active_tcp_frame_missing")
    tcp = lineage.get("active_tcp_offset_tool0_m") if isinstance(lineage, Mapping) else None
    if (
        not isinstance(tcp, list)
        or len(tcp) != 3
        or not all(_finite(value) for value in tcp)
        or lineage.get("active_tcp_joint") != "active_tcp_joint"
    ):
        issues.append("gazebo_active_tcp_numeric_transform_invalid")
    authorization = contract.get("authorization")
    if not isinstance(authorization, Mapping):
        issues.append("gazebo_authorization_invalid")
    else:
        for key, value in authorization.items():
            if value is not False:
                issues.append(f"gazebo_authorization_not_false:{key}")
    tick_schema = contract.get("tick_schema")
    tick_path = EXPERIMENT_ROOT / str(tick_schema or "")
    if not tick_path.is_file():
        issues.append("gazebo_tick_schema_missing")
    else:
        tick = load_object(tick_path)
        schema_value = ((tick.get("properties") or {}).get("schema") or {}).get("const")
        if schema_value != "ur10e_gazebo_v2_tick_v1":
            issues.append("gazebo_tick_schema_invalid")
    return issues


def flattened_parameters(config: Mapping[str, Any]) -> list[tuple[str, str, Mapping[str, Any]]]:
    rows: list[tuple[str, str, Mapping[str, Any]]] = []
    groups = config["parameter_groups"]
    for group_name in sorted(groups):
        for parameter_id, bounds in sorted(groups[group_name]["parameters"].items()):
            rows.append((group_name, parameter_id, bounds))
    return rows


def nominal_parameters(config: Mapping[str, Any]) -> dict[str, float]:
    return {
        parameter_id: float(bounds["default"])
        for _, parameter_id, bounds in flattened_parameters(config)
    }


def deterministic_uniform(seed: int, scenario_index: int, parameter_id: str) -> float:
    token = f"{seed}:{scenario_index}:{parameter_id}".encode("utf-8")
    integer = int.from_bytes(hashlib.sha256(token).digest()[:8], "big")
    return integer / float((1 << 64) - 1)


def _scenario(
    scenario_id: str,
    scenario_type: str,
    parameters: Mapping[str, float],
    *,
    seed: int,
) -> dict[str, Any]:
    row: dict[str, Any] = {
        "id": scenario_id,
        "type": scenario_type,
        "realization_seed": int.from_bytes(
            hashlib.sha256(f"{seed}:{scenario_id}:realization".encode("utf-8")).digest()[:4],
            "big",
        ),
        "parameters": dict(sorted(parameters.items())),
        "execution_status": "planned_not_executed",
        "permitted_interpretation": "robustness_falsification_input_only",
        "physics_acceptance_eligible": False,
    }
    row["scenario_fingerprint_sha256"] = sha256_value(row)
    return row


def build_scenario_manifest(
    config: Mapping[str, Any],
    *,
    config_binding: Mapping[str, Any],
    builder_binding: Mapping[str, Any],
) -> dict[str, Any]:
    seed = int(config["seed"])
    defaults = nominal_parameters(config)
    groups = config["parameter_groups"]
    scenarios = [_scenario("nominal", "nominal", defaults, seed=seed)]
    group_extrema: dict[str, dict[str, str]] = {}
    for group_name in sorted(groups):
        group_extrema[group_name] = {}
        for bound_name, bound_key in (("low", "minimum"), ("high", "maximum")):
            values = dict(defaults)
            for parameter_id, bounds in groups[group_name]["parameters"].items():
                values[parameter_id] = float(bounds[bound_key])
            scenario_id = f"oat_{group_name}_{bound_name}"
            scenarios.append(_scenario(scenario_id, "one_group_at_a_time_extreme", values, seed=seed))
            group_extrema[group_name][bound_name] = scenario_id
    combined_count = int(config["combined_scenario_count"])
    rows = flattened_parameters(config)
    for index in range(combined_count):
        values: dict[str, float] = {}
        for _, parameter_id, bounds in rows:
            low = float(bounds["minimum"])
            high = float(bounds["maximum"])
            ratio = deterministic_uniform(seed, index, parameter_id)
            values[parameter_id] = round(low + (high - low) * ratio, 12)
        scenarios.append(
            _scenario(
                f"joint_seeded_{index:03d}",
                "joint_seeded_falsification",
                values,
                seed=seed,
            )
        )
    manifest: dict[str, Any] = {
        "schema": SCENARIO_SCHEMA,
        "generated_at": config["artifact_epoch"],
        "mode": "offline_falsification_plan_only",
        "seed": seed,
        "generator": dict(builder_binding),
        "config": dict(config_binding),
        "scenario_count": len(scenarios),
        "coverage": {
            "required_parameter_groups": list(REQUIRED_PARAMETER_GROUPS),
            "parameter_ids": [parameter_id for _, parameter_id, _ in rows],
            "nominal_scenario": "nominal",
            "group_extrema": group_extrema,
            "combined_scenario_count": combined_count,
            "all_required_groups_have_low_and_high_extrema": all(
                set(group_extrema.get(name, {})) == {"low", "high"}
                for name in REQUIRED_PARAMETER_GROUPS
            ),
        },
        "execution": {
            "simulator_started": False,
            "scenario_executed_count": 0,
            "scenario_pass_count": 0,
            "runtime_metrics_present": False,
        },
        "scenarios": scenarios,
        "claim_boundary": {
            "scenario_plan_generated": True,
            "domain_randomization_executed": False,
            "domain_randomization_robustness_pass": False,
            "physics_pass": False,
            "live_motion_authorized": False,
            "live_accepted": False,
            "reproduction_complete": False,
        },
    }
    manifest["manifest_fingerprint_sha256"] = sha256_value(manifest)
    return manifest


def build_evidence_payload(
    *,
    mujoco_manifest_path: Path,
    gazebo_contract_path: Path,
    config_path: Path,
    scenario_path: Path,
    scenario_manifest: Mapping[str, Any],
) -> dict[str, Any]:
    config = load_object(config_path)
    mujoco = load_object(mujoco_manifest_path)
    gazebo = load_object(gazebo_contract_path)
    config_issues = validate_config(config)
    mujoco_issues = validate_mujoco_manifest(mujoco, manifest_dir=mujoco_manifest_path.parent)
    gazebo_issues = validate_gazebo_contract(gazebo)
    mujoco_rates = mujoco.get("rates_hz") if isinstance(mujoco.get("rates_hz"), Mapping) else {}
    gazebo_rates = gazebo.get("rates_hz") if isinstance(gazebo.get("rates_hz"), Mapping) else {}
    physics_rate_match = mujoco_rates.get("physics") == gazebo_rates.get("physics") == 2000
    control_rate_match = mujoco_rates.get("control") == gazebo_rates.get("controller") == 500
    mujoco_tcp = mujoco.get("active_tcp_offset_tool0_m")
    gazebo_lineage = gazebo.get("frame_lineage") if isinstance(gazebo.get("frame_lineage"), Mapping) else {}
    gazebo_tcp = gazebo_lineage.get("active_tcp_offset_tool0_m")
    tcp_contract_match = bool(
        isinstance(mujoco_tcp, list)
        and isinstance(gazebo_tcp, list)
        and len(mujoco_tcp) == len(gazebo_tcp) == 3
        and all(_finite(value) for value in mujoco_tcp + gazebo_tcp)
        and all(abs(float(left) - float(right)) <= 1e-15 for left, right in zip(mujoco_tcp, gazebo_tcp))
    )
    static_issues = config_issues + mujoco_issues + gazebo_issues
    static_contract_pass = (
        not static_issues and physics_rate_match and control_rate_match and tcp_contract_match
    )
    declared_model_blockers = [
        f"mujoco_model:{value}"
        for value in mujoco.get("blockers", [])
        if isinstance(value, str) and value
    ]
    blockers = _dedupe(
        [f"static_input:{issue}" for issue in static_issues]
        + ([] if physics_rate_match else ["cross_engine_static_physics_rate_mismatch"])
        + ([] if control_rate_match else ["cross_engine_static_control_rate_mismatch"])
        + ([] if tcp_contract_match else ["cross_engine_static_tcp_contract_mismatch"])
        + declared_model_blockers
        + list(RUNTIME_BLOCKERS)
    )
    payload: dict[str, Any] = {
        "schema": EVIDENCE_SCHEMA,
        "generated_at": config.get("artifact_epoch"),
        "mode": "offline_static_cross_engine_falsification_only",
        "status": (
            "falsification_plan_ready_runtime_equivalence_blocked"
            if static_contract_pass
            else "invalid_static_inputs_blocked"
        ),
        "inputs": {
            "mujoco_model_manifest": source_binding(
                mujoco_manifest_path, role="mujoco_static_model_bundle_manifest"
            ),
            "gazebo_static_contract": source_binding(
                gazebo_contract_path, role="gazebo_fortress_static_lane_contract"
            ),
            "randomization_config": source_binding(
                config_path, role="uncalibrated_falsification_parameter_ranges"
            ),
            "scenario_manifest": {
                **source_binding(scenario_path, role="seeded_falsification_scenario_manifest"),
                "path": scenario_path.name,
            },
        },
        "static_validation": {
            "mujoco_manifest_pass": not mujoco_issues,
            "gazebo_contract_pass": not gazebo_issues,
            "randomization_config_pass": not config_issues,
            "issues": static_issues,
        },
        "engine_comparison": {
            "physics_rate_hz": {
                "mujoco": mujoco_rates.get("physics"),
                "gazebo": gazebo_rates.get("physics"),
                "static_match": physics_rate_match,
            },
            "control_rate_hz": {
                "mujoco": mujoco_rates.get("control"),
                "gazebo": gazebo_rates.get("controller"),
                "static_match": control_rate_match,
            },
            "static_rate_contract_match": physics_rate_match and control_rate_match,
            "static_numeric_tcp_contract_match": tcp_contract_match,
            "static_input_contract_consistency_pass": static_contract_pass,
            "mujoco_runtime_evidence_present": False,
            "gazebo_runtime_evidence_present": False,
            "numeric_tcp_equivalence_proven": tcp_contract_match,
            "eoat_dynamics_equivalence_proven": False,
            "native_contact_same_run_equivalence_proven": False,
            "contact_equivalence_pass": False,
            "timing_equivalence_pass": False,
            "physics_equivalence_pass": False,
        },
        "domain_randomization": {
            "scenario_manifest_fingerprint_sha256": scenario_manifest.get(
                "manifest_fingerprint_sha256"
            ),
            "seed": scenario_manifest.get("seed"),
            "scenario_count": scenario_manifest.get("scenario_count"),
            "all_required_groups_have_low_and_high_extrema": (
                scenario_manifest.get("coverage") or {}
            ).get("all_required_groups_have_low_and_high_extrema"),
            "scenario_executed_count": 0,
            "runtime_metrics_present": False,
            "robustness_pass": False,
        },
        "claims": {
            "static_rate_contract_match": physics_rate_match and control_rate_match,
            "static_numeric_tcp_contract_match": tcp_contract_match,
            "seeded_falsification_plan_generated": True,
            "cross_engine_contact_equivalence": False,
            "cross_engine_timing_equivalence": False,
            "cross_engine_physics_equivalence": False,
            "domain_randomization_executed": False,
            "domain_randomization_robustness_pass": False,
            "p0_sim_physics_pass": False,
            "contact_sim_pass": False,
            "v30_offline_ready": False,
        },
        "claim_boundary": {
            "workflow_state": "liveprep_blocked",
            "current_program": "step5d_strict_rnn_ablation_v29",
            "v30_active": False,
            "offline_plan_cannot_promote_runtime_claim": True,
            "live_motion_authorized": False,
            "controller_upload_authorized": False,
            "bridge_start_authorized": False,
            "tp_play_authorized": False,
            "zero_ftsensor_authorized": False,
            "package_accepted": False,
            "live_accepted": False,
            "reproduction_complete": False,
        },
        "blockers": blockers,
    }
    payload["evidence_fingerprint_sha256"] = sha256_value(payload)
    return payload


def build(
    *,
    mujoco_manifest_path: Path,
    gazebo_contract_path: Path,
    config_path: Path,
    output_dir: Path,
) -> tuple[Path, Path]:
    config = load_object(config_path)
    config_issues = validate_config(config)
    if config_issues:
        raise ValueError("randomization config invalid: " + ",".join(config_issues))
    output_dir.mkdir(parents=True, exist_ok=True)
    builder_binding = source_binding(Path(__file__), role="deterministic_scenario_builder")
    config_binding = source_binding(config_path, role="uncalibrated_falsification_parameter_ranges")
    scenarios = build_scenario_manifest(
        config,
        config_binding=config_binding,
        builder_binding=builder_binding,
    )
    scenario_path = output_dir / "domain_randomization_scenarios.json"
    write_object(scenario_path, scenarios)
    evidence = build_evidence_payload(
        mujoco_manifest_path=mujoco_manifest_path,
        gazebo_contract_path=gazebo_contract_path,
        config_path=config_path,
        scenario_path=scenario_path,
        scenario_manifest=scenarios,
    )
    evidence_path = output_dir / "cross_engine_falsification_evidence.json"
    write_object(evidence_path, evidence)
    return scenario_path, evidence_path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mujoco-manifest", type=Path, required=True)
    parser.add_argument("--gazebo-contract", type=Path, default=DEFAULT_GAZEBO_CONTRACT)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    scenario_path, evidence_path = build(
        mujoco_manifest_path=args.mujoco_manifest.resolve(),
        gazebo_contract_path=args.gazebo_contract.resolve(),
        config_path=args.config.resolve(),
        output_dir=args.output_dir.resolve(),
    )
    result = {
        "scenario_manifest": str(scenario_path),
        "scenario_manifest_sha256": sha256_file(scenario_path),
        "evidence": str(evidence_path),
        "evidence_sha256": sha256_file(evidence_path),
    }
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
