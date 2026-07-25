import math
import json
from pathlib import Path
import unittest

from ur10e_vic.tacdiffusion.action import ActionProfile, TacDiffusionAction, derive_damping, guard_action
from ur10e_vic.tacdiffusion.dynamic_filter import DynamicFilterProfile, RateInvariantForceFilter
from ur10e_vic.tacdiffusion.expert import DeterministicExpert, ExpertFrameSemantics, ExpertInput, ExpertState
from ur10e_vic.tacdiffusion.mailbox import LatestModelMailbox, ModelPacket
from ur10e_vic.tacdiffusion.direct_torque_receiver import ReceiverSemanticState, build_receiver_source, parse_receiver_source
from ur10e_vic.tacdiffusion.observation import (
    OBSERVATION_DIMENSION,
    ObservationLineage,
    ObservationSlice,
    TacDiffusionObservation,
)


LINEAGE = ObservationLineage("kunwei_sensor", "tool0_tcp", "a" * 64, "b" * 64)
CONFIG_ROOT = Path(__file__).parents[1] / "config"


def obs_slice(sequence, timestamp, *, lineage=LINEAGE):
    zeros = (0.0,) * 6
    return ObservationSlice(sequence, timestamp, timestamp, zeros, zeros, zeros, zeros, zeros, zeros, zeros, lineage)


class ObservationContractTests(unittest.TestCase):
    def test_schema_contains_continuous_reference_terms_and_temporal_stack(self):
        current = ObservationSlice(2, 0.004, 0.004, (1,) * 6, (2,) * 6, (3,) * 6, (4,) * 6, (5,) * 6, (6,) * 6, (7,) * 6, LINEAGE)
        stacked = TacDiffusionObservation(obs_slice(1, 0.002), current)
        self.assertEqual(stacked.schema_version, "ur10e_tacdiffusion_observation/v2")
        self.assertEqual(len(stacked.vector), OBSERVATION_DIMENSION)
        self.assertEqual(stacked.temporal_alignment["current_sequence"], 2)
        with self.assertRaisesRegex(ValueError, "lineage"):
            TacDiffusionObservation(obs_slice(1, 0.002), obs_slice(2, 0.004, lineage=ObservationLineage("other", "tool0_tcp", "a" * 64, "b" * 64)))

    def test_future_external_sample_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "causally"):
            ObservationSlice(1, 0.002, 0.003, (0,) * 6, (0,) * 6, (0,) * 6, (0,) * 6, (0,) * 6, (0,) * 6, (0,) * 6, LINEAGE)

    def test_external_sample_freshness_and_source_identity_are_bounded(self):
        with self.assertRaisesRegex(ValueError, "stale"):
            ObservationSlice(1, 0.01, 0.0, (0,) * 6, (0,) * 6, (0,) * 6, (0,) * 6, (0,) * 6, (0,) * 6, (0,) * 6, LINEAGE, 1)
        fresh = obs_slice(1, 0.002)
        self.assertLessEqual(fresh.external_sample_age_s, 0.002)


