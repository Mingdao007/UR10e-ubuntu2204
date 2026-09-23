"""Replay one recorded model-input trace through frozen native SFC and DSFC.

The recorded trace remains fixed while each controller is stepped, producing
a true command-level same-input counterfactual for this *model-generated*
trace. The module also passes the candidate tangent intents to the existing
offline dual-space collision policy; those outputs remain intents and never
become hardware commands.
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import json
from pathlib import Path
from typing import Any, Mapping

import numpy as np

from contact_method_registry import default_registry
import tase_sfc_dsfc_comparison as comparison
from tase_sfc_dsfc_comparison import METHODS, _candidate, _load_frozen_profiles
from tase_research_campaign import FIXED_PROXY_ROTATION
from tase_collision_policy import OfflineDualSpaceCollisionPolicy
from tase_sfc_fusion import normal_tangent_projectors


SCHEMA = "ur10e.tase-sfc-dsfc-model-input-replay-v1"
REQUIRED_INPUTS = (
    "raw_force_base_n", "estimated_outward_normal_base", "tcp_position_base_m",
    "reference_position_base_m", "reference_velocity_base_m_s", "force_target_n",
    "jacobian", "joint_velocity_lower_rad_s", "joint_velocity_upper_rad_s", "state_age_s",
)
SYNTHETIC_COLLISION_FIXTURE = {
    "virtual_inertia": 1.0,
    "joint_damping": 5.0,
    "joint_stiffness": 10.0,
    "joint_velocity_cap_rad_s": 0.05,
    "corridor_radius_m": 0.001,
    "external_joint_torque_nm": [1.0, 0.0, 1.0, 0.0, 0.0, 0.0],
    "observer_receipt": {
        "admission_passed": True,
        "sample_fresh": True,
        "calibration_sha256": "synthetic-test-fixture-calibration",
        "dynamics_model_sha256": "synthetic-test-fixture-dynamics",
        "timestamps_sha256": "synthetic-test-fixture-clock",
    },
    "note": "values copied from the existing offline unit-test fixture; none is calibrated or admitted for physical use",
}
OFFLINE_ANALYSIS_INTERFACES = {
    "event_trial_protocol_id": "tase_joint_ee_apparatus_collision_v1",
    "event_trial_schema": "tase.collision-trial-v1",
    "image_mark_schema": "contact-board-marks.v1",
    "connected_to_live_transport": False,
    "instrumented_apparatus_trials_in_this_run": 0,
    "qualified_image_mark_trials_in_this_run": 0,
}


def _replay_input_digest(rows: list[dict[str, Any]]) -> str:
    """Digest every reconstructed controller-visible input for the replay."""
    digest = hashlib.sha256()
    previous_source_twist = np.zeros(6, dtype=float)
    for row in rows:
        observation, reference = _observation(row, previous_source_twist)
        record = {
            "time_s": observation["time_s"],
            "dt_s": row["dt_s"],
            "state_age_s": observation["state_age_s"],
            "position_m": list(observation["position_m"]),
            "rotation": np.asarray(observation["rotation"]).tolist(),
            "joint_position_rad": list(observation["joint_position_rad"]),
            "jacobian": np.asarray(observation["jacobian"]).tolist(),
            "raw_force_base_n": list(observation["raw_force_base_n"]),
            "raw_torque_base_nm": list(observation["raw_torque_base_nm"]),
            "joint_velocity_lower": list(observation["joint_velocity_lower"]),
            "joint_velocity_upper": list(observation["joint_velocity_upper"]),
            "linear_velocity_base_m_s": list(observation["linear_velocity_base_m_s"]),
            "angular_velocity_base_rad_s": list(observation["angular_velocity_base_rad_s"]),
            "local_normal_base": list(observation["local_normal_base"]),
            "software_injection_base_n": list(observation["software_injection_base_n"]),
            "cmd_valid": True,
            "integral_enabled": True,
            "integral_reset_reason": "",
            "reference": {
                "position_m": list(reference["position_m"]),
                "velocity_m_s": list(reference["velocity_m_s"]),
                "reference_force_n": reference["reference_force_n"],
                "phase": reference["phase"],
            },
        }
        digest.update(json.dumps(record, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8"))
        digest.update(b"\n")
        previous_source_twist = np.asarray(row["output"]["realized_jqdot_m_s_rad_s"], dtype=float)
    return digest.hexdigest()


def _trace_rows(path: Path) -> list[dict[str, Any]]:
    with gzip.open(path, "rt", encoding="utf-8") as stream:
        rows = [json.loads(line) for line in stream if line.strip()]
    if not rows:
        raise ValueError(f"empty input trace: {path}")
    for index, row in enumerate(rows):
        inputs = row.get("inputs")
        if not isinstance(inputs, Mapping) or any(key not in inputs for key in REQUIRED_INPUTS):
            raise ValueError(f"input trace row {index} is missing required fields")
    return rows


def _audit_representative_historical_trace(comparison_root: Path) -> dict[str, Any]:
    attempt_dir = comparison_root.parent / "tase-resident-tune-rate400-b-20260923-01" / "session-03" / "attempts" / "0001"
    sensor_path = attempt_dir / "raw_sensor.jsonl"
    receipt_path = attempt_dir / "attempt-result.json"
    required = {
        "jacobian_base",
        "joint_velocity_lower_rad_s",
        "joint_velocity_upper_rad_s",
        "reference_position_base_m",
        "reference_velocity_base_m_s",
    }
    if not sensor_path.is_file():
        return {
            "scope": "one representative sealed rate400 resident attempt",
            "attempt_dir": str(attempt_dir),
            "raw_sensor_trace_exists": False,
            "receipt_exists": receipt_path.is_file(),
            "same_input_alternate_controller_replay_eligible": False,
            "missing_required_fields": sorted(required),
            "claim_limit": "absence at this selected path is not a survey of every historical trace",
        }
    with sensor_path.open("r", encoding="utf-8") as stream:
        first = next((json.loads(line) for line in stream if line.strip()), None)
    fields = sorted(first) if isinstance(first, Mapping) else []
    return {
        "scope": "one representative sealed rate400 resident attempt",
        "attempt_dir": str(attempt_dir),
        "raw_sensor_trace_exists": True,
        "receipt_exists": receipt_path.is_file(),
        "raw_sensor_first_row_fields": fields,
        "same_input_alternate_controller_replay_eligible": bool(required.issubset(fields)),
        "missing_required_fields": sorted(required - set(fields)),
        "claim_limit": "this audit covers the sampled raw-sensor stream; it does not assert that every separate artifact format lacks the fields",
    }


def _observation(row: Mapping[str, Any], previous_source_twist: np.ndarray) -> tuple[dict[str, Any], dict[str, Any]]:
    inputs = row["inputs"]
    observation = {
        "time_s": float(row["time_s"]),
        "state_age_s": float(inputs["state_age_s"]),
        "position_m": tuple(float(value) for value in inputs["tcp_position_base_m"]),
        "rotation": FIXED_PROXY_ROTATION.copy(),
        "joint_position_rad": (0.0,) * 6,
        "jacobian": np.asarray(inputs["jacobian"], dtype=float),
        "raw_force_base_n": tuple(float(value) for value in inputs["raw_force_base_n"]),
        "raw_torque_base_nm": (0.0, 0.0, 0.0),
        "joint_velocity_lower": tuple(float(value) for value in inputs["joint_velocity_lower_rad_s"]),
        "joint_velocity_upper": tuple(float(value) for value in inputs["joint_velocity_upper_rad_s"]),
        "linear_velocity_base_m_s": tuple(float(value) for value in previous_source_twist[:3]),
        "angular_velocity_base_rad_s": tuple(float(value) for value in previous_source_twist[3:]),
        "local_normal_base": tuple(float(value) for value in inputs["estimated_outward_normal_base"]),
        "software_injection_base_n": (0.0, 0.0, 0.0),
        "cmd_valid": True,
        "integral_enabled": True,
        "integral_reset_reason": "",
    }
    reference = {
        "position_m": tuple(float(value) for value in inputs["reference_position_base_m"]),
        "velocity_m_s": tuple(float(value) for value in inputs["reference_velocity_base_m_s"]),
        "reference_force_n": float(inputs["force_target_n"]),
        "phase": "path",
    }
    return observation, reference


def _replay_one(rows: list[dict[str, Any]], *, method: str, law: str, profile: dict[str, Any]) -> dict[str, Any]:
    candidate = _candidate(profile, law)
    handle = default_registry().initialize(
        method,
        config=candidate,
        outer_config=profile["normal_outer_config"],
        qp_library=Path("."),
    )
    input_digest = _replay_input_digest(rows)
    command_digest = hashlib.sha256()
    normal_twist_rows: list[np.ndarray] = []
    tangent_twist_rows: list[np.ndarray] = []
    qdot_norms: list[float] = []
    normal_leak_max = 0.0
    realization_counts: set[int] = set()
    selected_for_collision: dict[str, Any] | None = None
    previous_source_twist = np.zeros(6, dtype=float)
    try:
        for index, row in enumerate(rows):
            observation, reference = _observation(row, previous_source_twist)
            result = handle.step(observation, reference, float(row["dt_s"]))
            diagnostic = result.get("diagnostics", {}).get("composition")
            if not isinstance(diagnostic, Mapping):
                raise ValueError("native tangent adapter did not return composition diagnostics")
            normal = np.asarray(diagnostic["normal"], dtype=float)
            tase_twist = np.asarray(diagnostic["tase_normal_and_orientation_m_s_rad_s"], dtype=float)
            tangent_twist = np.asarray(diagnostic["tangential_tangent_twist_m_s_rad_s"], dtype=float)
            qdot = np.asarray(result["qdot_rad_s"], dtype=float)
            if tase_twist.shape != (6,) or tangent_twist.shape != (6,) or qdot.shape != (6,):
                raise ValueError("replay output dimensions differ from the six-axis contract")
            realization_counts.add(int(diagnostic["final_realization_calls"]))
            normal_leak_max = max(normal_leak_max, abs(float(np.dot(normal, tangent_twist[:3]))))
            normal_twist_rows.append(tase_twist)
            tangent_twist_rows.append(tangent_twist)
            qdot_norms.append(float(np.linalg.norm(qdot)))
            encoded = json.dumps(
                {"qdot_rad_s": qdot.tolist(), "desired_twist": result["xdot_c"]},
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
            command_digest.update(encoded + b"\n")
            if index == len(rows) // 2:
                selected_for_collision = {
                    "normal": normal,
                    "tase_twist": tase_twist,
                    "tangent_twist": tangent_twist,
                    "observation": observation,
                    "reference": reference,
                    "jacobian": observation["jacobian"],
                }
            previous_source_twist = np.asarray(row["output"]["realized_jqdot_m_s_rad_s"], dtype=float)
    finally:
        handle.close()

    assert selected_for_collision is not None
    collision = _collision_intents(selected_for_collision)
    normal_values = np.asarray(normal_twist_rows)
    tangent_values = np.asarray(tangent_twist_rows)
    return {
        "method": method,
        "candidate": candidate,
        "common_input_sha256": input_digest,
        "command_output_sha256": command_digest.hexdigest(),
        "sample_count": len(rows),
        "final_realization_calls": sorted(realization_counts),
        "tangential_normal_leak_max_m_s": normal_leak_max,
        "tase_normal_translation_rms_m_s": float(np.sqrt(np.mean(np.square(normal_values[:, :3])))),
        "tase_orientation_rms_rad_s": float(np.sqrt(np.mean(np.square(normal_values[:, 3:])))),
        "tangential_translation_rms_m_s": float(np.sqrt(np.mean(np.square(tangent_values[:, :3])))),
        "max_joint_speed_norm_rad_s": max(qdot_norms, default=0.0),
        "collision_intents": collision,
    }


def _policy(normal: np.ndarray) -> OfflineDualSpaceCollisionPolicy:
    return OfflineDualSpaceCollisionPolicy(
        normal,
        virtual_inertia=SYNTHETIC_COLLISION_FIXTURE["virtual_inertia"],
        joint_damping=SYNTHETIC_COLLISION_FIXTURE["joint_damping"],
        joint_stiffness=SYNTHETIC_COLLISION_FIXTURE["joint_stiffness"],
        joint_velocity_cap_rad_s=SYNTHETIC_COLLISION_FIXTURE["joint_velocity_cap_rad_s"],
    )


def _collision_intents(sample: Mapping[str, Any]) -> dict[str, Any]:
    normal = sample["normal"]
    tase_twist = sample["tase_twist"]
    tangent_twist = sample["tangent_twist"]
    observation = sample["observation"]
    jacobian = sample["jacobian"]
    raw_force = np.asarray(observation["raw_force_base_n"], dtype=float)
    measured_normal_force = abs(float(np.dot(raw_force, np.asarray(normal, dtype=float))))
    common = {
        "phase": "PATH",
        "dt_s": 0.002,
        "normal": normal,
        "tase_twist": tase_twist,
        "sfc_twist": tangent_twist,
        "jacobian": jacobian,
        "q_rad": np.zeros(6),
        "collision_qualified": True,
        "normal_force_n": measured_normal_force,
        "lateral_error_m": 0.0,
        "corridor_radius_m": SYNTHETIC_COLLISION_FIXTURE["corridor_radius_m"],
        "joint_observer_receipt": SYNTHETIC_COLLISION_FIXTURE["observer_receipt"],
    }
    nominal = _policy(normal).step(
        **common,
        collision_site=None,
        external_joint_torque_nm=None,
    )
    tool = _policy(normal).step(
        **common,
        collision_site="tool",
        external_joint_torque_nm=None,
    )
    link = _policy(normal).step(
        **common,
        collision_site="link",
        external_joint_torque_nm=SYNTHETIC_COLLISION_FIXTURE["external_joint_torque_nm"],
    )
    drift_policy = _policy(normal)
    drift_policy.step(
        **common,
        collision_site="tool",
        external_joint_torque_nm=None,
    )
    drift = drift_policy.step(
        **{**common, "lateral_error_m": 0.002},
        collision_site=None,
        external_joint_torque_nm=None,
    )
    tangent_projector = normal_tangent_projectors(normal).Pt
    return {
        "nominal": {
            "mode": nominal.mode,
            "cartesian_twist": nominal.cartesian_twist,
            "command_authority": nominal.diagnostics["command_authority"],
        },
        "tool_contact": {
            "mode": tool.mode,
            "tangent_frozen": tool.tangent_frozen,
            "normal_intent_error_m_s": abs(float(np.dot(normal, np.asarray(tool.cartesian_twist[:3]) - tase_twist[:3]))),
            "orientation_intent_error_rad_s": float(np.linalg.norm(np.asarray(tool.cartesian_twist[3:]) - tase_twist[3:])),
            "remaining_tangent_speed_m_s": float(np.linalg.norm(tangent_projector @ np.asarray(tool.cartesian_twist[:3]))),
            "command_authority": tool.diagnostics["command_authority"],
        },
        "qualified_link_contact_fixture": {
            "mode": link.mode,
            "tangent_frozen": link.tangent_frozen,
            "joint_yield_preference_rad_s": link.joint_yield_preference_rad_s,
            "normal_intent_error_m_s": abs(float(np.dot(normal, np.asarray(link.cartesian_twist[:3]) - tase_twist[:3]))),
            "orientation_intent_error_rad_s": float(np.linalg.norm(np.asarray(link.cartesian_twist[3:]) - tase_twist[3:])),
            "remaining_tangent_speed_m_s": float(np.linalg.norm(tangent_projector @ np.asarray(link.cartesian_twist[:3]))),
            "recovery_requested": link.recovery_requested,
            "command_authority": link.diagnostics["command_authority"],
            "fixture_only": True,
        },
        "loaded_out_of_corridor_fixture": {
            "mode": drift.mode,
            "recovery_requested": drift.recovery_requested,
            "cartesian_twist": drift.cartesian_twist,
            "reason": drift.reason,
            "recovery_owner_required": drift.diagnostics.get("recovery_owner_required"),
            "fixture_only": True,
        },
    }


def run_replay(*, comparison_dir: Path, output_path: Path | None = None) -> dict[str, Any]:
    root = Path(comparison_dir).resolve()
    attempts_path = root / "attempts.jsonl"
    if not attempts_path.is_file():
        raise FileNotFoundError(attempts_path)
    attempts = [json.loads(line) for line in attempts_path.read_text(encoding="utf-8").splitlines() if line]
    source_attempts = [item for item in attempts if item["method"] == METHODS[0]]
    if not source_attempts:
        raise ValueError("comparison has no frozen SFC input traces to replay")
    profile = _load_frozen_profiles()
    historical_audit = _audit_representative_historical_trace(root)
    results: list[dict[str, Any]] = []
    for source in source_attempts:
        source_trace = root / source["trace_path"]
        rows = _trace_rows(source_trace)
        shared_input_sha = _replay_input_digest(rows)
        for method, law in zip(METHODS, ("SFC", "DSFC")):
            replay = _replay_one(rows, method=method, law=law, profile=profile)
            if replay["common_input_sha256"] != shared_input_sha:
                raise RuntimeError("controller replay did not consume the same recorded input sequence")
            results.append({
                "source_attempt_id": source["attempt_id"],
                "source_method": source["method"],
                "source_trace_sha256": source["trace_sha256"],
                "scenario": source["case"],
                "trial_key": source["trial_key"],
                **replay,
            })
    outputs_path = Path(output_path).resolve() if output_path is not None else root / "command-replay.json"
    document = {
        "schema": SCHEMA,
        "evidence_class": "same_input_command_replay_of_model_generated_traces",
        "physical_evidence": False,
        "input_trace_source": "SFC closed-loop model trace for each matched block and scenario; the exact same recorded input sequence is supplied to both laws",
        "methods": list(METHODS),
        "same_input_replay_units": len(source_attempts),
        "controller_replays": len(results),
        "native_parameters": profile["tangential_parameters"],
        "A_outer_profile": profile["normal_outer_config"],
        "historical_trace_audit": historical_audit,
        "offline_analysis_interfaces": OFFLINE_ANALYSIS_INTERFACES,
        "collision_policy": {
            "policy_id": "tase-dual-space-collision-offline-v1",
            "command_authority": "offline_intent_only",
            "physical_observer_qualified": False,
            "visual_corridor_qualified": False,
            "synthetic_test_fixture": SYNTHETIC_COLLISION_FIXTURE,
            "results": [
                {
                    "source_attempt_id": item["source_attempt_id"],
                    "scenario": item["scenario"],
                    "method": item["method"],
                    "intents": item["collision_intents"],
                }
                for item in results
            ],
        },
        "results": results,
    }
    outputs_path.write_text(json.dumps(document, indent=2, sort_keys=True, allow_nan=False) + "\n", encoding="utf-8")
    summary_path = root / "summary.json"
    if summary_path.is_file():
        summary = json.loads(summary_path.read_text(encoding="utf-8"))
        summary["same_input_command_replay"] = {
            "artifact": outputs_path.name,
            "source_trace_count": len(source_attempts),
            "controller_replays": len(results),
            "matching_input_sequences_verified": True,
            "max_tangential_normal_leak_m_s": max((
                float(item["tangential_normal_leak_max_m_s"]) for item in results
            ), default=0.0),
            "final_realization_calls": sorted({
                int(value)
                for item in results
                for value in item["final_realization_calls"]
            }),
            "evidence_class": document["evidence_class"],
        }
        summary["offline_analysis_interfaces"] = OFFLINE_ANALYSIS_INTERFACES
        summary["dual_space_collision_policy"] = {
            "policy_id": "tase-dual-space-collision-offline-v1",
            "tool_collision_tangent_frozen_for_all_replays": all(
                item["collision_intents"]["tool_contact"]["tangent_frozen"]
                for item in results
            ),
            "tool_collision_max_normal_intent_error_m_s": max((
                float(item["collision_intents"]["tool_contact"]["normal_intent_error_m_s"])
                for item in results
            ), default=0.0),
            "tool_collision_max_orientation_intent_error_rad_s": max((
                float(item["collision_intents"]["tool_contact"]["orientation_intent_error_rad_s"])
                for item in results
            ), default=0.0),
            "link_yield_preferences_are_intents_only": all(
                item["collision_intents"]["qualified_link_contact_fixture"]["command_authority"] == "offline_intent_only"
                and item["collision_intents"]["qualified_link_contact_fixture"]["joint_yield_preference_rad_s"] is not None
                for item in results
            ),
            "out_of_corridor_requests_recovery_owner_for_all_replays": all(
                item["collision_intents"]["loaded_out_of_corridor_fixture"]["recovery_owner_required"] is True
                for item in results
            ),
            "physical_joint_observer_qualified": False,
            "visual_corridor_qualified": False,
        }
        report_path = root / "report.md"
        if report_path.is_file():
            report = report_path.read_text(encoding="utf-8")
            marker = "## Same-input command replay and dual-space intent"
            if marker in report:
                report = report.split(marker, 1)[0].rstrip() + "\n\n"
            report += "\n".join([
                marker,
                "",
                f"Both frozen native laws were replayed on {len(source_attempts)} fixed SFC-generated model input traces ({len(results)} controller replays). Each pair consumed a byte-canonicalized identical sequence of wrench, estimated normal, pose, reference, Jacobian, bounds, and freshness values. Outputs were not fed back into the next recorded observation, so this is a command counterfactual on synthetic model traces, not live replay.",
                "",
                f"- Maximum tangential command leakage into the estimated normal direction: {summary['same_input_command_replay']['max_tangential_normal_leak_m_s']:.3g} m/s.",
                f"- Final joint-velocity realization calls observed: {summary['same_input_command_replay']['final_realization_calls']} per tick.",
                f"- Dual-space policy preserved the TASE normal/orientation intent under tool-contact fixture; maximum normal difference {summary['dual_space_collision_policy']['tool_collision_max_normal_intent_error_m_s']:.3g} m/s and orientation difference {summary['dual_space_collision_policy']['tool_collision_max_orientation_intent_error_rad_s']:.3g} rad/s.",
                "- The existing dual-space policy returned link-yield preferences as offline intents and requested the single recovery owner when the synthetic loaded lateral error exceeded its test-fixture corridor.",
                "- Event-scoring and board-mark-detection interfaces are retained locally (`tase_joint_ee_apparatus_collision_v1`, `contact-board-marks.v1`). This run contains zero instrumented-apparatus trials and zero qualified image-mark trials.",
                "- Joint-observer receipt, joint torque, and corridor error in this section are synthetic unit-test fixtures. No qualified instrumented observer, calibrated camera corridor, apparatus push, or human push was used.",
                "",
                "## Historical trace replay boundary",
                "",
                f"A scoped audit of `{historical_audit['attempt_dir']}` found raw-sensor fields `{historical_audit.get('raw_sensor_first_row_fields', [])}`. Same-input alternate-controller replay eligibility for that sampled raw-sensor stream is `{historical_audit['same_input_alternate_controller_replay_eligible']}`; missing required fields are `{historical_audit['missing_required_fields']}`. This single sampled stream is not a survey of every historical artifact. The new model traces include the fields needed for their explicitly model-only same-input replay.",
                "",
            ])
            report_path.write_text(report, encoding="utf-8")
            summary["artifacts"]["report_sha256"] = hashlib.sha256(report_path.read_bytes()).hexdigest()
        summary["artifacts"]["command_replay_sha256"] = hashlib.sha256(outputs_path.read_bytes()).hexdigest()
        summary_path.write_text(json.dumps(summary, indent=2, sort_keys=True, allow_nan=False) + "\n", encoding="utf-8")
    return document


def refresh_final_report(*, comparison_dir: Path) -> Path:
    """Re-render the summary table and retain the replay appendix verbatim."""
    root = Path(comparison_dir).resolve()
    summary_path = root / "summary.json"
    report_path = root / "report.md"
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    old_report = report_path.read_text(encoding="utf-8") if report_path.is_file() else ""
    marker = "## Same-input command replay and dual-space intent"
    appendix = ""
    if marker in old_report:
        appendix = marker + old_report.split(marker, 1)[1]
    report = comparison._render_report(summary).rstrip() + "\n\n"
    if appendix:
        report += appendix.rstrip() + "\n"
    report_path.write_text(report, encoding="utf-8")
    summary.setdefault("artifacts", {})["report_sha256"] = hashlib.sha256(report_path.read_bytes()).hexdigest()
    summary_path.write_text(json.dumps(summary, indent=2, sort_keys=True, allow_nan=False) + "\n", encoding="utf-8")
    return report_path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--comparison-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    result = run_replay(comparison_dir=args.comparison_dir, output_path=args.output)
    print(json.dumps({
        "schema": result["schema"],
        "same_input_replay_units": result["same_input_replay_units"],
        "controller_replays": result["controller_replays"],
        "output": str((args.output or (args.comparison_dir / "command-replay.json")).resolve()),
        "physical_evidence": result["physical_evidence"],
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["SCHEMA", "run_replay", "refresh_final_report"]
