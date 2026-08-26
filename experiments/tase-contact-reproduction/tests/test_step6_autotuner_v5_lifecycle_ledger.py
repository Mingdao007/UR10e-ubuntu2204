from __future__ import annotations

import copy
from dataclasses import replace
import hashlib
import json
import math
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from step5d_autotune_v4_r013 import lifecycle_trace  # noqa: E402
from step5d_autotune_v4_r004.runtime import TimingGuard  # noqa: E402
from step6_figure8_autotune_v1.v5_composition_contract import (  # noqa: E402
    RolloverCommand,
    V5AttemptKind,
    V5TPState,
)
from step6_figure8_autotune_v1.v5_lifecycle_ledger import (  # noqa: E402
    ArtifactBindingV2,
    BoundaryMode,
    ContactRolloverBoundaryV2,
    ControllerCommitAckV2,
    FigureEightPhysicalRecordV2,
    GateClosureEvidenceV2,
    GateFamiliesV2,
    HomeBoundaryV2,
    LedgerRole,
    LifecycleEventKind,
    LifecycleEventV2,
    MetricSnapshotV1,
    OptimizerReceiptV2,
    PathTailClosureV2,
    TellState,
    TrialBoundaryReceiptV2,
    TrialSliceV2,
    V5AmbiguousTellError,
    V5_EVENT_BUNDLE_SCHEMA,
    V5_GATE_OBSERVATION_SCHEMA,
    V5_GATE_OBSERVATION_VERSION,
    V5LifecycleLedgerError,
    V5PhysicalAdmissionLedgerV2,
    bind_v5_chain,
    canonical_sha256,
    index_sealed_r013_artifact,
    lifecycle_row_evidence_sha256,
    persist_gate_observation_artifact,
)
from step6_figure8_autotune_v1.v5_rollover import CandidateIdentityV1, TAIL_END_S  # noqa: E402
from step6_figure8_autotune_v1.v5_capability_acceptance import _record_summary  # noqa: E402


FP = "a" * 64
RELEASE = "b" * 64
ENTRY = "1" * 64
HOME = "2" * 64


def _identity(ordinal: int) -> CandidateIdentityV1:
    return CandidateIdentityV1(7, ordinal, V5AttemptKind.PRIMARY_NOVEL, 100 + ordinal)


def _flags(
    *,
    terminal: bool = False,
    fresh: bool = True,
    command_present: bool = True,
) -> int:
    value = lifecycle_trace.FLAG_SENSOR_PRESENT | lifecycle_trace.FLAG_OUTPUT_PRESENT
    if command_present:
        value |= lifecycle_trace.FLAG_COMMAND_PRESENT
    if fresh:
        value |= lifecycle_trace.FLAG_SENSOR_FRESH
    if terminal:
        value |= lifecycle_trace.FLAG_TERMINAL
    return value


def _pack_row(
    index: int,
    mono: float,
    state: int,
    phase: int,
    flags: int,
    *,
    filtered: float,
    normal: float,
    force: float,
    torque: float,
    command_mode: int = 0,
    packet_sequence: int | None = None,
    consumed_packet_sequence: int | None = None,
) -> tuple:
    return (
        index,
        mono,
        mono,
        mono,
        state,
        phase,
        command_mode,
        flags,
        index if packet_sequence is None else packet_sequence,
        index if consumed_packet_sequence is None else consumed_packet_sequence,
        normal,
        force,
        filtered,
        torque,
        *(0.0 for _ in range(31)),
    )


def _runtime_timing_acceptance(rows: list[dict], indices: list[int]) -> dict:
    guard = TimingGuard()
    origin = float(rows[indices[0]]["monotonic_s"])
    for index in indices:
        guard.observe(float(rows[index]["monotonic_s"]) - origin)
    return guard.acceptance()


