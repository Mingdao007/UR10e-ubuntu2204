"""Stable offline operator CLI for Step5d autotune v3."""

from __future__ import annotations

import argparse
import fcntl
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
    AttemptLedger,
    StateError,
    campaign_history,
    campaign_status,
    control_lock,
    load_attempt_ledger,
    orchestration_fingerprint,
    physical_status,
    read_service_state,
    set_stop_latch,
    atomic_json,
)


BATCH_SCHEMA = "step5d.autotune-v3.candidate-batch/v1"
TRIAL_BATCH_SCHEMA = "step5d.autotune-v3.trial-batch/v2"
TRIAL_BATCH_SCHEMA_V3 = "step5d.autotune-v3.trial-batch/v3"
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


def _load_batch(path: Path) -> tuple[str, str, list[dict[str, Any]]]:
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
    if payload["schema"] not in {
        BATCH_SCHEMA,
        TRIAL_BATCH_SCHEMA,
        TRIAL_BATCH_SCHEMA_V3,
    }:
        raise CliError("batch schema differs")
    campaign_id = payload["campaign_id"]
    source = payload["source"]
    if not isinstance(campaign_id, str) or not campaign_id or len(campaign_id) > 160:
        raise CliError("batch campaign_id is invalid")
    if not isinstance(source, str) or not source.strip() or len(source) > 240:
        raise CliError("batch source is invalid")
    candidates = payload["candidates"]
    required_count = 10 if payload["schema"] == TRIAL_BATCH_SCHEMA_V3 else 5
    if not isinstance(candidates, list) or len(candidates) != required_count:
        raise CliError(f"batch must contain exactly {required_count} candidates")
    from .runtime_profile import DEFAULT_OVERLAY, OVERLAY_FIELDS

    expected_candidate = (
        {"force_p_gain", "force_i_gain", "force_damping"}
        if payload["schema"] == BATCH_SCHEMA
        else set(OVERLAY_FIELDS) - {"control_candidate_uid"}
    )
    normalized: list[dict[str, Any]] = []
    for candidate in candidates:
        if not isinstance(candidate, dict) or set(candidate) != expected_candidate:
            raise CliError("candidate fields differ")
        row: dict[str, Any] = dict(DEFAULT_OVERLAY)
        for name in sorted(expected_candidate):
            value = candidate[name]
            if name == "execution_profile_id":
                if not isinstance(value, str) or not value:
                    raise CliError("candidate execution_profile_id must be a string")
                row[name] = value
                continue
            if isinstance(value, bool) or not isinstance(value, Decimal):
                raise CliError(f"candidate {name} must be a JSON number")
            if not value.is_finite() or value < 0:
                raise CliError(f"candidate {name} must be finite and non-negative")
            row[name] = value
        row.pop("control_candidate_uid", None)
        normalized.append(row)
    return campaign_id, source.strip(), normalized


def _candidate_mapping(candidate: Mapping[str, Any]) -> dict[str, float]:
    from .profile import normalize_candidate

    checked = normalize_candidate(
        {key: float(candidate[key]) for key in ("force_p_gain", "force_i_gain", "force_damping")}
    )
    if not isinstance(checked, Mapping) or set(checked) != {
        "force_p_gain",
        "force_i_gain",
        "force_damping",
    }:
        raise CliError("candidate normalizer returned an incompatible mapping")
    return {key: float(value) for key, value in checked.items()}


