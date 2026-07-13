#!/usr/bin/env python3
"""Concurrency contract tests that never touch the live bench."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from ur10e_parallel import (  # noqa: E402
    CONTRACT_ID,
    FileLease,
    ObserverBarrier,
    ResourceProfile,
    TaskRunner,
    TaskSpec,
    WeightedSemaphore,
    physical_core_count,
    require_immutable_completion_marker,
    throughput_lease,
)


class Ur10eParallelTest(unittest.TestCase):
    def profile(self, root: Path, *, parallel: bool = True) -> ResourceProfile:
        return ResourceProfile(
            cpu_workers=4,
            gpu_workers=2,
            gpu_vram_limit_pct=85.0,
            parallel=parallel,
            lock_root=root / "locks",
        )

    def task(
        self,
        root: Path,
        task_id: str,
        *,
        command: tuple[str, ...] | None = None,
        dependencies: tuple[str, ...] = (),
        resource: str = "cpu",
        cpu_tokens: int = 1,
        reservation: float = 0.0,
    ) -> TaskSpec:
        return TaskSpec(
            task_id=task_id,
            command=command
            or (
                sys.executable,
                "-c",
                "from pathlib import Path; Path('done.txt').write_text('ok')",
            ),
            output_dir=root / task_id,
            dependencies=dependencies,
            resource=resource,
            cpu_tokens=cpu_tokens,
            gpu_vram_reservation_pct=reservation,
        )

    def test_host_profile_resolves_physical_cores_and_serial_fallback(self) -> None:
        self.assertGreaterEqual(physical_core_count(), 1)
        profile = ResourceProfile.from_env(
            {
                "UR10E_CPU_WORKERS": "auto",
                "UR10E_GPU_WORKERS": "3",
                "UR10E_GPU_VRAM_LIMIT_PCT": "85",
                "UR10E_PARALLEL": "0",
            }
        )
        self.assertEqual(profile.cpu_workers, physical_core_count())
        self.assertEqual(profile.gpu_workers, 3)
        self.assertFalse(profile.parallel)

    def test_weighted_cpu_tokens_block_until_released(self) -> None:
        semaphore = WeightedSemaphore(2)
        semaphore.acquire(2)
        acquired = threading.Event()

        def waiter() -> None:
            semaphore.acquire(1)
            acquired.set()
            semaphore.release(1)

        thread = threading.Thread(target=waiter)
        thread.start()
        self.assertFalse(acquired.wait(0.05))
        semaphore.release(2)
        self.assertTrue(acquired.wait(1.0))
        thread.join()

    def test_dag_manifest_dependencies_and_fail_isolation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runner = TaskRunner(
                root=root,
                output_root=root / "run",
                profile=self.profile(root),
            )
            tasks = [
                self.task(root / "run", "ok"),
                self.task(
                    root / "run",
                    "fail",
                    command=(sys.executable, "-c", "raise SystemExit(9)"),
                ),
                self.task(root / "run", "child", dependencies=("fail",)),
                self.task(root / "run", "independent", dependencies=("ok",)),
            ]
            results = runner.run(tasks)
            self.assertEqual(results["ok"].status, "passed")
            self.assertEqual(results["fail"].exit_code, 9)
            self.assertEqual(results["child"].status, "blocked_dependency")
            self.assertEqual(results["independent"].status, "passed")
            manifest = json.loads(runner.manifest_path.read_text())
            self.assertEqual(manifest["contract_id"], CONTRACT_ID)
            by_task = {item["task"]: item for item in manifest["tasks"]}
            self.assertEqual(by_task["child"]["dependencies"], ["fail"])
            self.assertEqual(by_task["independent"]["claim_class"], "diagnostic_only")

    def test_output_directories_must_be_unique(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            shared = root / "same"
            runner = TaskRunner(
                root=root,
                output_root=root / "run",
                profile=self.profile(root),
            )
            tasks = [
                TaskSpec("one", ("true",), shared),
                TaskSpec("two", ("true",), shared),
            ]
            with self.assertRaisesRegex(ValueError, "must not overlap"):
                runner.run(tasks)

    def test_gpu_vram_admission_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runner = TaskRunner(
                root=root,
                output_root=root / "run",
                profile=self.profile(root),
                gpu_usage=lambda: 80.0,
            )
            results = runner.run(
                [
                    self.task(
                        root / "run",
                        "gpu",
                        resource="gpu",
                        reservation=6.0,
                    )
                ]
            )
            self.assertEqual(results["gpu"].status, "failed")
            self.assertIn("VRAM admission denied", results["gpu"].error or "")

    def test_formal_and_live_exclusive_lock_blocks_throughput(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            profile = self.profile(root)
            with throughput_lease(profile, exclusive=False):
                with self.assertRaises(BlockingIOError):
                    with throughput_lease(profile, exclusive=True, blocking=False):
                        pass
            with throughput_lease(profile, exclusive=True):
                with self.assertRaises(BlockingIOError):
                    with throughput_lease(profile, exclusive=False, blocking=False):
                        pass

    def test_observer_barrier_requires_every_endpoint(self) -> None:
        barrier = ObserverBarrier({"kunwei", "rtde", "video"})
        barrier.mark_ready("kunwei")
        barrier.mark_ready("rtde")
        self.assertFalse(barrier.wait(0.01))
        barrier.mark_ready("video")
        self.assertTrue(barrier.wait(0.01))

    def test_serial_fallback_never_overlaps_tasks(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            script = (
                "import pathlib,time; "
                "p=pathlib.Path('interval.json'); "
                "p.write_text(str(time.monotonic())); time.sleep(0.08)"
            )
            runner = TaskRunner(
                root=root,
                output_root=root / "run",
                profile=self.profile(root, parallel=False),
            )
            started = time.monotonic()
            results = runner.run(
                [
                    self.task(
                        root / "run",
                        "one",
                        command=(sys.executable, "-c", script),
                    ),
                    self.task(
                        root / "run",
                        "two",
                        command=(sys.executable, "-c", script),
                    ),
                ]
            )
            self.assertTrue(all(result.status == "passed" for result in results.values()))
            self.assertGreaterEqual(time.monotonic() - started, 0.14)

    def test_completion_marker_is_required_for_postprocess(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run_dir = Path(directory)
            with self.assertRaisesRegex(ValueError, "marker missing"):
                require_immutable_completion_marker(run_dir)
            marker = run_dir / ".capture_complete.json"
            marker.write_text(
                json.dumps({"immutable": True, "capture_closed": True}) + "\n"
            )
            self.assertTrue(require_immutable_completion_marker(run_dir)["immutable"])


if __name__ == "__main__":
    unittest.main()
