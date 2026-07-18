from __future__ import annotations

import inspect
import math
import unittest

import numpy as np

from ur10e_vic.tacdiffusion import (
    CONDITION_DIMENSION,
    PERMITTED_PROGRAM_CLAIM,
    CanonicalWrenchSample,
    DynamicForceFilter,
    ExpertTraceManifest,
    ForceDiffusionObservation,
    ForceDiffusionProposal,
    FrameCalibrationLineage,
    ModelTimingEvidence,
    causal_sync_wrench_1khz_to_control_500hz,
    convert_wrench_to_si,
    reconstruct_internal_wrench_from_previous_command,
    select_highest_accepted_model_rate,
    transform_wrench_to_target_frame,
)


def lineage() -> FrameCalibrationLineage:
    return FrameCalibrationLineage(
        external_sensor_frame_id="kunwei_sensor",
        canonical_tcp_frame_id="tool0_tcp",
        twist_frame_id="tool0_tcp",
        sensor_to_tcp_transform_sha256="a" * 64,
        calibration_sha256="b" * 64,
        zero_bias_sha256="c" * 64,
    )


def observation() -> ForceDiffusionObservation:
    return ForceDiffusionObservation(
        sequence=2,
        previous_timestamp_s=1.000,
        current_timestamp_s=1.002,
        previous_external_sample_timestamp_s=0.999,
        current_external_sample_timestamp_s=1.001,
        previous_external_wrench=(1, 2, 3, 4, 5, 6),
        current_external_wrench=(7, 8, 9, 10, 11, 12),
        previous_internal_wrench=(13, 14, 15, 16, 17, 18),
        current_internal_wrench=(19, 20, 21, 22, 23, 24),
        previous_ee_twist=(25, 26, 27, 28, 29, 30),
        current_ee_twist=(31, 32, 33, 34, 35, 36),
        lineage=lineage(),
        previous_external_source_sequence=1000,
        current_external_source_sequence=1002,
    )


def manifest(**overrides: object) -> ExpertTraceManifest:
    values: dict[str, object] = {
        "trace_id": "pilot-001",
        "source_kind": "ur10e_expert_demonstration",
        "dataset_split": "train",
        "controller_profile": "polyscope-5.25.2-direct-torque-v2-500hz",
        "canonical_frame_id": "tool0_tcp",
        "controller_verified": True,
        "controller_readback_sha256": "0" * 64,
        "sample_count": 100,
        "has_expert_ff_labels": True,
        "frame_calibration_sha256": "1" * 64,
        "expert_policy_sha256": "2" * 64,
        "software_sha256": "3" * 64,
        "package_sha256": "4" * 64,
        "trace_sha256": "5" * 64,
        "claim_boundary": "ur10e_expert_force_labels",
    }
    values.update(overrides)
    return ExpertTraceManifest(**values)


class TacDiffusionContractTests(unittest.TestCase):
    def test_observation_is_force_only_current_previous_36d(self) -> None:
        item = observation()
        self.assertEqual(len(item.condition_36d), CONDITION_DIMENSION)
        self.assertEqual(item.condition_36d[:6], item.current_external_wrench)
        self.assertEqual(item.condition_36d[6:12], item.current_internal_wrench)
        self.assertEqual(item.condition_36d[12:18], item.current_ee_twist)
        self.assertEqual(item.condition_36d[18:24], item.previous_external_wrench)
        self.assertFalse(item.vision_included)
        with self.assertRaisesRegex(ValueError, "vision"):
            ForceDiffusionObservation(**{**item.__dict__, "vision_included": True})

    def test_observation_requires_causality_si_lineage_and_fixed_rates(self) -> None:
        item = observation()
        with self.assertRaisesRegex(ValueError, "causally available"):
            ForceDiffusionObservation(
                **{
                    **item.__dict__,
                    "current_external_sample_timestamp_s": item.current_timestamp_s + 0.001,
                }
            )
        with self.assertRaisesRegex(ValueError, "stale"):
            ForceDiffusionObservation(
                **{
                    **item.__dict__,
                    "current_external_sample_timestamp_s": item.current_timestamp_s - 0.003,
                }
            )
        with self.assertRaisesRegex(ValueError, "1000 Hz"):
            ForceDiffusionObservation(**{**item.__dict__, "raw_wrench_rate_hz": 500})
        with self.assertRaisesRegex(ValueError, "same canonical TCP frame"):
            FrameCalibrationLineage(
                external_sensor_frame_id="sensor",
                canonical_tcp_frame_id="tcp",
                twist_frame_id="base",
                sensor_to_tcp_transform_sha256="a" * 64,
                calibration_sha256="b" * 64,
                zero_bias_sha256="c" * 64,
            )
        with self.assertRaisesRegex(ValueError, "SI units"):
            FrameCalibrationLineage(
                external_sensor_frame_id="sensor",
                canonical_tcp_frame_id="tcp",
                twist_frame_id="tcp",
                sensor_to_tcp_transform_sha256="a" * 64,
                calibration_sha256="b" * 64,
                zero_bias_sha256="c" * 64,
                wrench_units="kgf,kgf-mm",
            )

    def test_proposal_validates_model_identity_rate_and_vector(self) -> None:
        proposal = ForceDiffusionProposal(
            generated_at_s=1.0,
            sequence=3,
            raw_f_df=(1, 2, 3, 4, 5, 6),
            model_sha256="d" * 64,
            model_rate_hz=200,
            age_s=0.001,
            confidence=0.8,
        )
        self.assertTrue(proposal.shadow_only)
        with self.assertRaisesRegex(ValueError, "50, 100, 200, or 500"):
            ForceDiffusionProposal(**{**proposal.__dict__, "model_rate_hz": 250})
        with self.assertRaisesRegex(ValueError, "finite"):
            ForceDiffusionProposal(
                **{**proposal.__dict__, "raw_f_df": (math.nan, 0, 0, 0, 0, 0)}
            )


