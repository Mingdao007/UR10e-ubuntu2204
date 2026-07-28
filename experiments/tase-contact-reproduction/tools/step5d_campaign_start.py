#!/usr/bin/env python3
"""Executable, injectable Step5d campaign-start composition.

The coordinator owns ordering and receipt production only.  Controller,
Dashboard, RTDE, bridge, lease, and campaign workers are injected through
small protocols, which keeps the offline state-machine proof independent from
live transport code.  The concrete public entrypoint binds the Remote
Control adapters; ``--offline`` remains a no-network preflight.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import time
from typing import Any, Callable, Mapping, Protocol, Sequence

from step5d_no_tube_handoff import (
    HandoffError,
    HandoffManifest,
    HandoffState,
    HandoffStateStore,
    build_home_verified_receipt,
    build_queue_ready_receipt,
    load_manifest,
    validate_release_binding,
)


SCRIPT1_LOADED_SCHEMA = "step5d.no-tube-handoff/script1-loaded-v1"
SCRIPT1_PLAYED_SCHEMA = "step5d.no-tube-handoff/script1-played-v1"
R026_LOADED_SCHEMA = "step5d.no-tube-handoff/r026-loaded-v1"
R026_IDENTITY_SCHEMA = "step5d.no-tube-handoff/r026-identity-verified-v1"
BRIDGE_READY_SCHEMA = "step5d.no-tube-handoff/bridge-ready-v1"
CAMPAIGN_RUNNING_SCHEMA = "step5d.no-tube-handoff/campaign-running-v1"


class CampaignStartError(HandoffError):
    """A startup response cannot safely satisfy its explicit seam."""


class Script1Trigger(Protocol):
    def load(self, manifest: HandoffManifest) -> Mapping[str, Any]:
        ...

    def play(self, manifest: HandoffManifest) -> Mapping[str, Any]:
        ...


class HomeObserver(Protocol):
    def observe(self, manifest: HandoffManifest) -> Sequence[Mapping[str, Any]]:
        ...


class R026Loader(Protocol):
    def load(self, manifest: HandoffManifest) -> Mapping[str, Any]:
        ...


class R026IdentityObserver(Protocol):
    def observe(self, manifest: HandoffManifest) -> Mapping[str, Any]:
        ...


class BridgeStarter(Protocol):
    def start(self, manifest: HandoffManifest) -> Mapping[str, Any]:
        ...


class CampaignPlayer(Protocol):
    def play(self, manifest: HandoffManifest) -> Mapping[str, Any]:
        ...


def _object(response: Mapping[str, Any], role: str) -> Mapping[str, Any]:
    if not isinstance(response, Mapping):
        raise CampaignStartError(f"{role} response must be an object")
    return response


def _require(response: Mapping[str, Any], key: str, expected: Any, role: str) -> None:
    if response.get(key) != expected:
        raise CampaignStartError(f"{role} {key} differs from governed contract")


def _reject_lifecycle_side_effects(response: Mapping[str, Any], role: str) -> None:
    forbidden = (
        "bridge_started",
        "lease_valid",
        "arm_requested",
        "contact_enabled",
        "zero_requested",
        "tare_requested",
    )
    for key in forbidden:
        if response.get(key) is True:
            raise CampaignStartError(f"{role} must not perform {key}")


def build_script1_loaded_receipt(
    manifest: HandoffManifest, response: Mapping[str, Any]
) -> dict[str, Any]:
    response = _object(response, "Script 1 Load")
    _require(response, "action", "LOAD", "Script 1 Load")
    _require(response, "accepted", True, "Script 1 Load")
    _require(response, "program_id", "step5d_autotune_start_hover_r001", "Script 1 Load")
    _require(response, "controller_target", manifest.payload["script1"]["controller_target"], "Script 1 Load")
    _require(response, "safety_mode", "NORMAL", "Script 1 Load")
    _require(response, "program_running", False, "Script 1 Load")
    _reject_lifecycle_side_effects(response, "Script 1 Load")
    return {
        "schema": SCRIPT1_LOADED_SCHEMA,
        "status": "SCRIPT1_LOADED",
        "program_id": response["program_id"],
        "controller_target": response["controller_target"],
        "safety_mode": response["safety_mode"],
        "dashboard_response": dict(response),
    }


def build_script1_played_receipt(
    manifest: HandoffManifest, response: Mapping[str, Any]
) -> dict[str, Any]:
    response = _object(response, "Script 1 Play")
    _require(response, "action", "PLAY", "Script 1 Play")
    _require(response, "accepted", True, "Script 1 Play")
    _require(response, "program_id", "step5d_autotune_start_hover_r001", "Script 1 Play")
    _require(response, "controller_target", manifest.payload["script1"]["controller_target"], "Script 1 Play")
    _require(response, "safety_mode", "NORMAL", "Script 1 Play")
    _require(response, "program_running", True, "Script 1 Play")
    _reject_lifecycle_side_effects(response, "Script 1 Play")
    return {
        "schema": SCRIPT1_PLAYED_SCHEMA,
        "status": "SCRIPT1_PLAYED",
        "program_id": response["program_id"],
        "controller_target": response["controller_target"],
        "safety_mode": response["safety_mode"],
        "dashboard_response": dict(response),
    }


def build_r026_loaded_receipt(
    manifest: HandoffManifest, response: Mapping[str, Any]
) -> dict[str, Any]:
    response = _object(response, "r026 Load")
    script2 = manifest.payload["script2"]
    _require(response, "action", "LOAD", "r026 Load")
    _require(response, "accepted", True, "r026 Load")
    _require(response, "program_id", script2["program_id"], "r026 Load")
    _require(response, "controller_target", script2["controller_target"], "r026 Load")
    _require(response, "safety_mode", "NORMAL", "r026 Load")
    _require(response, "program_running", False, "r026 Load")
    _reject_lifecycle_side_effects(response, "r026 Load")
    return {
        "schema": R026_LOADED_SCHEMA,
        "status": "R026_LOADED",
        "program_id": response["program_id"],
        "controller_target": response["controller_target"],
        "release_manifest_sha256": script2["release_manifest_sha256"],
        "dashboard_response": dict(response),
    }


def build_r026_identity_receipt(
    manifest: HandoffManifest, response: Mapping[str, Any]
) -> dict[str, Any]:
    response = _object(response, "r026 identity observer")
    script2 = manifest.payload["script2"]
    _require(response, "program_id", script2["program_id"], "r026 identity observer")
    _require(response, "controller_target", script2["controller_target"], "r026 identity observer")
    _require(response, "execution_profile_id", script2["execution_profile_id"], "r026 identity observer")
    _require(response, "release_manifest_sha256", script2["release_manifest_sha256"], "r026 identity observer")
    triplet = response.get("triplet_sha256")
    if triplet != script2["sha256"]:
        raise CampaignStartError("r026 identity observer triplet differs")
    return {
        "schema": R026_IDENTITY_SCHEMA,
        "status": "R026_IDENTITY_VERIFIED",
        "program_id": response["program_id"],
        "execution_profile_id": response["execution_profile_id"],
        "release_manifest_sha256": response["release_manifest_sha256"],
        "triplet_sha256": dict(triplet),
        "observer_response": dict(response),
    }


def build_bridge_ready_receipt(
    manifest: HandoffManifest, response: Mapping[str, Any]
) -> dict[str, Any]:
    response = _object(response, "bridge startup")
    script2 = manifest.payload["script2"]
    _require(response, "controller_program_id", script2["program_id"], "bridge startup")
    _require(response, "release_manifest_sha256", script2["release_manifest_sha256"], "bridge startup")
    for key in ("heartbeat_fresh", "lease_valid", "single_writer", "safety_normal"):
        _require(response, key, True, "bridge startup")
    bridge_id = response.get("bridge_id")
    if not isinstance(bridge_id, str) or not bridge_id:
        raise CampaignStartError("bridge startup lacks bridge_id")
    return {
        "schema": BRIDGE_READY_SCHEMA,
        "status": "BRIDGE_READY",
        "bridge_id": bridge_id,
        "controller_program_id": response["controller_program_id"],
        "release_manifest_sha256": response["release_manifest_sha256"],
        "heartbeat_fresh": True,
        "lease_valid": True,
        "single_writer": True,
        "safety_normal": True,
        "bridge_response": dict(response),
    }


def build_campaign_running_receipt(
    manifest: HandoffManifest,
    response: Mapping[str, Any],
    bridge_receipt: Mapping[str, Any],
) -> dict[str, Any]:
    response = _object(response, "campaign Play")
    script2 = manifest.payload["script2"]
    _require(response, "action", "PLAY", "campaign Play")
    _require(response, "accepted", True, "campaign Play")
    _require(response, "program_id", script2["program_id"], "campaign Play")
    _require(response, "program_running", True, "campaign Play")
    _require(response, "safety_mode", "NORMAL", "campaign Play")
    _require(response, "bridge_id", bridge_receipt["bridge_id"], "campaign Play")
    _reject_lifecycle_side_effects(response, "campaign Play")
    return {
        "schema": CAMPAIGN_RUNNING_SCHEMA,
        "status": "CAMPAIGN_RUNNING",
        "program_id": response["program_id"],
        "bridge_id": response["bridge_id"],
        "execution_profile_id": script2["execution_profile_id"],
        "campaign_id": manifest.campaign_id,
        "play_response": dict(response),
    }


def _transition(store: HandoffStateStore, state: HandoffState, receipt: Mapping[str, Any]) -> dict[str, Any]:
    return store.transition(state, {"receipt": dict(receipt)})


class CampaignStartCoordinator:
    """Compose independent primitives into the governed startup order."""

    def __init__(
        self,
        manifest: HandoffManifest,
        store: HandoffStateStore,
        *,
        queue_root: Path,
        queue_viewer: Callable[[Path], Mapping[str, Any]],
        script1: Script1Trigger,
        home: HomeObserver,
        r026_loader: R026Loader,
        r026_identity: R026IdentityObserver,
        bridge: BridgeStarter,
        campaign: CampaignPlayer,
    ) -> None:
        self.manifest = manifest
        self.store = store
        self.queue_root = queue_root
        self.queue_viewer = queue_viewer
        self.script1 = script1
        self.home = home
        self.r026_loader = r026_loader
        self.r026_identity = r026_identity
        self.bridge = bridge
        self.campaign = campaign
        self._play_issued_this_attempt = False
        self._bridge_started_this_attempt = False

    def run(self) -> dict[str, Any]:
        try:
            binding = validate_release_binding(self.manifest)
            _transition(self.store, HandoffState.RELEASE_READY, {"binding": binding})

            queue_receipt = build_queue_ready_receipt(
                self.manifest, self.queue_viewer(self.queue_root)
            )
            _transition(self.store, HandoffState.QUEUE_READY, queue_receipt)

            loaded_receipt = build_script1_loaded_receipt(
                self.manifest, self.script1.load(self.manifest)
            )
            _transition(self.store, HandoffState.SCRIPT1_LOADED, loaded_receipt)
            try:
                played_response = self.script1.play(self.manifest)
            except Exception:
                self._play_issued_this_attempt = bool(
                    getattr(self.script1, "play_issued", False)
                )
                raise
            self._play_issued_this_attempt = bool(
                getattr(self.script1, "play_issued", True)
            )
            played_receipt = build_script1_played_receipt(
                self.manifest, played_response
            )
            _transition(self.store, HandoffState.SCRIPT1_PLAYED, played_receipt)
            home_receipt = build_home_verified_receipt(
                self.manifest, self.home.observe(self.manifest)
            )
            _transition(self.store, HandoffState.HOME_VERIFIED, home_receipt)

            r026_loaded = build_r026_loaded_receipt(
                self.manifest, self.r026_loader.load(self.manifest)
            )
            _transition(self.store, HandoffState.R026_LOADED, r026_loaded)
            r026_identity = build_r026_identity_receipt(
                self.manifest, self.r026_identity.observe(self.manifest)
            )
            _transition(self.store, HandoffState.R026_IDENTITY_VERIFIED, r026_identity)

            try:
                bridge_response = self.bridge.start(self.manifest)
            except Exception:
                self._bridge_started_this_attempt = bool(
                    getattr(self.bridge, "started_this_attempt", False)
                )
                raise
            self._bridge_started_this_attempt = bool(
                getattr(self.bridge, "started_this_attempt", True)
            )
            bridge_receipt = build_bridge_ready_receipt(
                self.manifest, bridge_response
            )
            _transition(self.store, HandoffState.BRIDGE_READY, bridge_receipt)
            campaign_receipt = build_campaign_running_receipt(
                self.manifest, self.campaign.play(self.manifest), bridge_receipt
            )
            _transition(self.store, HandoffState.CAMPAIGN_RUNNING, campaign_receipt)
            return campaign_receipt
        except Exception as exc:
            self._cleanup_after_failure()
            self._record_failure(exc)
            raise

    def _cleanup_after_failure(self) -> None:
        state = self.store.status().get("state")
        play_issued = self._play_issued_this_attempt or state in {
            HandoffState.SCRIPT1_PLAYED.value,
            HandoffState.HOME_VERIFIED.value,
            HandoffState.R026_LOADED.value,
            HandoffState.R026_IDENTITY_VERIFIED.value,
            HandoffState.BRIDGE_READY.value,
            HandoffState.CAMPAIGN_RUNNING.value,
        }
        if play_issued:
            stop = getattr(self.script1, "stop", None)
            if callable(stop):
                try:
                    stop()
                except Exception:
                    pass
        bridge_started = self._bridge_started_this_attempt or state in {
            HandoffState.BRIDGE_READY.value,
            HandoffState.CAMPAIGN_RUNNING.value,
        }
        if bridge_started:
            abort = getattr(self.bridge, "abort", None)
            if callable(abort):
                try:
                    abort()
                except Exception:
                    pass

    def _record_failure(self, exc: Exception) -> None:
        status = self.store.status()
        if status.get("state") == HandoffState.FAILED_CLOSED.value:
            return
        try:
            self.store.transition(
                HandoffState.FAILED_CLOSED,
                {
                    "failed_after": status.get("state"),
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                },
            )
        except Exception:
            # Preserve the primitive's original failure.  A failed receipt
            # write remains visible through the unchanged canonical state.
            pass


def build_remote_campaign_start_coordinator(
    manifest: HandoffManifest,
    store: HandoffStateStore,
    *,
    campaign_root: Path,
    output_root: Path,
    robot_host: str | None = None,
) -> CampaignStartCoordinator:
    """Bind the concrete Remote Control primitives to the coordinator."""

    from step5d_parameter_queue import authoritative_view
    from step5d_remote_startup import (
        RemoteBridgeStarter,
        RemoteCampaignPlayer,
        RemoteDashboardWriter,
        RemoteHomeObserver,
        RemoteR026IdentityObserver,
        RemoteR026Loader,
        RemoteScript1Trigger,
    )

    startup = manifest.remote_startup
    bound_host = robot_host or os.environ.get(
        startup["robot_host_env"], startup["robot_host_default"]
    )
    if not isinstance(bound_host, str) or not bound_host:
        raise CampaignStartError("Remote Control robot host is missing")
    script1_writer = RemoteDashboardWriter(
        bound_host,
        load_target=manifest.payload["script1"]["controller_target"],
        port=startup["dashboard_port"],
        timeout_s=startup["dashboard_timeout_s"],
    )
    r026_writer = RemoteDashboardWriter(
        bound_host,
        load_target=manifest.payload["script2"]["controller_target"],
        port=startup["dashboard_port"],
        timeout_s=startup["dashboard_timeout_s"],
    )
    script1 = RemoteScript1Trigger(
        robot_host=bound_host,
        writer=script1_writer,
        dashboard_port=startup["dashboard_port"],
        dashboard_timeout_s=startup["dashboard_timeout_s"],
        observe_timeout_s=startup["load_timeout_s"],
        poll_interval_s=startup["poll_interval_s"],
    )
    home = RemoteHomeObserver(
        robot_host=bound_host,
        rtde_port=startup["rtde_port"],
        dashboard_port=startup["dashboard_port"],
        dashboard_timeout_s=startup["dashboard_timeout_s"],
        frequency_hz=startup["home_frequency_hz"],
        timeout_s=startup["home_timeout_s"],
        poll_interval_s=startup["poll_interval_s"],
    )
    r026_loader = RemoteR026Loader(
        robot_host=bound_host,
        writer=r026_writer,
        dashboard_port=startup["dashboard_port"],
        dashboard_timeout_s=startup["dashboard_timeout_s"],
        observe_timeout_s=startup["load_timeout_s"],
        poll_interval_s=startup["poll_interval_s"],
    )
    r026_identity = RemoteR026IdentityObserver(
        robot_host=bound_host,
        dashboard_port=startup["dashboard_port"],
        dashboard_timeout_s=startup["dashboard_timeout_s"],
    )
    bridge = RemoteBridgeStarter(
        experiment_root=manifest.experiment_root,
        robot_host=bound_host,
        output_root=output_root,
        campaign_root=campaign_root,
    )
    campaign = RemoteCampaignPlayer(
        experiment_root=manifest.experiment_root,
        robot_host=bound_host,
        output_root=output_root,
        bridge=bridge,
    )
    return CampaignStartCoordinator(
        manifest,
        store,
        queue_root=campaign_root / "control" / "parameter_receiver",
        queue_viewer=lambda root: authoritative_view(root),
        script1=script1,
        home=home,
        r026_loader=r026_loader,
        r026_identity=r026_identity,
        bridge=bridge,
        campaign=campaign,
    )


def _offline_preflight(manifest_path: Path) -> dict[str, Any]:
    manifest = load_manifest(manifest_path)
    binding = validate_release_binding(manifest)
    return {
        "status": "OFFLINE_PREFLIGHT_PASS",
        "campaign_id": manifest.campaign_id,
        "state_sequence": [state.value for state in HandoffState if state not in {HandoffState.SAFE_HOLD, HandoffState.FAILED_CLOSED}],
        "binding": binding,
        "queue_watermarks": {
            "high": manifest.high_watermark,
            "low": manifest.low_watermark,
        },
        "tube": {"enabled": False, "policies": []},
        "live_actions": "remote_control_adapter_bound_but_not_invoked",
    }


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    campaign = subparsers.add_parser(
        "campaign-start",
        help="Remote Control governed campaign start; --offline is no-network preflight",
    )
    campaign.add_argument("--manifest", type=Path, required=True)
    campaign.add_argument("--offline", action="store_true", help="validate only; never contact live systems")
    campaign.add_argument("--campaign-root", type=Path)
    campaign.add_argument("--output-root", type=Path)
    campaign.add_argument("--robot-host")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.command != "campaign-start":
        raise CampaignStartError("unsupported campaign-start command")
    if args.offline:
        print(json.dumps(_offline_preflight(args.manifest), indent=2, sort_keys=True))
        return 0
    try:
        manifest = load_manifest(args.manifest)
        campaign_root = (
            args.campaign_root
            or manifest.experiment_root / "runs/step5d_autotune_v3/parameter-campaign"
        ).expanduser().absolute()
        output_root = (
            args.output_root
            or manifest.experiment_root
            / "runs/step5d_autotune_v3"
            / f"campaign-start-{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}-{os.getpid()}"
        ).expanduser().absolute()
        store = HandoffStateStore(
            campaign_root / "control" / "campaign-start",
            handoff_id=manifest.campaign_id,
        )
        coordinator = build_remote_campaign_start_coordinator(
            manifest,
            store,
            campaign_root=campaign_root,
            output_root=output_root,
            robot_host=args.robot_host,
        )
        result = coordinator.run()
    except (OSError, HandoffError, ValueError) as exc:
        print(
            f"Remote Control campaign-start blocked: {type(exc).__name__}:{exc}",
            file=__import__("sys").stderr,
        )
        return 3
    print(json.dumps(result, indent=2, sort_keys=True, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "BRIDGE_READY_SCHEMA",
    "CAMPAIGN_RUNNING_SCHEMA",
    "CampaignStartCoordinator",
    "CampaignStartError",
    "build_remote_campaign_start_coordinator",
    "R026_IDENTITY_SCHEMA",
    "R026_LOADED_SCHEMA",
    "SCRIPT1_LOADED_SCHEMA",
    "SCRIPT1_PLAYED_SCHEMA",
    "build_bridge_ready_receipt",
    "build_campaign_running_receipt",
    "build_r026_identity_receipt",
    "build_r026_loaded_receipt",
    "build_script1_loaded_receipt",
    "build_script1_played_receipt",
]
