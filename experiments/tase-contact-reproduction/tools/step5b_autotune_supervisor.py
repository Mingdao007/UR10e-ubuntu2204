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
import json
import multiprocessing
import os
import signal
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
    CONSTRAINT_VIOLATION_REASONS,
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
from step5b_autotune_optimizer import choose_candidate, read_observations


REALSETUP_SCRIPTS = Path("/home/andy/codex-private-skills-shared-main/skills/ur10e-realsetup/scripts")
sys.path.insert(0, str(REALSETUP_SCRIPTS))
from _ur_common import RTDEClient, dashboard_exchange  # noqa: E402


ROBOT_HOST = "192.168.1.18"
RUNS_ROOT = EXPERIMENT_ROOT / "runs" / "step5b_autotune_sessions"
ACTIVE_POINTER = EXPERIMENT_ROOT / "runs" / ".step5b_autotune_active.json"
DELIVERY_EVIDENCE = EXPERIMENT_ROOT / "config" / "step5b_autotune_delivery.json"
VENV_PYTHON = EXPERIMENT_ROOT / ".venv-step5b-autotune" / "bin" / "python"
INT_INPUT_FIELDS = [f"input_int_register_{index}" for index in range(24, 28)]
OBSERVE_FIELDS = [
    "actual_TCP_pose",
    "actual_q",
    "runtime_state",
    "robot_mode",
    "safety_mode",
    *[f"output_int_register_{index}" for index in range(24, 28)],
    "output_double_register_30",
    "output_double_register_35",
]


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
    evidence = json.loads(DELIVERY_EVIDENCE.read_text(encoding="utf-8"))
    if not evidence.get("controller_readback_verified"):
        return False, "controller read-back is not verified"
    if evidence.get("basename") != PROGRAM_BASENAME:
        return False, "delivery evidence basename mismatch"
    for artifact in evidence.get("artifacts", []):
        path = EXPERIMENT_ROOT / artifact["local_path"]
        if not path.is_file():
            return False, f"local package artifact missing: {path}"
        import hashlib

        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        if digest != artifact.get("sha256"):
            return False, f"local package SHA mismatch: {path.name}"
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
    predicted_mib = 1024.0
    projected_pct = 100.0 * (used_mib + predicted_mib) / total_mib
    return {
        "ok": memory.returncode == 0 and workers < 3 and projected_pct <= 85.0,
        "total_mib": total_mib,
        "used_mib": used_mib,
        "predicted_optimizer_mib": predicted_mib,
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
    }
    if offline:
        result["ok"] = delivery_ok and not result["bridge_processes"]
        result["dependencies"]["required_for_live"] = True
        return result
    snapshot = dashboard_snapshot()
    loaded = snapshot.get("get loaded program", "")
    safety_ok = "NORMAL" in snapshot.get("safetymode", "")
    program_ok = CONTROLLER_PROGRAM in loaded
    result["dashboard"] = snapshot
    result["loaded_program_ok"] = program_ok
    result["safety_normal"] = safety_ok
    result["ok"] = delivery_ok and deps_ok and gpu["ok"] and not result["bridge_processes"] and program_ok and safety_ok
    return result


def historical_run_roots() -> list[Path]:
    roots = [EXPERIMENT_ROOT / "runs"]
    main_root = Path("/home/andy/ur10e_ros2_ws/experiments/tase-contact-reproduction/runs")
    if main_root.resolve() != roots[0].resolve():
        roots.append(main_root)
    return roots


def _evaluate_history_run(run_dir: Path) -> dict[str, Any]:
    return evaluate_run(run_dir, allow_history=True)


def bootstrap_history(observations_path: Path, parallel_manifest_path: Path) -> int:
    seen: set[str] = set()
    run_dirs: list[Path] = []
    for root in historical_run_roots():
        for run_dir in sorted(root.glob("bridge_step5b_contact_cycloid_baseline_v2_*")):
            if str(run_dir.resolve()) in seen:
                continue
            seen.add(str(run_dir.resolve()))
            run_dirs.append(run_dir)
    started_at = now_iso()
    context = multiprocessing.get_context("spawn")
    with concurrent.futures.ProcessPoolExecutor(
        max_workers=min(16, max(1, len(run_dirs))),
        mp_context=context,
    ) as pool:
        results = list(pool.map(_evaluate_history_run, run_dirs))
    accepted = [result for result in results if result.get("eligible") and result.get("feasible")]
    for result in accepted:
        append_jsonl(observations_path, result)
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
                    "workers": min(16, max(1, len(run_dirs))),
                    "claim_class": "diagnostic_only",
                    "started_at": started_at,
                    "finished_at": now_iso(),
                    "exit_code": 0,
                    "inputs": [str(path) for path in run_dirs],
                    "outputs": [str(observations_path)],
                }
            ],
        },
    )
    return len(accepted)


def append_parallel_task(path: Path, task: dict[str, Any]) -> None:
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload.setdefault("tasks", []).append(task)
    write_json(path, payload)


