#!/usr/bin/env python3
"""Run the production parameter receiver inside one isolated offline lane.

The adapter validates an immutable local release candidate and its canonical
release-contract certificate. It replaces only the external live authority
and launch validators plus the queue-exhaustion termination boundary. The
production _send and _run_trial functions remain unchanged.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Any, Mapping


ROOT = Path(__file__).resolve().parents[1]
TOOLS = ROOT / "tools"
RUNTIME_SRC = ROOT.parents[1] / "src" / "ur10e_experiment_runtime"
sys.path.insert(0, str(RUNTIME_SRC))
sys.path.insert(0, str(TOOLS))

import run_step5d_parameter_campaign as production_runner  # noqa: E402
from step5d_autotune_v3.release_certificate import (  # noqa: E402
    load_release_certificate,
)
from step5d_autotune_v3.release_contract import (  # noqa: E402
    release_contract_scope_for_release,
)
from step5d_autotune_v3.release_identity import (  # noqa: E402
    load_local_release_candidate,
)
from step5d_parameter_queue import status as receiver_status  # noqa: E402


class OfflineCompositionComplete(RuntimeError):
    """The exact isolated queue reached its requested terminal count."""


def _inside(root: Path, path: Path, role: str) -> Path:
    resolved_root = root.resolve(strict=True)
    unresolved = path.expanduser()
    if unresolved.is_symlink():
        raise production_runner.ParameterCampaignError(f"{role} is a symlink")
    resolved = unresolved.resolve()
    try:
        resolved.relative_to(resolved_root)
    except ValueError as exc:
        raise production_runner.ParameterCampaignError(
            f"{role} escapes isolated lane"
        ) from exc
    return resolved


def _validate_binding(
    binding: Mapping[str, Any],
    *,
    release: Any,
    trial_count: int,
) -> None:
    required = {
        "schema",
        "campaign_id",
        "campaign_epoch",
        "campaign_fingerprint",
        "release_manifest_sha256",
        "trial_count",
        "claim_class",
    }
    if not isinstance(binding, Mapping) or set(binding) != required:
        raise production_runner.ParameterCampaignError(
            "offline campaign binding fields differ"
        )
    if (
        binding["schema"] != "step5d.offline-no-motion/campaign-binding-v1"
        or binding["campaign_epoch"] != 1
        or binding["release_manifest_sha256"] != release.manifest_sha256
        or binding["trial_count"] != trial_count
        or binding["claim_class"] != "offline_no_motion_production_composition"
    ):
        raise production_runner.ParameterCampaignError(
            "offline campaign binding identity differs"
        )


def run(args: argparse.Namespace) -> None:
    if args.trial_count not in {1, 10, 100}:
        raise production_runner.ParameterCampaignError(
            "offline trial count must be exactly 1, 10, or 100"
        )
    lane_root = args.lane_root.resolve(strict=True)
    for role in (
        "bridge_run",
        "campaign_root",
        "receiver_root",
        "mailbox",
        "runner_ready_file",
        "campaign_binding",
    ):
        _inside(lane_root, getattr(args, role), role)
    release, _descriptor = load_local_release_candidate(ROOT, args.release_candidate)
    expected_scope = release_contract_scope_for_release(ROOT, release)
    certificate, _evidence_path, _evidence = load_release_certificate(
        ROOT / "runs/step5d_autotune_v3",
        args.release_certificate,
        expected_scope=expected_scope,
    )
    binding = production_runner._strict_object(
        args.campaign_binding, "offline campaign binding"
    )
    _validate_binding(binding, release=release, trial_count=args.trial_count)
    if (
        args.release_manifest_sha256 != release.manifest_sha256
        or args.v3_program_id != release.program_id
        or certificate["scope"]["release_manifest_sha256"] != release.manifest_sha256
    ):
        raise production_runner.ParameterCampaignError(
            "offline runner release identity differs"
        )

    def validate_offline_authority(
        runner_args: argparse.Namespace,
        runner_binding: Mapping[str, Any],
    ) -> None:
        if runner_args is not args or runner_binding != binding:
            raise production_runner.ParameterCampaignError(
                "offline authority adapter binding differs"
            )

    def validate_offline_launch(
        runner_args: argparse.Namespace,
        runner_binding: Mapping[str, Any],
    ) -> None:
        if (
            runner_args is not args
            or runner_binding != binding
            or release.manifest_sha256
            != certificate["scope"]["release_manifest_sha256"]
        ):
            raise production_runner.ParameterCampaignError(
                "offline launch adapter binding differs"
            )

    original_wait = production_runner._wait_next_dispatch

    def wait_or_complete(runner_args, follower, observation):
        state = receiver_status(runner_args.receiver_root)
        if (
            state["attempted_count"] == args.trial_count
            and state["pending_count"] == 0
            and state["inflight"] is None
        ):
            raise OfflineCompositionComplete
        return original_wait(runner_args, follower, observation)

    original_authority = production_runner._validate_authority
    original_launch = production_runner._validate_launch_identity
    try:
        production_runner._validate_authority = validate_offline_authority
        production_runner._validate_launch_identity = validate_offline_launch
        production_runner._wait_next_dispatch = wait_or_complete
        production_runner.run(args)
    except OfflineCompositionComplete:
        return
    finally:
        production_runner._validate_authority = original_authority
        production_runner._validate_launch_identity = original_launch
        production_runner._wait_next_dispatch = original_wait


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--lane-root", type=Path, required=True)
    parser.add_argument("--release-candidate", type=Path, required=True)
    parser.add_argument("--release-certificate", type=Path, required=True)
    parser.add_argument("--trial-count", type=int, required=True)
    parser.add_argument("--experiment-root", type=Path, required=True)
    parser.add_argument("--bridge-run", type=Path, required=True)
    parser.add_argument("--campaign-root", type=Path, required=True)
    parser.add_argument("--receiver-root", type=Path, required=True)
    parser.add_argument("--mailbox", type=Path, required=True)
    parser.add_argument("--runner-ready-file", type=Path, required=True)
    parser.add_argument("--campaign-binding", type=Path, required=True)
    parser.add_argument("--campaign-lease", type=Path, required=True)
    parser.add_argument("--release-manifest-sha256", required=True)
    parser.add_argument("--v3-launch-profile", type=Path, required=True)
    parser.add_argument("--v3-program-id", required=True)
    parser.add_argument("--launch-basis", type=Path, required=True)
    parser.add_argument("--launch-basis-sha256", required=True)
    parser.add_argument("--delivery-observation", type=Path, required=True)
    parser.add_argument("--campaign-prepare", type=Path, required=True)
    parser.add_argument("--admission", type=Path, required=True)
    parser.add_argument("--canonical-owner-pid", type=int, required=True)
    parser.add_argument("--canonical-owner-starttime", type=int, required=True)
    parser.add_argument("--authority-epoch", type=int, required=True)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        run(args)
    except Exception as exc:
        print(
            f"offline parameter composition blocked: {type(exc).__name__}:{exc}",
            file=sys.stderr,
        )
        return 2
    print(
        json.dumps(
            {
                "ok": True,
                "claim_class": "offline_no_motion_production_composition",
                "trial_count": args.trial_count,
                "controller_io": False,
                "live_authorization": False,
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
