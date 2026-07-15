#!/usr/bin/env python3
"""Run one durable Step5d-native campaign against an existing bridge.

The TP and bridge are long-lived.  This process is the single campaign writer:
it persists ARM/ACK intent before publishing the mailbox command, follows the
bridge CSV for TP state and safe-home evidence, seals the immutable trial
bundle, and advances automatically until success or a typed stop phase.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import os
import subprocess
import sys
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any, Iterator, Mapping

from step5d_autotune_backend import (
    CampaignAuthorization,
    Step5dV35Backend,
)
from step5d_autotune_contract import CampaignSpec, ExecutionProfile
from step5d_autotune_coordinator import CampaignCoordinator
from step5d_autotune_journal import SupervisorJournal, TpSnapshot
from step5d_autotune_live_driver import (
    AtomicCommandMailbox,
    CampaignHomeReference,
    HostClosureCollector,
    TrialArtifactProducer,
    finalize_produced_bundle_and_dispatch_ack,
)
from step5d_autotune_state_machine import TpLoopState
from step5d_autotune_store import CampaignStore
from step5d_autotune_supervisor import CampaignPhase, CampaignSupervisor


STATE_NAMES = {int(state): state.name for state in TpLoopState}


def _finite(row: Mapping[str, str], name: str) -> float:
    try:
        value = float(row[name])
    except (KeyError, TypeError, ValueError) as exc:
        raise RuntimeError(f"bridge row lacks finite {name}") from exc
    if not math.isfinite(value):
        raise RuntimeError(f"bridge row has non-finite {name}")
    return value


def _integer(row: Mapping[str, str], name: str) -> int:
    value = _finite(row, name)
    if not value.is_integer():
        raise RuntimeError(f"bridge row {name} is not an integer")
    return int(value)


def tp_snapshot_from_bridge_row(row: Mapping[str, str]) -> TpSnapshot:
    state_value = _integer(row, "ur_output_int_register_26")
    try:
        state = STATE_NAMES[state_value]
    except KeyError as exc:
        raise RuntimeError(f"bridge row has unknown TP state {state_value}") from exc
    return TpSnapshot(
        campaign_epoch_echo=_integer(row, "ur_output_int_register_24"),
        trial_id_echo=_integer(row, "ur_output_int_register_25"),
        state=state,
        candidate_token_echo=_integer(row, "ur_output_int_register_27"),
        terminal_reason=_integer(row, "ur_output_int_register_28"),
        execution_profile_integer_id_echo=_integer(
            row, "ur_output_int_register_29"
        ),
        consumed_command_seq=_integer(row, "ur_output_int_register_30"),
    )


def closure_sample_from_bridge_row(row: Mapping[str, str]) -> dict[str, Any]:
    sample: dict[str, Any] = {
        "timestamp": _finite(row, "ur_timestamp"),
        "safety_mode": _integer(row, "ur_safety_mode"),
    }
    for name in ("actual_TCP_pose", "actual_TCP_speed", "actual_q", "actual_qd"):
        sample[name] = [
            _finite(row, f"ur_{name}_{index}") for index in range(6)
        ]
    for index in range(24, 31):
        sample[f"output_int_register_{index}"] = _integer(
            row, f"ur_output_int_register_{index}"
        )
    for index in range(36, 39):
        sample[f"output_double_register_{index}"] = _finite(
            row, f"ur_output_double_register_{index}"
        )
    return sample


class BridgeCsvFollower:
    def __init__(self, path: Path) -> None:
        self.path = path.resolve()
        if not self.path.is_file() or self.path.is_symlink():
            raise RuntimeError("bridge CSV must be an existing regular file")
        self.handle = self.path.open("r", newline="", encoding="utf-8")
        header = self.handle.readline()
        self.fieldnames = next(csv.reader([header]))
        if not self.fieldnames or len(self.fieldnames) != len(set(self.fieldnames)):
            raise RuntimeError("bridge CSV header is missing or duplicated")
        self.handle.seek(0, os.SEEK_END)

    def rows(self, *, timeout_s: float) -> Iterator[dict[str, str]]:
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            position = self.handle.tell()
            line = self.handle.readline()
            if not line or not line.endswith("\n"):
                self.handle.seek(position)
                time.sleep(0.01)
                continue
            values = next(csv.reader([line]))
            if len(values) != len(self.fieldnames):
                raise RuntimeError("bridge CSV row width changed")
            yield dict(zip(self.fieldnames, values))
        raise TimeoutError("timed out waiting for a fresh bridge row")

    def close(self) -> None:
        self.handle.close()


def _latest_complete_row(path: Path) -> dict[str, str]:
    with path.open("r", newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        latest: dict[str, str] | None = None
        for row in reader:
            if None not in row and all(value is not None for value in row.values()):
                latest = dict(row)
    if latest is None:
        raise RuntimeError("bridge CSV has no complete data row")
    return latest


def _campaign_spec(root: Path, fingerprint: str, epoch: int) -> CampaignSpec:
    source = json.loads(
        (root / "config" / "step5d_autotune_campaign_v1.json").read_text(
            encoding="utf-8"
        )
    )
    baseline = source["baseline"]
    objective = source["objective"]
    return CampaignSpec(
        campaign_id=f"step5d-native-{epoch}",
        campaign_epoch=epoch,
        campaign_fingerprint=fingerprint,
        target_force_n=float(baseline["target_force_n"]),
        objective_window_start_s=float(objective["window_s"][0]),
        objective_window_end_s=float(objective["window_s"][1]),
        objective_bin_s=float(objective["bin_s"]),
        required_bins=int(objective["required_complete_bins"]),
        success_mae_n=float(objective["success_mae_n"]),
        confirmation_relative_delta_max=float(
            objective["confirmation_relative_delta_max"]
        ),
        f0_shadow_reaction_normal_base=tuple(
            float(value) for value in baseline["f0_shadow_reaction_normal_base"]
        ),
    )


def _profile(root: Path) -> ExecutionProfile:
    source = json.loads(
        (root / "config" / "step5d_autotune_campaign_v1.json").read_text(
            encoding="utf-8"
        )
    )
    baseline = source["baseline"]
    return ExecutionProfile(
        profile_id=baseline["execution_profile_id"],
        normal_max_rate_rad_s=float(baseline["normal_max_rate_rad_s"]),
        host_qdot_slew_rad_s2=float(baseline["host_qdot_slew_rad_s2"]),
        tp_speedj_accel_rad_s2=float(baseline["tp_speedj_accel_rad_s2"]),
    )


def _event(path: Path, event: str, **payload: Any) -> None:
    row = {"event": event, "monotonic_s": time.monotonic(), **payload}
    encoded = json.dumps(row, sort_keys=True, allow_nan=False) + "\n"
    with path.open("a", encoding="utf-8") as handle:
        handle.write(encoded)
        handle.flush()
        os.fsync(handle.fileno())


def ensure_mailbox_parent(mailbox_path: Path, bridge_run: Path) -> None:
    expected = (bridge_run / "runtime").resolve()
    if mailbox_path.parent.resolve() != expected:
        raise RuntimeError("mailbox must belong to the selected bridge run")
    mailbox_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not mailbox_path.parent.is_dir() or mailbox_path.parent.is_symlink():
        raise RuntimeError("mailbox parent must be a real directory")


def run(args: argparse.Namespace) -> int:
    root = args.experiment_root.resolve()
    bridge_run = args.bridge_run.resolve()
    mailbox_path = args.mailbox.resolve()
    bridge_csv = bridge_run / "bridge_rtde_500hz.csv"
    ready = json.loads((bridge_run / "bridge_ready.json").read_text(encoding="utf-8"))
    if any(
        (
            ready.get("ok") is not True,
            ready.get("bridge_profile") != "step5d_strict_rnn_autotune_v1",
            ready.get("rtde_send_succeeded") is not True,
            ready.get("sensor_stream_ready") is not True,
            ready.get("prewarm_status") != "ok",
        )
    ):
        raise RuntimeError("bridge readiness is not live-complete")
    ensure_mailbox_parent(mailbox_path, bridge_run)
    initial = tp_snapshot_from_bridge_row(_latest_complete_row(bridge_csv))
    if initial.state != "READY_HOME":
        raise RuntimeError(f"TP must start at READY_HOME, got {initial.state}")

    backend = Step5dV35Backend(root)
    frozen = backend.freeze_fingerprint()
    campaign = _campaign_spec(root, frozen.composite_fingerprint, args.campaign_epoch)
    authorization = CampaignAuthorization(
        campaign_id=campaign.campaign_id,
        campaign_fingerprint=frozen.composite_fingerprint,
        bounded_baseline_and_loop=True,
        live_authorized=True,
        controller_readback_verified=True,
    )
    preflight = backend.preflight(offline=False, authorization=authorization)
    if not preflight.ok:
        raise RuntimeError("live backend preflight failed: " + ";".join(preflight.blockers))
    if args.preflight_only:
        print(
            json.dumps(
                {
                    "ok": True,
                    "mode": "preflight_only",
                    "bridge_run": str(bridge_run),
                    "mailbox": str(mailbox_path),
                    "campaign_fingerprint": frozen.composite_fingerprint,
                    "tp_state": initial.state,
                    "cuda_available": preflight.cuda_available,
                },
                sort_keys=True,
            )
        )
        return 0

    campaign_root = (bridge_run / "campaign").resolve()
    store = CampaignStore(campaign_root / "store")
    store.initialize(
        {
            "schema_version": "step5d.autotune.campaign-run/v1",
            "campaign": asdict(campaign),
            "frozen_fingerprint": backend.frozen_payload(frozen),
        }
    )
    supervisor = CampaignSupervisor(
        campaign=campaign,
        backend_id=frozen.backend_id,
        source_fingerprint=frozen.source_fingerprint,
        config_fingerprint=frozen.config_fingerprint,
        execution_profile=_profile(root),
    )
    coordinator = CampaignCoordinator(
        supervisor=supervisor,
        journal=SupervisorJournal(campaign_root / "journal"),
    )
    coordinator.persist_home()
    mailbox = AtomicCommandMailbox(mailbox_path, network_mode=True)
    follower = BridgeCsvFollower(bridge_csv)
    event_path = campaign_root / "events.jsonl"
    campaign_root.mkdir(parents=True, exist_ok=True)
    _event(event_path, "campaign_ready", campaign_id=campaign.campaign_id)

    try:
        while supervisor.phase is CampaignPhase.HOME:
            arm = coordinator.issue_arm(
                store,
                provenance_run_dir=bridge_run,
                require_cuda_botorch=True,
                cuda_fit_mode="serial",
            )
            trial = supervisor.active_trial
            if trial is None:
                raise RuntimeError("coordinator issued ARM without an active trial")
            prepared = backend.prepare_trial(trial, frozen)
            coordinator.dispatch(arm, prepared_trial=prepared, sink=mailbox)
            _event(
                event_path,
                "arm_dispatched",
                trial_uid=trial.trial_uid,
                trial_id=trial.trial_id,
                candidate=trial.candidate.payload(),
                profile=trial.execution_profile.payload(),
            )

            home_path = bridge_run / "campaign_home_reference.json"
            collector: HostClosureCollector | None = None
            for row in follower.rows(timeout_s=args.trial_timeout_s):
                snapshot = tp_snapshot_from_bridge_row(row)
                if snapshot.state != "WAIT_ACK":
                    continue
                if collector is None:
                    home = CampaignHomeReference.load(home_path)
                    collector = HostClosureCollector(
                        expected_arm=arm,
                        home_reference=home,
                    )
                sample = closure_sample_from_bridge_row(row)
                collector.observe(sample, monotonic_s=_finite(row, "t_monotonic_s"))
                if collector.ready:
                    break
            if collector is None or not collector.ready:
                raise RuntimeError("WAIT_ACK did not produce a complete safe closure")

            producer = TrialArtifactProducer(
                (bridge_run / "autotune_trials").resolve(), trial
            )
            result = finalize_produced_bundle_and_dispatch_ack(
                collector=collector,
                producer=producer,
                backend=backend,
                store=store,
                coordinator=coordinator,
                prepared_trial=prepared,
                command_sink=mailbox,
            )
            _event(
                event_path,
                "bundle_closed_ack_dispatched",
                trial_uid=trial.trial_uid,
                disposition=result.evaluation.disposition.value,
                eligible=result.evaluation.eligible,
                objective_mae_n=result.evaluation.objective_mae_n,
            )
            if result.ack_packet is None:
                break

            for row in follower.rows(timeout_s=args.ack_timeout_s):
                snapshot = tp_snapshot_from_bridge_row(row)
                if snapshot.consumed_command_seq != result.ack_packet.command_seq:
                    continue
                if snapshot.state not in {
                    "READY_HOME",
                    "WAIT_INFRA_READY",
                    "FAULT",
                }:
                    continue
                coordinator.reconcile(snapshot)
                break
            _event(event_path, "post_ack", phase=supervisor.phase.value)
            plot_command = [
                sys.executable,
                str(root / "tools" / "publish_step5d_autotune_plot.py"),
                str(result.immutable_bundle_path),
                "--output-dir",
                str(campaign_root / "plots"),
            ]
            try:
                published = subprocess.run(
                    plot_command,
                    check=True,
                    capture_output=True,
                    text=True,
                    timeout=90.0,
                )
                _event(
                    event_path,
                    "trial_plot_published",
                    trial_uid=trial.trial_uid,
                    result=json.loads(published.stdout),
                )
            except (subprocess.SubprocessError, ValueError, json.JSONDecodeError) as exc:
                _event(
                    event_path,
                    "trial_plot_publish_failed",
                    trial_uid=trial.trial_uid,
                    error=f"{type(exc).__name__}: {exc}",
                )
            if supervisor.phase is CampaignPhase.WAIT_INFRA_READY:
                break
            if supervisor.phase is CampaignPhase.HOME:
                recovery = supervisor.recovery_snapshot()
                probe = recovery.governor_probe
                probe_closed = bool(
                    probe is not None
                    and (
                        probe.stage == "b" and probe.identity_b is not None
                        or probe.stage == "a_prime"
                        and probe.identity_a_prime is not None
                    )
                )
                if probe_closed:
                    evidence = supervisor.build_governor_evidence()
                    governor_decision = coordinator.complete_governor_probe(
                        evidence
                    )
                    _event(
                        event_path,
                        "governor_probe_completed",
                        decision=asdict(governor_decision),
                    )
                elif (
                    not result.close_decision.same_candidate_retry_pending
                    and (
                        result.evaluation.eligible
                        or result.close_decision.reason
                        in {
                            "governor_profile_diagnostic_ready",
                            "governor_a_prime_profile_diagnostic_closed",
                        }
                    )
                ):
                    proposed_profile, governor_decision = (
                        coordinator.begin_governor_probe()
                    )
                    _event(
                        event_path,
                        "governor_probe_considered",
                        proposed_profile=(
                            None
                            if proposed_profile is None
                            else proposed_profile.payload()
                        ),
                        decision=asdict(governor_decision),
                    )
    finally:
        follower.close()

    _event(event_path, "campaign_terminal", phase=supervisor.phase.value)
    print(
        json.dumps(
            {
                "ok": supervisor.phase is CampaignPhase.SUCCEEDED,
                "phase": supervisor.phase.value,
                "campaign_root": str(campaign_root),
            },
            sort_keys=True,
        )
    )
    return 0 if supervisor.phase is CampaignPhase.SUCCEEDED else 2


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--experiment-root",
        type=Path,
        default=Path(__file__).resolve().parents[1],
    )
    parser.add_argument("--bridge-run", type=Path, required=True)
    parser.add_argument("--mailbox", type=Path, required=True)
    parser.add_argument("--campaign-epoch", type=int, default=1)
    parser.add_argument("--trial-timeout-s", type=float, default=180.0)
    parser.add_argument("--ack-timeout-s", type=float, default=10.0)
    parser.add_argument("--preflight-only", action="store_true")
    return parser.parse_args()


if __name__ == "__main__":
    raise SystemExit(run(parse_args()))
