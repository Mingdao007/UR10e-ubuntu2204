"""Stable operator CLI for Step5d autotune control plane v2."""

from __future__ import annotations

import argparse
import json
import sqlite3
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Sequence

from .config import ConfigError, StaticConfig, load_static_config
from .heartbeat import REVOCATION_SCHEMA
from .legacy import LegacyImporter
from .mailbox import AtomicMailbox, MailboxError
from .model import BatchSpec, ModelError
from .readiness import evaluate_preflight
from .report import render_trial_report
from .repository import Repository, RepositoryError
from .service import run_service


UNIT = "step5d-autotune-v2.service"
ALLOWED_CLOCK_SKEW_S = 1.0


def _root_default() -> Path:
    return Path(__file__).resolve().parents[2]


def _repo(root: Path, database: Path | None) -> tuple[Repository, StaticConfig]:
    config = load_static_config(root)
    repository = Repository(database.resolve() if database else config.database_path)
    repository.initialize()
    repository.register_deployment(config.deployment)
    return repository, config


def _status_repo(
    root: Path, database: Path | None
) -> tuple[Repository, StaticConfig]:
    """Open existing current truth without creating or registering anything."""

    config = load_static_config(root)
    if database is None:
        database_path = (
            root / str(config.payload["paths"]["database"])
        ).absolute()
        if database_path.resolve() != config.database_path:
            raise RepositoryError("configured campaign database path is unsafe")
    else:
        database_path = database.expanduser().absolute()
    repository = Repository(database_path, read_only=True)
    repository.integrity_check()
    return repository, config


def _fresh_status(
    repository: Repository, freshness_s: float, config: StaticConfig
) -> dict:
    status = repository.status()
    status["deployment_id"] = config.deployment.deployment_id
    status["deployment_authorized"] = config.deployment.deployment_authorized
    status["controller_readback_verified"] = (
        config.deployment.controller_readback_verified
    )
    static_blocker = None
    if not config.deployment.controller_readback_verified:
        static_blocker = "tp_v2_controller_readback_missing"
    elif not config.deployment.deployment_authorized:
        static_blocker = "deployment_not_authorized"
    elif config.payload["live_cutover"]["enabled"] is not True:
        static_blocker = "live_cutover_not_enabled"
    elif config.payload["live_cutover"]["blocked_until"]:
        static_blocker = (
            "cutover_blocked_"
            + config.payload["live_cutover"]["blocked_until"][0]
        )
    if static_blocker is not None:
        status["runtime_ready"] = False
        status["primary_blocker"] = static_blocker
    observed = status.get("observed_at")
    if observed:
        age = _aware_age_s(observed)
        if age is not None:
            status["fresh"] = -ALLOWED_CLOCK_SKEW_S <= age <= freshness_s
            status["age_s"] = age
        else:
            status["fresh"] = False
    if static_blocker is None and status["runtime_ready"] and not status["fresh"]:
        status["runtime_ready"] = False
        status["primary_blocker"] = "runtime_status_stale"
    runtime_deployment = (status.get("details") or {}).get("deployment_id")
    if (
        static_blocker is None
        and status["runtime_ready"]
        and runtime_deployment != config.deployment.deployment_id
    ):
        status["runtime_ready"] = False
        status["primary_blocker"] = "runtime_deployment_mismatch"
    if static_blocker is None:
        _apply_bridge_revocation(status, freshness_s=freshness_s, config=config)
    return status


def _aware_age_s(value: object) -> float | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        observed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if observed.tzinfo is None or observed.utcoffset() is None:
        return None
    return (datetime.now(timezone.utc) - observed).total_seconds()


