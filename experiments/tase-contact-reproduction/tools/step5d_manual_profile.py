"""Exact launch-profile binding inherited by the Manual V2 release."""

from __future__ import annotations

from pathlib import Path

from step5d_autotune_v3.profile import load_contract
from step5d_autotune_v3.runtime_profile import LaunchProfile, load_launch_profile


PARENT_PROFILE_TP_PROGRAM = "step5d_strict_rnn_autotune_v3_r009"
DEFAULT_LAUNCH_PROFILE = (
    Path(__file__).resolve().parents[1] / "config/step5d/manual/launch_profile.json"
)
CONTROL_CONTRACT = (
    Path(__file__).resolve().parents[1]
    / "config/step5/step5d_autotune_v3_control_contract.json"
)


def load_manual_launch_profile(
    path: Path = DEFAULT_LAUNCH_PROFILE,
) -> LaunchProfile:
    return load_launch_profile(
        path,
        contract=load_contract(CONTROL_CONTRACT),
        expected_tp_program_id=PARENT_PROFILE_TP_PROGRAM,
    )


__all__ = [
    "DEFAULT_LAUNCH_PROFILE",
    "PARENT_PROFILE_TP_PROGRAM",
    "load_manual_launch_profile",
]
