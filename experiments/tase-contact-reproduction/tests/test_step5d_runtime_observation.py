from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import time
from types import SimpleNamespace
from typing import Any, Mapping, Sequence

import pytest


ROOT = Path(__file__).resolve().parents[1]
TOOLS = ROOT / "tools"
RUNTIME_SRC = ROOT.parents[1] / "src/ur10e_experiment_runtime"
for import_root in (TOOLS, RUNTIME_SRC):
    if str(import_root) not in sys.path:
        sys.path.insert(0, str(import_root))


from step5d_autotune_v3.governance import (
    CurrentReleaseSnapshot,
    build_campaign_lease,
    reduce_observed_attestation,
    validate_observed_attestation,
)
from step5d_autotune_v3.qualification import (
    CONTENT_BINDING_SCHEMA,
    QUALIFICATION_RESULT_SCHEMA,
    production_process_role_paths,
    production_process_tree_fingerprint,
    validate_content_binding,
)
from step5d_autotune_v3 import runtime_observation as observation
from step5d_autotune_v3.runtime_observation import (
    RuntimeObservationError,
    RuntimeObservationPublisher,
)


REAL_PROC_OBSERVATION = observation._proc_observation
REAL_DISCOVER_WRITER_PROCESSES = observation._discover_writer_processes
NOW_NS = 2_000_000_000_000_000_000
CANONICAL_PID = 199
SUPERVISOR_PID = 200
BRIDGE_PID = 201
RUNNER_PID = 202
PROCESS_PIDS = {
    "canonical_launcher": CANONICAL_PID,
    "launcher_supervisor": SUPERVISOR_PID,
    "bridge_wrapper": BRIDGE_PID,
    "campaign_runner": RUNNER_PID,
}


def canonical_bytes(value: Any) -> bytes:
    return (
        json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        )
        + "\n"
    ).encode("ascii")


def digest(label: str) -> str:
    return hashlib.sha256(label.encode("ascii")).hexdigest()


def file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def release() -> CurrentReleaseSnapshot:
    return CurrentReleaseSnapshot(
        manifest_sha256=digest("manifest"),
        manifest_path=f"config/step5d/releases/{digest('manifest')}/manifest.json",
        program_id="step5d_strict_rnn_autotune_v3_r012",
        release_stage_id="step5d_strict_rnn_autotune_v3",
        source_fingerprint=digest("release-source"),
        launcher_sha256=file_sha256(ROOT / "scripts/step5d-autotune-v3.sh"),
        safety_envelope_sha256=digest("safety"),
        expected_triplet_sha256={
            ".script": digest("script"),
            ".txt": digest("txt"),
            ".urp": digest("urp"),
        },
        expected_loaded_program=(
            "/programs/andyl/kunwei/step5/"
            "step5d_strict_rnn_autotune_v3_r012.urp"
        ),
        expected_tp_runtime_identity={
            "protocol_version": 1,
            "digest_hi": 123456,
            "digest_lo": 789012,
        },
        valid=True,
        error=None,
    )


def qualification_processes() -> list[dict[str, Any]]:
    role_paths = production_process_role_paths()
    role_profiles = {
        "canonical_launcher": None,
        "launcher_supervisor": "control",
        "bridge_wrapper": "control",
        "campaign_runner": "optimizer",
    }
    processes = []
    for role, relative in sorted(role_paths.items()):
        script = (ROOT / relative).resolve(strict=True)
        pid = PROCESS_PIDS[role]
        ppid = (
            1
            if role == "canonical_launcher"
            else CANONICAL_PID
            if role == "launcher_supervisor"
            else SUPERVISOR_PID
        )
        profile = role_profiles[role]
        argv0 = (
            "/usr/bin/bash"
            if profile is None
            else f"/runtime/{profile}/bin/python"
        )
        argv = [argv0, str(script), "--output-root", "/qualification/run"]
        processes.append(
            {
                "role": role,
                "pid": pid,
                "ppid": ppid,
                "starttime": pid + 1000,
                "executable": argv0,
                "argv": argv,
                "argv0": argv0,
                "argv_sha256": hashlib.sha256(canonical_bytes(argv)).hexdigest(),
                "runtime_profile": profile,
                "environment_id": (
                    None if profile is None else digest(f"{profile}-environment")
                ),
                "script": str(script),
                "script_sha256": file_sha256(script),
            }
        )
    return processes


