#!/usr/bin/env python3
"""Owner-preserving parallel entrypoint for Step5d offline work."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import platform
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

from ur10e_parallel import (
    ResourceProfile,
    TaskRunner,
    TaskSpec,
    require_immutable_completion_marker,
    verified_closed_source,
)
from ur10e_decision_manifest import freeze as freeze_decisions
from step5d_timing_acceptance import (
    evaluate_step5d_v3_timing_raw,
    evaluate_timing_raw,
)


ROOT = Path(__file__).resolve().parents[1]
TOOLS = ROOT / "tools"
WORKFLOW = Path(__file__).resolve()
FORMAL_SOURCE_FILES = (
    "tools/run_step5d_parallel_workflow.py",
    "tools/contact_semantics.py",
    "tools/step5c_strict_rnn.py",
    "tools/step5d_paper_outer_loop.py",
    "tools/step5d_control_contract.py",
    "tools/step5d_runtime_interface.py",
    "tools/step5c_calibrated_kinematics_audit.py",
    "tools/kunwei_rtde_bridge.py",
    "tools/run_step5d_v30_remote_timing.py",
    "tools/build_step5d_v30_remote_timing_bundle.py",
    "tools/step5d_v30_timing.py",
    "tools/build_step5d_v30_offline_readiness.py",
    "tools/step5d_timing_acceptance.py",
    "tools/benchmark_step5d_v3_sphere_seam.py",
    "../../src/ur10e_experiment_runtime/ur10e_experiment_runtime/identity.py",
    "../../src/ur10e_experiment_runtime/ur10e_experiment_runtime/moving_sphere.py",
    "../../src/ur10e_experiment_runtime/ur10e_experiment_runtime/physical_prior.py",
    "../../src/ur10e_experiment_runtime/ur10e_experiment_runtime/stage_adapters.py",
)
FORMAL_INPUT_FILES = (
    "config/step5d_liveprep_solver_gate.json",
    "config/step5d_v30_profile_selection.json",
    "config/step5_stage_table.json",
    "config/current_stage.json",
    "config/ur10e_user_decisions_v1.json",
    "config/step5d_v29_remote_evidence_sha256.json",
)
SHORT_TIMING_SAMPLES = {
    "solver_samples": 128,
    "tick_samples": 256,
    "safe_hold_samples": 256,
    "component_diagnostic_samples": 64,
    "inner_iterations": 512,
}
FEATURE_WINDOWS_S = {
    "mujoco_startup": 0.08,
    "mujoco_steady": 0.40,
}


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def external_model_input_fingerprint() -> dict[str, str]:
    """Bind calibration and the complete UR xacro include directory."""
    import step5c_calibrated_kinematics_audit as kinematics

    calibration = Path(kinematics.DEFAULT_CALIBRATION_YAML).resolve()
    xacro = Path(kinematics.DEFAULT_XACRO_PATH).resolve()
    paths = {calibration, xacro}
    paths.update(path for path in xacro.parent.rglob("*") if path.is_file())
    return {str(path): sha256_file(path) for path in sorted(paths)}


def source_fingerprint(
    *, replay_csv: Path | None = None, environment: dict[str, str] | None = None,
    bundle: bytes | None = None, execution_contract: dict[str, Any] | None = None,
) -> dict[str, Any]:
    files = {
        relative: sha256_file(ROOT / relative)
        for relative in (*FORMAL_SOURCE_FILES, *FORMAL_INPUT_FILES)
    }
    return {
        "files": files,
        "replay_csv": (
            {"path": str(replay_csv.resolve()), "sha256": sha256_file(replay_csv.resolve())}
            if replay_csv is not None else None
        ),
        "timing_bundle_sha256": (
            hashlib.sha256(bundle).hexdigest() if bundle is not None else None
        ),
        "external_model_inputs": external_model_input_fingerprint(),
        "effective_environment": dict(sorted((environment or {}).items())),
        "execution_contract": execution_contract or {},
        "interpreter": {
            "path": str(Path(sys.executable).resolve()),
            "sha256": sha256_file(Path(sys.executable).resolve()),
            "version": sys.version,
            "platform": platform.platform(),
        },
        "cpu_affinity": sorted(os.sched_getaffinity(0)) if hasattr(os, "sched_getaffinity") else None,
    }


def test_python() -> Path:
    override = os.getenv("UR10E_TEST_PYTHON")
    candidate = Path(override).expanduser() if override else ROOT / ".venv/bin/python"
    if not candidate.is_file():
        raise RuntimeError(
            "isolated test Python missing; run: "
            "uv sync --frozen --only-group test-hermetic"
        )
    return Path(os.path.abspath(candidate))


def timing_environment() -> dict[str, str]:
    declared_pythonpath = os.getenv("UR10E_TIMING_PYTHONPATH", "")
    declared_ld = os.getenv("UR10E_TIMING_LD_LIBRARY_PATH", "")
    if not declared_pythonpath or not declared_ld:
        raise RuntimeError(
            "formal/short timing requires explicit UR10E_TIMING_PYTHONPATH and "
            "UR10E_TIMING_LD_LIBRARY_PATH; undeclared /tmp runtime defaults are forbidden"
        )
    effective = {
        "PYTHONPATH": declared_pythonpath,
        "LD_LIBRARY_PATH": declared_ld,
    }
    for key in (
        "CUDA_VISIBLE_DEVICES", "OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS",
        "MKL_NUM_THREADS", "NUMEXPR_NUM_THREADS", "UR10E_RNN_GPU_DEVICE",
    ):
        if key in os.environ:
            effective[key] = os.environ[key]
    return effective


def build_timing_bundle() -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        [sys.executable, str(TOOLS / "build_step5d_v30_remote_timing_bundle.py")],
        cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
    )


def timing_command_prefix(*, formal: bool, step5d_v3: bool) -> list[str]:
    if not formal:
        return []
    affinity = ["taskset", "-c", "11,13,14,15"]
    if step5d_v3:
        return affinity
    return [*affinity, "chrt", "-f", "20"]


def run_timing(
    *,
    replay_csv: Path,
    output: Path,
    formal: bool,
    step5d_v3: bool = False,
) -> int:
    environment = timing_environment()
    command_prefix = timing_command_prefix(formal=formal, step5d_v3=step5d_v3)
    execution_contract = {
        "formal": formal,
        "step5d_v3_moving_sphere": step5d_v3,
        "scheduler_contract": (
            "sched_other_0" if formal and step5d_v3 else "sched_fifo_20"
            if formal else "diagnostic_inherited"
        ),
        "command_prefix": command_prefix,
        "stdin_delivery": "generated_bundle_bytes",
    }
    bundle_before = build_timing_bundle()
    before = source_fingerprint(
        replay_csv=replay_csv, environment=environment, bundle=bundle_before.stdout,
        execution_contract=execution_contract,
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    command = [*command_prefix, sys.executable, "-"]
    command.extend(
        [
            "--experiment-root",
            str(ROOT),
            "--replay-csv",
            str(replay_csv),
        ]
    )
    if step5d_v3:
        command.append("--step5d-v3-moving-sphere")
    if not formal:
        command.extend(
            [
                "--solver-samples",
                str(SHORT_TIMING_SAMPLES["solver_samples"]),
                "--tick-samples",
                str(SHORT_TIMING_SAMPLES["tick_samples"]),
                "--safe-hold-samples",
                str(SHORT_TIMING_SAMPLES["safe_hold_samples"]),
                "--component-diagnostic-samples",
                str(SHORT_TIMING_SAMPLES["component_diagnostic_samples"]),
                "--no-pace-500hz",
                "--include-raw-samples",
                "--inner-iterations",
                str(SHORT_TIMING_SAMPLES["inner_iterations"]),
            ]
        )
    with output.open("wb") as handle:
        completed = subprocess.run(
            command,
            cwd=ROOT,
            env={**os.environ, **environment},
            input=bundle_before.stdout,
            stdout=handle,
            check=False,
        )
    bundle_after = build_timing_bundle()
    after = source_fingerprint(
        replay_csv=replay_csv, environment=environment, bundle=bundle_after.stdout,
        execution_contract=execution_contract,
    )
    metadata = {
        "formal": formal,
        "claim_class": (
            "formal_raw_capture_step5d_v3"
            if formal and step5d_v3
            else "formal_raw_capture"
            if formal
            else "diagnostic_only"
        ),
        "step5d_v3_moving_sphere": step5d_v3,
        "source_fingerprint_before": before,
        "source_fingerprint_after": after,
        "source_fingerprint_stable": before == after,
        "bundler_exit_code": bundle_before.returncode,
        "bundler_after_exit_code": bundle_after.returncode,
        "bundler_stderr_sha256": hashlib.sha256(bundle_before.stderr).hexdigest(),
        "harness_exit_code": completed.returncode,
        "output": str(output),
    }
    if not formal:
        metadata["feature_windows"] = {
            "purpose": "short_feature_bearing_daily_diagnostic",
            "samples": SHORT_TIMING_SAMPLES,
            "mujoco_duration_s": FEATURE_WINDOWS_S,
            "not_valid_for": [
                "formal_timing_acceptance",
                "direct_live_no_contact_canary_acceptance",
            ],
        }
    output.with_suffix(".metadata.json").write_text(
        json.dumps(metadata, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    if bundle_before.returncode != 0 or bundle_after.returncode != 0 or completed.returncode != 0:
        return completed.returncode or bundle_before.returncode or bundle_after.returncode
    if formal:
        evaluation = (
            evaluate_step5d_v3_timing_raw(ROOT, output)
            if step5d_v3
            else evaluate_timing_raw(ROOT, output)
        )
        output.with_suffix(".evaluation.json").write_text(
            json.dumps(evaluation, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
    return 0 if before == after else 76


def checksum_run(run_dir: Path, output: Path) -> int:
    files = [
        path
        for path in sorted(run_dir.iterdir())
        if path.is_file() and path.name != ".capture_complete.json"
    ]
    payload = {
        "schema_version": "step5d_immutable_source_checksums_v1",
        "source_run": str(run_dir.resolve()),
        "files": {
            path.name: {
                "bytes": path.stat().st_size,
                "sha256": sha256_file(path),
            }
            for path in files
        },
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    return 0


def internal_main(argv: list[str]) -> int | None:
    if not argv or not argv[0].startswith("__"):
        return None
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("internal_mode")
    parser.add_argument("--replay-csv", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--run-dir", type=Path)
    args = parser.parse_args(argv)
    if args.internal_mode == "__short-timing":
        if args.replay_csv is None:
            parser.error("--replay-csv is required")
        return run_timing(replay_csv=args.replay_csv, output=args.output, formal=False)
    if args.internal_mode == "__short-timing-v3":
        if args.replay_csv is None:
            parser.error("--replay-csv is required")
        return run_timing(
            replay_csv=args.replay_csv,
            output=args.output,
            formal=False,
            step5d_v3=True,
        )
    if args.internal_mode == "__formal-timing":
        if args.replay_csv is None:
            parser.error("--replay-csv is required")
        return run_timing(replay_csv=args.replay_csv, output=args.output, formal=True)
    if args.internal_mode == "__formal-timing-v3":
        if args.replay_csv is None:
            parser.error("--replay-csv is required")
        return run_timing(
            replay_csv=args.replay_csv,
            output=args.output,
            formal=True,
            step5d_v3=True,
        )
    if args.internal_mode == "__checksums":
        if args.run_dir is None:
            parser.error("--run-dir is required")
        return checksum_run(args.run_dir, args.output)
    parser.error(f"unknown internal mode: {args.internal_mode}")
    return 2


def task(
    output_root: Path,
    task_id: str,
    command: Iterable[str | Path],
    *,
    dependencies: Iterable[str] = (),
    resource: str = "cpu",
    claim_class: str = "diagnostic_only",
    cpu_tokens: int = 1,
    gpu_vram_reservation_pct: float = 0.0,
    gpu_device: str = "0",
    env: dict[str, str] | None = None,
) -> TaskSpec:
    return TaskSpec(
        task_id=task_id,
        command=tuple(str(value) for value in command),
        output_dir=output_root / task_id,
        dependencies=tuple(dependencies),
        resource=resource,
        claim_class=claim_class,
        cpu_tokens=cpu_tokens,
        gpu_vram_reservation_pct=gpu_vram_reservation_pct,
        gpu_device=gpu_device,
        env=env or {},
        cwd=ROOT,
    )


def check_tasks(output_root: Path, profile: ResourceProfile) -> list[TaskSpec]:
    return [
        task(
            output_root,
            "parallel-check",
            [str(ROOT / "check.sh")],
            resource="cpu",
            claim_class="deterministic_validation",
            cpu_tokens=profile.cpu_workers,
        )
    ]


def functional_tasks(
    output_root: Path,
    *,
    replay_csv: Path,
    model_manifest: Path,
) -> list[TaskSpec]:
    python = test_python()
    replay_output = output_root / "v30-replay" / "replay.json"
    replay_manifest = output_root / "v30-replay" / "evidence_manifest.json"
    protocol_nodes = [
        "tests/test_step5d_v30_profile.py::Step5dV30ProfileTest::test_deadline_overrun_hold_reuses_last_command_without_masking_stop",
        "tests/test_step5d_v30_profile.py::Step5dV30ProfileTest::test_v30_publish_history_requires_fresh_successful_rtde_send",
        "tests/test_step5d_v30_profile.py::Step5dV30ProfileTest::test_v30_exception_stop_publish_precedes_break_and_transport_close",
        "tests/test_step5d_no_contact_p0_v8.py::Step5dNoContactP0V8Test::test_qualified_canary_clock_resets_across_unconsumed_tick",
    ]
    common_pytest_env = {
        "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    return [
        task(
            output_root,
            "rnn-protocol-features",
            [python, "-m", "pytest", "-q", *protocol_nodes],
            resource="cpu",
            env=common_pytest_env,
        ),
        task(
            output_root,
            "v30-replay",
            [
                sys.executable,
                TOOLS / "replay_step5d_v30.py",
                "--run-dir",
                replay_csv.parent,
                "--source-run",
                replay_csv.parent,
                "--expected-csv-size",
                str(replay_csv.stat().st_size),
                "--replay-csv",
                replay_csv,
                "--manifest-output",
                replay_manifest,
                "--replay-output",
                replay_output,
            ],
            resource="cpu",
            cpu_tokens=2,
        ),
        task(
            output_root,
            "persistent-rnn-gpu-lane",
            [
                sys.executable,
                TOOLS / "run_step5d_rnn_diagnostic_lane.py",
                "--model-manifest",
                model_manifest,
                "--output-dir",
                output_root / "persistent-rnn-gpu-lane",
                "--startup-duration-s",
                str(FEATURE_WINDOWS_S["mujoco_startup"]),
                "--steady-duration-s",
                str(FEATURE_WINDOWS_S["mujoco_steady"]),
            ],
            resource="gpu_rnn",
            cpu_tokens=2,
            gpu_vram_reservation_pct=25.0,
            gpu_device=os.getenv("UR10E_RNN_GPU_DEVICE", "0"),
        ),
    ]


def formal_task(
    output_root: Path,
    *,
    replay_csv: Path,
    dependencies: Iterable[str] = (),
    step5d_v3: bool = False,
) -> TaskSpec:
    task_id = "formal-timing-v3" if step5d_v3 else "formal-timing"
    internal_mode = "__formal-timing-v3" if step5d_v3 else "__formal-timing"
    return task(
        output_root,
        task_id,
        [
            sys.executable,
            WORKFLOW,
            internal_mode,
            "--replay-csv",
            replay_csv,
            "--output",
            output_root / task_id / "timing.json",
        ],
        dependencies=dependencies,
        resource="formal_timing",
        claim_class=(
            "formal_raw_capture_step5d_v3"
            if step5d_v3
            else "formal_raw_capture"
        ),
        cpu_tokens=ResourceProfile.from_env().cpu_workers,
        gpu_vram_reservation_pct=0.0,
    )


def postprocess_tasks(output_root: Path, run_dir: Path) -> list[TaskSpec]:
    csv_path = run_dir / "bridge_rtde_500hz.csv"
    if not csv_path.is_file():
        raise ValueError(f"bridge CSV missing: {csv_path}")
    return [
        task(
            output_root,
            "frequency-summary",
            [
                sys.executable,
                TOOLS / "summarize_stage_frequency.py",
                csv_path,
                "--output",
                output_root / "frequency-summary" / "stage_frequency_summary.json",
            ],
            resource="cpu",
            claim_class="derived_evidence",
        ),
        task(
            output_root,
            "step5d-analysis",
            [
                sys.executable,
                TOOLS / "analyze_step5d_bridge_run.py",
                "--run-dir",
                run_dir,
                "--output",
                output_root / "step5d-analysis" / "step5d_bridge_analysis.json",
            ],
            resource="cpu",
            claim_class="derived_evidence",
        ),
        task(
            output_root,
            "source-checksums",
            [
                sys.executable,
                WORKFLOW,
                "__checksums",
                "--run-dir",
                run_dir,
                "--output",
                output_root / "source-checksums" / "derived_checksums.json",
            ],
            resource="io",
            claim_class="derived_evidence",
        ),
        task(
            output_root,
            "diagnostic-plot",
            [
                sys.executable,
                TOOLS / "plot_step5d_bridge_run.py",
                csv_path,
                "--output",
                output_root / "diagnostic-plot" / "step5d_bridge.png",
                "--metadata-output",
                output_root / "diagnostic-plot" / "plot_metadata.json",
            ],
            resource="cpu",
            claim_class="diagnostic_only",
            cpu_tokens=2,
        ),
    ]


def write_postprocess_aggregate(output_root: Path, run_dir: Path) -> None:
    artifact_paths = [
        output_root / "frequency-summary/stage_frequency_summary.json",
        output_root / "step5d-analysis/step5d_bridge_analysis.json",
        output_root / "source-checksums/derived_checksums.json",
        output_root / "diagnostic-plot/plot_metadata.json",
        output_root / "diagnostic-plot/step5d_bridge.png",
    ]
    aggregate = {
        "schema_version": "step5d_parallel_postprocess_v1",
        "source_run": str(run_dir.resolve()),
        "completion_marker_sha256": sha256_file(run_dir / ".capture_complete.json"),
        "artifacts": {
            path.relative_to(output_root).as_posix(): {
                "bytes": path.stat().st_size,
                "sha256": sha256_file(path),
            }
            for path in artifact_paths
            if path.is_file()
        },
    }
    (output_root / "postprocess_aggregate.json").write_text(
        json.dumps(aggregate, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def default_output_root(mode: str) -> Path:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return ROOT / "runs" / "parallel" / f"{mode}_{stamp}"


def main(argv: list[str] | None = None) -> int:
    values = list(sys.argv[1:] if argv is None else argv)
    internal = internal_main(values)
    if internal is not None:
        return internal
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "mode",
        choices=(
            "parallel-check",
            "offline-functional",
            "formal-timing",
            "formal-timing-v3",
            "offline-all",
            "postprocess",
        ),
    )
    parser.add_argument("run_dir", nargs="?", type=Path)
    parser.add_argument("--output-root", type=Path)
    parser.add_argument("--replay-csv", type=Path)
    parser.add_argument("--model-manifest", type=Path)
    parser.add_argument("--serial", action="store_true")
    args = parser.parse_args(values)

    if args.serial:
        os.environ["UR10E_PARALLEL"] = "0"
    profile = ResourceProfile.from_env()
    output_root = (args.output_root or default_output_root(args.mode)).resolve()
    if output_root.exists():
        parser.error(f"output root already exists: {output_root}")
    if args.mode in {
        "offline-functional",
        "formal-timing",
        "formal-timing-v3",
        "offline-all",
    } and (args.replay_csv is None or not args.replay_csv.is_file()):
        parser.error(f"replay CSV missing: {args.replay_csv}")
    output_root.mkdir(parents=True, exist_ok=False)
    decision_manifest = output_root / "user_decision_manifest.json"
    freeze_decisions(decision_manifest, root=ROOT)
    tasks: list[TaskSpec]
    if args.mode == "parallel-check":
        tasks = check_tasks(output_root, profile)
    elif args.mode == "offline-functional":
        if args.model_manifest is None or not args.model_manifest.is_file():
            parser.error(f"model manifest missing: {args.model_manifest}")
        tasks = functional_tasks(
            output_root,
            replay_csv=args.replay_csv.resolve(),
            model_manifest=args.model_manifest.resolve(),
        )
    elif args.mode in {"formal-timing", "formal-timing-v3"}:
        tasks = [
            formal_task(
                output_root,
                replay_csv=args.replay_csv.resolve(),
                step5d_v3=args.mode == "formal-timing-v3",
            )
        ]
    elif args.mode == "offline-all":
        if args.model_manifest is None or not args.model_manifest.is_file():
            parser.error(f"model manifest missing: {args.model_manifest}")
        tasks = functional_tasks(
            output_root,
            replay_csv=args.replay_csv.resolve(),
            model_manifest=args.model_manifest.resolve(),
        )
        tasks.append(
            formal_task(
                output_root,
                replay_csv=args.replay_csv.resolve(),
                dependencies=[item.task_id for item in tasks],
            )
        )
    else:
        if args.run_dir is None:
            parser.error("postprocess requires <run-dir>")
        run_dir = args.run_dir.resolve()
        if output_root == run_dir or output_root in run_dir.parents or run_dir in output_root.parents:
            parser.error("postprocess output root must be disjoint from immutable source run")
        require_immutable_completion_marker(run_dir)
        tasks = postprocess_tasks(output_root, run_dir)

    runner = TaskRunner(
        root=ROOT, output_root=output_root, profile=profile,
        decision_manifest=decision_manifest,
    )
    if args.mode == "postprocess":
        assert args.run_dir is not None
        with verified_closed_source(args.run_dir.resolve()):
            results = runner.run(tasks)
            passed = all(result.status == "passed" for result in results.values())
            if passed:
                write_postprocess_aggregate(output_root, args.run_dir.resolve())
    else:
        results = runner.run(tasks)
        passed = all(result.status == "passed" for result in results.values())
    print(f"parallel_run_manifest={runner.manifest_path}")
    return 0 if passed else 3


if __name__ == "__main__":
    raise SystemExit(main())
