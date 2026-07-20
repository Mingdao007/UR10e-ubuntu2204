from __future__ import annotations

import argparse
import fcntl
import json
import os
import subprocess
from pathlib import Path
from typing import Sequence

from .canonical import canonical_sha256, file_sha256, write_json
from .dataset import build_historical_dataset
from .schema import validate_proposal_artifact


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Build an offline-only Step5d trial-level proposal.")
    parser.add_argument("--v2-db", type=Path, required=True)
    parser.add_argument("--v3-freeze", type=Path, required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--skip-source-digest-verification", action="store_true")
    return parser


def _git_sha() -> str | None:
    try:
        return subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=Path(__file__).resolve().parents[4],
            check=True, capture_output=True, text=True,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None


def _code_source_sha256() -> str:
    package_root = Path(__file__).resolve().parent
    sources = {
        path.relative_to(package_root).as_posix(): file_sha256(path)
        for path in sorted(package_root.glob("*.py"))
    }
    runner = package_root.parent / "run_step5d_offline_trial_rl.py"
    sources[f"../{runner.name}"] = file_sha256(runner)
    return canonical_sha256(sources)


def assert_no_runtime_contention(lock_root: Path | None = None) -> None:
    root = lock_root or Path(f"/tmp/ur10e-experiment-runtime-locks-{os.getuid()}")
    held: list[str] = []
    for name in ("live_writer.lock", "formal_timing.lock"):
        path = root / name
        if not path.exists():
            continue
        with path.open("rb") as stream:
            try:
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                held.append(name)
            else:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
    if held:
        raise RuntimeError(f"runtime contention locks are held: {', '.join(held)}")


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.output_root.exists() and any(args.output_root.iterdir()):
        raise SystemExit(f"refusing non-empty output root: {args.output_root}")
    assert_no_runtime_contention()
    os.environ["OPENBLAS_NUM_THREADS"] = "1"
    os.environ["OMP_NUM_THREADS"] = "1"
    from .policy import PolicyConfig, build_offline_proposal

    dataset = build_historical_dataset(
        args.v2_db.resolve(), args.v3_freeze.resolve(),
        verify_sources=not args.skip_source_digest_verification,
    )
    dataset_path = args.output_root / "historical_dataset_manifest.json"
    dataset_file_sha = write_json(dataset_path, dataset)
    proposal = build_offline_proposal(dataset, PolicyConfig())
    proposal["code_git_sha"] = _git_sha()
    proposal["code_source_sha256"] = _code_source_sha256()
    proposal["dataset_manifest_file_sha256"] = dataset_file_sha
    proposal["source_digests"] = [dict(source) for source in dataset["sources"]]
    proposal["artifact_fingerprint"] = canonical_sha256({key: value for key, value in proposal.items() if key != "artifact_fingerprint"})
    validate_proposal_artifact(proposal)
    proposal_path = args.output_root / "offline_policy_proposal.json"
    proposal_file_sha = write_json(proposal_path, proposal)
    quality = {
        "schema": "step5d.offline-trial-rl/data-quality-report-v1",
        "dataset_manifest": str(dataset_path),
        "dataset_manifest_file_sha256": dataset_file_sha,
        "dataset_sha256": dataset["dataset_sha256"],
        "counts": dataset["counts"],
        "source_digest_verification": not args.skip_source_digest_verification,
        "trial_rows_are_physical_trials_not_dense_samples": True,
        "v2_orientation_ko_inferred": False,
        "current_v3_optimizer_population_modified": False,
        "issues": proposal["blockers"],
    }
    quality_path = args.output_root / "data_quality_report.json"
    quality_file_sha = write_json(quality_path, quality)
    summary = {
        "schema": "step5d.offline-trial-rl/run-summary-v1",
        "result_status": proposal["result_status"],
        "dataset_manifest": {"path": str(dataset_path), "sha256": dataset_file_sha},
        "proposal_artifact": {"path": str(proposal_path), "sha256": proposal_file_sha},
        "data_quality_report": {"path": str(quality_path), "sha256": quality_file_sha},
    }
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
