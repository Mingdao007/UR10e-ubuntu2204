#!/usr/bin/env python3
"""Contract tests for the production Step5d autotune campaign launcher."""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from run_step5d_autotune_campaign import (  # noqa: E402
    closure_sample_from_bridge_row,
    ensure_mailbox_parent,
    tp_snapshot_from_bridge_row,
)


def bridge_row() -> dict[str, str]:
    row = {
        "ur_timestamp": "10.5",
        "ur_safety_mode": "1",
        "ur_output_int_register_24": "1",
        "ur_output_int_register_25": "2",
        "ur_output_int_register_26": "70",
        "ur_output_int_register_27": "3",
        "ur_output_int_register_28": "1",
        "ur_output_int_register_29": "111",
        "ur_output_int_register_30": "4",
        "ur_output_double_register_36": "0.001",
        "ur_output_double_register_37": "0.002",
        "ur_output_double_register_38": "0.003",
    }
    for name in ("actual_TCP_pose", "actual_TCP_speed", "actual_q", "actual_qd"):
        for index in range(6):
            row[f"ur_{name}_{index}"] = str(index / 1000.0)
    return row


def test_bridge_row_maps_to_exact_tp_snapshot() -> None:
    snapshot = tp_snapshot_from_bridge_row(bridge_row())
    assert snapshot.state == "WAIT_ACK"
    assert snapshot.campaign_epoch_echo == 1
    assert snapshot.trial_id_echo == 2
    assert snapshot.candidate_token_echo == 3
    assert snapshot.execution_profile_integer_id_echo == 111
    assert snapshot.consumed_command_seq == 4


def test_bridge_row_maps_to_safe_closure_input_shape() -> None:
    sample = closure_sample_from_bridge_row(bridge_row())
    assert sample["safety_mode"] == 1
    assert sample["actual_TCP_pose"] == [index / 1000.0 for index in range(6)]
    assert sample["output_int_register_26"] == 70
    assert sample["output_double_register_38"] == 0.003


def test_campaign_creates_its_own_runtime_mailbox_directory() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        bridge_run = Path(tmp) / "bridge"
        bridge_run.mkdir()
        mailbox = bridge_run / "runtime" / "command.json"

        ensure_mailbox_parent(mailbox, bridge_run)

        assert mailbox.parent.is_dir()
        with pytest.raises(RuntimeError, match="selected bridge run"):
            ensure_mailbox_parent(Path(tmp) / "other" / "command.json", bridge_run)
