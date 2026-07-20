from __future__ import annotations

import sys
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from step5d_autotune_contract import (  # noqa: E402
    CampaignSpec,
    ExecutionProfile,
    ForceCandidate,
    TrialSpec,
    TrialTransition,
    TrialTransitionKind,
)
from step5d_autotune_live_driver import (  # noqa: E402
    AtomicCommandMailbox,
    BridgeMailboxRuntime,
    MailboxError,
)
from step5d_autotune_state_machine import (  # noqa: E402
    HostCommand,
    HostPacket,
    TpLoopState,
)
from step5d_autotune_v3.runtime_profile import (  # noqa: E402
    DEFAULT_OVERLAY,
    is_control_candidate_step,
    load_launch_profile,
)
from step5d_autotune_batch_plan import initialize_plan  # noqa: E402
import run_step5d_autotune_v3_bridge as bridge_wrapper  # noqa: E402
from run_step5d_autotune_v3_bridge import (  # noqa: E402
    V3AsyncBridgeTrialCsvRotator,
    _V3_RUNNER_CLOSURE_FIELDS,
    _apply_v3_arm_runtime,
)
import run_step5d_autotune_v3_live as live  # noqa: E402
from run_step5d_autotune_v3_live import (  # noqa: E402
    INITIAL_CONTROL_LOG2_K,
    INITIAL_LOG2,
    initial_candidates,
    initial_control_overlays,
)


def _prepared(overlay: dict) -> SimpleNamespace:
    candidate = ForceCandidate()
    profile = ExecutionProfile("nf050-slew050-a050", 0.05, 0.5, 0.5)
    trial = TrialSpec(
        campaign=CampaignSpec("v3-overlay-test", 1, "a" * 64),
        trial_id=1,
        candidate_token=2,
        command_seq=3,
        plant_epoch=1,
        candidate=candidate,
        execution_profile=profile,
        backend_id="step5d_v35_native_backend_v1",
        source_fingerprint="b" * 64,
        config_fingerprint="c" * 64,
        transition=TrialTransition(TrialTransitionKind.BASELINE),
    )
    return SimpleNamespace(
        trial=trial,
        frozen=SimpleNamespace(
            source_fingerprint=trial.source_fingerprint,
            config_fingerprint=trial.config_fingerprint,
            composite_fingerprint=trial.campaign.campaign_fingerprint,
        ),
        environment={
            "STEP5D_AUTOTUNE_FORCE_P": str(candidate.force_p_gain),
            "STEP5D_AUTOTUNE_FORCE_I": str(candidate.force_i_gain),
            "STEP5D_AUTOTUNE_FORCE_DAMPING": str(candidate.force_damping),
            "STEP5D_AUTOTUNE_NORMAL_RATE_RAD_S": "0.05",
            "STEP5D_AUTOTUNE_HOST_SLEW_RAD_S2": "0.5",
            "STEP5D_AUTOTUNE_SPEEDJ_ACCELERATION_RAD_S2": "0.5",
        },
        runner_arguments=(),
        trial_overlay=overlay,
    )


def _prepared_next(prepared: SimpleNamespace) -> SimpleNamespace:
    trial = replace(
        prepared.trial,
        trial_id=2,
        candidate_token=3,
        command_seq=4,
    )
    return SimpleNamespace(
        **{
            **vars(prepared),
            "trial": trial,
            "frozen": SimpleNamespace(
                source_fingerprint=trial.source_fingerprint,
                config_fingerprint=trial.config_fingerprint,
                composite_fingerprint=trial.campaign.campaign_fingerprint,
            ),
        }
    )


def _prepared_at(
    prepared: SimpleNamespace,
    *,
    trial_id: int,
    candidate_token: int,
    command_seq: int,
) -> SimpleNamespace:
    trial = replace(
        prepared.trial,
        trial_id=trial_id,
        candidate_token=candidate_token,
        command_seq=command_seq,
    )
    return SimpleNamespace(
        **{
            **vars(prepared),
            "trial": trial,
            "frozen": SimpleNamespace(
                source_fingerprint=trial.source_fingerprint,
                config_fingerprint=trial.config_fingerprint,
                composite_fingerprint=trial.campaign.campaign_fingerprint,
            ),
        }
    )


def _rtde(
    state: TpLoopState,
    packet: HostPacket | None,
    *,
    consumed_seq: int,
    reason: int = 0,
) -> dict[str, object]:
    return {
        "timestamp": 1.0,
        "safety_mode": 1,
        "actual_TCP_pose": [0.45, 0.10, 0.055, 3.128, 0.0, 0.042],
        "actual_TCP_speed": [0.0] * 6,
        "actual_q": [0.62, -1.65, -2.55, -0.49, 1.55, -0.95],
        "actual_qd": [0.0] * 6,
        "output_int_register_24": 0 if packet is None else packet.campaign_epoch,
        "output_int_register_25": 0 if packet is None else packet.trial_id,
        "output_int_register_26": int(state),
        "output_int_register_27": 0 if packet is None else packet.candidate_token,
        "output_int_register_28": reason,
        "output_int_register_29": 0 if packet is None else packet.execution_profile_id,
        "output_int_register_30": consumed_seq,
    }