def start_diagnostic(run_dir: Path) -> tuple[subprocess.Popen[Any], Any, str]:
    log_path = run_dir / "diagnostic_console.log"
    log_handle = log_path.open("w", encoding="utf-8")
    environment = os.environ.copy()
    environment.update({"OMP_NUM_THREADS": "1", "OPENBLAS_NUM_THREADS": "1", "MKL_NUM_THREADS": "1"})
    process = subprocess.Popen(
        [str(VENV_PYTHON), str(EXPERIMENT_ROOT / "tools" / "build_step5b_diagnostic_overview.py"), str(run_dir)],
        stdout=log_handle,
        stderr=subprocess.STDOUT,
        text=True,
        env=environment,
    )
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


def stop_bridge_child(process: subprocess.Popen[Any], *, home_observed: bool) -> int:
    if process.poll() is None:
        process.send_signal(signal.SIGINT)
    try:
        return process.wait(timeout=20.0)
    except subprocess.TimeoutExpired:
        if not home_observed:
            raise RuntimeError("bridge child did not stop and TP home was not observed")
        process.terminate()
        try:
            return process.wait(timeout=5.0)
        except subprocess.TimeoutExpired:
            process.kill()
            return process.wait(timeout=5.0)


def run_one_trial(
    session_dir: Path,
    session_epoch: int,
    trial_id: int,
    candidate: Candidate,
    stop_file: Path,
) -> dict[str, Any]:
    run_dir = session_dir / "trials" / f"trial_{trial_id:05d}_{int(candidate.target_force_n):02d}n"
    run_dir.mkdir(parents=True, exist_ok=False)
    token = candidate_token_low31(session_epoch, trial_id, candidate)
    command = bridge_command(candidate, run_dir, python_executable=str(VENV_PYTHON))
    write_json(run_dir / "candidate.json", {"candidate": candidate.payload(), "token_low31": token, "command": command})

    bridge_log = (run_dir / "bridge_console.log").open("w", encoding="utf-8")
    process: subprocess.Popen[Any] | None = None
    home_observed = False
    terminal_reason: int | None = None
    fatal_detail: str | None = None
    last_sample: dict[str, Any] = {}
    started_at = now_iso()
    bridge_rc: int | None = None
    try:
        with tp_observer() as (observer, recipe_id, type_names):
            process = subprocess.Popen(command, stdout=bridge_log, stderr=subprocess.STDOUT, text=True)
            wait_for_bridge_output(process, run_dir)
            write_handshake(session_epoch, trial_id, COMMAND_ARM, token)
            print(
                f"[autotune] trial {trial_id} armed; "
                "press TP Play once if the loop program is not already playing",
                flush=True,
            )
            deadline = time.monotonic() + 240.0
            saw_run = False
            next_dashboard = 0.0
            while time.monotonic() < deadline:
                if stop_file.exists() and process.poll() is None:
                    process.send_signal(signal.SIGINT)
                last_sample = observation_dict(observer, recipe_id, type_names)
                ack_epoch = int(last_sample.get("output_int_register_24", 0))
                ack_trial = int(last_sample.get("output_int_register_25", -1))
                state = int(last_sample.get("output_int_register_26", 0))
                reason = int(last_sample.get("output_int_register_27", 0))
                if ack_epoch == session_epoch and ack_trial == trial_id:
                    saw_run = saw_run or state == TP_RUN
                    if state == TP_HOME:
                        home_observed = True
                        terminal_reason = reason
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

    runtime = {
        "schema_version": "step5b_autotune_trial_runtime_v1",
        "started_at": started_at,
        "finished_at": now_iso(),
        "session_epoch": session_epoch,
        "trial_id": trial_id,
        "candidate_token_low31": token,
        "terminal_reason": terminal_reason,
        "home_verified": home_observed,
        "fatal_detail": fatal_detail,
        "bridge_returncode": bridge_rc,
        "last_tp_sample": last_sample,
    }
    write_json(run_dir / "trial_runtime.json", runtime)
    write_json(run_dir / "capture_complete.json", {"complete": True, "at": now_iso(), "bridge_returncode": bridge_rc})
    return {"run_dir": run_dir, "runtime": runtime}


def wait_for_resume_or_stop(session_dir: Path) -> bool:
    stop_file = session_dir / "STOP_REQUESTED"
    resume_file = session_dir / "RESUME_REQUESTED"
    while not stop_file.exists():
        if resume_file.exists():
            resume_file.unlink()
            return True
        time.sleep(0.25)
    return False