def _apply_bridge_revocation(
    status: dict, *, freshness_s: float, config: StaticConfig
) -> None:
    path = (config.runtime_root_path / "bridge_revocation.json").absolute()
    try:
        snapshot = AtomicMailbox(path).read_latest()
    except (MailboxError, OSError) as exc:
        status["runtime_ready"] = False
        status["primary_blocker"] = "bridge_revocation_evidence_invalid"
        status["details"] = {
            **dict(status.get("details") or {}),
            "bridge_revocation_evidence_error": f"{type(exc).__name__}:{exc}",
        }
        return
    if snapshot is None:
        return
    payload = snapshot.payload
    expected_fields = {
        "schema",
        "deployment_id",
        "bridge_pid",
        "bridge_launch_nonce",
        "observed_at",
        "reason",
        "durable_revocation",
        "durable_revocation_error",
    }
    age_s = _aware_age_s(payload.get("observed_at"))
    if age_s is not None and age_s > freshness_s + ALLOWED_CLOCK_SKEW_S:
        return
    pid = payload.get("bridge_pid")
    nonce = payload.get("bridge_launch_nonce")
    durable = payload.get("durable_revocation")
    error = payload.get("durable_revocation_error")
    valid = (
        set(payload) == expected_fields
        and payload.get("schema") == REVOCATION_SCHEMA
        and payload.get("deployment_id") == config.deployment.deployment_id
        and isinstance(pid, int)
        and not isinstance(pid, bool)
        and pid > 0
        and isinstance(nonce, str)
        and len(nonce) == 64
        and all(character in "0123456789abcdef" for character in nonce)
        and age_s is not None
        and age_s >= -ALLOWED_CLOCK_SKEW_S
        and isinstance(payload.get("reason"), str)
        and bool(payload.get("reason"))
        and durable in {"pending", "complete", "failed", "not_configured"}
        and (error is None or isinstance(error, str))
    )
    runtime_details = dict(status.get("details") or {})
    identity_matches = (
        runtime_details.get("bridge_pid") == pid
        and runtime_details.get("bridge_launch_nonce") == nonce
    )
    if not valid or not identity_matches:
        status["runtime_ready"] = False
        status["primary_blocker"] = "bridge_revocation_evidence_invalid"
        runtime_details["bridge_revocation_evidence_error"] = (
            "invalid_payload" if not valid else "runtime_identity_mismatch"
        )
        status["details"] = runtime_details
        return
    status["runtime_ready"] = False
    status["primary_blocker"] = "bridge_liveness_lost"
    status["details"] = {
        **runtime_details,
        "bridge_revocation_evidence": dict(payload),
        "bridge_revocation_sequence": snapshot.sequence,
    }


