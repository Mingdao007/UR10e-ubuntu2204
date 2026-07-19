#!/usr/bin/env python3
"""Filesystem-only subprocess harness for the v3 exact ten-row acceptance gate.

This file is test infrastructure, not a production bridge.  It deliberately
uses the frozen v1 CampaignStore, SupervisorJournal, CampaignCoordinator and
AtomicCommandMailbox while keeping every transport local to a temporary
directory.  No socket, controller, RTDE, Dashboard, or motion surface exists.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import tempfile
import time
import types
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Mapping


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

# Hosted v3 CI intentionally installs only pytest.  The forced-candidate path
# below never executes NumPy, but the frozen v1 optimizer imports it while the
# real supervisor module is defined.  Keep this narrow fallback test-only.
try:  # pragma: no cover - the normal workstation has NumPy.
    import numpy  # noqa: F401
except ModuleNotFoundError:  # pragma: no cover - exercised by hermetic CI.
    numpy_stub = types.ModuleType("numpy")
    numpy_stub.argmax = lambda values: max(  # type: ignore[attr-defined]
        range(len(values)), key=lambda index: values[index]
    )
    sys.modules["numpy"] = numpy_stub

from step5d_autotune_batch_plan import CandidateBatchPlan  # noqa: E402
from step5d_autotune_contract import (  # noqa: E402
    CampaignSpec,
    CaptureArtifactPaths,
    CaptureManifest,
    Evaluation,
    ExecutionProfile,
    ForceCandidate,
    TrialDisposition,
)
from step5d_autotune_coordinator import CampaignCoordinator  # noqa: E402
from step5d_autotune_journal import (  # noqa: E402
    ReconcileAction,
    SupervisorJournal,
    TpSnapshot,
)
from step5d_autotune_live_driver import (  # noqa: E402
    AtomicCommandMailbox,
    ImmutableBundleStoreReceipt,
    MailboxError,
)
from step5d_autotune_runtime_lifecycle import (  # noqa: E402
    PostAckControllerReadback,
    prepare_batch_attempt_context,
)
from step5d_autotune_state_machine import (  # noqa: E402
    HostCommand,
    SafeClosureEvidence,
)
from step5d_autotune_store import CampaignStore  # noqa: E402
from step5d_autotune_supervisor import (  # noqa: E402
    CampaignPhase,
    CampaignSupervisor,
    execution_profile_integer_id,
)
from ur10e_experiment_runtime.stage_adapters import (  # noqa: E402
    control_candidate_uid,
)


POLL_S = 0.002
TIMEOUT_S = 10.0
SOURCE_FINGERPRINT = "b" * 64
CONFIG_FINGERPRINT = "c" * 64
CAMPAIGN_FINGERPRINT = "a" * 64


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def atomic_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary_path = Path(temporary)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, allow_nan=False, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
        _fsync_directory(path.parent)
    finally:
        temporary_path.unlink(missing_ok=True)


def append_event(path: Path, event: str, **details: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "event": event,
        "monotonic_ns": time.monotonic_ns(),
        "pid": os.getpid(),
        **details,
    }
    encoded = json.dumps(payload, allow_nan=False, sort_keys=True) + "\n"
    with path.open("a", encoding="utf-8") as handle:
        handle.write(encoded)
        handle.flush()
        os.fsync(handle.fileno())


def wait_for_file(path: Path, *, timeout_s: float = TIMEOUT_S) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if path.is_file():
            return
        time.sleep(POLL_S)
    raise TimeoutError(f"timed out waiting for {path}")


def read_json(path: Path) -> dict[str, Any]:
    deadline = time.monotonic() + TIMEOUT_S
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                raise ValueError("payload is not an object")
            return payload
        except (FileNotFoundError, json.JSONDecodeError, ValueError) as exc:
            last_error = exc
            time.sleep(POLL_S)
    raise TimeoutError(f"timed out reading {path}: {last_error}")


def safe_closure() -> SafeClosureEvidence:
    return SafeClosureEvidence(
        tp_position_error_m=0.001,
        tp_orientation_error_rad=0.01,
        tp_joint_error_max_rad=0.005,
        host_position_error_m=0.001,
        host_orientation_error_rad=0.01,
        host_joint_error_max_rad=0.005,
        host_tcp_linear_speed_m_s=0.0005,
        host_tcp_angular_speed_rad_s=0.005,
        host_qd_max_rad_s=0.005,
        host_safety_mode="NORMAL",
        host_dwell_s=0.5,
        trial_token_match=True,
        capture_hashes_complete=True,
        terminal_manifest_complete=True,
        fingerprint_closed=True,
    )


def sha256(encoded: bytes) -> str:
    return hashlib.sha256(encoded).hexdigest()


def write_evaluated_bundle(
    store: CampaignStore,
    trial: Any,
    root: Path,
    *,
    candidate_index: int,
) -> tuple[Path, CaptureManifest, Evaluation]:
    closure = safe_closure()
    artifact_dir = root / "captures" / trial.trial_uid
    artifact_dir.mkdir(parents=True)
    csv_bytes = (
        "stage,path_time_s,force_b_z_n,trial_uid,candidate_index\n"
        f"25,5.05,12.0,{trial.trial_uid},{candidate_index}\n"
    ).encode("utf-8")
    metadata_bytes = (
        json.dumps(
            {
                "schema_version": "step5d.v3.fake-metadata/v1",
                "trial_uid": trial.trial_uid,
                "backend_id": trial.backend_id,
                "candidate_token": trial.candidate_token,
            },
            allow_nan=False,
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")
    terminal_bytes = (
        json.dumps(
            {
                "schema_version": "step5d.v3.fake-terminal/v1",
                "trial_uid": trial.trial_uid,
                "backend_id": trial.backend_id,
                "candidate_token": trial.candidate_token,
                "terminal_reason": 1,
                "safe_closure_evidence": closure.payload(),
            },
            allow_nan=False,
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")
    paths = CaptureArtifactPaths(
        artifact_dir / "capture.csv",
        artifact_dir / "metadata.json",
        artifact_dir / "terminal_manifest.json",
    )
    for path, encoded in zip(
        paths.by_role().values(),
        (csv_bytes, metadata_bytes, terminal_bytes),
        strict=True,
    ):
        path.write_bytes(encoded)

    manifest = CaptureManifest(
        trial_uid=trial.trial_uid,
        backend_id=trial.backend_id,
        source_fingerprint_pre=trial.source_fingerprint,
        source_fingerprint_post=trial.source_fingerprint,
        config_fingerprint_pre=trial.config_fingerprint,
        config_fingerprint_post=trial.config_fingerprint,
        candidate_token=trial.candidate_token,
        terminal_reason=1,
        host_cause=None,
        csv_sha256=sha256(csv_bytes),
        metadata_sha256=sha256(metadata_bytes),
        terminal_manifest_sha256=sha256(terminal_bytes),
        completion_marker=True,
        cadence_ok=True,
        feedback_fresh=True,
        rnn_oracle_aligned=True,
        safety_normal=True,
        returned_safe=True,
        immutable_bundle_written=True,
        stage25_complete_s=60.0,
        safe_closure_evidence=closure,
        evidence={"source": "filesystem_only_fake_bridge_gate"},
    )
    evaluation = Evaluation(
        trial_uid=trial.trial_uid,
        backend_id=trial.backend_id,
        eligible=True,
        disposition=TrialDisposition.OBJECTIVE,
        objective_mae_n=0.40 + candidate_index / 100.0,
        force_bias_n=0.0,
        force_std_n=0.1,
        coverage_12_plus_minus_1_ratio=1.0,
        complete_bins=550,
        safe_closure=True,
        metrics={"source": "fake_evaluator"},
    )
    bundle = store.write_trial_bundle(
        trial,
        manifest,
        evaluation,
        artifact_paths=paths,
    )
    return bundle, manifest, evaluation


def prepared_trial(
    trial: Any,
    *,
    batch_row_index: int,
    trial_overlay: Mapping[str, Any],
) -> SimpleNamespace:
    candidate = trial.candidate
    execution = trial.execution_profile
    environment = {
        "STEP5D_AUTOTUNE_TRIAL_UID": trial.trial_uid,
        "STEP5D_AUTOTUNE_CAMPAIGN_EPOCH": str(trial.campaign.campaign_epoch),
        "STEP5D_AUTOTUNE_TRIAL_ID": str(trial.trial_id),
        "STEP5D_AUTOTUNE_CANDIDATE_TOKEN": str(trial.candidate_token),
        "STEP5D_AUTOTUNE_EXECUTION_PROFILE_ID": str(
            execution_profile_integer_id(execution)
        ),
        "STEP5D_AUTOTUNE_COMMAND_SEQ": str(trial.command_seq),
        "STEP5D_AUTOTUNE_FORCE_P": str(candidate.force_p_gain),
        "STEP5D_AUTOTUNE_FORCE_I": str(candidate.force_i_gain),
        "STEP5D_AUTOTUNE_FORCE_DAMPING": str(candidate.force_damping),
        "STEP5D_AUTOTUNE_NORMAL_RATE_RAD_S": str(execution.normal_max_rate_rad_s),
        "STEP5D_AUTOTUNE_HOST_SLEW_RAD_S2": str(
            execution.host_qdot_slew_rad_s2
        ),
        "STEP5D_AUTOTUNE_SPEEDJ_ACCELERATION_RAD_S2": str(
            execution.tp_speedj_accel_rad_s2
        ),
    }
    frozen = SimpleNamespace(
        source_fingerprint=trial.source_fingerprint,
        config_fingerprint=trial.config_fingerprint,
        composite_fingerprint=trial.campaign.campaign_fingerprint,
    )
    arguments = (
        "--step5d-autotune-force-p",
        environment["STEP5D_AUTOTUNE_FORCE_P"],
        "--step5d-autotune-force-i",
        environment["STEP5D_AUTOTUNE_FORCE_I"],
        "--step5d-autotune-force-damping",
        environment["STEP5D_AUTOTUNE_FORCE_DAMPING"],
        "--step5d-autotune-normal-rate-rad-s",
        environment["STEP5D_AUTOTUNE_NORMAL_RATE_RAD_S"],
        "--step5d-autotune-host-slew-rad-s2",
        environment["STEP5D_AUTOTUNE_HOST_SLEW_RAD_S2"],
        "--step5d-autotune-speedj-acceleration-rad-s2",
        environment["STEP5D_AUTOTUNE_SPEEDJ_ACCELERATION_RAD_S2"],
        "--step5d-autotune-campaign-epoch",
        environment["STEP5D_AUTOTUNE_CAMPAIGN_EPOCH"],
        "--step5d-autotune-trial-id",
        environment["STEP5D_AUTOTUNE_TRIAL_ID"],
        "--step5d-autotune-command",
        "1",
        "--step5d-autotune-candidate-token",
        environment["STEP5D_AUTOTUNE_CANDIDATE_TOKEN"],
        "--step5d-autotune-execution-profile-id",
        environment["STEP5D_AUTOTUNE_EXECUTION_PROFILE_ID"],
        "--step5d-autotune-command-sequence",
        environment["STEP5D_AUTOTUNE_COMMAND_SEQ"],
    )
    return SimpleNamespace(
        trial=trial,
        frozen=frozen,
        environment=environment,
        runner_arguments=arguments,
        batch_row_index=batch_row_index,
        trial_overlay=dict(trial_overlay),
    )


def exact_trial_overlay(
    candidate: ForceCandidate,
    profile: ExecutionProfile,
) -> dict[str, Any]:
    control = {
        "force_p_gain": candidate.force_p_gain,
        "force_i_gain": candidate.force_i_gain,
        "force_damping": candidate.force_damping,
        "orientation_ko": 0.4,
    }
    return {
        **control,
        "control_candidate_uid": control_candidate_uid(control),
        "execution_profile_id": profile.profile_id,
        "step5d_preload_filtered_min_n": 5.0,
        "step5d_preload_filtered_max_n": 22.0,
        "step5d_preload_raw_min_n": 3.0,
        "step5d_preload_raw_max_n": 25.0,
        "step5d_preload_force_norm_max_n": 25.0,
        "step5d_preload_hold_s": 0.1,
        "step5d_preload_timeout_s": 10.0,
    }


def snapshot_from_payload(payload: Mapping[str, Any]) -> TpSnapshot:
    return TpSnapshot(
        payload["campaign_epoch_echo"],
        payload["trial_id_echo"],
        payload["state"],
        payload["candidate_token_echo"],
        payload["terminal_reason"],
        payload["execution_profile_integer_id_echo"],
        payload["consumed_command_seq"],
    )


def wait_snapshot(
    path: Path,
    *,
    state: str,
    consumed_command_seq: int,
) -> tuple[dict[str, Any], TpSnapshot]:
    deadline = time.monotonic() + TIMEOUT_S
    last: dict[str, Any] | None = None
    while time.monotonic() < deadline:
        last = read_json(path)
        snapshot_payload = last.get("snapshot")
        if isinstance(snapshot_payload, dict):
            snapshot = snapshot_from_payload(snapshot_payload)
            if (
                snapshot.state == state
                and snapshot.consumed_command_seq == consumed_command_seq
            ):
                return last, snapshot
        time.sleep(POLL_S)
    raise TimeoutError(
        f"timed out waiting for {state}/{consumed_command_seq}; last={last}"
    )


def packet_payload(command: Any) -> dict[str, int]:
    packet = command.packet
    return {
        "campaign_epoch": packet.campaign_epoch,
        "trial_id": packet.trial_id,
        "command": int(packet.command),
        "candidate_token": packet.candidate_token,
        "execution_profile_id": packet.execution_profile_id,
        "command_seq": packet.command_seq,
    }


def run_bridge(args: argparse.Namespace) -> int:
    root = Path(args.root).absolute()
    mailbox = AtomicCommandMailbox((root / "control" / "command_mailbox.json").absolute())
    snapshot_path = Path(args.snapshot).absolute()
    events = Path(args.events).absolute()
    ready = Path(args.ready).absolute()
    stop = Path(args.stop).absolute()
    last_sha: str | None = None
    append_event(events, "process_started", role="fake_bridge")
    append_event(events, "simulated_play", session="one_continuous_fake_tp_session")
    atomic_json(ready, {"pid": os.getpid(), "role": "fake_bridge"})
    while not stop.exists():
        try:
            command = mailbox.read_latest()
        except MailboxError:
            # AtomicCommandMailbox intentionally rejects a file identity that
            # changes during read.  A concurrent fsync+replace is retriable;
            # malformed stable bytes remain visible and fail on every retry.
            time.sleep(POLL_S)
            continue
        if command is None or command.sha256 == last_sha:
            time.sleep(POLL_S)
            continue
        last_sha = command.sha256
        packet = command.packet
        if packet.command is HostCommand.ARM:
            snapshot = {
                "campaign_epoch_echo": packet.campaign_epoch,
                "trial_id_echo": packet.trial_id,
                "state": "WAIT_ACK",
                "candidate_token_echo": packet.candidate_token,
                "terminal_reason": 1,
                "execution_profile_integer_id_echo": packet.execution_profile_id,
                "consumed_command_seq": packet.command_seq,
            }
            event = "wait_ack"
        elif packet.command is HostCommand.ACK_BUNDLE:
            batch_row_index = command.binding.batch_row_index
            if batch_row_index is None:
                raise RuntimeError("exact batch ACK lacks batch_row_index binding")
            typed_state = (
                "READY_HOME_CLOSED" if batch_row_index == 10 else "READY_NEAR"
            )
            snapshot = {
                "campaign_epoch_echo": packet.campaign_epoch,
                "trial_id_echo": packet.trial_id,
                "state": typed_state,
                "candidate_token_echo": packet.candidate_token,
                "terminal_reason": 1,
                "execution_profile_integer_id_echo": packet.execution_profile_id,
                "consumed_command_seq": packet.command_seq,
            }
            event = (
                "ready_home_closed" if batch_row_index == 10 else "ready_near"
            )
        else:
            raise RuntimeError(f"unexpected fake-bridge command: {packet.command}")
        payload = {
            "mailbox_sha256": command.sha256,
            "packet": packet_payload(command),
            "snapshot": snapshot,
            "trial_uid": command.binding.trial_uid,
        }
        atomic_json(snapshot_path, payload)
        append_event(events, event, **payload)
    append_event(events, "process_stopped", role="fake_bridge")
    return 0


def run_runner(args: argparse.Namespace) -> int:
    root = Path(args.root).absolute()
    events = Path(args.events).absolute()
    heartbeat = Path(args.heartbeat).absolute()
    ready = Path(args.ready).absolute()
    done = Path(args.done).absolute()
    stop = Path(args.stop).absolute()
    start_gate = Path(args.start_gate).absolute()
    snapshot_path = Path(args.snapshot).absolute()
    mailbox_path = (root / "control" / "command_mailbox.json").absolute()
    mailbox_path.parent.mkdir(parents=True, exist_ok=True)

    campaign = CampaignSpec(
        campaign_id="v3-faithful-fake-bridge",
        campaign_epoch=1,
        campaign_fingerprint=CAMPAIGN_FINGERPRINT,
        f0_shadow_reaction_normal_base=(0.0, 0.0, 1.0),
    )
    profile = ExecutionProfile("nf010-slew010-a010", 0.010)
    store = CampaignStore(root / "store")
    store.initialize(
        {
            "campaign": asdict(campaign),
            "execution_profile": profile.payload(),
            "selection_policy": "codex_batches",
        }
    )
    supervisor = CampaignSupervisor(
        campaign=campaign,
        backend_id="step5d_v35_native",
        source_fingerprint=SOURCE_FINGERPRINT,
        config_fingerprint=CONFIG_FINGERPRINT,
        execution_profile=profile,
        selection_policy="codex_batches",
    )
    journal = SupervisorJournal(root / "journal")
    coordinator = CampaignCoordinator(supervisor=supervisor, journal=journal)
    coordinator.persist_home()
    mailbox = AtomicCommandMailbox(mailbox_path)

    def runner_event(event: str, **details: Any) -> None:
        append_event(
            events,
            event,
            control_fingerprint=args.control_fingerprint,
            **details,
        )
        atomic_json(
            heartbeat,
            {
                "event": event,
                "journal_revision": coordinator.journal.load_latest().revision,
                "pid": os.getpid(),
                "trial_uid": details.get("trial_uid"),
            },
        )

    runner_event("process_started", role="runner")
    atomic_json(ready, {"pid": os.getpid(), "role": "runner"})
    wait_for_file(start_gate)

    candidates = tuple(
        ForceCandidate.from_log2(p=p, damping=damping, i=0.0)
        for damping in (0.0, 0.25)
        for p in (0.0, 0.25, 0.50, 0.75, 1.0)
    )
    batch_plan = CandidateBatchPlan(
        campaign_id=campaign.campaign_id,
        revision=1,
        closed=False,
        code_fix_replay_candidate_uid=None,
        batch_size=10,
        batches=(candidates,),
        payload={},
    )
    for index, candidate in enumerate(candidates, 1):
        batch_context = prepare_batch_attempt_context(
            plan=batch_plan,
            selected_candidate=candidate,
            profile=profile,
            overlay_resolver=lambda selected: exact_trial_overlay(
                selected,
                profile,
            ),
            campaign_uid=campaign.campaign_id,
            experiment_fingerprint=CAMPAIGN_FINGERPRINT,
            launch_fingerprint=args.control_fingerprint,
            controller_readback_fingerprint="c" * 64,
            authorization_ref_sha256="d" * 64,
            stopping_bound_fingerprint=None,
            plant_epoch=campaign.campaign_epoch,
            campaign_root=root,
            campaign_home_pose=(0.45, 0.10, 0.20, 3.14, 0.0, 0.0),
        )
        arm = coordinator.issue_arm(
            store,
            forced_candidate=candidate,
            require_cuda_botorch=False,
        )
        trial = supervisor.active_trial
        if trial is None:
            raise RuntimeError("v1 supervisor did not retain the issued ARM")
        prepared = prepared_trial(
            trial,
            batch_row_index=index,
            trial_overlay=batch_context.expected_row.trial_overlay,
        )
        batch_context.start_attempt(trial, prepared.trial_overlay)
        arm_receipt = coordinator.dispatch(arm, prepared_trial=prepared, sink=mailbox)
        runner_event(
            "arm_dispatched",
            candidate_index=index,
            command_seq=arm.command_seq,
            mailbox_sha256=arm_receipt.mailbox_sha256,
            trial_uid=trial.trial_uid,
        )

        _, wait_ack = wait_snapshot(
            snapshot_path,
            state="WAIT_ACK",
            consumed_command_seq=arm.command_seq,
        )
        reconcile = coordinator.reconcile(wait_ack)
        if reconcile.decision.action is not ReconcileAction.RESUME_CLOSURE:
            raise RuntimeError(
                f"WAIT_ACK did not enter durable closure: {reconcile.decision}"
            )
        runner_event(
            "wait_ack_reconciled",
            candidate_index=index,
            command_seq=arm.command_seq,
            trial_uid=trial.trial_uid,
        )

        bundle, manifest, evaluation = write_evaluated_bundle(
            store,
            trial,
            root,
            candidate_index=index,
        )
        decision = coordinator.close_trial(
            manifest=manifest,
            evaluation=evaluation,
            safe_closure=manifest.safe_closure_evidence,
            bundle_path=bundle,
        )
        if not decision.ack_permitted:
            raise RuntimeError("safe evaluated bundle did not permit ACK")
        history_rows = [
            row
            for row in store.read_resume_history()
            if row.get("trial_uid") == trial.trial_uid
        ]
        if len(history_rows) != 1:
            raise RuntimeError("immutable bundle lacks one exact store history row")
        store_receipt = ImmutableBundleStoreReceipt(
            trial_uid=trial.trial_uid,
            bundle_path=bundle.resolve(),
            bundle_sha256=sha256(bundle.read_bytes()),
            history_identity=history_rows[0]["history_identity"],
        )
        batch_context.record_bundle(store_receipt)
        runner_event(
            "bundle_evaluated",
            bundle_path=str(bundle),
            candidate_index=index,
            disposition=evaluation.disposition.value,
            trial_uid=trial.trial_uid,
        )

        ack = coordinator.issue_ack(
            bundle,
            verified_resume_history=store.read_resume_history(),
        )
        ack_receipt = coordinator.dispatch(ack, prepared_trial=prepared, sink=mailbox)
        runner_event(
            "ack_dispatched",
            candidate_index=index,
            command_seq=ack.command_seq,
            mailbox_sha256=ack_receipt.mailbox_sha256,
            trial_uid=trial.trial_uid,
        )

        typed_state = "READY_HOME_CLOSED" if index == 10 else "READY_NEAR"
        _, typed_ready = wait_snapshot(
            snapshot_path,
            state=typed_state,
            consumed_command_seq=ack.command_seq,
        )
        readback = PostAckControllerReadback(
            batch_uid=batch_context.identity.batch_uid,
            row_index=index,
            trial_uid=trial.trial_uid,
            return_reference_uid=batch_context.reference.reference_uid,
            return_reference=batch_context.reference.kind,
            ack_command_seq=ack.command_seq,
            consumed_command_seq=typed_ready.consumed_command_seq,
            tp_state=typed_ready.state,
            batch_row_echo=index,
            return_kind_echo=batch_context.reference.kind.value,
            return_guard_mask=0x7F,
            position_error_m=0.001,
            orientation_error_rad=0.01,
            tcp_linear_speed_m_s=0.0005,
            tcp_angular_speed_rad_s=0.005,
            qd_max_rad_s=0.005,
            return_phase_echo=40.3,
            return_segment_id=3,
            return_current_angular_speed_rad_s=0.0,
            return_current_angular_acceleration_rad_s2=0.0,
            return_max_angular_speed_rad_s=0.05,
            return_max_angular_acceleration_rad_s2=0.1,
            return_max_sample_gap_s=0.002,
            dwell_s=0.5,
            safety_mode="NORMAL",
            safety_guards={
                name: True
                for name in (
                    "force",
                    "torque",
                    "joints",
                    "sensor_freshness",
                    "heartbeat",
                    "contact_loss",
                    "route_workspace",
                )
            },
            transcript_sha256=sha256(
                f"typed-return:{index}:{trial.trial_uid}:{ack.command_seq}".encode(
                    "ascii"
                )
            ),
        )
        admission = batch_context.complete_post_ack(
            trial=trial,
            arm_packet=arm,
            ack_packet=ack,
            store_receipt=store_receipt,
            manifest=manifest,
            evaluation=evaluation,
            readback=readback,
        )
        persisted = coordinator.reconcile(
            typed_ready,
            trial_brief_admission=admission,
        )
        if (
            persisted.decision.action is not ReconcileAction.PERSIST_POST_ACK
            or supervisor.phase is not CampaignPhase.HOME
        ):
            raise RuntimeError(
                f"exact ACK echo did not return durable HOME: {persisted.decision}"
            )
        runner_event(
            "typed_return_persisted",
            candidate_index=index,
            consumed_command_seq=typed_ready.consumed_command_seq,
            return_reference=batch_context.reference.kind.value,
            trial_brief_publication_uid=admission.publication_uid,
            trial_uid=trial.trial_uid,
        )

    batch_result = batch_context.journal.finalize()
    if batch_context.journal.verified_exit_code() != 0:
        raise RuntimeError("durable exact BatchResult did not authorize exit 0")
    runner_event(
        "campaign_complete",
        history_count=len(store.read_resume_history()),
        batch_uid=batch_context.identity.batch_uid,
        batch_result_uid=batch_result["batch_result_uid"],
        trial_uid=None,
    )
    atomic_json(
        done,
        {
            "pid": os.getpid(),
            "status": "complete",
            "batch_uid": batch_context.identity.batch_uid,
            "batch_result_uid": batch_result["batch_result_uid"],
            "verified_exit_code": 0,
        },
    )
    while not stop.exists():
        time.sleep(POLL_S)
    runner_event("process_stopped", role="runner")
    return 0


def run_watchdog(args: argparse.Namespace) -> int:
    events = Path(args.events).absolute()
    heartbeat = Path(args.heartbeat).absolute()
    ready = Path(args.ready).absolute()
    stop = Path(args.stop).absolute()
    monitored = {"runner": args.runner_pid, "bridge": args.bridge_pid}
    last_heartbeat: str | None = None
    append_event(events, "process_started", role="watchdog", monitored=monitored)
    atomic_json(ready, {"pid": os.getpid(), "role": "watchdog"})
    while not stop.exists():
        for role, pid in monitored.items():
            try:
                os.kill(pid, 0)
            except ProcessLookupError as exc:
                append_event(events, "child_exit", role=role, child_pid=pid)
                raise RuntimeError(f"{role} exited before watchdog stop") from exc
        if heartbeat.is_file():
            encoded = heartbeat.read_bytes()
            digest = hashlib.sha256(encoded).hexdigest()
            if digest != last_heartbeat:
                last_heartbeat = digest
                payload = json.loads(encoded)
                append_event(
                    events,
                    "heartbeat_observed",
                    heartbeat_event=payload["event"],
                    journal_revision=payload["journal_revision"],
                )
        time.sleep(POLL_S)
    append_event(events, "process_stopped", role="watchdog")
    return 0


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser()
    subparsers = result.add_subparsers(dest="role", required=True)

    bridge = subparsers.add_parser("bridge")
    bridge.add_argument("--root", required=True)
    bridge.add_argument("--snapshot", required=True)
    bridge.add_argument("--events", required=True)
    bridge.add_argument("--ready", required=True)
    bridge.add_argument("--stop", required=True)

    runner = subparsers.add_parser("runner")
    runner.add_argument("--root", required=True)
    runner.add_argument("--snapshot", required=True)
    runner.add_argument("--events", required=True)
    runner.add_argument("--heartbeat", required=True)
    runner.add_argument("--ready", required=True)
    runner.add_argument("--done", required=True)
    runner.add_argument("--stop", required=True)
    runner.add_argument("--start-gate", required=True)
    runner.add_argument("--control-fingerprint", required=True)

    watchdog = subparsers.add_parser("watchdog")
    watchdog.add_argument("--events", required=True)
    watchdog.add_argument("--heartbeat", required=True)
    watchdog.add_argument("--ready", required=True)
    watchdog.add_argument("--stop", required=True)
    watchdog.add_argument("--runner-pid", required=True, type=int)
    watchdog.add_argument("--bridge-pid", required=True, type=int)
    return result


def main() -> int:
    args = parser().parse_args()
    if args.role == "bridge":
        return run_bridge(args)
    if args.role == "runner":
        return run_runner(args)
    if args.role == "watchdog":
        return run_watchdog(args)
    raise RuntimeError(f"unsupported harness role: {args.role}")


if __name__ == "__main__":
    raise SystemExit(main())
