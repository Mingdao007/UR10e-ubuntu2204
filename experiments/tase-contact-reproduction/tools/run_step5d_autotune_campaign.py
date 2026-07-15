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
import hashlib
import json
import math
import os
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterator, Mapping


def _bootstrap_stable_cuda_runtime() -> None:
    """Re-exec the CLI with the persistent Step5d CuPy/CUDA runtime."""

    if __name__ != "__main__" or os.environ.get("STEP5D_CUDA_BOOTSTRAPPED") == "1":
        return
    runtime = Path(
        os.environ.get(
            "STEP5D_PYTHON_RUNTIME_ROOT",
            "/home/andy/.codex-python/ur10e-digital-twin-20260711",
        )
    )
    if not (runtime / "cupy").is_dir():
        return
    library_dirs = tuple(
        runtime / "nvidia" / package / "lib"
        for package in ("cuda_nvrtc", "nvjitlink", "cuda_runtime")
        if (runtime / "nvidia" / package / "lib").is_dir()
    )
    required_libraries = tuple(
        runtime / "nvidia" / package / "lib"
        for package in ("cuda_nvrtc", "cuda_runtime")
    )
    if not all(path.is_dir() for path in required_libraries):
        return
    environment = dict(os.environ)
    environment["STEP5D_CUDA_BOOTSTRAPPED"] = "1"
    environment["PYTHONPATH"] = os.pathsep.join(
        (str(runtime), environment.get("PYTHONPATH", ""))
    ).rstrip(os.pathsep)
    environment["LD_LIBRARY_PATH"] = os.pathsep.join(
        (*map(str, library_dirs), environment.get("LD_LIBRARY_PATH", ""))
    ).rstrip(os.pathsep)
    os.execve(sys.executable, [sys.executable, *sys.argv], environment)


_bootstrap_stable_cuda_runtime()

from step5d_autotune_backend import (
    CampaignAuthorization,
    Step5dV35Backend,
)
from step5d_autotune_contract import CampaignSpec, ExecutionProfile, ForceCandidate
from step5d_autotune_coordinator import CampaignCoordinator
from step5d_autotune_journal import (
    JournalIntegrityError,
    ReconcileAction,
    SupervisorJournal,
    TpSnapshot,
)
from step5d_autotune_live_driver import (
    AtomicCommandMailbox,
    CampaignHomeReference,
    HostClosureCollector,
    TrialArtifactProducer,
    finalize_produced_bundle_and_dispatch_ack,
)
from step5d_autotune_state_machine import TpLoopState
from step5d_autotune_store import CampaignStore
from step5d_autotune_supervisor import (
    TERMINAL_PHASES,
    CampaignPhase,
    CampaignSupervisor,
    execution_profile_integer_id,
)


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


def _campaign_spec(
    root: Path,
    fingerprint: str,
    epoch: int,
    *,
    campaign_id: str | None = None,
) -> CampaignSpec:
    source = json.loads(
        (root / "config" / "step5d_autotune_campaign_v1.json").read_text(
            encoding="utf-8"
        )
    )
    baseline = source["baseline"]
    objective = source["objective"]
    return CampaignSpec(
        campaign_id=campaign_id or f"step5d-native-{epoch}",
        campaign_epoch=epoch,
        campaign_fingerprint=fingerprint,
        target_force_n=float(baseline["target_force_n"]),
        objective_window_start_s=float(objective["window_s"][0]),
        objective_window_end_s=float(objective["window_s"][1]),
        objective_bin_s=float(objective["bin_s"]),
        required_bins=int(objective["required_complete_bins"]),
        success_mae_n=float(objective["success_mae_n"]),
        confirmation_relative_delta_max=float(
            objective.get("confirmation_relative_delta_max", 0.15)
        ),
        f0_shadow_reaction_normal_base=tuple(
            float(value) for value in baseline["f0_shadow_reaction_normal_base"]
        ),
    )


def _campaign_from_payload(payload: Mapping[str, Any]) -> CampaignSpec:
    values = dict(payload)
    shadow = values.get("f0_shadow_reaction_normal_base")
    if shadow is not None:
        values["f0_shadow_reaction_normal_base"] = tuple(shadow)
    return CampaignSpec(**values)


@dataclass(frozen=True)
class CampaignEpochLayout:
    epoch: int
    root: Path
    store_root: Path
    journal_root: Path
    manifest: Mapping[str, Any]
    campaign: CampaignSpec


