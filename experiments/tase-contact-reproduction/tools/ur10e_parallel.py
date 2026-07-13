#!/usr/bin/env python3
"""Resource-aware DAG runner for offline UR10e work.

This module coordinates work selected by an existing UR owner.  It does not
authorize live actions or replace any package, bridge, controller, or contact
gate.
"""

from __future__ import annotations

import fcntl
import json
import os
import subprocess
import threading
import time
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence


CONTRACT_ID = "ur10e_concurrency_contract_v1"
MANIFEST_SCHEMA = "ur10e_parallel_run_manifest_v1"
DEFAULT_LOCK_ROOT = Path("/tmp/ur10e-resource-locks")
BLAS_THREAD_ENV = {
    "OMP_NUM_THREADS": "1",
    "OPENBLAS_NUM_THREADS": "1",
    "MKL_NUM_THREADS": "1",
    "NUMEXPR_NUM_THREADS": "1",
    "VECLIB_MAXIMUM_THREADS": "1",
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _positive_int(value: str, *, name: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer: {value!r}") from exc
    if parsed < 1:
        raise ValueError(f"{name} must be >= 1")
    return parsed


def physical_core_count() -> int:
    """Return physical cores without adding a psutil runtime dependency."""

    topology: set[tuple[str, str]] = set()
    for cpu_dir in sorted(Path("/sys/devices/system/cpu").glob("cpu[0-9]*")):
        try:
            package = (cpu_dir / "topology/physical_package_id").read_text().strip()
            core = (cpu_dir / "topology/core_id").read_text().strip()
        except OSError:
            continue
        topology.add((package, core))
    if topology:
        return len(topology)
    return max(1, os.cpu_count() or 1)


@dataclass(frozen=True)
class ResourceProfile:
    cpu_workers: int
    gpu_workers: int
    gpu_vram_limit_pct: float
    parallel: bool
    lock_root: Path = DEFAULT_LOCK_ROOT

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "ResourceProfile":
        values = dict(os.environ if env is None else env)
        raw_cpu = values.get("UR10E_CPU_WORKERS", "auto").strip().lower()
        cpu_workers = (
            physical_core_count()
            if raw_cpu == "auto"
            else _positive_int(raw_cpu, name="UR10E_CPU_WORKERS")
        )
        gpu_workers = _positive_int(
            values.get("UR10E_GPU_WORKERS", "3"), name="UR10E_GPU_WORKERS"
        )
        try:
            gpu_vram_limit_pct = float(
                values.get("UR10E_GPU_VRAM_LIMIT_PCT", "85")
            )
        except ValueError as exc:
            raise ValueError("UR10E_GPU_VRAM_LIMIT_PCT must be numeric") from exc
        if not 0.0 < gpu_vram_limit_pct <= 100.0:
            raise ValueError("UR10E_GPU_VRAM_LIMIT_PCT must be in (0, 100]")
        parallel = values.get("UR10E_PARALLEL", "1") not in {"0", "false", "False"}
        lock_root = Path(
            values.get("UR10E_LOCK_ROOT", str(DEFAULT_LOCK_ROOT))
        ).expanduser()
        return cls(
            cpu_workers=cpu_workers,
            gpu_workers=gpu_workers,
            gpu_vram_limit_pct=gpu_vram_limit_pct,
            parallel=parallel,
            lock_root=lock_root,
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "cpu_workers": self.cpu_workers,
            "gpu_workers": self.gpu_workers,
            "gpu_vram_limit_pct": self.gpu_vram_limit_pct,
            "parallel": self.parallel,
            "blas_threads_per_worker": 1,
            "lock_root": str(self.lock_root),
        }


class WeightedSemaphore:
    def __init__(self, capacity: int) -> None:
        if capacity < 1:
            raise ValueError("capacity must be positive")
        self.capacity = capacity
        self.available = capacity
        self._condition = threading.Condition()

    def acquire(self, tokens: int) -> None:
        if tokens < 1 or tokens > self.capacity:
            raise ValueError(
                f"token request {tokens} is outside [1, {self.capacity}]"
            )
        with self._condition:
            self._condition.wait_for(lambda: self.available >= tokens)
            self.available -= tokens

    def release(self, tokens: int) -> None:
        with self._condition:
            self.available += tokens
            if self.available > self.capacity:
                raise RuntimeError("weighted semaphore over-release")
            self._condition.notify_all()


class FileLease:
    """Cross-process advisory lock with shared/exclusive modes."""

    def __init__(self, path: Path, *, exclusive: bool, blocking: bool = True) -> None:
        self.path = path
        self.exclusive = exclusive
        self.blocking = blocking
        self._handle: Any = None

    def __enter__(self) -> "FileLease":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._handle = self.path.open("a+")
        operation = fcntl.LOCK_EX if self.exclusive else fcntl.LOCK_SH
        if not self.blocking:
            operation |= fcntl.LOCK_NB
        try:
            fcntl.flock(self._handle.fileno(), operation)
        except BlockingIOError:
            self._handle.close()
            self._handle = None
            raise
        return self

    def __exit__(self, *_: object) -> None:
        if self._handle is not None:
            fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
            self._handle.close()
            self._handle = None


def throughput_lease(
    profile: ResourceProfile,
    *,
    exclusive: bool,
    blocking: bool = True,
) -> FileLease:
    return FileLease(
        profile.lock_root / "throughput.lock",
        exclusive=exclusive,
        blocking=blocking,
    )


def gpu_memory_usage_pct() -> float:
    completed = subprocess.run(
        [
            "nvidia-smi",
            "--query-gpu=memory.used,memory.total",
            "--format=csv,noheader,nounits",
        ],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        timeout=5.0,
    )
    if completed.returncode != 0:
        raise RuntimeError(f"nvidia-smi failed: {completed.stderr.strip()}")
    percentages: list[float] = []
    for line in completed.stdout.splitlines():
        used_raw, total_raw = (part.strip() for part in line.split(",", 1))
        total = float(total_raw)
        if total <= 0:
            raise RuntimeError("nvidia-smi reported non-positive total VRAM")
        percentages.append(100.0 * float(used_raw) / total)
    if not percentages:
        raise RuntimeError("nvidia-smi returned no GPU rows")
    return max(percentages)


@dataclass(frozen=True)
class TaskSpec:
    task_id: str
    command: tuple[str, ...]
    output_dir: Path
    dependencies: tuple[str, ...] = ()
    resource: str = "cpu"
    claim_class: str = "diagnostic_only"
    cpu_tokens: int = 1
    gpu_vram_reservation_pct: float = 0.0
    env: Mapping[str, str] = field(default_factory=dict)
    cwd: Path | None = None

    def __post_init__(self) -> None:
        if not self.task_id:
            raise ValueError("task_id is required")
        if not self.command:
            raise ValueError(f"task {self.task_id} has no command")
        if self.resource not in {"cpu", "gpu", "io", "formal_timing"}:
            raise ValueError(f"unsupported resource lane: {self.resource}")


@dataclass
class TaskResult:
    task_id: str
    status: str
    started_at: str | None
    ended_at: str
    elapsed_s: float
    exit_code: int | None
    output_dir: str
    stdout: str | None
    stderr: str | None
    error: str | None = None

    def as_dict(self, task: TaskSpec) -> dict[str, Any]:
        outputs: list[str] = []
        output_dir = Path(self.output_dir)
        if output_dir.is_dir():
            outputs = [
                str(path)
                for path in sorted(output_dir.rglob("*"))
                if path.is_file()
            ]
        return {
            "task": self.task_id,
            "dependencies": list(task.dependencies),
            "resource": task.resource,
            "claim_class": task.claim_class,
            "cpu_tokens": task.cpu_tokens,
            "gpu_vram_reservation_pct": task.gpu_vram_reservation_pct,
            "command": list(task.command),
            "status": self.status,
            "start_time": self.started_at,
            "end_time": self.ended_at,
            "elapsed_s": self.elapsed_s,
            "exit_code": self.exit_code,
            "output_dir": self.output_dir,
            "stdout": self.stdout,
            "stderr": self.stderr,
            "error": self.error,
            "outputs": outputs,
        }


class ObserverBarrier:
    """In-process start barrier used by observer launch coordinators."""

    def __init__(self, expected: Iterable[str]) -> None:
        self.expected = frozenset(expected)
        if not self.expected:
            raise ValueError("observer barrier needs at least one endpoint")
        self.ready_endpoints: set[str] = set()
        self._condition = threading.Condition()

    def mark_ready(self, endpoint: str) -> None:
        if endpoint not in self.expected:
            raise ValueError(f"unexpected observer endpoint: {endpoint}")
        with self._condition:
            self.ready_endpoints.add(endpoint)
            self._condition.notify_all()

    def wait(self, timeout_s: float) -> bool:
        with self._condition:
            return self._condition.wait_for(
                lambda: self.ready_endpoints == set(self.expected), timeout=timeout_s
            )


class TaskRunner:
    def __init__(
        self,
        *,
        root: Path,
        output_root: Path,
        profile: ResourceProfile | None = None,
        gpu_usage: Callable[[], float] = gpu_memory_usage_pct,
    ) -> None:
        self.root = root.resolve()
        self.output_root = output_root.resolve()
        self.profile = profile or ResourceProfile.from_env()
        self.gpu_usage = gpu_usage
        self.cpu_tokens = WeightedSemaphore(self.profile.cpu_workers)
        self.gpu_tokens = WeightedSemaphore(self.profile.gpu_workers)
        self.results: dict[str, TaskResult] = {}
        self._manifest_lock = threading.Lock()

    @property
    def manifest_path(self) -> Path:
        return self.output_root / "parallel_run_manifest.json"

    def _validate(self, tasks: Sequence[TaskSpec]) -> dict[str, TaskSpec]:
        by_id = {task.task_id: task for task in tasks}
        if len(by_id) != len(tasks):
            raise ValueError("duplicate task ids")
        output_dirs = [task.output_dir.resolve() for task in tasks]
        if len(set(output_dirs)) != len(output_dirs):
            raise ValueError("task output directories must not overlap")
        for task in tasks:
            missing = set(task.dependencies) - set(by_id)
            if missing:
                raise ValueError(
                    f"task {task.task_id} has missing dependencies: {sorted(missing)}"
                )
            if task.cpu_tokens > self.profile.cpu_workers:
                raise ValueError(
                    f"task {task.task_id} requests {task.cpu_tokens} CPU tokens, "
                    f"pool has {self.profile.cpu_workers}"
                )

        visiting: set[str] = set()
        visited: set[str] = set()

        def visit(task_id: str) -> None:
            if task_id in visiting:
                raise ValueError("task dependency cycle")
            if task_id in visited:
                return
            visiting.add(task_id)
            for dependency in by_id[task_id].dependencies:
                visit(dependency)
            visiting.remove(task_id)
            visited.add(task_id)

        for task_id in by_id:
            visit(task_id)
        return by_id

    def _manifest_payload(self, tasks: Mapping[str, TaskSpec]) -> dict[str, Any]:
        return {
            "schema_version": MANIFEST_SCHEMA,
            "contract_id": CONTRACT_ID,
            "generated_at": utc_now(),
            "root": str(self.root),
            "output_root": str(self.output_root),
            "resource_profile": self.profile.as_dict(),
            "tasks": [
                self.results[task_id].as_dict(tasks[task_id])
                for task_id in sorted(self.results)
            ],
        }

    def _write_manifest(self, tasks: Mapping[str, TaskSpec]) -> None:
        with self._manifest_lock:
            self.output_root.mkdir(parents=True, exist_ok=True)
            payload = self._manifest_payload(tasks)
            temporary = self.manifest_path.with_suffix(".json.tmp")
            temporary.write_text(
                json.dumps(payload, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            temporary.replace(self.manifest_path)

    def _blocked_dependency(self, task: TaskSpec) -> TaskResult:
        return TaskResult(
            task_id=task.task_id,
            status="blocked_dependency",
            started_at=None,
            ended_at=utc_now(),
            elapsed_s=0.0,
            exit_code=None,
            output_dir=str(task.output_dir),
            stdout=None,
            stderr=None,
            error="one or more dependencies did not pass",
        )

    def _execute(self, task: TaskSpec) -> TaskResult:
        task.output_dir.mkdir(parents=True, exist_ok=False)
        stdout_path = task.output_dir / "stdout.log"
        stderr_path = task.output_dir / "stderr.log"
        started_at = utc_now()
        started = time.monotonic()
        self.cpu_tokens.acquire(task.cpu_tokens)
        gpu_acquired = False
        try:
            if task.resource in {"gpu", "formal_timing"}:
                self.gpu_tokens.acquire(1)
                gpu_acquired = True
                observed = self.gpu_usage()
                projected = observed + task.gpu_vram_reservation_pct
                if projected > self.profile.gpu_vram_limit_pct:
                    raise RuntimeError(
                        "GPU VRAM admission denied: "
                        f"observed={observed:.2f}% reservation="
                        f"{task.gpu_vram_reservation_pct:.2f}% limit="
                        f"{self.profile.gpu_vram_limit_pct:.2f}%"
                    )
            exclusive = task.resource == "formal_timing"
            with throughput_lease(self.profile, exclusive=exclusive):
                env = os.environ.copy()
                env.update(BLAS_THREAD_ENV)
                env.update({str(key): str(value) for key, value in task.env.items()})
                env["UR10E_CONCURRENCY_CONTRACT"] = CONTRACT_ID
                env["UR10E_CLAIM_CLASS"] = task.claim_class
                with stdout_path.open("w", encoding="utf-8") as stdout, stderr_path.open(
                    "w", encoding="utf-8"
                ) as stderr:
                    completed = subprocess.run(
                        list(task.command),
                        cwd=str((task.cwd or self.root).resolve()),
                        env=env,
                        stdout=stdout,
                        stderr=stderr,
                        check=False,
                    )
            status = "passed" if completed.returncode == 0 else "failed"
            return TaskResult(
                task_id=task.task_id,
                status=status,
                started_at=started_at,
                ended_at=utc_now(),
                elapsed_s=time.monotonic() - started,
                exit_code=completed.returncode,
                output_dir=str(task.output_dir),
                stdout=str(stdout_path),
                stderr=str(stderr_path),
            )
        except Exception as exc:
            stderr_path.write_text(f"{type(exc).__name__}: {exc}\n", encoding="utf-8")
            return TaskResult(
                task_id=task.task_id,
                status="failed",
                started_at=started_at,
                ended_at=utc_now(),
                elapsed_s=time.monotonic() - started,
                exit_code=70,
                output_dir=str(task.output_dir),
                stdout=str(stdout_path),
                stderr=str(stderr_path),
                error=f"{type(exc).__name__}: {exc}",
            )
        finally:
            if gpu_acquired:
                self.gpu_tokens.release(1)
            self.cpu_tokens.release(task.cpu_tokens)

    def run(self, tasks: Sequence[TaskSpec]) -> dict[str, TaskResult]:
        by_id = self._validate(tasks)
        self.output_root.mkdir(parents=True, exist_ok=True)
        self._write_manifest(by_id)
        pending = set(by_id)
        running: dict[Future[TaskResult], str] = {}
        max_workers = 1 if not self.profile.parallel else max(
            self.profile.cpu_workers, self.profile.gpu_workers
        )
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            while pending or running:
                made_progress = False
                for task_id in sorted(tuple(pending)):
                    task = by_id[task_id]
                    if not all(dep in self.results for dep in task.dependencies):
                        continue
                    if any(
                        self.results[dep].status != "passed"
                        for dep in task.dependencies
                    ):
                        self.results[task_id] = self._blocked_dependency(task)
                        pending.remove(task_id)
                        self._write_manifest(by_id)
                        made_progress = True
                        continue
                    if not self.profile.parallel and running:
                        continue
                    future = executor.submit(self._execute, task)
                    running[future] = task_id
                    pending.remove(task_id)
                    made_progress = True
                    if not self.profile.parallel:
                        break
                if running:
                    completed, _ = wait(running, return_when=FIRST_COMPLETED)
                    for future in completed:
                        task_id = running.pop(future)
                        self.results[task_id] = future.result()
                        self._write_manifest(by_id)
                    continue
                if pending and not made_progress:
                    raise RuntimeError("DAG scheduler made no progress")
        return dict(self.results)


def require_immutable_completion_marker(run_dir: Path) -> dict[str, Any]:
    marker = run_dir / ".capture_complete.json"
    if not marker.is_file():
        raise ValueError(f"immutable completion marker missing: {marker}")
    payload = json.loads(marker.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or payload.get("immutable") is not True:
        raise ValueError(f"completion marker is not immutable: {marker}")
    if payload.get("capture_closed") is not True:
        raise ValueError(f"capture is not closed: {marker}")
    return payload
