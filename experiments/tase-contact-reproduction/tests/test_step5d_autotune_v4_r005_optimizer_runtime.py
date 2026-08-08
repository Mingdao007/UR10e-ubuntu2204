from __future__ import annotations

import ast
import copy
import dataclasses
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import time

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT.parents[1] / "src/ur10e_experiment_runtime"))

from step5d_optimizer_runtime import (  # noqa: E402
    OptimizerRuntimeError,
    OptimizerSubprocessClient,
    resolve_optimizer_runtime,
)


def test_live_admission_uses_resolver_ttl_for_durable_optimizer_attestation(
    tmp_path: Path,
) -> None:
    from step5d_autotune_v3.runtime_installation import current_pointer_path
    from step5d_autotune_v4_r004.contracts import runtime_identity_limbs
    from step5d_autotune_v4_r005.contracts import load_contract
    from step5d_autotune_v4_r005.live_adapter import (
        R005LiveAdapter,
        R005LiveInputs,
        build_verified_mature_r005_writer,
    )
    from step5d_autotune_v4_r005.observations import ObservationLedger
    from step5d_autotune_v4_r005.tp import RUNTIME_PROTOCOL

    contract = load_contract()
    from step5d_autotune_v4_r004.contracts import load_contract as load_r004_contract

    parent = load_r004_contract()
    resolved = resolve_optimizer_runtime()
    attestation_observed_s = float(resolved.attestation["observed_at_unix_ns"]) / 1e9
    assert time.time() - attestation_observed_s > 30.0
    assert time.time() - attestation_observed_s < resolved.declaration.stale_after_s
    now = time.time()
    runtime_hi, runtime_lo = runtime_identity_limbs(
        contract.raw["program"], contract.sha256, contract.campaign_fingerprint
    )
    program_root = ROOT / "programs/step5/step5d/step5d_strict_rnn_autotune_v4_r005"
    triplet = {
        role: hashlib.sha256(program_root.with_suffix(suffix).read_bytes()).hexdigest()
        for role, suffix in (("script", ".script"), ("txt", ".txt"), ("urp", ".urp"))
    }
    common = {
        "observed_at_s": now - 2.0,
        "safety_mode": "NORMAL",
        "stationary": True,
    }
    controller = {
        **common,
        "program": contract.raw["program"],
        "controller_target": "/programs/andyl/kunwei/step5/step5d_strict_rnn_autotune_v4_r005.urp",
        "receipt_sha256": "b" * 64,
        "script_sha256": triplet["script"],
        "txt_sha256": triplet["txt"],
        "urp_sha256": triplet["urp"],
        "eoat_identity_sha256": contract.raw["invariants"]["eoat_profile"]["sha256"],
        "readback": {
            "actual_tcp_speed_m_s_rad_s": [0.0] * 6,
            "payload_kg": 0.413,
            "payload_cog_m": [0.0011, 0.0031, 0.0163],
            "tcp_offset_m_rad": [0.0, 0.0, 0.0874, 0.0, 0.0, 0.0],
        },
        "route_id": "r005-route-test",
        "runtime_protocol": RUNTIME_PROTOCOL,
        "runtime_digest_hi": runtime_hi,
        "runtime_digest_lo": runtime_lo,
    }
    script1 = {
        **common,
        "receipt_sha256": "c" * 64,
        "script_sha256": parent.script1_sha256["script"],
        "final_pose": [0.487834547, 0.129337053, 0.033, 3.120752062, 0.0, 0.068626833],
        "final_q": [0.0] * 6,
        "eoat_identity_sha256": contract.raw["invariants"]["eoat_profile"]["sha256"],
    }
    runtime = {
        "observed_at_s": now - 1.0,
        "program": contract.raw["program"],
        "script_sha256": triplet["script"],
        "runtime_protocol": RUNTIME_PROTOCOL,
        "runtime_digest_hi": runtime_hi,
        "runtime_digest_lo": runtime_lo,
        "session_epoch": 5005,
        "resident_session_id": "r005-resident-test",
        "program_running": True,
        "uninterrupted": True,
    }
    controller_path = tmp_path / "controller.json"
    script1_path = tmp_path / "script1.json"
    runtime_path = tmp_path / "runtime.json"
    software_baseline_path = tmp_path / "software-baseline.json"
    for path, payload in (
        (controller_path, controller),
        (script1_path, script1),
        (runtime_path, runtime),
    ):
        path.write_text(json.dumps(payload), encoding="utf-8")
    software_baseline_path.write_text(
        json.dumps(
            {
                "schema": "step5d.autotune-v4/r005-software-baseline-v1",
                "observed_at_s": now - 1.5,
                "sample_count": 900,
                "mean_wrench_n_nm": [1.0, 2.0, 3.0, 0.1, 0.2, 0.3],
                "parse_errors": 0,
                "dropped_bytes": 0,
                "zero_tare_config_write": False,
            }
        ),
        encoding="utf-8",
    )
    ledger_path = tmp_path / "observations.jsonl"
    ObservationLedger(
        ledger_path,
        campaign_fingerprint=contract.campaign_fingerprint,
        eoat_sha256=contract.raw["invariants"]["eoat_profile"]["sha256"],
    )
    queue_root = tmp_path / "queue"
    authority_root = tmp_path / "authority"
    queue_root.mkdir()
    authority_root.mkdir()
    inputs = R005LiveInputs(
        controller_receipt=controller_path,
        script1_receipt=script1_path,
        runtime_evidence=runtime_path,
        runtime_attestation=Path(str(resolved.pointer["attestation_path"])),
        optimizer_pointer=current_pointer_path(),
        launch_profile_path=ROOT / "config/step5/step5d_autotune_v4_r005_launch_profile.json",
        release_manifest_sha256="a" * 64,
        baseline_ledger=ROOT / "config/step5d/autotune_v4_r005_baseline_ledger_genesis.json",
        software_baseline_receipt=software_baseline_path,
        ledger_path=ledger_path,
        queue_root=queue_root,
        authority_root=authority_root,
        controller_host="controller.test",
        kunwei_host="kunwei.test",
        kunwei_port=5152,
        route_id="r005-route-test",
        attempt_id="r005-attempt-test",
        resident_session_id="r005-resident-test",
        session_epoch=5005,
        expected_triplet=triplet,
        eoat_sha256=contract.raw["invariants"]["eoat_profile"]["sha256"],
        campaign_fingerprint=contract.campaign_fingerprint,
        contract_sha256=contract.sha256,
        now_s=now,
    )
    inputs.validate(contract=contract)
    assert inputs.software_baseline_n() == (1.0, 2.0, 3.0, 0.1, 0.2, 0.3)
    mature = build_verified_mature_r005_writer(inputs, contract=contract)
    assert mature.writer.software_baseline_n == (1.0, 2.0, 3.0, 0.1, 0.2, 0.3)
    assert mature.writer._canonical_runtime_only is True
    assert mature.writer.contract.raw["program"] == "step5d_strict_rnn_autotune_v4_r005"
    assert "live_boundary" in mature.writer.contract.raw
    assert (
        mature.writer.contract.raw["live_boundary"][
            "canonical_calibrated_numeric_residual_policy"
        ]["action"]
        == "zero_qdot_baseline_hold_until_converged"
    )
    adapted = R005LiveAdapter(contract=contract).adapt_verified_writer(mature)
    assert mature.writer._path_sample_sink == adapted.observe_r004_path_sample