def _write_artifact(
    path: Path,
    *,
    attempt_count: int = 5,
    raw_delta: float = 0.0,
    filtered_delta: float = 0.0,
    tail_delta: float = 0.0,
    omit_bin: tuple[int, int] | None = None,
    stale_bin: tuple[int, int] | None = None,
    wrong_phase_bin: tuple[int, int] | None = None,
    home_state: int = 78,
    home_terminal: bool = True,
    event_clock_delta: float = 0.0,
    packet_mutations: dict[int, int] | None = None,
    identities: tuple[CandidateIdentityV1, ...] | None = None,
    gate_failure: tuple[int, str, str] | None = None,
    pending_echo_count: int = 1,
) -> tuple[dict, dict]:
    if type(pending_echo_count) is not int or pending_echo_count < 1:
        raise ValueError("fixture pending echo count is invalid")
    path.parent.mkdir(parents=True, exist_ok=True)
    metadata = {
        "schema": lifecycle_trace.LIFECYCLE_SCHEMA,
        "version": lifecycle_trace.LIFECYCLE_VERSION,
        "ordinal": 1,
        "execution_id": path.stem,
        "kind": "PRIMARY_NOVEL",
        "epoch": 7,
        "path_requested": True,
        "capture_started_before_motion": True,
        "record_size": lifecycle_trace.RECORD_SIZE,
        "started_monotonic_s": 0.0,
    }
    encoded = json.dumps(metadata, sort_keys=True, separators=(",", ":")).encode()
    rows: list[tuple] = []
    path_starts: list[float] = []
    path_start_indices: list[int] = []
    last_path_indices: list[int] = []
    seam_indices: list[int] = []
    seam_pending_indices: list[tuple[int, ...]] = []
    home_index = -1
    path_start = 1.0
    rows.append(
        _pack_row(
            0,
            0.994,
            11,
            11,
            _flags(command_present=False),
            filtered=1.0,
            normal=1.0,
            force=1.0,
            torque=0.01,
            command_mode=-1,
            packet_sequence=-1,
            consumed_packet_sequence=-1,
        )
    )
    for pre_index, (state, phase, mono) in enumerate(
        ((11, 11, 0.996), (20, 20, 0.998)),
        start=1,
    ):
        rows.append(_pack_row(pre_index, mono, state, phase, _flags(), filtered=1.0, normal=1.0, force=1.0, torque=0.01))
    resolved_identities = (
        tuple(_identity(ordinal) for ordinal in range(1, attempt_count + 1))
        if identities is None
        else tuple(identities)
    )
    if len(resolved_identities) != attempt_count:
        raise ValueError("fixture identity count differs")
    for ordinal in range(1, attempt_count + 1):
        path_starts.append(path_start)
        path_start_indices.append(len(rows))
        for bin_index in range(600):
            if omit_bin == (ordinal, bin_index):
                continue
            for tick in range(50):
                # The predecessor seam is the first sample of later attempts;
                # the next successor-owned PATH command starts one 500 Hz tick
                # later and still covers metric bin zero.
                if (
                    ordinal > 1
                    and bin_index == 0
                    and tick < pending_echo_count
                ):
                    continue
                state, phase = int(V5TPState.PATH), int(V5TPState.PATH)
                flags = _flags(fresh=stale_bin != (ordinal, bin_index))
                if wrong_phase_bin == (ordinal, bin_index):
                    phase = 0
                value = 5.0 + 0.001 * bin_index + filtered_delta
                rows.append(
                    _pack_row(
                        len(rows),
                        path_start + 0.1 * bin_index + 0.002 * tick,
                        state,
                        phase,
                        flags,
                        filtered=value,
                        normal=ordinal + bin_index + raw_delta,
                        force=2.0 + bin_index + raw_delta,
                        torque=0.02 * ordinal + raw_delta,
                        command_mode=2,
                    )
                )
        tail_tick = 0
        while 60.0 + 0.002 * tail_tick < TAIL_END_S - 1e-12:
            local_time = 60.0 + 0.002 * tail_tick
            state = (
                int(V5TPState.ROLLOVER_PREPARED)
                if local_time >= TAIL_END_S - 0.010
                else int(V5TPState.CLOSURE_TAIL)
            )
            rows.append(
                _pack_row(
                    len(rows),
                    path_start + local_time,
                    state,
                    0,
                    _flags(),
                    filtered=100.0 + tail_delta,
                    normal=100.0 + tail_delta,
                    force=100.0 + tail_delta,
                    torque=10.0 + tail_delta,
                    command_mode=2,
                )
            )
            tail_tick += 1
        if ordinal < attempt_count:
            seam_indices.append(len(rows))
            pending_indices: list[int] = []
            for pending_index in range(pending_echo_count):
                pending_indices.append(len(rows))
                rows.append(
                    _pack_row(
                        len(rows),
                        path_start
                        + TAIL_END_S
                        + 0.002 * pending_index,
                        int(V5TPState.ROLLOVER_COMMITTED),
                        0,
                        _flags(),
                        filtered=300.0 + tail_delta,
                        normal=300.0 + tail_delta,
                        force=300.0 + tail_delta,
                        torque=30.0 + tail_delta,
                        command_mode=2,
                    )
                )
            seam_pending_indices.append(tuple(pending_indices))
            last_path_indices.append(pending_indices[0])
            path_start += TAIL_END_S
        else:
            rows.append(
                _pack_row(
                    len(rows),
                    path_start + TAIL_END_S,
                    int(V5TPState.ROLLOVER_PREPARED),
                    0,
                    _flags(),
                    filtered=200.0 + tail_delta,
                    normal=200.0 + tail_delta,
                    force=200.0 + tail_delta,
                    torque=20.0 + tail_delta,
                    command_mode=2,
                )
            )
            last_path_indices.append(len(rows) - 1)
            rows.append(
                _pack_row(
                    len(rows),
                    path_start + TAIL_END_S + 0.002,
                    int(V5TPState.RETURNING_HOME),
                    int(V5TPState.RETURNING_HOME),
                    _flags(),
                    filtered=1.0,
                    normal=1.0,
                    force=1.0,
                    torque=0.01,
                    command_mode=0,
                )
            )
            home_index = len(rows)
            rows.append(_pack_row(len(rows), path_start + TAIL_END_S + 0.004, home_state, int(V5TPState.READY_HOME_NEXT) if home_state == int(V5TPState.READY_HOME_NEXT) else 0, _flags(terminal=home_terminal), filtered=400.0, normal=400.0, force=400.0, torque=40.0))
    if packet_mutations:
        resolved_packet_mutations = {}
        for key, value in packet_mutations.items():
            target = len(rows) + key if key < 0 else key
            resolved_packet_mutations[target] = target - 1 if value == -1 else value
        rows = [
            row[:8]
            + (
                resolved_packet_mutations.get(int(row[0]), row[8]),
                resolved_packet_mutations.get(int(row[0]), row[9]),
            )
            + row[10:]
            for row in rows
        ]
    with path.open("wb") as stream:
        stream.write(lifecycle_trace._HEADER.pack(lifecycle_trace.LIFECYCLE_MAGIC, lifecycle_trace.LIFECYCLE_VERSION, lifecycle_trace.RECORD_SIZE, len(encoded)))
        stream.write(encoded)
        for values in rows:
            stream.write(lifecycle_trace._RECORD.pack(*values))
        stream.flush()
    loaded_metadata, loaded_rows = lifecycle_trace.load_lifecycle_artifact(path)
    assert loaded_metadata == metadata
    assert len(loaded_rows) == len(rows)
    receipt = {
        "schema": lifecycle_trace.LIFECYCLE_RECEIPT_SCHEMA,
        "status": "complete",
        "artifact_path": str(path.resolve()),
        "artifact_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "artifact_row_count": len(rows),
        "artifact_sample_index_gap_count": 0,
        "packet_sequence_gap_count": 0,
        "packet_sequence_regression_count": 0,
        "coverage_complete": True,
        "home_verified": True,
        "path_requested": True,
        "path_coverage_complete": True,
        "capture_started_before_motion": True,
        "terminal_state": 78,
        "terminal_sensor_present": True,
        "dropped_chunks": 0,
        "write_errors": 0,
        "invalid_rows": 0,
        "observer_errors": 0,
        "missing_force_rows": 0,
        "missing_pose_rows": 0,
        "gc_window": {
            "schema": "step6.autotune/figure8-v5-gc-window-receipt-v1",
            "version": 1,
            "scope": "ARM_EXECUTE_MOTION_CONTACT_SAFE_RETURN_HOME",
            "pre_enabled": True,
            "entered": True,
            "restored": True,
            "post_enabled": True,
            "restored_after_timing_lease": True,
        },
        "errors": [],
    }
    events: list[LifecycleEventV2] = [
        LifecycleEventV2(
            LifecycleEventKind.CHAIN_START,
            3,
            loaded_rows[3]["monotonic_s"],
            resolved_identities[0],
            rtde_timestamp_s=loaded_rows[3]["rtde_timestamp_s"],
            event_evidence_sha256=ENTRY,
            referenced_row_sha256=lifecycle_row_evidence_sha256(loaded_rows[3]),
        )
    ]
    switch_gate_receipts: list[dict[str, object]] = []
    for ordinal, seam_index in enumerate(seam_indices, 1):
        seam = loaded_rows[seam_index]
        switch_receipt = {
            "schema": "step6.autotune/figure8-v5-switch-gate-receipt-v1",
            "version": 1,
            "old_identity": resolved_identities[ordinal - 1].as_dict(),
            "next_identity": resolved_identities[ordinal].as_dict(),
            "generation": ordinal,
            "qdot_generation": ordinal,
            "tail_endpoint_s": TAIL_END_S,
            "controller_overlay": {29: 28, 30: ordinal, 31: ordinal},
            "commit_seed_qdot": [0.0] * 6,
            "gate_receipt": {
                "guard_stack": {"terminal_stop": False},
                "qdot": {"allowed": True},
            },
            "narrow_force_windows_blocking": False,
        }
        switch_gate_receipts.append(switch_receipt)
        events.append(
            LifecycleEventV2(
                LifecycleEventKind.CONTACT_ROLLOVER,
                seam_index,
                seam["monotonic_s"] + event_clock_delta,
                resolved_identities[ordinal - 1],
                rtde_timestamp_s=seam["rtde_timestamp_s"],
                next_identity=resolved_identities[ordinal],
                generation=ordinal,
                qdot_generation=ordinal,
                event_evidence_sha256=canonical_sha256(switch_receipt),
                referenced_row_sha256=lifecycle_row_evidence_sha256(seam),
            )
        )
    home = loaded_rows[home_index]
    events.append(
        LifecycleEventV2(
            LifecycleEventKind.HOME,
            home_index,
            home["monotonic_s"],
            resolved_identities[-1],
            rtde_timestamp_s=home["rtde_timestamp_s"],
            terminal_state=V5TPState.READY_HOME_NEXT,
            event_evidence_sha256=HOME,
            referenced_row_sha256=lifecycle_row_evidence_sha256(home),
        )
    )
    event_mappings = [event.as_dict() for event in events]
    default_gate_receipt = {
        "guard_stack": {"terminal_stop": False},
        "qdot": {"allowed": True},
    }
    observations: list[dict[str, object]] = []
    timing_sample_indices = [
        index
        for index, row in enumerate(loaded_rows)
        if int(row["flags"]) & lifecycle_trace.FLAG_COMMAND_PRESENT
        and not (int(row["flags"]) & lifecycle_trace.FLAG_TERMINAL)
    ]
    gate_sample_indices = [
        index
        for index, row in enumerate(loaded_rows)
        if int(row["flags"]) & lifecycle_trace.FLAG_COMMAND_PRESENT
        and row["command_mode"] == 2
    ]
    failure_applied = False
    pending_generation_by_sample = {
        sample_index: generation
        for generation, pending_indices in enumerate(
            seam_pending_indices,
            start=1,
        )
        for sample_index in pending_indices
    }
    for sample_index in gate_sample_indices:
        source_row = loaded_rows[sample_index]
        attempt_index = sum(seam < sample_index for seam in seam_indices)
        pending_generation = pending_generation_by_sample.get(sample_index)
        seam_index = (
            seam_indices[attempt_index]
            if attempt_index < len(seam_indices)
            and seam_indices[attempt_index] == sample_index
            else None
        )
        local_time = (
            TAIL_END_S
            if seam_index is not None
            else float(source_row["rtde_timestamp_s"]) - path_starts[attempt_index]
        )
        phase = "path" if local_time < 60.0 else "tail"
        expected = (
            resolved_identities[attempt_index + 1]
            if seam_index is not None
            else resolved_identities[attempt_index]
        )
        host_attempt_index = (
            pending_generation - 1
            if pending_generation is not None
            else attempt_index
        )
        gate_receipt = (
            switch_gate_receipts[attempt_index]["gate_receipt"]
            if seam_index is not None
            else default_gate_receipt
        )
        safety_normal = True
        guard_terminal_stop = False
        command_envelope_allowed = True
        controller_candidate_token = expected.candidate_token
        if not failure_applied and gate_failure == (
            attempt_index + 1,
            phase,
            "safety",
        ):
            safety_normal = False
            failure_applied = True
        elif not failure_applied and gate_failure == (
            attempt_index + 1,
            phase,
            "tube_cbf",
        ):
            guard_terminal_stop = True
            failure_applied = True
        elif not failure_applied and gate_failure == (
            attempt_index + 1,
            phase,
            "identity",
        ):
            controller_candidate_token += 1
            failure_applied = True
        elif not failure_applied and gate_failure == (
            attempt_index + 1,
            phase,
            "command_envelope",
        ):
            command_envelope_allowed = False
            failure_applied = True
        observations.append(
            {
                "sample_index": sample_index,
                "qdot": list(source_row["qdot"]),
                "gate_receipt_sha256": canonical_sha256(dict(gate_receipt)),
                "safety_normal": safety_normal,
                "stop_request": False,
                "controller_epoch": expected.epoch,
                "controller_ordinal": expected.ordinal,
                "controller_candidate_token": controller_candidate_token,
                "guard_terminal_stop": guard_terminal_stop,
                "guard_soft_fail_closed": False,
                "command_envelope_allowed": command_envelope_allowed,
                "host_epoch": resolved_identities[host_attempt_index].epoch,
                "host_ordinal": resolved_identities[host_attempt_index].ordinal,
                "host_candidate_token": resolved_identities[
                    host_attempt_index
                ].candidate_token,
                "rollover_command": (
                    int(RolloverCommand.COMMIT)
                    if pending_generation is not None
                    else int(RolloverCommand.NONE)
                ),
                "rollover_generation": (
                    pending_generation
                    if pending_generation is not None
                    else 0
                ),
                "activation_pending": pending_generation is not None,
                "activation_durable": pending_generation is None,
            }
        )
    gate_observation_binding = persist_gate_observation_artifact(
        path.with_name(path.name + ".gate-observations.json"),
        source_artifact_path=path,
        observations=observations,
        timing_sample_indices=timing_sample_indices,
        control_start_sample_index=timing_sample_indices[0],
        control_end_sample_index=timing_sample_indices[-1],
        runtime_timing_acceptance=_runtime_timing_acceptance(
            loaded_rows, timing_sample_indices
        ),
    )
    bundle = {
        "schema": V5_EVENT_BUNDLE_SCHEMA,
        "version": 2,
        "cold_verified": True,
        "events_sha256": canonical_sha256(event_mappings),
        "entry_evidence_sha256": ENTRY,
        "events": event_mappings,
        "switch_gate_receipts": switch_gate_receipts,
        "gate_observation_artifact": gate_observation_binding,
    }
    return receipt, bundle


