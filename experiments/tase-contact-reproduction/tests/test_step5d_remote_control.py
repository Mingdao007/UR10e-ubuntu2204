#!/usr/bin/env python3
"""Offline tests for the strict-RNN Step5d Remote preparation release."""

from __future__ import annotations

import contextlib
import hashlib
import io
import os
import json
import re
import subprocess
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


EXPERIMENT_ROOT = Path(__file__).resolve().parents[1]
REPOSITORY_ROOT = EXPERIMENT_ROOT.parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT / "src/ur10e_experiment_runtime"))
sys.path.insert(0, str(EXPERIMENT_ROOT / "tools"))

from step5c_strict_rnn import StrictRnnCommandResult  # noqa: E402
from step5d_autotune_contract import ForceCandidate  # noqa: E402
from step5d_autotune_v3.release_identity import ROLLING_PROTOCOL  # noqa: E402
from step5d_autotune_v3.runtime_profile import DEFAULT_OVERLAY  # noqa: E402
from step5d_control_contract import (  # noqa: E402
    SafetyEnvelope,
    Step5dObservation,
    StrictRnnControlPolicy,
)
from step5d_remote_control.contracts import (  # noqa: E402
    RECEIPT_SCHEMA,
    TRIAL_SOURCE_SCHEMA,
    RemoteControlError,
    RemoteExecutionUid,
    RemoteParameterProjection,
    REMOTE_EXECUTION_UID_SCHEMA,
    REMOTE_EXECUTION_UID_VERSION,
    REQUIRED_SOURCE_PATHS,
    canonical_sha256,
    import_result,
    load_prepared_control_trial,
    load_remote_release,
    materialize_controller_config,
    prepare_control_trial,
    validate_receipt,
)
from step5d_remote_control.runtime import (  # noqa: E402
    RnnOnlyExecutor,
    build_executor,
)
from ur10e_experiment_runtime.candidate_identity import (  # noqa: E402
    ControlCandidateUid,
    OccurrenceUid,
    ParameterUid,
    TransportCandidateUid,
)


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _trial_source() -> dict[str, object]:
    overlay = dict(DEFAULT_OVERLAY)
    control_uid = ControlCandidateUid.parse(overlay["control_candidate_uid"])
    occurrence_uid = OccurrenceUid.from_control(
        control_uid,
        protocol=ROLLING_PROTOCOL,
        logical_batch_sequence=3,
        row_index=4,
        plan_revision=7,
        selection_role="bo_candidate",
        replicate_ordinal=1,
    )
    candidate = ForceCandidate(
        force_p_gain=overlay["force_p_gain"],
        force_i_gain=overlay["force_i_gain"],
        force_damping=overlay["force_damping"],
    )
    transport_uid = TransportCandidateUid.from_occurrence(
        occurrence_uid,
        parameter_uid=ParameterUid.from_candidate_digest(candidate.candidate_uid),
        protocol=ROLLING_PROTOCOL,
    )
    return {
        "schema": TRIAL_SOURCE_SCHEMA,
        "campaign_id": "campaign-remote-test",
        "campaign_fingerprint": "1" * 64,
        "trial_uid": "2" * 64,
        "source_protocol": ROLLING_PROTOCOL,
        "logical_batch_sequence": 3,
        "plan_revision": 7,
        "occurrence_uid": str(occurrence_uid),
        "transport_candidate_uid": str(transport_uid),
        "batch_row_index": 4,
        "selection_role": "bo_candidate",
        "replicate_ordinal": 1,
        "profile_id": "nf100-slew050-a050",
        "plant_epoch": 11,
        "trial_overlay": overlay,
    }


def _observation() -> Step5dObservation:
    identity = tuple(
        tuple(float(row == column) for column in range(6)) for row in range(6)
    )
    return Step5dObservation(
        sequence=1,
        timestamp_s=0.002,
        q=(0.0,) * 6,
        qd=(0.0,) * 6,
        tcp_pose=(0.0,) * 6,
        tcp_twist=(0.0,) * 6,
        wrench=(0.0,) * 6,
        jacobian=identity,
        desired_twist=(0.0, 0.0, 0.001, 0.0, 0.0, 0.0),
        reaction_normal=(0.0, 0.0, -1.0),
        approach_normal=(0.0, 0.0, 1.0),
        command_frame="base",
        normal_frame="base",
        omega_minus=(-0.5,) * 6,
        omega_plus=(0.5,) * 6,
        normal_motion_policy="diagnostic_only",
        dt_s=0.002,
    )


