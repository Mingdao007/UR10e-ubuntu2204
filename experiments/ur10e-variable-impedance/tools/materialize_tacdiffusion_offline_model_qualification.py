"""Materialize and validate offline TacDiffusion model/candidate evidence.

The default command is gate-deferred: it validates the frozen campaign,
creates no checkpoint, and never queries CUDA.  CUDA execution requires both
``--gate-open`` and ``--execute-cuda`` so the external gate is explicit.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys


EXPERIMENT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(EXPERIMENT_ROOT))

from ur10e_vic.tacdiffusion import offline_model_qualification as qualification  # noqa: E402


DEFAULT_CAMPAIGN_ROOT = EXPERIMENT_ROOT / "evidence" / "tacdiffusion_offline_fixture_campaign_v1"
DEFAULT_QUALIFICATION_ROOT = EXPERIMENT_ROOT / "evidence" / "tacdiffusion_offline_model_qualification_v1"
DEFAULT_K800_ROOT = EXPERIMENT_ROOT / "evidence" / "tacdiffusion_inactive_k800_candidate_v1"


def _materialize_deferred(output_root: Path, campaign_root: Path, repo_root: Path) -> dict[str, object]:
    if output_root.exists() and any(output_root.iterdir()):
        qualification._validate_output_layout(output_root, qualification.DEFERRED_OUTPUT_NAMES, "gate-deferred")
    fixture = qualification.validate_fixture_campaign(campaign_root, repo_root=repo_root)
    contract = qualification.build_contract_payload(fixture, repo_root=repo_root)
    report = qualification.build_gate_deferred_evidence(
        campaign_root,
        repo_root=repo_root,
        output_root=output_root,
    )
    output_root.mkdir(parents=True, exist_ok=True)
    qualification._write_json_atomic(output_root / "qualification.contract.json", contract)
    qualification._write_json_atomic(output_root / "qualification.gate-deferred.json", report)
    qualification.validate_gate_deferred_evidence(
        output_root / "qualification.gate-deferred.json",
        bundle_root=campaign_root,
        repo_root=repo_root,
    )
    return {
        "status": report["status"],
        "cuda_execution_status": report["cuda_execution_status"],
        "artifact_path": str(output_root / "qualification.gate-deferred.json"),
        "artifact_sha256": report["artifact_sha256"],
        "contract_path": str(output_root / "qualification.contract.json"),
        "contract_sha256": contract["artifact_sha256"],
        "checkpoint_artifacts": [],
        "fixture_only": True,
        "production_promotion_allowed": False,
    }


def _validate_existing(qualification_root: Path, candidate_root: Path, campaign_root: Path, repo_root: Path) -> dict[str, object]:
    candidate = qualification.validate_k800_candidate(
        candidate_root / "candidate.manifest.json",
        repo_root=repo_root,
    )
    cuda_path = qualification_root / qualification.CUDA_ARTIFACT_NAME
    deferred_path = qualification_root / qualification.DEFERRED_ARTIFACT_NAME
    if cuda_path.is_file():
        qualification_artifact = qualification.validate_cuda_qualification_evidence(
            cuda_path,
            bundle_root=campaign_root,
            repo_root=repo_root,
        )
    elif deferred_path.is_file():
        qualification_artifact = qualification.validate_gate_deferred_evidence(
            deferred_path,
            bundle_root=campaign_root,
            repo_root=repo_root,
        )
    else:
        raise qualification.QualificationContractError("qualification output is partial: neither CUDA nor deferred report exists")
    return {
        "validation_status": "verified",
        "cuda_execution_status": qualification_artifact["cuda_execution_status"],
        "qualification_artifact_sha256": qualification_artifact["artifact_sha256"],
        "candidate_sha256": candidate["candidate_sha256"],
        "fixture_only": True,
        "production_promotion_allowed": False,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="materialize-tacdiffusion-offline-model-qualification")
    parser.add_argument("--campaign-root", default=str(DEFAULT_CAMPAIGN_ROOT))
    parser.add_argument("--qualification-output", default=str(DEFAULT_QUALIFICATION_ROOT))
    parser.add_argument("--candidate-output", default=str(DEFAULT_K800_ROOT))
    parser.add_argument("--repo-root", default=str(EXPERIMENT_ROOT.parent.parent))
    parser.add_argument("--validate-only", action="store_true")
    parser.add_argument("--gate-open", action="store_true", help="Root-authorized external CUDA gate is open")
    parser.add_argument("--execute-cuda", action="store_true", help="Actually run the one-worker CUDA qualification")
    args = parser.parse_args(argv)
    campaign_root = Path(args.campaign_root)
    qualification_root = Path(args.qualification_output)
    candidate_root = Path(args.candidate_output)
    repo_root = Path(args.repo_root)
    if args.execute_cuda and not args.gate_open:
        parser.error("--execute-cuda requires the explicit --gate-open gate")
    if args.validate_only:
        result = _validate_existing(qualification_root, candidate_root, campaign_root, repo_root)
    else:
        candidate = qualification.materialize_k800_candidate(candidate_root, repo_root=repo_root)
        if args.execute_cuda:
            report = qualification.run_cuda_qualification(
                campaign_root,
                output_root=qualification_root,
                repo_root=repo_root,
                gate_open=True,
            )
            result = {
                "status": report["status"],
                "cuda_execution_status": report["cuda_execution_status"],
                "qualification_artifact_path": str(qualification_root / "qualification.cuda.json"),
                "qualification_artifact_sha256": report["artifact_sha256"],
                "candidate_sha256": candidate["candidate_sha256"],
                "fixture_only": True,
                "production_promotion_allowed": False,
            }
        else:
            result = _materialize_deferred(qualification_root, campaign_root, repo_root)
            result["candidate_sha256"] = candidate["candidate_sha256"]
            result["candidate_manifest_path"] = str(candidate_root / "candidate.manifest.json")
    print(json.dumps(result, indent=2, sort_keys=True, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
