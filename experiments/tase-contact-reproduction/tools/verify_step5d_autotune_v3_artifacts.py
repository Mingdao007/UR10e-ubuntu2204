#!/usr/bin/env python3
"""Verify the immutable offline, TP V3, read-back, and URSim evidence bundle."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
from pathlib import Path
from typing import Any, Mapping, Sequence

from step5d_autotune_v3.profile import (
    ContractViolation,
    contract_sha256,
    control_fingerprint,
    load_contract,
)
from step5d_autotune_v3.state import StateError, orchestration_fingerprint


ROOT = Path(__file__).resolve().parents[1]
V3_STAGE_ID = "step5d_strict_rnn_autotune_v3"
V1_STAGE_ID = "step5d_strict_rnn_autotune_v1"
TP_FINGERPRINT = "62cdda2e967d4c8d95d3b356751ff553ff8534043865ba957f638a810d45d8bc"
LEDGER_SHA256 = "19cf2241ea070e3dc8eccfbe118660104f4c3f8e40ea25cb6f0efecabc7acf99"
URSIM_IMAGE = (
    "universalrobots/ursim_e-series:5.25.2@sha256:"
    "a4c4365207d54d1a1a4ead87526ff3781e2e98ae703c72f362060a46688fa7a4"
)
URSIM_RAW_SHA256 = "b9e7c21709b6b85b8eef6312f95ffd094ce42d20370e8df7d00c3fae59cca41c"
URSIM_RESULT_SHA256 = "a6ea4c2251f97d3949032f58c036f8a3974a3c41c21111d28b8b2deeeddf742e"
EXPECTED_SHA256 = {
    "programs/step5/step5d/step5d_strict_rnn_autotune_v3.script":
        "97ca4a9e035bc0f5b9a2345adb8711ec3275886c0939fb0f23c4a5aecd4c4f8f",
    "programs/step5/step5d/step5d_strict_rnn_autotune_v3.txt":
        "fa503d6e9db062f196b66c1125075aa4503854a329a399b31f6b7a899f955692",
    "programs/step5/step5d/step5d_strict_rnn_autotune_v3.urp":
        "5202d7783421cbecc6ca89d3e1c6c709d34f01868ae83d4ce0f01acbbfe7b129",
    "programs/step5/step5d/step5d_strict_rnn_autotune_v3.deploy-manifest.json":
        TP_FINGERPRINT,
    "config/step5/tp_watchdog_v2.json":
        "fdea0f91364b7bf67c162b352e634b439671fdce2b42a4b07f531c05ad6c2750",
    "config/step5/tp_watchdog_blocks/host_heartbeat_fail_closed_v2.script":
        "fe0c5a2e991fcc065efde75760f5c164b14f161cc5c2b8668941c64fbf4aa8e4",
    "config/step5d_autotune_controller_readback_v3.json":
        "c6d33760cb6113d4a1fa60a9099aacda79b134ab27a4c109b47672d16d2a2ba9",
    "config/step5/step5d_autotune_v3_attempt_ledger.json": LEDGER_SHA256,
    "config/step5/step5d_autotune_v3_control_contract.json":
        "6a06f61583200e812e4fc5d958a426fc02d9fa6fbd8d5d6e73e2723df34844ad",
    "config/step5/golden_replay_g10_v1.json":
        "f56a3eef529494b6c209ca5534abec082519848446724a94426de522852260f8",
    "config/step5/golden_replay_g10_v3_result.json":
        "faab489644a52f554d01c30e4d51916173aa0b0701332438d7358e04bfca7567",
    "config/step5/step5d_autotune_v3_ursim_hold_raw.json": URSIM_RAW_SHA256,
    "config/step5/step5d_autotune_v3_ursim_hold_result.json": URSIM_RESULT_SHA256,
    "config/step5/step5d_autotune_v3_offline_validation.json":
        "e37dff069293d9dbf6e2b5881b946394a1f16cda65d970adfb8b47caa9b873e7",
    "config/step5d/manifests/step5d_strict_rnn_autotune_v3/runtime_calibration.json":
        "70229a0c94d4a546c1a5f27e033bf34a8c0d302776c3227a39d857f12a366a48",
}
TRIPLET_SHA256 = {
    ".script": EXPECTED_SHA256[
        "programs/step5/step5d/step5d_strict_rnn_autotune_v3.script"
    ],
    ".txt": EXPECTED_SHA256[
        "programs/step5/step5d/step5d_strict_rnn_autotune_v3.txt"
    ],
    ".urp": EXPECTED_SHA256[
        "programs/step5/step5d/step5d_strict_rnn_autotune_v3.urp"
    ],
}
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class ArtifactVerificationError(RuntimeError):
    """The immutable evidence bundle or inactive selector is inconsistent."""


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _unique_object(pairs: Sequence[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ArtifactVerificationError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _load_json(path: Path, *, role: str) -> Mapping[str, Any]:
    try:
        payload = json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=_unique_object,
            parse_constant=lambda value: (_ for _ in ()).throw(
                ArtifactVerificationError(f"non-finite JSON constant: {value}")
            ),
        )
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ArtifactVerificationError(f"{role} is not valid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise ArtifactVerificationError(f"{role} must be an object")
    return payload


def _require_equal(actual: Any, expected: Any, role: str) -> None:
    if actual != expected:
        raise ArtifactVerificationError(
            f"{role} differs: expected={expected!r}, observed={actual!r}"
        )


def _verify_exact_files(root: Path) -> None:
    for relative, expected in EXPECTED_SHA256.items():
        path = root / relative
        if path.is_symlink() or not path.is_file():
            raise ArtifactVerificationError(f"immutable artifact missing or unsafe: {relative}")
        observed = _sha256(path)
        if observed != expected:
            raise ArtifactVerificationError(
                f"immutable artifact digest differs: {relative}: {observed}"
            )


def verify(root: Path = ROOT) -> dict[str, Any]:
    root = root.expanduser().resolve(strict=True)
    _verify_exact_files(root)
    try:
        contract = load_contract(
            root / "config/step5/step5d_autotune_v3_control_contract.json"
        )
        current_contract_sha = contract_sha256(contract)
        current_control_fingerprint = control_fingerprint(contract)
        current_orchestration_fingerprint = orchestration_fingerprint(root)
    except (ContractViolation, StateError) as exc:
        raise ArtifactVerificationError(f"current source fingerprint failed: {exc}") from exc

    deploy = _load_json(
        root / "programs/step5/step5d/step5d_strict_rnn_autotune_v3.deploy-manifest.json",
        role="TP deploy manifest",
    )
    _require_equal(deploy.get("schema_version"), 1, "TP deploy schema")
    _require_equal(deploy.get("basename"), V3_STAGE_ID, "TP basename")
    expected_artifacts = [
        {
            "filename": f"{V3_STAGE_ID}{extension}",
            "sha256": digest,
            "source": f"{V3_STAGE_ID}{extension}",
        }
        for extension, digest in TRIPLET_SHA256.items()
    ]
    _require_equal(deploy.get("artifacts"), expected_artifacts, "TP deploy artifacts")

    readback = _load_json(
        root / "config/step5d_autotune_controller_readback_v3.json",
        role="controller readback attestation",
    )
    _require_equal(readback.get("schema"), "step5d.autotune.controller-readback/v3", "readback schema")
    _require_equal(readback.get("verified"), True, "fresh readback verification")
    _require_equal(readback.get("tp_fingerprint"), TP_FINGERPRINT, "TP fingerprint")
    _require_equal(readback.get("triplet_sha256"), TRIPLET_SHA256, "readback triplet")

    watchdog = _load_json(
        root / "config/step5/tp_watchdog_v2.json", role="TP watchdog manifest"
    )
    for field in ("control_math_changed", "trajectory_changed", "waypoint_changed"):
        _require_equal(watchdog.get(field), False, f"watchdog {field}")
    _require_equal(
        watchdog.get("auto_home_after_heartbeat_loss"),
        False,
        "watchdog auto-home policy",
    )
    delivery = watchdog.get("controller_delivery") or {}
    if not isinstance(delivery, dict) or set(delivery.values()) != {True}:
        raise ArtifactVerificationError("watchdog controller-delivery proof is incomplete")
    blocks = (watchdog.get("watchdog_diff_gate") or {}).get("required_blocks")
    expected_block = [{
        "block_id": "host_heartbeat_fail_closed_v2",
        "source": "config/step5/tp_watchdog_blocks/host_heartbeat_fail_closed_v2.script",
        "sha256": EXPECTED_SHA256[
            "config/step5/tp_watchdog_blocks/host_heartbeat_fail_closed_v2.script"
        ],
        "required_in": [".script", ".urp"],
    }]
    _require_equal(blocks, expected_block, "watchdog reviewed block")

    current = _load_json(root / "config/current_stage.json", role="current selector")
    _require_equal(current.get("current_stage_id"), V1_STAGE_ID, "current v1 selector")
    _require_equal(current.get("program"), V1_STAGE_ID, "current v1 program")

    table = _load_json(root / "config/step5_stage_table.json", role="Step5 stage table")
    rows = [row for row in table.get("stages", []) if isinstance(row, dict)]
    matches = [row for row in rows if row.get("id") == V3_STAGE_ID]
    if len(matches) != 1:
        raise ArtifactVerificationError("inactive v3 selector row must exist exactly once")
    v3 = matches[0]
    for field, expected in (("active", False), ("blocked", True), ("bridge", False)):
        _require_equal(v3.get(field), expected, f"v3 selector {field}")
    binding = v3.get("current_binding") or {}
    _require_equal(binding.get("is_current"), False, "v3 current binding")
    _require_equal(binding.get("live_authorized"), False, "v3 live authorization")
    package = v3.get("package_delivery") or {}
    _require_equal(
        package.get("status"),
        "controller_readback_verified_inactive",
        "v3 package delivery status",
    )
    _require_equal(package.get("controller_uploaded_by_v3"), True, "v3 upload claim")
    _require_equal(package.get("controller_readback_verified"), True, "v3 readback claim")
    _require_equal(package.get("tp_fingerprint"), TP_FINGERPRINT, "v3 TP binding")
    _require_equal(package.get("sha256"), TRIPLET_SHA256, "v3 triplet binding")
    _require_equal(
        package.get("fresh_controller_sha_at"),
        "2026-07-19T00:24:04+08:00",
        "v3 fresh controller SHA timestamp",
    )
    _require_equal(
        package.get("controller_readback_manifest"),
        "config/step5d_autotune_controller_readback_v3.json",
        "v3 readback source",
    )
    readiness = v3.get("execution_readiness") or {}
    _require_equal(
        readiness.get("state"), "ready_for_hil_full_bridge_hold", "v3 readiness state"
    )
    _require_equal(
        readiness.get("public_success_signal"),
        "ready_for_hil_full_bridge_hold",
        "v3 public success signal",
    )
    _require_equal(readiness.get("package_delivery_complete"), True, "v3 package readiness")
    for field in (
        "hil_no_motion_complete",
        "candidate_current",
        "candidate_live_authorized",
        "live_runtime_promoted",
        "same_process_startup_gate_complete",
        "ready_to_execute",
        "ready_to_load_play",
        "ready_to_start_bridge",
        "ready_to_arm",
        "ready_for_contact_or_motion",
    ):
        _require_equal(readiness.get(field), False, f"v3 readiness {field}")
    operator_trigger = readiness.get("operator_trigger") or {}
    _require_equal(
        operator_trigger.get("user_confirmation_required"),
        False,
        "v3 user confirmation policy",
    )
    _require_equal(
        operator_trigger.get("candidate_stage_id"), V3_STAGE_ID, "v3 operator trigger identity"
    )
    _require_equal(
        operator_trigger.get("internal_launch_binding"),
        "process_fingerprint_and_campaign_bound",
        "v3 internal launch binding",
    )
    migration = v3.get("migration") or {}
    _require_equal(migration.get("ledger_sha256"), LEDGER_SHA256, "v3 ledger binding")
    _require_equal(
        migration.get("pending_or_automatic_retry_imported"),
        False,
        "v3 migration retry policy",
    )

    g10 = _load_json(
        root / "config/step5/golden_replay_g10_v3_result.json",
        role="G10 formal replay result",
    )
    _require_equal(g10.get("ok"), True, "G10 formal replay result")
    _require_equal(g10.get("no_motion"), True, "G10 no-motion boundary")
    _require_equal(g10.get("group_id"), "G10", "G10 group identity")
    metrics = g10.get("executable_replay") or {}
    if not isinstance(metrics, dict):
        raise ArtifactVerificationError("G10 formal replay metrics are missing")
    if any(
        (
            metrics.get("structural_failure_rows") != 0,
            float(metrics.get("rnn_oracle_qdot_delta_max_rad_s", math.inf)) > 1e-6,
            float(metrics.get("qdot_max_abs_rad_s", math.inf)) > 0.5,
            float(metrics.get("slew_violation_max_rad_s", math.inf)) > 1e-12,
        )
    ):
        raise ArtifactVerificationError("G10 formal replay thresholds differ")

    validation = _load_json(
        root / "config/step5/step5d_autotune_v3_offline_validation.json",
        role="offline validation decision",
    )
    decision = validation.get("decision") or {}
    validation_identity = validation.get("identity") or {}
    _require_equal(
        validation_identity.get("contract_sha256"),
        current_contract_sha,
        "current contract fingerprint",
    )
    _require_equal(
        validation_identity.get("control_fingerprint"),
        current_control_fingerprint,
        "current control fingerprint",
    )
    _require_equal(
        validation_identity.get("orchestration_fingerprint"),
        current_orchestration_fingerprint,
        "current orchestration fingerprint",
    )
    _require_equal(decision.get("go_no_go"), "go", "v3 offline decision")
    _require_equal(
        decision.get("acceptance_scope"),
        "offline_tooling_and_ursim_hold_only",
        "v3 acceptance scope",
    )
    _require_equal(decision.get("rollout_authorized"), False, "v3 rollout authorization")
    _require_equal(decision.get("current_selector"), V1_STAGE_ID, "rollback selector")
    _require_equal(decision.get("v3_active"), False, "v3 inactive decision")
    _require_equal(
        decision.get("package_delivery"),
        "controller_readback_verified_explicit_v3",
        "v3 package delivery decision",
    )
    _require_equal(
        decision.get("execution_readiness"),
        "ready_for_hil_full_bridge_hold",
        "v3 execution readiness decision",
    )

    ursim = _load_json(
        root / "config/step5/step5d_autotune_v3_ursim_hold_result.json",
        role="immutable URSim HOLD result",
    )
    _require_equal(ursim.get("status"), "pass", "URSim HOLD result")
    _require_equal(
        ursim.get("acceptance_scope"),
        "offline_tooling_and_ursim_hold_only",
        "URSim acceptance scope",
    )
    identity = ursim.get("identity") or {}
    for field in ("control_fingerprint", "orchestration_fingerprint", "contract_sha256"):
        _require_equal(
            identity.get(field),
            validation_identity.get(field),
            f"URSim validation identity {field}",
        )
    image = ursim.get("image") or {}
    _require_equal(image.get("reference"), URSIM_IMAGE, "URSim image digest")
    _require_equal(image.get("entrypoint"), ["/entrypoint.sh"], "URSim entrypoint")
    _require_equal(image.get("entrypoint_overridden"), False, "URSim entrypoint policy")
    _require_equal(image.get("robot_model"), "UR10", "URSim robot model")
    network = ursim.get("network") or {}
    for field, expected in (
        ("internal", True),
        ("external_network_connected", False),
        ("robot_network_connected", False),
        ("host_ports_published", False),
        ("removed_after_gate", True),
    ):
        _require_equal(network.get(field), expected, f"URSim network {field}")
    container = ursim.get("container_lifecycle") or {}
    for field, expected in (
        ("privileged", False),
        ("mounts", []),
        ("devices", []),
        ("removed_after_gate", True),
    ):
        _require_equal(container.get(field), expected, f"URSim container {field}")
    lifecycle = ursim.get("service_lifecycle") or {}
    _require_equal(
        (lifecycle.get("observed") or [])[:3],
        ["STOPPED", "STARTING", "READY_HOME"],
        "URSim service lifecycle prefix",
    )
    for field in ("hardware_enabled", "bridge_started", "controller_touched", "tp_started"):
        _require_equal(lifecycle.get(field), False, f"URSim service {field}")
    hold = ursim.get("hold") or {}
    _require_equal(hold.get("dashboard_program_state"), "STOPPED", "URSim program state")
    _require_equal(hold.get("dashboard_running"), False, "URSim running state")
    _require_equal(hold.get("rtde_output_recipe_only"), True, "URSim RTDE recipe")
    _require_equal(hold.get("rtde_input_recipe_created"), False, "URSim RTDE input recipe")
    for field in (
        "max_abs_actual_qd_rad_s",
        "max_abs_actual_tcp_speed",
        "play_count",
        "arm_count",
        "motion_count",
        "controller_write_count",
        "forbidden_action_count",
    ):
        _require_equal(hold.get(field), 0, f"URSim HOLD {field}")
    watchdog_result = ursim.get("watchdog") or {}
    _require_equal(watchdog_result.get("hold_monitor_passed"), True, "URSim watchdog")
    _require_equal(
        watchdog_result.get("immutable_tp_watchdog_artifact_verified"),
        True,
        "URSim immutable TP watchdog",
    )
    rollback = ursim.get("rollback") or {}
    _require_equal(rollback.get("service_exit_code"), 0, "URSim service rollback exit")
    _require_equal(rollback.get("service_final_phase"), "stopped", "URSim rollback phase")
    _require_equal(rollback.get("v1_selector_before"), V1_STAGE_ID, "URSim rollback before")
    _require_equal(rollback.get("v1_selector_after"), V1_STAGE_ID, "URSim rollback after")
    _require_equal(rollback.get("v3_active_after"), False, "URSim rollback v3 state")
    raw = ursim.get("raw_evidence") or {}
    _require_equal(raw.get("sha256"), URSIM_RAW_SHA256, "URSim raw evidence binding")
    _require_equal(raw.get("byte_identical_copy_verified"), True, "URSim raw evidence copy")
    ursim_decision = ursim.get("decision") or {}
    _require_equal(ursim_decision.get("offline_acceptance"), "go", "URSim offline decision")
    _require_equal(
        ursim_decision.get("live_robot_acceptance"),
        "not_run_not_authorized",
        "URSim live boundary",
    )
    _require_equal(ursim_decision.get("rollout_authorized"), False, "URSim rollout")
    _require_equal(ursim_decision.get("v3_active"), False, "URSim v3 activity")
    _require_equal(ursim_decision.get("current_selector"), V1_STAGE_ID, "URSim selector")
    ursim_gate = (validation.get("gates") or {}).get("ursim_hold_only") or {}
    _require_equal(ursim_gate.get("status"), "pass", "offline URSim gate")
    _require_equal(ursim_gate.get("result_sha256"), URSIM_RESULT_SHA256, "URSim result binding")

    matrix = _load_json(
        root / "config/step5d_autotune_v3_test_matrix.json",
        role="Step5d v3 test matrix",
    )
    _require_equal(
        matrix.get("claim_boundary"),
        "offline_tooling_and_ursim_hold_only",
        "test matrix claim boundary",
    )
    _require_equal(
        matrix.get("evidence_manifest"),
        "config/step5d/manifests/step5d_strict_rnn_autotune_v3/test_evidence.json",
        "test matrix evidence manifest",
    )
    readiness_gate = matrix.get("operator_readiness_gate") or {}
    _require_equal(
        readiness_gate.get("command"),
        ["python3", "tools/verify_step5d_autotune_v3_execution_readiness.py", "--json"],
        "test matrix operator readiness command",
    )
    _require_equal(
        readiness_gate.get("public_success_signal_policy"),
        "next_legal_action_not_broad_pass",
        "test matrix operator success signal",
    )
    _require_equal(
        readiness_gate.get("user_confirmation_required"),
        False,
        "test matrix user confirmation policy",
    )
    if "hil_no_motion_pass" not in readiness_gate.get("ready_to_execute_requires", []):
        raise ArtifactVerificationError("test matrix execution readiness omits HIL")
    hil_permit = matrix.get("hil_launch_permit_gate") or {}
    _require_equal(
        hil_permit.get("command"),
        [
            "python3",
            "tools/verify_step5d_autotune_v3_hil_authorization.py",
            "--json",
        ],
        "test matrix HIL launch permit command",
    )
    for field, expected in (
        ("scope", "hil_full_bridge_hold"),
        ("candidate_stage_id", V3_STAGE_ID),
        ("current_fingerprint_binding_required", True),
        ("parent_process_binding_required", True),
        ("serial", True),
        ("hold_required", True),
        ("live_writer_allowed", True),
        ("user_confirmation_required", False),
    ):
        _require_equal(
            hil_permit.get(field), expected, f"test matrix HIL launch permit {field}"
        )
    large = (matrix.get("lanes") or {}).get("large_ursim") or {}
    _require_equal(large.get("container_image"), URSIM_IMAGE, "large URSim image")
    _require_equal(
        large.get("simulator_polyscope_version"), "5.25.2", "large URSim PolyScope"
    )
    _require_equal(
        large.get("target_polyscope_version_equivalence_claimed"),
        False,
        "large URSim target-version claim boundary",
    )
    for field, expected in (
        ("external_network_allowed", False),
        ("robot_network_allowed", False),
        ("host_port_publication_allowed", False),
        ("rtde_input_recipe_allowed", False),
        ("controller_access_allowed", False),
        ("live_writer_allowed", False),
        ("arm_allowed", False),
        ("motion_allowed", False),
    ):
        _require_equal(large.get(field), expected, f"large URSim {field}")
    evidence = _load_json(
        root / "config/step5d/manifests/step5d_strict_rnn_autotune_v3/test_evidence.json",
        role="Step5d v3 test evidence",
    )
    _require_equal(evidence.get("schema"), "step5d.autotune-v3/test-evidence-v1", "test evidence schema")
    evidence_large = (evidence.get("lanes") or {}).get("large_ursim") or {}
    _require_equal(evidence_large.get("status"), "pass", "large URSim evidence status")
    _require_equal(evidence_large.get("result_sha256"), URSIM_RESULT_SHA256, "large URSim result")
    _require_equal(evidence_large.get("raw_evidence_sha256"), URSIM_RAW_SHA256, "large URSim raw evidence")
    _require_equal(evidence_large.get("cleanup_completed"), True, "large URSim cleanup")

    fingerprint_input = json.dumps(
        {
            "tp_fingerprint": TP_FINGERPRINT,
            "ledger_sha256": LEDGER_SHA256,
            "immutable_artifacts": EXPECTED_SHA256,
        },
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return {
        "schema": "step5d.autotune-v3/immutable-artifact-report-v1",
        "ok": True,
        "current_stage_id": V1_STAGE_ID,
        "v3_stage_id": V3_STAGE_ID,
        "v3_active": False,
        "execution_readiness": "ready_for_hil_full_bridge_hold",
        "ready_to_execute": False,
        "acceptance_scope": "offline_tooling_and_ursim_hold_only",
        "rollout_authorized": False,
        "live_robot_acceptance": "not_run_not_authorized",
        "ursim_result_sha256": URSIM_RESULT_SHA256,
        "control_fingerprint": current_control_fingerprint,
        "orchestration_fingerprint": current_orchestration_fingerprint,
        "tp_fingerprint": TP_FINGERPRINT,
        "attempt_ledger_sha256": LEDGER_SHA256,
        "artifact_set_fingerprint": hashlib.sha256(fingerprint_input).hexdigest(),
        "verified_paths": sorted(EXPECTED_SHA256),
    }


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--json", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    report = verify(args.root)
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print("step5d_autotune_v3_artifacts=pass")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