def _artifact(tmp_path: Path, *, attempt_count: int = 5, **kwargs):
    path = tmp_path / "chain.r013life"
    receipt, bundle = _write_artifact(path, attempt_count=attempt_count, **kwargs)
    return index_sealed_r013_artifact(path, receipt, bundle)


def _rehash_gate_sidecar(bundle: dict, mutate) -> dict:
    forged = copy.deepcopy(bundle)
    binding = forged["gate_observation_artifact"]
    path = Path(binding["artifact_path"])
    body = json.loads(path.read_text(encoding="utf-8"))
    mutate(body)
    previous = "0" * 64
    for sequence_index, values in enumerate(body["observations"]):
        compact = dict(
            zip(body["observation_columns"], values, strict=True)
        )
        row_sha256 = canonical_sha256(
            {
                "schema": V5_GATE_OBSERVATION_SCHEMA,
                "version": V5_GATE_OBSERVATION_VERSION,
                "sequence_index": sequence_index,
                **compact,
            }
        )
        previous = canonical_sha256(
            {
                "previous_sha256": previous,
                "observation_row_sha256": row_sha256,
            }
        )
    body["timing_count"] = len(body["timing_rows"])
    body["observation_count"] = len(body["observations"])
    body["observation_head_sha256"] = previous
    payload = (
        json.dumps(
            body,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
        + b"\n"
    )
    path.write_bytes(payload)
    binding["artifact_sha256"] = hashlib.sha256(payload).hexdigest()
    binding["artifact_size"] = len(payload)
    binding["timing_count"] = len(body["timing_rows"])
    binding["observation_count"] = len(body["observations"])
    binding["observation_head_sha256"] = previous
    return forged


def _gate_closure(
    artifact,
    attempt: TrialSliceV2,
) -> PathTailClosureV2:
    evidence = GateClosureEvidenceV2.from_artifact(artifact, attempt)
    return PathTailClosureV2(evidence.path, evidence.tail, evidence)


def _chain(tmp_path: Path, *, attempt_count: int = 5, prefix: str = "chain", **artifact_kwargs):
    path = tmp_path / f"{prefix}.r013life"
    receipt, bundle = _write_artifact(path, attempt_count=attempt_count, **artifact_kwargs)
    artifact = index_sealed_r013_artifact(path, receipt, bundle)
    starts = [3]
    seam_indices = [event.sample_index for event in artifact.events if event.kind is LifecycleEventKind.CONTACT_ROLLOVER]
    starts.extend(seam_indices)
    home_index = next(event.sample_index for event in artifact.events if event.kind is LifecycleEventKind.HOME)
    attempts = []
    for ordinal in range(1, attempt_count + 1):
        start = starts[ordinal - 1]
        end = seam_indices[ordinal - 1] if ordinal < attempt_count else home_index + 1
        clock_start = artifact.rows[start]["monotonic_s"]
        clock_end = artifact.rows[end - 1]["monotonic_s"] if ordinal == attempt_count else artifact.rows[end]["monotonic_s"]
        identity = _identity(ordinal)
        metric = MetricSnapshotV1.from_artifact_slice(artifact, candidate_identity=identity, sample_start_index=start, sample_end_index=end, path_clock_start_s=clock_start)
        if ordinal < attempt_count:
            seam = artifact.rows[end]
            ack = ControllerCommitAckV2(_identity(ordinal + 1), ordinal, ordinal, end, seam["monotonic_s"], seam["rtde_timestamp_s"])
            boundary = TrialBoundaryReceiptV2(ContactRolloverBoundaryV2(identity, _identity(ordinal + 1), ordinal, ordinal, TAIL_END_S, artifact.events[ordinal].event_evidence_sha256, ack, end, seam["monotonic_s"], seam["rtde_timestamp_s"]))
        else:
            home = artifact.rows[end - 1]
            boundary = TrialBoundaryReceiptV2(HomeBoundaryV2(V5TPState.READY_HOME_NEXT, end - 1, home["monotonic_s"], HOME, home["rtde_timestamp_s"]))
        attempts.append(TrialSliceV2(f"{prefix}-attempt-{ordinal}", f"{prefix}-trial-{ordinal}", identity, start, end, clock_start, clock_end, metric, boundary))
    chain = bind_v5_chain(artifact, attempts, campaign_fingerprint=FP, release_identity_sha256=RELEASE, role=LedgerRole.PRIMARY, chain_id=f"{prefix}-chain")
    records = [
        FigureEightPhysicalRecordV2.from_attempt(
            chain,
            attempt,
            _gate_closure(artifact, attempt),
        )
        for attempt in chain.attempts
    ]
    return artifact, chain, records


def test_five_attempt_chain_uses_real_half_open_seams_and_home(tmp_path: Path):
    artifact, chain, records = _chain(tmp_path)
    assert len(chain.attempts) == len(records) == 5
    assert chain.attempts[0].sample_start_index == 3
    assert [attempt.sample_start_index for attempt in chain.attempts[1:]] == [attempt.boundary.boundary.commit_sample_index for attempt in chain.attempts[:-1]]
    assert [attempt.sample_end_index for attempt in chain.attempts[:-1]] == [attempt.boundary.boundary.commit_sample_index for attempt in chain.attempts[:-1]]
    assert all(attempt.boundary.mode is BoundaryMode.CONTACT_ROLLOVER for attempt in chain.attempts[:-1])
    assert all(not attempt.boundary.safe_return and not attempt.boundary.return_gate for attempt in chain.attempts[:-1])
    assert chain.attempts[-1].boundary.mode is BoundaryMode.HOME
    assert chain.attempts[-1].boundary.boundary.sample_index == len(artifact.rows) - 1
    assert chain.attempts[-1].boundary.safe_return and chain.attempts[-1].boundary.return_gate
    assert all(record.eligible for record in records)


def test_metric_is_derived_with_exact_bins_and_formal_relabel(tmp_path: Path):
    artifact, chain, records = _chain(tmp_path, attempt_count=1)
    snapshot = records[0].metric_snapshot
    assert len(snapshot.evidence_bins) == 600 and len(snapshot.formal_bins) == 550
    assert snapshot.formal_bins[0].filtered_normal_n == snapshot.evidence_bins[50].filtered_normal_n
    expected = math.fsum(abs(5.0 + 0.001 * index - 5.0) for index in range(50, 600)) / 550
    assert snapshot.formal_mae_n == pytest.approx(expected)
    expected_full = math.fsum(abs(item.filtered_normal_n - 5.0) for item in snapshot.evidence_bins) / 600
    assert snapshot.primary_mae_n == pytest.approx(expected_full)
    assert not hasattr(MetricSnapshotV1, "from_binned_samples")
    assert snapshot.evidence_bins[0].raw_signed_normal_n == pytest.approx(1.0)


def test_capability_summary_consumes_a_real_cold_record(tmp_path: Path):
    _, _, records = _chain(tmp_path, attempt_count=1)
    summary = _record_summary(records[0])
    assert summary["trial_id"] == records[0].trial_id
    assert summary["gate_families"] == {
        "path": records[0].closure.path.as_dict(),
        "tail": records[0].closure.tail.as_dict(),
    }
    assert (
        summary["gate_closure_evidence_sha256"]
        == records[0].closure.evidence.evidence_sha256
    )


def test_tail_and_raw_channels_are_derived_but_not_metric_gate(tmp_path: Path):
    artifact_a, chain_a, records_a = _chain(tmp_path / "a", attempt_count=1)
    artifact_b, chain_b, records_b = _chain(tmp_path / "b", attempt_count=1, tail_delta=999.0)
    assert artifact_a.artifact_sha256 != artifact_b.artifact_sha256
    assert records_a[0].metric_snapshot.content_hash == records_b[0].metric_snapshot.content_hash
    artifact_c, chain_c, records_c = _chain(tmp_path / "c", attempt_count=1, raw_delta=77.0)
    assert records_c[0].metric_snapshot.formal_mae_n == records_a[0].metric_snapshot.formal_mae_n
    assert records_c[0].metric_snapshot.metric_rows_sha256 != records_a[0].metric_snapshot.metric_rows_sha256
    assert records_c[0].metric_snapshot.content_hash != records_a[0].metric_snapshot.content_hash
    telemetry = {"window_4_6_n": -100.0, "window_3_7_n": 100.0, "force_7_n": 0.0, "torque_0_30_nm": 100.0}
    same = FigureEightPhysicalRecordV2.from_attempt(chain_a, chain_a.attempts[0], records_a[0].closure, optional_telemetry=telemetry)
    _, failed_chain, failed_records = _chain(
        tmp_path / "failed-gate",
        attempt_count=1,
        gate_failure=(1, "path", "safety"),
    )
    failed = FigureEightPhysicalRecordV2.from_attempt(
        failed_chain,
        failed_chain.attempts[0],
        failed_records[0].closure,
        optional_telemetry=telemetry,
    )
    assert same.eligible and not failed.eligible and failed.tell_token() is None


def test_all_true_closure_without_cold_evidence_is_not_admissible(tmp_path: Path):
    artifact, chain, records = _chain(tmp_path, attempt_count=1)
    gates = GateFamiliesV2(True, True, True, True, True, True)
    forged = FigureEightPhysicalRecordV2.from_attempt(
        chain,
        chain.attempts[0],
        PathTailClosureV2(gates, gates),
    )
    assert records[0].eligible
    assert not forged.eligible
    assert "gate_closure_evidence_missing" in forged.admission_reasons
    assert forged.tell_token() is None


def test_arbitrary_switch_hash_without_matching_typed_receipt_fails_cold(tmp_path: Path):
    path = tmp_path / "arbitrary-switch.r013life"
    receipt, bundle = _write_artifact(path, attempt_count=2)
    forged_bundle = copy.deepcopy(bundle)
    rollover = next(
        item
        for item in forged_bundle["events"]
        if item["kind"] == LifecycleEventKind.CONTACT_ROLLOVER.value
    )
    rollover["event_evidence_sha256"] = "f" * 64
    forged_bundle["events_sha256"] = canonical_sha256(forged_bundle["events"])
    with pytest.raises(V5LifecycleLedgerError, match="switch gate receipt"):
        index_sealed_r013_artifact(path, receipt, forged_bundle)


def test_removed_failed_observation_still_fails_after_full_sidecar_rehash(
    tmp_path: Path,
):
    path = tmp_path / "removed-failure.r013life"
    receipt, bundle = _write_artifact(
        path,
        attempt_count=1,
        gate_failure=(1, "path", "safety"),
    )

    def remove_failed(body: dict) -> None:
        safety_column = body["observation_columns"].index("safety_normal")
        failed_index = next(
            index
            for index, values in enumerate(body["observations"])
            if values[safety_column] is False
        )
        del body["observations"][failed_index]
        del body["timing_rows"][failed_index]

    forged = _rehash_gate_sidecar(bundle, remove_failed)
    with pytest.raises(
        V5LifecycleLedgerError,
        match="exactly cover|count differs|cold-derived",
    ):
        index_sealed_r013_artifact(path, receipt, forged)


def test_rewritten_timing_dt_fails_after_sidecar_rehash(tmp_path: Path):
    path = tmp_path / "rewritten-timing.r013life"
    receipt, bundle = _write_artifact(path, attempt_count=1)

    def rewrite_dt(body: dict) -> None:
        body["timing_rows"][100][3] = 0.003

    forged = _rehash_gate_sidecar(bundle, rewrite_dt)
    with pytest.raises(V5LifecycleLedgerError, match="cold-derived"):
        index_sealed_r013_artifact(path, receipt, forged)


@pytest.mark.parametrize(
    ("row_index", "boundary"),
    ((0, "CONTROL_START"), (-1, "CONTROL_END")),
)
def test_truncated_timing_boundary_fails_after_sidecar_rehash(
    tmp_path: Path,
    row_index: int,
    boundary: str,
):
    path = tmp_path / f"truncated-{boundary.lower()}.r013life"
    receipt, bundle = _write_artifact(path, attempt_count=1)
    _metadata, source_rows = lifecycle_trace.load_lifecycle_artifact(path)
    surviving_boundary: list[int] = []

    def truncate(body: dict) -> None:
        body["timing_rows"].pop(row_index)
        if row_index == 0:
            body["timing_rows"][0][2] = None
            body["timing_rows"][0][3] = 0.002
        timing_samples = [int(row[0]) for row in body["timing_rows"]]
        body["runtime_timing_acceptance"] = _runtime_timing_acceptance(
            source_rows,
            timing_samples,
        )
        surviving_boundary.append(
            timing_samples[0] if row_index == 0 else timing_samples[-1]
        )

    forged = _rehash_gate_sidecar(bundle, truncate)
    binding_key = (
        "timing_start_sample_index"
        if row_index == 0
        else "timing_end_sample_index"
    )
    forged["gate_observation_artifact"][binding_key] = surviving_boundary[0]
    with pytest.raises(V5LifecycleLedgerError, match=boundary):
        index_sealed_r013_artifact(path, receipt, forged)


def test_fabricated_switch_commit_seed_fails_with_rehashed_event_bundle(
    tmp_path: Path,
):
    path = tmp_path / "fabricated-switch.r013life"
    receipt, bundle = _write_artifact(path, attempt_count=2)
    forged = copy.deepcopy(bundle)
    switch = forged["switch_gate_receipts"][0]
    switch["commit_seed_qdot"][0] = 0.01
    rollover = next(
        event
        for event in forged["events"]
        if event["kind"] == LifecycleEventKind.CONTACT_ROLLOVER.value
    )
    rollover["event_evidence_sha256"] = canonical_sha256(switch)
    forged["events_sha256"] = canonical_sha256(forged["events"])
    with pytest.raises(
        V5LifecycleLedgerError,
        match="switch gate observation|activation-pending authority",
    ):
        index_sealed_r013_artifact(path, receipt, forged)


def test_successor_none_moved_before_durability_fails_after_full_rehash(
    tmp_path: Path,
) -> None:
    path = tmp_path / "early-successor-none.r013life"
    receipt, bundle = _write_artifact(path, attempt_count=2)
    next_identity = _identity(2)

    def publish_early(body: dict) -> None:
        columns = body["observation_columns"]
        pending_column = columns.index("activation_pending")
        durable_column = columns.index("activation_durable")
        command_column = columns.index("rollover_command")
        generation_column = columns.index("rollover_generation")
        host_epoch_column = columns.index("host_epoch")
        host_ordinal_column = columns.index("host_ordinal")
        host_token_column = columns.index("host_candidate_token")
        seam = next(
            values
            for values in body["observations"]
            if values[pending_column] is True
        )
        seam[pending_column] = False
        seam[durable_column] = True
        seam[command_column] = int(RolloverCommand.NONE)
        seam[generation_column] = 0
        seam[host_epoch_column] = next_identity.epoch
        seam[host_ordinal_column] = next_identity.ordinal
        seam[host_token_column] = next_identity.candidate_token

    forged = _rehash_gate_sidecar(bundle, publish_early)
    with pytest.raises(
        V5LifecycleLedgerError,
        match="activation-pending interval is empty",
    ):
        index_sealed_r013_artifact(path, receipt, forged)


def test_activation_pending_cannot_reenter_after_early_successor_full_rehash(
    tmp_path: Path,
) -> None:
    path = tmp_path / "pending-reentry.r013life"
    receipt, bundle = _write_artifact(
        path,
        attempt_count=2,
        pending_echo_count=3,
    )
    index_sealed_r013_artifact(path, receipt, bundle)
    next_identity = _identity(2)

    def split_pending_interval(body: dict) -> None:
        columns = body["observation_columns"]
        pending_column = columns.index("activation_pending")
        durable_column = columns.index("activation_durable")
        command_column = columns.index("rollover_command")
        generation_column = columns.index("rollover_generation")
        host_epoch_column = columns.index("host_epoch")
        host_ordinal_column = columns.index("host_ordinal")
        host_token_column = columns.index("host_candidate_token")
        pending_rows = [
            values
            for values in body["observations"]
            if values[pending_column] is True
        ]
        assert len(pending_rows) == 3
        early_successor = pending_rows[1]
        early_successor[pending_column] = False
        early_successor[durable_column] = True
        early_successor[command_column] = int(RolloverCommand.NONE)
        early_successor[generation_column] = 0
        early_successor[host_epoch_column] = next_identity.epoch
        early_successor[host_ordinal_column] = next_identity.ordinal
        early_successor[host_token_column] = next_identity.candidate_token

    forged = _rehash_gate_sidecar(bundle, split_pending_interval)
    with pytest.raises(
        V5LifecycleLedgerError,
        match="activation-pending interval re-entry",
    ):
        index_sealed_r013_artifact(path, receipt, forged)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"omit_bin": (1, 123)},
        {"stale_bin": (1, 123)},
        {"wrong_phase_bin": (1, 123)},
    ],
)
def test_metric_gap_sensor_and_real_phase_falsifiers_fail_closed(tmp_path: Path, kwargs):
    artifact = _artifact(tmp_path, attempt_count=1, **kwargs)
    with pytest.raises(V5LifecycleLedgerError):
        MetricSnapshotV1.from_artifact_slice(artifact, candidate_identity=_identity(1), sample_start_index=3, sample_end_index=len(artifact.rows), path_clock_start_s=artifact.rows[3]["monotonic_s"])


