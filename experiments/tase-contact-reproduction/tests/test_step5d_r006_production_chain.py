from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parents[1]
TOOLS = ROOT / "tools"
RUNTIME_SRC = ROOT.parents[1] / "src" / "ur10e_experiment_runtime"
sys.path.insert(0, str(TOOLS))
sys.path.insert(0, str(ROOT / "tests"))
sys.path.insert(0, str(RUNTIME_SRC))

from step5d_machine_campaign_binding import (  # noqa: E402
    write_machine_campaign_binding,
)
from run_step5d_autotune_campaign import (  # noqa: E402
    NORMAL_FILTER_PROFILES,
    _campaign_binding,
    _validate_machine_plan_files,
)
from step5d_autotune_backend import Step5dV35Backend  # noqa: E402
from step5d_autotune_batch_plan import load_plan  # noqa: E402
from step5d_autotune_v3.runtime_profile import load_launch_profile  # noqa: E402
from step5d_autotune_v3.runtime_profile import normalized_overlay_sha256  # noqa: E402
from step5d_autotune_v3.launch_basis import (  # noqa: E402
    make_launch_basis,
    write_launch_basis,
)
from step5d_autotune_v3.runtime_gate import process_starttime  # noqa: E402
from step5d_v3_fake_bridge_harness import exact_trial_overlay  # noqa: E402


PLAN_FIXTURE = ROOT / "tests/fixtures/step5d_r005_exact_candidate_plan.json"
PLAN_SHA256 = "bed54b7482fa596fcc6bf34fa4c4aabbeecfe9c86903b123aa68f4edeaec5935"
RECEIVER_PLAN_SCHEMA = "step5d.parameter-receiver/launch-plan-v1"
LAUNCH_PROFILE = ROOT / "config/step5/step5d_autotune_v3_launch_profile.json"
PROGRAM = json.loads(LAUNCH_PROFILE.read_text(encoding="utf-8"))["tp_program_id"]


def _atomic_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, allow_nan=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _overlay_plan(candidate_plan: Path, path: Path) -> dict[str, object]:
    plan = load_plan(candidate_plan, campaign_id="step5d-native-1")
    launch = load_launch_profile(LAUNCH_PROFILE, expected_tp_program_id=PROGRAM)
    allowed = launch.trial_overlay_policy["execution_profile_id"]["allowed"]
    authorized = [
        profile
        for profile in NORMAL_FILTER_PROFILES
        if profile.profile_id in allowed
    ]
    if not authorized:
        raise RuntimeError("launch profile has no authorized execution profile")
    profile = authorized[0]
    batches = []
    if any(plan.occurrences):
        for batch_id, occurrences in enumerate(plan.occurrences, start=1):
            trials = []
            for occurrence in occurrences:
                overlay = exact_trial_overlay(occurrence.candidate, profile)
                trials.append(
                    {
                        "occurrence_uid": occurrence.occurrence_uid,
                        "transport_candidate_uid": occurrence.transport_candidate_uid,
                        "control_candidate_uid": occurrence.control_candidate_uid,
                        "normalized_overlay_sha256": normalized_overlay_sha256(
                            launch, overlay
                        ),
                        "overlay": overlay,
                    }
                )
            batches.append(
                {
                    "batch_id": batch_id,
                    "source": plan.payload["batches"][batch_id - 1]["source"],
                    "trials": trials,
                }
            )
    else:
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
        batches.append(
            {
                "batch_id": 1,
                "source": plan.payload["batches"][0]["source"],
                "trials": trials,
            }
        )
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


