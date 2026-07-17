"""Stable offline operator CLI for Step5d autotune v3."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
from decimal import Decimal
from pathlib import Path
from typing import Any, Mapping, Sequence

from .service import run_service
from .state import (
    CampaignPaths,
    StateError,
    campaign_history,
    campaign_status,
    control_lock,
    load_attempt_ledger,
    orchestration_fingerprint,
    physical_status,
    read_service_state,
    set_stop_latch,
)


BATCH_SCHEMA = "step5d.autotune-v3.candidate-batch/v1"
UNIT = "step5d-autotune-v3.service"


class CliError(RuntimeError):
    """An operator request violates the offline v3 contract."""


def _root_default() -> Path:
    return Path(__file__).resolve().parents[2]


def _campaign_default(root: Path) -> Path:
    return root / "runs" / "step5d_autotune_v3"


def _ledger_default(root: Path) -> Path:
    return root / "config" / "step5" / "step5d_autotune_v3_attempt_ledger.json"


def _reject_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON constant is forbidden: {value}")


def _unique_object(pairs: Sequence[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key is forbidden: {key}")
        result[key] = value
    return result


def _load_batch(path: Path) -> tuple[str, str, list[dict[str, Decimal]]]:
    if path.is_symlink() or not path.is_file():
        raise CliError("batch must be a real regular JSON file")
    try:
        payload = json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=_unique_object,
            parse_float=Decimal,
            parse_int=Decimal,
            parse_constant=_reject_constant,
        )
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        raise CliError(f"batch is not strict JSON: {exc}") from exc
    required = {"schema", "campaign_id", "source", "candidates"}
    if not isinstance(payload, dict) or set(payload) != required:
        raise CliError("batch fields differ")
    if payload["schema"] != BATCH_SCHEMA:
        raise CliError("batch schema differs")
    campaign_id = payload["campaign_id"]
    source = payload["source"]
    if not isinstance(campaign_id, str) or not campaign_id or len(campaign_id) > 160:
        raise CliError("batch campaign_id is invalid")
    if not isinstance(source, str) or not source.strip() or len(source) > 240:
        raise CliError("batch source is invalid")
    candidates = payload["candidates"]
    if not isinstance(candidates, list) or len(candidates) != 5:
        raise CliError("batch must contain exactly five candidates")
    expected_candidate = {"force_p_gain", "force_i_gain", "force_damping"}
    normalized: list[dict[str, Decimal]] = []
    for candidate in candidates:
        if not isinstance(candidate, dict) or set(candidate) != expected_candidate:
            raise CliError("candidate fields differ")
        row: dict[str, Decimal] = {}
        for name in sorted(expected_candidate):
            value = candidate[name]
            if isinstance(value, bool) or not isinstance(value, Decimal):
                raise CliError(f"candidate {name} must be a JSON number")
            if not value.is_finite() or value <= 0:
                raise CliError(f"candidate {name} must be finite and positive")
            row[name] = value
        normalized.append(row)
    return campaign_id, source.strip(), normalized


def _candidate_mapping(candidate: Mapping[str, Decimal]) -> dict[str, float]:
    from .profile import normalize_candidate

    checked = normalize_candidate({key: float(value) for key, value in candidate.items()})
    if not isinstance(checked, Mapping) or set(checked) != {
        "force_p_gain",
        "force_i_gain",
        "force_damping",
    }:
        raise CliError("candidate normalizer returned an incompatible mapping")
    return {key: float(value) for key, value in checked.items()}


def _validate_candidates(
    candidates: Sequence[Mapping[str, Decimal]],
    *,
    ledger_path: Path,
) -> tuple[list[Any], str, str]:
    from .launcher import check_effective_config
    from step5d_autotune_contract import ForceCandidate

    ledger = load_attempt_ledger(ledger_path)
    force_candidates: list[ForceCandidate] = []
    control_fingerprint: str | None = None
    execution_profile_id: str | None = None
    seen: set[str] = set()
    for raw_candidate in candidates:
        candidate = _candidate_mapping(raw_candidate)
        report = dict(check_effective_config(candidate=candidate))
        if report.get("ok") is not True:
            raise CliError("candidate effective-config check did not pass")
        candidate_fingerprint = report.get("control_fingerprint")
        profile_id = report.get("execution_profile_id")
        if not isinstance(candidate_fingerprint, str) or len(candidate_fingerprint) != 64:
            raise CliError("candidate check lacks control fingerprint")
        if not isinstance(profile_id, str) or not profile_id:
            raise CliError("candidate check lacks execution profile identity")
        if control_fingerprint not in {None, candidate_fingerprint}:
            raise CliError("candidate changed the frozen control fingerprint")
        if execution_profile_id not in {None, profile_id}:
            raise CliError("batch crosses execution profiles")
        attempted = ledger.attempted_group(candidate, profile_id)
        if attempted is not None:
            raise CliError(
                f"candidate repeats physically attempted tuple {attempted}; "
                "automatic retry is forbidden"
            )
        force_candidate = ForceCandidate(**candidate)
        if force_candidate.candidate_uid in seen:
            raise CliError("batch repeats an exact candidate")
        seen.add(force_candidate.candidate_uid)
        force_candidates.append(force_candidate)
        control_fingerprint = candidate_fingerprint
        execution_profile_id = profile_id
    assert control_fingerprint is not None and execution_profile_id is not None
    return force_candidates, control_fingerprint, execution_profile_id


def _append_batch(
    paths: CampaignPaths,
    *,
    campaign_id: str,
    source: str,
    candidates: Sequence[Any],
) -> Any:
    from step5d_autotune_batch_plan import append_batch, initialize_plan, load_plan

    with control_lock(paths):
        if paths.candidate_plan.exists():
            plan = load_plan(paths.candidate_plan, campaign_id=campaign_id)
            if plan.closed:
                raise CliError("candidate plan is already closed")
            return append_batch(paths.candidate_plan, candidates=candidates, source=source)
        paths.control.mkdir(parents=True, exist_ok=True, mode=0o700)
        temporary = paths.candidate_plan.with_name(
            f".{paths.candidate_plan.name}.{os.getpid()}.new"
        )
        try:
            initialize_plan(temporary, campaign_id=campaign_id)
            plan = append_batch(temporary, candidates=candidates, source=source)
            os.replace(temporary, paths.candidate_plan)
            directory_fd = os.open(
                paths.control, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
            )
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
            return plan
        finally:
            temporary.unlink(missing_ok=True)


def _queue_fingerprint(candidates: Sequence[Any]) -> str:
    encoded = json.dumps(
        [candidate.payload() for candidate in candidates],
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _active_service_matches_campaign(paths: CampaignPaths) -> bool:
    state = read_service_state(paths)
    if state.get("fresh") is not True or state.get("pid_alive") is not True:
        return False
    pid = state.get("pid")
    if type(pid) is not int:
        return False
    try:
        argv = (Path("/proc") / str(pid) / "cmdline").read_bytes().split(b"\0")
        tokens = [token.decode("utf-8", errors="strict") for token in argv if token]
    except (OSError, UnicodeError):
        return False
    try:
        index = tokens.index("--campaign-root")
    except ValueError:
        return False
    return (
        index + 1 < len(tokens)
        and Path(tokens[index + 1]).expanduser().absolute() == paths.root
        and "--_service" in tokens
    )


def _systemd_start(
    experiment_root: Path,
    campaign_root: Path,
    *,
    resume: bool,
) -> dict[str, Any]:
    active = subprocess.run(
        ["systemctl", "--user", "is-active", "--quiet", UNIT],
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    if active.returncode == 0:
        paths = CampaignPaths(campaign_root)
        if not _active_service_matches_campaign(paths):
            raise CliError(
                "active v3 unit is not fresh and exactly bound to the requested campaign root"
            )
        if resume:
            set_stop_latch(paths, armed=False)
        return {
            "unit": UNIT,
            "already_active": True,
            "started": False,
            "resume_cleared": resume,
        }
    script = experiment_root / "scripts" / "step5d-autotune-v3.sh"
    if script.is_symlink() or not script.is_file() or not os.access(script, os.X_OK):
        raise CliError("canonical v3 shell entrypoint is missing")
    command = [
        "systemd-run",
        "--user",
        "--unit=step5d-autotune-v3",
        "--property=Restart=no",
        "--property=KillMode=control-group",
        "--property=NoNewPrivileges=yes",
        "--property=PrivateDevices=yes",
        "--property=RestrictAddressFamilies=AF_UNIX",
        "--property=IPAddressDeny=any",
        "--collect",
        "--no-block",
        str(script),
        "--campaign-root",
        str(campaign_root),
        "--_service",
    ]
    if resume:
        command.append("--_service-resume")
    completed = subprocess.run(command, check=False)
    if completed.returncode != 0:
        raise CliError(f"offline systemd service start failed with rc={completed.returncode}")
    return {"unit": UNIT, "already_active": False, "started": True}


def _report(paths: CampaignPaths, ledger_path: Path) -> dict[str, Any]:
    outcomes = []
    for row in campaign_history(paths):
        evaluation = row.get("evaluation") or {}
        outcomes.append(
            {
                "trial_uid": row.get("trial_uid"),
                "candidate_uid": row.get("candidate_uid"),
                "disposition": evaluation.get("disposition"),
                "eligible": evaluation.get("eligible") is True,
                "objective_mae_n": evaluation.get("objective_mae_n"),
            }
        )
    ledger = load_attempt_ledger(ledger_path)
    return {
        "schema": "step5d.autotune-v3.report/v1",
        "status": campaign_status(paths),
        "attempt_ledger": {"sha256": ledger.sha256, "summary": dict(ledger.summary)},
        "outcomes": outcomes,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="step5d-autotune-v3")
    parser.add_argument("--experiment-root", type=Path, default=_root_default())
    parser.add_argument("--campaign-root", type=Path)
    parser.add_argument("--attempt-ledger", type=Path)
    parser.add_argument(
        "--_service", dest="internal_service", action="store_true", help=argparse.SUPPRESS
    )
    parser.add_argument(
        "--_service-resume",
        dest="internal_service_resume",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    commands = parser.add_subparsers(dest="command")
    start = commands.add_parser("start")
    start.add_argument("--check", action="store_true")
    start.add_argument("--json", action="store_true")
    start.add_argument("--resume", action="store_true")
    status = commands.add_parser("status")
    status.add_argument("--json", action="store_true")
    enqueue = commands.add_parser("enqueue")
    enqueue.add_argument("--batch", type=Path, required=True)
    report = commands.add_parser("report")
    report.add_argument("--json", action="store_true")
    commands.add_parser("stop-after-current")
    return parser


def _emit(payload: Mapping[str, Any], *, json_output: bool, text: str) -> None:
    print(json.dumps(payload, indent=2, sort_keys=True) if json_output else text)


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    experiment_root = args.experiment_root.expanduser().absolute()
    campaign_root = (
        _campaign_default(experiment_root)
        if args.campaign_root is None
        else args.campaign_root.expanduser().absolute()
    )
    ledger_path = (
        _ledger_default(experiment_root)
        if args.attempt_ledger is None
        else args.attempt_ledger.expanduser().absolute()
    )
    paths = CampaignPaths(campaign_root)
    try:
        if args.internal_service:
            if args.command is not None:
                raise CliError("internal service mode cannot include an operator command")
            return run_service(
                experiment_root,
                campaign_root,
                resume=args.internal_service_resume,
            )
        if args.internal_service_resume:
            raise CliError("internal service resume requires internal service mode")
        if args.command is None:
            build_parser().error("an operator command is required")
        if args.command == "start":
            from .launcher import check_effective_config

            check = dict(check_effective_config())
            if check.get("ok") is not True:
                raise CliError("effective control contract check did not pass")
            if args.check:
                _emit(check, json_output=args.json, text="effective control contract: ok")
                return 0
            started = _systemd_start(
                experiment_root, campaign_root, resume=args.resume
            )
            payload = {**check, **started, "hardware_enabled": False}
            _emit(payload, json_output=args.json, text="offline v3 service start requested")
            return 0
        if args.command == "status":
            ledger = load_attempt_ledger(ledger_path)
            payload = {
                **campaign_status(paths),
                "attempt_ledger": {
                    "sha256": ledger.sha256,
                    "summary": dict(ledger.summary),
                },
            }
            _emit(
                payload,
                json_output=args.json,
                text=(
                    f"phase={payload['service']['phase']} "
                    f"queue_revision={payload['queue']['revision']} "
                    f"stop_after_current={str(payload['stop_after_current']['armed']).lower()}"
                ),
            )
            return 0
        if args.command == "enqueue":
            campaign_id, source, raw_candidates = _load_batch(args.batch)
            candidates, control_fp, profile_id = _validate_candidates(
                raw_candidates, ledger_path=ledger_path
            )
            plan = _append_batch(
                paths,
                campaign_id=campaign_id,
                source=source,
                candidates=candidates,
            )
            payload = {
                "ok": True,
                "campaign_id": plan.campaign_id,
                "candidate_count": len(candidates),
                "execution_profile_id": profile_id,
                "control_fingerprint": control_fp,
                "orchestration_fingerprint": orchestration_fingerprint(experiment_root),
                "queue_revision": plan.revision,
                "queue_fingerprint": _queue_fingerprint(candidates),
                "restart_triggered": False,
                "release_gate_triggered": False,
            }
            print(json.dumps(payload, sort_keys=True))
            return 0
        if args.command == "report":
            payload = _report(paths, ledger_path)
            if args.json:
                print(json.dumps(payload, indent=2, sort_keys=True))
            else:
                print(
                    "# Step5d Autotune v3 offline report\n\n"
                    f"- Queue revision: {payload['status']['queue']['revision']}\n"
                    f"- Recorded v1 outcomes: {len(payload['outcomes'])}\n"
                    "- Hardware enabled: false\n"
                )
            return 0
        if args.command == "stop-after-current":
            latch = set_stop_latch(paths, armed=True)
            physical = physical_status(paths)
            print(
                json.dumps(
                    {
                        "ok": True,
                        "armed": True,
                        "revision": latch["revision"],
                        "immediate_when_observed": physical["safe_to_stop"],
                        "trial_active": physical["trial_active"],
                    },
                    sort_keys=True,
                )
            )
            return 0
    except (RuntimeError, StateError, OSError, ValueError) as exc:
        print(f"refusing: {exc}", file=sys.stderr)
        return 2
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