def run_session() -> int:
    if os.environ.get("STEP5B_AUTOTUNE_CONFIRM") != CONFIRMATION_TOKEN:
        raise SystemExit(f"refusing live start: set STEP5B_AUTOTUNE_CONFIRM='{CONFIRMATION_TOKEN}'")
    ready = preflight(offline=False)
    if not ready["ok"]:
        print(json.dumps(ready, indent=2, sort_keys=True))
        raise SystemExit("live preflight failed")

    timestamp = datetime.now().astimezone().strftime("%Y%m%d_%H%M%S")
    session_dir = RUNS_ROOT / f"session_{timestamp}"
    session_dir.mkdir(parents=True, exist_ok=False)
    session_epoch = int(time.time()) & 0x7FFFFFFF
    observations_path = session_dir / "observations.jsonl"
    parallel_manifest_path = session_dir / "parallel_run_manifest.json"
    stop_file = session_dir / "STOP_REQUESTED"
    history_count = bootstrap_history(observations_path, parallel_manifest_path)
    optimizer_started = now_iso()
    capacity = gpu_capacity_status()
    if not capacity["ok"]:
        raise RuntimeError(f"GPU capacity gate failed before initial selection: {capacity}")
    candidate, selection = choose_candidate(read_observations(observations_path), require_botorch=True)
    append_parallel_task(
        parallel_manifest_path,
        {
            "id": "optimizer_initial",
            "dependencies": ["history_bootstrap"],
            "resource_lane": "GPU functional",
            "workers": 1,
            "claim_class": "diagnostic_only",
            "started_at": optimizer_started,
            "finished_at": now_iso(),
            "exit_code": 0,
            "outputs": ["initial_candidate"],
            "device": selection.get("device", "cuda:0"),
        },
    )
    state = {
        "schema_version": "step5b_autotune_session_v1",
        "status": "running",
        "started_at": now_iso(),
        "session_dir": str(session_dir),
        "session_epoch": session_epoch,
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
    try:
        trial_id = 0
        with concurrent.futures.ProcessPoolExecutor(max_workers=16, mp_context=context) as cpu_pool:
            while not stop_file.exists():
                trial_id += 1
                state.update({"last_trial_id": trial_id, "selection": selection, "candidate": candidate.payload()})
                write_json(session_dir / "session.json", state)
                write_json(ACTIVE_POINTER, state)
                outcome = run_one_trial(session_dir, session_epoch, trial_id, candidate, stop_file)
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
                        "outputs": [str(outcome["run_dir"] / "capture_complete.json")],
                    },
                )

                # The immutable capture marker exists now. CPU evaluator and diagnostic
                # fan out immediately; GPU selection starts as soon as evaluation is ready.
                postprocess_started = now_iso()
                diagnostic_process, diagnostic_log, diagnostic_started = start_diagnostic(outcome["run_dir"])
                evaluation_future = cpu_pool.submit(evaluate_run, outcome["run_dir"])
                evaluation = evaluation_future.result()
                evaluation_finished = now_iso()
                write_json(outcome["run_dir"] / "evaluation.json", evaluation)
                append_jsonl(observations_path, evaluation)

                reason = outcome["runtime"].get("terminal_reason")
                fatal = outcome["runtime"].get("fatal_detail") or not outcome["runtime"].get("home_verified")
                if reason in FATAL_SESSION_REASONS:
                    fatal = fatal or f"fatal terminal reason {reason}"
                should_continue = not fatal and not stop_file.exists() and reason != 4
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
                            read_observations(observations_path), require_botorch=True
                        )
                        optimizer_finished = now_iso()

                diagnostic_rc = diagnostic_process.wait()
                diagnostic_log.close()
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
                            "workers": 1,
                            "claim_class": "diagnostic_only",
                            "started_at": optimizer_started,
                            "finished_at": optimizer_finished,
                            "exit_code": 0,
                            "outputs": ["next_candidate"],
                            "device": next_selection.get("device", "cuda:0"),
                        },
                    )

                if fatal:
                    state.update({"status": "fatal", "fatal_detail": str(fatal), "finished_at": now_iso()})
                    exit_code = 4
                    break
                if stop_file.exists() or reason == 4:
                    state.update({"status": "stopped", "finished_at": now_iso()})
                    break

                violation = (not evaluation.get("feasible")) or reason in CONSTRAINT_VIOLATION_REASONS
                if violation:
                    state["consecutive_constraint_violations"] += 1
                else:
                    state["consecutive_constraint_violations"] = 0
                if state["consecutive_constraint_violations"] >= 2:
                    write_handshake(session_epoch, trial_id, COMMAND_HOLD, 0)
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
    finally:
        try:
            write_handshake(session_epoch, int(state.get("last_trial_id", 0)), COMMAND_STOP, 0)
        except (OSError, RuntimeError, socket.timeout) as exc:
            state["stop_handshake_error"] = f"{type(exc).__name__}: {exc}"
        if state.get("status") == "running":
            state.update({"status": "stopped", "finished_at": now_iso()})
        write_json(session_dir / "session.json", state)
        write_json(ACTIVE_POINTER, state)
    return exit_code


def active_state() -> tuple[dict[str, Any], Path | None]:
    if not ACTIVE_POINTER.is_file():
        return {"status": "no_session"}, None
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
        return 3
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
