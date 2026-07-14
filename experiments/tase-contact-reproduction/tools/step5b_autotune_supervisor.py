#!/usr/bin/env python3
"""Persistent one-writer-at-a-time supervisor for Step5b TP autotuning.

This tool never loads or starts a TP program. ``start`` is live/contact gated;
the operator must already have opened the verified package in Local Control and
must press Play manually after the supervisor announces that the first bridge
child is ready.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import fcntl
import hashlib
import json
import math
import multiprocessing
import os
import signal
import secrets
import socket
import struct
import subprocess
import sys
import time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator

from step5b_autotune_contract import (
    COMMAND_ARM,
    COMMAND_HOLD,
    COMMAND_STOP,
    CONFIRMATION_TOKEN,
    CONTROLLER_PROGRAM,
    EXPERIMENT_ROOT,
    FATAL_SESSION_REASONS,
    PROGRAM_BASENAME,
    TP_FAULT,
    TP_HOME,
    TP_RUN,
    TP_STATE_NAMES,
    Candidate,
    bridge_command,
    candidate_token_low31,
    load_contract,
)
from step5b_autotune_evaluator import evaluate_run
from step5b_autotune_evidence import (
    BACKEND_ID,
    EVALUATION_SCHEMA,
    OBJECTIVE_NAME,
    OBJECTIVE_UNIT,
    atomic_write_json,
    fingerprint_core,
    fingerprint_record,
    physical_capture_uid_from_sha256,
    quarantine_evidence,
    read_evaluation_jsonl,
    sha256_file,
    source_config_fingerprint,
    trial_spec_payload,
)
from step5b_autotune_optimizer import choose_candidate, read_observations
from step5b_autotune_promotion import write_promotion_artifacts


REALSETUP_SCRIPTS = Path("/home/andy/codex-private-skills-shared-main/skills/ur10e-realsetup/scripts")
sys.path.insert(0, str(REALSETUP_SCRIPTS))
from _ur_common import RTDEClient, dashboard_exchange  # noqa: E402


ROBOT_HOST = "192.168.1.18"
RUNS_ROOT = EXPERIMENT_ROOT / "runs" / "step5b_autotune_sessions"
ACTIVE_POINTER = EXPERIMENT_ROOT / "runs" / ".step5b_autotune_active.json"
DELIVERY_EVIDENCE = EXPERIMENT_ROOT / "config" / "step5b_autotune_delivery_v2.json"
AUTHORIZATION_LEDGER = EXPERIMENT_ROOT / "config" / "step5b_tp_autotune_authorization_v2.json"
VENV_PYTHON = EXPERIMENT_ROOT / ".venv-step5b-autotune" / "bin" / "python"
INT_INPUT_FIELDS = [f"input_int_register_{index}" for index in range(24, 28)]
OBSERVE_FIELDS = [
    "actual_TCP_pose",
    "actual_q",
    "runtime_state",
    "robot_mode",
    "safety_mode",
    *[f"output_int_register_{index}" for index in range(24, 29)],
    "output_double_register_30",
    "output_double_register_35",
]
RUNTIME_ROOT = Path(os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")) / "ur10e"
SESSION_LOCK_PATH = RUNTIME_ROOT / "step5b_autotune_session.lock"
LIVE_WRITER_LOCK_PATH = RUNTIME_ROOT / "live_writer.lock"


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path)


def append_jsonl(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, sort_keys=True) + "\n")


@contextmanager
def exclusive_lock(path: Path) -> Iterator[Any]:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a+", encoding="utf-8")
    try:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError(f"lock busy: {path}") from exc
        handle.seek(0)
        handle.truncate()
        handle.write(json.dumps({"pid": os.getpid(), "acquired_at": now_iso()}) + "\n")
        handle.flush()
        yield handle
    finally:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()


def lock_available(path: Path) -> tuple[bool, str]:
    try:
        with exclusive_lock(path):
            return True, "available"
    except (OSError, RuntimeError) as exc:
        return False, str(exc)


def physical_cpu_workers() -> int:
    logical = os.cpu_count() or 1
    return min(16, max(1, logical // 2))


class AutotuneRTDEClient(RTDEClient):
    def setup_inputs(self, fields: list[str]) -> tuple[int, list[str]]:
        self._send_packet("I", ",".join(fields).encode())
        packet_type, data = self._recv_packet()
        if packet_type != ord("I"):
            raise RuntimeError(f"unexpected RTDE input setup response {packet_type}")
        recipe_id = data[0]
        type_names = data[1:].decode("ascii", errors="replace").split(",")
        if recipe_id == 0 or any(name == "NOT_FOUND" for name in type_names):
            raise RuntimeError(f"invalid RTDE integer recipe: id={recipe_id} types={type_names}")
        return recipe_id, type_names

    def send_ints(self, recipe_id: int, type_names: list[str], values: list[int]) -> None:
        if type_names != ["INT32"] * len(values):
            raise RuntimeError(f"unexpected RTDE integer types: {type_names}")
        payload = bytearray([recipe_id])
        for value in values:
            payload.extend(struct.pack("!i", int(value)))
        self._send_packet("U", bytes(payload))


def write_handshake(session_epoch: int, trial_id: int, command: int, token: int) -> None:
    with AutotuneRTDEClient(ROBOT_HOST, timeout=2.0) as client:
        client.negotiate()
        recipe_id, type_names = client.setup_inputs(INT_INPUT_FIELDS)
        client.start()
        client.send_ints(recipe_id, type_names, [session_epoch, trial_id, command, token])
        time.sleep(0.05)


@contextmanager
def tp_observer() -> Iterator[tuple[RTDEClient, int, list[str]]]:
    with RTDEClient(ROBOT_HOST, timeout=2.0) as client:
        client.negotiate()
        recipe_id, type_names = client.setup_outputs(20.0, OBSERVE_FIELDS)
        client.start()
        yield client, recipe_id, type_names


def observation_dict(client: RTDEClient, recipe_id: int, type_names: list[str]) -> dict[str, Any]:
    values = client.recv_recipe_sample(recipe_id, type_names)
    return dict(zip(OBSERVE_FIELDS, values))


def handshake_progress(
    sample: dict[str, Any],
    *,
    session_epoch: int,
    trial_id: int,
    token: int,
    saw_run: bool,
    home_release_sent: bool,
) -> tuple[bool, bool, bool, int, int]:
    ack_epoch = int(sample.get("output_int_register_24", 0))
    ack_trial = int(sample.get("output_int_register_25", -1))
    state = int(sample.get("output_int_register_26", 0))
    reason = int(sample.get("output_int_register_27", 0))
    ack_token = int(sample.get("output_int_register_28", 0))
    exact = ack_epoch == session_epoch and ack_trial == trial_id and ack_token == token
    fresh_run = saw_run or (exact and state == TP_RUN)
    fresh_home = bool(exact and fresh_run and state == TP_HOME)
    release_ack = bool(exact and home_release_sent and state == 10)
    return fresh_run, fresh_home, release_ack, state if exact else 0, reason if exact else 0


def dashboard_snapshot() -> dict[str, str]:
    return dashboard_exchange(
        ROBOT_HOST,
        ["get loaded program", "safetymode", "robotmode", "running", "programState"],
        timeout=2.0,
    )


def local_bridge_processes() -> list[str]:
    completed = subprocess.run(
        ["pgrep", "-af", "[k]unwei_rtde_bridge.py"],
        check=False,
        capture_output=True,
        text=True,
    )
    return [line for line in completed.stdout.splitlines() if line.strip()]


def verify_delivery_evidence() -> tuple[bool, str]:
    if not DELIVERY_EVIDENCE.is_file():
        return False, f"missing {DELIVERY_EVIDENCE}"
    try:
        evidence = json.loads(DELIVERY_EVIDENCE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return False, f"delivery evidence unreadable: {type(exc).__name__}"
    if not evidence.get("controller_readback_verified"):
        return False, "controller read-back is not verified"
    if evidence.get("basename") != PROGRAM_BASENAME:
        return False, "delivery evidence basename mismatch"
    artifacts = evidence.get("artifacts")
    if not isinstance(artifacts, list) or len(artifacts) != 3:
        return False, "delivery evidence must bind exactly three artifacts"

    expected_names = {f"{PROGRAM_BASENAME}{suffix}" for suffix in (".script", ".txt", ".urp")}
    observed_names: set[str] = set()
    root = EXPERIMENT_ROOT.resolve()
    for artifact in artifacts:
        if not isinstance(artifact, dict):
            return False, "delivery artifact entry is not an object"
        expected_sha = artifact.get("sha256")
        if not isinstance(expected_sha, str) or len(expected_sha) != 64:
            return False, "delivery artifact SHA256 is invalid"
        for label, key in (("local package", "local_path"), ("fresh read-back", "readback_path")):
            raw_path = artifact.get(key)
            if not isinstance(raw_path, str) or not raw_path:
                return False, f"{label} path missing from delivery evidence"
            path = (EXPERIMENT_ROOT / raw_path).resolve()
            if path.parent == root or root not in path.parents:
                return False, f"{label} path escapes experiment root: {raw_path}"
            if not path.is_file() or path.is_symlink():
                return False, f"{label} artifact missing or unsafe: {path}"
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            if digest != expected_sha:
                return False, f"{label} SHA mismatch: {path.name}"
        observed_names.add(Path(artifact["local_path"]).name)

    if observed_names != expected_names:
        return False, "delivery evidence triplet basename mismatch"

    validation_path_raw = evidence.get("urp_validation_evidence")
    if not isinstance(validation_path_raw, str) or not validation_path_raw:
        return False, "URP read-back validation evidence is missing"
    validation_path = (EXPERIMENT_ROOT / validation_path_raw).resolve()
    if root not in validation_path.parents or not validation_path.is_file() or validation_path.is_symlink():
        return False, "URP read-back validation evidence is missing or unsafe"
    try:
        validation = json.loads(validation_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return False, f"URP read-back validation unreadable: {type(exc).__name__}"
    if not validation.get("pass") or validation.get("state") != "controller read-back verified":
        return False, "URP read-back validation did not pass"
    if validation.get("basename") != PROGRAM_BASENAME:
        return False, "URP read-back validation basename mismatch"
    return True, "controller read-back verified"


def dependency_status() -> tuple[bool, str]:
    if not VENV_PYTHON.is_file():
        return False, f"missing isolated environment {VENV_PYTHON.parent.parent}"
    completed = subprocess.run(
        [
            str(VENV_PYTHON),
            "-c",
            (
                "import torch,botorch,gpytorch; "
                "assert torch.cuda.is_available(), 'CUDA unavailable'; "
                "print(torch.__version__,botorch.__version__,gpytorch.__version__,torch.cuda.get_device_name(0))"
            ),
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    return completed.returncode == 0, (completed.stdout or completed.stderr).strip()


def gpu_capacity_status() -> dict[str, Any]:
    memory = subprocess.run(
        [
            "nvidia-smi",
            "--query-gpu=memory.total,memory.used",
            "--format=csv,noheader,nounits",
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=5,
    )
    if memory.returncode != 0 or not memory.stdout.strip():
        return {"ok": False, "detail": (memory.stderr or "nvidia-smi unavailable").strip()}
    total_mib, used_mib = [float(value.strip()) for value in memory.stdout.splitlines()[0].split(",")]
    compute = subprocess.run(
        [
            "nvidia-smi",
            "--query-compute-apps=pid",
            "--format=csv,noheader,nounits",
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=5,
    )
    workers = len([line for line in compute.stdout.splitlines() if line.strip().isdigit()])
    planned_optimizer_workers = 1
    predicted_mib_per_worker = 1024.0
    predicted_mib = planned_optimizer_workers * predicted_mib_per_worker
    projected_pct = 100.0 * (used_mib + predicted_mib) / total_mib
    return {
        "ok": (
            memory.returncode == 0
            and workers + planned_optimizer_workers <= 3
            and projected_pct <= 85.0
        ),
        "total_mib": total_mib,
        "used_mib": used_mib,
        "predicted_optimizer_mib": predicted_mib,
        "predicted_mib_per_worker": predicted_mib_per_worker,
        "planned_optimizer_workers": planned_optimizer_workers,
        "projected_vram_pct": projected_pct,
        "observed_compute_workers": workers,
        "max_compute_workers": 3,
        "vram_limit_pct": 85.0,
    }


def preflight(*, offline: bool) -> dict[str, Any]:
    contract = load_contract()
    delivery_ok, delivery_detail = verify_delivery_evidence()
    deps_ok, deps_detail = dependency_status()
    gpu = gpu_capacity_status()
    session_lock_ok, session_lock_detail = lock_available(SESSION_LOCK_PATH)
    live_lock_ok, live_lock_detail = lock_available(LIVE_WRITER_LOCK_PATH)
    authorization = json.loads(AUTHORIZATION_LEDGER.read_text(encoding="utf-8"))
    authorization_fields = (
        "live_authorized",
        "controller_delivery_authorized",
        "controller_readback_verified",
        "bridge_start_authorized",
        "tp_play_authorized",
        "contact_motion_authorized",
    )
    authorization_ok = all(authorization.get(field) is True for field in authorization_fields)
    result: dict[str, Any] = {
        "ok": False,
        "at": now_iso(),
        "mode": "offline" if offline else "bench_read_only",
        "stage_id": contract["stage_id"],
        "current_stage_unchanged": contract["isolation"]["current_stage_unchanged"],
        "delivery": {"ok": delivery_ok, "detail": delivery_detail},
        "dependencies": {"ok": deps_ok, "detail": deps_detail},
        "gpu": gpu,
        "bridge_processes": local_bridge_processes(),
        "authorization": {**authorization, "ok": authorization_ok, "required_true": list(authorization_fields)},
        "locks": {
            "session": {"ok": session_lock_ok, "detail": session_lock_detail, "path": str(SESSION_LOCK_PATH)},
            "live_writer": {"ok": live_lock_ok, "detail": live_lock_detail, "path": str(LIVE_WRITER_LOCK_PATH)},
        },
    }
    if offline:
        result["ok"] = all(
            (delivery_ok, deps_ok, bool(gpu.get("ok")), authorization_ok, session_lock_ok, live_lock_ok, not result["bridge_processes"])
        )
        return result
    snapshot = dashboard_snapshot()
    loaded = snapshot.get("get loaded program", "")
    safety_ok = "NORMAL" in snapshot.get("safetymode", "")
    program_ok = CONTROLLER_PROGRAM in loaded
    result["dashboard"] = snapshot
    result["loaded_program_ok"] = program_ok
    result["safety_normal"] = safety_ok
    result["ok"] = all(
        (delivery_ok, deps_ok, gpu["ok"], authorization_ok, session_lock_ok, live_lock_ok, not result["bridge_processes"], program_ok, safety_ok)
    )
    return result


def historical_run_roots() -> list[Path]:
    roots = [EXPERIMENT_ROOT / "runs"]
    main_root = Path("/home/andy/ur10e_ros2_ws/experiments/tase-contact-reproduction/runs")
    if main_root.resolve() != roots[0].resolve():
        roots.append(main_root)
    return roots


def observation_history_paths() -> list[Path]:
    seen: set[str] = set()
    paths: list[Path] = []
    for runs_root in historical_run_roots():
        sessions_root = runs_root / "step5b_autotune_sessions"
        if not sessions_root.is_dir():
            continue
        for path in sessions_root.glob("session_*/observations.jsonl"):
            resolved = str(path.resolve())
            if resolved in seen:
                continue
            seen.add(resolved)
            paths.append(path)
    return sorted(paths, key=lambda path: str(path.resolve()))


def trial_identity_occurrences(
    trial_uid: str,
    physical_capture_uid: str,
) -> list[dict[str, Any]]:
    occurrences: list[dict[str, Any]] = []
    for history_path in observation_history_paths():
        validated_identities = {
            (observation.trial_uid, observation.physical_capture_uid)
            for observation in read_observations(history_path)
        }
        for record in read_evaluation_jsonl(history_path):
            payload = record.payload
            if not payload.get("eligible") or payload.get("quarantined"):
                continue
            if (
                str(payload.get("trial_uid", "")),
                str(payload.get("physical_capture_uid", "")),
            ) not in validated_identities:
                continue
            duplicate_fields = [
                field
                for field, value in {
                    "trial_uid": trial_uid,
                    "physical_capture_uid": physical_capture_uid,
                }.items()
                if value and payload.get(field) == value
            ]
            if not duplicate_fields:
                continue
            occurrences.append(
                {
                    "source_jsonl": str(history_path.resolve()),
                    "line_number": record.line_number,
                    "run_dir": str(payload.get("run_dir", "")),
                    "duplicate_fields": duplicate_fields,
                }
            )
    return occurrences


def enforce_unique_trial_uid(
    evaluation: dict[str, Any],
    *,
    observations_path: Path,
    run_dir: Path,
) -> dict[str, Any]:
    if not evaluation.get("eligible"):
        return evaluation
    trial_uid = str(evaluation.get("trial_uid", ""))
    physical_capture_uid = str(evaluation.get("physical_capture_uid", ""))
    fingerprint = evaluation.get("fingerprint")
    identity_failures: list[str] = []
    if len(trial_uid) != 64:
        identity_failures.append("eligible_evaluation_missing_stable_trial_uid")
    if len(physical_capture_uid) != 64:
        identity_failures.append("eligible_evaluation_missing_physical_capture_uid")
    if evaluation.get("backend_id") != BACKEND_ID:
        identity_failures.append("eligible_evaluation_backend_mismatch")
    if not isinstance(fingerprint, dict) or not fingerprint.get("verified"):
        identity_failures.append("eligible_evaluation_fingerprint_unverified")

    duplicates = (
        trial_identity_occurrences(trial_uid, physical_capture_uid)
        if not identity_failures
        else []
    )
    if duplicates:
        identity_failures.append("duplicate_trial_or_physical_capture_uid_across_history")
    if not identity_failures:
        return evaluation

    quarantine_path = quarantine_evidence(
        run_dir,
        reasons=identity_failures,
        artifact_paths=[run_dir / "evaluation.json", observations_path],
        context={
            "trial_uid": trial_uid,
            "physical_capture_uid": physical_capture_uid,
            "backend_id": evaluation.get("backend_id"),
            "duplicate_occurrences": duplicates,
        },
    )
    failures = list(evaluation.get("failures") or [])
    failures.extend(reason for reason in identity_failures if reason not in failures)
    return {
        **evaluation,
        "eligible": False,
        "feasible": False,
        "objective": None,
        "disposition": "INTEGRITY_FAILURE",
        "failures": failures,
        "quarantined": True,
        "quarantine_path": str(quarantine_path) if quarantine_path else None,
        "duplicate_occurrences": duplicates,
    }


def _evaluate_history_run(run_dir: Path) -> dict[str, Any]:
    return evaluate_run(run_dir, allow_history=True)


def historical_run_dirs() -> list[Path]:
    seen: set[str] = set()
    run_dirs: list[Path] = []
    for root in historical_run_roots():
        for run_dir in sorted(root.glob("bridge_step5b_contact_cycloid_baseline_v2_*")):
            if str(run_dir.resolve()) in seen:
                continue
            seen.add(str(run_dir.resolve()))
            run_dirs.append(run_dir)
    return sorted(run_dirs, key=lambda path: (path.name, str(path.resolve())))


def bootstrap_history(observations_path: Path, parallel_manifest_path: Path) -> int:
    run_dirs = historical_run_dirs()
    started_at = now_iso()
    reference_path = observations_path.parent / "legacy_history_reference.json"
    grouped: dict[str, dict[str, Any]] = {}
    unresolved: list[str] = []
    for run_dir in run_dirs:
        csv_paths = sorted(run_dir.glob("bridge_rtde_*hz.csv"))
        capture_sha256 = sha256_file(csv_paths[0]) if len(csv_paths) == 1 else None
        if capture_sha256 is None:
            unresolved.append(str(run_dir))
            continue
        physical_capture_uid = physical_capture_uid_from_sha256(capture_sha256)
        record = grouped.setdefault(
            physical_capture_uid,
            {
                "physical_capture_uid": physical_capture_uid,
                "bridge_csv_sha256": capture_sha256,
                "aliases": [],
                "training_eligible": False,
            },
        )
        record["aliases"].append(str(run_dir))
    write_json(
        reference_path,
        {
            "schema_version": "step5b_autotune_legacy_history_reference_v1",
            "training_observations_imported": 0,
            "reason": "pre-v4 or non-supervisor history is content-grouped audit-only evidence",
            "run_dirs": [str(path) for path in run_dirs],
            "content_grouped_captures": [grouped[key] for key in sorted(grouped)],
            "unresolved_run_dirs": unresolved,
        },
    )
    write_json(
        parallel_manifest_path,
        {
            "contract_id": "ur10e_concurrency_contract_v1",
            "task": "step5b_autotune_session",
            "tasks": [
                {
                    "id": "history_bootstrap",
                    "dependencies": [],
                    "resource_lane": "CPU throughput",
                    "workers": 1,
                    "claim_class": "diagnostic_only",
                    "started_at": started_at,
                    "finished_at": now_iso(),
                    "exit_code": 0,
                    "inputs": [str(path) for path in run_dirs],
                    "outputs": [str(reference_path)],
                }
            ],
        },
    )
    return 0


def append_parallel_task(path: Path, task: dict[str, Any]) -> None:
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload.setdefault("tasks", []).append(task)
    write_json(path, payload)


def start_diagnostic(run_dir: Path) -> tuple[subprocess.Popen[Any], Any, str]:
    log_path = run_dir / "diagnostic_console.log"
    log_handle = log_path.open("w", encoding="utf-8")
    environment = os.environ.copy()
    environment.update({"OMP_NUM_THREADS": "1", "OPENBLAS_NUM_THREADS": "1", "MKL_NUM_THREADS": "1"})
    try:
        process = subprocess.Popen(
            [str(VENV_PYTHON), str(EXPERIMENT_ROOT / "tools" / "build_step5b_diagnostic_overview.py"), str(run_dir)],
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            text=True,
            env=environment,
        )
    except Exception:
        log_handle.close()
        raise
    return process, log_handle, now_iso()


def wait_for_bridge_output(process: subprocess.Popen[Any], run_dir: Path, timeout_s: float = 10.0) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f"bridge child exited before output start: rc={process.returncode}")
        if (run_dir / "metadata.json").is_file():
            return
        time.sleep(0.05)
    raise RuntimeError("bridge child did not create metadata.json within 10 s")


def verify_bridge_metadata(run_dir: Path, candidate: Candidate) -> None:
    metadata = json.loads((run_dir / "metadata.json").read_text(encoding="utf-8"))
    args = metadata.get("args") if isinstance(metadata.get("args"), dict) else {}
    expected = {
        "target_force_n": candidate.target_force_n,
        "step4e_force_p_gain": candidate.force_p_gain,
        "step4e_force_i_gain": candidate.force_i_gain,
        "step4e_force_damping": candidate.force_damping,
        "step4e_normal_filter_alpha": candidate.normal_filter_alpha,
    }
    if str(args.get("step4e_version")) != "step5b_v2":
        raise RuntimeError("bridge metadata profile mismatch")
    for key, value in expected.items():
        if not math.isclose(float(args.get(key, math.nan)), value, rel_tol=0.0, abs_tol=1e-12):
            raise RuntimeError(f"bridge metadata candidate mismatch: {key}")
    if Path(str(args.get("output_dir", ""))).resolve() != run_dir.resolve():
        raise RuntimeError("bridge metadata output_dir mismatch")


def process_group_exists(process_group_id: int) -> bool:
    try:
        os.killpg(process_group_id, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def stop_bridge_child(process: subprocess.Popen[Any], *, home_observed: bool) -> int:
    del home_observed  # cleanup escalation is unconditional; HOME only controls capture acceptance.
    leader_returncode = process.poll()
    for sig, timeout_s in ((signal.SIGINT, 20.0), (signal.SIGTERM, 5.0), (signal.SIGKILL, 5.0)):
        if process_group_exists(process.pid):
            try:
                os.killpg(process.pid, sig)
            except ProcessLookupError:
                pass
        if leader_returncode is None:
            try:
                leader_returncode = int(process.wait(timeout=timeout_s))
            except subprocess.TimeoutExpired:
                leader_returncode = None
        if leader_returncode is not None and not process_group_exists(process.pid):
            return leader_returncode
    if leader_returncode is None:
        raise RuntimeError("bridge process-group leader could not be reaped after SIGINT/TERM/KILL")
    raise RuntimeError("bridge descendant survived SIGINT/TERM/KILL after leader reap")


def _run_one_trial_locked(
    session_dir: Path,
    session_epoch: int,
    trial_id: int,
    candidate: Candidate,
    stop_file: Path,
    *,
    session_uid: str,
    frozen_fingerprint: dict[str, Any],
) -> dict[str, Any]:
    run_dir = session_dir / "trials" / f"trial_{trial_id:05d}_{int(candidate.target_force_n):02d}n"
    run_dir.mkdir(parents=True, exist_ok=False)
    token = candidate_token_low31(session_epoch, trial_id, candidate)
    command = bridge_command(candidate, run_dir, python_executable=str(VENV_PYTHON))

    bridge_log = (run_dir / "bridge_console.log").open("w", encoding="utf-8")
    process: subprocess.Popen[Any] | None = None
    home_observed = False
    terminal_reason: int | None = None
    fatal_detail: str | None = None
    last_sample: dict[str, Any] = {}
    started_at = now_iso()
    bridge_rc: int | None = None
    saw_run = False
    home_release_ack = False
    fingerprint_pre_record: dict[str, Any] | None = None
    fingerprint_post_record: dict[str, Any] | None = None
    fingerprint_verified = False
    trial_spec: dict[str, Any] | None = None
    trial_spec_sha256: str | None = None
    try:
        fingerprint_pre_record = fingerprint_record("pre")
        atomic_write_json(run_dir / "trial_fingerprint_pre.json", fingerprint_pre_record)
        pre_core = fingerprint_core(fingerprint_pre_record, expected_phase="pre")
        if pre_core != frozen_fingerprint:
            raise RuntimeError("trial pre fingerprint differs from frozen session fingerprint")
        trial_spec = trial_spec_payload(
            session_uid=session_uid,
            trial_id=trial_id,
            candidate=candidate.payload(),
            candidate_token_low31=token,
            fingerprint_pre_sha256=pre_core["combined_sha256"],
        )
        atomic_write_json(run_dir / "trial_spec.json", trial_spec)
        trial_spec_sha256 = sha256_file(run_dir / "trial_spec.json")
        if trial_spec_sha256 is None:
            raise RuntimeError("trial_spec.json digest unavailable")
        write_json(
            run_dir / "candidate.json",
            {
                **trial_spec,
                "trial_spec_sha256": trial_spec_sha256,
                "token_low31": token,
                "command": command,
            },
        )
        with tp_observer() as (observer, recipe_id, type_names):
            write_handshake(session_epoch, trial_id, COMMAND_HOLD, token)
            process = subprocess.Popen(
                command,
                stdout=bridge_log,
                stderr=subprocess.STDOUT,
                text=True,
                start_new_session=True,
            )
            wait_for_bridge_output(process, run_dir)
            verify_bridge_metadata(run_dir, candidate)
            write_handshake(session_epoch, trial_id, COMMAND_ARM, token)
            print(
                f"[autotune] trial {trial_id} armed; "
                "press TP Play once if the loop program is not already playing",
                flush=True,
            )
            deadline = time.monotonic() + 240.0
            next_dashboard = 0.0
            home_release_sent = False
            while time.monotonic() < deadline:
                if stop_file.exists() and process.poll() is None:
                    os.killpg(process.pid, signal.SIGINT)
                last_sample = observation_dict(observer, recipe_id, type_names)
                saw_run, fresh_home, release_ack, state, reason = handshake_progress(
                    last_sample,
                    session_epoch=session_epoch,
                    trial_id=trial_id,
                    token=token,
                    saw_run=saw_run,
                    home_release_sent=home_release_sent,
                )
                if fresh_home and not home_release_sent:
                    home_observed = True
                    terminal_reason = reason
                    write_handshake(session_epoch, trial_id, COMMAND_HOLD, token)
                    home_release_sent = True
                elif release_ack:
                    home_release_ack = True
                    break
                if state == TP_FAULT:
                    terminal_reason = reason
                    fatal_detail = f"TP fault terminal_reason={reason}"
                    break
                if time.monotonic() >= next_dashboard:
                    snapshot = dashboard_snapshot()
                    if "NORMAL" not in snapshot.get("safetymode", ""):
                        fatal_detail = f"Dashboard safety left NORMAL: {snapshot.get('safetymode')}"
                        break
                    if saw_run and "true" not in snapshot.get("running", "").lower() and not home_observed:
                        fatal_detail = f"TP program stopped or paused before HOME: {snapshot.get('programState')}"
                        break
                    next_dashboard = time.monotonic() + 1.0
                if process.poll() is not None and not home_observed:
                    # TP normally detects heartbeat stale and returns home. Keep observing it.
                    deadline = min(deadline, time.monotonic() + 45.0)
            else:
                fatal_detail = "trial observer timeout"
    except Exception as exc:
        fatal_detail = f"trial observer failure: {type(exc).__name__}: {exc}"
    finally:
        if process is not None:
            try:
                bridge_rc = stop_bridge_child(process, home_observed=home_observed)
            except Exception as exc:
                fatal_detail = (
                    f"{fatal_detail}; bridge cleanup failure: {type(exc).__name__}: {exc}"
                    if fatal_detail
                    else f"bridge cleanup failure: {type(exc).__name__}: {exc}"
                )
        bridge_log.close()

    try:
        fingerprint_post_record = fingerprint_record("post")
        atomic_write_json(run_dir / "trial_fingerprint_post.json", fingerprint_post_record)
        pre_core = fingerprint_core(fingerprint_pre_record or {}, expected_phase="pre")
        post_core = fingerprint_core(fingerprint_post_record, expected_phase="post")
        fingerprint_verified = pre_core == post_core == frozen_fingerprint
        if not fingerprint_verified:
            fatal_detail = fatal_detail or "source/config fingerprint differs from frozen session contract"
    except (KeyError, OSError, TypeError, ValueError) as exc:
        fatal_detail = (
            f"{fatal_detail}; fingerprint closure failure: {type(exc).__name__}: {exc}"
            if fatal_detail
            else f"fingerprint closure failure: {type(exc).__name__}: {exc}"
        )

    fingerprint_pre_sha256 = (
        fingerprint_pre_record.get("fingerprint", {}).get("combined_sha256")
        if fingerprint_pre_record
        else None
    )
    fingerprint_post_sha256 = (
        fingerprint_post_record.get("fingerprint", {}).get("combined_sha256")
        if fingerprint_post_record
        else None
    )
    bridge_csv_paths = sorted(run_dir.glob("bridge_rtde_*hz.csv"))
    bridge_csv_sha256 = sha256_file(bridge_csv_paths[0]) if len(bridge_csv_paths) == 1 else None
    physical_capture_uid = (
        physical_capture_uid_from_sha256(bridge_csv_sha256)
        if bridge_csv_sha256 is not None
        else None
    )
    if physical_capture_uid is None:
        fatal_detail = fatal_detail or "exactly one readable bridge CSV is required for capture identity"

    full_identity = {
        "backend_id": BACKEND_ID,
        "session_uid": session_uid,
        "candidate_uid": trial_spec.get("candidate_uid") if trial_spec else None,
        "trial_uid": trial_spec.get("trial_uid") if trial_spec else None,
        "trial_spec_sha256": trial_spec_sha256,
        "physical_capture_uid": physical_capture_uid,
    }

    runtime = {
        "schema_version": "step5b_autotune_trial_runtime_v4",
        "started_at": started_at,
        "finished_at": now_iso(),
        "session_epoch": session_epoch,
        "trial_id": trial_id,
        "candidate_token_low31": token,
        "terminal_reason": terminal_reason,
        "home_verified": home_observed,
        "fresh_run_observed": saw_run,
        "home_release_ack": home_release_ack,
        **full_identity,
        "fingerprint_pre_sha256": fingerprint_pre_sha256,
        "fingerprint_post_sha256": fingerprint_post_sha256,
        "fingerprint_verified": fingerprint_verified,
        "fatal_detail": fatal_detail,
        "bridge_returncode": bridge_rc,
        "last_tp_sample": last_sample,
    }
    write_json(run_dir / "trial_runtime.json", runtime)
    reaped = (
        process is not None
        and process.poll() is not None
        and bridge_rc is not None
        and not process_group_exists(process.pid)
    )
    capture_complete = bool(
        reaped
        and saw_run
        and home_observed
        and home_release_ack
        and fingerprint_verified
        and not fatal_detail
    )
    marker = "capture_complete.json" if capture_complete else "capture_incomplete.json"
    write_json(
        run_dir / marker,
        {
            "schema_version": "step5b_autotune_capture_manifest_v4",
            "complete": capture_complete,
            "at": now_iso(),
            "bridge_returncode": bridge_rc,
            "process_group_reaped": reaped,
            "fresh_run_observed": saw_run,
            "home_verified": home_observed,
            "home_release_ack": home_release_ack,
            **full_identity,
            "fingerprint_pre_sha256": fingerprint_pre_sha256,
            "fingerprint_post_sha256": fingerprint_post_sha256,
            "fingerprint_verified": fingerprint_verified,
            "fatal_detail": fatal_detail,
        },
    )
    return {"run_dir": run_dir, "runtime": runtime, "capture_complete": capture_complete}


def run_one_trial(
    session_dir: Path,
    session_epoch: int,
    trial_id: int,
    candidate: Candidate,
    stop_file: Path,
    *,
    session_uid: str | None = None,
    frozen_fingerprint: dict[str, Any] | None = None,
) -> dict[str, Any]:
    session_uid = session_uid or secrets.token_hex(32)
    frozen_fingerprint = frozen_fingerprint or source_config_fingerprint()
    with exclusive_lock(LIVE_WRITER_LOCK_PATH):
        return _run_one_trial_locked(
            session_dir,
            session_epoch,
            trial_id,
            candidate,
            stop_file,
            session_uid=session_uid,
            frozen_fingerprint=frozen_fingerprint,
        )


def trial_continuation(runtime: dict[str, Any], *, stop_requested: bool) -> tuple[int | None, str | None, bool]:
    reason = runtime.get("terminal_reason")
    reason = int(reason) if reason is not None else None
    fatal_detail = runtime.get("fatal_detail") or None
    if not runtime.get("home_verified"):
        fatal_detail = fatal_detail or "home not verified"
    if reason in FATAL_SESSION_REASONS:
        fatal_detail = fatal_detail or f"fatal terminal reason {reason}"
    return reason, fatal_detail, not fatal_detail and not stop_requested


def evidence_continuation(evaluation: dict[str, Any]) -> tuple[bool, str | None]:
    failures = ",".join(str(item) for item in evaluation.get("failures", []))
    if evaluation.get("quarantined"):
        return (
            False,
            "trial evidence quarantined; fix evidence/code before another live trial: "
            + failures,
        )
    if evaluation.get("disposition") not in {"OBJECTIVE", "PARAMETER_CONSTRAINT"}:
        return (
            False,
            "trial is not eligible for optimizer history; inspect platform evidence "
            "before another live trial: "
            + failures,
        )
    if (
        evaluation.get("eligible") is not True
        or evaluation.get("supervisor_closure_verified") is not True
    ):
        return (
            False,
            "trainable disposition lacks explicit eligibility or verified closure: " + failures,
        )
    return True, None


def wait_for_resume_or_stop(session_dir: Path) -> bool:
    stop_file = session_dir / "STOP_REQUESTED"
    resume_file = session_dir / "RESUME_REQUESTED"
    while not stop_file.exists():
        if resume_file.exists():
            resume_file.unlink()
            return True
        time.sleep(0.25)
    return False


def _run_session_after_preflight() -> int:
    session_epoch = secrets.randbelow(0x7FFFFFFF) + 1
    session_uid = secrets.token_hex(32)
    frozen_fingerprint = source_config_fingerprint()
    timestamp = datetime.now().astimezone().strftime("%Y%m%d_%H%M%S_%f")
    session_dir = RUNS_ROOT / f"session_{timestamp}_e{session_epoch}"
    session_dir.mkdir(parents=True, exist_ok=False)
    write_json(
        session_dir / "session_fingerprint.json",
        {
            "schema_version": "step5b_autotune_session_fingerprint_v1",
            "backend_id": BACKEND_ID,
            "session_uid": session_uid,
            "fingerprint": frozen_fingerprint,
        },
    )
    observations_path = session_dir / "observations.jsonl"
    parallel_manifest_path = session_dir / "parallel_run_manifest.json"
    stop_file = session_dir / "STOP_REQUESTED"
    history_count = bootstrap_history(observations_path, parallel_manifest_path)
    optimizer_started = now_iso()
    capacity = gpu_capacity_status()
    if not capacity["ok"]:
        raise RuntimeError(f"GPU capacity gate failed before initial selection: {capacity}")
    candidate, selection = choose_candidate(
        read_observations(
            observations_path,
            expected_fingerprint_sha256=frozen_fingerprint["combined_sha256"],
        ),
        require_botorch=True,
    )
    append_parallel_task(
        parallel_manifest_path,
        {
            "id": "optimizer_initial",
            "dependencies": ["history_bootstrap"],
            "resource_lane": "GPU functional",
            "workers": selection["gpu_workers"],
            "claim_class": "diagnostic_only",
            "started_at": optimizer_started,
            "finished_at": now_iso(),
            "exit_code": 0,
            "outputs": ["initial_candidate"],
            "device": selection["device"],
        },
    )
    state = {
        "schema_version": "step5b_autotune_session_v4",
        "status": "running",
        "started_at": now_iso(),
        "session_dir": str(session_dir),
        "session_epoch": session_epoch,
        "session_uid": session_uid,
        "backend_id": BACKEND_ID,
        "frozen_fingerprint_sha256": frozen_fingerprint["combined_sha256"],
        "pid": os.getpid(),
        "history_observations": history_count,
        "last_trial_id": 0,
        "consecutive_constraint_violations": 0,
    }
    write_json(session_dir / "session.json", state)
    write_json(ACTIVE_POINTER, state)

    def request_stop(_signum: int, _frame: Any) -> None:
        stop_file.touch()

    signal.signal(signal.SIGINT, request_stop)
    signal.signal(signal.SIGTERM, request_stop)
    exit_code = 0
    context = multiprocessing.get_context("spawn")
    diagnostic_process: subprocess.Popen[Any] | None = None
    diagnostic_log: Any = None
    try:
        trial_id = 0
        with concurrent.futures.ProcessPoolExecutor(max_workers=physical_cpu_workers(), mp_context=context) as cpu_pool:
            while not stop_file.exists():
                trial_id += 1
                state.update({"last_trial_id": trial_id, "selection": selection, "candidate": candidate.payload()})
                write_json(session_dir / "session.json", state)
                write_json(ACTIVE_POINTER, state)
                outcome = run_one_trial(
                    session_dir,
                    session_epoch,
                    trial_id,
                    candidate,
                    stop_file,
                    session_uid=session_uid,
                    frozen_fingerprint=frozen_fingerprint,
                )
                append_parallel_task(
                    parallel_manifest_path,
                    {
                        "id": f"trial_{trial_id:05d}_capture_complete",
                        "dependencies": [
                            "optimizer_initial" if trial_id == 1 else f"trial_{trial_id - 1:05d}_next_candidate"
                        ],
                        "resource_lane": "Live writer",
                        "workers": 1,
                        "claim_class": "live_gated",
                        "started_at": outcome["runtime"]["started_at"],
                        "finished_at": outcome["runtime"]["finished_at"],
                        "exit_code": outcome["runtime"].get("bridge_returncode"),
                        "outputs": [
                            str(
                                outcome["run_dir"]
                                / ("capture_complete.json" if outcome["capture_complete"] else "capture_incomplete.json")
                            )
                        ],
                    },
                )

                if not outcome["capture_complete"]:
                    state.update(
                        {
                            "status": "fatal",
                            "fatal_detail": "capture incomplete; postprocess and optimizer suppressed",
                            "finished_at": now_iso(),
                        }
                    )
                    exit_code = 4
                    break

                # The immutable capture marker exists now. CPU evaluator and diagnostic
                # fan out immediately; GPU selection starts as soon as evaluation is ready.
                postprocess_started = now_iso()
                diagnostic_process, diagnostic_log, diagnostic_started = start_diagnostic(outcome["run_dir"])
                evaluation_future = cpu_pool.submit(evaluate_run, outcome["run_dir"])
                try:
                    evaluation = evaluation_future.result()
                except Exception as exc:
                    reason = f"evaluator_exception:{type(exc).__name__}:{exc}"
                    quarantine_path = quarantine_evidence(
                        outcome["run_dir"],
                        reasons=[reason],
                        artifact_paths=[
                            outcome["run_dir"] / "metadata.json",
                            outcome["run_dir"] / "trial_runtime.json",
                            outcome["run_dir"] / "capture_complete.json",
                        ],
                        context={"backend_id": BACKEND_ID},
                    )
                    evaluation = {
                        "schema_version": EVALUATION_SCHEMA,
                        "backend_id": BACKEND_ID,
                        "run_dir": str(outcome["run_dir"]),
                        "trial_uid": None,
                        "eligible": False,
                        "feasible": False,
                        "objective": None,
                        "objective_name": OBJECTIVE_NAME,
                        "objective_unit": OBJECTIVE_UNIT,
                        "full_trial": False,
                        "supervisor_closure_verified": False,
                        "disposition": "INTEGRITY_FAILURE",
                        "fingerprint": {"verified": False},
                        "failures": [reason],
                        "quarantined": True,
                        "quarantine_path": str(quarantine_path) if quarantine_path else None,
                    }
                evaluation_finished = now_iso()
                evaluation = enforce_unique_trial_uid(
                    evaluation,
                    observations_path=observations_path,
                    run_dir=outcome["run_dir"],
                )
                write_json(outcome["run_dir"] / "evaluation.json", evaluation)
                append_jsonl(observations_path, evaluation)
                promotion_started = now_iso()
                promotion = write_promotion_artifacts(observations_path, session_dir)
                promotion_finished = now_iso()
                state["step5d_promotion"] = {
                    "status": promotion["status"],
                    "latest_json": promotion["latest_json"],
                    "promotion_id": promotion.get("promotion_id"),
                }

                reason, fatal, should_continue = trial_continuation(
                    outcome["runtime"], stop_requested=stop_file.exists()
                )
                evidence_can_continue, evidence_fatal = evidence_continuation(evaluation)
                if not evidence_can_continue:
                    fatal = evidence_fatal
                    should_continue = False
                next_candidate: Candidate | None = None
                next_selection: dict[str, Any] | None = None
                optimizer_finished: str | None = None
                optimizer_started = now_iso()
                if should_continue:
                    capacity = gpu_capacity_status()
                    if not capacity["ok"]:
                        fatal = f"GPU capacity gate failed before next selection: {capacity}"
                        should_continue = False
                    else:
                        next_candidate, next_selection = choose_candidate(
                            read_observations(
                                observations_path,
                                expected_fingerprint_sha256=frozen_fingerprint[
                                    "combined_sha256"
                                ],
                            ),
                            require_botorch=True,
                        )
                        optimizer_finished = now_iso()

                diagnostic_rc = diagnostic_process.wait()
                diagnostic_log.close()
                diagnostic_process = None
                diagnostic_log = None
                diagnostic_finished = now_iso()
                append_parallel_task(
                    parallel_manifest_path,
                    {
                        "id": f"trial_{trial_id:05d}_evaluator",
                        "dependencies": [f"trial_{trial_id:05d}_capture_complete"],
                        "resource_lane": "CPU throughput",
                        "workers": 1,
                        "claim_class": "offline_tooling",
                        "started_at": postprocess_started,
                        "finished_at": evaluation_finished,
                        "exit_code": 0,
                        "outputs": [str(outcome["run_dir"] / "evaluation.json")],
                    },
                )
                append_parallel_task(
                    parallel_manifest_path,
                    {
                        "id": f"trial_{trial_id:05d}_step5d_promotion",
                        "dependencies": [f"trial_{trial_id:05d}_evaluator"],
                        "resource_lane": "CPU throughput",
                        "workers": 1,
                        "claim_class": "offline_tooling",
                        "started_at": promotion_started,
                        "finished_at": promotion_finished,
                        "exit_code": 0,
                        "outputs": [promotion["latest_json"]],
                    },
                )
                append_parallel_task(
                    parallel_manifest_path,
                    {
                        "id": f"trial_{trial_id:05d}_diagnostic",
                        "dependencies": [f"trial_{trial_id:05d}_capture_complete"],
                        "resource_lane": "CPU throughput",
                        "workers": 1,
                        "claim_class": "diagnostic_only",
                        "started_at": diagnostic_started,
                        "finished_at": diagnostic_finished,
                        "exit_code": diagnostic_rc,
                        "outputs": [str(outcome["run_dir"] / "step5b_diagnostic_overview_summary.json")],
                    },
                )
                if next_candidate is not None and next_selection is not None:
                    append_parallel_task(
                        parallel_manifest_path,
                        {
                            "id": f"trial_{trial_id:05d}_next_candidate",
                            "dependencies": [f"trial_{trial_id:05d}_evaluator"],
                            "resource_lane": "GPU functional",
                            "workers": next_selection["gpu_workers"],
                            "claim_class": "diagnostic_only",
                            "started_at": optimizer_started,
                            "finished_at": optimizer_finished,
                            "exit_code": 0,
                            "outputs": ["next_candidate"],
                            "device": next_selection["device"],
                        },
                    )

                if fatal:
                    state.update({"status": "fatal", "fatal_detail": str(fatal), "finished_at": now_iso()})
                    exit_code = 4
                    break
                if stop_file.exists():
                    state.update({"status": "stopped", "finished_at": now_iso()})
                    break

                violation = evaluation.get("disposition") == "PARAMETER_CONSTRAINT"
                if violation:
                    state["consecutive_constraint_violations"] += 1
                else:
                    state["consecutive_constraint_violations"] = 0
                if state["consecutive_constraint_violations"] >= 2:
                    with exclusive_lock(LIVE_WRITER_LOCK_PATH):
                        write_handshake(session_epoch, trial_id, COMMAND_HOLD, candidate_token_low31(session_epoch, trial_id, candidate))
                    state["status"] = "paused_after_two_constraint_violations"
                    write_json(session_dir / "session.json", state)
                    write_json(ACTIVE_POINTER, state)
                    if not wait_for_resume_or_stop(session_dir):
                        state.update({"status": "stopped", "finished_at": now_iso()})
                        break
                    state["status"] = "running"
                    state["consecutive_constraint_violations"] = 0

                if next_candidate is None or next_selection is None:
                    state.update(
                        {
                            "status": "fatal",
                            "fatal_detail": "next GPU candidate missing",
                            "finished_at": now_iso(),
                        }
                    )
                    exit_code = 4
                    break
                candidate, selection = next_candidate, next_selection
                write_json(session_dir / "session.json", state)
                write_json(ACTIVE_POINTER, state)
    except Exception as exc:
        state.update(
            {
                "status": "fatal",
                "fatal_detail": f"session exception: {type(exc).__name__}: {exc}",
                "finished_at": now_iso(),
            }
        )
        exit_code = 4
    finally:
        if diagnostic_process is not None:
            if diagnostic_process.poll() is None:
                diagnostic_process.terminate()
                try:
                    diagnostic_process.wait(timeout=5.0)
                except subprocess.TimeoutExpired:
                    diagnostic_process.kill()
                    diagnostic_process.wait(timeout=5.0)
            if diagnostic_log is not None:
                diagnostic_log.close()
        try:
            with exclusive_lock(LIVE_WRITER_LOCK_PATH):
                write_handshake(session_epoch, int(state.get("last_trial_id", 0)), COMMAND_STOP, 0)
        except (OSError, RuntimeError, socket.timeout) as exc:
            state["stop_handshake_error"] = f"{type(exc).__name__}: {exc}"
        if state.get("status") == "running":
            state.update({"status": "stopped", "finished_at": now_iso()})
        write_json(session_dir / "session.json", state)
        write_json(ACTIVE_POINTER, state)
    return exit_code


def run_session() -> int:
    if os.environ.get("STEP5B_AUTOTUNE_CONFIRM") != CONFIRMATION_TOKEN:
        raise SystemExit(f"refusing live start: set STEP5B_AUTOTUNE_CONFIRM='{CONFIRMATION_TOKEN}'")
    ready = preflight(offline=False)
    if not ready["ok"]:
        print(json.dumps(ready, indent=2, sort_keys=True))
        raise SystemExit("live preflight failed")
    with exclusive_lock(SESSION_LOCK_PATH):
        return _run_session_after_preflight()


def active_state() -> tuple[dict[str, Any], Path | None]:
    if not ACTIVE_POINTER.is_file():
        return {
            "status": "no_session",
            "route": json.loads(AUTHORIZATION_LEDGER.read_text(encoding="utf-8")),
        }, None
    state = json.loads(ACTIVE_POINTER.read_text(encoding="utf-8"))
    session_dir = Path(state["session_dir"])
    session_path = session_dir / "session.json"
    if session_path.is_file():
        state = json.loads(session_path.read_text(encoding="utf-8"))
    return state, session_dir


def control_active(action: str) -> int:
    state, session_dir = active_state()
    if session_dir is None:
        print(json.dumps(state, indent=2))
        return 0 if action == "status" else 3
    if action == "status":
        print(json.dumps(state, indent=2, sort_keys=True))
        return 0
    if action == "stop":
        (session_dir / "STOP_REQUESTED").touch()
        print(json.dumps({"status": "stop_requested", "session_dir": str(session_dir)}, indent=2))
        return 0
    if action == "resume":
        if state.get("status") != "paused_after_two_constraint_violations":
            print(json.dumps({"status": "not_paused", "session": state}, indent=2))
            return 3
        (session_dir / "RESUME_REQUESTED").touch()
        print(json.dumps({"status": "resume_requested", "session_dir": str(session_dir)}, indent=2))
        return 0
    raise AssertionError(action)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    preflight_parser = subparsers.add_parser("preflight")
    preflight_parser.add_argument("--offline", action="store_true")
    subparsers.add_parser("start")
    subparsers.add_parser("status")
    subparsers.add_parser("stop")
    subparsers.add_parser("resume")
    args = parser.parse_args()
    if args.command == "preflight":
        result = preflight(offline=args.offline)
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0 if result["ok"] else 3
    if args.command == "start":
        return run_session()
    return control_active(args.command)


if __name__ == "__main__":
    raise SystemExit(main())
