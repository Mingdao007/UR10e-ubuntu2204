"""Offline-only CLI for dataset hashing, DBIL training/inference, and timing."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Mapping, Sequence

from .contracts import ImpedanceObservation, PoseSample
from .dbil.config import DBILConfig
from .dbil.convert_parkour import convert_parkour_dataset
from .dbil.dataset import DatasetStats, load_dataset, sha256_file, write_dataset_manifest
from .dbil.inference import (
    TorchDBILShadowPredictor,
    benchmark_paced_shadow_predictor,
    benchmark_shadow_predictor,
    evaluate_shadow_predictor,
)
from .dbil import inference as inference_module
from .dbil.model import train_checkpoint
from .dbil.timing import build_paced_timing_selection_manifest
from .trace import (
    convert_ur_bridge_trace,
    load_ur_trace_dataset,
    run_shadow_policy_ablation,
)


LOCKED_UPSTREAM_COMMIT = "8c05a4d4aca8012927a9fdf5bfcd8313247f6a30"


def _validate_lock(path: Path) -> dict[str, object]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("upstream_commit") != LOCKED_UPSTREAM_COMMIT:
        raise ValueError("DBIL upstream commit drifted")
    if payload.get("active_enabled") is not False:
        raise ValueError("DBIL active gate must remain disabled")
    if payload.get("paper_version") != "arXiv:2509.19696v3":
        raise ValueError("DBIL paper binding drifted")
    if payload.get("paper_title") != (
        "Diffusion-Based Impedance Learning for Contact-Rich Manipulation Tasks"
    ):
        raise ValueError("DBIL paper title binding drifted")
    if payload.get("license_sha256") != (
        "4515e7bdab12b171031edd73e264a339351d4f2dae00f519d905ba205f238d81"
    ):
        raise ValueError("DBIL upstream license binding drifted")
    if payload.get("pretrained_checkpoint") != "not_present_in_pinned_git_tree":
        raise ValueError("DBIL checkpoint availability binding drifted")
    return payload


def _observation_from_mapping(payload: Mapping[str, Any]) -> ImpedanceObservation:
    if payload.get("schema_version") != 1:
        raise ValueError("observation JSON requires schema_version=1")
    poses = tuple(
        PoseSample(values[:3], values[3:7]) for values in payload["pose_history"]
    )
    nominal = payload["nominal_zft"]
    lineage = payload["lineage"]
    return ImpedanceObservation(
        sequence=int(payload["sequence"]),
        timestamp_s=float(payload["timestamp_s"]),
        pose_history=poses,
        twist_history=payload["twist_history"],
        wrench_history=payload["wrench_history"],
        nominal_zft=PoseSample(nominal[:3], nominal[3:7]),
        joint_position_rad=payload["joint_position_rad"],
        joint_velocity_rad_s=payload["joint_velocity_rad_s"],
        jacobian_base=payload.get("jacobian_base"),
        frame_id=lineage["frame_id"],
        sensor_id=lineage["sensor_id"],
        calibration_hash=lineage["calibration_hash"],
    )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Offline-only UR10e VIC/DBIL preparation; no controller I/O"
    )
    commands = parser.add_subparsers(dest="command", required=True)
    lock = commands.add_parser("validate-lock")
    lock.add_argument("--lock", type=Path, required=True)

    dataset = commands.add_parser("dataset-manifest")
    dataset.add_argument("--dataset", type=Path, required=True)
    dataset.add_argument("--output", type=Path, required=True)
    dataset.add_argument("--stats-output", type=Path)

    convert = commands.add_parser("convert-parkour")
    convert.add_argument("--source-root", type=Path, required=True)
    convert.add_argument("--source-git-root", type=Path, required=True)
    convert.add_argument("--dataset-output", type=Path, required=True)
    convert.add_argument("--manifest-output", type=Path, required=True)
    convert.add_argument("--stats-output", type=Path, required=True)

    train = commands.add_parser("train")
    train.add_argument("--dataset", type=Path, required=True)
    train.add_argument("--stats", type=Path, required=True)
    train.add_argument("--checkpoint", type=Path, required=True)
    train.add_argument("--epochs", type=int, default=1)
    train.add_argument("--batch-size", type=int, default=32)
    train.add_argument("--learning-rate", type=float, default=1e-4)
    train.add_argument("--device", default="auto")
    train.add_argument("--max-train-samples", type=int)

    infer = commands.add_parser("infer-window")
    infer.add_argument("--checkpoint", type=Path, required=True)
    infer.add_argument("--checkpoint-sha256", required=True)
    infer.add_argument("--stats", type=Path, required=True)
    infer.add_argument("--stats-sha256", required=True)
    infer.add_argument("--observation-json", type=Path, required=True)
    infer.add_argument("--device", default="auto")

    benchmark = commands.add_parser("benchmark-inference")
    benchmark.add_argument("--checkpoint", type=Path, required=True)
    benchmark.add_argument("--checkpoint-sha256", required=True)
    benchmark.add_argument("--stats", type=Path, required=True)
    benchmark.add_argument("--stats-sha256", required=True)
    benchmark.add_argument("--observation-json", type=Path, required=True)
    benchmark.add_argument("--device", default="auto")
    benchmark.add_argument("--duration-s", type=float, default=60.0)
    benchmark.add_argument("--warmup-iterations", type=int, default=5)

    paced = commands.add_parser("benchmark-rates")
    paced.add_argument("--checkpoint", type=Path, required=True)
    paced.add_argument("--checkpoint-sha256", required=True)
    paced.add_argument("--stats", type=Path, required=True)
    paced.add_argument("--stats-sha256", required=True)
    paced.add_argument("--observation-json", type=Path, required=True)
    paced.add_argument("--device", default="auto")
    paced.add_argument("--duration-per-rate-s", type=float, default=60.0)
    paced.add_argument("--warmup-iterations", type=int, default=5)
    paced.add_argument("--output", type=Path, required=True)

    evaluate = commands.add_parser("evaluate")
    evaluate.add_argument("--checkpoint", type=Path, required=True)
    evaluate.add_argument("--checkpoint-sha256", required=True)
    evaluate.add_argument("--stats", type=Path, required=True)
    evaluate.add_argument("--stats-sha256", required=True)
    evaluate.add_argument("--dataset", type=Path, required=True)
    evaluate.add_argument("--device", default="auto")
    evaluate.add_argument("--split-id", type=int, default=2)
    evaluate.add_argument("--max-samples", type=int)

    trace = commands.add_parser("convert-ur-trace")
    trace.add_argument("--csv", type=Path, required=True)
    trace.add_argument("--dataset-output", type=Path, required=True)
    trace.add_argument("--manifest-output", type=Path, required=True)
    trace.add_argument(
        "--command-kind",
        choices=("step5b_twist", "strict_rnn_qdot"),
        default="step5b_twist",
    )
    trace.add_argument("--calibration-sha256")
    trace.add_argument("--frame-transform-sha256")
    trace.add_argument("--task-zft-json", type=Path)
    trace.add_argument("--calibration-artifact", type=Path)
    trace.add_argument("--frame-transform-artifact", type=Path)
    trace.add_argument("--task-zft-artifact", type=Path)

    ablation = commands.add_parser("ablate-ur-trace")
    ablation.add_argument("--dataset", type=Path, required=True)
    ablation.add_argument("--checkpoint", type=Path, required=True)
    ablation.add_argument("--checkpoint-sha256", required=True)
    ablation.add_argument("--stats", type=Path, required=True)
    ablation.add_argument("--stats-sha256", required=True)
    ablation.add_argument("--device", default="auto")
    ablation.add_argument("--max-observations", type=int, default=64)
    ablation.add_argument("--output", type=Path, required=True)

    timing = commands.add_parser("select-rate")
    timing.add_argument("--evidence", type=Path, required=True)
    timing.add_argument("--checkpoint", type=Path, required=True)
    timing.add_argument("--stats", type=Path, required=True)
    timing.add_argument("--observation-json", type=Path, required=True)
    timing.add_argument("--output", type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    if args.command == "validate-lock":
        result = _validate_lock(args.lock)
    elif args.command == "dataset-manifest":
        result = write_dataset_manifest(
            args.dataset, args.output, stats_output_path=args.stats_output
        )
    elif args.command == "convert-parkour":
        result = convert_parkour_dataset(
            args.source_root,
            args.dataset_output,
            args.manifest_output,
            args.stats_output,
            source_git_root=args.source_git_root,
        )
    elif args.command == "train":
        arrays = load_dataset(args.dataset, DBILConfig())
        stats = DatasetStats.from_path(args.stats)
        result = train_checkpoint(
            arrays,
            stats,
            args.dataset,
            args.stats,
            args.checkpoint,
            epochs=args.epochs,
            batch_size=args.batch_size,
            learning_rate=args.learning_rate,
            device=args.device,
            max_train_samples=args.max_train_samples,
        )
    elif args.command == "convert-ur-trace":
        task_zft = None
        if args.task_zft_json is not None:
            task_payload = json.loads(args.task_zft_json.read_text(encoding="utf-8"))
            task_zft = task_payload.get("pose", task_payload) if isinstance(task_payload, dict) else task_payload
        result = convert_ur_bridge_trace(
            args.csv,
            args.dataset_output,
            args.manifest_output,
            command_kind=args.command_kind,
            calibration_sha256=args.calibration_sha256,
            frame_transform_sha256=args.frame_transform_sha256,
            task_zft=task_zft,
            calibration_artifact=args.calibration_artifact,
            frame_transform_artifact=args.frame_transform_artifact,
            task_zft_artifact=args.task_zft_artifact,
        )
    elif args.command == "ablate-ur-trace":
        predictor = TorchDBILShadowPredictor(
            args.checkpoint,
            args.stats,
            expected_checkpoint_sha256=args.checkpoint_sha256,
            expected_stats_sha256=args.stats_sha256,
            device=args.device,
        )
        result = run_shadow_policy_ablation(
            load_ur_trace_dataset(args.dataset),
            predictor,
            max_observations=args.max_observations,
        )
        result["model_hash"] = predictor.model_hash
        result["dataset_sha256"] = sha256_file(args.dataset)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(
            json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
    elif args.command in {
        "infer-window",
        "benchmark-inference",
        "benchmark-rates",
        "evaluate",
    }:
        predictor = TorchDBILShadowPredictor(
            args.checkpoint,
            args.stats,
            expected_checkpoint_sha256=args.checkpoint_sha256,
            expected_stats_sha256=args.stats_sha256,
            device=args.device,
        )
        if args.command == "evaluate":
            result = evaluate_shadow_predictor(
                predictor,
                load_dataset(args.dataset),
                dataset_path=args.dataset,
                split_id=args.split_id,
                max_samples=args.max_samples,
            )
            result["model_hash"] = predictor.model_hash
        else:
            payload = json.loads(args.observation_json.read_text(encoding="utf-8"))
            observation = _observation_from_mapping(payload)
        if args.command == "benchmark-rates":
            assert inference_module.__file__ is not None
            result = benchmark_paced_shadow_predictor(
                predictor,
                observation,
                duration_per_rate_s=args.duration_per_rate_s,
                warmup_iterations=args.warmup_iterations,
                artifact_bindings={
                    "checkpoint_sha256": sha256_file(args.checkpoint),
                    "stats_sha256": sha256_file(args.stats),
                    "observation_sha256": sha256_file(args.observation_json),
                    "harness_source_sha256": sha256_file(
                        Path(inference_module.__file__)
                    ),
                },
            )
            result["model_hash"] = predictor.model_hash
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(
                json.dumps(result, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
        elif args.command == "benchmark-inference":
            result = benchmark_shadow_predictor(
                predictor,
                observation,
                duration_s=args.duration_s,
                warmup_iterations=args.warmup_iterations,
            )
            result["model_hash"] = predictor.model_hash
        elif args.command == "infer-window":
            prediction = predictor.predict(observation)
            result = {
                "bundle_valid": True,
                "model_hash": predictor.model_hash,
                "observation_schema": 1,
                "inference_executed": True,
                "s_zft": list(prediction.s_zft.position_m)
                + list(prediction.s_zft.quaternion_wxyz),
                "confidence": prediction.confidence,
                "shadow_only": True,
                "active_enabled": False,
            }
    else:
        payload = json.loads(args.evidence.read_text(encoding="utf-8"))
        assert inference_module.__file__ is not None
        result = build_paced_timing_selection_manifest(
            payload,
            expected_bindings={
                "checkpoint_sha256": sha256_file(args.checkpoint),
                "stats_sha256": sha256_file(args.stats),
                "observation_sha256": sha256_file(args.observation_json),
                "harness_source_sha256": sha256_file(
                    Path(inference_module.__file__)
                ),
            },
        )
        if args.output is not None:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(
                json.dumps(result, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
