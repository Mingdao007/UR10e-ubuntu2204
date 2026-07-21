from __future__ import annotations

import sys
import json
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
RUNTIME_SRC = ROOT.parents[1] / "src/ur10e_experiment_runtime"
sys.path.insert(0, str(RUNTIME_SRC))
sys.path.insert(0, str(ROOT / "tools"))

from step5d_autotune_batch_plan import (  # noqa: E402
    append_r008_batch,
    initialize_r008_plan,
    load_plan,
)
from step5d_autotune_contract import Evaluation, ForceCandidate, TrialDisposition  # noqa: E402
from step5d_autotune_optimizer import Observation, replicate_noise_variances  # noqa: E402
from step5d_autotune_journal import TpSnapshot  # noqa: E402
from step5d_autotune_state_machine import HostCommand, HostPacket  # noqa: E402
from step5d_r008_completion import CompletionError, CompletionJournal  # noqa: E402
import run_step5d_autotune_campaign as campaign_runner  # noqa: E402
from step5d_autotune_v3.runtime_profile import DEFAULT_OVERLAY  # noqa: E402
from step5d_autotune_r008_policy import (  # noqa: E402
    BASELINE,
    PROTOCOL,
    anchor_drifted,
    bo_gate,
    confirmation_batch,
    confirmation_success,
    initialization_batch,
    plateau_reached,
    recovery_batch,
)
from ur10e_experiment_runtime import BatchIdentity, BatchRow  # noqa: E402
from ur10e_experiment_runtime.stage_adapters import control_candidate_uid  # noqa: E402


def _evaluation(index: int, objective: float, *, eligible: bool = True) -> Evaluation:
    return Evaluation(
        trial_uid=f"{index:064x}",
        backend_id="backend",
        eligible=eligible,
        disposition=(TrialDisposition.OBJECTIVE if eligible else TrialDisposition.FAIL_CLOSED),
        objective_mae_n=(objective if eligible else None),
        force_bias_n=(0.0 if eligible else None),
        force_std_n=(0.1 if eligible else None),
        coverage_12_plus_minus_1_ratio=(1.0 if eligible else None),
        complete_bins=(550 if eligible else 0),
        safe_closure=True,
        structural_failures=(() if eligible else ("oracle_invalid",)),
    )


def _observation(index: int, candidate: ForceCandidate, objective: float) -> Observation:
    return Observation(candidate, _evaluation(index, objective), "profile", 1)


def test_normal_rate_0_1_is_the_persistent_canonical_default() -> None:
    campaign = json.loads((ROOT / "config/step5d_autotune_campaign_v1.json").read_text())
    contract = json.loads(
        (ROOT / "config/step5/step5d_autotune_v3_control_contract.json").read_text()
    )
    launch = json.loads(
        (ROOT / "config/step5/step5d_autotune_v3_launch_profile.json").read_text()
    )
    assert DEFAULT_OVERLAY["execution_profile_id"] == "nf100-slew050-a050"
    assert launch["trial_overlay_policy"]["execution_profile_id"]["allowed"] == [
        "nf100-slew050-a050"
    ]
    assert campaign["baseline"]["normal_max_rate_rad_s"] == 0.1
    assert campaign["baseline"]["execution_profile_integer_id"] == 633
    defaults = contract["effective_fields"]["safety_invariant"]
    assert defaults["bridge_normal_max_rate_rad_s"] == 0.1
    assert defaults["step4e_normal_max_rate_rad_s"] == 0.1
    assert defaults["step5d_autotune_normal_rate_rad_s"] == 0.1


def test_initialization_and_recovery_are_exact_five_row_occurrence_batches() -> None:
    first = initialization_batch(1)
    second = initialization_batch(2)
    recovery = recovery_batch(3)
    assert tuple(row.candidate for row in first[:3]) == (BASELINE,) * 3
    assert tuple(row.candidate for row in second[:3]) == (BASELINE,) * 3
    assert recovery[0].candidate == BASELINE
    assert all(len(batch) == 5 for batch in (first, second, recovery))
    assert len({row.occurrence_uid for row in first + second + recovery}) == 15
    assert len({row.candidate.candidate_uid for row in first}) == 3
    assert tuple(row.replicate_ordinal for row in first[:3]) == (1, 2, 3)


def test_pending_completion_reuses_packet_and_consumes_exactly_once(tmp_path: Path) -> None:
    journal = CompletionJournal(tmp_path / "pending_completion.json")
    packet = HostPacket(1, 15, HostCommand.COMPLETE_AT_HOME, 9, 633, 16, 3)
    assert journal.prepare(packet) == packet
    assert CompletionJournal(journal.path).prepare(packet) == packet
    with pytest.raises(CompletionError, match="differs"):
        journal.prepare(HostPacket(1, 15, HostCommand.COMPLETE_AT_HOME, 9, 633, 17, 3))
    snapshot = TpSnapshot(1, 15, "READY_HOME_CLOSED", 9, 1, 633, 16, 3)
    journal.mark_consumed(snapshot)
    journal.mark_consumed(snapshot)
    assert journal.load() == (packet, "consumed")


