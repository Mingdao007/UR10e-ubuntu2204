from __future__ import annotations

import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import build_step5d_autotune_tp as v1  # noqa: E402
import build_step5d_autotune_tp_v3 as r009  # noqa: E402
import build_step5d_manual_tp_v1 as manual  # noqa: E402
from run_step5d_manual_live_campaign import _prepared  # noqa: E402
from step5d_autotune_live_driver import (  # noqa: E402
    AtomicCommandMailbox,
    MailboxError,
    decode_execution_profile_id,
)
from step5d_manual_queue import enqueue  # noqa: E402
from step5d_manual_runtime import prepare_next_intent  # noqa: E402
from step5d_profile_semantic_contract import (  # noqa: E402
    NETWORK_PROFILE_CODES,
    PROFILE_CODE_SPACE,
    ProfileSemanticContractError,
    assert_network_profile_semantics,
    assert_triplet_network_profile_semantics,
    tp_accepts_network_profile,
)


PROFILE = ROOT / "config/step5/step5d_autotune_v3_launch_profile.json"


def _host_network_codes() -> frozenset[int]:
    accepted: set[int] = set()
    for code in PROFILE_CODE_SPACE:
        try:
            decode_execution_profile_id(code, network_mode=True)
        except MailboxError:
            continue
        accepted.add(code)
    return frozenset(accepted)


@pytest.mark.parametrize(
    "rendered",
    (v1.render_script, r009.render_script, manual.render_script),
    ids=("base", "r009", "manual"),
)
def test_final_tp_renderers_equal_the_host_network_profile_lattice(rendered) -> None:
    host_codes = _host_network_codes()
    assert host_codes == NETWORK_PROFILE_CODES
    report = assert_network_profile_semantics(
        rendered(), expected_codes=host_codes
    )
    assert len(report.tp_accepted_codes) == 45


def test_triplet_script_and_urp_cached_contents_are_semantically_equal() -> None:
    stamp = "2026-07-21T1700HKT_PROFILE_SEMANTIC_TEST"
    script = v1.build_package_script(stamp)
    urp = v1.build_urp(script, v1.PROGRAM_NAME, v1.CONTROLLER_DIR)
    reports = assert_triplet_network_profile_semantics(script, urp)
    assert set(reports) == {"script", "urp_cached_contents"}


def test_exact_manual_i1e4_mailbox_handshake_is_tp_accepted(tmp_path: Path) -> None:
    queue = tmp_path / "manual_queue.json"
    enqueue(
        queue,
        campaign_id="manual-profile-contract",
        release_manifest_sha256="a" * 64,
        launch_profile_path=PROFILE,
        force_p=0.001,
        force_i=0.0001,
        force_damping=7.0,
        occurrence_nonce="1" * 32,
    )
    state = tmp_path / "manual_state.json"
    intent = prepare_next_intent(
        queue_path=queue,
        state_path=state,
        campaign_id="manual-profile-contract",
        release_manifest_sha256="a" * 64,
    )
    assert intent is not None
    packet, prepared = _prepared(intent.document())
    mailbox = AtomicCommandMailbox((tmp_path / "command.json").absolute())
    mailbox.send_command(packet, prepared_trial=prepared)
    command = mailbox.read_latest()
    assert command is not None
    assert command.handshake == {
        "campaign_epoch": 1,
        "trial_id": 1,
        "command": 1,
        "candidate_token": packet.candidate_token,
        "execution_profile_id": 633,
        "command_seq": 1,
        "batch_row_index": 1,
        "logical_batch_sequence": 1,
    }
    assert command.binding.candidate.force_i_gain == 0.0001
    assert tp_accepts_network_profile(
        manual.render_script(), command.handshake["execution_profile_id"]
    )


def test_checker_detects_a_removed_live_normal_digit() -> None:
    script = v1.render_script()
    assert script.count(" or normal_level == 6") == 1
    mutated = script.replace(" or normal_level == 6", "", 1)
    with pytest.raises(
        ProfileSemanticContractError,
        match=r"host_only=\[611, 612, 613, 621, 622, 623, 631, 632, 633\]",
    ):
        assert_network_profile_semantics(mutated)


@pytest.mark.parametrize("code", (411, 412, 413, 421, 422, 423, 431, 432, 433))
def test_offline_normal_profiles_remain_forbidden_on_the_network(code: int) -> None:
    with pytest.raises(MailboxError, match="offline_only"):
        decode_execution_profile_id(code, network_mode=True)
    assert not tp_accepts_network_profile(v1.render_script(), code)