def test_event_row_binding_receipt_path_home_and_forged_snapshot_fail(tmp_path: Path):
    artifact, chain, records = _chain(tmp_path / "good", attempt_count=2)
    fake_bin = replace(records[0].metric_snapshot.evidence_bins[0], filtered_normal_n=99.0)
    forged = replace(records[0].metric_snapshot, evidence_bins=(fake_bin,) + records[0].metric_snapshot.evidence_bins[1:])
    forged_attempt = replace(chain.attempts[0], metric_snapshot=forged)
    with pytest.raises(V5LifecycleLedgerError):
        bind_v5_chain(artifact, (forged_attempt, *chain.attempts[1:]), campaign_fingerprint=FP, release_identity_sha256=RELEASE, role=LedgerRole.PRIMARY, chain_id="forged")
    receipt, bundle = _write_artifact(tmp_path / "mismatch" / "x.r013life", attempt_count=2, event_clock_delta=0.25)
    altered = index_sealed_r013_artifact(tmp_path / "mismatch" / "x.r013life", receipt, bundle)
    with pytest.raises(V5LifecycleLedgerError):
        bind_v5_chain(altered, chain.attempts, campaign_fingerprint=FP, release_identity_sha256=RELEASE, role=LedgerRole.PRIMARY, chain_id="clock")
    wrong_path = dict(artifact.receipt)
    wrong_path["artifact_path"] = str(tmp_path / "not-the-artifact.r013life")
    with pytest.raises(V5LifecycleLedgerError):
        index_sealed_r013_artifact(Path(artifact.artifact_path), wrong_path, artifact.event_bundle)
    with pytest.raises(V5LifecycleLedgerError):
        _artifact(tmp_path / "bad-home", attempt_count=1, home_state=25)


