"""Stable operator CLI for Step5d autotune control plane v2."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Sequence

from .config import ConfigError, StaticConfig, load_static_config
from .legacy import LegacyImporter
from .model import BatchSpec, ModelError
from .readiness import evaluate_preflight
from .report import render_trial_report
from .repository import Repository, RepositoryError
from .service import run_service


UNIT = "step5d-autotune-v2.service"


def _root_default() -> Path:
    return Path(__file__).resolve().parents[2]


def _repo(root: Path, database: Path | None) -> tuple[Repository, StaticConfig]:
    config = load_static_config(root)
    repository = Repository(database.resolve() if database else config.database_path)
    repository.initialize()
    repository.register_deployment(config.deployment)
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
        try:
            age = (datetime.now(timezone.utc) - datetime.fromisoformat(observed)).total_seconds()
            status["fresh"] = age <= freshness_s
            status["age_s"] = age
        except ValueError:
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
    return status


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
        if status["runtime_ready"] or status.get("primary_blocker") not in {
            None,
            "runtime_not_observed",
        }:
            print(json.dumps(status, indent=2, sort_keys=True))
            return 0 if status["runtime_ready"] else 78
        time.sleep(0.2)
    raise RepositoryError("service did not publish fresh readiness within 30 seconds")


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
        repository, config = _repo(root, args.database)
        if args.command == "status":
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
        if args.command == "enqueue":
            payload = json.loads(args.file.read_text(encoding="utf-8"))
            batch = BatchSpec.from_mapping(payload)
            repository.enqueue_batch(batch)
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
    except (ConfigError, ModelError, RepositoryError, OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"refusing: {exc}", file=sys.stderr)
        return 2
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