class TacDiffusionSignalTests(unittest.TestCase):
    def test_explicit_si_conversion_and_spatial_wrench_transform(self) -> None:
        converted = convert_wrench_to_si(
            (1, 2, 3, 4, 5, 6),
            force_scale_to_newtons=10.0,
            torque_scale_to_newton_metres=0.001,
        )
        self.assertEqual(converted, (10.0, 20.0, 30.0, 0.004, 0.005, 0.006))
        transformed = transform_wrench_to_target_frame(
            (1, 0, 0, 0, 0, 0),
            rotation_target_from_source=np.eye(3),
            target_origin_to_source_origin_target_m=(0, 1, 0),
        )
        self.assertEqual(transformed, (1.0, 0.0, 0.0, 0.0, 0.0, -1.0))
        with self.assertRaisesRegex(ValueError, "proper rotation"):
            transform_wrench_to_target_frame(
                (1, 0, 0, 0, 0, 0),
                rotation_target_from_source=np.diag((1, 1, -1)),
                target_origin_to_source_origin_target_m=(0, 0, 0),
            )

    def test_causal_sync_never_uses_future_1khz_sample(self) -> None:
        samples = tuple(
            CanonicalWrenchSample(
                sequence=index,
                timestamp_s=index / 1000.0,
                wrench_tcp_si=(index,) * 6,
                frame_id="tcp",
                calibration_sha256="a" * 64,
            )
            for index in range(6)
        )
        aligned = causal_sync_wrench_1khz_to_control_500hz(
            samples,
            (0.0005, 0.0025, 0.0045),
            expected_frame_id="tcp",
            expected_calibration_sha256="a" * 64,
        )
        self.assertEqual([item.source_sequence for item in aligned], [0, 2, 4])
        self.assertTrue(
            all(item.source_timestamp_s <= item.control_timestamp_s for item in aligned)
        )
        self.assertEqual(samples[1].wrench_tcp_si, (1.0,) * 6)
        with self.assertRaisesRegex(ValueError, "frame mismatch"):
            causal_sync_wrench_1khz_to_control_500hz(
                samples,
                (0.0005,),
                expected_frame_id="base",
                expected_calibration_sha256="a" * 64,
            )

    def test_internal_wrench_uses_previous_command_and_dynamics_only(self) -> None:
        target_wrench = np.arange(1.0, 7.0)
        coriolis = np.full(6, 0.2)
        joint_damping = np.full(6, 0.1)
        applied = target_wrench + coriolis - joint_damping
        primary = reconstruct_internal_wrench_from_previous_command(
            previous_applied_no_gravity_joint_torque_nm=applied,
            previous_jacobian_tcp=np.eye(6),
            previous_coriolis_joint_torque_nm=coriolis,
            previous_joint_damping_torque_nm=joint_damping,
            jacobian_frame_id="tcp",
            canonical_tcp_frame_id="tcp",
        )
        with_shadow = reconstruct_internal_wrench_from_previous_command(
            previous_applied_no_gravity_joint_torque_nm=applied,
            previous_jacobian_tcp=np.eye(6),
            previous_coriolis_joint_torque_nm=coriolis,
            previous_joint_damping_torque_nm=joint_damping,
            jacobian_frame_id="tcp",
            canonical_tcp_frame_id="tcp",
            actual_current_as_torque_nm_shadow=applied + 1.0,
        )
        np.testing.assert_allclose(primary.wrench_tcp_si, target_wrench, atol=1e-10)
        self.assertEqual(primary.wrench_tcp_si, with_shadow.wrench_tcp_si)
        self.assertIsNone(primary.actual_current_shadow_wrench_tcp_si)
        self.assertGreater(with_shadow.actual_current_shadow_delta_norm or 0.0, 0.0)
        self.assertEqual(primary.source_tick_offset, -1)
        parameter_names = inspect.signature(
            reconstruct_internal_wrench_from_previous_command
        ).parameters
        self.assertFalse(any("external" in name for name in parameter_names))
        with self.assertRaisesRegex(ValueError, "same canonical TCP frame"):
            reconstruct_internal_wrench_from_previous_command(
                previous_applied_no_gravity_joint_torque_nm=applied,
                previous_jacobian_tcp=np.eye(6),
                previous_coriolis_joint_torque_nm=coriolis,
                previous_joint_damping_torque_nm=joint_damping,
                jacobian_frame_id="base",
                canonical_tcp_frame_id="tcp",
            )