def test_missing_stale_and_tampered_v3_pointer_fail_closed(tmp_path: Path) -> None:
    missing = {
        "XDG_DATA_HOME": str(tmp_path / "data"),
        "XDG_STATE_HOME": str(tmp_path / "state"),
        "XDG_CACHE_HOME": str(tmp_path / "cache"),
    }
    with pytest.raises(OptimizerRuntimeError, match="unavailable|missing"):
        resolve_optimizer_runtime(environ=missing)

    current = resolve_optimizer_runtime()
    tampered = copy.deepcopy(dict(current.pointer))
    tampered["profiles"]["optimizer"]["environment_id"] = "0" * 64
    with pytest.raises(OptimizerRuntimeError, match="attestation|differs"):
        resolve_optimizer_runtime(
            pointer_loader=lambda **_kwargs: tampered,
        )

    observed_ns = int(current.attestation["observed_at_unix_ns"])
    with pytest.raises(OptimizerRuntimeError, match="stale"):
        resolve_optimizer_runtime(
            now_unix_s=observed_ns / 1_000_000_000.0 + 30.0 * 24.0 * 60.0 * 60.0 + 1.0,
        )


def test_system_python_delegates_without_parent_torch_import(
    tmp_path: Path,
) -> None:
    system_python = shutil.which("python3")
    assert system_python
    fake = tmp_path / "isolated-optimizer-python"
    fake.write_text(
        "#!/usr/bin/env python3\n"
        "import hashlib, json, sys\n"
        "raw = sys.stdin.buffer.read()\n"
        "body = json.loads(raw.decode('utf-8'))\n"
        "response = {'schema': 'step5d.optimizer-runtime/response-v1', 'ok': True, "
        "'request_sha256': hashlib.sha256(raw).hexdigest(), "
        "'attestation': body['expected_attestation'], "
        "'result': {'delegated': True, 'delegated_argv0': sys.argv[0], "
        "'selected': {}, 'metadata': {}}}\n"
        "sys.stdout.write(json.dumps(response, sort_keys=True, separators=(',', ':')))\n",
        encoding="utf-8",
    )
    fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
    code = """
import dataclasses
import os
import sys
from pathlib import Path
from step5d_optimizer_runtime import OptimizerSubprocessClient, resolve_optimizer_runtime

assert "torch" not in sys.modules
runtime = resolve_optimizer_runtime()
runtime = dataclasses.replace(runtime, python_executable=Path(os.environ["FAKE_OPTIMIZER_PYTHON"]))
client = OptimizerSubprocessClient(
    worker_module="step5d_autotune_v4_r005.optimizer_worker",
    runtime_resolver=lambda: runtime,
)
result = client.request({"probe": "system-python-delegation"})
assert result["delegated"] is True
assert result["delegated_argv0"] == os.environ["FAKE_OPTIMIZER_PYTHON"]
assert "torch" not in sys.modules
"""
    environment = dict(os.environ)
    environment["PYTHONPATH"] = os.pathsep.join(
        (str(ROOT / "tools"), str(ROOT / "src"), str(ROOT.parents[1] / "src/ur10e_experiment_runtime"))
    )
    environment["FAKE_OPTIMIZER_PYTHON"] = str(fake)
    completed = subprocess.run(
        [system_python, "-c", code],
        cwd=ROOT,
        env=environment,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr


def test_wrong_child_attestation_fails_closed(tmp_path: Path) -> None:
    fake = tmp_path / "wrong-attestation-python"
    fake.write_text(
        "#!/usr/bin/env python3\n"
        "import hashlib, json, sys\n"
        "raw = sys.stdin.buffer.read()\n"
        "body = json.loads(raw.decode('utf-8'))\n"
        "wrong = dict(body['expected_attestation'])\n"
        "wrong['environment_id'] = '0' * 64\n"
        "response = {'schema': 'step5d.optimizer-runtime/response-v1', 'ok': True, "
        "'request_sha256': hashlib.sha256(raw).hexdigest(), 'attestation': wrong, "
        "'result': {'selected': {}, 'metadata': {}}}\n"
        "sys.stdout.write(json.dumps(response, sort_keys=True, separators=(',', ':')))\n",
        encoding="utf-8",
    )
    fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
    runtime = dataclasses.replace(
        resolve_optimizer_runtime(),
        python_executable=fake,
    )
    client = OptimizerSubprocessClient(
        worker_module="step5d_autotune_v4_r005.optimizer_worker",
        runtime_resolver=lambda: runtime,
    )
    with pytest.raises(OptimizerRuntimeError, match="attestation"):
        client.request({"probe": "wrong-child-attestation"})


def test_parent_optimizer_source_has_no_runtime_or_cpu_fallback() -> None:
    path = ROOT / "tools/step5d_autotune_v4_r005/optimizer.py"
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported = {
        alias.name.split(".", 1)[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    imported.update(
        alias.name.split(".", 1)[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
        for alias in node.names
    )
    assert not {"torch", "botorch", "gpytorch"}.intersection(imported)
    assert "sys.executable" not in source
    assert "python3" not in source
    assert "CPU fallback" not in source
    assert "local optimizer" not in source
    assert "degraded fallback" not in source
    assert "OptimizerSubprocessClient" in source

    runtime_source = (ROOT / "tools/step5d_optimizer_runtime.py").read_text(encoding="utf-8")
    assert "runtime.python_executable" in runtime_source
    assert "optimizer must not inherit the control host interpreter" in runtime_source
    assert "optimizer must not reuse the control host prefix" in runtime_source
    assert "optimizer device binding is not CUDA" in runtime_source
    assert "subprocess.run" in runtime_source


def test_real_isolated_cuda_qlognei_with_one_x_pending(tmp_path) -> None:
    import sys as parent_sys

    from step5d_autotune_v4_r005.contracts import Candidate, P_ANCHOR, load_contract
    from step5d_autotune_v4_r005.observations import ObservationLedger, ObservationRecord
    from step5d_autotune_v4_r005.optimizer import V4BoAdapter
    from step5d_force_objective import ForceObjectiveBuilder, ForcePathSample

    assert "torch" not in parent_sys.modules
    contract = load_contract()

    def raw_fixture(force: float):
        builder = ForceObjectiveBuilder()
        samples = []
        for sample_index in range(600):
            sample = ForcePathSample(
                path_time_s=sample_index / 10.0,
                path_phase=25,
                filtered_normal_n=force,
                source_sequences={"packet": sample_index + 1, "rtde": sample_index + 1},
                source_ages_s={"packet": 0.001, "rtde": 0.001},
            )
            builder.add(sample)
            samples.append(sample)
        return builder.finalize(provenance="raw_path_evidence"), tuple(samples)

    ledger = ObservationLedger(
        tmp_path / "cuda-observations.jsonl",
        campaign_fingerprint=contract.campaign_fingerprint,
        eoat_sha256="c" * 64,
    )
    for index in range(1, 7):
        objective, samples = raw_fixture(5.0 + 1.0 + index / 100.0)
        ledger.append(ObservationRecord(
            campaign_fingerprint=contract.campaign_fingerprint,
            epoch=1,
            attempt_sequence=index,
            kind="BOOTSTRAP_PD",
            candidate=Candidate(),
            safe_return=True,
            binding_ok=True,
            safety_gate=True,
            contact_gate=True,
            return_gate=True,
            motion_gate=True,
            timing_gate=True,
            identity_gate=True,
            qualification_passed=False,
            force_objective=objective,
            metrics={"offline": True, "execution_id": f"r005-cuda-execution-{index}"},
            raw_path_samples=samples,
            sealed=True,
        ))
    observations = ledger.records
    pending = (Candidate(force_p_gain=P_ANCHOR * 2.0**0.25),)
    result = V4BoAdapter(seed=7005, ledger=ledger).ask(
        observations=observations,
        pending=pending,
        incumbent=Candidate(),
    )
    metadata = dict(result.metadata)
    attestation = metadata["child_attestation"]
    resolved = resolve_optimizer_runtime()
    assert attestation == resolved.expected_attestation()
    assert metadata["device"] == "cuda:0"
    assert metadata["x_pending_count"] == 1
    assert metadata["x_pending_shape"] == [1, 7]
    assert metadata["training_observation_count"] == 6
    assert "torch" not in parent_sys.modules
