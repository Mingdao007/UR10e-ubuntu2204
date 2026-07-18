from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import verify_step5d_autotune_v3_hil_authorization as gate  # noqa: E402


IDENTITY = {
    "contract_sha256": "a" * 64,
    "control_fingerprint": "b" * 64,
    "orchestration_fingerprint": "c" * 64,
}


@pytest.fixture(autouse=True)
def _current_readiness():
    with patch.object(
        gate.execution_readiness,
        "verify",
        return_value={
            "state": gate.execution_readiness.READY_FOR_HIL,
            "ready_to_execute": False,
            "identity": dict(IDENTITY),
        },
    ):
        yield


def test_canonical_entrypoint_builds_process_bound_hold_permit() -> None:
    permit = gate.build_launch_permit(
        root=ROOT,
        parent_pid=1234,
        launch_id="1" * 32,
    )
    report = gate.verify_launch_permit(
        permit,
        expected_parent_pid=1234,
        root=ROOT,
    )
    assert report["scope"] == "hil_full_bridge_hold"
    assert report["issued_by"] == "canonical_v3_hil_entrypoint"
    assert report["hold_required"] is True
    assert "arm" in report["forbidden_actions"]


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("parent_pid", 9999, "parent_pid"),
        ("candidate_stage_id", "step5d_strict_rnn_autotune_v1", "candidate_stage_id"),
        ("scope", "live_motion", "scope"),
        ("issued_by", "user_current_turn_explicit", "issued_by"),
        ("hold_required", False, "hold_required"),
    ],
)
def test_internal_permit_mutations_fail_closed(field: str, value, message: str) -> None:
    permit = gate.build_launch_permit(
        root=ROOT,
        parent_pid=1234,
        launch_id="2" * 32,
    )
    permit[field] = value
    with pytest.raises(gate.LaunchPermitError, match=message):
        gate.verify_launch_permit(
            permit,
            expected_parent_pid=1234,
            root=ROOT,
        )


def test_internal_permit_identity_and_action_lists_are_exact() -> None:
    permit = gate.build_launch_permit(
        root=ROOT,
        parent_pid=1234,
        launch_id="3" * 32,
    )
    permit["identity"]["control_fingerprint"] = "f" * 64
    with pytest.raises(gate.LaunchPermitError, match="identity"):
        gate.verify_launch_permit(
            permit,
            expected_parent_pid=1234,
            root=ROOT,
        )

    permit = gate.build_launch_permit(
        root=ROOT,
        parent_pid=1234,
        launch_id="4" * 32,
    )
    permit["allowed_actions"] = [*permit["allowed_actions"], "arm"]
    with pytest.raises(gate.LaunchPermitError, match="allowed_actions"):
        gate.verify_launch_permit(
            permit,
            expected_parent_pid=1234,
            root=ROOT,
        )


def test_cli_has_no_user_authorization_or_thread_arguments() -> None:
    parser = gate.parse_args([])
    assert not hasattr(parser, "authorization")
    assert not hasattr(parser, "expected_thread_id")
