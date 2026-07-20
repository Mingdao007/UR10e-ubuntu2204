from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
TOOLS = ROOT / "tools"
RUNTIME_SRC = ROOT.parents[1] / "src" / "ur10e_experiment_runtime"
sys.path.insert(0, str(TOOLS))
sys.path.insert(0, str(ROOT / "tests"))
sys.path.insert(0, str(RUNTIME_SRC))

from prepare_step5d_autotune_launch import (  # noqa: E402
    write_machine_campaign_binding,
)
from step5d_autotune_backend import Step5dV35Backend  # noqa: E402
from step5d_autotune_batch_plan import load_plan  # noqa: E402
from step5d_autotune_v3.runtime_profile import load_launch_profile  # noqa: E402
from step5d_v3_fake_bridge_harness import exact_trial_overlay  # noqa: E402
from run_step5d_autotune_campaign import _profile  # noqa: E402


PLAN_FIXTURE = ROOT / "tests/fixtures/step5d_r005_exact_candidate_plan.json"
PLAN_SHA256 = "bed54b7482fa596fcc6bf34fa4c4aabbeecfe9c86903b123aa68f4edeaec5935"
LAUNCH_PROFILE = ROOT / "config/step5/step5d_autotune_v3_launch_profile.json"


def _atomic_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, allow_nan=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _overlay_plan(candidate_plan: Path, path: Path) -> dict[str, object]:
    plan = load_plan(candidate_plan, campaign_id="step5d-native-1")
    profile = _profile(ROOT)
    launch = load_launch_profile(LAUNCH_PROFILE)
    trials = []
    for candidate in plan.candidates:
        overlay = exact_trial_overlay(candidate, profile)
        trials.append(
            {
                "transport_candidate_uid": candidate.candidate_uid,
                "control_candidate_uid": overlay["control_candidate_uid"],
                "overlay": overlay,
            }
        )
    batches = [
        {
            "batch_id": 1,
            "source": plan.payload["batches"][0]["source"],
            "trials": trials,
        }
    ]
    fingerprint = hashlib.sha256(
        json.dumps(
            {
                "launch_profile_fingerprint": launch.fingerprint,
                "batches": batches,
            },
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()
    payload: dict[str, object] = {
        "schema": "step5d.autotune-v3/trial-overlay-plan-v2",
        "revision": plan.revision,
        "candidate_count": len(plan.candidates),
        "launch_profile_fingerprint": launch.fingerprint,
        "fingerprint": fingerprint,
        "batches": batches,
    }
    _atomic_json(path, payload)
    return payload


def _wait_file(path: Path, process: subprocess.Popen[str], timeout_s: float) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if path.is_file():
            return
        if process.poll() is not None:
            stdout, stderr = process.communicate()
            raise AssertionError(
                f"transport exited before {path.name}: rc={process.returncode}\n"
                f"stdout={stdout}\nstderr={stderr}"
            )
        time.sleep(0.01)
    raise AssertionError(f"timed out waiting for {path}")


def _terminate(process: subprocess.Popen[str]) -> None:
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=3.0)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=3.0)


def test_formal_runner_follows_growing_production_csv_to_real_arm2(
    tmp_path: Path,
) -> None:
    """The release gate may fake transport, never the production host runner."""

    assert hashlib.sha256(PLAN_FIXTURE.read_bytes()).hexdigest() == PLAN_SHA256
    bridge_run = (tmp_path / "bridge").resolve()
    campaign_root = (tmp_path / "campaign").resolve()
    mailbox = bridge_run / "runtime/command.json"
    candidate_plan = campaign_root / "control/candidate_plan.json"
    candidate_plan.parent.mkdir(parents=True)
    shutil.copyfile(PLAN_FIXTURE, candidate_plan)
    overlays = campaign_root / "control/v3_trial_overlays.json"
    _overlay_plan(candidate_plan, overlays)
    frozen = Step5dV35Backend(ROOT).freeze_fingerprint()
    binding = (bridge_run / "runtime/campaign_binding.json").resolve()
    write_machine_campaign_binding(
        binding,
        campaign_id="step5d-native-1",
        campaign_epoch=1,
        campaign_fingerprint=frozen.composite_fingerprint,
        candidate_plan_path=candidate_plan,
        trial_overlay_plan_path=overlays,
        binding_source="r006_offline_production_chain_gate",
    )

    environment = dict(os.environ)
    environment["PYTHONPATH"] = os.pathsep.join(
        (str(RUNTIME_SRC), str(TOOLS), environment.get("PYTHONPATH", ""))
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
                "--selection-policy",
                "codex_batches",
                "--candidate-plan",
                str(candidate_plan),
                "--v3-trial-overlays",
                str(overlays),
                "--v3-launch-profile",
                str(LAUNCH_PROFILE),
                "--v3-derived-postprocess-root",
                str(campaign_root / "postprocess"),
                "--trial-timeout-s",
                "15",
                "--plan-wait-timeout-s",
                "5",
                "--offline-release-gate",
            ],
            cwd=ROOT,
            env=environment,
            capture_output=True,
            text=True,
            timeout=45.0,
        )
        assert runner.returncode == 0, (
            f"runner rc={runner.returncode}\nstdout={runner.stdout}\n"
            f"stderr={runner.stderr}"
        )
        result = json.loads(runner.stdout.strip().splitlines()[-1])
        assert result["offline_gate_arm2_observed"] is True
        assert result["trial_completed"] is True
        assert result["phase"] == "trial_active"
        assert result["bridge_csv_follower"]["partial_line_polls"] >= 1
        assert result["bridge_csv_follower"]["rows_seen"] >= 2

        transport_stdout, transport_stderr = transport.communicate(timeout=10.0)
        assert transport.returncode == 0, (
            f"transport rc={transport.returncode}\nstdout={transport_stdout}\n"
            f"stderr={transport_stderr}"
        )
        transport_stats = json.loads(
            (bridge_run / "r006_fake_transport_stats.json").read_text(
                encoding="utf-8"
            )
        )
        assert transport_stats["protocol"] == "v3_direct_arm_v1"
        assert transport_stats["arm_sequences"] == [1, 2]
        assert transport_stats["commands"] == ["ARM", "ARM"]
        assert transport_stats["partial_visibility_exercised"] is True
        assert transport_stats["terminal_capture_sealed"] is True
        assert transport_stats["writer"]["terminal_flushes"] == 1
        assert transport_stats["writer"]["buffered_flushes"] >= 1

        events = [
            json.loads(line)
            for line in (campaign_root / "events.jsonl").read_text(
                encoding="utf-8"
            ).splitlines()
        ]
        assert sum(row["event"] == "direct_bundle_cold_read_verified" for row in events) == 1
        assert sum(row["event"] == "direct_ready_committed" for row in events) == 1
        assert sum(row["event"] == "offline_release_gate_arm2_entered_run" for row in events) == 1
        assert not any("ack" in row["event"].lower() for row in events)

        briefs = tuple((campaign_root / "trial_briefs").glob("*.json"))
        assert len(briefs) == 1
        brief = json.loads(briefs[0].read_text(encoding="utf-8"))
        assert brief["objective"] is None
        assert brief["optimizer_eligible"] is False
        assert brief["protocol"] == "v3_direct_arm_v1"
    finally:
        _terminate(transport)
