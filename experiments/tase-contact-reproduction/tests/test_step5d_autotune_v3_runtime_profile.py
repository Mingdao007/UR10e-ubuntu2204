from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
if os.environ.get("STEP5D_V3_HERMETIC_PARSER_CI") == "1":
    sys.path.insert(0, str(ROOT / "tests"))
    from step5d_v3_parser_ci_stubs import install as install_parser_ci_stubs

    install_parser_ci_stubs()
sys.path.insert(0, str(ROOT / "tools"))

from step5d_autotune_live_driver import AtomicCommandMailbox  # noqa: E402
from step5d_autotune_v3.launcher import build_bridge_argv  # noqa: E402
from step5d_autotune_v3.profile import ContractViolation  # noqa: E402
from ur10e_experiment_runtime import ControlCandidateUid  # noqa: E402
from step5d_autotune_v3.runtime_profile import (  # noqa: E402
    DEFAULT_OVERLAY,
    OVERLAY_FIELDS,
    IdentityCachedMailbox,
    apply_profile_to_argv,
    comparison_profile_fingerprint,
    launch_mutable_flags,
    load_launch_profile,
    normalize_trial_overlay,
    overlay_fingerprint,
)

PROFILE_PATH = ROOT / "config/step5/step5d_autotune_v3_launch_profile.json"
PROGRAM = json.loads(PROFILE_PATH.read_text(encoding="utf-8"))["tp_program_id"]


def test_default_profile_exposes_broad_launch_surface_and_exact_trial_overlay() -> None:
    profile = load_launch_profile(expected_tp_program_id=PROGRAM)
    contract = json.loads(
        (ROOT / "config/step5/step5d_autotune_v3_control_contract.json").read_text()
    )
    assert len(launch_mutable_flags(contract)) == 39
    overlay = normalize_trial_overlay(DEFAULT_OVERLAY, profile=profile)
    assert tuple(overlay) == OVERLAY_FIELDS
    assert len(overlay) == 15
    assert overlay["normal_filter_tau_s"] == 0.35
    assert profile.document["control_profile_id"] == "step5d_strict_rnn_autotune_v1"
    assert profile.document["tp_program_id"] == PROGRAM
    assert profile.hard_tube_enabled is False
    assert profile.document["hard_tube_policy"] == {"enabled": False}


def test_default_tau_preserves_v2_identity_and_nondefault_tau_uses_v3() -> None:
    legacy_material = {
        "force_p_gain": 0.001,
        "force_i_gain": 0.00001,
        "force_damping": 7.0,
        "orientation_ko": 0.4,
    }
    legacy = ControlCandidateUid.from_overlay(legacy_material)
    default_tau = ControlCandidateUid.from_overlay(
        {**legacy_material, "normal_filter_tau_s": 0.35}
    )
    tuned_tau = ControlCandidateUid.from_overlay(
        {**legacy_material, "normal_filter_tau_s": 0.7}
    )
    assert default_tau == legacy
    assert default_tau.startswith("control:v2:")
    assert tuned_tau.startswith("control:v3:")
    assert tuned_tau != default_tau
    assert ControlCandidateUid.parse(default_tau) == default_tau
    assert ControlCandidateUid.parse(tuned_tau) == tuned_tau


