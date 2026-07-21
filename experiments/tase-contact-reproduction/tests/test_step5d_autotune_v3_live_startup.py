from __future__ import annotations

import json
import inspect
import os
import sys
from types import SimpleNamespace
from pathlib import Path
from typing import Any

import pytest
import subprocess


ROOT = Path(__file__).resolve().parents[1]
TESTS = ROOT / "tests"
sys.path.insert(0, str(TESTS))
sys.path.insert(0, str(ROOT / "tools"))

if (
    os.environ.get("STEP5D_V3_HERMETIC_PARSER_CI") == "1"
    and "kunwei_rtde_bridge" not in sys.modules
):
    from step5d_v3_parser_ci_stubs import install as install_parser_ci_stubs

    install_parser_ci_stubs()

import run_step5d_autotune_v3_live as live  # noqa: E402
import preflight_step5d_autotune_v3 as preflight  # noqa: E402


def _row(*, runtime_state: int, state: int = 10) -> dict[str, str]:
    row = {
        "ur_output_int_register_26": str(state),
        "ur_runtime_state": str(runtime_state),
        "ur_safety_mode": "1",
    }
    for index in (24, 25, 27, 28, 29, 30):
        row[f"ur_output_int_register_{index}"] = "0"
    return row


def test_postplay_runtime_requires_playing_normal() -> None:
    playing = _row(runtime_state=2)

    assert live._runtime_playing_normal(playing) is True


def test_preplay_does_not_wait_for_stale_stopped_tp_output_registers() -> None:
    source = (ROOT / "tools/run_step5d_autotune_v3_live.py").read_text(
        encoding="utf-8"
    )

    assert "_ready_home_zero_identity" not in source
    assert "pre-Play READY_HOME" not in source
    assert "stationary zero-identity READY_HOME" not in source


def test_live_consumer_accepts_the_complete_production_preflight_schema(
    tmp_path: Path,
) -> None:
    identity = {
        "tick_semantics_fingerprint": "a" * 64,
        "timing_harness_fingerprint": "b" * 64,
        "runtime_environment_fingerprint": "c" * 64,
        "deployment_fingerprint": "d" * 64,
        "orchestration_fingerprint": "e" * 64,
        "release_basis_fingerprint": "f" * 64,
    }
    payload = {
        "schema": preflight.SCHEMA,
        "ok": True,
        "fresh": True,
        "candidate_stage_id": live.RELEASE_STAGE_ID,
        "control_profile_id": live.CONTROL_PROFILE_ID,
        "tp_program_id": live.TP_PROGRAM_ID,
        "identity": identity,
        "predicates": {
            name: {"ok": True} for name in preflight.PREDICATE_NAMES
        },
    }
    path = tmp_path / "live_preflight.json"
    path.write_text(json.dumps(payload), encoding="utf-8")

    observed = live._validate_preflight(path, identity)

    assert observed == payload
    assert "prealign_start_clearance" in observed["predicates"]

    del payload["predicates"]["prealign_start_clearance"]
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(live.LiveLaunchError, match="predicates are incomplete"):
        live._validate_preflight(path, identity)


def test_operator_play_signal_precedes_runner_recovery_from_fresh_tp_state() -> None:
    source = inspect.getsource(live.run)
    runner_ready = source.index(
        '_wait_file(runner_ready, runner, args.ready_timeout_s, "campaign runner")'
    )
    bridge_ready = source.index(
        '_wait_file(bridge_run / "bridge_ready.json", bridge, args.ready_timeout_s, "bridge")'
    )
    no_arm_ready = source.index('print("V3_BRIDGE_READY_NO_ARM"', bridge_ready)
    campaign_ready = source.index('print("V3_CAMPAIGN_READY_FOR_TP_PLAY"', no_arm_ready)
    play_signal = source.index('print("READY_FOR_ONE_PLAY_TO_MOVE"')
    play_observed = source.index("if _runtime_playing_normal", play_signal)
    runner_start = source.index("runner = subprocess.Popen(", play_observed)

    assert bridge_ready < no_arm_ready < campaign_ready < play_signal
    assert play_signal < play_observed < runner_start < runner_ready
    assert source.count("READY_FOR_ONE_PLAY_TO_MOVE") == 1
    assert 'READY_FOR_TP_PLAY_V3"' not in source
    assert "campaign_authorization.json" not in source
    assert '"--authorization-file"' not in source
    assert '"--campaign-binding"' in source
    assert '"--campaign-arming-context"' not in source
    assert "legacy_campaign_root" not in source
    assert "V3_BATCH_10_COMPLETE_FINAL_HOME_CONFIRMED" in source