def test_receipt_has_no_invented_v1_artifact_size_and_seam_states_are_real(tmp_path: Path):
    artifact, _, _ = _chain(tmp_path, attempt_count=2)
    assert "artifact_size" not in artifact.receipt
    assert all(event.referenced_row_sha256 != "0" * 64 for event in artifact.events)
    assert all(event.event_evidence_sha256 != event.referenced_row_sha256 for event in artifact.events)
    for event in artifact.events:
        row = artifact.rows[event.sample_index]
        if event.kind is LifecycleEventKind.CONTACT_ROLLOVER:
            assert row["tp_state"] == 28 and row["phase_code"] == 0
        if event.kind is LifecycleEventKind.HOME:
            assert row["tp_state"] == 78 and row["phase_code"] == 78


def test_packet_sequence_duplicate_is_allowed_but_regression_and_gap_are_recomputed(tmp_path: Path):
    # -2 is the final non-terminal PATH row; repeating its predecessor does
    # not manufacture a subsequent two-sequence jump.
    duplicate = _artifact(tmp_path / "duplicate", attempt_count=1, packet_mutations={-2: -1})
    assert duplicate.receipt["packet_sequence_gap_count"] == 0
    with pytest.raises(V5LifecycleLedgerError):
        _artifact(tmp_path / "regression", attempt_count=1, packet_mutations={603: 601})
    with pytest.raises(V5LifecycleLedgerError):
        _artifact(tmp_path / "gap", attempt_count=1, packet_mutations={603: 604})