REMOTE_SCRIPT = EXPERIMENT_ROOT / "scripts/step5d-autotune-v3-remote.sh"


class RemoteOperatorSurfaceTest(unittest.TestCase):
    def test_remote_script_has_strict_syntax(self) -> None:
        result = subprocess.run(
            ["bash", "-n", str(REMOTE_SCRIPT)],
            cwd=EXPERIMENT_ROOT,
            text=True,
            capture_output=True,
            check=False,
            env={**os.environ, "PYTHONPATH": str(EXPERIMENT_ROOT / "tools")},
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_remote_script_help_lists_only_allowed_commands(self) -> None:
        result = subprocess.run(
            [str(REMOTE_SCRIPT), "--help"],
            cwd=EXPERIMENT_ROOT,
            text=True,
            capture_output=True,
            check=False,
            env={**os.environ, "PYTHONPATH": str(EXPERIMENT_ROOT / "tools")},
        )
        self.assertEqual(result.returncode, 0)
        self.assertIn("step5d-autotune-v3-remote.sh <command>", result.stdout)
        for command in (
            "status",
            "validate-release",
            "prepare-trial",
            "replay",
            "check",
        ):
            self.assertIn(f"{command}", result.stdout)

    def test_remote_script_rejects_live_route(self) -> None:
        result = subprocess.run(
            [str(REMOTE_SCRIPT), "live"],
            cwd=EXPERIMENT_ROOT,
            text=True,
            capture_output=True,
            check=False,
            env={**os.environ, "PYTHONPATH": str(EXPERIMENT_ROOT / "tools")},
        )
        self.assertEqual(result.returncode, 64)
        self.assertIn("unsupported command: live", result.stderr)

    def test_remote_core_has_no_forbidden_runtime_imports(self) -> None:
        package_root = EXPERIMENT_ROOT / "tools/step5d_remote_control"
        forbidden_patterns = (
            re.compile(r"^\s*(?:import|from)\s+rclpy\b", re.MULTILINE),
            re.compile(r"^\s*(?:import|from)\s+socket\b", re.MULTILINE),
            re.compile(r"^\s*(?:import|from)\s+network\b", re.MULTILINE),
            re.compile(r"^\s*(?:import|from)\s+subprocess\b", re.MULTILINE),
            re.compile(r"^\s*(?:import|from)\s+controller_manager\b", re.MULTILINE),
            re.compile(r"^\s*(?:import|from)\s+optimizer\b", re.MULTILINE),
        )
        for path in sorted(package_root.glob("*.py")):
            source = path.read_text(encoding="utf-8")
            for pattern in forbidden_patterns:
                self.assertIsNone(
                    pattern.search(source),
                    f"{path}: forbidden import pattern {pattern.pattern}",
                )


class _FakeStrictSolver:
    def __init__(self) -> None:
        self.config = SimpleNamespace(epsilon=0.01, sigr_exponent_r=0.8)
        self.reset_count = 0
        self.solve_count = 0

    def reset_state(self) -> None:
        self.reset_count += 1

    def solve(self, *, actual_q, actual_qd, target_state):
        del actual_q, actual_qd
        self.solve_count += 1
        qdot = tuple(float(value) for value in target_state["xdot_c"])
        return StrictRnnCommandResult(
            qdot=qdot,
            solver_status=40.0,
            residual_norm=0.0,
            diagnostics={"active_bounds_mask": (False,) * 6},
        )


class RemoteReleaseTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.release = load_remote_release()

    def test_release_is_v2_and_offline_only(self) -> None:
        self.assertEqual(cls_release := self.release.document["schema"], "step5d.remote-control/release-v2")
        self.assertFalse(cls_release and self.release.live_authorized)
        self.assertTrue(self.release.offline_build_only)

    def test_release_inherits_all_numeric_control_parameters_from_v3(self) -> None:
        parameters = self.release.control_parameters
        self.assertEqual(parameters["update_rate_hz"], 500.0)
        self.assertEqual(parameters["qdot_limit_rad_s"], 0.5)
        self.assertEqual(parameters["max_acceleration_rad_s2"], 0.5)
        self.assertEqual(parameters["watchdog_stale_timeout_s"], 5.0 / 500.0)
        self.assertEqual(parameters["rnn_epsilon"], 0.01)
        self.assertEqual(parameters["rnn_sigr_exponent_r"], 0.8)
        self.assertEqual(parameters["rnn_inner_iterations"], 512)
        self.assertEqual(parameters["rnn_backend"], "cupy")
        self.assertFalse(self.release.live_authorized)

    def test_controller_config_is_materialized_and_binded(self) -> None:
        config = materialize_controller_config(self.release)
        manager = config["controller_manager"]["ros__parameters"]
        controller = config["remote_watchdog"]["ros__parameters"]
        self.assertEqual(manager["update_rate"], 500)
        self.assertEqual(
            manager["remote_watchdog"]["type"],
            "ur10e_step5d_remote_watchdog/WatchdogController",
        )
        self.assertEqual(controller["max_abs_velocity_rad_s"], 0.5)
        self.assertEqual(controller["max_acceleration_rad_s2"], 0.5)
        self.assertEqual(controller["stale_timeout_s"], 0.01)
        self.assertEqual(len(controller["joints"]), 6)
        self.assertEqual(
            self.release.controller_config_sha256, canonical_sha256(config)
        )

    def test_source_hash_drift_fails_before_use(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            release_document = dict(self.release.document)
            source_sha = dict(release_document["source_sha256"])
            source_sha[
                "experiments/tase-contact-reproduction/config/step5d_liveprep_solver_gate.json"
            ] = "0" * 64
            release_document["source_sha256"] = source_sha
            release_path = Path(temporary) / "release.json"
            release_path.write_text(json.dumps(release_document), encoding="utf-8")
            with self.assertRaisesRegex(RemoteControlError, "source drifted"):
                load_remote_release(release_path, experiment_root=EXPERIMENT_ROOT)

    def test_governed_source_cannot_be_removed_from_release(self) -> None:
        self.assertEqual(
            set(self.release.document["source_sha256"]), REQUIRED_SOURCE_PATHS
        )
        with tempfile.TemporaryDirectory() as temporary:
            release_document = dict(self.release.document)
            source_sha = dict(release_document["source_sha256"])
            source_sha.pop("experiments/tase-contact-reproduction/STEP5_FLOW.md")
            release_document["source_sha256"] = source_sha
            release_path = Path(temporary) / "release.json"
            release_path.write_text(json.dumps(release_document), encoding="utf-8")
            with self.assertRaisesRegex(RemoteControlError, "source set differs"):
                load_remote_release(release_path, experiment_root=EXPERIMENT_ROOT)

    def test_frozen_core_documents_are_unchanged(self) -> None:
        digest_key = "experiments/tase-contact-reproduction/tools/step5d_control_contract.py"
        relative = "tools/step5d_control_contract.py"
        self.assertEqual(
            _sha(EXPERIMENT_ROOT / relative),
            self.release.document["source_sha256"][digest_key],
        )


class RemoteIdentityTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.release = load_remote_release()

    def test_remote_parameter_projection_is_exactly_ten_fields(self) -> None:
        projection = RemoteParameterProjection.from_release(self.release.control_parameters)
        payload = projection.as_payload()
        expected_fields = [
            "update_rate_hz",
            "qdot_limit_rad_s",
            "max_acceleration_rad_s2",
            "host_slew_rad_s2",
            "actuator_acceleration_rad_s2",
            "watchdog_stale_timeout_s",
            "rnn_epsilon",
            "rnn_sigr_exponent_r",
            "rnn_inner_iterations",
            "rnn_backend",
        ]
        self.assertEqual(list(payload), expected_fields)
        self.assertEqual(payload, dict(self.release.control_parameters))

    def test_execution_uid_is_route_projected_and_hash_bound(self) -> None:
        source = _trial_source()
        prepared = prepare_control_trial(source, release=self.release)
        expected_uid = RemoteExecutionUid.from_source(
            source["transport_candidate_uid"],
            transport_id=self.release.transport_id,
            release_sha256=self.release.release_sha256,
            controller_config_sha256=self.release.controller_config_sha256,
        )
        self.assertEqual(expected_uid.schema, REMOTE_EXECUTION_UID_SCHEMA)
        self.assertEqual(expected_uid.version, REMOTE_EXECUTION_UID_VERSION)
        self.assertEqual(prepared.execution_uid, str(expected_uid.uid))
        changed_route_uid = RemoteExecutionUid.from_source(
            source["transport_candidate_uid"],
            transport_id="route-2",
            release_sha256=self.release.release_sha256,
            controller_config_sha256=self.release.controller_config_sha256,
        )
        self.assertNotEqual(expected_uid.uid, changed_route_uid.uid)

    def test_cross_route_receipt_rejected_by_execution_uid(self) -> None:
        prepared = prepare_control_trial(_trial_source(), release=self.release)
        route_divergent_uid = RemoteExecutionUid.from_source(
            prepared.transport_candidate_uid,
            transport_id=f"{self.release.transport_id}-x",
            release_sha256=self.release.release_sha256,
            controller_config_sha256=self.release.controller_config_sha256,
        )
        receipt = {
            "schema": RECEIPT_SCHEMA,
            "trial_uid": prepared.trial_uid,
            "occurrence_uid": prepared.occurrence_uid,
            "transport_candidate_uid": prepared.transport_candidate_uid,
            "execution_uid": str(route_divergent_uid.uid),
            "envelope_sha256": prepared.envelope_sha256,
            "release_sha256": self.release.release_sha256,
            "transport_id": self.release.transport_id,
            "execution_mode": "offline_replay",
            "outcome": "offline_replay",
            "artifacts_sha256": {},
            "safe_closure": {
                "command_zero_confirmed": True,
                "controller_inactive_confirmed": True,
                "watchdog_status": "inactive",
            },
        }
        with self.assertRaisesRegex(RemoteControlError, "execution_uid differs"):
            validate_receipt(receipt, prepared=prepared, release=self.release)


class RemoteTrialContractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.release = load_remote_release()

    def test_trial_preserves_explicit_occurrence_transport_and_execution_uid(self) -> None:
        source = _trial_source()
        prepared = prepare_control_trial(source, release=self.release)
        self.assertEqual(prepared.remote_parameter_projection, dict(self.release.control_parameters))
        self.assertEqual(prepared.occurrence_uid, source["occurrence_uid"])
        self.assertEqual(
            prepared.transport_candidate_uid,
            source["transport_candidate_uid"],
        )
        self.assertEqual(
            prepared.execution_uid,
            str(
                RemoteExecutionUid.from_source(
                    source["transport_candidate_uid"],
                    transport_id=self.release.transport_id,
                    release_sha256=self.release.release_sha256,
                    controller_config_sha256=self.release.controller_config_sha256,
                )
            ),
        )
        self.assertEqual(prepared.source_protocol, ROLLING_PROTOCOL)
        self.assertEqual(prepared.logical_batch_sequence, 3)
        self.assertEqual(prepared.plan_revision, 7)
        self.assertEqual(prepared.plant_epoch, 11)
        self.assertEqual(prepared.transport_id, self.release.transport_id)
        self.assertEqual(
            prepared.control_candidate_uid,
            prepared.trial_overlay["control_candidate_uid"],
        )
        restored = load_prepared_control_trial(
            prepared.bundle(), release=self.release
        )
        self.assertEqual(restored, prepared)

    def test_occurrence_identity_is_never_inferred(self) -> None:
        source = _trial_source()
        del source["occurrence_uid"]
        with self.assertRaisesRegex(RemoteControlError, "missing=.*occurrence_uid"):
            prepare_control_trial(source, release=self.release)

    def test_cross_namespace_or_material_substitution_is_rejected(self) -> None:
        source = _trial_source()
        source["occurrence_uid"] = source["transport_candidate_uid"]
        with self.assertRaisesRegex(RemoteControlError, "identity chain is invalid"):
            prepare_control_trial(source, release=self.release)

    def test_receipt_works_without_optimizer_eligibility(self) -> None:
        prepared = prepare_control_trial(_trial_source(), release=self.release)
        receipt = {
            "schema": RECEIPT_SCHEMA,
            "trial_uid": prepared.trial_uid,
            "occurrence_uid": prepared.occurrence_uid,
            "transport_candidate_uid": prepared.transport_candidate_uid,
            "execution_uid": prepared.execution_uid,
            "envelope_sha256": prepared.envelope_sha256,
            "release_sha256": self.release.release_sha256,
            "transport_id": self.release.transport_id,
            "execution_mode": "offline_replay",
            "outcome": "offline_replay",
            "artifacts_sha256": {},
            "safe_closure": {
                "command_zero_confirmed": True,
                "controller_inactive_confirmed": True,
                "watchdog_status": "inactive",
            },
        }
        result = import_result(receipt, prepared=prepared, release=self.release)
        self.assertEqual(result["status"], "validated_not_committed")
        self.assertEqual(result["blocker"], "campaign_transport_adapter_not_synced")
        self.assertNotIn("optimizer_eligibility", json.dumps(receipt).lower())
        self.assertNotIn("optimizer_eligible", json.dumps(result).lower())

    def test_receipt_rejects_optimizer_eligibility_input(self) -> None:
        prepared = prepare_control_trial(_trial_source(), release=self.release)
        receipt = {
            "schema": RECEIPT_SCHEMA,
            "trial_uid": prepared.trial_uid,
            "occurrence_uid": prepared.occurrence_uid,
            "transport_candidate_uid": prepared.transport_candidate_uid,
            "execution_uid": prepared.execution_uid,
            "envelope_sha256": prepared.envelope_sha256,
            "release_sha256": self.release.release_sha256,
            "transport_id": self.release.transport_id,
            "execution_mode": "offline_replay",
            "outcome": "offline_replay",
            "artifacts_sha256": {},
            "safe_closure": {
                "command_zero_confirmed": True,
                "controller_inactive_confirmed": True,
                "watchdog_status": "inactive",
            },
            "optimizer_eligible": False,
        }
        with self.assertRaisesRegex(
            RemoteControlError, "fields differ"
        ):
            validate_receipt(receipt, prepared=prepared, release=self.release)

    def test_live_receipt_is_rejected_by_prep_release(self) -> None:
        prepared = prepare_control_trial(_trial_source(), release=self.release)
        receipt = {
            "schema": RECEIPT_SCHEMA,
            "trial_uid": prepared.trial_uid,
            "occurrence_uid": prepared.occurrence_uid,
            "transport_candidate_uid": prepared.transport_candidate_uid,
            "execution_uid": prepared.execution_uid,
            "envelope_sha256": prepared.envelope_sha256,
            "release_sha256": self.release.release_sha256,
            "transport_id": self.release.transport_id,
            "execution_mode": "live",
            "outcome": "completed",
            "artifacts_sha256": {},
            "safe_closure": {
                "command_zero_confirmed": True,
                "controller_inactive_confirmed": True,
                "watchdog_status": "inactive",
            },
        }
        with self.assertRaisesRegex(RemoteControlError, "does not authorize live receipts"):
            validate_receipt(receipt, prepared=prepared, release=self.release)


class RemoteRnnOnlyRuntimeTest(unittest.TestCase):
    def test_every_nonzero_output_is_returned_by_strict_policy(self) -> None:
        solver = _FakeStrictSolver()
        executor = RnnOnlyExecutor(
            StrictRnnControlPolicy(solver),
            safety_envelope=SafetyEnvelope(qdot_cap_rad_s=0.5),
            max_slew_rad_s2=0.5,
            capacity=2,
        )
        result = executor.step(_observation())
        self.assertTrue(result.decision.accepted)
        self.assertEqual(result.command.qdot, result.candidate.qdot)
        self.assertEqual(result.command.qdot[2], 0.001)
        self.assertEqual(solver.solve_count, 1)
        self.assertEqual(executor.diagnostics.count, 1)

    def test_malformed_observation_fails_closed_with_exact_zero_evidence(self) -> None:
        solver = _FakeStrictSolver()
        executor = RnnOnlyExecutor(
            StrictRnnControlPolicy(solver),
            safety_envelope=SafetyEnvelope(qdot_cap_rad_s=0.5),
            max_slew_rad_s2=0.5,
            capacity=1,
        )
        malformed = replace(_observation(), desired_twist=(1.0,))  # type: ignore[arg-type]
        result = executor.step(malformed)
        self.assertFalse(result.decision.accepted)
        self.assertTrue(result.command.stop_requested)
        self.assertEqual(result.command.qdot, (0.0,) * 6)
        self.assertEqual(executor.diagnostics.count, 1)
        self.assertEqual(executor.diagnostics.numeric[0, -6:].tolist(), [0.0] * 6)

    def test_remote_runtime_contains_no_comparison_solver_or_state_seed_call(self) -> None:
        package = EXPERIMENT_ROOT / "tools/step5d_remote_control"
        runtime_source = "\n".join(
            path.read_text(encoding="utf-8").lower()
            for path in sorted(package.glob("*.py"))
        )
        self.assertNotIn("dls", runtime_source)
        self.assertNotIn("pinv", runtime_source)
        self.assertNotIn("warm_start", runtime_source)

    def test_missing_v3_backend_never_falls_back(self) -> None:
        release = load_remote_release()
        with patch(
            "step5d_remote_control.runtime.StrictTaseRnnSolver",
            side_effect=RuntimeError("backend unavailable"),
        ):
            with self.assertRaisesRegex(RemoteControlError, "fallback is forbidden"):
                build_executor(release, capacity=1)


if __name__ == "__main__":
    unittest.main()