def test_canonical_shell_bridge_route_cannot_fall_back_to_v1() -> None:
    source = (ROOT / "scripts/step5d-autotune-v3.sh").read_text(encoding="utf-8")

    assert '"${1:-}" == "bridge"' in source
    assert "--bridge-start-context" in source
    assert "--campaign-arming-context" not in source
    assert "run_step5d_autotune_v3_live.py" in source
    assert "step5d-autotune-live.sh" not in source
    assert "bridge-line-operator.sh" not in source


def test_internal_live_worker_refuses_direct_execution(tmp_path: Path) -> None:
    environment = dict(os.environ)
    environment.pop(live.CANONICAL_LAUNCH_ENV, None)
    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "tools/run_step5d_autotune_v3_live.py"),
            "--output-root",
            str(tmp_path / "output"),
            "--preflight",
            str(tmp_path / "preflight.json"),
            "--bridge-start-context",
            str(tmp_path / "bridge-start.json"),
        ],
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert "not a public entrypoint" in result.stdout
    assert "step5d-autotune-v3.sh" in result.stdout


def test_first_campaign_home_is_loaded_only_after_arm_dispatch() -> None:
    source = (ROOT / "tools/run_step5d_autotune_campaign.py").read_text(
        encoding="utf-8"
    )
    loop = source.index("while supervisor.phase is CampaignPhase.HOME")
    dispatch = source.index(
        "coordinator.dispatch(arm, prepared_trial=prepared, sink=mailbox)", loop
    )
    wait_home = source.index("_wait_for_campaign_home_reference(home_path)", dispatch)

    assert dispatch < wait_home