def test_rollover_rtde_must_match_row_event_boundary_and_ack(tmp_path: Path):
    artifact, chain, _ = _chain(tmp_path, attempt_count=2)
    attempt = chain.attempts[0]
    proof = attempt.boundary.boundary
    changed_rtde = proof.commit_rtde_timestamp_s + 0.5
    changed_ack = replace(proof.controller_commit_ack, rtde_timestamp_s=changed_rtde)
    changed_boundary = TrialBoundaryReceiptV2(replace(proof, controller_commit_ack=changed_ack, commit_rtde_timestamp_s=changed_rtde))
    changed_attempt = replace(attempt, boundary=changed_boundary)
    with pytest.raises(V5LifecycleLedgerError):
        bind_v5_chain(artifact, (changed_attempt, chain.attempts[1]), campaign_fingerprint=FP, release_identity_sha256=RELEASE, role=LedgerRole.PRIMARY, chain_id="rtde-mismatch")


def test_out_of_range_rollover_sample_is_typed_failure(tmp_path: Path):
    artifact, chain, _ = _chain(tmp_path, attempt_count=2)
    attempt = chain.attempts[0]
    proof = attempt.boundary.boundary
    out_of_range = len(artifact.rows) + 1
    changed_ack = replace(proof.controller_commit_ack, sample_index=out_of_range)
    changed_boundary = TrialBoundaryReceiptV2(replace(proof, controller_commit_ack=changed_ack, commit_sample_index=out_of_range))
    changed_attempt = replace(attempt, sample_end_index=out_of_range, boundary=changed_boundary)
    with pytest.raises(V5LifecycleLedgerError):
        bind_v5_chain(artifact, (changed_attempt, chain.attempts[1]), campaign_fingerprint=FP, release_identity_sha256=RELEASE, role=LedgerRole.PRIMARY, chain_id="bounds")