class ActionAndExpertTests(unittest.TestCase):
    def test_12d_action_k_bounds_slew_and_damping(self):
        profile = ActionProfile()
        action = guard_action(TacDiffusionAction((100, 0, 0, 3, 0, 0), (2000,) * 6), profile=profile)
        self.assertEqual(len(action.vector12), 12)
        self.assertLessEqual(math.sqrt(sum(value * value for value in action.raw_f_df[:3])), 20.0)
        self.assertLessEqual(action.raw_f_df[3], 2.0)
        self.assertEqual(action.stiffness, profile.stiffness_max)
        previous = TacDiffusionAction((0,) * 6, profile.stiffness_baseline)
        stepped = guard_action(TacDiffusionAction((20,) * 6, profile.stiffness_baseline), previous=previous, dt_s=0.002, profile=profile)
        self.assertEqual(stepped.stiffness, profile.stiffness_baseline)
        self.assertEqual(stepped.raw_f_df[0], 0.2)
        damping = derive_damping(profile.stiffness_baseline, profile)
        self.assertAlmostEqual(damping[0], 2.0 * math.sqrt(2.0 * 600.0))
        with self.assertRaisesRegex(ValueError, "finite"):
            TacDiffusionAction((math.nan,) * 6, profile.stiffness_baseline)

    def test_expert_is_stateful_deterministic_and_labels_pre_filter_command(self):
        expert = DeterministicExpert()
        acquire = expert.step(ExpertInput(0.0, 5.0, (0,) * 6, (0,) * 6, 0.0, 0.01))
        self.assertEqual(acquire.state, ExpertState.CONTACT_ACQUIRE)
        self.assertLess(acquire.action.raw_f_df[2], 0.0)  # positive scalar along approach (TCP -z)
        track = expert.step(ExpertInput(4.0, 5.0, (0.001,) * 6, (0,) * 6, 0.2, 0.01))
        self.assertEqual(track.state, ExpertState.TRACK)
        self.assertEqual(track.pre_filter_label, track.action.raw_f_df)
        relief = expert.step(ExpertInput(9.0, 5.0, (0,) * 6, (0,) * 6, 0.3, 0.01))
        self.assertEqual(relief.state, ExpertState.HIGH_FORCE_RELIEF)
        self.assertGreater(relief.action.raw_f_df[2], 0.0)
        failed = expert.step(ExpertInput(5.0, 5.0, (0,) * 6, (0,) * 6, 0.3, 0.01, fault=True))
        self.assertEqual(failed.state, ExpertState.FAILED_RETRACT)
        self.assertEqual(track.frame_semantics.normal_command_definition, "command_along_approach_normal_base")
        self.assertIn("reaction_normal_base", track.frame_semantics.normal_load_definition)

    def test_expert_frame_roles_are_not_implicit_signs(self):
        with self.assertRaisesRegex(ValueError, "negative reaction_normal"):
            ExpertFrameSemantics((0.0, 0.0, 1.0), (0.0, 0.0, 1.0))

    def test_track_label_is_reference_and_load_policy_not_forbidden_pd_formula(self):
        expert = DeterministicExpert()
        decision = expert.step(ExpertInput(5.0, 5.0, (100.0,) * 6, (100.0,) * 6, 0.2, 0.01, desired_twist=(0.2, -0.1, 0.0, 0.0, 0.0, 0.0)))
        self.assertEqual(decision.state, ExpertState.TRACK)
        self.assertEqual(decision.pre_filter_label, decision.action.raw_f_df)
        self.assertLessEqual(abs(decision.action.raw_f_df[0]), 1.5)
        self.assertLessEqual(abs(decision.action.raw_f_df[1]), 1.5)
        self.assertEqual(decision.action.raw_f_df[5], 0.0)
        self.assertEqual(decision.action.raw_f_df[2], 0.0)  # no normal error in this auxiliary TRACK case


