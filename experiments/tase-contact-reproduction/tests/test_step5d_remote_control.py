#!/usr/bin/env python3
"""Offline tests for the lightweight strict-RNN Remote preparation release."""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
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
from step5d_autotune_v3.runtime_profile import DEFAULT_OVERLAY  # noqa: E402
from step5d_control_contract import (  # noqa: E402
    SafetyEnvelope,
    Step5dObservation,
    StrictRnnControlPolicy,
)
from step5d_remote_control.contracts import (  # noqa: E402
    RECEIPT_SCHEMA,
    TRANSPORT_ID,
    TRIAL_SOURCE_SCHEMA,
    RemoteControlError,
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
    reject_live_run,
)


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _trial_source() -> dict[str, object]:
    return {
        "schema": TRIAL_SOURCE_SCHEMA,
        "campaign_id": "campaign-remote-test",
        "campaign_fingerprint": "1" * 64,
        "trial_uid": "2" * 64,
        "occurrence_uid": "3" * 64,
        "batch_row_index": 7,
        "profile_id": "nf100-slew050-a050",
        "plant_epoch": 11,
        "trial_overlay": dict(DEFAULT_OVERLAY),
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

    def test_controller_config_is_materialized_from_bound_values(self) -> None:
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
                load_remote_release(
                    release_path, experiment_root=EXPERIMENT_ROOT
                )

    def test_frozen_v3_core_bytes_are_unchanged(self) -> None:
        relative = "tools/step5d_control_contract.py"
        self.assertEqual(
            _sha(EXPERIMENT_ROOT / relative),
            self.release.contract["source_sha256"][relative],
        )


class RemoteTrialContractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.release = load_remote_release()

    def test_trial_preserves_explicit_occurrence_epoch_and_transport(self) -> None:
        prepared = prepare_control_trial(_trial_source(), release=self.release)
        self.assertEqual(prepared.occurrence_uid, "3" * 64)
        self.assertEqual(prepared.plant_epoch, 11)
        self.assertEqual(prepared.transport_id, TRANSPORT_ID)
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

    def test_offline_receipt_validates_but_cannot_mutate_campaign(self) -> None:
        prepared = prepare_control_trial(_trial_source(), release=self.release)
        receipt = {
            "schema": RECEIPT_SCHEMA,
            "trial_uid": prepared.trial_uid,
            "occurrence_uid": prepared.occurrence_uid,
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
        validated = validate_receipt(
            receipt, prepared=prepared, release=self.release
        )
        self.assertFalse(validated.optimizer_eligible)
        result = import_result(receipt, prepared=prepared, release=self.release)
        self.assertEqual(result["status"], "validated_not_committed")
        self.assertEqual(
            result["blocker"], "campaign_transport_adapter_not_synced"
        )
        self.assertFalse(result["optimizer_eligible"])
        self.assertNotIn("tp", json.dumps(receipt).lower())

    def test_live_receipt_is_rejected_by_prep_release(self) -> None:
        prepared = prepare_control_trial(_trial_source(), release=self.release)
        receipt = {
            "schema": RECEIPT_SCHEMA,
            "trial_uid": prepared.trial_uid,
            "occurrence_uid": prepared.occurrence_uid,
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
            "optimizer_eligible": True,
        }
        with self.assertRaisesRegex(RemoteControlError, "does not authorize live"):
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

    def test_live_entry_fails_before_driver_surface(self) -> None:
        release = load_remote_release()
        output = io.StringIO()
        with contextlib.redirect_stderr(io.StringIO()):
            code = reject_live_run(release, output=output)
        payload = json.loads(output.getvalue())
        self.assertEqual(code, 3)
        self.assertFalse(payload["live_certified"])
        self.assertEqual(payload["blocker"], "remote_release_not_live_authorized")
        self.assertNotIn("rclpy", sys.modules)

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