def test_v3_mailbox_binds_and_applies_all_seven_preload_fields(tmp_path: Path) -> None:
    overlay = dict(DEFAULT_OVERLAY)
    overlay.update(
        {
            "step5d_preload_filtered_min_n": 6.0,
            "step5d_preload_filtered_max_n": 16.0,
            "step5d_preload_raw_min_n": 5.0,
            "step5d_preload_raw_max_n": 18.0,
            "step5d_preload_force_norm_max_n": 25.0,
            "step5d_preload_hold_s": 0.2,
            "step5d_preload_timeout_s": 12.0,
        }
    )
    prepared = _prepared(overlay)
    trial = prepared.trial
    packet = HostPacket(
        campaign_epoch=1,
        trial_id=1,
        command=HostCommand.ARM,
        candidate_token=2,
        execution_profile_id=533,
        command_seq=3,
    )
    path = (tmp_path / "command.json").absolute()
    AtomicCommandMailbox(path).send_command(packet, prepared_trial=prepared)
    command = AtomicCommandMailbox(path).read_latest()
    assert command is not None
    assert command.binding.trial_overlay == overlay

    args = SimpleNamespace()
    BridgeMailboxRuntime._apply_arm_runtime(args, command.binding)
    for field in (
        "step5d_preload_filtered_min_n",
        "step5d_preload_filtered_max_n",
        "step5d_preload_raw_min_n",
        "step5d_preload_raw_max_n",
        "step5d_preload_force_norm_max_n",
        "step5d_preload_hold_s",
        "step5d_preload_timeout_s",
    ):
        assert getattr(args, field) == overlay[field]


def test_v1_mailbox_schema_remains_without_trial_overlay(tmp_path: Path) -> None:
    prepared = _prepared(dict(DEFAULT_OVERLAY))
    del prepared.trial_overlay
    packet = HostPacket(1, 1, HostCommand.ARM, 2, 533, 3)
    path = (tmp_path / "command.json").absolute()
    AtomicCommandMailbox(path).send_command(packet, prepared_trial=prepared)
    command = AtomicCommandMailbox(path).read_latest()
    assert command is not None
    assert command.binding.trial_overlay is None


def _direct_runtime(
    tmp_path: Path,
) -> tuple[
    SimpleNamespace,
    SimpleNamespace,
    HostPacket,
    AtomicCommandMailbox,
    BridgeMailboxRuntime,
]:
    prepared = _prepared(dict(DEFAULT_OVERLAY))
    arm1 = HostPacket(1, 1, HostCommand.ARM, 2, 533, 3)
    mailbox = AtomicCommandMailbox((tmp_path / "command.json").absolute())
    mailbox.send_command(arm1, prepared_trial=prepared)
    runtime = BridgeMailboxRuntime(
        mailbox.path,
        completion_protocol="v3_direct_arm_v1",
    )
    args = SimpleNamespace()
    assert runtime.poll(
        args,
        _rtde(TpLoopState.READY_HOME, None, consumed_seq=0),
    )
    assert runtime.last_command_seq == arm1.command_seq
    return prepared, args, arm1, mailbox, runtime


def test_direct_mailbox_rejects_fresh_legacy_ack_without_advancing_sequence(
    tmp_path: Path,
) -> None:
    prepared, args, arm1, mailbox, runtime = _direct_runtime(tmp_path)
    ack = HostPacket(1, 1, HostCommand.ACK_BUNDLE, 2, 533, 4)
    mailbox.send_command(ack, prepared_trial=prepared)
    with pytest.raises(MailboxError, match="legacy ACK is forbidden"):
        runtime.poll(
            args,
            _rtde(
                TpLoopState.READY_NEAR,
                arm1,
                consumed_seq=arm1.command_seq,
                reason=1,
            ),
        )
    assert runtime.last_command_seq == arm1.command_seq
    assert runtime.last_command is not None
    assert runtime.last_command.packet.command is HostCommand.ARM
    assert args.step5d_autotune_handshake["command"] == int(HostCommand.ARM)


