#!/usr/bin/env python3
"""Offline regression tests for Step5d-native runtime compatibility surfaces."""

from __future__ import annotations

import hashlib
import json
import math
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import build_step5d_autotune_tp_v3 as tp_builder_v3  # noqa: E402
import kunwei_rtde_bridge as bridge  # noqa: E402
from step5d_autotune_backend import BACKEND_ID, FrozenFingerprint, Step5dV35Backend  # noqa: E402
from step5d_autotune_contract import (  # noqa: E402
    CampaignSpec,
    ExecutionProfile,
    ForceCandidate,
    TrialSource,
    TrialSpec,
    TrialTransition,
    TrialTransitionKind,
)
import step5d_autotune_state_machine as state_machine  # noqa: E402
import step5d_runtime_interface as runtime  # noqa: E402
from ur10e_experiment_runtime.physical_prior import (  # noqa: E402
    STEP5D_V3_PHYSICAL_PRIOR,
)

V3_TEST_PROGRAM = "step5d_strict_rnn_autotune_v3_r999"


AUTOTUNE = runtime.STEP5D_AUTOTUNE_STAGE_ID
V35 = runtime.STEP5D_ABLATION_V35_STAGE_ID
V3 = "step5d_strict_rnn_autotune_v3"


def parse_autotune(*extra: str):
    return bridge.parse_args(["--bridge-profile", AUTOTUNE, *extra])


