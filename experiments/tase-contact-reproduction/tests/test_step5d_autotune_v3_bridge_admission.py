from __future__ import annotations

import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import check_step5d_autotune_v3_bridge_admission as admission_cli  # noqa: E402
from step5d_autotune_v3 import bridge_admission  # noqa: E402


EXPECTED_PROGRAM = (
    "/programs/andyl/kunwei/step5/"
    "step5d_strict_rnn_autotune_v3_r012.urp"
)


def _observation_fixture(
    tmp_path: Path,
    monkeypatch,
    *,
    program_state: str,
    loaded_program: str,
) -> tuple[dict[str, object], list[str]]:
    root = tmp_path / "experiment"
    root.mkdir()
    delivery_path = root / "runs/delivery-observation.json"
    delivery_path.parent.mkdir()
    delivery_path.write_text('{"fixture":true}\n', encoding="utf-8")
    release = SimpleNamespace(
        manifest_sha256="a" * 64,
        program_id="step5d_strict_rnn_autotune_v3_r012",
    )
    commands: list[str] = []

    monkeypatch.setattr(
        bridge_admission,
        "load_runtime_release",
        lambda _root: release,
    )
    monkeypatch.setattr(
        bridge_admission,
        "release_runtime_contract",
        lambda _root, _release: {
            "expected_loaded_program": EXPECTED_PROGRAM,
        },
    )
    monkeypatch.setattr(
        bridge_admission,
        "resolve_delivery_observation",
        lambda _root, **_kwargs: (
            delivery_path,
            {"transaction_id": "b" * 32},
        ),
    )
    monkeypatch.setattr(
        bridge_admission,
        "load_delivery_observation",
        lambda _root, _path, **_kwargs: {"transaction_id": "b" * 32},
    )
    monkeypatch.setattr(
        bridge_admission,
        "release_robot_host",
        lambda _root, _release: "robot",
    )

    def dashboard_reader(
        _host: str,
        requested: list[str],
        **_kwargs: object,
    ) -> dict[str, str]:
        commands.extend(requested)
        return {
            "programState": program_state,
            "get loaded program": f"Loaded program: {loaded_program}",
        }

    result = bridge_admission.observe_bridge_admission(
        root,
        dashboard_reader=dashboard_reader,
    )
    return result, commands


def test_wrong_program_returns_action_required_without_authority(
    tmp_path: Path,
    monkeypatch,
) -> None:
    result, commands = _observation_fixture(
        tmp_path,
        monkeypatch,
        program_state="STOPPED wrong.urp",
        loaded_program="/programs/wrong.urp",
    )

    assert result["state"] == "ACTION_REQUIRED"
    assert result["reason_code"] == "EXTERNAL_ACTION_REQUIRED"
    assert result["operator_action"] == (
        "LOAD_EXACT_PROGRAM_ON_TP_AND_LEAVE_STOPPED"
    )
    assert result["authority_acquired"] is False
    assert result["attempt_created"] is False
    assert commands == ["programState", "get loaded program"]


def test_exact_loaded_stopped_program_is_bench_ready(
    tmp_path: Path,
    monkeypatch,
) -> None:
    result, commands = _observation_fixture(
        tmp_path,
        monkeypatch,
        program_state="STOPPED step5d_strict_rnn_autotune_v3_r012.urp",
        loaded_program=EXPECTED_PROGRAM,
    )

    assert result["state"] == "BENCH_READY"
    assert result["reason_code"] == "PROGRAM_LOADED_STOPPED"
    assert result["operator_action"] is None
    assert result["authority_acquired"] is False
    assert result["attempt_created"] is False
    assert commands == ["programState", "get loaded program"]


def test_admission_expected_program_is_bound_to_release_contract(
    tmp_path: Path,
    monkeypatch,
) -> None:
    result, _commands = _observation_fixture(
        tmp_path,
        monkeypatch,
        program_state="STOPPED step5d_strict_rnn_autotune_v3_r012.urp",
        loaded_program=EXPECTED_PROGRAM,
    )
    result["expected_loaded_program"] = "/programs/forged.urp"
    release = SimpleNamespace(
        manifest_sha256="a" * 64,
        program_id="step5d_strict_rnn_autotune_v3_r012",
    )

    with pytest.raises(
        bridge_admission.BridgeAdmissionError,
        match="expected program differs",
    ):
        bridge_admission.validate_bridge_admission(
            tmp_path / "experiment",
            result,
            release=release,
            now_ns=int(result["observed_at_unix_ns"]),
        )


def test_cli_uses_exit_75_for_external_action_required(
    tmp_path: Path,
    monkeypatch,
) -> None:
    output = tmp_path / "admission.json"
    payload = {
        "schema": bridge_admission.SCHEMA,
        "state": "ACTION_REQUIRED",
        "ok": False,
        "reason_code": "EXTERNAL_ACTION_REQUIRED",
        "authority_acquired": False,
        "attempt_created": False,
    }
    monkeypatch.setattr(
        admission_cli,
        "observe_bridge_admission",
        lambda *_args, **_kwargs: payload,
    )
    monkeypatch.setattr(
        admission_cli,
        "admission_index_path",
        lambda *_args, **_kwargs: tmp_path / "indexed-admission.json",
    )

    assert admission_cli.main(["--root", str(tmp_path), "--output", str(output)]) == 75
    assert json.loads(output.read_text(encoding="utf-8")) == payload
