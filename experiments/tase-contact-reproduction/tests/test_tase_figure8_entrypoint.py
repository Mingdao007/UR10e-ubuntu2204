from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import numpy as np


ROOT = Path(__file__).resolve().parents[1]
import sys

sys.path.insert(0, str(ROOT / "tools"))

from step5d_paper_outer_loop import (  # noqa: E402
    Step5dOuterLoopConfig,
    Step5dOuterLoopInputs,
    Step5dOuterLoopState,
)
from tase_figure8_entrypoint import (  # noqa: E402
    PROTOCOLS,
    PROVIDER_ID,
    TRAJECTORY_ID,
    chain_receipt,
    canonical_command,
    runtime_environment,
)
from tase_rnn_mature_provider import TaseRnnMatureProvider  # noqa: E402


def test_both_protocols_are_explicit_and_do_not_share_scoring_window() -> None:
    r013 = PROTOCOLS["r013-60s"]
    full = PROTOCOLS["full-cycle"]
    assert (r013.score_start_s, r013.score_end_s) == (5.0, 60.0)
    assert r013.complete_period_s is None
    assert full.complete_period_s == 62.831853
    assert full.duration_s == 62.831853
    assert r013.required_bins == full.required_bins == 550


def test_entrypoint_always_delegates_to_one_canonical_supervisor() -> None:
    command = canonical_command(PROTOCOLS["r013-60s"], ["--output-root", "/tmp/run"])
    assert command[0].endswith("scripts/step5d-autotune-v3.sh")
    assert command[1] == "bridge-live"
    assert "step5d-autotune-v3.sh" in " ".join(command)
    env = runtime_environment(PROTOCOLS["r013-60s"], {})
    assert env["TASE_CONTROL_PROVIDER"] == PROVIDER_ID
    assert env["TASE_TRAJECTORY"] == TRAJECTORY_ID
    assert env["TASE_PROTOCOL_ID"] == "r013-60s"


def test_chain_receipt_names_single_writer_and_complete_lifecycle() -> None:
    receipt = chain_receipt(PROTOCOLS["r013-60s"])
    assert receipt["supervisor"] == "run_step5d_autotune_v3_live.py"
    assert receipt["provider"] == PROVIDER_ID
    assert "single" in str(receipt["writer"])
    assert receipt["lifecycle"][-2:] == ["verified Home", "result"]


def test_mature_provider_delegates_existing_outer_loop_without_changing_result() -> None:
    provider = TaseRnnMatureProvider()
    config = Step5dOuterLoopConfig(kp=0.0, ko=0.0, kf=0.0)
    state = Step5dOuterLoopState()
    inputs = Step5dOuterLoopInputs(
        tcp_pose_base=(0.0, 0.0, 0.0, 0.0, 0.0, 0.0),
        tcp_speed_base=(0.0, 0.0, 0.0, 0.0, 0.0, 0.0),
        force_tcp_n=(0.0, 0.0, -5.0),
        x_pd_base=(0.0, 0.0, 0.0),
        xdot_pd_base=(0.0, 0.0, 0.0),
        dt_s=0.002,
    )
    result = provider.compute_outer_loop(config, state, inputs)
    assert result.cmd_valid is True
    assert result.diagnostics["normal_load_n"] == 5.0
    provider.validate_binding({"TASE_CONTROL_PROVIDER": PROVIDER_ID, "TASE_TRAJECTORY": TRAJECTORY_ID})


def test_shell_entrypoint_help_and_dry_run_stop_before_controller() -> None:
    script = ROOT / "scripts" / "figure8.sh"
    subprocess.run(["bash", "-n", str(script)], check=True)
    help_result = subprocess.run([str(script), "--help"], check=True, text=True, capture_output=True)
    assert "canonical Step5d supervisor" in help_result.stdout
    dry = subprocess.run([str(script), "--dry-run"], check=True, text=True, capture_output=True)
    payload = json.loads(dry.stdout)
    assert payload["command"][1] == "bridge-live"
    assert payload["environment"]["TASE_CONTROL_PROVIDER"] == PROVIDER_ID
    assert payload["environment"]["TASE_PATH_SHAPE"] == "eight"


def test_prepare_only_is_a_no_io_probe_and_full_cycle_is_explicitly_offline() -> None:
    script = ROOT / "scripts" / "figure8.sh"
    prepared = subprocess.run([str(script), "--prepare-only"], check=True, text=True, capture_output=True)
    assert json.loads(prepared.stdout)["environment"]["TASE_PATH_SHAPE"] == "eight"
    blocked = subprocess.run([str(script), "--protocol", "full-cycle"], text=True, capture_output=True)
    assert blocked.returncode == 64
    assert "offline protocol" in blocked.stderr


def test_bridge_binds_provider_at_runtime_and_delegates_outer_loop() -> None:
    source = (ROOT / "tools" / "kunwei_rtde_bridge.py").read_text(encoding="utf-8")
    assert "state.tase_rnn_mature_provider.validate_binding()" in source
    assert "state.tase_rnn_mature_provider.compute_outer_loop(" in source