def test_ledger_spans_two_artifacts_and_cold_reopens_each_binding(tmp_path: Path):
    _, _, first = _chain(tmp_path / "first", attempt_count=1, prefix="first")
    _, _, second = _chain(tmp_path / "second", attempt_count=1, prefix="second", raw_delta=10.0)
    path = tmp_path / "ledger.jsonl"
    ledger = V5PhysicalAdmissionLedgerV2(path, campaign_fingerprint=FP, release_identity_sha256=RELEASE, role=LedgerRole.PRIMARY)
    ledger.append_record(first[0])
    ledger.append_record(second[0])
    restarted = V5PhysicalAdmissionLedgerV2(path, campaign_fingerprint=FP, release_identity_sha256=RELEASE, role=LedgerRole.PRIMARY)
    assert restarted.fresh_process_verify().record_count == 2
    tampered = json.loads(path.read_text(encoding="utf-8").splitlines()[1])
    tampered["record"]["metric_snapshot"]["evidence_bins"][0]["filtered_normal_n"] = 999.0
    lines = path.read_text(encoding="utf-8").splitlines()
    lines[1] = json.dumps(tampered, sort_keys=True, separators=(",", ":"))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    with pytest.raises(V5LifecycleLedgerError):
        V5PhysicalAdmissionLedgerV2(path, campaign_fingerprint=FP, release_identity_sha256=RELEASE, role=LedgerRole.PRIMARY)
    role_path = tmp_path / "role.jsonl"
    V5PhysicalAdmissionLedgerV2(role_path, campaign_fingerprint=FP, release_identity_sha256=RELEASE, role=LedgerRole.PRIMARY)
    with pytest.raises(V5LifecycleLedgerError):
        V5PhysicalAdmissionLedgerV2(role_path, campaign_fingerprint=FP, release_identity_sha256=RELEASE, role=LedgerRole.CORRECTION)
    cross_fp = tmp_path / "cross-fingerprint.jsonl"
    V5PhysicalAdmissionLedgerV2(cross_fp, campaign_fingerprint=FP, release_identity_sha256=RELEASE, role=LedgerRole.PRIMARY)
    with pytest.raises(V5LifecycleLedgerError):
        V5PhysicalAdmissionLedgerV2(cross_fp, campaign_fingerprint="e" * 64, release_identity_sha256=RELEASE, role=LedgerRole.PRIMARY)
    v1_path = tmp_path / "v1.jsonl"
    v1_path.write_text(json.dumps({"schema": "step6.autotune/figure8-physical-ledger-v1"}) + "\n", encoding="utf-8")
    with pytest.raises(V5LifecycleLedgerError):
        V5PhysicalAdmissionLedgerV2(v1_path, campaign_fingerprint=FP, release_identity_sha256=RELEASE, role=LedgerRole.PRIMARY)


@pytest.mark.parametrize("field, value", [("schema", "wrong-artifact-binding"), ("version", 999)])
def test_recomputed_outer_hash_cannot_hide_nested_binding_schema(tmp_path: Path, field: str, value):
    _, _, records = _chain(tmp_path / "schema", attempt_count=1)
    path = tmp_path / f"schema-{field}.jsonl"
    ledger = V5PhysicalAdmissionLedgerV2(path, campaign_fingerprint=FP, release_identity_sha256=RELEASE, role=LedgerRole.PRIMARY)
    ledger.append_record(records[0])
    binding = records[0].artifact_binding.as_dict()
    binding[field] = value
    with pytest.raises(V5LifecycleLedgerError):
        ArtifactBindingV2.from_mapping(binding)
    lines = path.read_text(encoding="utf-8").splitlines()
    row = json.loads(lines[1])
    row["record"]["artifact_binding"][field] = value
    row["row_sha256"] = V5PhysicalAdmissionLedgerV2._row_hash(row)
    lines[1] = json.dumps(row, sort_keys=True, separators=(",", ":"))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    with pytest.raises(V5LifecycleLedgerError):
        V5PhysicalAdmissionLedgerV2(path, campaign_fingerprint=FP, release_identity_sha256=RELEASE, role=LedgerRole.PRIMARY)


def test_record_mapping_schema_is_not_replaced_by_defaults(tmp_path: Path):
    _, _, records = _chain(tmp_path / "record-schema", attempt_count=1)
    path = tmp_path / "record-schema.jsonl"
    ledger = V5PhysicalAdmissionLedgerV2(path, campaign_fingerprint=FP, release_identity_sha256=RELEASE, role=LedgerRole.PRIMARY)
    ledger.append_record(records[0])
    lines = path.read_text(encoding="utf-8").splitlines()
    row = json.loads(lines[1])
    row["record"]["schema"] = "wrong-record-schema"
    row["row_sha256"] = V5PhysicalAdmissionLedgerV2._row_hash(row)
    lines[1] = json.dumps(row, sort_keys=True, separators=(",", ":"))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    with pytest.raises(V5LifecycleLedgerError):
        V5PhysicalAdmissionLedgerV2(path, campaign_fingerprint=FP, release_identity_sha256=RELEASE, role=LedgerRole.PRIMARY)


def test_forged_in_memory_artifact_cannot_poison_ledger(tmp_path: Path):
    artifact, chain, records = _chain(tmp_path / "forged", attempt_count=1)
    forged_rows = list(artifact.rows)
    altered_row = dict(forged_rows[3])
    altered_row["filtered_normal_n"] = 777.0
    forged_rows[3] = altered_row
    forged_artifact = replace(artifact, rows=tuple(forged_rows))
    with pytest.raises(V5LifecycleLedgerError):
        replace(records[0], source_artifact=forged_artifact)
    forged_record = copy.copy(records[0])
    object.__setattr__(forged_record, "source_artifact", forged_artifact)
    path = tmp_path / "forged-ledger.jsonl"
    ledger = V5PhysicalAdmissionLedgerV2(path, campaign_fingerprint=FP, release_identity_sha256=RELEASE, role=LedgerRole.PRIMARY)
    before = (len(path.read_text(encoding="utf-8").splitlines()), ledger.head_sha256)
    with pytest.raises(V5LifecycleLedgerError):
        ledger.append_record(forged_record)
    after = (len(path.read_text(encoding="utf-8").splitlines()), ledger.head_sha256)
    assert after == before