class FilterMailboxTests(unittest.TestCase):
    def test_rate_invariant_filter_has_explicit_50ms_settling_and_no_overshoot(self):
        profile = DynamicFilterProfile(settling_time_s=0.05, damping_ratio=1.0, rate_hz=500)
        trajectories = []
        for rate in (50, 100, 500):
            filt = RateInvariantForceFilter(DynamicFilterProfile(0.05, 1.0, rate))
            samples = [filt.step((1, 0, 0, 0, 0, 0)).filtered_f_ff[0] for _ in range(int(0.5 * rate))]
            trajectories.append(samples)
            self.assertLessEqual(max(samples), 1.0 + 1e-9)
            settling_index = next(index for index, value in enumerate(samples) if all(abs(tail - 1.0) <= 0.02 for tail in samples[index:]))
            self.assertLessEqual(settling_index / rate, 0.06)
        self.assertAlmostEqual(trajectories[0][-1], trajectories[-1][-1], places=5)
        self.assertEqual(RateInvariantForceFilter(profile).reset().filtered_f_ff, (0.0,) * 6)

    def test_critical_settling_constant_and_underdamped_velocity_derivative(self):
        critical = RateInvariantForceFilter(DynamicFilterProfile(0.05, 1.0, 500))
        samples = [critical.step((1, 0, 0, 0, 0, 0)).filtered_f_ff[0] for _ in range(100)]
        at_settling = samples[24]
        self.assertGreaterEqual(at_settling, 0.979)
        self.assertLessEqual(at_settling, 1.0)
        under = RateInvariantForceFilter(DynamicFilterProfile(0.05, 0.5, 500))
        first = under.step((1, 0, 0, 0, 0, 0))
        self.assertGreater(first.filter_velocity[0], 0.0)
        self.assertTrue(all(math.isfinite(value) for value in first.filter_velocity))

    def test_mailbox_identity_and_stale_read(self):
        profile = ActionProfile()
        mailbox = LatestModelMailbox()
        packet = ModelPacket(1, 1.0, TacDiffusionAction((1,) * 6, profile.stiffness_baseline), profile, 0.001)
        mailbox.publish(packet)
        self.assertEqual(mailbox.read(now_s=1.001, max_age_s=0.01).reason, "fresh")
        self.assertEqual(mailbox.read(now_s=1.1, max_age_s=0.01).reason, "stale")
        with self.assertRaisesRegex(ValueError, "increase"):
            mailbox.publish(packet)

    def test_stale_model_smooths_to_zero_and_fails_only_current_episode(self):
        profile = ActionProfile()
        mailbox = LatestModelMailbox()
        mailbox.publish(ModelPacket(1, 1.0, TacDiffusionAction((10,) * 6, profile.stiffness_baseline), profile, 0.001))
        filter_ = RateInvariantForceFilter()
        fresh = mailbox.episode_step(filter_, now_s=1.001, dt_s=0.002, max_age_s=0.01)
        self.assertFalse(fresh.episode_failed)
        stale = mailbox.episode_step(filter_, now_s=1.1, dt_s=0.002, max_age_s=0.01)
        for index in range(50):
            stale = mailbox.episode_step(filter_, now_s=1.1 + (index + 1) * 0.002, dt_s=0.002, max_age_s=0.01)
        self.assertTrue(stale.episode_failed)
        self.assertEqual(stale.reason, "stale")
        self.assertLess(abs(stale.filtered_state.filtered_f_ff[0]), abs(fresh.filtered_state.filtered_f_ff[0]))


class ReceiverSemanticTests(unittest.TestCase):
    @staticmethod
    def packet(sequence=1, *, episode=101, raw=(1.0, 0.0, 0.0, 0.0, 0.0, 0.0), model_sequence=1):
        return {
            "sequence": sequence,
            "heartbeat": sequence,
            "lease_id": 77,
            "episode_identity": episode,
            "model_sequence": model_sequence,
            "model_period_us": 10000,
            "model_mode": 1,
            "model_timestamp_us": sequence * 2000,
            "raw_f_df": raw,
            "stiffness": (600.0, 600.0, 600.0, 30.0, 30.0, 30.0),
        }

    def test_receiver_fault_safe_exit_then_new_episode_stays_alive(self):
        receiver = ReceiverSemanticState()
        self.assertEqual(receiver.append(self.packet()), "RUNNING")
        bad = self.packet(sequence=2, raw=(2.0, 0.0, 0.0, 0.0, 0.0, 0.0), model_sequence=1)
        self.assertEqual(receiver.append(bad), "EPISODE_FAULT")
        self.assertEqual(receiver.phase, "WAITING_HOME_ACK")
        self.assertEqual(len(receiver.safe_exit_trace), 10)
        self.assertTrue(all(abs(row[0]) < 1.0 for row in receiver.safe_exit_trace))
        with self.assertRaisesRegex(ValueError, "stale"):
            receiver.acknowledge_home(100, 100)
        self.assertEqual(receiver.acknowledge_home(101, 101), "WAIT_FOREVER")
        next_packet = self.packet(episode=202)
        self.assertEqual(receiver.append(next_packet), "RUNNING")
        self.assertEqual(receiver.command("DRAIN"), "RETURN_CAMPAIGN_HOME")

    def test_receiver_parser_and_disabled_model_contract(self):
        contract = parse_receiver_source(build_receiver_source())
        self.assertTrue(contract.direct_torque_required)
        self.assertIn("model_active_allowed = False", build_receiver_source())
        self.assertIn("model_active_allowed = True", build_receiver_source(active_authorized=True))
        receiver = ReceiverSemanticState()
        disabled = self.packet(raw=(0.0,) * 6, model_sequence=0)
        disabled.update(model_mode=0, model_period_us=0, model_timestamp_us=0)
        self.assertEqual(receiver.append(disabled), "RUNNING")
        invalid = self.packet(sequence=2, episode=0)
        self.assertEqual(receiver.append(invalid), "EPISODE_FAULT")
        self.assertGreater(receiver.home_identity, 0)
        first_fault_identity = receiver.home_identity
        receiver.acknowledge_home(first_fault_identity, first_fault_identity)
        invalid_again = self.packet(sequence=1, episode=0)
        self.assertEqual(receiver.append(invalid_again), "EPISODE_FAULT")
        self.assertEqual(receiver.home_identity, first_fault_identity + 1)


