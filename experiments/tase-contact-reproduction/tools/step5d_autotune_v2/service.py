"""Systemd-owned v2 service entrypoint; Restart is intentionally disabled."""

from __future__ import annotations

import secrets
import sys
import time
from pathlib import Path

from .bridge import BridgeError, BridgeProcess, LiveWriterLock
from .config import load_static_config
from .heartbeat import HeartbeatPublisher
from .mailbox import AtomicMailbox
from .readiness import evaluate_preflight
from .report import reconcile_missing_trial_reports, write_trial_report
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
    live_writer_lock: LiveWriterLock | None = None
    heartbeat: HeartbeatPublisher | None = None
    transfer_worker: TransferWorker | None = None
    bridge_loss: list[str] = []
    ready_details: dict[str, object] = {}
    try:
        repository.claim_writer(token=token)
        report_paths: list[str] = []
        for content, path in reconcile_missing_trial_reports(
            repository,
            deployment_id=config.deployment.deployment_id,
            output_root=config.runtime_root_path / "reports",
        ):
            report_paths.append(str(path))
            print(content, end="", flush=True)
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
        live_writer_lock = LiveWriterLock()
        live_writer_lock.acquire()
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
            "bridge_health_evidence": str(ready.health_evidence_path),
            "sample_rate_hz": ready.sample_rate_hz,
            "startup_stationary_verified": ready.startup_stationary_verified,
            "startup": {
                "phase": "awaiting_tp_play",
                "baseline": ready.startup_baseline,
                "operator_action": "press_tp_play",
                "startup_gate_passed": False,
                "motion_allowed": False,
                "evidence_path": str(ready.startup_evidence_path),
            },
        }

        def health_details() -> dict[str, object]:
            deadline = time.monotonic() + 0.25
            while True:
                try:
                    health = bridge.check_health(
                        max_age_s=2.0, require_progress=True
                    )
                    break
                except BridgeError as exc:
                    if (
                        "counters did not progress" not in str(exc)
                        or time.monotonic() >= deadline
                    ):
                        raise
                    time.sleep(0.002)
            return {
                "schema": "step5d.autotune.bridge-health/v2",
                "pid": health.pid,
                "deployment_id": health.deployment_id,
                "launch_nonce": health.launch_nonce,
                "health_sequence": health.health_sequence,
                "observed_at": health.observed_at,
                "configured_rate_hz": health.configured_rate_hz,
                "sample_counter": health.sample_counter,
                "rtde_healthy": health.rtde_healthy,
                "command_transport_healthy": health.command_transport_healthy,
                "event_transport_healthy": health.event_transport_healthy,
                "evidence_path": str(health.evidence_path),
            }

        def revoke_bridge_health(exc: BaseException) -> None:
            error = f"{type(exc).__name__}:{exc}"
            bridge_loss.append(error)
            repository.revoke_runtime_for_bridge_loss(
                deployment_authorized=config.deployment.deployment_authorized,
                primary_blocker="bridge_liveness_lost",
                details={**ready_details, "bridge_health_error": error},
            )

        heartbeat = HeartbeatPublisher(
            repository=repository,
            mailbox=AtomicMailbox(config.heartbeat_path),
            writer_token=token,
            deployment_id=config.deployment.deployment_id,
            ready_details=ready_details,
            health_probe=health_details,
            health_failure_callback=revoke_bridge_health,
            failure_mailbox=AtomicMailbox(
                (runtime_root / "bridge_revocation.json").resolve()
            ),
            runtime_ready=False,
            primary_blocker=None,
        )
        bridge.start_watcher(heartbeat.fail_from_bridge, interval_s=0.1)
        heartbeat.start()
        startup_stable_s = float(bridge_config.get("startup_stable_s", 0.5))
        startup = bridge.wait_startup_gate(
            timeout_s=float(bridge_config.get("operator_play_timeout_s", 120.0)),
            minimum_stable_s=startup_stable_s,
        )
        ready_details = {
            **ready_details,
            "startup": {
                "phase": startup.phase,
                "baseline": startup.baseline,
                "operator_action": None,
                "startup_gate_passed": startup.startup_gate_passed,
                "motion_allowed": False,
                "stable_duration_s": startup.stable_duration_s,
                "sample_counter": startup.sample_counter,
                "observed_at": startup.observed_at,
                "evidence_path": str(startup.evidence_path),
            },
        }
        heartbeat.update_runtime_status(
            runtime_ready=True,
            primary_blocker=None,
            details=ready_details,
        )
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
        bridge.stop_watcher()
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
        if bridge_loss:
            try:
                repository.revoke_runtime_for_bridge_loss(
                    deployment_authorized=config.deployment.deployment_authorized,
                    primary_blocker="bridge_liveness_lost",
                    details={
                        **ready_details,
                        "bridge_health_errors": bridge_loss,
                        "service_error": f"{type(exc).__name__}:{exc}",
                        "durable_revocation_retry": True,
                    },
                )
            except Exception as revoke_exc:
                print(
                    "step5d-autotune-v2 durable bridge revocation retry failed: "
                    f"{revoke_exc}",
                    file=sys.stderr,
                )
        else:
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
        if live_writer_lock is not None:
            live_writer_lock.release()
        try:
            repository.release_writer(token)
        except Exception:
            pass
