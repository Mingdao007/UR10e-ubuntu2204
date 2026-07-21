#!/usr/bin/env python3
"""Offline fake-RTDE tests for the continuous Step5d autotune host lane."""

from __future__ import annotations

import csv
import os
import math
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import build_step5d_autotune_tp as tp_builder  # noqa: E402
import kunwei_rtde_bridge as bridge  # noqa: E402
from step5d_autotune_contract import (  # noqa: E402
    CampaignSpec,
    CaptureArtifactPaths,
    CaptureManifest,
    Evaluation,
    ExecutionProfile,
    ForceCandidate,
    SafeClosureEvidence,
    TrialSource,
    TrialTransition,
    TrialTransitionKind,
    TrialDisposition,
    TrialSpec,
)
from step5d_autotune_live_driver import (  # noqa: E402
    AtomicCommandMailbox,
    BridgeMailboxRuntime,
    BridgeTrialCsvRotator,
    CampaignHomeReference,
    classify_reason4_host_cause,
    ClosureNotReady,
    HostClosureCollector,
    LegacyFloatStopRequired,
    MailboxError,
    TrialArtifactProducer,
    decode_execution_profile_id,
    execution_profile_id_for,
    finalize_bundle_and_dispatch_ack_for_test_fixture,
    integer_stop_transport,
    terminal_float_reason_crosscheck,
)
from step5d_autotune_state_machine import (  # noqa: E402
    HostCommand,
    HostPacket,
    LoopCoordinator,
    TpLoopState,
    TpPacket,
)
from step5d_runtime_interface import STEP5D_AUTOTUNE_STAGE_ID  # noqa: E402


SHA_A = "a" * 64
SHA_B = "b" * 64
SHA_C = "c" * 64


def make_trial(
    *,
    trial_id: int = 1,
    command_seq: int = 10,
    candidate_token: int = 20,
    candidate: ForceCandidate | None = None,
    profile: ExecutionProfile | None = None,
) -> TrialSpec:
    campaign = CampaignSpec(
        campaign_id="offline-fake-campaign",
        campaign_epoch=7,
        campaign_fingerprint=SHA_A,
    )
    selected = candidate or ForceCandidate()
    selected_profile = profile or ExecutionProfile("nf010-slew010-a010", 0.010)
    if selected == ForceCandidate():
        transition = TrialTransition(TrialTransitionKind.BASELINE)
    else:
        transition = TrialTransition(
            TrialTransitionKind.FORCE_SEARCH,
            source=TrialSource(
                trial_uid="0" * 64,
                candidate=ForceCandidate.from_log2(
                    p=selected.log2_p
                    - math.copysign(0.25, selected.log2_p),
                    damping=selected.log2_damping,
                    i=selected.log2_i if selected.i_mode == "positive" else 0.0,
                    i_off=selected.i_mode == "off",
                ),
                profile_id=selected_profile.profile_id,
                plant_epoch=1,
                campaign_id=campaign.campaign_id,
                campaign_epoch=campaign.campaign_epoch,
                campaign_fingerprint=campaign.campaign_fingerprint,
                backend_id="step5d-v35-native-test",
                source_fingerprint=SHA_B,
                config_fingerprint=SHA_C,
            ),
        )
    return TrialSpec(
        campaign=campaign,
        trial_id=trial_id,
        candidate_token=candidate_token,
        command_seq=command_seq,
        plant_epoch=1,
        candidate=selected,
        execution_profile=selected_profile,
        backend_id="step5d-v35-native-test",
        source_fingerprint=SHA_B,
        config_fingerprint=SHA_C,
        transition=transition,
    )


def execution_profile_id(profile: ExecutionProfile) -> int:
    normal = {0.010: 1, 0.015: 2, 0.020: 3, 0.030: 4}[
        profile.normal_max_rate_rad_s
    ]
    actuator = {0.1: 1, 0.2: 2, 0.5: 3}
    return (
        100 * normal
        + 10 * actuator[profile.host_qdot_slew_rad_s2]
        + actuator[profile.tp_speedj_accel_rad_s2]
    )


def make_prepared(trial: TrialSpec) -> SimpleNamespace:
    candidate = trial.candidate
    profile = trial.execution_profile
    return SimpleNamespace(
        trial=trial,
        frozen=SimpleNamespace(
            source_fingerprint=trial.source_fingerprint,
            config_fingerprint=trial.config_fingerprint,
            composite_fingerprint=trial.campaign.campaign_fingerprint,
        ),
        environment={
            "STEP5D_AUTOTUNE_FORCE_P": f"{candidate.force_p_gain:.12g}",
            "STEP5D_AUTOTUNE_FORCE_I": f"{candidate.force_i_gain:.12g}",
            "STEP5D_AUTOTUNE_FORCE_DAMPING": f"{candidate.force_damping:.12g}",
            "STEP5D_AUTOTUNE_NORMAL_RATE_RAD_S": (
                f"{profile.normal_max_rate_rad_s:.12g}"
            ),
            "STEP5D_AUTOTUNE_HOST_SLEW_RAD_S2": (
                f"{profile.host_qdot_slew_rad_s2:.12g}"
            ),
            "STEP5D_AUTOTUNE_SPEEDJ_ACCELERATION_RAD_S2": (
                f"{profile.tp_speedj_accel_rad_s2:.12g}"
            ),
        },
        runner_arguments=(),
    )


