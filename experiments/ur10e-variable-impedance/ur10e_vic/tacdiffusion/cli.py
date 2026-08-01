"""Portable CLI for TacDiffusion dataset, model, inference, and timing."""

from __future__ import annotations

import argparse
from dataclasses import asdict
import json
from pathlib import Path
from typing import Sequence

from .benchmark import (
    benchmark_paced_predictor,
    validate_paced_benchmark_candidate,
)
from .dataset import (
    build_dataset_manifest,
    load_expert_dataset_npz,
    validate_dataset_manifest,
    write_dataset_manifest,
)
from .model import (
    evaluate_checkpoint,
    load_predictor,
    train_ddpm,
    validate_checkpoint_manifest,
)
from .offline_campaign import (
    materialize_offline_campaign_bundle,
    validate_offline_campaign_bundle,
)


DEFAULT_OFFLINE_CAMPAIGN_BUNDLE = (
    Path(__file__).resolve().parents[2]
    / "evidence"
    / "tacdiffusion_offline_fixture_campaign_v1"
)


def _write_json(path: str | Path, payload: object) -> None:
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.tmp")
    temporary.write_text(
        json.dumps(payload, indent=2, sort_keys=True, allow_nan=False) + "\n",
        encoding="utf-8",
    )
    temporary.replace(destination)


def _condition(path: str | Path) -> tuple[float, ...]:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if isinstance(payload, dict):
        payload = payload.get("condition")
    if not isinstance(payload, list) or len(payload) != 36:
        raise ValueError("condition JSON must be a 36-value list")
    return tuple(float(value) for value in payload)


def _trace_paths(args: argparse.Namespace) -> list[str]:
    paths = list(args.trace_manifest or [])
    if not paths:
        raise ValueError("at least one --trace-manifest artifact is required")
    return paths


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ur10e-tacdiffusion")
    subparsers = parser.add_subparsers(dest="command", required=True)

    validate_dataset = subparsers.add_parser("validate-dataset")
    validate_dataset.add_argument("--dataset", required=True)
    validate_dataset.add_argument("--manifest")
    validate_dataset.add_argument("--trace-manifest", action="append")

    write_manifest = subparsers.add_parser("write-dataset-manifest")
    write_manifest.add_argument("--dataset", required=True)
    write_manifest.add_argument("--manifest", required=True)
    write_manifest.add_argument("--trace-manifest", action="append", required=True)

    train = subparsers.add_parser("train")
    train.add_argument("--dataset", required=True)
    train.add_argument("--dataset-manifest", required=True)
    train.add_argument("--trace-manifest", action="append", required=True)
    train.add_argument("--checkpoint", required=True)
    train.add_argument("--checkpoint-manifest", required=True)
    train.add_argument("--epochs", type=int, default=1500)
    train.add_argument("--batch-size", type=int, default=4096)
    train.add_argument("--learning-rate", type=float, default=1e-3)
    train.add_argument("--device", default="auto")
    train.add_argument("--resume-checkpoint")
    train.add_argument(
        "--evidence-scope",
        choices=("offline_training", "fixture_only"),
        default="offline_training",
    )

    validate_checkpoint = subparsers.add_parser("validate-checkpoint")
    validate_checkpoint.add_argument("--checkpoint", required=True)
    validate_checkpoint.add_argument("--manifest", required=True)
    validate_checkpoint.add_argument("--expected-dataset-sha256")

    evaluate = subparsers.add_parser("evaluate")
    evaluate.add_argument("--checkpoint", required=True)
    evaluate.add_argument("--checkpoint-manifest", required=True)
    evaluate.add_argument("--dataset", required=True)
    evaluate.add_argument("--dataset-manifest", required=True)
    evaluate.add_argument("--trace-manifest", action="append", required=True)
    evaluate.add_argument(
        "--split", choices=("validation", "test"), default="validation"
    )
    evaluate.add_argument("--max-samples", type=int)
    evaluate.add_argument("--device", default="auto")
    evaluate.add_argument("--output")

    infer = subparsers.add_parser("infer")
    infer.add_argument("--checkpoint", required=True)
    infer.add_argument("--checkpoint-manifest", required=True)
    infer.add_argument("--condition-json", required=True)
    infer.add_argument("--seed", type=int, default=42)
    infer.add_argument("--device", default="auto")

    benchmark = subparsers.add_parser("benchmark")
    benchmark.add_argument("--checkpoint", required=True)
    benchmark.add_argument("--checkpoint-manifest", required=True)
    benchmark.add_argument("--condition-json", required=True)
    benchmark.add_argument("--duration-per-rate-s", type=float, default=60.0)
    benchmark.add_argument("--output", required=True)
    benchmark.add_argument("--device", default="auto")

    validate_benchmark = subparsers.add_parser("validate-benchmark")
    validate_benchmark.add_argument("--candidate", required=True)
    validate_benchmark.add_argument("--checkpoint-sha256", required=True)
    validate_benchmark.add_argument("--dataset-sha256", required=True)
    validate_benchmark.add_argument("--output")

    offline_campaign = subparsers.add_parser(
        "materialize-offline-campaign",
        help="materialize or verify the deterministic network-free fixture campaign",
    )
    offline_campaign.add_argument(
        "--output",
        default=str(DEFAULT_OFFLINE_CAMPAIGN_BUNDLE),
    )
    offline_campaign.add_argument("--validate-only", action="store_true")
    return parser