def _start(root: Path, database: Path | None) -> int:
    repository, config = _repo(root, database)
    report = evaluate_preflight(config, repository)
    if not report.ready_to_launch:
        repository.set_runtime_status(
            deployment_authorized=report.deployment_authorized,
            runtime_ready=False,
            primary_blocker=report.primary_blocker,
            details=report.details,
        )
        print(json.dumps(_fresh_status(repository, 5.0, config), indent=2, sort_keys=True))
        return 78
    active = subprocess.run(
        ["systemctl", "--user", "is-active", "--quiet", UNIT], check=False
    )
    if active.returncode != 0:
        repository.set_metadata("stop_after_current", "0")
        subprocess.run(
            ["systemctl", "--user", "reset-failed", UNIT],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        tool = root / "tools" / "step5d-autotunectl.py"
        argv = [
            "systemd-run",
            "--user",
            f"--unit={UNIT.removesuffix('.service')}",
            "--property=Restart=no",
            "--property=KillMode=control-group",
            "--collect",
            "--no-block",
            sys.executable,
            str(tool),
            "--root",
            str(root),
        ]
        if database:
            argv.extend(("--database", str(database.resolve())))
        argv.append("_service")
        completed = subprocess.run(argv, check=False)
        if completed.returncode != 0:
            raise RepositoryError(f"systemd-run failed with rc={completed.returncode}")
    deadline = time.monotonic() + 30.0
    while time.monotonic() < deadline:
        status = _fresh_status(repository, 5.0, config)
        if _start_status_is_terminal(status):
            print(json.dumps(status, indent=2, sort_keys=True))
            return 0 if status["runtime_ready"] else 78
        time.sleep(0.2)
    raise RepositoryError("service did not publish fresh readiness within 30 seconds")


def _start_status_is_terminal(status: Mapping[str, Any]) -> bool:
    if status.get("runtime_ready") is True:
        return True
    return status.get("fresh") is True and status.get("primary_blocker") not in {
        None,
        "runtime_not_observed",
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="step5d-autotunectl")
    parser.add_argument("--root", type=Path, default=_root_default())
    parser.add_argument("--database", type=Path)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("start")
    status = commands.add_parser("status")
    status.add_argument("--json", action="store_true")
    enqueue = commands.add_parser("enqueue")
    enqueue.add_argument("--file", type=Path, required=True)
    report = commands.add_parser("report")
    report.add_argument("--batch", default="latest")
    report.add_argument("--trial")
    report.add_argument("--format", choices=("markdown",), default="markdown")
    replay = commands.add_parser("replay")
    replay.add_argument("--group", required=True)
    replay.add_argument("--reason", required=True)
    commands.add_parser("stop-after-current")
    legacy = commands.add_parser("legacy-import")
    legacy.add_argument("--source-root", type=Path, required=True)
    legacy.add_argument("--mapping", type=Path, required=True)
    legacy.add_argument("--output", type=Path, required=True)
    commands.add_parser("_service")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    root = args.root.resolve()
    try:
        if args.command == "_service":
            return run_service(root, database=args.database)
        if args.command == "start":
            return _start(root, args.database)
        if args.command == "status":
            repository, config = _status_repo(root, args.database)
            status = _fresh_status(
                repository,
                float(config.payload["live_cutover"]["status_freshness_s"]),
                config,
            )
            if args.json:
                print(json.dumps(status, indent=2, sort_keys=True))
            else:
                print(
                    f"runtime_ready={str(status['runtime_ready']).lower()} "
                    f"deployment_authorized={str(status['deployment_authorized']).lower()} "
                    f"blocker={status['primary_blocker']}"
                )
            return 0
        repository, config = _repo(root, args.database)
        if args.command == "enqueue":
            payload = json.loads(args.file.read_text(encoding="utf-8"))
            batch = BatchSpec.from_mapping(payload)
            repository.enqueue_batch(
                batch, deployment_id=config.deployment.deployment_id
            )
            print(json.dumps({"batch_id": batch.batch_id, "groups": [c.group_id for c in batch.candidates]}))
            return 0
        if args.command == "report":
            batch_id = (
                None
                if args.trial
                else (repository.latest_batch_id() if args.batch == "latest" else args.batch)
            )
            print(
                render_trial_report(repository, trial_id=args.trial, batch_id=batch_id),
                end="",
            )
            return 0
        if args.command == "replay":
            nonce = uuid.uuid4().hex
            replay = repository.create_replay(
                group_id=args.group, reason=args.reason, nonce=nonce
            )
            print(
                json.dumps(
                    {
                        "candidate_id": replay.candidate_id,
                        "source_group": replay.group_id,
                        "purpose": "replay",
                        "nonce": nonce,
                        "optimizer_history": False,
                    },
                    sort_keys=True,
                )
            )
            return 0
        if args.command == "stop-after-current":
            repository.set_metadata("stop_after_current", "1")
            print("stop-after-current armed")
            return 0
        if args.command == "legacy-import":
            result = LegacyImporter(
                repository,
                source_root=args.source_root,
                mapping_path=args.mapping,
            ).run(output_path=args.output)
            print(json.dumps(result, indent=2, sort_keys=True))
            return 0
    except (
        ConfigError,
        ModelError,
        RepositoryError,
        OSError,
        ValueError,
        json.JSONDecodeError,
        sqlite3.Error,
    ) as exc:
        if args.command == "status" and args.json:
            print(
                json.dumps(
                    {
                        "schema": "step5d.autotune.status-error/v2",
                        "runtime_ready": False,
                        "deployment_authorized": False,
                        "controller_readback_verified": False,
                        "fresh": False,
                        "primary_blocker": "repository_state_unavailable",
                        "error": f"{type(exc).__name__}:{exc}",
                    },
                    indent=2,
                    sort_keys=True,
                )
            )
        print(f"refusing: {exc}", file=sys.stderr)
        return 2
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
