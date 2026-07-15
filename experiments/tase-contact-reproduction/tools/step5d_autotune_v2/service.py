"""Systemd-owned v2 service entrypoint; Restart is intentionally disabled."""

from __future__ import annotations

import secrets
import sys
from pathlib import Path

from .bridge import BridgeError, BridgeProcess
from .config import load_static_config
from .heartbeat import HeartbeatPublisher
from .mailbox import AtomicMailbox
from .readiness import evaluate_preflight
from .report import write_trial_report
from .repository import Repository
from .runtime import JsonlBridgePort
from .supervisor import CampaignSupervisor, TrialOutcome
from .transfer import TransferWorker


def run_service(root: Path, *, database: Path | None = None) -> int:
    config = load_static_config(root)
    repository = Repository(database.resolve() if database else config.database_path)
    repository.initialize()
    repository.register_deployment(config.deployment)
    token = secrets.token_hex(32)
    bridge: BridgeProcess | None = None
    heartbeat: HeartbeatPublisher | None = None
    transfer_worker: TransferWorker | None = None
    try:
        repository.claim_writer(token=token)
        report = evaluate_preflight(config, repository)
        if not report.ready_to_launch:
            repository.set_runtime_status(
                deployment_authorized=report.deployment_authorized,
                runtime_ready=False,
                primary_blocker=report.primary_blocker,
                details=report.details,
            )
            return 78
        bridge_config = config.payload["bridge"]
        runtime_root = config.runtime_root_path
        bridge = BridgeProcess(
            argv=bridge_config["argv"],
            root=root,
            runtime_root=runtime_root,
            deployment_id=config.deployment.deployment_id,
            environment=bridge_config.get("environment") or {},
        )
        ready = bridge.start(timeout_s=float(bridge_config.get("ready_timeout_s", 15)))
        ready_details = {
            **report.details,
            "bridge_pid": ready.pid,
            "bridge_ready_evidence": str(ready.evidence_path),
            "bridge_launch_nonce": ready.launch_nonce,
            "sample_rate_hz": ready.sample_rate_hz,
            "startup_home_verified": ready.startup_home_verified,
        }
        heartbeat = HeartbeatPublisher(
            repository=repository,
            mailbox=AtomicMailbox(config.heartbeat_path),
            writer_token=token,
            deployment_id=config.deployment.deployment_id,
            ready_details=ready_details,
        )
        heartbeat.start()
        transfer_worker = TransferWorker(
            repository,
            timeout_s=float(config.payload["postprocess"]["transfer_timeout_s"]),
        )
        transfer_worker.start()
        port = JsonlBridgePort(
            repository=repository,
            mailbox=AtomicMailbox(config.mailbox_path),
            event_path=runtime_root / "bridge_events.jsonl",
            allowed_artifact_root=runtime_root,
            deployment_id=config.deployment.deployment_id,
            analyzer_argv=bridge_config.get("analyzer_argv") or (),
            command_root=root,
            transfer_destination=config.payload["postprocess"]["mac_destination"],
            timeout_s=float(bridge_config.get("trial_timeout_s", 180)),
            health_check=heartbeat.check,
        )
        report_paths: list[str] = []

        def publish_outcome_report(outcome: TrialOutcome) -> None:
            trial_id = outcome.trial_id
            content, path = write_trial_report(
                repository,
                trial_id=trial_id,
                output_root=runtime_root / "reports",
            )
            report_paths.append(str(path))
            print(content, end="", flush=True)
            if transfer_worker is not None:
                transfer_worker.wake()

        outcomes = CampaignSupervisor(
            repository, port, outcome_callback=publish_outcome_report
        ).run_pending(
            deployment_id=config.deployment.deployment_id
        )
        heartbeat.stop()
        heartbeat = None
        transfer_worker.stop()
        transfer_worker = None
        blocker = outcomes[-1].primary_blocker if outcomes else "no_pending_candidate"
        repository.set_runtime_status(
            deployment_authorized=True,
            runtime_ready=False,
            primary_blocker=blocker or "batch_complete",
            details={
                "completed_trials": [outcome.trial_id for outcome in outcomes],
                "trial_reports": report_paths,
            },
        )
        return 0 if outcomes and all(row.state == "complete" for row in outcomes) else 75
    except Exception as exc:
        if transfer_worker is not None:
            try:
                transfer_worker.stop()
            except Exception:
                pass
            transfer_worker = None
        if heartbeat is not None:
            try:
                heartbeat.stop()
            except Exception:
                pass
            heartbeat = None
        try:
            repository.set_runtime_status(
                deployment_authorized=config.deployment.deployment_authorized,
                runtime_ready=False,
                primary_blocker=f"service_failure:{type(exc).__name__}",
                details={"error": str(exc)},
            )
        except Exception:
            pass
        print(f"step5d-autotune-v2 service failed: {exc}", file=sys.stderr)
        return 70
    finally:
        if transfer_worker is not None:
            try:
                transfer_worker.stop()
            except Exception:
                pass
        if heartbeat is not None:
            try:
                heartbeat.stop()
            except Exception:
                pass
        if bridge is not None:
            try:
                bridge.stop()
            except BridgeError:
                pass
        try:
            repository.release_writer(token)
        except Exception:
            pass