def _fixture_launch_basis(
    path: Path,
    *,
    campaign_fingerprint: str,
    owner_pid: int,
    owner_starttime: int,
) -> dict[str, object]:
    now_ns = time.time_ns()
    current_pointer = json.loads(
        (ROOT / "config/step5d/current.json").read_text(encoding="utf-8")
    )
    release_manifest = json.loads(
        (ROOT / current_pointer["manifest_path"]).read_text(encoding="utf-8")
    )
    runtime_identity = release_manifest["tp_runtime_identity"]
    canonical = lambda value: json.dumps(  # noqa: E731
        value,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
        ensure_ascii=True,
    ).encode("utf-8")
    delivery_evidence = ROOT / "config/step5d/current.json"
    effective_config = {
        "launch_profile": json.loads(LAUNCH_PROFILE.read_text(encoding="utf-8")),
        "release_manifest": release_manifest,
    }
    payload = make_launch_basis(
        release_manifest_sha256=current_pointer["manifest_sha256"],
        runtime_identity_sha256=hashlib.sha256(canonical(runtime_identity)).hexdigest(),
        campaign_fingerprint=campaign_fingerprint,
        delivery_observation_sha256=hashlib.sha256(delivery_evidence.read_bytes()).hexdigest(),
        owner_pid=owner_pid,
        owner_starttime=owner_starttime,
        authority_epoch=1,
        launch_nonce=hashlib.sha256(
            f"r006-offline-fixture:{path.resolve()}".encode("utf-8")
        ).hexdigest()[:32],
        argv_sha256=hashlib.sha256(canonical(sys.argv)).hexdigest(),
        effective_config_sha256=hashlib.sha256(canonical(effective_config)).hexdigest(),
        worktree_root=str(ROOT.resolve()),
        repository_head=subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True
        ).strip(),
        issued_at_unix_ns=now_ns - 1_000_000,
        expires_at_unix_ns=now_ns + 3_600_000_000_000,
    )
    return write_launch_basis(path, payload)


def test_formal_runner_reaches_canonical_lease_and_fails_closed(
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
    _atomic_json(
        receiver_plan,
        {
            "schema": RECEIVER_PLAN_SCHEMA,
            "campaign_id": "step5d-native-1",
            "revision": 1,
            "protocol": "v3_full_home_parameter_receiver_v1",
            "unbounded": True,
            "one_inflight": True,
            "optimizer_required": False,
        },
    )
    frozen = Step5dV35Backend(ROOT).freeze_fingerprint()
    binding = (bridge_run / "runtime/campaign_binding.json").resolve()
    write_machine_campaign_binding(
        binding,
        campaign_id="step5d-native-1",
        campaign_epoch=1,
        campaign_fingerprint=frozen.composite_fingerprint,
        receiver_plan_path=receiver_plan,
        optimizer_plan_path=candidate_plan,
        trial_overlay_plan_path=overlays,
        binding_source="r006_offline_production_chain_gate",
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
            ],
            cwd=ROOT,
            env=environment,
            capture_output=True,
            text=True,
            timeout=45.0,
        )
        combined_output = f"{runner.stdout}\n{runner.stderr}"
        assert runner.returncode != 0
        assert "canonical campaign lease" in combined_output
        assert not (bridge_run / "r006_fake_transport_stats.json").exists()
    finally:
        _terminate(transport)