def qualification_binding(current: CurrentReleaseSnapshot) -> dict[str, Any]:
    files = {
        "tools/run_step5d_autotune_v3_live.py": file_sha256(
            ROOT / "tools/run_step5d_autotune_v3_live.py"
        )
    }
    environment = {
        "python_executable": "/usr/bin/python3",
        "python_version": "3.test",
        "runtime_binding": {
            "schema": "step5d.autotune-v3/runtime-process-binding-v1",
            "bundle_id": digest("runtime-bundle"),
            "contract_sha256": digest("runtime-contract"),
            "lock_sha256": digest("runtime-lock"),
            "runtime_attestation_sha256": digest("runtime-attestation"),
            "gpu_uuid": "GPU-runtime-observation-fixture",
            "profiles": {
                profile: {
                    "environment_id": digest(f"{profile}-environment"),
                    "python_executable": f"/runtime/{profile}/bin/python",
                    "record_tree_sha256": digest(f"{profile}-record"),
                    "profile_tree_sha256": digest(f"{profile}-tree"),
                }
                for profile in ("control", "optimizer")
            },
        },
    }
    return {
        "schema": CONTENT_BINDING_SCHEMA,
        "manifest_sha256": current.manifest_sha256,
        "source": {
            "files": files,
            "files_fingerprint": hashlib.sha256(canonical_bytes(files)).hexdigest(),
            "fingerprint": current.source_fingerprint,
        },
        "environment": {
            "values": environment,
            "fingerprint": hashlib.sha256(canonical_bytes(environment)).hexdigest(),
        },
        "launcher": {
            "path": str((ROOT / "scripts/step5d-autotune-v3.sh").resolve()),
            "sha256": current.launcher_sha256,
        },
        "process_tree": {
            "complete": True,
            "processes": qualification_processes(),
            "fingerprint": production_process_tree_fingerprint(ROOT),
        },
        "endpoint_substitution": {
            "roles": ["dashboard", "kunwei", "rtde", "tp"],
            "endpoint_only": True,
            "motion_capable": False,
            "production_processes_retained": True,
            "ready_writer": "production_bridge",
        },
    }


def qualification_reference(
    root: Path,
    current: CurrentReleaseSnapshot,
    *,
    ok: bool = True,
    source_fingerprint: str | None = None,
) -> dict[str, str]:
    binding = qualification_binding(current)
    if source_fingerprint is not None:
        binding["source"]["fingerprint"] = source_fingerprint
    validate_content_binding(binding)
    payload = {
        "schema": QUALIFICATION_RESULT_SCHEMA,
        "ok": ok,
        "lifecycle_complete": ok,
        "state": "QUALIFIED" if ok else "BLOCKED",
        "reason_code": "QUALIFIED" if ok else "ENDPOINT_INJECTION_UNAVAILABLE",
        "binding": binding,
        "bridge": {"alive": ok},
        "play_row_sha256": digest("play-row") if ok else None,
        "first_arm_seq": 1 if ok else None,
        "trial_evidence_ref": {"sha256": digest("trial")} if ok else None,
        "next_arm_seq": 2 if ok else None,
        "events": [
            {
                "phase": "QUALIFIED" if ok else "BLOCKED",
                "observed_at_ns": NOW_NS - 10_000_000_000,
                "details": {},
            }
        ],
        "remaining_integration_seam": None if ok else "blocked",
    }
    path = root / "qualification.json"
    encoded = canonical_bytes(payload)
    path.write_bytes(encoded)
    return {"path": str(path), "sha256": hashlib.sha256(encoded).hexdigest()}


def governance_lease(current: CurrentReleaseSnapshot) -> dict[str, Any]:
    return build_campaign_lease(
        campaign_id="campaign-1",
        manifest_sha256=current.manifest_sha256,
        campaign_fingerprint=digest("campaign"),
        safety_envelope_sha256=current.safety_envelope_sha256,
        issued_at_unix_ns=NOW_NS - 20_000_000_000,
        expires_at_unix_ns=NOW_NS + 60_000_000_000,
        supervisor_pid=SUPERVISOR_PID,
        supervisor_starttime_ticks=SUPERVISOR_PID + 1000,
    )


