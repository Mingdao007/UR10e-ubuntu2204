from __future__ import annotations

import ast
import hashlib
import json
import os
import sys
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import run_step5d_autotune_v3_bridge as wrapper  # noqa: E402
from run_step5d_autotune_campaign import closure_sample_from_bridge_row  # noqa: E402


def _ticket(path: Path, argv: list[str]) -> Path:
    encoded = json.dumps(argv, sort_keys=True, separators=(",", ":")).encode()
    path.write_text(
        json.dumps(
            {
                "schema": wrapper.TICKET_SCHEMA,
                "parent_pid": os.getppid(),
                "argv_sha256": hashlib.sha256(encoded).hexdigest(),
                "launch_id": "1" * 32,
                "scope": "live_continuous_campaign",
                "identity": {
                    "contract_sha256": "0" * 64,
                    "control_fingerprint": "a" * 64,
                    "orchestration_fingerprint": "d" * 64,
                },
                "launch_profile_fingerprint": "b" * 64,
                "trial_overlay_fingerprint": "c" * 64,
                "release_stage_id": "step5d_strict_rnn_autotune_v3",
                "control_profile_id": "step5d_strict_rnn_autotune_v1",
                "tp_program_id": "step5d_strict_rnn_autotune_v3",
                "campaign_binding": {
                    "campaign_id": "campaign-v3",
                    "campaign_epoch": 2,
                    "candidate_plan_revision": 1,
                    "candidate_plan_sha256": "e" * 64,
                    "trial_overlay_plan_sha256": "f" * 64,
                },
            }
        ),
        encoding="utf-8",
    )
    return path


def test_wrapper_requires_parent_and_exact_argv_ticket(tmp_path: Path) -> None:
    argv = ["--bridge-profile", "step5d_strict_rnn_autotune_v1"]
    ticket = _ticket(tmp_path / "ticket.json", argv)
    assert wrapper._strict_ticket(ticket, argv)["scope"] == "live_continuous_campaign"
    try:
        wrapper._strict_ticket(ticket, [*argv, "--duration-s", "1"])
    except wrapper.BridgeTicketError as exc:
        assert "argv binding" in str(exc)
    else:
        raise AssertionError("mutated argv was accepted")


def test_wrapper_refuses_direct_start_without_runtime_ticket(capsys) -> None:
    with patch.dict(os.environ, {}, clear=True):
        assert wrapper.main([]) == 24
    assert "RUNTIME_TICKET" in capsys.readouterr().err


def test_live_ticket_requires_exact_campaign_binding(tmp_path: Path) -> None:
    argv = ["--bridge-profile", "step5d_strict_rnn_autotune_v1"]
    ticket = _ticket(tmp_path / "ticket.json", argv)
    payload = json.loads(ticket.read_text(encoding="utf-8"))
    payload["campaign_binding"].pop("trial_overlay_plan_sha256")
    ticket.write_text(json.dumps(payload), encoding="utf-8")
    try:
        wrapper._strict_ticket(ticket, argv)
    except wrapper.BridgeTicketError as exc:
        assert "campaign binding" in str(exc)
    else:
        raise AssertionError("incomplete campaign binding was accepted")


def test_wrapper_source_has_no_campaign_runner_arm_or_motion_surface() -> None:
    source = Path(wrapper.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    calls = {
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert "Popen" not in calls
    assert "run" not in calls
    assert "start_step5d_autotune_runner" not in source
    assert "HostCommand.ARM" not in source
    assert "speedj(" not in source
    assert "movel(" not in source


def test_production_compact_schema_satisfies_wait_ack_closure_consumer() -> None:
    production_fields = [
        *wrapper._V3_COMPACT_EXACT_FIELDS,
        *(f"ur_actual_TCP_pose_{index}" for index in range(6)),
        *(f"ur_actual_TCP_speed_{index}" for index in range(6)),
        *(f"ur_actual_q_{index}" for index in range(6)),
        *(f"ur_actual_qd_{index}" for index in range(6)),
        *(f"ur_actual_qdd_{index}" for index in range(6)),
        *(f"ur_output_int_register_{index}" for index in range(24, 31)),
    ]
    compact = wrapper.compact_v3_fieldnames(production_fields)
    assert wrapper._V3_RUNNER_CLOSURE_FIELDS.issubset(compact)
    row = {name: "0" for name in compact}
    row["ur_output_int_register_26"] = "70"
    sample = closure_sample_from_bridge_row(row)
    assert sample["output_double_register_36"] == 0.0
    assert sample["output_double_register_37"] == 0.0
    assert sample["output_double_register_38"] == 0.0


def test_observed_wait_ack_schema_incident_is_exactly_closed() -> None:
    incident = json.loads(
        (ROOT / "tests/fixtures/v3_wait_ack_schema_incident.json").read_text(
            encoding="utf-8"
        )
    )
    observed = set(incident["observed_compact_output_double_register_fields"])
    required = set(incident["runner_required_output_double_register_fields"])
    assert sorted(required - observed) == incident["missing_fields"]
    assert required.issubset(wrapper._V3_RUNNER_CLOSURE_FIELDS)
    assert required.issubset(wrapper._V3_COMPACT_EXACT_FIELDS)