class TacDiffusionFilterAndTimingTests(unittest.TestCase):
    def test_paper_second_order_filter_discretization_is_pinned(self) -> None:
        filter_ = DynamicForceFilter()
        state = filter_.step((1, 0, 0, 0, 0, 0))
        expected_acceleration = 0.9 * 0.3
        expected_velocity = (1.0 / 500.0) * expected_acceleration
        expected_position = (1.0 / 500.0) * expected_velocity
        self.assertAlmostEqual(state.filtered_f_ff_velocity[0], expected_velocity)
        self.assertAlmostEqual(state.filtered_f_ff[0], expected_position)
        self.assertEqual(filter_.reset().filtered_f_ff, (0.0,) * 6)
        with self.assertRaisesRegex(ValueError, "alpha is pinned"):
            DynamicForceFilter(alpha=1.0)
        with self.assertRaisesRegex(ValueError, "six finite"):
            filter_.step((math.inf, 0, 0, 0, 0, 0))

    def test_rate_selection_requires_all_rates_and_chooses_highest_pass(self) -> None:
        evidence = (
            ModelTimingEvidence(500, 60.0, 0.00161, 0),
            ModelTimingEvidence(200, 60.0, 0.003, 0),
            ModelTimingEvidence(100, 60.0, 0.004, 0),
            ModelTimingEvidence(50, 60.0, 0.008, 0),
        )
        selection = select_highest_accepted_model_rate(evidence)
        self.assertTrue(selection.all_rates_tested)
        self.assertEqual(selection.selected_rate_hz, 200)
        self.assertEqual(len(selection.evidence_sha256), 64)
        self.assertTrue(selection.shadow_only)
        incomplete = select_highest_accepted_model_rate(evidence[1:])
        self.assertFalse(incomplete.all_rates_tested)
        self.assertIsNone(incomplete.selected_rate_hz)
        no_finite_output = ModelTimingEvidence(500, 60.0, 0.001, 0, 1)
        self.assertFalse(no_finite_output.accepted)


class TacDiffusionManifestTests(unittest.TestCase):
    def test_expert_manifest_is_hash_bound_and_requires_verified_hardware_for_training(self) -> None:
        verified = manifest()
        unverified = manifest(controller_verified=False)
        self.assertTrue(verified.training_eligible)
        self.assertFalse(unverified.training_eligible)
        with self.assertRaisesRegex(ValueError, "controller profile"):
            manifest(controller_profile="polyscope-5.11.9-velocity")
        self.assertEqual(verified.permitted_program_claim, PERMITTED_PROGRAM_CLAIM)
        self.assertEqual(len(verified.fingerprint_sha256), 64)
        self.assertNotEqual(verified.fingerprint_sha256, unverified.fingerprint_sha256)
        with self.assertRaisesRegex(ValueError, "claim boundary"):
            manifest(permitted_program_claim="Panda 1 kHz exact reproduction")

    def test_v27_v29_replay_is_pipeline_only_and_never_a_force_label(self) -> None:
        for source_kind in ("legacy_v27_replay", "legacy_v29_replay"):
            replay = manifest(
                source_kind=source_kind,
                dataset_split="pipeline_only",
                controller_verified=False,
                has_expert_ff_labels=False,
                claim_boundary="observation_pipeline_only",
            )
            self.assertFalse(replay.training_eligible)
            with self.assertRaisesRegex(ValueError, "cannot contain F_ff labels"):
                manifest(
                    source_kind=source_kind,
                    dataset_split="pipeline_only",
                    has_expert_ff_labels=True,
                    claim_boundary="observation_pipeline_only",
                )


if __name__ == "__main__":
    unittest.main()
