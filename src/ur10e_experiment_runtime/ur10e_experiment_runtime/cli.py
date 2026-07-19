"""Thin ``ur-exp`` manifest CLI.

This module contains command parsing and delegation only.  It has no Step
dispatch, trajectory formulas, transport setup, ROS imports, or live actions.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any, Mapping, Sequence

from .contracts import ExperimentRuntimeError
from .failure_to_guard import (
    FailureToGuardError,
    build_change_contract,
    load_failure_ledger,
)
from .identity import StrictJSONError, canonical_json_bytes, load_strict_json
from .runtime import (
    load_experiment_spec,
    plan_experiment,
    resume_campaign,
    run_experiment,
    start_campaign,
    status,
)


def _default_output_root() -> Path:
    return Path.cwd() / "ur-exp-runs"


def _emit(document: Any, *, stream: Any | None = None) -> None:
    target = sys.stdout if stream is None else stream
    target.write(canonical_json_bytes(document).decode("utf-8"))
    target.write("\n")


def _handle_validate(args: argparse.Namespace) -> int:
    spec = load_experiment_spec(args.spec)
    _emit(
        {
            "schema": "ur10e.validate_result/v1",
            "valid": True,
            "experiment_id": spec.experiment_id,
            "experiment_fingerprint": spec.fingerprint,
        }
    )
    return 0


def _handle_plan(args: argparse.Namespace) -> int:
    spec = load_experiment_spec(args.spec)
    plan = plan_experiment(spec, args.lane)
    if args.touched_path:
        governance_root = args.governance_root or (
            Path.cwd()
            / "experiments/tase-contact-reproduction/config/failure_to_guard"
        )
        registry = load_strict_json(governance_root / "invariant_registry_v1.json")
        coverage = load_strict_json(governance_root / "coverage_map_v1.json")
        if not isinstance(registry, Mapping) or not isinstance(coverage, Mapping):
            raise FailureToGuardError("governance registry and coverage must be objects")
        plan["change_contract"] = build_change_contract(
            args.touched_path,
            registry,
            coverage,
            failure_ledger_entries=load_failure_ledger(
                governance_root / "failure_ledger_v1.jsonl"
            ),
        ).to_dict()
    else:
        plan["change_contract"] = None
    _emit(plan)
    return 0


def _handle_run(args: argparse.Namespace) -> int:
    spec = load_experiment_spec(args.spec)
    manifest = run_experiment(
        spec,
        args.lane,
        output_root=args.output_root,
    )
    _emit(
        {
            "schema": "ur10e.run_result/v1",
            "mode": "offline_scaffold_only",
            "run_uid": manifest.run_uid,
            "manifest_path": str(manifest.source_path),
            "external_actions": [],
        }
    )
    return 0


def _handle_status(args: argparse.Namespace) -> int:
    _emit(status(args.identifier, output_root=args.output_root))
    return 0


def _handle_campaign_start(args: argparse.Namespace) -> int:
    spec = load_experiment_spec(args.spec)
    start_campaign(spec, args.lane, args.authorization_ref)
    return 0  # pragma: no cover - fail-closed contract


def _handle_campaign_resume(args: argparse.Namespace) -> int:
    resume_campaign(args.campaign_uid, args.authorization_ref)
    return 0  # pragma: no cover - fail-closed contract


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ur-exp",
        description="Validate and scaffold immutable UR10e experiment manifests.",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    validate_parser = commands.add_parser("validate")
    validate_parser.add_argument("spec", type=Path)
    validate_parser.set_defaults(handler=_handle_validate)

    plan_parser = commands.add_parser("plan")
    plan_parser.add_argument("spec", type=Path)
    plan_parser.add_argument("--lane", required=True)
    plan_parser.add_argument(
        "--touched-path",
        action="append",
        default=[],
        help="repository-relative path used to generate a Change Contract",
    )
    plan_parser.add_argument(
        "--governance-root",
        type=Path,
        help="directory containing the frozen Failure-to-Guard registry files",
    )
    plan_parser.set_defaults(handler=_handle_plan)

    run_parser = commands.add_parser("run")
    run_parser.add_argument("spec", type=Path)
    run_parser.add_argument(
        "--lane",
        required=True,
        choices=("offline", "replay", "gazebo", "ursim", "hil"),
    )
    run_parser.add_argument("--output-root", type=Path, default=_default_output_root())
    run_parser.set_defaults(handler=_handle_run)

    status_parser = commands.add_parser("status")
    status_parser.add_argument("identifier")
    status_parser.add_argument("--output-root", type=Path, default=_default_output_root())
    status_parser.set_defaults(handler=_handle_status)

    campaign_parser = commands.add_parser("campaign")
    campaign_commands = campaign_parser.add_subparsers(
        dest="campaign_command", required=True
    )
    start_parser = campaign_commands.add_parser("start")
    start_parser.add_argument("spec", type=Path)
    start_parser.add_argument("--lane", required=True, choices=("live_autotune",))
    start_parser.add_argument("--authorization-ref", required=True)
    start_parser.set_defaults(handler=_handle_campaign_start)

    resume_parser = campaign_commands.add_parser("resume")
    resume_parser.add_argument("campaign_uid")
    resume_parser.add_argument("--authorization-ref", required=True)
    resume_parser.set_defaults(handler=_handle_campaign_resume)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.handler(args))
    except (ExperimentRuntimeError, FailureToGuardError, StrictJSONError) as exc:
        _emit(
            {
                "schema": "ur10e.cli_error/v1",
                "error": type(exc).__name__,
                "message": str(exc),
                "external_actions": [],
            },
            stream=sys.stderr,
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