def test_user_stop_completion_reaches_state77_and_is_not_resent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    arm = HostPacket(1, 7, HostCommand.ARM, 9, 633, 7, 2)
    snapshot = TpSnapshot(1, 7, "READY_HOME_CLOSED", 9, 1, 633, 8, 2)

    class Mailbox:
        def __init__(self) -> None:
            self.packets: list[HostPacket] = []

        def send_command(self, packet: HostPacket, *, prepared_trial: object) -> None:
            self.packets.append(packet)

    class Follower:
        def rows(self, *, timeout_s: float):
            yield object()

        def note_predicate_reject(self) -> None:
            raise AssertionError("exact state77 must be accepted")

    mailbox = Mailbox()
    follower = Follower()
    monkeypatch.setattr(campaign_runner, "tp_snapshot_from_bridge_row", lambda row: snapshot)
    arguments = {
        "campaign_root": tmp_path,
        "arm": arm,
        "prepared_trial": object(),
        "mailbox": mailbox,
        "follower": follower,
        "timeout_s": 1.0,
        "event_path": tmp_path / "events.jsonl",
        "reason": "user_stop",
    }
    assert campaign_runner._complete_rolling_at_home(**arguments)
    assert mailbox.packets == [
        HostPacket(1, 7, HostCommand.COMPLETE_AT_HOME, 9, 633, 8, 2)
    ]
    assert campaign_runner._complete_rolling_at_home(**arguments)
    assert len(mailbox.packets) == 1
    assert CompletionJournal(
        tmp_path / "control/pending_completion.json"
    ).load() == (mailbox.packets[0], "consumed")


def test_r008_plan_accepts_control_repeats_but_rejects_occurrence_replay(tmp_path: Path) -> None:
    path = tmp_path / "candidate_plan.json"
    initialize_r008_plan(path, campaign_id="campaign")
    first = append_r008_batch(path, occurrences=initialization_batch(1), source="init-p")
    second = append_r008_batch(path, occurrences=initialization_batch(2), source="init-d")
    assert first.batch_size == 5
    assert len(second.batches) == 2
    assert second.batches[0][:3] == (BASELINE,) * 3
    assert second.occurrences[1][0].occurrence_uid != second.occurrences[0][0].occurrence_uid
    payload = dict(load_plan(path).payload)
    payload["batches"][1]["occurrences"][0]["occurrence_uid"] = payload["batches"][0]["occurrences"][0]["occurrence_uid"]
    path.write_text(__import__("json").dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="occurrence UID"):
        load_plan(path)


def test_batch_identity_separates_occurrence_transport_and_control_namespaces() -> None:
    planned = initialization_batch(1)
    rows = []
    for occurrence in planned:
        candidate = occurrence.candidate
        control = {
            "force_p_gain": candidate.force_p_gain,
            "force_i_gain": candidate.force_i_gain,
            "force_damping": candidate.force_damping,
            "orientation_ko": 0.4,
        }
        overlay = {
            **control,
            "control_candidate_uid": control_candidate_uid(control),
            "execution_profile_id": "nf010-slew010-a010",
            "step5d_preload_filtered_min_n": 2.0,
            "step5d_preload_filtered_max_n": 8.0,
            "step5d_preload_raw_min_n": 1.0,
            "step5d_preload_raw_max_n": 12.0,
            "step5d_preload_force_norm_max_n": 20.0,
            "step5d_preload_hold_s": 0.5,
            "step5d_preload_timeout_s": 5.0,
        }
        rows.append(
            BatchRow(
                occurrence.row_index,
                control,
                overlay,
                occurrence.occurrence_uid,
                occurrence.transport_candidate_uid,
                occurrence.selection_role,
                occurrence.replicate_ordinal,
            )
        )
    identity = BatchIdentity(
        campaign_uid="campaign",
        experiment_fingerprint="1" * 64,
        launch_fingerprint="2" * 64,
        adapter_fingerprint="3" * 64,
        physical_prior_fingerprint="4" * 64,
        safety_envelope_fingerprint="5" * 64,
        return_policy_fingerprint="6" * 64,
        controller_readback_fingerprint="7" * 64,
        authorization_ref_sha256="8" * 64,
        plant_epoch=1,
        rows=tuple(rows),
        protocol=PROTOCOL,
        logical_batch_sequence=1,
        plan_revision=1,
    )
    assert len(identity.rows) == 5
    assert len({row.occurrence_uid for row in identity.rows}) == 5
    assert identity.rows[0].control_candidate_uid == identity.rows[1].control_candidate_uid
    assert identity.rows[0].transport_candidate_uid != identity.rows[1].transport_candidate_uid


def test_bo_gate_noise_and_terminal_policies_are_strict() -> None:
    observations = [
        _observation(1, BASELINE, 0.20),
        _observation(2, BASELINE, 0.22),
        _observation(3, BASELINE, 0.18),
        _observation(4, ForceCandidate.from_log2(p=-0.25, damping=0, i=0), 0.17),
        _observation(5, ForceCandidate.from_log2(p=0.25, damping=0, i=0), 0.16),
        _observation(6, ForceCandidate.from_log2(p=0, damping=0.25, i=0), 0.15),
    ]
    assert bo_gate(observations)
    variances = replicate_noise_variances(observations)
    assert len(variances) == 6
    assert min(variances) >= 1e-4
    assert confirmation_success((0.099, 0.100, 0.08))
    assert not confirmation_success((0.10, 0.10, 0.099))
    assert anchor_drifted((1.21,), (1.0, 1.0, 1.0))
    assert not anchor_drifted((1.20,), (1.0, 1.0, 1.0))
    assert plateau_reached((0.019, 0.0), (0.009, 0.0))
    assert not plateau_reached((0.02, 0.0), (0.009, 0.0))


def test_confirmation_batch_uses_three_incumbent_occurrences_and_two_challengers() -> None:
    challengers = (
        ForceCandidate.from_log2(p=-0.25, damping=0, i=0),
        ForceCandidate.from_log2(p=0.25, damping=0, i=0),
    )
    batch = confirmation_batch(BASELINE, challengers, sequence=9)
    assert tuple(row.candidate for row in batch[:3]) == (BASELINE,) * 3
    assert batch[3].candidate != batch[4].candidate
    assert len({row.occurrence_uid for row in batch}) == 5