def run(args: argparse.Namespace) -> dict[str, object]:
    if args.command == "validate-dataset":
        if args.manifest:
            return validate_dataset_manifest(
                args.dataset, args.manifest, _trace_paths(args)
            )
        dataset = load_expert_dataset_npz(args.dataset)
        return {
            "validation_status": "dataset_structure_only",
            "artifact_chain_verified": False,
            "sample_count": dataset.sample_count,
            "episode_count": dataset.episode_count,
            "source_kind": dataset.source_kind,
            "training_eligible": False,
        }
    if args.command == "write-dataset-manifest":
        return write_dataset_manifest(
            args.dataset, args.manifest, _trace_paths(args)
        )
    if args.command == "train":
        return train_ddpm(
            dataset_path=args.dataset,
            dataset_manifest_path=args.dataset_manifest,
            expert_trace_manifest_paths=_trace_paths(args),
            checkpoint_path=args.checkpoint,
            checkpoint_manifest_path=args.checkpoint_manifest,
            epochs=args.epochs,
            batch_size=args.batch_size,
            learning_rate=args.learning_rate,
            device=args.device,
            resume_checkpoint_path=args.resume_checkpoint,
            evidence_scope=args.evidence_scope,
        )
    if args.command == "validate-checkpoint":
        return validate_checkpoint_manifest(
            args.checkpoint,
            args.manifest,
            expected_dataset_sha256=args.expected_dataset_sha256,
        )
    if args.command == "evaluate":
        result = evaluate_checkpoint(
            checkpoint_path=args.checkpoint,
            checkpoint_manifest_path=args.checkpoint_manifest,
            dataset_path=args.dataset,
            dataset_manifest_path=args.dataset_manifest,
            expert_trace_manifest_paths=_trace_paths(args),
            split=args.split,
            max_samples=args.max_samples,
            device=args.device,
        )
        if args.output:
            _write_json(args.output, result)
        return result
    if args.command in {"infer", "benchmark"}:
        checkpoint_manifest = validate_checkpoint_manifest(
            args.checkpoint, args.checkpoint_manifest
        )
        predictor = load_predictor(
            args.checkpoint,
            expected_checkpoint_sha256=str(
                checkpoint_manifest["checkpoint_sha256"]
            ),
            expected_dataset_sha256=str(checkpoint_manifest["dataset_sha256"]),
            device=args.device,
        )
        condition = _condition(args.condition_json)
        if args.command == "infer":
            return asdict(predictor.predict(condition, seed=args.seed))
        candidate = benchmark_paced_predictor(
            predictor,
            condition,
            duration_per_rate_s=args.duration_per_rate_s,
            checkpoint_sha256=str(checkpoint_manifest["checkpoint_sha256"]),
            dataset_sha256=str(checkpoint_manifest["dataset_sha256"]),
        )
        _write_json(args.output, candidate)
        return {
            "candidate_status": candidate["candidate_status"],
            "candidate_artifact": Path(args.output).name,
            "producer_selection_eligible": False,
            "producer_selected_rate_hz": None,
        }
    if args.command == "validate-benchmark":
        candidate = json.loads(Path(args.candidate).read_text(encoding="utf-8"))
        result = validate_paced_benchmark_candidate(
            candidate,
            expected_checkpoint_sha256=args.checkpoint_sha256,
            expected_dataset_sha256=args.dataset_sha256,
        )
        if args.output:
            _write_json(args.output, result)
        return result
    if args.command == "materialize-offline-campaign":
        if args.validate_only:
            manifest = validate_offline_campaign_bundle(args.output)
            return {
                "validation_status": "verified",
                "artifact_root": str(Path(args.output)),
                "artifact_root_digest": manifest["bundle_digest_sha256"],
            }
        result = materialize_offline_campaign_bundle(args.output)
        return {
            "validation_status": "materialized_and_verified",
            "artifact_root": str(result.root),
            "artifact_root_digest": result.artifact_root_digest,
            "campaign_counters": result.campaign_receipt["counters"],
            "dataset_shape": {
                "observations": result.dataset_manifest["observation_shape"],
                "actions": result.dataset_manifest["action_shape"],
            },
            "split_episode_counts": result.dataset_manifest["split_episode_counts"],
            "fixture_only": True,
            "production_promotion_allowed": False,
        }
    raise ValueError(f"unsupported command: {args.command}")


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        result = run(args)
    except (OSError, RuntimeError, ValueError, KeyError) as error:
        parser.exit(2, f"error: {error}\n")
    print(json.dumps(result, indent=2, sort_keys=True, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