def fake_process(
    pid: int,
    role: str,
    expected_script: Path,
) -> dict[str, Any]:
    return {
        "role": role,
        "pid": pid,
        "ppid": (
            1
            if role == "canonical_launcher"
            else CANONICAL_PID
            if role == "launcher_supervisor"
            else SUPERVISOR_PID
        ),
        "starttime_ticks": pid + 1000,
        "executable": "/usr/bin/python3",
        "argv": [
            "/usr/bin/python3",
            str(expected_script),
            "--output-root",
            f"/live/{pid}",
        ],
        "source": str(expected_script),
        "source_sha256": file_sha256(expected_script),
    }


def test_process_observation_resolves_relative_shell_launcher_from_process_cwd(
    tmp_path: Path,
) -> None:
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    launcher = scripts / "step5d-autotune-v3.sh"
    launcher.write_text(
        "#!/usr/bin/env bash\n"
        "while true; do sleep 0.1; done\n",
        encoding="utf-8",
    )
    launcher.chmod(0o755)
    process = subprocess.Popen(
        ["scripts/step5d-autotune-v3.sh"],
        cwd=tmp_path,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        deadline = time.monotonic() + 2.0
        while (
            b"step5d-autotune-v3.sh"
            not in Path(f"/proc/{process.pid}/cmdline").read_bytes()
            and time.monotonic() < deadline
        ):
            time.sleep(0.01)
        observed = REAL_PROC_OBSERVATION(
            process.pid,
            "canonical_launcher",
            launcher,
        )
    finally:
        process.terminate()
        process.wait(timeout=5.0)

    assert observed["pid"] == process.pid
    assert observed["source"] == str(launcher.resolve())
    assert observed["argv"][-1] == "scripts/step5d-autotune-v3.sh"


def fake_writer_processes(_experiment_root: Path) -> list[dict[str, Any]]:
    return [
        {
            "pid": BRIDGE_PID,
            "ppid": SUPERVISOR_PID,
            "starttime_ticks": BRIDGE_PID + 1000,
            "executable": "/usr/bin/python3",
            "argv": [
                "/usr/bin/python3",
                str((ROOT / "tools/run_step5d_autotune_v3_bridge.py").resolve()),
            ],
            "matched_sources": ["tools/run_step5d_autotune_v3_bridge.py"],
        }
    ]


def fake_qualification_validator(
    payload: Mapping[str, Any],
    *,
    experiment_root: Path,
    manifest_sha256: str,
    source_fingerprint: str,
    launcher_sha256: str,
) -> Mapping[str, Any]:
    del experiment_root
    if payload.get("ok") is not True or payload.get("lifecycle_complete") is not True:
        raise ValueError("qualification did not complete the production path")
    binding = payload["binding"]
    observed = {
        "manifest_sha256": binding["manifest_sha256"],
        "source_fingerprint": binding["source"]["fingerprint"],
        "launcher_sha256": binding["launcher"]["sha256"],
    }
    expected = {
        "manifest_sha256": manifest_sha256,
        "source_fingerprint": source_fingerprint,
        "launcher_sha256": launcher_sha256,
    }
    for name, expected_value in expected.items():
        if observed[name] != expected_value:
            raise ValueError(f"qualification {name} differs")
    return binding


@pytest.fixture(autouse=True)
def fake_proc(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(observation, "_proc_observation", fake_process)
    monkeypatch.setattr(
        observation, "_discover_writer_processes", fake_writer_processes
    )
    # Qualification's production-path contract has its own integration suite.
    # These publisher unit tests retain only the binding boundary they consume.
    monkeypatch.setattr(
        observation, "validate_qualification_result", fake_qualification_validator
    )


def start_publisher(tmp_path: Path) -> tuple[RuntimeObservationPublisher, CurrentReleaseSnapshot]:
    current = release()
    campaign = tmp_path / "campaign"
    return (
        RuntimeObservationPublisher.start(
            experiment_root=ROOT,
            campaign_root=campaign,
            run_id="run-1",
            release=current,
            qualification_evidence=qualification_reference(tmp_path, current),
            lease=governance_lease(current),
        ),
        current,
    )


def recipe() -> tuple[list[str], list[str]]:
    fields = [f"output_int_register_{index}" for index in range(24, 38)]
    return fields, ["INT32"] * len(fields)


def bridge_csv(path: Path, *, identity: tuple[int, int, int] = (1, 123456, 789012)) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "sensor_age_s,ur_output_int_register_35,ur_output_int_register_36,"
        "ur_output_int_register_37\n"
        f"0.010,{identity[0]},{identity[1]},{identity[2]}\n",
        encoding="utf-8",
    )


def delivery(current: CurrentReleaseSnapshot) -> dict[str, Any]:
    return {
        "schema": "step5d.autotune-v3/delivery-observation-v1",
        "transaction_id": "d" * 32,
        "fresh_controller_checked_at": datetime.fromtimestamp(
            (NOW_NS - 50_000_000) / 1_000_000_000,
            tz=timezone.utc,
        ).isoformat(),
        "release_manifest_sha256": current.manifest_sha256,
        "triplet_sha256": current.expected_triplet_sha256,
    }


def publish(
    publisher: RuntimeObservationPublisher,
    current: CurrentReleaseSnapshot,
    csv_path: Path,
    *,
    observed_at: int = NOW_NS,
    writer_pids: Sequence[int] = (BRIDGE_PID,),
    exited_processes: Mapping[str, int] | None = None,
) -> dict[str, Any]:
    fields, types = recipe()
    return publisher.publish(
        bridge_pid=BRIDGE_PID,
        bridge_starttime_ticks=BRIDGE_PID + 1000,
        bridge_csv=csv_path,
        process_pids=PROCESS_PIDS,
        writer_pids=writer_pids,
        uploaded_triplet_sha256=current.expected_triplet_sha256,
        readback_triplet_sha256=current.expected_triplet_sha256,
        delivery_observation=delivery(current),
        dashboard_result={
            "get loaded program": f"Loaded program: {current.expected_loaded_program}"
        },
        controller_observed_at_unix_ns=observed_at - 20_000_000,
        rtde_output_fields=fields,
        rtde_output_types=types,
        mailbox={
            "observed_at_unix_ns": observed_at - 10_000_000,
            "pending_arm_sequence": None,
            "duplicate_arm_detected": False,
        },
        observed_at_unix_ns=observed_at,
        exited_processes=exited_processes,
    )


def assert_reference(root: Path, reference: Mapping[str, str]) -> None:
    path = Path(reference["path"])
    assert not path.is_absolute()
    absolute = root / path
    assert absolute.is_file()
    assert hashlib.sha256(absolute.read_bytes()).hexdigest() == reference["sha256"]


def test_publish_materializes_strict_relative_evidence_and_no_readiness(
    tmp_path: Path,
) -> None:
    publisher, current = start_publisher(tmp_path)
    csv_path = tmp_path / "bridge/bridge_rtde_500hz.csv"
    bridge_csv(csv_path)

    result = publish(publisher, current, csv_path)
    row = validate_observed_attestation(result["attestation"])

    assert row["sequence"] == 1
    assert row["controller"]["tp_runtime_identity"] == current.expected_tp_runtime_identity
    assert row["controller"]["loaded_program"] == current.expected_loaded_program
    assert row["process"]["process_tree_fingerprint"] == (
        production_process_tree_fingerprint(ROOT)
    )
    for reference in (
        row["offline"]["evidence"],
        row["process"]["evidence"],
        row["controller"]["evidence"],
        row["controller"]["delivery_observation"],
        row["mailbox"]["evidence"],
    ):
        assert_reference(publisher.campaign_root, reference)
    assert not any("ready" in path.name for path in publisher.campaign_root.rglob("*"))
    assert "bench_ready" not in row


def test_csv_follower_ignores_partial_row_and_sequence_is_monotonic(
    tmp_path: Path,
) -> None:
    publisher, current = start_publisher(tmp_path)
    csv_path = tmp_path / "bridge.csv"
    bridge_csv(csv_path)
    with csv_path.open("a", encoding="utf-8") as handle:
        handle.write("0.0,1,9,8")

    first = publish(publisher, current, csv_path, observed_at=NOW_NS)
    assert first["attestation"]["controller"]["tp_runtime_identity"] == (
        current.expected_tp_runtime_identity
    )

    with csv_path.open("a", encoding="utf-8") as handle:
        handle.write("\n")
    second = publish(
        publisher,
        current,
        csv_path,
        observed_at=NOW_NS + 100_000_000,
    )
    assert second["attestation"]["sequence"] == 2
    assert second["attestation"]["controller"]["tp_runtime_identity"] == {
        "protocol_version": 1,
        "digest_hi": 9,
        "digest_lo": 8,
    }
    assert second["pointer"]["sequence"] == 2


def test_wrong_runtime_identity_is_published_as_observation_but_fails_bench(
    tmp_path: Path,
) -> None:
    publisher, current = start_publisher(tmp_path)
    csv_path = tmp_path / "wrong.csv"
    bridge_csv(csv_path, identity=(1, 999, 888))
    publisher.update_lifecycle(
        "waiting_for_play", observed_at_unix_ns=NOW_NS - 3_000_000_000
    )
    publisher.update_lifecycle(
        "play_observed", observed_at_unix_ns=NOW_NS - 2_000_000_000
    )
    publisher.update_lifecycle(
        "play_identity_recheck",
        observed_at_unix_ns=NOW_NS - 1_000_000_000,
        readback_triplet_sha256=current.expected_triplet_sha256,
        loaded_program=current.expected_loaded_program,
        tp_runtime_identity={
            "protocol_version": 1,
            "digest_hi": 999,
            "digest_lo": 888,
        },
    )

    row = publish(publisher, current, csv_path)["attestation"]
    status = reduce_observed_attestation(
        current,
        row,
        campaign_root=publisher.campaign_root,
        now_ns=NOW_NS,
        proc_starttime_reader=lambda pid: {
            SUPERVISOR_PID: SUPERVISOR_PID + 1000,
            BRIDGE_PID: BRIDGE_PID + 1000,
            RUNNER_PID: RUNNER_PID + 1000,
        }.get(pid),
    )

    assert status["predicates"]["tp_runtime_identity_verified"] is False
    assert status["predicates"]["bench_ready"] is False
    assert "TP_RUNTIME_IDENTITY_MISMATCH" in status["blocker"]["reason_codes"]


def test_missing_csv_and_mailbox_remain_machine_observed_fail_closed(
    tmp_path: Path,
) -> None:
    publisher, current = start_publisher(tmp_path)
    fields, types = recipe()
    result = publisher.publish(
        bridge_pid=BRIDGE_PID,
        bridge_starttime_ticks=BRIDGE_PID + 1000,
        bridge_csv=tmp_path / "missing.csv",
        process_pids=PROCESS_PIDS,
        writer_pids=[BRIDGE_PID],
        uploaded_triplet_sha256=current.expected_triplet_sha256,
        readback_triplet_sha256=current.expected_triplet_sha256,
        delivery_observation=delivery(current),
        dashboard_result=None,
        controller_observed_at_unix_ns=None,
        rtde_output_fields=fields,
        rtde_output_types=types,
        mailbox=None,
        observed_at_unix_ns=NOW_NS,
    )
    row = result["attestation"]
    assert row["process"]["heartbeat_at_unix_ns"] is None
    assert row["controller"]["tp_runtime_identity"] is None
    assert row["mailbox"]["observed_at_unix_ns"] is None


def test_lifecycle_updates_are_ordered_and_trial_evidence_is_imported(
    tmp_path: Path,
) -> None:
    publisher, current = start_publisher(tmp_path)
    trial = tmp_path / "trial.json"
    trial.write_text('{"safe_closure":true}\n', encoding="utf-8")
    publisher.update_lifecycle(
        "waiting_for_play", observed_at_unix_ns=NOW_NS - 6_000_000_000
    )
    publisher.update_lifecycle(
        "play_observed", observed_at_unix_ns=NOW_NS - 5_000_000_000
    )
    publisher.update_lifecycle(
        "play_identity_recheck",
        observed_at_unix_ns=NOW_NS - 4_000_000_000,
        readback_triplet_sha256=current.expected_triplet_sha256,
        loaded_program=current.expected_loaded_program,
        tp_runtime_identity=current.expected_tp_runtime_identity,
    )
    publisher.update_lifecycle(
        "first_arm_ack",
        observed_at_unix_ns=NOW_NS - 3_000_000_000,
        sequence=1,
    )
    publisher.update_lifecycle(
        "trial_completion",
        observed_at_unix_ns=NOW_NS - 2_000_000_000,
        trial_id="trial-1",
        evidence=trial,
    )
    publisher.update_lifecycle(
        "next_arm_ack",
        observed_at_unix_ns=NOW_NS - 1_000_000_000,
        sequence=2,
    )
    csv_path = tmp_path / "bridge.csv"
    bridge_csv(csv_path)

    row = publish(publisher, current, csv_path)["attestation"]
    assert row["events"]["next_arm_ack"]["sequence"] == 2
    assert_reference(
        publisher.campaign_root, row["events"]["trial_completion"]["evidence"]
    )
    publisher.revoke_lease(
        observed_at_unix_ns=NOW_NS + 1,
        reason="campaign_terminal",
    )
    publisher.update_lifecycle(
        "campaign_terminal",
        observed_at_unix_ns=NOW_NS + 2,
        reason="campaign_complete",
        runner_exit_code=0,
    )
    terminal_row = publish(
        publisher,
        current,
        csv_path,
        observed_at=NOW_NS + 3,
    )["attestation"]
    terminal_status = reduce_observed_attestation(
        current,
        terminal_row,
        campaign_root=publisher.campaign_root,
        now_ns=NOW_NS + 4,
        proc_starttime_reader=lambda pid: {
            SUPERVISOR_PID: SUPERVISOR_PID + 1000,
            BRIDGE_PID: BRIDGE_PID + 1000,
            RUNNER_PID: RUNNER_PID + 1000,
        }.get(pid),
    )
    assert terminal_status["terminal"]["completed"] is True
    assert terminal_status["outcome"]["live_proven"] is True
    assert terminal_status["blocker"]["reason_codes"] == []
    assert terminal_status["next_action"] == "campaign_complete"
    with pytest.raises(RuntimeObservationError, match="backwards"):
        publisher.update_lifecycle(
            "play_observed", observed_at_unix_ns=NOW_NS - 500_000_000
        )


@pytest.mark.parametrize(
    ("ok", "source_fingerprint", "message"),
    [
        (False, None, "did not complete"),
        (True, digest("different-source"), "source_fingerprint differs"),
    ],
)
def test_start_rejects_unqualified_or_different_source_without_pointer(
    tmp_path: Path,
    ok: bool,
    source_fingerprint: str | None,
    message: str,
) -> None:
    current = release()
    campaign = tmp_path / "campaign"
    reference = qualification_reference(
        tmp_path, current, ok=ok, source_fingerprint=source_fingerprint
    )

    with pytest.raises(RuntimeObservationError, match=message):
        RuntimeObservationPublisher.start(
            experiment_root=ROOT,
            campaign_root=campaign,
            run_id="run-1",
            release=current,
            qualification_evidence=reference,
            lease=governance_lease(current),
        )
    assert not (campaign / "governance/current.json").exists()


def test_process_parentage_or_bridge_identity_cannot_be_attested(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    publisher, current = start_publisher(tmp_path)
    csv_path = tmp_path / "bridge.csv"
    bridge_csv(csv_path)

    def wrong_parent(pid: int, role: str, script: Path) -> dict[str, Any]:
        result = fake_process(pid, role, script)
        if role == "campaign_runner":
            result["ppid"] = 999
        return result

    monkeypatch.setattr(observation, "_proc_observation", wrong_parent)
    with pytest.raises(RuntimeObservationError, match="parentage"):
        publish(publisher, current, csv_path)
    assert not (publisher.campaign_root / "governance/current.json").exists()

    monkeypatch.setattr(observation, "_proc_observation", fake_process)
    fields, types = recipe()
    with pytest.raises(RuntimeObservationError, match="PID/starttime"):
        publisher.publish(
            bridge_pid=BRIDGE_PID,
            bridge_starttime_ticks=9999,
            bridge_csv=csv_path,
            process_pids=PROCESS_PIDS,
            writer_pids=[BRIDGE_PID],
            uploaded_triplet_sha256=current.expected_triplet_sha256,
            readback_triplet_sha256=current.expected_triplet_sha256,
            delivery_observation=delivery(current),
            dashboard_result=None,
            controller_observed_at_unix_ns=None,
            rtde_output_fields=fields,
            rtde_output_types=types,
            mailbox=None,
            observed_at_unix_ns=NOW_NS,
        )


def test_zero_exit_runner_uses_only_previously_published_process_provenance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    publisher, current = start_publisher(tmp_path)
    csv_path = tmp_path / "bridge.csv"
    bridge_csv(csv_path)
    publish(publisher, current, csv_path)

    def exited_runner(pid: int, role: str, script: Path) -> dict[str, Any]:
        if role == "campaign_runner":
            raise RuntimeObservationError("runner exited")
        return fake_process(pid, role, script)

    monkeypatch.setattr(observation, "_proc_observation", exited_runner)
    monkeypatch.setattr(
        observation, "_proc_entry_exists", lambda pid: pid != RUNNER_PID
    )
    result = publish(
        publisher,
        current,
        csv_path,
        observed_at=NOW_NS + 1,
        exited_processes={"campaign_runner": 0},
    )
    process_ref = result["attestation"]["process"]["evidence"]
    evidence = json.loads(
        (publisher.campaign_root / process_ref["path"]).read_text(encoding="utf-8")
    )
    runner = next(
        process
        for process in evidence["processes"]
        if process["role"] == "campaign_runner"
    )
    assert runner["exit_code"] == 0
    assert runner["observed_terminal_exit"] is True
    assert runner["pid"] == RUNNER_PID
    assert runner["starttime_ticks"] == RUNNER_PID + 1000


def test_exited_runner_cache_cannot_be_invented_or_used_for_nonzero_exit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    publisher, current = start_publisher(tmp_path)
    csv_path = tmp_path / "bridge.csv"
    bridge_csv(csv_path)

    def exited_runner(pid: int, role: str, script: Path) -> dict[str, Any]:
        if role == "campaign_runner":
            raise RuntimeObservationError("runner exited")
        return fake_process(pid, role, script)

    monkeypatch.setattr(observation, "_proc_observation", exited_runner)
    monkeypatch.setattr(
        observation, "_proc_entry_exists", lambda pid: pid != RUNNER_PID
    )
    with pytest.raises(RuntimeObservationError, match="cached campaign_runner"):
        publish(
            publisher,
            current,
            csv_path,
            exited_processes={"campaign_runner": 0},
        )

    monkeypatch.setattr(observation, "_proc_observation", fake_process)
    publish(publisher, current, csv_path)
    monkeypatch.setattr(observation, "_proc_observation", exited_runner)
    with pytest.raises(RuntimeObservationError, match="only a zero-exit"):
        publish(
            publisher,
            current,
            csv_path,
            observed_at=NOW_NS + 1,
            exited_processes={"campaign_runner": 1},
        )
    monkeypatch.setattr(observation, "_proc_entry_exists", lambda _pid: True)
    with pytest.raises(RuntimeObservationError, match="runner exited"):
        publish(
            publisher,
            current,
            csv_path,
            observed_at=NOW_NS + 2,
            exited_processes={"campaign_runner": 0},
        )


def test_single_writer_evidence_is_independently_discovered_from_proc(
    tmp_path: Path,
) -> None:
    publisher, current = start_publisher(tmp_path)
    csv_path = tmp_path / "bridge.csv"
    bridge_csv(csv_path)

    row = publish(publisher, current, csv_path)["attestation"]
    process_reference = row["process"]["evidence"]
    evidence = json.loads(
        (publisher.campaign_root / process_reference["path"]).read_text(
            encoding="utf-8"
        )
    )

    assert row["process"]["writer_pids"] == [BRIDGE_PID]
    assert evidence["writer_pids"] == [BRIDGE_PID]
    assert evidence["writer_processes"] == fake_writer_processes(ROOT)


def test_proc_writer_discovery_matches_absolute_and_relative_python_entrypoints(
    tmp_path: Path,
) -> None:
    proc_root = tmp_path / "proc"
    proc_root.mkdir()

    def process(
        pid: int, *, argv: Sequence[str], ppid: int = 1, starttime: int
    ) -> None:
        proc = proc_root / str(pid)
        proc.mkdir()
        suffix = ["S", str(ppid), *("0" for _ in range(17)), str(starttime)]
        (proc / "stat").write_text(
            f"{pid} (python3) {' '.join(suffix)}\n", encoding="ascii"
        )
        (proc / "cmdline").write_bytes(b"\0".join(item.encode() for item in argv) + b"\0")
        (proc / "cwd").symlink_to(ROOT, target_is_directory=True)
        (proc / "exe").symlink_to("/usr/bin/python3")

    process(
        BRIDGE_PID,
        argv=[
            "/usr/bin/python3",
            str((ROOT / "tools/run_step5d_autotune_v3_bridge.py").resolve()),
        ],
        starttime=BRIDGE_PID + 1000,
    )
    process(
        999,
        argv=["python3", "tools/kunwei_rtde_bridge.py"],
        starttime=1999,
    )
    process(
        1000,
        argv=["vim", str(ROOT / "tools/kunwei_rtde_bridge.py")],
        starttime=2000,
    )

    rows = REAL_DISCOVER_WRITER_PROCESSES(ROOT, proc_root=proc_root)

    assert [row["pid"] for row in rows] == [BRIDGE_PID, 999]
    assert rows[0]["matched_sources"] == [
        "tools/run_step5d_autotune_v3_bridge.py"
    ]
    assert rows[1]["matched_sources"] == ["tools/kunwei_rtde_bridge.py"]


def test_extra_machine_discovered_writer_fails_even_when_caller_claims_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    publisher, current = start_publisher(tmp_path)
    csv_path = tmp_path / "bridge.csv"
    bridge_csv(csv_path)
    extra = {
        "pid": 999,
        "ppid": 1,
        "starttime_ticks": 1999,
        "executable": "/usr/bin/python3",
        "argv": ["python3", str(ROOT / "tools/kunwei_rtde_bridge.py")],
        "matched_sources": ["tools/kunwei_rtde_bridge.py"],
    }
    monkeypatch.setattr(
        observation,
        "_discover_writer_processes",
        lambda _root: [*fake_writer_processes(ROOT), extra],
    )

    with pytest.raises(RuntimeObservationError, match="exactly one production bridge"):
        publish(publisher, current, csv_path, writer_pids=[BRIDGE_PID])
    assert not (publisher.campaign_root / "governance/current.json").exists()


def test_caller_writer_claim_cannot_override_machine_discovery(
    tmp_path: Path,
) -> None:
    publisher, current = start_publisher(tmp_path)
    csv_path = tmp_path / "bridge.csv"
    bridge_csv(csv_path)

    with pytest.raises(RuntimeObservationError, match="claim differs"):
        publish(publisher, current, csv_path, writer_pids=[999])
    assert not (publisher.campaign_root / "governance/current.json").exists()


def test_runtime_lease_is_converted_without_reusing_it_as_safety_boundary(
    tmp_path: Path,
) -> None:
    current = release()
    runtime_lease = SimpleNamespace(
        campaign_id="campaign-1",
        manifest_sha256=current.manifest_sha256,
        campaign_fingerprint=digest("campaign"),
        safety_envelope_sha256=current.safety_envelope_sha256,
        supervisor_pid=SUPERVISOR_PID,
        supervisor_starttime=SUPERVISOR_PID + 1000,
        issued_at="2033-05-18T03:33:00+00:00",
    )
    publisher = RuntimeObservationPublisher.start(
        experiment_root=ROOT,
        campaign_root=tmp_path / "campaign",
        run_id="run-1",
        release=current,
        qualification_evidence=qualification_reference(tmp_path, current),
        lease=runtime_lease,
        lease_expires_at_unix_ns=NOW_NS + 60_000_000_000,
    )

    assert publisher.lease["campaign_id"] == runtime_lease.campaign_id
    publisher.revoke_lease(
        observed_at_unix_ns=NOW_NS + 1,
        reason="user_stop",
    )
    assert publisher.lease["revocation_reason"] == "user_stop"


def test_external_blocker_evidence_is_copied_and_governance_validated(
    tmp_path: Path,
) -> None:
    publisher, current = start_publisher(tmp_path)
    csv_path = tmp_path / "bridge.csv"
    bridge_csv(csv_path)
    outage = tmp_path / "outage.json"
    outage.write_text('{"port":30004,"reachable":false}\n', encoding="utf-8")
    fields, types = recipe()

    result = publisher.publish(
        bridge_pid=BRIDGE_PID,
        bridge_starttime_ticks=BRIDGE_PID + 1000,
        bridge_csv=csv_path,
        process_pids=PROCESS_PIDS,
        writer_pids=[BRIDGE_PID],
        uploaded_triplet_sha256=current.expected_triplet_sha256,
        readback_triplet_sha256=current.expected_triplet_sha256,
        delivery_observation=delivery(current),
        dashboard_result=None,
        controller_observed_at_unix_ns=None,
        rtde_output_fields=fields,
        rtde_output_types=types,
        mailbox=None,
        observed_at_unix_ns=NOW_NS,
        external_blocker={
            "reason_code": "RTDE_UNREACHABLE",
            "observed_at_unix_ns": NOW_NS - 1,
            "evidence": outage,
        },
    )

    external = result["attestation"]["external_blocker"]
    assert external["reason_code"] == "RTDE_UNREACHABLE"
    assert_reference(publisher.campaign_root, external["evidence"])
