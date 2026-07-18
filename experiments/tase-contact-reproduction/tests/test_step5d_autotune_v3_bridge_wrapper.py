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


def _ticket(path: Path, argv: list[str]) -> Path:
    encoded = json.dumps(argv, sort_keys=True, separators=(",", ":")).encode()
    path.write_text(
        json.dumps(
            {
                "schema": wrapper.TICKET_SCHEMA,
                "parent_pid": os.getppid(),
                "argv_sha256": hashlib.sha256(encoded).hexdigest(),
                "authorization_id": "019f71d6-aaaa-7bbb-8ccc-e951de6daa96",
                "authorization_scope": "hil_full_bridge_hold",
                "identity": {"control_fingerprint": "a" * 64},
                "launch_profile_fingerprint": "b" * 64,
                "trial_overlay_fingerprint": "c" * 64,
                "release_stage_id": "step5d_strict_rnn_autotune_v3",
                "control_profile_id": "step5d_strict_rnn_autotune_v1",
                "tp_program_id": "step5d_strict_rnn_autotune_v3",
            }
        ),
        encoding="utf-8",
    )
    return path


def test_wrapper_requires_parent_and_exact_argv_ticket(tmp_path: Path) -> None:
    argv = ["--bridge-profile", "step5d_strict_rnn_autotune_v1"]
    ticket = _ticket(tmp_path / "ticket.json", argv)
    assert wrapper._strict_ticket(ticket, argv)["authorization_scope"] == (
        "hil_full_bridge_hold"
    )
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