class RemoteHeadlessConfigTests(unittest.TestCase):
    def _load(self, name):
        with (CONFIG_ROOT / name).open(encoding="utf-8") as handle:
            return json.load(handle)

    def test_v4_contract_binds_current_builder_and_packet_layout_without_mutating_v3(self):
        contract = self._load("direct_torque_remote_headless_v4.json")
        receiver = contract["mainline_receiver"]
        self.assertEqual(receiver["python_module"], "ur10e_vic.tacdiffusion.direct_torque_receiver")
        self.assertEqual(receiver["builder_symbol"], "build_receiver_source")
        self.assertEqual(receiver["emitted_receiver_schema"], "ur10e_direct_torque_receiver/v3")
        self.assertTrue(receiver["v3_is_immutable"])
        packet = contract["packet"]
        self.assertEqual(packet["input_integer_count"], len(packet["input_integer_registers"]))
        self.assertEqual(packet["input_double_count"], len(packet["input_double_registers"]))
        self.assertEqual(packet["input_integer_count"], 12)
        self.assertEqual(packet["input_double_count"], 24)
        self.assertEqual(contract["controller"]["model_update_rate_hz_candidates"], [50, 100])
        self.assertFalse(contract["authorization"]["active_allowed_default"])
        self.assertTrue(contract["no_motion"])
        self.assertEqual(parse_receiver_source(build_receiver_source()).control_rate_hz, 500)

    def test_no_motion_protocol_records_runtime_boundary_and_forbidden_operations(self):
        protocol = self._load("ursim_remote_headless_no_motion_protocol.json")
        self.assertTrue(protocol["validation"]["parser_validation"])
        self.assertTrue(protocol["validation"]["semantic_validation"])
        self.assertFalse(protocol["validation"]["network_executed"])
        self.assertFalse(protocol["validation"]["robot_io_executed"])
        boundary = protocol["runtime_boundary"]
        self.assertEqual(boundary["locally_available_ursim_image"], "5.25.2")
        self.assertFalse(boundary["polyscope_5_26_ursim_runtime_installed"])
        self.assertFalse(boundary["polyscope_5_26_ursim_runtime_claimed"])
        self.assertEqual(set(protocol["forbidden_operations"]), {"Dashboard", "Load", "Play", "bridge", "ARM", "RTDE_writer", "motion"})

    def test_experiment_plan_is_current_84d_12d_rate_bound_and_inactive(self):
        plan = self._load("tacdiffusion_experiment_plan.json")
        observation = plan["observation_contract"]
        self.assertEqual(observation["dimension"], 84)
        self.assertEqual(observation["slice_dimension"], 42)
        self.assertEqual(observation["slice_order"], ["current", "previous"])
        self.assertEqual({field["name"] for field in observation["slice_fields"]}, {"external_wrench", "internal_wrench", "actual_ee_twist", "desired_pose", "desired_twist", "desired_acceleration", "tracking_error"})
        self.assertEqual(plan["proposal_contract"]["action_dimension"], 12)
        self.assertIn("derived", plan["proposal_contract"]["damping_semantics"])
        model = plan["model_configuration"]
        self.assertEqual(model["candidate_model_rates_hz"], [50, 100])
        self.assertFalse(model["benchmark_selection"]["fixed_wall_clock_sleep"])
        self.assertFalse(model["benchmark_selection"]["fixed_60_second_qualification"])
        self.assertFalse(plan["active_model_enabled"])
        self.assertFalse(plan["live_motion_authorized"])


if __name__ == "__main__":
    unittest.main()
