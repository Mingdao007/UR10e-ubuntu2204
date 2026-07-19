from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

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
)
from step5d_autotune_state_machine import HostCommand, HostPacket  # noqa: E402
from step5d_autotune_v3.runtime_profile import (  # noqa: E402
    DEFAULT_OVERLAY,
    is_control_candidate_step,
    load_launch_profile,
)
from step5d_autotune_v3.state import load_attempt_ledger  # noqa: E402
from run_step5d_autotune_v3_bridge import (  # noqa: E402
    V3AsyncBridgeTrialCsvRotator,
    _V3_RUNNER_CLOSURE_FIELDS,
    _apply_v3_arm_runtime,
)
from run_step5d_autotune_v3_live import (  # noqa: E402
    INITIAL_CONTROL_LOG2_K,
    INITIAL_LOG2,
    LiveLaunchError,
    _validate_initial_candidate_path,
    initial_candidates,
    initial_control_overlays,
)
from run_step5d_autotune_campaign import AdoptedCandidateHistory  # noqa: E402


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


def test_initial_live_batch_is_reachable_from_observed_durable_history_before_play() -> None:
    candidates = initial_candidates()
    assert len(candidates) == len({item.candidate_uid for item in candidates}) == 10
    for candidate, expected in zip(candidates, INITIAL_LOG2, strict=True):
        observed = (candidate.log2_p, candidate.log2_i, candidate.log2_damping)
        assert observed == pytest.approx(expected, abs=1e-12)
    ledger = load_attempt_ledger(
        ROOT / "config/step5/step5d_autotune_v3_attempt_ledger.json"
    )
    assert all(
        ledger.attempted_group(candidate.payload(), "nf050-slew050-a050") is None
        for candidate in candidates
    )
    fixture = json.loads(
        (ROOT / "tests/fixtures/v3_post_play_pre_arm_lattice_incident.json").read_text(
            encoding="utf-8"
        )
    )
    executed = tuple(
        ForceCandidate.from_log2(p=p, i=i, damping=damping)
        for p, i, damping in fixture["executed_log2_candidates"]
    )
    history = AdoptedCandidateHistory(
        campaign_id="step5d-native-15",
        campaign_epoch=fixture["provenance"]["legacy_campaign_epoch"],
        profile_id=fixture["profile_id"],
        plant_epoch=fixture["plant_epoch"],
        executed_candidates=executed,
        physically_attempted_candidate_uids=frozenset(
            candidate.candidate_uid for candidate in executed
        ),
        fingerprint="a" * 64,
    )
    rejected = ForceCandidate.from_log2(
        p=fixture["rejected_first_log2_candidate"][0],
        i=fixture["rejected_first_log2_candidate"][1],
        damping=fixture["rejected_first_log2_candidate"][2],
    )
    with pytest.raises(LiveLaunchError, match="candidate 1 is not reachable"):
        _validate_initial_candidate_path((rejected,), adopted_history=history)
    validated = _validate_initial_candidate_path(candidates, adopted_history=history)
    assert validated["executed_anchor_count"] == 11
    assert validated["planned_candidate_count"] == 10
    assert tuple(INITIAL_LOG2[0]) == tuple(fixture["expected_first_log2_candidate"])


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


def test_v3_arm_boundary_applies_real_orientation_k() -> None:
    profile = load_launch_profile()
    overlay = initial_control_overlays(profile)[0]
    binding = SimpleNamespace(trial_overlay=overlay)
    args = SimpleNamespace()
    bridge = SimpleNamespace(STEP5D_V33_ORIENTATION_KO=0.4)

    _apply_v3_arm_runtime(bridge, args, binding, lambda *_args: None)

    assert bridge.STEP5D_V33_ORIENTATION_KO == overlay["orientation_ko"]
    assert args.step5d_autotune_orientation_ko == overlay["orientation_ko"]
    assert args.step5d_autotune_force_p == overlay["force_p_gain"]
    assert args.step5d_autotune_control_candidate_uid == overlay["control_candidate_uid"]
    assert args.step5d_physical_prior_sha256 == (
        "c8019aee2c293746e1edb23097aeab1d7dfb1b8dee09df10ce568fb634f47c9f"
    )
    assert args.step5d_physical_prior_binding_valid is True
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