def test_production_chain_generates_home_without_test_fixture(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    identity = {
        "tick_semantics_fingerprint": "a" * 64,
        "timing_harness_fingerprint": "b" * 64,
        "runtime_environment_fingerprint": "c" * 64,
        "deployment_fingerprint": "d" * 64,
        "orchestration_fingerprint": "e" * 64,
        "release_basis_fingerprint": "f" * 64,
    }
    context_path = tmp_path / "bridge-start-context.json"
    context_path.write_text("{}\n", encoding="utf-8")
    preflight_path = tmp_path / "preflight.json"
    preflight_path.write_text("{}\n", encoding="utf-8")
    output_root = tmp_path / "output"
    campaign_root = tmp_path / "campaign"
    fake_bridge = ROOT / "tests/step5d_v3_production_chain_fake_bridge.py"

    monkeypatch.setattr(
        live,
        "require_bridge_start",
        lambda *_args, **_kwargs: (
            {"bridge_start_ready": True},
            SimpleNamespace(identity=identity),
        ),
    )
    monkeypatch.setattr(
        live,
        "check_effective_config",
        lambda **_kwargs: {"effective_config": {"robot_host": "fake-robot"}},
    )
    monkeypatch.setattr(
        live,
        "_validate_preflight",
        lambda *_args, **_kwargs: {"controller_identity_sha256": "1" * 64},
    )
    monkeypatch.setattr(
        live,
        "dashboard_exchange",
        lambda *_args, **_kwargs: {"programState": "STOPPED fake.urp"},
    )
    monkeypatch.setattr(live, "WRAPPER", fake_bridge)
    monkeypatch.setattr(
        live,
        "build_bridge_argv",
        lambda runtime_root, **_kwargs: [
            sys.executable,
            str(fake_bridge),
            "--output-dir",
            str(runtime_root / "bridge"),
            "--mailbox",
            str(runtime_root / "command.json"),
        ],
    )
    args = SimpleNamespace(
        output_root=output_root,
        preflight=preflight_path,
        campaign_root=campaign_root,
        bridge_start_context=context_path,
        launch_profile=ROOT / "config/step5/step5d_autotune_v3_launch_profile.json",
        ready_timeout_s=5.0,
        play_timeout_s=5.0,
    )

    with pytest.raises(live.LiveLaunchError, match="bridge exited"):
        live.run(args)

    bridge_run = output_root / "runtime/bridge"
    home_path = bridge_run / "campaign_home_reference.json"
    runner_ready = bridge_run / "runtime/campaign_runner_ready.json"
    assert home_path.is_file()
    assert runner_ready.is_file()
    assert not (tmp_path / "fixture-campaign-home-reference.json").exists()
    home = json.loads(home_path.read_text(encoding="utf-8"))
    assert home["schema"] == "step5d.autotune.campaign-home-reference/v1"
    assert home["ready_handshake"]["state"] == 10
    assert home["ready_handshake"]["campaign_epoch_echo"] == 0
    assert home["ready_handshake"]["trial_id_echo"] == 0
    assert "FileNotFoundError" not in (output_root / "campaign_runner.log").read_text(
        encoding="utf-8"
    )
    runner_log = (output_root / "campaign_runner.log").read_text(encoding="utf-8")
    assert "differs from the pre-ARM READY_HOME pose" not in runner_log
    rows = (bridge_run / "bridge_rtde_500hz.csv").read_text(encoding="utf-8")
    assert ",24.0\n" in rows
    assert ",24.2\n" in rows


def test_fault_after_play_has_one_immediate_operator_action(monkeypatch, capsys) -> None:
    monkeypatch.setattr(
        live,
        "dashboard_exchange",
        lambda *_args, **_kwargs: {"programState": "PLAYING program.urp"},
    )

    assert live._announce_stop_if_playing("robot") is True
    assert capsys.readouterr().out.strip() == "ACTION_REQUIRED_PRESS_TP_STOP_NOW"


class _Clock:
    def __init__(self) -> None:
        self.value = 0.0

    def monotonic(self) -> float:
        return self.value

    def sleep(self, duration: float) -> None:
        self.value += duration


def test_cleanup_never_sends_dashboard_stop() -> None:
    replies: list[dict[str, Any]] = [
        {"programState": "PLAYING step5d_strict_rnn_autotune_v3.urp"},
        {"programState": "STOPPED step5d_strict_rnn_autotune_v3.urp"},
    ]
    commands: list[list[str]] = []

    def exchange(_host, requested, **_kwargs):
        commands.append(requested)
        return replies.pop(0)

    result = live._stop_v3_program(
        "robot", exchange=exchange
    )
    assert result["ok"] is True
    assert result["method"] == "observed_stopped_after_operator_stop"
    assert commands == [["programState"], ["programState"]]
    assert result["stop_request"] is None


def test_operator_tp_stop_is_observed_read_only() -> None:
    replies: list[dict[str, Any]] = [
        {"programState": "PLAYING step5d_strict_rnn_autotune_v3.urp"},
        {"programState": "STOPPED step5d_strict_rnn_autotune_v3.urp"},
    ]
    clock = _Clock()
    result = live._stop_v3_program(
        "robot",
        exchange=lambda *_args, **_kwargs: replies.pop(0),
        monotonic=clock.monotonic,
        sleep=clock.sleep,
    )
    assert result["ok"] is True
    assert result["method"] == "observed_stopped_after_operator_stop"


def test_persistent_playing_requires_one_explicit_tp_stop_action() -> None:
    clock = _Clock()

    def exchange(_host, commands, **_kwargs):
        assert commands == ["programState"]
        return {"programState": "PLAYING step5d_strict_rnn_autotune_v3.urp"}

    result = live._stop_v3_program(
        "robot",
        timeout_s=0.3,
        poll_interval_s=0.1,
        exchange=exchange,
        monotonic=clock.monotonic,
        sleep=clock.sleep,
    )
    assert result["ok"] is False
    assert result["required_operator_action"] == "PRESS_TP_STOP"
    assert result["stop_request"] is None