def test_overlay_applies_atomically_to_one_argv_snapshot() -> None:
    profile = load_launch_profile(expected_tp_program_id=PROGRAM)
    overlay = dict(DEFAULT_OVERLAY)
    overlay["force_i_gain"] = 0.00002
    overlay.pop("control_candidate_uid")
    overlay["execution_profile_id"] = "nf100-slew050-a050"
    overlay["step5d_preload_hold_s"] = 0.2
    overlay["normal_filter_tau_s"] = 0.7
    argv = apply_profile_to_argv(
        build_bridge_argv(Path("/tmp/step5d-v3-overlay")),
        profile=profile,
        overlay=overlay,
    )
    values = {argv[index]: argv[index + 1] for index in range(2, len(argv) - 1) if argv[index].startswith("--")}
    assert values["--step5d-autotune-force-i"] == "2e-05"
    assert values["--step5d-autotune-normal-rate-rad-s"] == "0.1"
    assert values["--step5d-autotune-host-slew-rad-s2"] == "0.5"
    assert values["--step5d-preload-hold-s"] == "0.2"
    assert values["--bridge-normal-filter-tau-s"] == "0.7"
    assert overlay_fingerprint(profile, overlay) != overlay_fingerprint(profile, DEFAULT_OVERLAY)
    assert comparison_profile_fingerprint(profile, overlay) != comparison_profile_fingerprint(profile, DEFAULT_OVERLAY)


@pytest.mark.parametrize(
    "mutation",
    [
        {"unknown": 1},
        {"step5d_preload_filtered_min_n": 20.0},
        {"step5d_preload_force_norm_max_n": 10.0},
        {"execution_profile_id": "nf020-slew010-a010"},
        {"execution_profile_id": "nf030-offline"},
    ],
)
def test_overlay_unknown_out_of_envelope_and_offline_profile_fail_closed(mutation: dict) -> None:
    profile = load_launch_profile(expected_tp_program_id=PROGRAM)
    overlay = dict(DEFAULT_OVERLAY)
    overlay.update(mutation)
    with pytest.raises(ContractViolation):
        normalize_trial_overlay(overlay, profile=profile)


def test_launch_profile_rejects_contract_bound_and_over_ceiling_override(tmp_path: Path) -> None:
    payload = dict(
        load_launch_profile(expected_tp_program_id=PROGRAM).document
    )
    contract = json.loads(
        (ROOT / "config/step5/step5d_autotune_v3_control_contract.json").read_text()
    )
    payload["launch_overrides"] = {"--bridge-profile": "future"}
    path = tmp_path / "profile.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ContractViolation, match="non-launch-mutable"):
        load_launch_profile(
            path, contract=contract, expected_tp_program_id=PROGRAM
        )
    payload["launch_overrides"] = {"--max-normal-force-n": 61}
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ContractViolation, match="hard ceiling"):
        load_launch_profile(
            path, contract=contract, expected_tp_program_id=PROGRAM
        )


@pytest.mark.parametrize(
    "policy",
    [
        None,
        {},
        {"enabled": 0},
        {"enabled": False, "radius_m": 0.03},
    ],
)
def test_launch_profile_rejects_non_boolean_or_ambiguous_hard_tube_policy(
    tmp_path: Path,
    policy: object,
) -> None:
    profile = load_launch_profile(expected_tp_program_id=PROGRAM)
    payload = dict(profile.document)
    payload["hard_tube_policy"] = policy
    path = tmp_path / "profile.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    contract = json.loads(
        (ROOT / "config/step5/step5d_autotune_v3_control_contract.json").read_text()
    )
    with pytest.raises(ContractViolation, match="hard-tube launch policy"):
        load_launch_profile(
            path,
            contract=contract,
            expected_tp_program_id=PROGRAM,
        )


def test_identity_cached_mailbox_skips_decode_until_atomic_identity_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = (tmp_path / "mailbox.json").absolute()
    path.write_text("{}", encoding="ascii")
    delegate = AtomicCommandMailbox(path, network_mode=True)
    calls = 0
    sentinel_one = object()
    sentinel_two = object()

    def decode():
        nonlocal calls
        calls += 1
        return sentinel_one if calls == 1 else sentinel_two

    monkeypatch.setattr(delegate, "read_latest", decode)
    cached = IdentityCachedMailbox(delegate)
    assert cached.read_latest() is sentinel_one
    assert cached.read_latest() is sentinel_one
    replacement = tmp_path / "replacement.json"
    replacement.write_text('{"v":2}', encoding="ascii")
    os.replace(replacement, path)
    assert cached.read_latest() is sentinel_two
    assert calls == 2