@pytest.mark.parametrize(
    "state",
    (
        TpLoopState.READY_HOME_CLOSED,
        TpLoopState.WAIT_INFRA_READY,
        TpLoopState.FAULT,
    ),
)
def test_direct_mailbox_forbids_next_arm_after_terminal_halt_states(
    tmp_path: Path,
    state: TpLoopState,
) -> None:
    prepared, args, arm1, mailbox, runtime = _direct_runtime(tmp_path)
    prepared2 = _prepared_next(prepared)
    arm2 = HostPacket(1, 2, HostCommand.ARM, 3, 533, 4)
    mailbox.send_command(arm2, prepared_trial=prepared2)
    with pytest.raises(MailboxError, match="fresh ARM is invalid"):
        runtime.poll(
            args,
            _rtde(state, arm1, consumed_seq=arm1.command_seq, reason=1),
        )
    assert runtime.last_command_seq == arm1.command_seq


def test_direct_mailbox_accepts_next_arm_only_from_ready_near(tmp_path: Path) -> None:
    prepared, args, arm1, mailbox, runtime = _direct_runtime(tmp_path)
    prepared2 = _prepared_next(prepared)
    arm2 = HostPacket(1, 2, HostCommand.ARM, 3, 533, 4)
    mailbox.send_command(arm2, prepared_trial=prepared2)
    assert runtime.poll(
        args,
        _rtde(
            TpLoopState.READY_NEAR,
            arm1,
            consumed_seq=arm1.command_seq,
            reason=1,
        ),
    )
    assert runtime.active is not None
    assert runtime.active.packet == arm2
    assert runtime.last_command_seq == arm2.command_seq


def test_direct_fake_transport_runs_ten_rows_then_refuses_arm11(
    tmp_path: Path,
) -> None:
    """Exercise the production mailbox protocol for the complete r006 batch."""

    template = _prepared(dict(DEFAULT_OVERLAY))
    mailbox = AtomicCommandMailbox((tmp_path / "command.json").absolute())
    runtime = BridgeMailboxRuntime(
        mailbox.path,
        completion_protocol="v3_direct_arm_v1",
    )
    args = SimpleNamespace()
    commands: list[HostCommand] = []

    current_packet: HostPacket | None = None
    for row_index in range(1, 11):
        prepared = _prepared_at(
            template,
            trial_id=row_index,
            candidate_token=100 + row_index,
            command_seq=row_index,
        )
        packet = HostPacket(
            1,
            row_index,
            HostCommand.ARM,
            100 + row_index,
            533,
            row_index,
        )
        mailbox.send_command(packet, prepared_trial=prepared)
        prior_state = (
            TpLoopState.READY_HOME
            if current_packet is None
            else TpLoopState.READY_NEAR
        )
        assert runtime.poll(
            args,
            _rtde(
                prior_state,
                current_packet,
                consumed_seq=0 if current_packet is None else current_packet.command_seq,
                reason=0 if current_packet is None else 1,
            ),
        )
        assert runtime.active is not None
        assert runtime.active.packet == packet
        commands.append(packet.command)
        current_packet = packet

    assert current_packet is not None
    assert runtime.last_command_seq == 10
    assert commands == [HostCommand.ARM] * 10
    assert HostCommand.ACK_BUNDLE not in commands

    prepared11 = _prepared_at(
        template,
        trial_id=11,
        candidate_token=111,
        command_seq=11,
    )
    arm11 = HostPacket(1, 11, HostCommand.ARM, 111, 533, 11)
    mailbox.send_command(arm11, prepared_trial=prepared11)
    with pytest.raises(MailboxError, match="fresh ARM is invalid"):
        runtime.poll(
            args,
            _rtde(
                TpLoopState.READY_HOME_CLOSED,
                current_packet,
                consumed_seq=current_packet.command_seq,
                reason=1,
            ),
        )
    assert runtime.last_command_seq == 10


def test_initial_live_batch_uses_fresh_campaign_local_history(
    tmp_path: Path,
) -> None:
    candidates = initial_candidates()
    assert len(candidates) == len({item.candidate_uid for item in candidates}) == 10
    for candidate, expected in zip(candidates, INITIAL_LOG2, strict=True):
        observed = (candidate.log2_p, candidate.log2_i, candidate.log2_damping)
        assert observed == pytest.approx(expected, abs=1e-12)
    campaign_root = tmp_path / "fresh-v3"
    candidate_plan = campaign_root / "control/candidate_plan.json"
    initialize_plan(
        candidate_plan,
        campaign_id="fresh-v3-plant-epoch",
        batch_size=10,
    )
    with patch.object(
        live.v3_cli,
        "_validate_candidates",
        wraps=live.v3_cli._validate_candidates,
    ) as validate:
        plan, overlays = live._ensure_initial_batch(
            campaign_root=campaign_root,
            campaign_id="fresh-v3-plant-epoch",
            launch_profile_path=(
                ROOT / "config/step5/step5d_autotune_v3_launch_profile.json"
            ),
        )
    assert plan.revision == 1
    assert len(plan.batches[0]) == 10
    assert overlays["candidate_count"] == 10
    ledger = validate.call_args.kwargs["attempt_ledger"]
    assert ledger.tuples == {}
    assert ledger.summary["attempt_records"] == 0
    assert "ledger_path" not in validate.call_args.kwargs
    source = Path(live.__file__).read_text(encoding="utf-8")
    assert "AdoptedCandidateHistory" not in source
    assert "legacy_campaign_root" not in source