def _validate_candidates(
    candidates: Sequence[Mapping[str, Any]],
    *,
    ledger_path: Path | None = None,
    attempt_ledger: AttemptLedger | None = None,
    launch_profile_path: Path,
) -> tuple[list[Any], list[dict[str, Any]], str, str, str]:
    from .launcher import check_effective_config
    from step5d_autotune_contract import ForceCandidate

    if (ledger_path is None) == (attempt_ledger is None):
        raise CliError("exactly one attempt-ledger source is required")
    ledger = (
        load_attempt_ledger(ledger_path)
        if ledger_path is not None
        else attempt_ledger
    )
    assert ledger is not None
    force_candidates: list[ForceCandidate] = []
    control_fingerprint: str | None = None
    execution_profile_id: str | None = None
    launch_profile_fingerprint: str | None = None
    seen: set[str] = set()
    overlays: list[dict[str, Any]] = []
    from .runtime_profile import (
        DEFAULT_OVERLAY,
        comparison_profile_fingerprint,
        is_control_candidate_step,
        load_launch_profile,
        normalize_trial_overlay,
    )

    launch_profile = load_launch_profile(launch_profile_path)
    legacy_comparison = comparison_profile_fingerprint(launch_profile, DEFAULT_OVERLAY)
    for raw_candidate in candidates:
        candidate = _candidate_mapping(raw_candidate)
        raw_overlay = {
            key: (float(value) if isinstance(value, Decimal) else value)
            for key, value in raw_candidate.items()
        }
        overlay = normalize_trial_overlay(raw_overlay, profile=launch_profile)
        report = dict(
            check_effective_config(
                candidate=candidate,
                launch_profile_path=launch_profile_path,
                trial_overlay=overlay,
            )
        )
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
        comparison_fp = report.get("comparison_profile_fingerprint")
        attempted = (
            ledger.attempted_group(candidate, profile_id)
            if comparison_fp == legacy_comparison
            else None
        )
        if attempted is not None:
            raise CliError(
                f"candidate repeats physically attempted tuple {attempted}; "
                "automatic retry is forbidden"
            )
        force_candidate = ForceCandidate(**candidate)
        control_uid = overlay["control_candidate_uid"]
        if control_uid in seen:
            raise CliError("batch repeats an exact control candidate")
        if overlays and not is_control_candidate_step(overlays[-1], overlay):
            raise CliError(
                "adjacent V3 control candidates must change exactly one "
                "P/I/damping/orientation_ko coordinate by 0.25 octave"
            )
        seen.add(control_uid)
        force_candidates.append(force_candidate)
        overlays.append(overlay)
        control_fingerprint = candidate_fingerprint
        execution_profile_id = "per_trial_overlay"
        launch_profile_fingerprint = report.get("launch_profile_fingerprint")
    assert control_fingerprint is not None and execution_profile_id is not None
    assert isinstance(launch_profile_fingerprint, str)
    return (
        force_candidates,
        overlays,
        control_fingerprint,
        execution_profile_id,
        launch_profile_fingerprint,
    )


