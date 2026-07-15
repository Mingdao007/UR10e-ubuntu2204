"""Separate static deployment authorization from fresh runtime readiness."""

from __future__ import annotations

import socket
from dataclasses import dataclass
from typing import Any, Mapping

from .config import StaticConfig
from .dashboard import DashboardClient, DashboardError
from .repository import Repository


@dataclass(frozen=True)
class ReadinessReport:
    deployment_authorized: bool
    ready_to_launch: bool
    primary_blocker: str | None
    details: Mapping[str, Any]


def _probe(host: str, port: int, timeout_s: float = 0.5) -> tuple[bool, str | None]:
    try:
        with socket.create_connection((host, port), timeout=timeout_s):
            return True, None
    except OSError as exc:
        return False, str(exc)


def evaluate_preflight(config: StaticConfig, repository: Repository) -> ReadinessReport:
    deployment = config.deployment
    details: dict[str, Any] = {
        "deployment_id": deployment.deployment_id,
        "code_fingerprint": deployment.code_fingerprint,
        "database_integrity": "ok",
        "checks": {},
    }
    repository.integrity_check()
    if not deployment.controller_readback_verified:
        return ReadinessReport(
            deployment_authorized=False,
            ready_to_launch=False,
            primary_blocker="tp_v2_controller_readback_missing",
            details=details,
        )
    if not deployment.deployment_authorized:
        return ReadinessReport(False, False, "deployment_not_authorized", details)
    cutover = config.payload.get("live_cutover") or {}
    if cutover.get("enabled") is not True:
        return ReadinessReport(True, False, "live_cutover_not_enabled", details)
    blockers = list(cutover.get("blocked_until") or [])
    if blockers:
        details["checks"]["cutover_blockers"] = blockers
        return ReadinessReport(
            True, False, f"cutover_blocked_{blockers[0]}", details
        )
    active_trial_id = repository.active_trial_id()
    pending = repository.next_pending_candidate()
    details["checks"]["candidate_executable"] = {
        "active_trial_id": active_trial_id,
        "next_candidate_id": None if pending is None else pending["candidate_id"],
    }
    if active_trial_id is not None:
        active = repository.trial_detail(active_trial_id)
        if active["deployment_id"] != deployment.deployment_id:
            return ReadinessReport(
                True, False, "active_trial_deployment_mismatch", details
            )
    if pending is None and active_trial_id is None:
        return ReadinessReport(True, False, "no_pending_candidate", details)
    bridge = config.payload.get("bridge") or {}
    if bridge.get("live_enabled") is not True or not bridge.get("argv"):
        return ReadinessReport(True, False, "v2_bridge_command_not_frozen", details)
    controller = config.payload.get("controller") or {}
    host = str(controller.get("host", ""))
    ports = controller.get("required_ports") or []
    for port in ports:
        ok, error = _probe(host, int(port))
        details["checks"][f"tcp_{port}"] = {"ok": ok, "error": error}
        if not ok:
            return ReadinessReport(True, False, f"controller_port_{port}_unreachable", details)
    try:
        dashboard = DashboardClient(host).snapshot()
    except DashboardError as exc:
        details["checks"]["dashboard"] = {"ok": False, "error": str(exc)}
        return ReadinessReport(True, False, "dashboard_snapshot_failed", details)
    details["checks"]["dashboard"] = {"ok": True, "snapshot": dashboard}
    if "NORMAL" not in dashboard["safetymode"]:
        return ReadinessReport(True, False, "dashboard_safety_not_normal", details)
    expected_program = str(controller.get("program", ""))
    if expected_program and expected_program not in dashboard["get loaded program"]:
        return ReadinessReport(True, False, "dashboard_program_identity_mismatch", details)
    return ReadinessReport(True, True, None, details)
