from __future__ import annotations

import json
import os
import subprocess
import sys
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
from step5d_machine_campaign_binding import write_machine_campaign_binding  # noqa: E402
from step5d_autotune_r008_policy import initialization_batch, recovery_batch  # noqa: E402
from ur10e_experiment_runtime.candidate_identity import ControlCandidateUid  # noqa: E402
from test_step5d_r006_production_chain import (  # noqa: E402
    LAUNCH_PROFILE,
    PROGRAM,
    _fixture_launch_basis,
    _overlay_plan,
    _terminate,
    _wait_file,
)
from step5d_autotune_v3.runtime_gate import process_starttime  # noqa: E402


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


def test_production_runner_fails_closed_at_canonical_lease_before_arm(
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
    current_pointer = json.loads(
        (ROOT / "config/step5d/current.json").read_text(encoding="utf-8")
    )
    receiver_plan = (
        campaign_root
        / "control"
        / "parameter_receiver_bindings"
        / current_pointer["manifest_sha256"]
        / "plan.json"
    )
    receiver_plan.parent.mkdir(parents=True)
    receiver_plan.write_text(
        json.dumps(
            {
                "schema": "step5d.parameter-receiver/launch-plan-v1",
                "campaign_id": "step5d-native-1",
                "revision": 1,
                "protocol": "v3_full_home_parameter_receiver_v1",
                "unbounded": True,
                "one_inflight": True,
                "optimizer_required": False,
            },
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    frozen = Step5dV35Backend(ROOT).freeze_fingerprint()
    binding = (bridge_run / "runtime/campaign_binding.json").resolve()
    write_machine_campaign_binding(
        binding,
        campaign_id="step5d-native-1",
        campaign_epoch=1,
        campaign_fingerprint=frozen.composite_fingerprint,
        receiver_plan_path=receiver_plan,
        optimizer_plan_path=plan_path,
        trial_overlay_plan_path=overlays,
        binding_source="r008_offline_rolling_production_chain_gate",
    )
    owner_pid = os.getpid()
    owner_starttime = process_starttime(owner_pid)
    launch_basis_path = bridge_run / "runtime/launch-basis.json"
    launch_basis = _fixture_launch_basis(
        launch_basis_path,
        campaign_fingerprint=frozen.composite_fingerprint,
        owner_pid=owner_pid,
        owner_starttime=owner_starttime,
    )
    environment = dict(os.environ)
    environment["PYTHONPATH"] = os.pathsep.join(
        (str(RUNTIME_SRC), str(TOOLS), str(ROOT / "tests"), environment.get("PYTHONPATH", ""))
    ).rstrip(os.pathsep)
    environment["STEP5D_CUDA_BOOTSTRAPPED"] = "1"
    transport = subprocess.Popen(
        [
                sys.executable,
                str(ROOT / "tests/step5d_r006_production_csv_transport.py"),
                "--bridge-run",
                str(bridge_run),
                "--mailbox",
                str(mailbox),
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
                "--launch-basis",
                str(launch_basis_path),
                "--launch-basis-sha256",
                str(launch_basis["basis_sha256"]),
                "--owner-pid",
                str(owner_pid),
                "--owner-starttime",
                str(owner_starttime),
                "--selection-policy",
                "codex_batches",
                "--candidate-plan",
                str(plan_path),
                "--v3-trial-overlays",
                str(overlays),
                "--v3-launch-profile",
                str(LAUNCH_PROFILE),
                "--v3-program-id",
                str(PROGRAM),
                "--v3-derived-postprocess-root",
                str(campaign_root / "postprocess"),
                "--trial-timeout-s",
                "20",
                "--plan-wait-timeout-s",
                "5",
                "--close-after-plan-revision",
                "3",
            ],
            cwd=ROOT,
            env=environment,
            capture_output=True,
            text=True,
            timeout=90.0,
        )
        combined_output = f"{runner.stdout}\n{runner.stderr}"
        assert runner.returncode != 0
        assert "live campaign requires the canonical campaign lease" in combined_output
        assert not (bridge_run / "r008_fake_transport_stats.json").exists()
        assert not (bridge_run / "r006_fake_transport_stats.json").exists()
        assert not (campaign_root / "control/pending_completion.json").exists()
    finally:
        _terminate(transport)
