#!/usr/bin/env python3
"""Offline-safe CLI for the lightweight Step5d Remote preparation release."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
_RUNTIME_PYTHON_ROOT = _REPOSITORY_ROOT / "src/ur10e_experiment_runtime"
if str(_RUNTIME_PYTHON_ROOT) not in sys.path:
    sys.path.insert(0, str(_RUNTIME_PYTHON_ROOT))

from step5d_remote_control.contracts import (
    DEFAULT_RELEASE_PATH,
    RemoteControlError,
    import_result,
    load_json_object,
    load_prepared_control_trial,
    load_remote_release,
    materialize_controller_config,
    prepare_control_trial,
    write_json_atomic,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--release", type=Path, default=DEFAULT_RELEASE_PATH)
    commands = parser.add_subparsers(dest="command", required=True)

    validate = commands.add_parser("validate-release")
    validate.add_argument("--controller-config-out", type=Path)

    prepare = commands.add_parser("prepare-trial")
    prepare.add_argument("--input", type=Path, required=True)
    prepare.add_argument("--output", type=Path, required=True)

    replay = commands.add_parser("replay")
    replay.add_argument("--observations", type=Path, required=True)
    replay.add_argument("--diagnostics-csv", type=Path, required=True)
    replay.add_argument("--summary-out", type=Path, required=True)

    live = commands.add_parser("run-single-trial")
    live.add_argument("--prepared-trial", type=Path, required=True)
    live.add_argument("--live", action="store_true", required=True)

    result = commands.add_parser("import-result")
    result.add_argument("--prepared-trial", type=Path, required=True)
    result.add_argument("--receipt", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "replay":
            from step5d_autotune_v3.runtime_calibration import (
                RuntimeCalibrationError,
                bootstrap_stable_cuda_runtime,
            )

            try:
                bootstrap_stable_cuda_runtime()
            except RuntimeCalibrationError as exc:
                raise RemoteControlError(
                    f"inherited V3 CUDA runtime is unavailable: {exc}"
                ) from exc
        release = load_remote_release(args.release)
        if args.command == "validate-release":
            if args.controller_config_out is not None:
                write_json_atomic(
                    args.controller_config_out,
                    materialize_controller_config(release),
                )
            result = {
                "ok": True,
                "state": "offline_ready",
                "live_certified": False,
                "release_sha256": release.release_sha256,
                "transport_id": release.transport_id,
                "control_parameters": dict(release.control_parameters),
            }
        elif args.command == "prepare-trial":
            source = load_json_object(args.input, role="Remote trial source")
            prepared = prepare_control_trial(source, release=release)
            write_json_atomic(args.output, prepared.bundle())
            result = {
                "ok": True,
                "state": "prepared_offline",
                "trial_uid": prepared.trial_uid,
                "occurrence_uid": prepared.occurrence_uid,
                "transport_candidate_uid": prepared.transport_candidate_uid,
                "envelope_sha256": prepared.envelope_sha256,
                "output": str(args.output.resolve()),
            }
        elif args.command == "replay":
            from step5d_remote_control.runtime import replay_jsonl

            result = replay_jsonl(
                args.observations,
                release=release,
                diagnostics_csv=args.diagnostics_csv,
            )
            write_json_atomic(args.summary_out, result)
        elif args.command == "run-single-trial":
            from step5d_remote_control.runtime import reject_live_run

            bundle = load_json_object(
                args.prepared_trial, role="Remote prepared trial bundle"
            )
            load_prepared_control_trial(bundle, release=release)
            return reject_live_run(release, output=sys.stdout)
        elif args.command == "import-result":
            bundle = load_json_object(
                args.prepared_trial, role="Remote prepared trial bundle"
            )
            prepared = load_prepared_control_trial(bundle, release=release)
            receipt = load_json_object(args.receipt, role="Remote trial receipt")
            result = import_result(receipt, prepared=prepared, release=release)
        else:  # pragma: no cover - argparse owns the command set.
            raise AssertionError(args.command)
    except (RemoteControlError, OSError, ValueError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, sort_keys=True), file=sys.stderr)
        return 2
    print(json.dumps(result, sort_keys=True))
    return 0 if result.get("ok") is True else 4


if __name__ == "__main__":
    raise SystemExit(main())