def test_artifact_mutation_after_append_fails_before_tell(tmp_path: Path):
    artifact, _, records = _chain(tmp_path, attempt_count=1)
    path = tmp_path / "ledger.jsonl"
    ledger = V5PhysicalAdmissionLedgerV2(path, campaign_fingerprint=FP, release_identity_sha256=RELEASE, role=LedgerRole.PRIMARY)
    ledger.append_record(records[0])
    Path(artifact.artifact_path).write_bytes(Path(artifact.artifact_path).read_bytes() + b"tamper")
    with pytest.raises(V5LifecycleLedgerError):
        ledger.prepare_tell(records[0].trial_id)


def test_exactly_once_tell_crash_matrix_and_ineligible_has_no_token(tmp_path: Path):
    _, _, records = _chain(tmp_path / "eligible", attempt_count=1)
    path = tmp_path / "tell.jsonl"
    ledger = V5PhysicalAdmissionLedgerV2(path, campaign_fingerprint=FP, release_identity_sha256=RELEASE, role=LedgerRole.PRIMARY)
    with pytest.raises(V5LifecycleLedgerError):
        ledger.prepare_tell(records[0].trial_id)
    ledger.append_record(records[0])
    prepared = ledger.prepare_tell(records[0])
    restarted = V5PhysicalAdmissionLedgerV2(path, campaign_fingerprint=FP, release_identity_sha256=RELEASE, role=LedgerRole.PRIMARY)
    assert restarted.prepare_tell(records[0]).tell_token == prepared.tell_token
    authorized = restarted.authorize_tell(records[0].trial_id)
    calls = [authorized.tell_token]
    with pytest.raises(V5AmbiguousTellError):
        restarted.reconcile_tell(records[0].trial_id)
    after_authorize_crash = V5PhysicalAdmissionLedgerV2(path, campaign_fingerprint=FP, release_identity_sha256=RELEASE, role=LedgerRole.PRIMARY)
    with pytest.raises(V5AmbiguousTellError):
        after_authorize_crash.reconcile_tell(records[0].trial_id)
    optimizer_receipt = OptimizerReceiptV2(records[0].trial_id, authorized.tell_token, records[0].record_sha256, "optimizer-1")
    assert after_authorize_crash.reconcile_tell(records[0].trial_id, optimizer_receipt).state is TellState.OPTIMIZER_RECEIPT
    after_optimizer_crash = V5PhysicalAdmissionLedgerV2(path, campaign_fingerprint=FP, release_identity_sha256=RELEASE, role=LedgerRole.PRIMARY)
    assert after_optimizer_crash.reconcile_tell(records[0].trial_id).state is TellState.OPTIMIZER_RECEIPT
    assert after_optimizer_crash.commit_tell(records[0].trial_id, optimizer_receipt).state is TellState.COMMITTED
    after_commit_crash = V5PhysicalAdmissionLedgerV2(path, campaign_fingerprint=FP, release_identity_sha256=RELEASE, role=LedgerRole.PRIMARY)
    assert after_commit_crash.authorize_tell(records[0].trial_id).state is TellState.COMMITTED
    assert after_commit_crash.reconcile_tell(records[0].trial_id, optimizer_receipt).state is TellState.COMMITTED
    assert after_commit_crash.commit_tell(records[0].trial_id, optimizer_receipt).state is TellState.COMMITTED
    assert calls == [authorized.tell_token]
    _, chain, ineligible_records = _chain(
        tmp_path / "ineligible",
        attempt_count=1,
        prefix="ineligible",
        gate_failure=(1, "path", "safety"),
    )
    ineligible = FigureEightPhysicalRecordV2.from_attempt(
        chain,
        chain.attempts[0],
        ineligible_records[0].closure,
    )
    ineligible_ledger = V5PhysicalAdmissionLedgerV2(tmp_path / "ineligible.jsonl", campaign_fingerprint=FP, release_identity_sha256=RELEASE, role=LedgerRole.PRIMARY)
    receipt = ineligible_ledger.append_record(ineligible)
    assert not receipt.eligible and receipt.tell_token is None
    with pytest.raises(V5LifecycleLedgerError):
        ineligible_ledger.prepare_tell(ineligible.trial_id)


@pytest.mark.parametrize(
    "state,receipt_mode,match",
    (
        (TellState.AUTHORIZED, "none", "begin at PREPARED"),
        (TellState.PREPARED, "present", "pre-receipt"),
    ),
)
def test_cold_tell_verifier_rejects_illegal_genesis_and_receipt_semantics(
    tmp_path: Path,
    state: TellState,
    receipt_mode: str,
    match: str,
) -> None:
    _, _, records = _chain(tmp_path / state.value, attempt_count=1)
    path = tmp_path / f"forged-{state.value}.jsonl"
    ledger = V5PhysicalAdmissionLedgerV2(
        path,
        campaign_fingerprint=FP,
        release_identity_sha256=RELEASE,
        role=LedgerRole.PRIMARY,
    )
    ledger.append_record(records[0])
    rows = path.read_text(encoding="utf-8").splitlines()
    prior = json.loads(rows[-1])
    token = records[0].tell_token()
    assert token is not None
    forged = {
        "schema": prior["schema"],
        "version": prior["version"],
        "record_type": "tell_state",
        "campaign_fingerprint": FP,
        "release_identity_sha256": RELEASE,
        "role": LedgerRole.PRIMARY.value,
        "previous_sha256": prior["row_sha256"],
        "trial_id": records[0].trial_id,
        "tell_token": token,
        "state": state.value,
    }
    if receipt_mode == "present":
        forged["optimizer_receipt"] = OptimizerReceiptV2(
            records[0].trial_id,
            token,
            records[0].record_sha256,
            "forged-prepared-receipt",
        ).as_dict()
    forged["row_sha256"] = V5PhysicalAdmissionLedgerV2._row_hash(forged)
    path.write_text(
        "\n".join((*rows, json.dumps(forged, sort_keys=True, separators=(",", ":"))))
        + "\n",
        encoding="utf-8",
    )
    with pytest.raises(V5LifecycleLedgerError, match=match):
        V5PhysicalAdmissionLedgerV2(
            path,
            campaign_fingerprint=FP,
            release_identity_sha256=RELEASE,
            role=LedgerRole.PRIMARY,
        )


def test_no_live_transport_or_sensor_imports_in_v5_lifecycle_module():
    source = (ROOT / "tools/step6_figure8_autotune_v1/v5_lifecycle_ledger.py").read_text(encoding="utf-8").lower()
    assert "import rtde" not in source
    assert "kunwei" not in source
    assert "connect(" not in source