def discover_campaign_epochs(campaign_root: Path) -> tuple[CampaignEpochLayout, ...]:
    """Return the verified epoch chain instead of assuming epoch 1 is parent."""

    campaign_root = campaign_root.resolve()
    roots: list[Path] = []
    if (campaign_root / "store" / "campaign.json").is_file():
        roots.append(campaign_root)
    epochs_root = campaign_root / "epochs"
    if epochs_root.is_dir():
        for candidate in sorted(epochs_root.iterdir()):
            if candidate.is_symlink() or not candidate.is_dir():
                raise RuntimeError("campaign epochs directory contains an unsafe entry")
            if len(candidate.name) != 10 or not candidate.name.isdigit():
                raise RuntimeError("campaign epoch directory name must be ten digits")
            if (candidate / "store" / "campaign.json").is_file():
                roots.append(candidate.resolve())

    layouts: list[CampaignEpochLayout] = []
    seen_epochs: set[int] = set()
    campaign_id: str | None = None
    for epoch_root in roots:
        manifest_path = epoch_root / "store" / "campaign.json"
        if manifest_path.is_symlink():
            raise RuntimeError("campaign manifest must not be a symlink")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if not isinstance(manifest, dict) or not isinstance(
            manifest.get("campaign"), Mapping
        ):
            raise RuntimeError("campaign manifest lacks a campaign object")
        campaign = _campaign_from_payload(manifest["campaign"])
        if epoch_root.parent == epochs_root and int(epoch_root.name) != campaign.campaign_epoch:
            raise RuntimeError("campaign epoch directory disagrees with its manifest")
        if campaign.campaign_epoch in seen_epochs:
            raise RuntimeError("campaign epoch appears in more than one store")
        if campaign_id is not None and campaign.campaign_id != campaign_id:
            raise RuntimeError("campaign epoch chain crosses campaign_id")
        seen_epochs.add(campaign.campaign_epoch)
        campaign_id = campaign_id or campaign.campaign_id
        layouts.append(
            CampaignEpochLayout(
                epoch=campaign.campaign_epoch,
                root=epoch_root,
                store_root=epoch_root / "store",
                journal_root=epoch_root / "journal",
                manifest=manifest,
                campaign=campaign,
            )
        )
    return tuple(sorted(layouts, key=lambda row: row.epoch))