def packet_for(
    trial: TrialSpec,
    command: HostCommand,
    *,
    command_seq: int | None = None,
    profile_id: int | None = None,
) -> HostPacket:
    return HostPacket(
        campaign_epoch=trial.campaign.campaign_epoch,
        trial_id=trial.trial_id,
        command=command,
        candidate_token=trial.candidate_token,
        execution_profile_id=(
            execution_profile_id(trial.execution_profile)
            if profile_id is None
            else profile_id
        ),
        command_seq=trial.command_seq if command_seq is None else command_seq,
    )


def fake_rtde(
    state: TpLoopState,
    packet: HostPacket | None,
    *,
    consumed_seq: int,
    reason: int = 0,
    token_delta: int = 0,
    timestamp: float = 1.0,
) -> dict[str, object]:
    if packet is None:
        epoch = trial_id = token = profile_id = 0
    else:
        epoch = packet.campaign_epoch
        trial_id = packet.trial_id
        token = packet.candidate_token + token_delta
        profile_id = packet.execution_profile_id
    return {
        "timestamp": timestamp,
        "output_int_register_24": epoch,
        "output_int_register_25": trial_id,
        "output_int_register_26": int(state),
        "output_int_register_27": token,
        "output_int_register_28": reason,
        "output_int_register_29": profile_id,
        "output_int_register_30": consumed_seq,
        "output_double_register_36": 0.0002,
        "output_double_register_37": 0.001,
        "output_double_register_38": 0.0005,
        "actual_TCP_pose": [0.4, -0.2, 0.3, 0.0, 0.0, 0.0],
        "actual_q": [0.0] * 6,
        "actual_TCP_speed": [0.0] * 6,
        "actual_qd": [0.0] * 6,
        "safety_mode": "NORMAL",
    }


def fake_bridge_args() -> SimpleNamespace:
    return SimpleNamespace(
        step5d_autotune_handshake={
            "campaign_epoch": 0,
            "trial_id": 0,
            "command": 0,
            "candidate_token": 0,
            "execution_profile_id": 0,
            "command_seq": 0,
        },
        bridge_normal_max_rate_rad_s=0.010,
        step4e_normal_max_rate_rad_s=0.010,
    )