def _append_overlay_batch(
    paths: CampaignPaths,
    *,
    plan: Any,
    source: str,
    candidates: Sequence[Any],
    overlays: Sequence[Mapping[str, Any]],
    launch_profile_fingerprint: str,
) -> dict[str, Any]:
    if len(candidates) != len(overlays):
        raise CliError("candidate and overlay counts differ")
    if paths.trial_overlays.exists():
        from .state import read_strict_json

        current = read_strict_json(paths.trial_overlays, role="v3 trial overlay plan")
        if (
            not isinstance(current, dict)
            or current.get("schema")
            not in {
                "step5d.autotune-v3/trial-overlay-plan-v1",
                "step5d.autotune-v3/trial-overlay-plan-v2",
            }
            or current.get("revision") != plan.revision - 1
            or current.get("launch_profile_fingerprint") != launch_profile_fingerprint
        ):
            raise CliError("existing V3 trial-overlay plan is not append-compatible")
        batches = list(current["batches"])
    else:
        if plan.revision != 1:
            raise CliError("cannot attach V3 overlays to a pre-existing candidate plan")
        batches = []
    occurrences = (
        plan.occurrences[plan.revision - 1]
        if getattr(plan, "occurrences", ())
        else ()
    )
    if occurrences and len(occurrences) != len(candidates):
        raise CliError("rolling occurrence and overlay counts differ")
    from .runtime_profile import load_launch_profile, normalized_overlay_sha256

    overlay_profile = load_launch_profile()
    if overlay_profile.fingerprint != launch_profile_fingerprint:
        raise CliError("overlay writer launch-profile identity differs")

    batches.append(
        {
            "batch_id": plan.revision,
            "source": source,
            "trials": [
                {
                    "occurrence_uid": (
                        occurrences[index].occurrence_uid
                        if occurrences
                        else candidate.candidate_uid
                    ),
                    "transport_candidate_uid": (
                        occurrences[index].transport_candidate_uid
                        if occurrences
                        else candidate.candidate_uid
                    ),
                    "control_candidate_uid": overlay["control_candidate_uid"],
                    "normalized_overlay_sha256": normalized_overlay_sha256(
                        overlay_profile, overlay
                    ),
                    "overlay": dict(overlay),
                }
                for index, (candidate, overlay) in enumerate(
                    zip(candidates, overlays, strict=True)
                )
            ],
        }
    )
    candidate_count = sum(len(batch["trials"]) for batch in batches)
    fingerprint_material = {
        "launch_profile_fingerprint": launch_profile_fingerprint,
        "batches": batches,
    }
    fingerprint = hashlib.sha256(
        json.dumps(
            fingerprint_material,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()
    payload = {
        "schema": "step5d.autotune-v3/trial-overlay-plan-v2",
        "revision": plan.revision,
        "candidate_count": candidate_count,
        "launch_profile_fingerprint": launch_profile_fingerprint,
        "fingerprint": fingerprint,
        "batches": batches,
    }
    atomic_json(paths.trial_overlays, payload)
    return payload


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
            initialize_plan(
                temporary,
                campaign_id=campaign_id,
                batch_size=len(candidates),
            )
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
    parser.add_argument("--launch-profile", type=Path)
    parser.add_argument(
        "--_service", dest="internal_service", action="store_true", help=argparse.SUPPRESS
    )
    parser.add_argument(
        "--_service-resume",
        dest="internal_service_resume",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--_launch-attempt-id", dest="internal_launch_attempt_id", help=argparse.SUPPRESS
    )
    parser.add_argument(
        "--_launch-attempt-state",
        dest="internal_launch_attempt_state",
        choices=("STARTED", "PASSED", "COMPLETED", "FAILED", "CANCELLED"),
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--_launch-attempt-phase",
        dest="internal_launch_attempt_phase",
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--_launch-attempt-exit-code",
        dest="internal_launch_attempt_exit_code",
        type=int,
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--_launch-attempt-reason-code",
        dest="internal_launch_attempt_reason_code",
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--_launch-attempt-detail",
        dest="internal_launch_attempt_detail",
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--_launch-attempt-external-evidence",
        dest="internal_launch_attempt_external_evidence",
        type=Path,
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--_launch-attempt-route", dest="internal_launch_attempt_route", help=argparse.SUPPRESS
    )
    parser.add_argument(
        "--_launch-manifest-sha256", dest="internal_launch_manifest_sha256", help=argparse.SUPPRESS
    )
    parser.add_argument(
        "--_launch-repository-head", dest="internal_launch_repository_head", help=argparse.SUPPRESS
    )
    parser.add_argument(
        "--_launch-runtime-environment-id",
        dest="internal_launch_runtime_environment_id",
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--_launch-campaign-path",
        dest="internal_launch_campaign_path",
        type=Path,
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--_launch-output-root",
        dest="internal_launch_output_root",
        type=Path,
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--_launch-owner-pid", dest="internal_launch_owner_pid", type=int, help=argparse.SUPPRESS
    )
    parser.add_argument(
        "--_launch-owner-starttime",
        dest="internal_launch_owner_starttime",
        type=int,
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--_launch-owner-authority-epoch",
        dest="internal_launch_owner_authority_epoch",
        type=int,
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--_launch-route-snapshot",
        dest="internal_launch_route_snapshot",
        type=Path,
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
    launch_profile_path = (
        experiment_root / "config/step5/step5d_autotune_v3_launch_profile.json"
        if args.launch_profile is None
        else args.launch_profile.expanduser().absolute()
    )
    paths = CampaignPaths(campaign_root)
    try:
        launch_attempt_mode = any(
            value is not None
            for value in (
                args.internal_launch_attempt_id,
                args.internal_launch_attempt_state,
                args.internal_launch_attempt_phase,
                args.internal_launch_attempt_exit_code,
                args.internal_launch_attempt_reason_code,
                args.internal_launch_attempt_detail,
                args.internal_launch_attempt_external_evidence,
                args.internal_launch_attempt_route,
                args.internal_launch_manifest_sha256,
                args.internal_launch_repository_head,
                args.internal_launch_runtime_environment_id,
                args.internal_launch_campaign_path,
                args.internal_launch_output_root,
                args.internal_launch_owner_pid,
                args.internal_launch_owner_starttime,
                args.internal_launch_owner_authority_epoch,
                args.internal_launch_route_snapshot,
            )
        )
        if launch_attempt_mode:
            if args.command is not None or args.internal_service or args.internal_service_resume:
                raise CliError(
                    "internal launch-attempt recording cannot include another mode"
                )
            if (
                args.internal_launch_attempt_id is None
                or args.internal_launch_attempt_state is None
                or args.internal_launch_attempt_phase is None
            ):
                raise CliError("internal launch-attempt recording fields are incomplete")
            from .governance import (
                publish_launch_attempt,
                read_proc_starttime_ticks,
            )

            external_reference = None
            external_path = args.internal_launch_attempt_external_evidence
            if external_path is not None:
                unresolved = external_path.expanduser().absolute()
                if unresolved.is_symlink() or not unresolved.is_file():
                    raise CliError("launch-attempt external evidence must be a real file")
                resolved = unresolved.resolve(strict=True)
                try:
                    relative = resolved.relative_to(campaign_root.resolve())
                except ValueError as exc:
                    raise CliError(
                        "launch-attempt external evidence must stay under campaign root"
                    ) from exc
                external_reference = {
                    "path": relative.as_posix(),
                    "sha256": hashlib.sha256(resolved.read_bytes()).hexdigest(),
                }
            binding_values = (
                args.internal_launch_repository_head,
                args.internal_launch_runtime_environment_id,
                args.internal_launch_campaign_path,
                args.internal_launch_output_root,
                args.internal_launch_owner_pid,
                args.internal_launch_owner_starttime,
                args.internal_launch_owner_authority_epoch,
            )
            bindings = None
            if any(value is not None for value in binding_values):
                if any(value is None for value in binding_values):
                    raise CliError("launch-attempt v2 bindings are incomplete")
                route_reference = None
                if args.internal_launch_route_snapshot is not None:
                    snapshot = args.internal_launch_route_snapshot.expanduser().absolute()
                    if snapshot.is_symlink() or not snapshot.is_file():
                        raise CliError("launch route snapshot must be a real file")
                    route_reference = {
                        "path": str(snapshot.resolve(strict=True)),
                        "sha256": hashlib.sha256(snapshot.read_bytes()).hexdigest(),
                    }
                bindings = {
                    "repository_head": args.internal_launch_repository_head,
                    "runtime_environment_id": args.internal_launch_runtime_environment_id,
                    "campaign_root": str(args.internal_launch_campaign_path.expanduser().absolute()),
                    "output_root": str(args.internal_launch_output_root.expanduser().absolute()),
                    "resource_owner": {
                        "pid": args.internal_launch_owner_pid,
                        "starttime_ticks": args.internal_launch_owner_starttime,
                        "authority_epoch": args.internal_launch_owner_authority_epoch,
                    },
                    "route_snapshot": route_reference,
                }
            if bindings is None:
                raise CliError(
                    "legacy unbound launch-attempt recording is retired"
                )
            from step5d_bridge_authority import (
                BridgeAuthorityError,
                LOCK_FILE as BRIDGE_AUTHORITY_LOCK_FILE,
                load_current as load_bridge_authority,
            )

            authority_lock = (campaign_root / BRIDGE_AUTHORITY_LOCK_FILE).open(
                "a+"
            )
            fcntl.flock(authority_lock.fileno(), fcntl.LOCK_EX)
            try:
                try:
                    authority = load_bridge_authority(campaign_root)
                except (OSError, ValueError, BridgeAuthorityError) as exc:
                    raise CliError(
                        f"launch-attempt owner authority is invalid: {exc}"
                    ) from exc
                owner = bindings["resource_owner"]
                if (
                    authority is None
                    or authority.get("state") != "ACTIVE"
                    or authority.get("attempt_id")
                    != args.internal_launch_attempt_id
                    or authority.get("sequence") != owner["authority_epoch"]
                    or authority.get("owner")
                    != {
                        "pid": owner["pid"],
                        "starttime_ticks": owner["starttime_ticks"],
                    }
                    or os.getppid() != owner["pid"]
                    or read_proc_starttime_ticks(owner["pid"])
                    != owner["starttime_ticks"]
                ):
                    raise CliError(
                        "launch-attempt recorder is not the active authority-owner child"
                    )
                recorded = publish_launch_attempt(
                    campaign_root,
                    attempt_id=args.internal_launch_attempt_id,
                    state=args.internal_launch_attempt_state,
                    phase=args.internal_launch_attempt_phase,
                    manifest_sha256=args.internal_launch_manifest_sha256,
                    exit_code=args.internal_launch_attempt_exit_code,
                    reason_code=args.internal_launch_attempt_reason_code,
                    detail=args.internal_launch_attempt_detail,
                    external_evidence=external_reference,
                    route=args.internal_launch_attempt_route,
                    bindings=bindings,
                )
            finally:
                fcntl.flock(authority_lock.fileno(), fcntl.LOCK_UN)
                authority_lock.close()
            print(json.dumps(recorded, sort_keys=True))
            return 0
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
            from .governance import resolve_governed_status

            integrity_errors: dict[str, str] = {}
            try:
                integrity_status = campaign_status(paths)
            except Exception as exc:
                integrity_errors["legacy_status"] = f"{type(exc).__name__}:{exc}"
            else:
                for role in ("queue", "service", "physical"):
                    section = integrity_status.get(role)
                    if isinstance(section, Mapping) and section.get("integrity_error"):
                        integrity_errors[role] = str(section["integrity_error"])
            try:
                load_attempt_ledger(ledger_path)
            except Exception as exc:
                integrity_errors["attempt_ledger"] = f"{type(exc).__name__}:{exc}"
            payload = resolve_governed_status(
                experiment_root,
                campaign_root,
                integrity_errors=integrity_errors,
            )
            _emit(
                payload,
                json_output=args.json,
                text=(
                    f"state={payload['state']} "
                    f"release_sha={payload['release']['sha256']} "
                    f"offline_proven={str(payload['predicates']['offline_proven']).lower()} "
                    f"bench_ready={str(payload['predicates']['bench_ready']).lower()} "
                    f"next_action={payload['next_action']}"
                ),
            )
            return 0
        if args.command == "enqueue":
            campaign_id, source, raw_candidates = _load_batch(args.batch)
            candidates, overlays, control_fp, profile_id, launch_fp = _validate_candidates(
                raw_candidates,
                ledger_path=ledger_path,
                launch_profile_path=launch_profile_path,
            )
            plan = _append_batch(
                paths,
                campaign_id=campaign_id,
                source=source,
                candidates=candidates,
            )
            overlay_plan = _append_overlay_batch(
                paths,
                plan=plan,
                source=source,
                candidates=candidates,
                overlays=overlays,
                launch_profile_fingerprint=launch_fp,
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
                "launch_profile_fingerprint": launch_fp,
                "trial_overlay_plan_fingerprint": overlay_plan["fingerprint"],
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