def test_production_chain_binds_receiver_and_optimizer_plan_identities_separately(
    tmp_path: Path,
) -> None:
    """The strict machine writer accepts a receiver plan, not an optimizer plan."""

    assert hashlib.sha256(PLAN_FIXTURE.read_bytes()).hexdigest() == PLAN_SHA256
    campaign_root = (tmp_path / "campaign").resolve()
    bridge_run = (tmp_path / "bridge").resolve()
    candidate_plan = campaign_root / "control/candidate_plan.json"
    candidate_plan.parent.mkdir(parents=True)
    shutil.copyfile(PLAN_FIXTURE, candidate_plan)
    overlays = campaign_root / "control/v3_trial_overlays.json"
    _overlay_plan(candidate_plan, overlays)
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
    _atomic_json(
        receiver_plan,
        {
            "schema": RECEIVER_PLAN_SCHEMA,
            "campaign_id": "step5d-native-1",
            "revision": 1,
            "protocol": "v3_full_home_parameter_receiver_v1",
            "unbounded": True,
            "one_inflight": True,
            "optimizer_required": False,
        },
    )
    frozen = Step5dV35Backend(ROOT).freeze_fingerprint()
    binding = bridge_run / "runtime/receiver_campaign_binding.json"
    payload = write_machine_campaign_binding(
        binding,
        campaign_id="step5d-native-1",
        campaign_epoch=1,
        campaign_fingerprint=frozen.composite_fingerprint,
        receiver_plan_path=receiver_plan,
        optimizer_plan_path=candidate_plan,
        trial_overlay_plan_path=overlays,
        binding_source="r006_offline_production_chain_gate",
    )
    assert payload["receiver_plan"]["revision"] == 1
    assert payload["receiver_plan"]["sha256"] == hashlib.sha256(
        receiver_plan.read_bytes()
    ).hexdigest()
    assert payload["optimizer_plan"]["revision"] == load_plan(
        candidate_plan, campaign_id="step5d-native-1"
    ).revision
    assert payload["optimizer_plan"]["sha256"] == hashlib.sha256(
        candidate_plan.read_bytes()
    ).hexdigest()
    assert json.loads(binding.read_text(encoding="utf-8")) == payload

    machine_binding = _campaign_binding(
        binding,
        campaign=SimpleNamespace(
            campaign_id="step5d-native-1",
            campaign_epoch=1,
            campaign_fingerprint=frozen.composite_fingerprint,
        ),
        campaign_fingerprint=frozen.composite_fingerprint,
    )
    _validate_machine_plan_files(
        machine_binding,
        campaign_root=campaign_root,
        campaign_id="step5d-native-1",
        plan_path=candidate_plan,
        overlay_path=overlays,
        release_manifest_sha256=current_pointer["manifest_sha256"],
    )
    receiver_payload = json.loads(receiver_plan.read_text(encoding="utf-8"))
    receiver_payload["revision"] = 2
    _atomic_json(receiver_plan, receiver_payload)
    with pytest.raises(RuntimeError, match="receiver identity differs"):
        _validate_machine_plan_files(
            machine_binding,
            campaign_root=campaign_root,
            campaign_id="step5d-native-1",
            plan_path=candidate_plan,
            overlay_path=overlays,
            release_manifest_sha256=current_pointer["manifest_sha256"],
        )
    receiver_payload["revision"] = 1
    _atomic_json(receiver_plan, receiver_payload)
    candidate_plan.write_bytes(candidate_plan.read_bytes() + b"\n")
    with pytest.raises(RuntimeError, match="optimizer identity differs"):
        _validate_machine_plan_files(
            machine_binding,
            campaign_root=campaign_root,
            campaign_id="step5d-native-1",
            plan_path=candidate_plan,
            overlay_path=overlays,
            release_manifest_sha256=current_pointer["manifest_sha256"],
        )

    with pytest.raises(
        RuntimeError, match="exact receiver plan"
    ):
        write_machine_campaign_binding(
            bridge_run / "runtime/rejected_receiver_binding.json",
            campaign_id="step5d-native-1",
            campaign_epoch=1,
            campaign_fingerprint=frozen.composite_fingerprint,
            receiver_plan_path=candidate_plan,
            trial_overlay_plan_path=overlays,
            binding_source="r006_offline_production_chain_gate",
        )
    with pytest.raises(RuntimeError, match="exact optimizer plan"):
        write_machine_campaign_binding(
            bridge_run / "runtime/rejected_optimizer_binding.json",
            campaign_id="step5d-native-1",
            campaign_epoch=1,
            campaign_fingerprint=frozen.composite_fingerprint,
            receiver_plan_path=receiver_plan,
            optimizer_plan_path=receiver_plan,
            trial_overlay_plan_path=overlays,
            binding_source="r006_offline_production_chain_gate",
        )
