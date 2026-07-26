from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
TOOLS = ROOT / "tools"
RUNTIME_SRC = ROOT.parents[1] / "src/ur10e_experiment_runtime"
sys.path.insert(0, str(RUNTIME_SRC))
sys.path.insert(0, str(TOOLS))
sys.path.insert(0, str(ROOT / "tests"))

from step5d_autotune_backend import Step5dV35Backend  # noqa: E402
from step5d_autotune_batch_plan import (  # noqa: E402
    append_r008_batch,
    initialize_rolling_plan,
    mark_rolling_plan_open_empty,
)
from prepare_step5d_autotune_launch import write_machine_campaign_binding  # noqa: E402
from step5d_autotune_r008_policy import initialization_batch, recovery_batch  # noqa: E402
from ur10e_experiment_runtime.candidate_identity import ControlCandidateUid  # noqa: E402
from test_step5d_r006_production_chain import (  # noqa: E402
    LAUNCH_PROFILE,
    _overlay_plan,
    _terminate,
    _wait_file,
)


def _bind_control(rows):
    return tuple(
        row.bind_control_candidate_uid(
            str(ControlCandidateUid.from_overlay(
                {
                    "force_p_gain": row.candidate.force_p_gain,
                    "force_i_gain": row.candidate.force_i_gain,
                    "force_damping": row.candidate.force_damping,
                    "orientation_ko": 0.4,
                }
            ))
        )
        for row in rows
    )


def test_formal_runner_crosses_row5_batch2_and_arm11_with_real_csv_processes(
    tmp_path: Path,
) -> None:
    bridge_run = (tmp_path / "bridge").resolve()
    campaign_root = (tmp_path / "campaign").resolve()
    mailbox = bridge_run / "runtime/command.json"
    plan_path = campaign_root / "control/candidate_plan.json"
    initialize_rolling_plan(plan_path, campaign_id="step5d-native-1")
    append_r008_batch(
        plan_path, occurrences=_bind_control(initialization_batch(1)), source="init-p"
    )
    mark_rolling_plan_open_empty(plan_path)
    append_r008_batch(
        plan_path, occurrences=_bind_control(initialization_batch(2)), source="init-d"
    )
    mark_rolling_plan_open_empty(plan_path)
    append_r008_batch(
        plan_path, occurrences=_bind_control(recovery_batch(3)), source="recovery"
    )
    overlays = campaign_root / "control/v3_trial_overlays.json"
    _overlay_plan(plan_path, overlays)
    frozen = Step5dV35Backend(ROOT).freeze_fingerprint()
    binding = (bridge_run / "runtime/campaign_binding.json").resolve()
    write_machine_campaign_binding(
        binding,
        campaign_id="step5d-native-1",
        campaign_epoch=1,
        campaign_fingerprint=frozen.composite_fingerprint,
        candidate_plan_path=plan_path,
        trial_overlay_plan_path=overlays,
        binding_source="r008_offline_rolling_production_chain_gate",
    )
    environment = dict(os.environ)
    environment["PYTHONPATH"] = os.pathsep.join(
        (str(RUNTIME_SRC), str(TOOLS), str(ROOT / "tests"), environment.get("PYTHONPATH", ""))
    ).rstrip(os.pathsep)
    environment["STEP5D_CUDA_BOOTSTRAPPED"] = "1"
    transport = subprocess.Popen(
        [
            sys.executable,
            str(ROOT / "tests/step5d_r008_rolling_csv_transport.py"),
            "--bridge-run",
            str(bridge_run),
            "--mailbox",
            str(mailbox),
            "--trial-count",
            "15",
        ],
        cwd=ROOT,
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        _wait_file(bridge_run / "bridge_ready.json", transport, 10.0)
        runner = subprocess.run(
            [
                sys.executable,
                str(ROOT / "tools/run_step5d_autotune_campaign.py"),
                "--experiment-root",
                str(ROOT),
                "--bridge-run",
                str(bridge_run),
                "--campaign-root",
                str(campaign_root),
                "--mailbox",
                str(mailbox),
                "--campaign-binding",
                str(binding),
                "--selection-policy",
                "codex_batches",
                "--candidate-plan",
                str(plan_path),
                "--v3-trial-overlays",
                str(overlays),
                "--v3-launch-profile",
                str(LAUNCH_PROFILE),
                "--v3-derived-postprocess-root",
                str(campaign_root / "postprocess"),
                "--trial-timeout-s",
                "20",
                "--plan-wait-timeout-s",
                "5",
                "--close-after-plan-revision",
                "3",
                "--offline-release-gate",
            ],
            cwd=ROOT,
            env=environment,
            capture_output=True,
            text=True,
            timeout=90.0,
        )
        if runner.returncode != 0:
            _terminate(transport)
            pytest.fail(
                f"runner returncode={runner.returncode}\n"
                f"runner stdout={runner.stdout}\n"
                f"runner stderr={runner.stderr}\n"
                f"transport returncode={transport.returncode}"
            )
        result = json.loads(runner.stdout.strip().splitlines()[-1])
        assert result["batch_completed"] is True
        assert result["completion_consumed"] is True
        assert result["trial_completed"] is True
        deadline = time.monotonic() + 10.0
        while transport.poll() is None and time.monotonic() < deadline:
            time.sleep(0.01)
        stdout, stderr = transport.communicate(timeout=3.0)
        assert transport.returncode == 0, f"stdout={stdout}\nstderr={stderr}"
        stats = json.loads((bridge_run / "r008_fake_transport_stats.json").read_text())
        assert stats["protocol"] == "v3_full_home_rolling_arm_v1"
        assert stats["arm_sequences"] == list(range(1, 16))
        assert stats["logical_batch_sequences"][4:6] == [1, 2]
        assert stats["rows"][4:6] == [5, 1]
        assert stats["commit_transcripts"] == [
            [24, 25, 27, 28, 29, 31, 32, 33, 34, 26, 30]
        ] * 15
        assert 11 in stats["arm_sequences"]
        assert stats["complete_command_seq"] == 16
        assert stats["final_state"] == 77
        completion = json.loads(
            (campaign_root / "control/pending_completion.json").read_text()
        )
        assert completion["status"] == "consumed"
        assert completion["packet"]["command"] == 4
        batch_roots = tuple((campaign_root / "runtime_batches").iterdir())
        assert len(batch_roots) == 3
        assert len(tuple((campaign_root / "trial_briefs").glob("*.json"))) == 15
    finally:
        _terminate(transport)