def test_initial_control_batch_is_ten_unique_quarter_octave_steps_including_k() -> None:
    profile = load_launch_profile()
    overlays = initial_control_overlays(profile)
    assert len(overlays) == 10
    assert len({row["control_candidate_uid"] for row in overlays}) == 10
    baseline = dict(DEFAULT_OVERLAY)
    prior = ForceCandidate.from_log2(p=0.75, i=0.75, damping=0.25)
    baseline.update(
        {
            "force_p_gain": prior.force_p_gain,
            "force_i_gain": prior.force_i_gain,
            "force_damping": prior.force_damping,
            "orientation_ko": 0.4,
        }
    )
    baseline.pop("control_candidate_uid", None)
    for previous, current in zip((baseline, *overlays[:-1]), overlays, strict=True):
        assert is_control_candidate_step(previous, current)
    assert [row["orientation_ko"] for row in overlays] == pytest.approx(
        [row[3] for row in INITIAL_CONTROL_LOG2_K], abs=1e-12
    )


def test_v3_arm_boundary_applies_real_orientation_k_without_moving_sphere() -> None:
    profile = load_launch_profile()
    overlay = initial_control_overlays(profile)[0]
    binding = SimpleNamespace(
        trial_overlay=overlay,
        campaign_epoch=1,
        campaign_fingerprint="a" * 64,
    )
    args = SimpleNamespace()
    bridge = SimpleNamespace(STEP5D_V33_ORIENTATION_KO=0.4)
    _apply_v3_arm_runtime(
        bridge,
        args,
        binding,
        None,
        lambda *_args: None,
    )

    assert bridge.STEP5D_V33_ORIENTATION_KO == overlay["orientation_ko"]
    assert args.step5d_autotune_orientation_ko == overlay["orientation_ko"]
    assert args.step5d_autotune_force_p == overlay["force_p_gain"]
    assert args.step5d_autotune_control_candidate_uid == overlay["control_candidate_uid"]
    assert args.step5d_physical_prior_sha256 == (
        "c8019aee2c293746e1edb23097aeab1d7dfb1b8dee09df10ce568fb634f47c9f"
    )
    assert args.step5d_physical_prior_binding_valid is True
    assert args.step5d_moving_sphere_enabled is False
    assert args.step5d_physical_prior_identity_payload["approach_axis_b"] == [
        0.043955267,
        -0.020079909,
        -0.998831683,
    ]


def test_v3_capture_writer_is_async_compact_and_binds_real_candidate(tmp_path: Path) -> None:
    overlay = initial_control_overlays(load_launch_profile())[0]
    fields = (
        *sorted(_V3_RUNNER_CLOSURE_FIELDS),
        "rtde_feedback_age_s",
        "_step5d_stage25_echo_consumed",
        "ur_output_double_register_35",
        "large_unused_diagnostic",
    )
    rotator = V3AsyncBridgeTrialCsvRotator(tmp_path.absolute(), fields)
    binding = SimpleNamespace(
        trial_uid="a" * 64,
        backend_id="backend",
        campaign_epoch=1,
        trial_id=2,
        candidate_token=3,
        execution_profile_id=533,
        arm_command_seq=4,
        trial_overlay=overlay,
    )
    active = SimpleNamespace(binding=binding)

    def output(state: int, reason: int = 0) -> dict[str, int]:
        return {
            "output_int_register_24": 1,
            "output_int_register_25": 2,
            "output_int_register_26": state,
            "output_int_register_27": 3,
            "output_int_register_28": reason,
            "output_int_register_29": 533,
            "output_int_register_30": 4,
        }

    row = {name: 0 for name in fields}
    for index in range(24, 31):
        row[f"ur_output_int_register_{index}"] = output(20)[
            f"output_int_register_{index}"
        ]
    assert rotator.observe(row, active=active, rtde_output=output(20)) is True
    row["ur_output_int_register_26"] = 70
    row["ur_output_int_register_28"] = 1
    assert rotator.observe(row, active=active, rtde_output=output(70, 1)) is True
    rotator.close()

    capture = tmp_path / ("a" * 64) / "capture.csv"
    header = capture.read_text(encoding="utf-8").splitlines()[0].split(",")
    assert "large_unused_diagnostic" not in header
    assert "autotune_control_candidate_uid" in header
    assert "autotune_orientation_ko" in header