class Step5dAutotuneRuntimeTest(unittest.TestCase):
    def test_v1_stage_is_historical_while_v3_is_current(self) -> None:
        table = json.loads((ROOT / "config" / "step5_stage_table.json").read_text(encoding="utf-8"))
        row = next(item for item in table["stages"] if item["id"] == AUTOTUNE)
        current = json.loads((ROOT / "config" / "current_stage.json").read_text(encoding="utf-8"))

        self.assertFalse(row["active"])
        self.assertFalse(row["bridge"])
        self.assertFalse(row["current_binding"]["is_current"])
        self.assertTrue(row["package_delivery"]["controller_uploaded"])
        self.assertTrue(row["package_delivery"]["controller_readback_verified"])
        self.assertEqual(
            row["package_delivery"]["status"], "controller_readback_verified_current"
        )
        self.assertEqual(current["program"], V3)
        self.assertEqual(current["current_stage_id"], V3)

    def test_stage_binds_exact_v35_script_bytes(self) -> None:
        table = json.loads((ROOT / "config" / "step5_stage_table.json").read_text(encoding="utf-8"))
        row = next(item for item in table["stages"] if item["id"] == AUTOTUNE)
        source = ROOT / row["source_binding"]["program_source"]
        digest = hashlib.sha256(source.read_bytes()).hexdigest()

        self.assertEqual(row["source_binding"]["stage_id"], V35)
        self.assertEqual(
            row["source_binding"]["control_and_guard_semantics"],
            "exact_v35_behavioral_port",
        )
        self.assertEqual(digest, row["source_binding"]["program_source_sha256"])

    def test_runtime_membership_and_stage_env_exclude_alpha(self) -> None:
        self.assertIn(AUTOTUNE, runtime.STEP5D_V30_CONTROL_CONTRACT_STAGE_IDS)
        self.assertIn(AUTOTUNE, runtime.STEP5D_ABLATION_STAGE_IDS)
        self.assertIn(AUTOTUNE, bridge.STEP5D_LIVEPREP_STAGE_IDS)
        env = runtime.build_stage_env(AUTOTUNE, ROOT)

        self.assertNotIn("BRIDGE_NORMAL_FILTER_ALPHA", env)
        self.assertEqual(env["BRIDGE_NORMAL_FILTER_TAU_S"], "0.35")
        self.assertEqual(env["STEP5D_AUTOTUNE_NORMAL_RATE_RAD_S"], "0.050")
        self.assertEqual(env["STEP5D_AUTOTUNE_FORCE_P"], "0.001000")
        self.assertEqual(env["STEP5D_AUTOTUNE_FORCE_I"], "0.00001000")
        self.assertEqual(env["STEP5D_AUTOTUNE_FORCE_DAMPING"], "7.000")

    def test_runtime_interface_is_v35_control_with_fixed_autotune_contract(self) -> None:
        interface = runtime.resolve_runtime_interface(program=AUTOTUNE, root=ROOT, env={})
        profile = interface.hard_contract["runtime_profile"]
        autotune = interface.hard_contract["autotune_profile"]

        self.assertEqual(interface.stage25_control_mode, "speedj_rnn_live")
        self.assertTrue(interface.hard_contract["v30_control_contract"])
        self.assertFalse(interface.hard_contract["offline_candidate"])
        self.assertEqual(profile["qdot_cap_rad_s"], 0.5)
        self.assertEqual(profile["guard_schema"], "step5d_v35_permissive_contact_quota_safe_other")
        self.assertEqual(interface.hard_contract["runtime_scheduler"]["policy"], "SCHED_OTHER")
        self.assertEqual(interface.bridge_defaults.normal_filter_alpha, 0.0)
        self.assertEqual(autotune["normal_filter_alpha"], "rejected")
        self.assertEqual(autotune["normal_filter_tau_s"], 0.35)
        self.assertEqual(autotune["normal_filter_dt_s"], 0.002)
        self.assertEqual(
            tuple(autotune["live_normal_rate_rad_s"]),
            (0.01, 0.015, 0.02, 0.05, 0.1),
        )
        self.assertEqual(tuple(autotune["offline_only_normal_rate_rad_s"]), (0.03,))
        self.assertEqual(
            autotune["handshake"]["host_to_tp"],
            runtime.STEP5D_AUTOTUNE_HANDSHAKE_HOST_TO_TP,
        )
        self.assertEqual(
            autotune["handshake"]["tp_to_host"],
            runtime.STEP5D_AUTOTUNE_HANDSHAKE_TP_TO_HOST,
        )

    def test_bridge_defaults_map_native_force_coordinates(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            args = parse_autotune()

        self.assertEqual(args.step5d_stage25_control_mode, "speedj_rnn_live")
        self.assertEqual(args.step5d_qdot_limit_rad_s, 0.5)
        self.assertEqual(args.bridge_normal_filter_tau_s, 0.35)
        self.assertEqual(args.bridge_normal_filter_alpha, 0.0)
        self.assertEqual(args.bridge_normal_max_rate_rad_s, 0.1)
        self.assertEqual(args.step5d_autotune_force_terms["Md"], 1000.0)
        self.assertEqual(args.step5d_autotune_force_terms["kf"], 0.01)
        self.assertEqual(args.step5d_autotune_force_terms["Bd"], 7000.0)
        self.assertEqual(args.step5d_autotune_profile_eligibility, "live_eligible")

    def test_explicit_autotune_seams_override_force_rate_slew_and_speedj(self) -> None:
        with patch.dict(
            os.environ,
            {
                "STEP5D_AUTOTUNE_FORCE_P": "0.002",
                "STEP5D_AUTOTUNE_FORCE_I": "0.00002",
                "STEP5D_AUTOTUNE_FORCE_DAMPING": "10",
                "STEP5D_AUTOTUNE_NORMAL_RATE_RAD_S": "0.020",
                "STEP5D_AUTOTUNE_HOST_SLEW_RAD_S2": "0.2",
                "STEP5D_AUTOTUNE_SPEEDJ_ACCELERATION_RAD_S2": "0.5",
            },
            clear=True,
        ):
            args = parse_autotune()

        self.assertEqual(args.step5d_autotune_force_terms["Md"], 500.0)
        self.assertEqual(args.step5d_autotune_force_terms["kf"], 0.01)
        self.assertEqual(args.step5d_autotune_force_terms["Bd"], 5000.0)
        self.assertEqual(args.bridge_normal_max_rate_rad_s, 0.02)
        self.assertEqual(args.step5d_autotune_host_slew_rad_s2, 0.2)
        self.assertEqual(args.step5d_autotune_speedj_acceleration_rad_s2, 0.5)

    def test_backend_candidate_arguments_reach_the_active_autotune_force_path(self) -> None:
        candidate = ForceCandidate.from_log2(p=1.0, damping=-0.5, i=0.75)
        profile = ExecutionProfile(
            "nf020-slew020-a050",
            0.020,
            host_qdot_slew_rad_s2=0.2,
            tp_speedj_accel_rad_s2=0.5,
        )
        trial_campaign = CampaignSpec(
            campaign_id="runtime-binding",
            campaign_epoch=7,
            campaign_fingerprint="a" * 64,
            f0_shadow_reaction_normal_base=(0.0, 0.0, 1.0),
        )
        trial = TrialSpec(
            campaign=trial_campaign,
            trial_id=9,
            candidate_token=11,
            command_seq=13,
            plant_epoch=1,
            candidate=candidate,
            execution_profile=profile,
            backend_id=BACKEND_ID,
            source_fingerprint="b" * 64,
            config_fingerprint="c" * 64,
            transition=TrialTransition(
                TrialTransitionKind.FORCE_SEARCH,
                source=TrialSource(
                    trial_uid=hashlib.sha256(b"runtime-binding-source").hexdigest(),
                    candidate=ForceCandidate.from_log2(
                        p=0.75,
                        damping=-0.5,
                        i=0.75,
                    ),
                    profile_id=profile.profile_id,
                    plant_epoch=1,
                    campaign_id=trial_campaign.campaign_id,
                    campaign_epoch=trial_campaign.campaign_epoch,
                    campaign_fingerprint=trial_campaign.campaign_fingerprint,
                    backend_id=BACKEND_ID,
                    source_fingerprint="b" * 64,
                    config_fingerprint="c" * 64,
                ),
            ),
        )
        frozen = FrozenFingerprint(
            backend_id=BACKEND_ID,
            git_commit="0" * 40,
            source_fingerprint=trial.source_fingerprint,
            config_fingerprint=trial.config_fingerprint,
            composite_fingerprint=trial.campaign.campaign_fingerprint,
            source_files={},
            config_files={},
            v35_package_sha256={},
            controller_readback_manifest="manifest.json",
            controller_readback_manifest_sha256="d" * 64,
            rnn_contract={},
            force_frame_contract_sha256="e" * 64,
        )
        prepared = Step5dV35Backend(ROOT).prepare_trial(trial, frozen)

        with patch.dict(os.environ, {}, clear=True):
            args = bridge.parse_args(list(prepared.runner_arguments))

        for name, expected in (
            ("P", candidate.force_p_gain),
            ("I", candidate.force_i_gain),
            ("damping", candidate.force_damping),
            ("Md", candidate.native_mapping["Md"]),
            ("kf", candidate.native_mapping["kf"]),
            ("Bd", candidate.native_mapping["Bd"]),
        ):
            self.assertTrue(
                math.isclose(
                    args.step5d_autotune_force_terms[name],
                    expected,
                    rel_tol=1e-11,
                    abs_tol=1e-15,
                ),
                name,
            )
        self.assertEqual(args.bridge_normal_max_rate_rad_s, 0.020)
        self.assertEqual(args.step5d_autotune_host_slew_rad_s2, 0.2)
        self.assertEqual(args.step5d_autotune_speedj_acceleration_rad_s2, 0.5)
        self.assertEqual(args.step5d_autotune_handshake["campaign_epoch"], 7)
        self.assertEqual(args.step5d_autotune_handshake["trial_id"], 9)
        self.assertEqual(args.step5d_autotune_handshake["command_seq"], 13)

    def test_alpha_is_rejected_from_cli_and_environment(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(SystemExit, "rejects normal_filter_alpha"):
                parse_autotune("--bridge-normal-filter-alpha", "0.2")
        with patch.dict(os.environ, {"STEP5D_NORMAL_FILTER_ALPHA": "0.2"}, clear=True):
            with self.assertRaisesRegex(SystemExit, "rejects normal_filter_alpha"):
                parse_autotune()

    def test_normal_rate_profiles_and_fixed_filter_dt(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            for value in ("0.010", "0.015", "0.020"):
                args = parse_autotune("--step5d-autotune-normal-rate-rad-s", value)
                self.assertEqual(args.step5d_autotune_profile_eligibility, "live_eligible")
            with self.assertRaisesRegex(SystemExit, "offline-only"):
                parse_autotune("--step5d-autotune-normal-rate-rad-s", "0.030")
            offline = parse_autotune(
                "--step5d-autotune-normal-rate-rad-s",
                "0.030",
                "--step5d-autotune-offline-only-profile",
            )
            with self.assertRaisesRegex(SystemExit, "must be one of"):
                parse_autotune("--step5d-autotune-normal-rate-rad-s", "0.012")

        self.assertEqual(offline.step5d_autotune_profile_eligibility, "offline_only")
        self.assertEqual(bridge.step5d_normal_filter_dt_s(AUTOTUNE, 0.019), 0.002)
        self.assertEqual(bridge.step5d_normal_filter_dt_s(V35, 0.019), 0.019)

    def test_continuous_trials_reset_diagnostics_once_per_trial_id(self) -> None:
        args = parse_autotune()
        prior = STEP5D_V3_PHYSICAL_PRIOR
        args.step5d_physical_prior_reaction_normal_b = prior.reaction_normal_b
        args.step5d_physical_prior_approach_axis_b = prior.approach_axis_b
        args.step5d_physical_prior_precontact_rotvec_rad = prior.precontact_rotvec_rad
        args.step5d_physical_prior_identity_payload = prior.identity_payload()
        args.step5d_physical_prior_sha256 = prior.fingerprint
        args.step5d_physical_prior_binding_valid = True
        args.step5d_controller_progress_adapter = Mock()
        args.step5d_moving_sphere_kernel = Mock()
        state = bridge.BridgeState()
        diagnostics = bridge.DeferredV30Diagnostics(capacity=2)
        state.step5d_v30_deferred_diagnostics = diagnostics

        diagnostics.count = 2
        diagnostics.overflowed = True
        args.step5d_autotune_handshake["trial_id"] = 1
        self.assertTrue(
            bridge.reset_step5d_autotune_diagnostics_for_trial(state, args)
        )
        self.assertEqual(diagnostics.count, 0)
        self.assertFalse(diagnostics.overflowed)
        args.step5d_controller_progress_adapter.reset.assert_called_once_with()
        args.step5d_moving_sphere_kernel.reset.assert_called_once_with()

        diagnostics.count = 1
        self.assertFalse(
            bridge.reset_step5d_autotune_diagnostics_for_trial(state, args)
        )
        self.assertEqual(diagnostics.count, 1)
        args.step5d_controller_progress_adapter.reset.assert_called_once_with()
        args.step5d_moving_sphere_kernel.reset.assert_called_once_with()

        args.step5d_autotune_handshake["trial_id"] = 2
        self.assertTrue(
            bridge.reset_step5d_autotune_diagnostics_for_trial(state, args)
        )
        self.assertEqual(diagnostics.count, 0)
        self.assertEqual(args.step5d_controller_progress_adapter.reset.call_count, 2)
        self.assertEqual(args.step5d_moving_sphere_kernel.reset.call_count, 2)

    def test_qdot_tau_and_low_frequency_levels_are_fail_closed(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(SystemExit, "qdot cap is fixed"):
                parse_autotune("--step5d-qdot-limit-rad-s", "0.4")
            with self.assertRaisesRegex(SystemExit, "tau is fixed"):
                parse_autotune("--bridge-normal-filter-tau-s", "0.2")
            with self.assertRaisesRegex(SystemExit, "must be one of"):
                parse_autotune("--step5d-autotune-host-slew-rad-s2", "0.3")
            with self.assertRaisesRegex(SystemExit, "must be one of"):
                parse_autotune("--step5d-autotune-speedj-acceleration-rad-s2", "0.3")

    def test_raw_v1_bridge_is_rejected_before_mailbox_or_hardware(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            args = parse_autotune()
        with self.assertRaisesRegex(SystemExit, "binding verification failed"):
            bridge.require_v29_live_bridge_authorization(args, root=ROOT)

    def test_saturation_telemetry_fields_are_part_of_csv_contract(self) -> None:
        expected = {
            "_step5d_normal_rate_limiter_active",
            "_step5d_normal_rate_limiter_saturated_s",
            "_step5d_normal_rate_limiter_active_s",
            "_step5d_normal_rate_limiter_duty",
            "_step5d_normal_filter_dt_s",
            "_step5d_normal_filter_tau_s",
            "_step5d_normal_rate_limit_rad_s",
        }
        self.assertTrue(expected.issubset(set(bridge.STEP5D_DIAG_FIELDS)))

    def test_autotune_handshake_extends_only_the_autotune_rtde_recipes(self) -> None:
        expected_inputs = [f"input_int_register_{index}" for index in range(24, 32)]
        expected_outputs = [f"output_int_register_{index}" for index in range(24, 38)]

        self.assertEqual(
            bridge.rtde_input_fields_for(AUTOTUNE),
            [*bridge.INPUT_FIELDS, *expected_inputs],
        )
        self.assertEqual(
            bridge.rtde_output_fields_for(AUTOTUNE),
            [*bridge.OUTPUT_FIELDS, *expected_outputs],
        )
        self.assertEqual(bridge.rtde_input_fields_for(V35), bridge.INPUT_FIELDS)
        self.assertEqual(bridge.rtde_input_names_for(V35), bridge.INPUT_NAMES)
        self.assertEqual(bridge.rtde_output_fields_for(V35), bridge.OUTPUT_FIELDS)
        self.assertEqual(
            list(runtime.STEP5D_AUTOTUNE_HANDSHAKE_HOST_TO_TP),
            expected_inputs,
        )
        self.assertEqual(
            bridge.STEP5D_AUTOTUNE_HANDSHAKE_INPUT_NAMES,
            list(runtime.STEP5D_AUTOTUNE_HANDSHAKE_HOST_TO_TP.values()),
        )
        self.assertEqual(
            list(runtime.STEP5D_AUTOTUNE_HANDSHAKE_TP_TO_HOST),
            expected_outputs,
        )
        self.assertEqual(
            bridge.STEP5D_AUTOTUNE_HANDSHAKE_OUTPUT_NAMES,
            list(runtime.STEP5D_AUTOTUNE_HANDSHAKE_TP_TO_HOST.values()),
        )
        authoritative_host_to_tp = {
            f"input_int_register_{register}": name
            for name, register in state_machine.HOST_TO_TP_INTEGER_REGISTERS.items()
        }
        authoritative_tp_to_host = {
            f"output_int_register_{register}": name
            for name, register in state_machine.TP_TO_HOST_INTEGER_REGISTERS.items()
        }
        self.assertEqual(
            {
                field: runtime.STEP5D_AUTOTUNE_HANDSHAKE_HOST_TO_TP[field]
                for field in authoritative_host_to_tp
            },
            authoritative_host_to_tp,
        )
        self.assertEqual(
            {
                field: runtime.STEP5D_AUTOTUNE_HANDSHAKE_TP_TO_HOST[field]
                for field in authoritative_tp_to_host
            },
            authoritative_tp_to_host,
        )
        table = json.loads((ROOT / "config" / "step5_stage_table.json").read_text(encoding="utf-8"))
        row = next(item for item in table["stages"] if item["id"] == AUTOTUNE)
        handshake = row["wire_protocol"]["autotune_handshake"]
        self.assertEqual(
            handshake["host_to_tp"],
            runtime.STEP5D_AUTOTUNE_HANDSHAKE_HOST_TO_TP,
        )
        self.assertEqual(
            handshake["tp_to_host"],
            runtime.STEP5D_AUTOTUNE_HANDSHAKE_TP_TO_HOST,
        )

    def test_autotune_handshake_args_are_exact_nonnegative_int32(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            args = parse_autotune(
                "--step5d-autotune-campaign-epoch",
                "101",
                "--step5d-autotune-trial-id",
                "202",
                "--step5d-autotune-command",
                "3",
                "--step5d-autotune-candidate-token",
                "404",
                "--step5d-autotune-execution-profile-id",
                "633",
                "--step5d-autotune-command-sequence",
                "606",
                "--step5d-autotune-normal-rate-rad-s",
                "0.1",
                "--step5d-autotune-host-slew-rad-s2",
                "0.5",
                "--step5d-autotune-speedj-acceleration-rad-s2",
                "0.5",
            )
            with self.assertRaisesRegex(SystemExit, "nonnegative INT32"):
                parse_autotune("--step5d-autotune-trial-id", "-1")
            with self.assertRaisesRegex(SystemExit, "nonnegative INT32"):
                parse_autotune(
                    "--step5d-autotune-command-sequence",
                    str(2_147_483_648),
                )
            with self.assertRaisesRegex(SystemExit, "exactly three level digits"):
                parse_autotune(
                    "--step5d-autotune-campaign-epoch",
                    "1",
                    "--step5d-autotune-trial-id",
                    "1",
                    "--step5d-autotune-command",
                    "3",
                    "--step5d-autotune-candidate-token",
                    "1",
                    "--step5d-autotune-execution-profile-id",
                    "5",
                    "--step5d-autotune-command-sequence",
                    "1",
                )

        self.assertEqual(
            args.step5d_autotune_handshake,
            {
                "campaign_epoch": 101,
                "trial_id": 202,
                "command": 3,
                "candidate_token": 404,
                "execution_profile_id": 633,
                "command_seq": 606,
                "batch_row_index": 0,
                "logical_batch_sequence": 0,
            },
        )

    def test_runtime_handshake_matches_rendered_tp_wrapper(self) -> None:
        wrapper = tp_builder_v3.render_script(V3_TEST_PROGRAM)
        for name, register in state_machine.HOST_TO_TP_INTEGER_REGISTERS.items():
            self.assertIn(
                f"{name} = read_input_integer_register({register})",
                wrapper,
            )
        output_expressions = {
            "campaign_epoch_echo": "campaign_epoch",
            "trial_id_echo": "trial_id",
            "state": "state",
            "candidate_token_echo": "candidate_token",
            "terminal_reason": "terminal_reason",
            "execution_profile_id_echo": "execution_profile_id",
            "consumed_command_seq": "consumed_command_seq",
            "batch_row_index_echo": "codex_autotune_batch_row_echo",
            "return_kind_echo": "codex_autotune_return_kind_echo",
            "return_guard_mask": "codex_autotune_return_guard_mask",
            "logical_batch_sequence_echo": (
                "codex_autotune_logical_batch_sequence_echo"
            ),
        }
        for name, register in state_machine.TP_TO_HOST_INTEGER_REGISTERS.items():
            self.assertIn(
                f"write_output_integer_register({register}, {output_expressions[name]})",
                wrapper,
            )

    def test_open_rtde_bridge_negotiates_profile_specific_recipes_offline(self) -> None:
        instances = []

        class FakeRTDE:
            def __init__(self, host: str, timeout: float) -> None:
                self.host = host
                self.timeout = timeout
                self.output_fields = []
                self.input_fields = []
                self.started = False
                instances.append(self)

            def __enter__(self):
                return self

            def __exit__(self, *_args) -> None:
                return None

            def negotiate(self) -> None:
                return None

            def setup_outputs(self, _hz: float, fields: list[str]):
                self.output_fields = list(fields)
                return 7, ["DOUBLE"] * len(fields)

            def setup_inputs(self, fields: list[str]):
                self.input_fields = list(fields)
                return 8, ["DOUBLE"] * len(fields)

            def start(self) -> None:
                self.started = True

        with patch.dict(os.environ, {}, clear=True):
            args = parse_autotune()
        with patch.object(bridge, "RTDEBridgeClient", FakeRTDE):
            client, input_recipe, _input_types, output_recipe, _output_types = (
                bridge.open_rtde_bridge(args)
            )

        self.assertIs(client, instances[0])
        self.assertTrue(client.started)
        self.assertEqual(input_recipe, 8)
        self.assertEqual(output_recipe, 7)
        self.assertEqual(client.input_fields, bridge.rtde_input_fields_for(AUTOTUNE))
        self.assertEqual(client.output_fields, bridge.rtde_output_fields_for(AUTOTUNE))
        self.assertEqual(client._output_fields, bridge.rtde_output_fields_for(AUTOTUNE))

    def test_exception_stop_zero_fills_extended_handshake_recipe(self) -> None:
        class RecordingRTDE:
            def send_input_sample(self, _recipe, _types, values) -> None:
                self.values = values

        rtde = RecordingRTDE()
        extra = len(bridge.STEP5D_AUTOTUNE_HANDSHAKE_INPUT_FIELDS)
        _command, _packet, event = bridge.publish_step5d_v30_exception_stop(
            rtde,
            recipe_id=1,
            type_names=["DOUBLE"] * len(bridge.INPUT_NAMES) + ["INT32"] * extra,
            heartbeat=1.0,
            original_error=RuntimeError("synthetic"),
        )

        self.assertTrue(event["stop_publish_succeeded"])
        self.assertEqual(len(rtde.values), len(bridge.INPUT_NAMES) + extra)
        self.assertEqual(rtde.values[-extra:], [0] * extra)

    def test_rtde_decoder_uses_the_negotiated_autotune_output_fields(self) -> None:
        client = object.__new__(bridge.RTDEBridgeClient)
        client._output_fields = list(bridge.STEP5D_AUTOTUNE_HANDSHAKE_OUTPUT_FIELDS)
        values = (11, 22, 33, 44, 55, 66, 77)
        payload = bytes([9]) + bridge.struct.pack("!7i", *values)

        decoded = client._decode_output_sample(payload, ["INT32"] * len(values))

        self.assertEqual(
            decoded,
            dict(zip(bridge.STEP5D_AUTOTUNE_HANDSHAKE_OUTPUT_FIELDS, values)),
        )


if __name__ == "__main__":
    unittest.main()