def _sha256_path(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


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


def profile_from_epoch(layout: CampaignEpochLayout) -> ExecutionProfile:
    """Restore the retained profile from durable epoch bytes, never current config."""

    latest = SupervisorJournal(layout.journal_root).load_latest()
    payload = layout.manifest.get("execution_profile")
    if not isinstance(payload, Mapping):
        state = latest.state
        cursors = [
            state.active_trial,
            None if state.pending_ack is None else state.pending_ack.trial,
            None if state.pending_retry is None else state.pending_retry.origin_trial,
        ]
        for cursor in cursors:
            if cursor is None:
                continue
            trial_payload = json.loads(
                Path(cursor.trial_spec.path).read_text(encoding="utf-8")
            )
            payload = trial_payload.get("execution_profile")
            if isinstance(payload, Mapping):
                break
        if not isinstance(payload, Mapping):
            for row in reversed(CampaignStore(layout.store_root).read_resume_history()):
                trial_payload = row.get("trial")
                candidate = (
                    trial_payload.get("execution_profile")
                    if isinstance(trial_payload, Mapping)
                    else None
                )
                if (
                    isinstance(candidate, Mapping)
                    and candidate.get("profile_id") == state.execution_profile_id
                ):
                    payload = candidate
                    break
    if not isinstance(payload, Mapping):
        raise RuntimeError("durable epoch does not contain its retained execution profile")
    profile = ExecutionProfile(**dict(payload))
    if any(
        (
            profile.profile_id != latest.state.execution_profile_id,
            execution_profile_integer_id(profile)
            != latest.state.execution_profile_integer_id,
        )
    ):
        raise RuntimeError("durable epoch profile differs from its journal binding")
    return profile


def _campaign_authorization(
    path: Path,
    *,
    campaign: CampaignSpec,
    campaign_fingerprint: str,
) -> CampaignAuthorization:
    """Load an external owner-produced authorization bound to one fingerprint."""

    if not path.is_absolute() or path.is_symlink() or not path.is_file():
        raise RuntimeError("campaign authorization must be an absolute regular file")
    payload = json.loads(path.read_text(encoding="utf-8"))
    required = {
        "schema_version",
        "campaign_id",
        "campaign_epoch",
        "campaign_fingerprint",
        "bounded_baseline_and_loop",
        "live_authorized",
        "controller_readback_verified",
        "authorization_source",
        "authorized_at",
    }
    if not isinstance(payload, dict) or set(payload) != required:
        raise RuntimeError("campaign authorization fields differ from v1 contract")
    if payload["schema_version"] != "step5d_autotune_campaign_authorization_v1":
        raise RuntimeError("campaign authorization schema mismatch")
    if any(
        (
            payload["campaign_id"] != campaign.campaign_id,
            payload["campaign_epoch"] != campaign.campaign_epoch,
            payload["campaign_fingerprint"] != campaign_fingerprint,
            payload["bounded_baseline_and_loop"] is not True,
            payload["live_authorized"] is not True,
            payload["controller_readback_verified"] is not True,
            not isinstance(payload["authorization_source"], str),
            not payload["authorization_source"].strip(),
            not isinstance(payload["authorized_at"], str),
            not payload["authorized_at"].strip(),
        )
    ):
        raise RuntimeError("campaign authorization is not bound to this exact epoch/fingerprint")
    return CampaignAuthorization(
        campaign_id=campaign.campaign_id,
        campaign_fingerprint=campaign_fingerprint,
        bounded_baseline_and_loop=True,
        live_authorized=True,
        controller_readback_verified=True,
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
    forced_values = (args.force_p, args.force_i, args.force_damping)
    if any(value is not None for value in forced_values) and not all(
        value is not None for value in forced_values
    ):
        raise RuntimeError("--force-p, --force-i, and --force-damping must be supplied together")
    forced_candidate = (
        None
        if forced_values[0] is None
        else ForceCandidate(
            force_p_gain=forced_values[0],
            force_i_gain=forced_values[1],
            force_damping=forced_values[2],
        )
    )
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
    campaign_root = (bridge_run / "campaign").resolve()
    campaign_root.mkdir(parents=True, exist_ok=True)
    epoch_chain = discover_campaign_epochs(campaign_root)
    if epoch_chain and args.campaign_epoch < epoch_chain[-1].epoch:
        raise RuntimeError("campaign epoch cannot regress behind the latest durable epoch")
    existing_layout = next(
        (row for row in epoch_chain if row.epoch == args.campaign_epoch),
        None,
    )
    prior_layout = next(
        (row for row in reversed(epoch_chain) if row.epoch < args.campaign_epoch),
        None,
    )
    campaign = _campaign_spec(
        root,
        frozen.composite_fingerprint,
        args.campaign_epoch,
        campaign_id=(
            None
            if not epoch_chain
            else epoch_chain[-1].campaign.campaign_id
        ),
    )
    if args.authorization_file is None:
        raise RuntimeError(
            "--authorization-file is required; the runner cannot self-authorize live motion"
        )
    authorization = _campaign_authorization(
        args.authorization_file.resolve(),
        campaign=campaign,
        campaign_fingerprint=frozen.composite_fingerprint,
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

    epoch_root = (
        existing_layout.root
        if existing_layout is not None
        else campaign_root
        if not epoch_chain
        else campaign_root / "epochs" / f"{campaign.campaign_epoch:010d}"
    )
    store = CampaignStore(epoch_root / "store")
    current_profile = _profile(root)
    if existing_layout is not None:
        store.initialize(dict(existing_layout.manifest))
    else:
        parent_epoch: dict[str, Any] | None = None
        if prior_layout is not None:
            parent_journal = SupervisorJournal(prior_layout.journal_root).load_latest()
            parent_epoch = {
                "campaign_epoch": prior_layout.epoch,
                "journal_revision": parent_journal.revision,
                "journal_record_sha256": parent_journal.record_sha256,
                "store_manifest_sha256": _sha256_path(
                    prior_layout.store_root / "campaign.json"
                ),
            }
        store.initialize({
            "schema_version": "step5d.autotune.campaign-run/v1",
            "campaign": asdict(campaign),
            "frozen_fingerprint": backend.frozen_payload(frozen),
            "execution_profile": current_profile.payload(),
            "parent_epoch": parent_epoch,
        })
    journal = SupervisorJournal(epoch_root / "journal")
    migrating = bool(
        prior_layout is not None
        and existing_layout is None
        and not (epoch_root / "journal" / "latest.json").exists()
    )
    if migrating:
        assert prior_layout is not None
        old_frozen = prior_layout.manifest["frozen_fingerprint"]
        old_supervisor = CampaignSupervisor(
            campaign=prior_layout.campaign,
            backend_id=old_frozen["backend_id"],
            source_fingerprint=old_frozen["source_fingerprint"],
            config_fingerprint=old_frozen["config_fingerprint"],
            execution_profile=profile_from_epoch(prior_layout),
        )
        old_journal = SupervisorJournal(prior_layout.journal_root)
        old_restored = CampaignCoordinator.restore(
            supervisor=old_supervisor,
            journal=old_journal,
            latest=old_journal.load_latest(),
            resume_history=CampaignStore(prior_layout.store_root).read_resume_history(),
            promotion_history=CampaignStore(prior_layout.store_root).read_promotion_history(),
            tp_snapshot=initial,
        )
        if (
            old_restored.decision.action is ReconcileAction.SEND_PERSISTED_ARM
            and forced_candidate is not None
        ):
            mailbox_payload = json.loads(mailbox_path.read_text(encoding="utf-8"))
            mailbox_seq = int(mailbox_payload["packet"]["command_seq"])
            if mailbox_seq > initial.consumed_command_seq:
                raise RuntimeError("mailbox contains an unconsumed command and cannot be replaced")
            old_restored.coordinator.cancel_unconsumed_arm(initial, persist=False)
        elif old_restored.decision.action is not ReconcileAction.RESUME_HOME:
            raise RuntimeError(
                "prior code epoch is not safely at Home: "
                f"{old_restored.decision.action.value}: "
                f"{old_restored.decision.reason}"
            )
        coordinator = old_restored.coordinator.resume_after_code_change(
            new_journal=journal,
            campaign=campaign,
            source_fingerprint=frozen.source_fingerprint,
            config_fingerprint=frozen.config_fingerprint,
        )
        supervisor = coordinator.supervisor
        resumed = True
    else:
        supervisor = CampaignSupervisor(
            campaign=campaign,
            backend_id=frozen.backend_id,
            source_fingerprint=frozen.source_fingerprint,
            config_fingerprint=frozen.config_fingerprint,
            execution_profile=_profile(root),
        )
        try:
            latest = journal.load_latest()
        except JournalIntegrityError as exc:
            if "missing" not in str(exc) and "no durable revision" not in str(exc):
                raise
            supervisor.seed_command_sequence_from_tp(
                initial.consumed_command_seq
            )
            coordinator = CampaignCoordinator(supervisor=supervisor, journal=journal)
            coordinator.persist_home()
            resumed = False
        else:
            restored = CampaignCoordinator.restore(
                supervisor=supervisor,
                journal=journal,
                latest=latest,
                resume_history=store.read_resume_history(),
                promotion_history=store.read_promotion_history(),
                tp_snapshot=initial,
            )
            if restored.decision.action is not ReconcileAction.RESUME_HOME:
                raise RuntimeError(
                    "existing campaign cannot resume from READY_HOME: "
                    f"{restored.decision.action.value}: {restored.decision.reason}"
                )
            coordinator = restored.coordinator
            resumed = True
    mailbox = AtomicCommandMailbox(mailbox_path, network_mode=True)
    follower = BridgeCsvFollower(bridge_csv)
    event_path = campaign_root / "events.jsonl"
    campaign_root.mkdir(parents=True, exist_ok=True)
    _event(
        event_path,
        "campaign_resumed" if resumed else "campaign_ready",
        campaign_id=campaign.campaign_id,
        campaign_epoch=campaign.campaign_epoch,
        migrated_code_epoch=migrating,
    )

    completed_trials = 0
    try:
        while supervisor.phase is CampaignPhase.HOME:
            arm = coordinator.issue_arm(
                store,
                provenance_run_dir=bridge_run,
                require_cuda_botorch=True,
                cuda_fit_mode="serial",
                forced_candidate=forced_candidate,
            )
            forced_candidate = None
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
            completed_trials += 1
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
            if args.one_trial:
                break
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

    one_trial_complete = bool(
        args.one_trial
        and completed_trials == 1
        and supervisor.phase is CampaignPhase.HOME
    )
    campaign_succeeded = supervisor.phase is CampaignPhase.SUCCEEDED
    campaign_terminal = supervisor.phase in TERMINAL_PHASES
    _event(
        event_path,
        "campaign_terminal" if campaign_terminal else "runner_stopped",
        phase=supervisor.phase.value,
        trial_completed=completed_trials > 0,
        one_trial_limit_reached=one_trial_complete,
        campaign_succeeded=campaign_succeeded,
    )
    print(
        json.dumps(
            {
                "run_ok": campaign_succeeded or one_trial_complete,
                "trial_completed": completed_trials > 0,
                "campaign_succeeded": campaign_succeeded,
                "campaign_terminal": campaign_terminal,
                "phase": supervisor.phase.value,
                "campaign_root": str(campaign_root),
            },
            sort_keys=True,
        )
    )
    return 0 if campaign_succeeded or one_trial_complete else 2


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--experiment-root",
        type=Path,
        default=Path(__file__).resolve().parents[1],
    )
    parser.add_argument("--bridge-run", type=Path, required=True)
    parser.add_argument("--mailbox", type=Path, required=True)
    parser.add_argument("--authorization-file", type=Path)
    parser.add_argument("--campaign-epoch", type=int, default=1)
    parser.add_argument("--trial-timeout-s", type=float, default=180.0)
    parser.add_argument("--ack-timeout-s", type=float, default=10.0)
    parser.add_argument("--force-p", type=float)
    parser.add_argument("--force-i", type=float)
    parser.add_argument("--force-damping", type=float)
    parser.add_argument("--one-trial", action="store_true")
    parser.add_argument("--preflight-only", action="store_true")
    return parser.parse_args()


if __name__ == "__main__":
    raise SystemExit(run(parse_args()))