class Step5dAutotuneLiveDriverTest(unittest.TestCase):
    def test_no_arm_provider_blocks_before_mailbox_read(self) -> None:
        trial = make_trial()
        arm = packet_for(trial, HostCommand.ARM)
        with tempfile.TemporaryDirectory() as directory:
            mailbox_path = Path(directory) / "command.json"
            sink = AtomicCommandMailbox(mailbox_path)
            sink.send_command(arm, prepared_trial=make_prepared(trial))
            runtime = BridgeMailboxRuntime(
                mailbox_path,
                arming_context_provider=lambda: None,
            )
            args = fake_bridge_args()
            with patch.object(
                runtime.mailbox,
                "read_latest",
                wraps=runtime.mailbox.read_latest,
            ) as read_latest:
                self.assertFalse(
                    runtime.poll(
                        args,
                        fake_rtde(TpLoopState.READY_HOME, None, consumed_seq=0),
                    )
                )
                read_latest.assert_not_called()
            self.assertEqual(args.step5d_autotune_handshake["command"], 0)

    def test_published_arming_context_allows_one_mailbox_read(self) -> None:
        trial = make_trial()
        arm = packet_for(trial, HostCommand.ARM)
        with tempfile.TemporaryDirectory() as directory:
            mailbox_path = Path(directory) / "command.json"
            sink = AtomicCommandMailbox(mailbox_path)
            sink.send_command(arm, prepared_trial=make_prepared(trial))
            runtime = BridgeMailboxRuntime(
                mailbox_path,
                arming_context_provider=lambda: object(),
            )
            args = fake_bridge_args()
            with patch.object(
                runtime.mailbox,
                "read_latest",
                wraps=runtime.mailbox.read_latest,
            ) as read_latest:
                self.assertTrue(
                    runtime.poll(
                        args,
                        fake_rtde(TpLoopState.READY_HOME, None, consumed_seq=0),
                    )
                )
                read_latest.assert_called_once_with()
            self.assertEqual(args.step5d_autotune_handshake["command"], 1)

    def test_terminal_float_crosscheck_ignores_transient_search_reason(self) -> None:
        def tp(state: TpLoopState, reason: int) -> TpPacket:
            return TpPacket(7, 1, state, 20, reason, 111, 10)

        self.assertTrue(
            terminal_float_reason_crosscheck(
                [
                    (tp(TpLoopState.RUN, 0), 11.0),
                    (tp(TpLoopState.TERMINAL, 1), 1.0),
                    (tp(TpLoopState.WAIT_ACK, 1), 1.0),
                ],
                final_reason=1,
            )
        )
        self.assertFalse(
            terminal_float_reason_crosscheck(
                [(tp(TpLoopState.WAIT_ACK, 1), 11.0)],
                final_reason=1,
            )
        )

    def test_arm_closure_ack_ready_and_next_arm_in_one_bridge(self) -> None:
        trial1 = make_trial()
        prepared1 = make_prepared(trial1)
        arm1 = packet_for(trial1, HostCommand.ARM)
        with tempfile.TemporaryDirectory() as directory:
            mailbox_path = Path(directory) / "command.json"
            sink = AtomicCommandMailbox(mailbox_path)
            runtime = BridgeMailboxRuntime(mailbox_path)
            args = fake_bridge_args()

            sink.send_command(arm1, prepared_trial=prepared1)
            self.assertTrue(
                runtime.poll(
                    args,
                    fake_rtde(TpLoopState.READY_HOME, None, consumed_seq=0),
                )
            )
            self.assertEqual(args.step5d_autotune_handshake["command"], 1)
            self.assertEqual(args.step5d_autotune_force_terms["P"], 0.001)
            self.assertFalse(
                runtime.poll(
                    args,
                    fake_rtde(TpLoopState.RUN, arm1, consumed_seq=10),
                )
            )

            wait_ack = fake_rtde(
                TpLoopState.WAIT_ACK, arm1, consumed_seq=10, reason=1
            )
            collector = HostClosureCollector.for_test_fixture(
                expected_arm=arm1,
                campaign_home_pose=[0.4, -0.2, 0.3, 0.0, 0.0, 0.0],
                campaign_home_q=[0.0] * 6,
                max_sample_gap_s=0.02,
            )
            for index in range(51):
                collector.observe(wait_ack, monotonic_s=index * 0.01)
            evidence = collector.finalize(
                capture_hashes_complete=True,
                terminal_manifest_complete=True,
                fingerprint_closed=True,
            )
            self.assertTrue(evidence.returned_safe)

            ack1 = packet_for(trial1, HostCommand.ACK_BUNDLE, command_seq=11)
            sink.send_command(ack1, prepared_trial=prepared1)
            original_terms = dict(args.step5d_autotune_force_terms)
            self.assertTrue(runtime.poll(args, wait_ack))
            self.assertEqual(args.step5d_autotune_handshake["command"], 2)
            self.assertEqual(args.step5d_autotune_force_terms, original_terms)

            ready = fake_rtde(TpLoopState.READY_HOME, None, consumed_seq=11)
            self.assertFalse(runtime.poll(args, ready))
            profile2 = ExecutionProfile(
                "nf015-slew020-a050", 0.015, 0.2, 0.5
            )
            trial2 = make_trial(
                trial_id=2,
                command_seq=12,
                candidate_token=21,
                candidate=ForceCandidate(force_p_gain=0.002),
                profile=profile2,
            )
            arm2 = packet_for(trial2, HostCommand.ARM)
            sink.send_command(arm2, prepared_trial=make_prepared(trial2))
            self.assertTrue(runtime.poll(args, ready))
            self.assertEqual(args.step5d_autotune_force_terms["P"], 0.002)
            self.assertEqual(args.step5d_autotune_normal_rate_rad_s, 0.015)
            self.assertEqual(args.step5d_autotune_host_slew_rad_s2, 0.2)
            self.assertEqual(args.step5d_autotune_speedj_acceleration_rad_s2, 0.5)
            self.assertEqual(args.step5d_autotune_handshake["execution_profile_id"], 223)

    def test_profile_crosscheck_and_offline_030_fail_closed(self) -> None:
        self.assertEqual(
            decode_execution_profile_id(323, network_mode=True),
            (0.020, 0.2, 0.5),
        )
        with self.assertRaisesRegex(MailboxError, "three level digits"):
            decode_execution_profile_id(3, network_mode=True)
        with self.assertRaisesRegex(MailboxError, "offline_only"):
            decode_execution_profile_id(411, network_mode=True)
        with tempfile.TemporaryDirectory() as directory:
            sink = AtomicCommandMailbox(Path(directory) / "command.json")
            trial = make_trial()
            with self.assertRaisesRegex(MailboxError, "cross-check"):
                sink.send_command(
                    packet_for(trial, HostCommand.ARM, profile_id=112),
                    prepared_trial=make_prepared(trial),
                )
            offline = make_trial(
                profile=ExecutionProfile(
                    "nf030-offline", 0.030, live_eligible=False
                )
            )
            with self.assertRaisesRegex(MailboxError, "offline_only"):
                sink.send_command(
                    packet_for(offline, HostCommand.ARM),
                    prepared_trial=make_prepared(offline),
                )

    def test_live_050_profile_has_unique_network_code(self) -> None:
        profile = ExecutionProfile("nf050-slew050-a050", 0.050, 0.5, 0.5)
        self.assertEqual(execution_profile_id_for(profile, network_mode=True), 533)
        self.assertEqual(
            decode_execution_profile_id(533, network_mode=True),
            (0.050, 0.5, 0.5),
        )
        self.assertEqual(
            decode_execution_profile_id(333, network_mode=True),
            (0.020, 0.5, 0.5),
        )

    def test_canonical_live_100_profile_has_unique_network_code(self) -> None:
        profile = ExecutionProfile("nf100-slew050-a050", 0.100, 0.5, 0.5)
        self.assertEqual(execution_profile_id_for(profile, network_mode=True), 633)
        self.assertEqual(
            decode_execution_profile_id(633, network_mode=True),
            (0.100, 0.5, 0.5),
        )

    def test_live_profile_integer_codec_is_injective_across_full_lattice(self) -> None:
        encoded: dict[int, tuple[float, float, float]] = {}
        for normal in (0.010, 0.015, 0.020, 0.050, 0.100):
            for host_slew in (0.1, 0.2, 0.5):
                for tp_accel in (0.1, 0.2, 0.5):
                    profile = ExecutionProfile(
                        (
                            f"nf{round(normal * 1000):03d}"
                            f"-slew{round(host_slew * 100):03d}"
                            f"-a{round(tp_accel * 100):03d}"
                        ),
                        normal,
                        host_slew,
                        tp_accel,
                    )
                    code = execution_profile_id_for(profile, network_mode=True)
                    self.assertNotIn(code, encoded)
                    encoded[code] = (normal, host_slew, tp_accel)
                    self.assertEqual(
                        decode_execution_profile_id(code, network_mode=True),
                        (normal, host_slew, tp_accel),
                    )
        self.assertEqual(len(encoded), 45)

    def test_offline_030_mailbox_round_trip_preserves_offline_mode(self) -> None:
        offline = make_trial(
            profile=ExecutionProfile("nf030-offline", 0.030, live_eligible=False)
        )
        arm = packet_for(offline, HostCommand.ARM)
        with tempfile.TemporaryDirectory() as directory:
            mailbox = AtomicCommandMailbox(
                Path(directory) / "offline-command.json",
                network_mode=False,
            )
            mailbox.send_command(arm, prepared_trial=make_prepared(offline))
            decoded = mailbox.read_latest()
        self.assertIsNotNone(decoded)
        assert decoded is not None
        self.assertEqual(decoded.packet.execution_profile_id, 411)
        self.assertEqual(decoded.binding.profile.normal_max_rate_rad_s, 0.030)
        self.assertFalse(decoded.binding.profile.live_eligible)

    def test_disconnect_reconnect_identity_change_and_stale_ack_fail_closed(self) -> None:
        trial = make_trial()
        prepared = make_prepared(trial)
        arm = packet_for(trial, HostCommand.ARM)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "command.json"
            sink = AtomicCommandMailbox(path)
            runtime = BridgeMailboxRuntime(path)
            args = fake_bridge_args()
            sink.send_command(arm, prepared_trial=prepared)
            runtime.poll(
                args,
                fake_rtde(TpLoopState.READY_HOME, None, consumed_seq=0),
                connection_epoch=0,
            )
            self.assertTrue(runtime.identity_commit_pending)
            self.assertFalse(
                runtime.poll(
                    args,
                    fake_rtde(TpLoopState.RUN, arm, consumed_seq=10),
                    connection_epoch=0,
                )
            )
            self.assertFalse(runtime.identity_commit_pending)
            with self.assertRaisesRegex(MailboxError, "identity changed"):
                runtime.poll(
                    args,
                    fake_rtde(
                        TpLoopState.RUN,
                        arm,
                        consumed_seq=10,
                        token_delta=1,
                    ),
                    connection_epoch=1,
                )

            wait_ack = fake_rtde(
                TpLoopState.WAIT_ACK, arm, consumed_seq=10, reason=1
            )
            ack = packet_for(trial, HostCommand.ACK_BUNDLE, command_seq=11)
            sink.send_command(ack, prepared_trial=prepared)
            runtime.poll(args, wait_ack, connection_epoch=1)
            path.write_text(path.read_text(encoding="ascii") + "\n", encoding="ascii")
            with self.assertRaisesRegex(MailboxError, "did not increase"):
                runtime.poll(args, wait_ack, connection_epoch=1)

    def test_arm_identity_commit_tolerates_only_bounded_partial_publication(self) -> None:
        trial = make_trial()
        prepared = make_prepared(trial)
        arm = packet_for(trial, HostCommand.ARM)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "command.json"
            sink = AtomicCommandMailbox(path)
            sink.send_command(arm, prepared_trial=prepared)
            args = fake_bridge_args()
            runtime = BridgeMailboxRuntime(path)
            self.assertTrue(
                runtime.poll(
                    args,
                    fake_rtde(TpLoopState.READY_HOME, None, consumed_seq=0),
                    connection_epoch=0,
                )
            )
            self.assertTrue(runtime.identity_commit_pending)
            self.assertFalse(
                runtime.poll(
                    args,
                    fake_rtde(
                        TpLoopState.ARMED,
                        arm,
                        consumed_seq=0,
                        token_delta=1,
                    ),
                    connection_epoch=0,
                )
            )
            self.assertTrue(runtime.identity_commit_pending)
            self.assertFalse(
                runtime.poll(
                    args,
                    fake_rtde(TpLoopState.ARMED, arm, consumed_seq=arm.command_seq),
                    connection_epoch=0,
                )
            )
            self.assertFalse(runtime.identity_commit_pending)

    def test_arm_identity_commit_fails_closed_on_run_reconnect_or_timeout(self) -> None:
        trial = make_trial()
        prepared = make_prepared(trial)
        arm = packet_for(trial, HostCommand.ARM)

        def pending_runtime(directory: str) -> tuple[BridgeMailboxRuntime, object]:
            path = Path(directory) / "command.json"
            sink = AtomicCommandMailbox(path)
            sink.send_command(arm, prepared_trial=prepared)
            args = fake_bridge_args()
            runtime = BridgeMailboxRuntime(path)
            runtime.poll(
                args,
                fake_rtde(TpLoopState.READY_HOME, None, consumed_seq=0),
                connection_epoch=0,
            )
            return runtime, args

        with tempfile.TemporaryDirectory() as directory:
            runtime, args = pending_runtime(directory)
            with self.assertRaisesRegex(MailboxError, "RUN before ARM identity commit"):
                runtime.poll(
                    args,
                    fake_rtde(TpLoopState.RUN, arm, consumed_seq=0),
                    connection_epoch=0,
                )
        with tempfile.TemporaryDirectory() as directory:
            runtime, args = pending_runtime(directory)
            with self.assertRaisesRegex(MailboxError, "reconnected during"):
                runtime.poll(
                    args,
                    fake_rtde(TpLoopState.ARMED, arm, consumed_seq=0),
                    connection_epoch=1,
                )
        with tempfile.TemporaryDirectory() as directory:
            runtime, args = pending_runtime(directory)
            assert runtime._pending_arm_started_s is not None
            runtime._pending_arm_started_s -= runtime.IDENTITY_COMMIT_TIMEOUT_S + 0.001
            with self.assertRaisesRegex(MailboxError, "commit timed out"):
                runtime.poll(
                    args,
                    fake_rtde(TpLoopState.ARMED, arm, consumed_seq=0),
                    connection_epoch=0,
                )

    def test_fresh_bridge_reattaches_run_and_wait_ack_from_mailbox_binding(self) -> None:
        trial = make_trial()
        prepared = make_prepared(trial)
        arm = packet_for(trial, HostCommand.ARM)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "command.json"
            sink = AtomicCommandMailbox(path)
            sink.send_command(arm, prepared_trial=prepared)
            args = fake_bridge_args()
            runtime = BridgeMailboxRuntime(path)
            self.assertFalse(
                runtime.poll(
                    args,
                    fake_rtde(TpLoopState.RUN, arm, consumed_seq=arm.command_seq),
                )
            )
            self.assertEqual(runtime.active.binding.trial_uid, trial.trial_uid)
            self.assertEqual(args.step5d_autotune_handshake["command"], 1)
            self.assertEqual(args.step5d_autotune_force_terms["P"], 0.001)

            ack = packet_for(
                trial,
                HostCommand.ACK_BUNDLE,
                command_seq=arm.command_seq + 1,
            )
            sink.send_command(ack, prepared_trial=prepared)
            restarted = BridgeMailboxRuntime(path)
            wait_ack = fake_rtde(
                TpLoopState.WAIT_ACK,
                arm,
                consumed_seq=arm.command_seq,
                reason=1,
            )
            self.assertTrue(restarted.poll(args, wait_ack))
            self.assertEqual(args.step5d_autotune_handshake["command"], 2)

    def test_per_trial_rotator_rejects_other_identity_and_publishes_at_wait_ack(self) -> None:
        trial = make_trial()
        arm = packet_for(trial, HostCommand.ARM)
        with tempfile.TemporaryDirectory() as directory:
            mailbox_path = Path(directory) / "command.json"
            sink = AtomicCommandMailbox(mailbox_path)
            sink.send_command(arm, prepared_trial=make_prepared(trial))
            active = sink.read_latest()
            assert active is not None
            capture_root = (Path(directory) / "captures").absolute()
            rotator = BridgeTrialCsvRotator(
                capture_root,
                ("stage", "path_time_s", "force_b_z_n"),
            )
            wrong = fake_rtde(
                TpLoopState.RUN,
                arm,
                consumed_seq=arm.command_seq,
                token_delta=1,
            )
            self.assertFalse(
                rotator.observe(
                    {"stage": 25, "path_time_s": 5.1, "force_b_z_n": 12.0},
                    active=active,
                    rtde_output=wrong,
                )
            )
            run = fake_rtde(
                TpLoopState.RUN,
                arm,
                consumed_seq=arm.command_seq,
            )
            self.assertTrue(
                rotator.observe(
                    {"stage": 25, "path_time_s": 5.1, "force_b_z_n": 12.0},
                    active=active,
                    rtde_output=run,
                )
            )
            wait_ack = fake_rtde(
                TpLoopState.WAIT_ACK,
                arm,
                consumed_seq=arm.command_seq,
                reason=1,
            )
            self.assertTrue(
                rotator.observe(
                    {"stage": 25, "path_time_s": 60.0, "force_b_z_n": 12.0},
                    active=active,
                    rtde_output=wait_ack,
                )
            )
            rotator.close()
            capture_path = capture_root / trial.trial_uid / "capture.csv"
            self.assertTrue(capture_path.is_file())
            text = capture_path.read_text()
            self.assertEqual(text.count(trial.trial_uid), 2)
            self.assertNotIn(str(trial.candidate_token + 1), text)

    def test_run_stop_uses_legacy_float_and_integer_stop_is_outer_only(self) -> None:
        self.assertEqual(
            classify_reason4_host_cause("rtde_feedback_stale_structural_stop"),
            "infra_stop",
        )
        with self.assertRaisesRegex(MailboxError, "cannot be typed"):
            classify_reason4_host_cause("rnn_evidence_only")
        self.assertEqual(
            integer_stop_transport(TpLoopState.RUN), "legacy_float_stop_request"
        )
        self.assertEqual(integer_stop_transport(TpLoopState.READY_HOME), "integer_stop")
        self.assertEqual(integer_stop_transport(TpLoopState.WAIT_ACK), "integer_stop")
        trial = make_trial()
        prepared = make_prepared(trial)
        arm = packet_for(trial, HostCommand.ARM)
        stop = packet_for(trial, HostCommand.STOP, command_seq=11)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "command.json"
            sink = AtomicCommandMailbox(path)
            runtime = BridgeMailboxRuntime(path)
            args = fake_bridge_args()
            sink.send_command(arm, prepared_trial=prepared)
            runtime.poll(args, fake_rtde(TpLoopState.READY_HOME, None, consumed_seq=0))
            sink.send_command(stop, prepared_trial=prepared)
            with self.assertRaises(LegacyFloatStopRequired):
                runtime.poll(
                    args, fake_rtde(TpLoopState.RUN, arm, consumed_seq=10)
                )

    def test_closure_gap_identity_and_safety_changes_reset_dwell(self) -> None:
        trial = make_trial()
        arm = packet_for(trial, HostCommand.ARM)
        collector = HostClosureCollector.for_test_fixture(
            expected_arm=arm,
            campaign_home_pose=[0.4, -0.2, 0.3, 0.0, 0.0, 0.0],
            campaign_home_q=[0.0] * 6,
            max_sample_gap_s=0.02,
        )
        sample = fake_rtde(TpLoopState.WAIT_ACK, arm, consumed_seq=10, reason=1)
        for index in range(21):
            collector.observe(sample, monotonic_s=index * 0.01)
        self.assertAlmostEqual(collector.dwell_s, 0.2)
        collector.observe(sample, monotonic_s=0.30)
        self.assertEqual(collector.dwell_s, 0.0)
        wrong = dict(sample)
        wrong["output_int_register_27"] = trial.candidate_token + 1
        collector.observe(wrong, monotonic_s=0.31)
        self.assertEqual(collector.dwell_s, 0.0)
        unsafe = dict(sample)
        unsafe["safety_mode"] = "PROTECTIVE_STOP"
        collector.observe(unsafe, monotonic_s=0.32)
        self.assertEqual(collector.dwell_s, 0.0)

    def test_production_assessment_derives_missing_evidence_and_rejects_tamper(self) -> None:
        trial = make_trial()
        arm = packet_for(trial, HostCommand.ARM)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).absolute()
            trial_dir = root / trial.trial_uid
            trial_dir.mkdir()
            path = trial_dir / "capture.csv"
            fields = [
                "autotune_trial_uid",
                "autotune_backend_id",
                *(f"ur_output_int_register_{index}" for index in range(24, 31)),
            ]
            row = {
                "autotune_trial_uid": trial.trial_uid,
                "autotune_backend_id": trial.backend_id,
                "ur_output_int_register_24": arm.campaign_epoch,
                "ur_output_int_register_25": arm.trial_id,
                "ur_output_int_register_26": int(TpLoopState.WAIT_ACK),
                "ur_output_int_register_27": arm.candidate_token,
                "ur_output_int_register_28": 1,
                "ur_output_int_register_29": arm.execution_profile_id,
                "ur_output_int_register_30": arm.command_seq,
            }
            with path.open("w", newline="", encoding="utf-8") as handle:
                writer = csv.DictWriter(handle, fieldnames=fields)
                writer.writeheader()
                writer.writerow(row)
            producer = TrialArtifactProducer(root, trial)
            assessment = producer.derive_assessment(
                arm, expected_terminal_reason=1
            )
            self.assertFalse(assessment.completion_marker)
            self.assertFalse(assessment.cadence_ok)
            self.assertFalse(assessment.feedback_fresh)
            self.assertFalse(assessment.rnn_oracle_aligned)

            row["ur_output_int_register_27"] = arm.candidate_token + 1
            with path.open("w", newline="", encoding="utf-8") as handle:
                writer = csv.DictWriter(handle, fieldnames=fields)
                writer.writeheader()
                writer.writerow(row)
            with self.assertRaisesRegex(MailboxError, "exact ARM identity"):
                producer.derive_assessment(arm, expected_terminal_reason=1)

    def test_reason4_and_campaign_home_are_derived_from_bridge_provenance(self) -> None:
        trial = make_trial()
        prepared = make_prepared(trial)
        arm = packet_for(trial, HostCommand.ARM)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).absolute()
            mailbox = AtomicCommandMailbox(root / "command.json")
            mailbox.send_command(arm, prepared_trial=prepared)
            runtime = BridgeMailboxRuntime(root / "command.json")
            runtime.poll(
                fake_bridge_args(),
                fake_rtde(TpLoopState.READY_HOME, None, consumed_seq=0),
            )
            reference = runtime.campaign_home_reference
            self.assertIsInstance(reference, CampaignHomeReference)
            assert reference is not None
            reference.verify_trial(trial)

            trial_dir = root / "captures" / trial.trial_uid
            trial_dir.mkdir(parents=True)
            path = trial_dir / "capture.csv"
            fields = [
                "autotune_trial_uid",
                "autotune_backend_id",
                "guard_reason",
                "stop_request",
                *(f"ur_output_int_register_{index}" for index in range(24, 31)),
            ]
            row = {
                "autotune_trial_uid": trial.trial_uid,
                "autotune_backend_id": trial.backend_id,
                "guard_reason": "rtde_feedback_stale_structural_stop",
                "stop_request": 1,
                "ur_output_int_register_24": arm.campaign_epoch,
                "ur_output_int_register_25": arm.trial_id,
                "ur_output_int_register_26": int(TpLoopState.WAIT_ACK),
                "ur_output_int_register_27": arm.candidate_token,
                "ur_output_int_register_28": 4,
                "ur_output_int_register_29": arm.execution_profile_id,
                "ur_output_int_register_30": arm.command_seq,
            }
            with path.open("w", newline="", encoding="utf-8") as handle:
                writer = csv.DictWriter(handle, fieldnames=fields)
                writer.writeheader()
                writer.writerow(row)
            assessment = TrialArtifactProducer(
                root / "captures", trial
            ).derive_assessment(arm, expected_terminal_reason=4)
            self.assertEqual(assessment.host_cause, "infra_stop")
            self.assertEqual(
                assessment.evidence["reason4_cause_status"],
                "typed_from_bridge_capture",
            )

            reference.path.write_bytes(reference.path.read_bytes() + b"\n")
            with self.assertRaisesRegex(MailboxError, "does not bind"):
                reference.verify_trial(trial)

        fixture = HostClosureCollector.for_test_fixture(
            expected_arm=arm,
            campaign_home_pose=[0.4, -0.2, 0.3, 0.0, 0.0, 0.0],
            campaign_home_q=[0.0] * 6,
        )
        with self.assertRaises(ClosureNotReady):
            fixture.require_production_home(trial)

    def test_loop_coordinator_ack_precedes_wait_infra(self) -> None:
        trial = make_trial()
        coordinator = LoopCoordinator()
        coordinator.arm(trial)
        evidence = SafeClosureEvidence(
            tp_position_error_m=0.0,
            tp_orientation_error_rad=0.0,
            tp_joint_error_max_rad=0.0,
            host_position_error_m=0.0,
            host_orientation_error_rad=0.0,
            host_joint_error_max_rad=0.0,
            host_tcp_linear_speed_m_s=0.0,
            host_tcp_angular_speed_rad_s=0.0,
            host_qd_max_rad_s=0.0,
            host_safety_mode="NORMAL",
            host_dwell_s=0.5,
            trial_token_match=True,
            capture_hashes_complete=True,
            terminal_manifest_complete=True,
            fingerprint_closed=True,
        )
        disposition = coordinator.close_trial(
            reason=8,
            host_cause=None,
            evidence=evidence,
            eligible_evidence=False,
            immutable_bundle_written=True,
        )
        self.assertIs(disposition, TrialDisposition.WAIT_INFRA_READY)
        self.assertIs(coordinator.state, TpLoopState.WAIT_ACK)
        with self.assertRaises(RuntimeError):
            coordinator.arm(trial)
        coordinator.ack_bundle()
        self.assertIs(coordinator.state, TpLoopState.WAIT_INFRA_READY)
        coordinator.infra_ready()
        self.assertIs(coordinator.state, TpLoopState.READY_HOME)

    def test_closure_bundle_issue_ack_and_mailbox_dispatch_order(self) -> None:
        trial = make_trial()
        arm = packet_for(trial, HostCommand.ARM)
        collector = HostClosureCollector.for_test_fixture(
            expected_arm=arm,
            campaign_home_pose=[0.4, -0.2, 0.3, 0.0, 0.0, 0.0],
            campaign_home_q=[0.0] * 6,
            max_sample_gap_s=0.02,
        )
        sample = fake_rtde(TpLoopState.WAIT_ACK, arm, consumed_seq=10, reason=1)
        for index in range(51):
            collector.observe(sample, monotonic_s=index * 0.01)
        order: list[str] = []

        def manifest_factory(closure: SafeClosureEvidence) -> CaptureManifest:
            return CaptureManifest(
                trial_uid=trial.trial_uid,
                backend_id=trial.backend_id,
                source_fingerprint_pre=trial.source_fingerprint,
                source_fingerprint_post=trial.source_fingerprint,
                config_fingerprint_pre=trial.config_fingerprint,
                config_fingerprint_post=trial.config_fingerprint,
                candidate_token=trial.candidate_token,
                terminal_reason=1,
                host_cause=None,
                csv_sha256="d" * 64,
                metadata_sha256="e" * 64,
                terminal_manifest_sha256="f" * 64,
                completion_marker=True,
                cadence_ok=True,
                feedback_fresh=True,
                rnn_oracle_aligned=True,
                safety_normal=True,
                returned_safe=True,
                immutable_bundle_written=True,
                stage25_complete_s=55.0,
                safe_closure_evidence=closure,
            )

        evaluation = Evaluation(
            trial_uid=trial.trial_uid,
            backend_id=trial.backend_id,
            eligible=True,
            disposition=TrialDisposition.OBJECTIVE,
            objective_mae_n=0.2,
            force_bias_n=0.0,
            force_std_n=0.1,
            coverage_12_plus_minus_1_ratio=1.0,
            complete_bins=550,
            safe_closure=True,
        )

        class Backend:
            def evaluate_trial(self, *args):
                order.append("evaluate")
                return evaluation

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bundle_path = (root / "immutable_trial_bundle.json").absolute()

            class Store:
                def write_trial_bundle(self, *args, **kwargs):
                    order.append("bundle")
                    bundle_path.write_text("{}\n", encoding="ascii")
                    return bundle_path

                def read_resume_history(self):
                    order.append("history")
                    return [
                        {
                            "trial_uid": trial.trial_uid,
                            "history_identity": "9" * 64,
                        }
                    ]

            ack = packet_for(trial, HostCommand.ACK_BUNDLE, command_seq=11)

            class Coordinator:
                def close_trial(self, **kwargs):
                    order.append("close")
                    self.assert_bundle_exists = bundle_path.exists()
                    return SimpleNamespace(ack_permitted=True)

                def issue_ack(self, path, *, verified_resume_history):
                    order.append("issue_ack")
                    self.assert_history = verified_resume_history
                    return ack

                def dispatch(self, packet, *, prepared_trial, sink):
                    order.append("dispatch")
                    sink.send_command(packet, prepared_trial=prepared_trial)

            coordinator = Coordinator()
            mailbox = AtomicCommandMailbox(root / "command.json")
            result = finalize_bundle_and_dispatch_ack_for_test_fixture(
                collector=collector,
                trial=trial,
                manifest_factory=manifest_factory,
                backend=Backend(),
                store=Store(),
                coordinator=coordinator,
                artifact_paths=CaptureArtifactPaths(
                    csv_path=root / "capture.csv",
                    metadata_path=root / "metadata.json",
                    terminal_manifest_path=root / "terminal.json",
                ),
                csv_path=root / "capture.csv",
                prepared_trial=make_prepared(trial),
                command_sink=mailbox,
                capture_hashes_complete=True,
                terminal_manifest_complete=True,
                fingerprint_closed=True,
            )
            dispatched = mailbox.read_latest()

        self.assertEqual(
            order,
            ["evaluate", "bundle", "close", "history", "issue_ack", "dispatch"],
        )
        self.assertTrue(coordinator.assert_bundle_exists)
        self.assertEqual(result.ack_packet, ack)
        self.assertIsNotNone(dispatched)
        assert dispatched is not None
        self.assertIs(dispatched.packet.command, HostCommand.ACK_BUNDLE)

    def test_tp_builder_validates_full_network_profile_and_documents_stop(self) -> None:
        script = tp_builder.render_script()
        self.assertIn("codex_autotune_network_profile_valid", script)
        self.assertIn("codex_autotune_post_ack_state", script)
        self.assertIn("post_ack_state == 75", script)
        self.assertIn("normal_level == 5", script)
        self.assertIn("legacy float stop_request safety carrier", script)

    def test_bridge_mailbox_requires_absolute_path_and_cupy(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(SystemExit, "absolute"):
                bridge.parse_args(
                    [
                        "--bridge-profile",
                        STEP5D_AUTOTUNE_STAGE_ID,
                        "--step5d-autotune-command-mailbox",
                        "relative.json",
                    ]
                )
            with tempfile.TemporaryDirectory() as directory:
                path = str(Path(directory) / "command.json")
                args = bridge.parse_args(
                    [
                        "--bridge-profile",
                        STEP5D_AUTOTUNE_STAGE_ID,
                        "--step5d-autotune-command-mailbox",
                        path,
                    ]
                )
                self.assertEqual(args.step5d_rnn_backend, "cupy")
                self.assertEqual(args.duration_s, 30.0)
                self.assertFalse(bridge.bridge_duration_expired(args, 10_000.0))
                static_args = bridge.parse_args(
                    ["--bridge-profile", STEP5D_AUTOTUNE_STAGE_ID]
                )
                self.assertTrue(bridge.bridge_duration_expired(static_args, 31.0))
                with self.assertRaisesRegex(SystemExit, "CPU fallback"):
                    bridge.parse_args(
                        [
                            "--bridge-profile",
                            STEP5D_AUTOTUNE_STAGE_ID,
                            "--step5d-autotune-command-mailbox",
                            path,
                            "--step5d-rnn-backend",
                            "numpy",
                        ]
                    )


if __name__ == "__main__":
    unittest.main()
