from __future__ import annotations

import copy
import json
import math
import os
import sys
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from step5d_force_objective import (  # noqa: E402
    ForceObjectiveBuilder,
    ForcePathSample,
)
from step5d_autotune_v4_r009 import early_abort as early_abort_module  # noqa: E402
from step5d_autotune_v4_r009.contracts import build_contract  # noqa: E402
from step5d_autotune_v4_r009.early_abort import (  # noqa: E402
    R009ActiveModeRejected,
    R009AuditEvent,
    R009CausalFormalMetric,
    R009EarlyAbortConfig,
    R009EarlyAbortRecord,
    R009InMemorySidecar,
    R009MetricError,
    R009OutcomeCode,
    R009ShadowSidecarStore,
    R009WatermarkRegression,
    append_audit_event,
    build_shadow_sidecar_row,
    canonical_audit_metadata_bytes,
    canonicalize_audit_metadata,
    resolve_early_abort_mode,
)
from step5d_autotune_v4_r009.identity import (  # noqa: E402
    DEFAULT_EXECUTABLE_BEHAVIOR_CONFIG,
    R009EarlyAbortConfig as IdentityR009EarlyAbortConfig,
    build_behavior_manifest,
)


def _bundle(*, parent: str = "a" * 64, source_value: str = "b" * 64):
    manifest = build_behavior_manifest(
        parent_r006_contract_sha256=parent,
        parent_r006_source_closure_sha256="c" * 64,
        source_set={"tools/r009_fixture.py": source_value},
    )
    return build_contract(
        behavior_manifest=manifest,
        controller_triplet_sha256={
            "script": "1" * 64,
            "txt": "2" * 64,
            "urp": "3" * 64,
        },
    )


def _sample(path_time_s: float, force_n: float, sequence: int) -> ForcePathSample:
    return ForcePathSample(
        path_time_s=path_time_s,
        filtered_normal_n=force_n,
        path_phase=25,
        source_sequences={"rtde": sequence},
        source_ages_s={"rtde": 0.0},
    )


def _record(
    release_identity: Any,
    *,
    dispatch_id: str = "dispatch-1",
    sequence: int = 1,
    execution_id: str = "execution-1",
    execution_attributes: dict[str, Any] | None = None,
    dispatch_attributes: dict[str, Any] | None = None,
    payload: Any = None,
) -> R009EarlyAbortRecord:
    return R009EarlyAbortRecord.from_parts(
        release_identity=release_identity,
        execution_identity={
            "execution_id": execution_id,
            "dispatch_sequence": sequence,
            "identity": (
                {"worker": "offline"}
                if execution_attributes is None
                else execution_attributes
            ),
        },
        dispatch_identity={
            "dispatch_id": dispatch_id,
            "dispatch_sequence": sequence,
            "identity": (
                {"channel": "A"}
                if dispatch_attributes is None
                else dispatch_attributes
            ),
        },
        payload={"candidate": {"gain": 1.0}} if payload is None else payload,
    )


def _write_row(path: Path, row: dict[str, Any]) -> None:
    path.write_bytes(early_abort_module.canonical_bytes(row) + b"\n")


def _store(tmp_path: Path, release_identity: Any, **kwargs: Any):
    return R009ShadowSidecarStore(
        tmp_path / "r009-early-abort-shadow.jsonl",
        release_identity,
        **kwargs,
    )


def test_formal_metric_is_equal_bin_canonical_and_boundary_exact() -> None:
    metric = R009CausalFormalMetric()
    canonical = ForceObjectiveBuilder()
    samples = (
        _sample(5.0, 6.0, 1),
        _sample(5.05, 8.0, 2),
        _sample(5.1, 4.0, 3),
    )
    for sample in samples:
        metric.observe(sample)
        canonical.add(sample)

    partial = metric.snapshot(5.2)
    assert partial.closed_bin_count == 2
    assert partial.observed_closed_bins == 2
    assert partial.open_bin_index == 2
    assert partial.numerator_n == pytest.approx(3.0)
    assert partial.partial_mae_n == pytest.approx(3.0 / 550.0)
    assert partial.denominator_bins == 550

    full_metric = R009CausalFormalMetric()
    full_canonical = ForceObjectiveBuilder()
    for index in range(550):
        sample = _sample(5.0 + index * 0.1 + 0.01, 5.0 + (index % 7) * 0.25, index + 10)
        full_metric.observe(sample)
        full_canonical.add(sample)
    snapshot = full_metric.snapshot(60.0)
    objective = full_canonical.finalize()
    assert snapshot.closed_bin_count == 550
    assert snapshot.complete_bins == 550
    assert snapshot.formal_mae_n == pytest.approx(objective.v2_mae_n)
    assert snapshot.partial_mae_n == pytest.approx(objective.v2_mae_n)

    boundary_metric = R009CausalFormalMetric()
    for sample in (
        _sample(5.0, 5.0, 1),
        _sample(5.1, 5.0, 2),
        _sample(59.999999, 5.0, 3),
    ):
        boundary_metric.observe(sample)
    at_start = boundary_metric.snapshot(5.0)
    at_next = boundary_metric.snapshot(5.1)
    before_end = boundary_metric.snapshot(59.999999)
    at_end = boundary_metric.snapshot(60.0)
    assert (at_start.closed_bin_count, at_start.open_bin_index) == (0, 0)
    assert (at_next.closed_bin_count, at_next.open_bin_index) == (1, 1)
    assert (before_end.closed_bin_count, before_end.open_bin_index) == (549, 549)
    assert (at_end.closed_bin_count, at_end.open_bin_index) == (550, None)


def test_causal_snapshot_hides_future_prefilled_bins_and_rejects_regression() -> None:
    builder = ForceObjectiveBuilder()
    for index in range(550):
        builder.add(_sample(5.0 + index * 0.1 + 0.01, 5.5, index + 1))
    metric = R009CausalFormalMetric(builder)
    early = metric.snapshot(5.1)
    assert early.complete_bins == 1
    assert early.observed_closed_bins == 1
    assert early.formal_mae_n is None
    assert early.partial_mae_n == pytest.approx(0.5 / 550.0)
    with pytest.raises(R009WatermarkRegression):
        metric.snapshot(5.0)


def test_watermark_seals_late_samples_but_allows_open_bin_and_increasing_samples() -> None:
    metric = R009CausalFormalMetric()
    sealed_sample = _sample(5.0, 6.0, 1)
    metric.observe(sealed_sample)
    first = metric.snapshot(5.1)
    first_bytes = early_abort_module.canonical_bytes(first.as_dict())

    late = metric.observe_outcome(_sample(5.05, 8.0, 2))
    assert late.code is R009OutcomeCode.SAMPLE_REJECTED
    assert late.accepted is False
    late_replay = metric.observe_outcome(sealed_sample)
    assert late_replay.code is R009OutcomeCode.SAMPLE_REJECTED
    assert late_replay.accepted is False
    with pytest.raises(R009MetricError):
        metric.observe(_sample(5.0, 6.0, 3))
    repeated = metric.snapshot(5.1)
    assert early_abort_module.canonical_bytes(repeated.as_dict()) == first_bytes
    assert repeated.as_dict() == first.as_dict()

    at_watermark = metric.observe_outcome(_sample(5.1, 4.0, 4))
    assert at_watermark.code is R009OutcomeCode.SAMPLE_ACCEPTED
    assert at_watermark.accepted is True
    assert metric.observe(_sample(5.2, 5.0, 5)) is True
    increasing = metric.snapshot(5.2)
    assert increasing.closed_bin_count == 2
    assert increasing.observed_closed_bins == 2


def test_sample_replay_and_conflict_are_typed() -> None:
    metric = R009CausalFormalMetric()
    sample = _sample(5.01, 5.0, 1)
    assert metric.observe(sample) is True
    assert metric.observe(sample) is False
    with pytest.raises(ValueError, match="conflicting payload"):
        metric.observe(_sample(5.01, 6.0, 1))


def test_active_mode_is_fail_closed_and_channel_c_is_deferred() -> None:
    with pytest.raises(R009ActiveModeRejected):
        resolve_early_abort_mode({"R009_EARLY_ABORT_MODE": "active"})
    assert resolve_early_abort_mode({}) == "shadow"
    config = IdentityR009EarlyAbortConfig.from_mapping(
        DEFAULT_EXECUTABLE_BEHAVIOR_CONFIG["values"]["early_abort"]
    )
    assert config.channel_c == {
        "status": "future_deferred",
        "requires_new_tp": True,
        "requires_new_fingerprint": True,
        "requires_new_contract": True,
        "reuse_hard_stop_channel": False,
    }


def test_early_abort_config_drift_changes_manifest_and_campaign_identity() -> None:
    baseline_config = copy.deepcopy(DEFAULT_EXECUTABLE_BEHAVIOR_CONFIG)
    drifted_config = copy.deepcopy(DEFAULT_EXECUTABLE_BEHAVIOR_CONFIG)
    drifted_config["values"]["early_abort"]["kappa_start"] = 4.0
    baseline = build_behavior_manifest(
        parent_r006_contract_sha256="a" * 64,
        parent_r006_source_closure_sha256="b" * 64,
        source_set={"tools/r009_fixture.py": "c" * 64},
        executable_behavior_config=baseline_config,
    )
    drifted = build_behavior_manifest(
        parent_r006_contract_sha256="a" * 64,
        parent_r006_source_closure_sha256="b" * 64,
        source_set={"tools/r009_fixture.py": "c" * 64},
        executable_behavior_config=drifted_config,
    )
    baseline_contract = _bundle()
    drifted_contract = build_contract(
        behavior_manifest=drifted,
        controller_triplet_sha256={
            "script": "1" * 64,
            "txt": "2" * 64,
            "urp": "3" * 64,
        },
    )
    assert baseline.behavior_manifest_sha256 != drifted.behavior_manifest_sha256
    assert baseline.campaign_fingerprint != drifted.campaign_fingerprint
    assert baseline_contract.release_identity_sha256 != drifted_contract.release_identity_sha256


def test_release_execution_dispatch_payload_bindings_are_immutable() -> None:
    bundle = _bundle()
    execution_attributes = {"nested": {"value": 1}}
    dispatch_attributes = {"nested": {"value": 2}}
    payload = {"nested": [1, {"value": 3}]}
    record = _record(
        bundle.release_identity,
        execution_attributes=execution_attributes,
        dispatch_attributes=dispatch_attributes,
        payload=payload,
    )
    execution_attributes["nested"]["value"] = 99
    dispatch_attributes["nested"]["value"] = 98
    payload["nested"][1]["value"] = 97
    view = record.as_dict()
    assert view["execution_identity"]["identity"]["nested"]["value"] == 1
    assert view["dispatch_identity"]["identity"]["nested"]["value"] == 2
    assert view["payload"]["nested"][1]["value"] == 3
    assert record.payload_sha256 == early_abort_module.canonical_payload_digest(view["payload"])


def test_same_dispatch_slot_with_changed_payload_or_any_identity_is_conflict() -> None:
    bundle = _bundle()
    store = R009InMemorySidecar(bundle.release_identity)
    original = _record(bundle.release_identity)
    assert store.admit(original).code is R009OutcomeCode.ADMITTED
    assert store.admit(original.as_dict()).code is R009OutcomeCode.IDEMPOTENT_DUPLICATE
    conflicts = (
        _record(bundle.release_identity, payload={"candidate": {"gain": 2.0}}),
        _record(bundle.release_identity, execution_id="execution-2"),
        _record(bundle.release_identity, execution_attributes={"changed": True}),
        _record(bundle.release_identity, dispatch_attributes={"changed": True}),
        _record(_bundle(parent="d" * 64).release_identity),
    )
    for conflicting in conflicts:
        assert store.admit(conflicting).code is R009OutcomeCode.DISPATCH_IDENTITY_CONFLICT


def test_sidecar_admission_duplicate_conflict_and_safe_to_append(tmp_path: Path) -> None:
    bundle = _bundle()
    record = _record(bundle.release_identity)
    store = _store(tmp_path, bundle.release_identity)
    admitted = store.append(record)
    assert admitted.code is R009OutcomeCode.SIDECAR_ADMITTED
    original_bytes = store.path.read_bytes()
    loaded = store.load()
    assert loaded.safe_to_append is True
    assert not hasattr(loaded, "safe_to_resume")
    assert [item.code for item in loaded.outcomes] == [R009OutcomeCode.SIDECAR_ADMITTED]

    duplicate = store.append(record.as_dict())
    assert duplicate.code is R009OutcomeCode.SIDECAR_IDEMPOTENT_DUPLICATE
    assert store.path.read_bytes() == original_bytes

    conflict = store.append(
        _record(bundle.release_identity, payload={"candidate": {"gain": 2.0}})
    )
    assert conflict.code is R009OutcomeCode.SIDECAR_DISPATCH_CONFLICT
    assert store.path.read_bytes() == original_bytes


def test_sidecar_load_classifies_exact_duplicate_and_missing_final_newline(
    tmp_path: Path,
) -> None:
    bundle = _bundle()
    record = _record(bundle.release_identity)
    path = tmp_path / "r009-early-abort-shadow.jsonl"
    row_bytes = early_abort_module.canonical_bytes(build_shadow_sidecar_row(record)) + b"\n"
    path.write_bytes(row_bytes + row_bytes)
    store = _store(tmp_path, bundle.release_identity)
    loaded = store.load()
    assert loaded.safe_to_append is True
    assert [item.code for item in loaded.outcomes] == [
        R009OutcomeCode.SIDECAR_ADMITTED,
        R009OutcomeCode.SIDECAR_IDEMPOTENT_DUPLICATE,
    ]

    path.write_bytes(row_bytes[:-1])
    unsafe = store.load()
    assert unsafe.safe_to_append is False
    assert len(unsafe.outcomes) == 1
    assert unsafe.outcomes[0].code is R009OutcomeCode.SIDECAR_MALFORMED_ROW


@pytest.mark.parametrize(
    ("mutation", "expected"),
    (
        (lambda row: row.update(schema="wrong"), R009OutcomeCode.SIDECAR_SCHEMA_MISMATCH),
        (lambda row: row.update(shadow_only=False), R009OutcomeCode.SIDECAR_POLICY_MISMATCH),
        (
            lambda row: row["execution_identity"].update(identity={"changed": True}),
            R009OutcomeCode.SIDECAR_EXECUTION_IDENTITY_MISMATCH,
        ),
        (
            lambda row: row.update(execution_identity_sha256="f" * 64),
            R009OutcomeCode.SIDECAR_EXECUTION_IDENTITY_MISMATCH,
        ),
        (
            lambda row: row["dispatch_identity"].update(identity={"changed": True}),
            R009OutcomeCode.SIDECAR_DISPATCH_IDENTITY_MISMATCH,
        ),
        (
            lambda row: row.update(dispatch_identity_sha256="f" * 64),
            R009OutcomeCode.SIDECAR_DISPATCH_IDENTITY_MISMATCH,
        ),
        (
            lambda row: row.update(payload={"changed": True}),
            R009OutcomeCode.SIDECAR_PAYLOAD_IDENTITY_MISMATCH,
        ),
        (
            lambda row: row.update(payload_sha256="f" * 64),
            R009OutcomeCode.SIDECAR_PAYLOAD_IDENTITY_MISMATCH,
        ),
    ),
)
def test_sidecar_row_identity_schema_and_policy_mismatches_are_typed(
    tmp_path: Path,
    mutation: Any,
    expected: R009OutcomeCode,
) -> None:
    bundle = _bundle()
    row = copy.deepcopy(build_shadow_sidecar_row(_record(bundle.release_identity)))
    mutation(row)
    path = tmp_path / "r009-early-abort-shadow.jsonl"
    _write_row(path, row)
    result = _store(tmp_path, bundle.release_identity).load()
    assert result.safe_to_append is False
    assert len(result.outcomes) == 1
    assert result.outcomes[0].code is expected


def test_sidecar_expected_release_identity_and_digest_mismatch_is_typed(
    tmp_path: Path,
) -> None:
    expected = _bundle()
    other = _bundle(parent="d" * 64)
    _write_row(
        tmp_path / "r009-early-abort-shadow.jsonl",
        build_shadow_sidecar_row(_record(other.release_identity)),
    )
    result = _store(tmp_path, expected.release_identity).load()
    assert result.safe_to_append is False
    assert result.outcomes[0].code is R009OutcomeCode.SIDECAR_RELEASE_IDENTITY_MISMATCH


@pytest.mark.parametrize(
    "encoded",
    (
        b'{"schema":1,"schema":2}\n',
        b'{"schema":NaN}\n',
        b'[]\n',
        b'\n',
        b'{bad}\n',
    ),
)
def test_sidecar_malformed_rows_are_never_silently_skipped(
    tmp_path: Path, encoded: bytes
) -> None:
    bundle = _bundle()
    path = tmp_path / "r009-early-abort-shadow.jsonl"
    path.write_bytes(encoded)
    result = _store(tmp_path, bundle.release_identity).load()
    assert result.safe_to_append is False
    assert len(result.outcomes) == 1
    assert result.outcomes[0].code is R009OutcomeCode.SIDECAR_MALFORMED_ROW


def test_every_scanned_row_is_typed_and_unsafe_rows_block_append(tmp_path: Path) -> None:
    bundle = _bundle()
    record = _record(bundle.release_identity)
    path = tmp_path / "r009-early-abort-shadow.jsonl"
    valid = early_abort_module.canonical_bytes(build_shadow_sidecar_row(record)) + b"\n"
    path.write_bytes(valid + b'{"schema":NaN}\n')
    store = _store(tmp_path, bundle.release_identity)
    result = store.load()
    assert len(result.outcomes) == 2
    assert result.outcomes[0].code is R009OutcomeCode.SIDECAR_ADMITTED
    assert result.outcomes[1].code is R009OutcomeCode.SIDECAR_MALFORMED_ROW
    before = path.read_bytes()
    append_result = store.append(_record(bundle.release_identity, sequence=2, dispatch_id="d2"))
    assert append_result.accepted is False
    assert append_result.code is R009OutcomeCode.SIDECAR_MALFORMED_ROW
    assert path.read_bytes() == before


def test_sidecar_cap_symlink_directory_read_write_and_fsync_failures_are_typed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bundle = _bundle()
    record = _record(bundle.release_identity)

    capped_config = copy.deepcopy(DEFAULT_EXECUTABLE_BEHAVIOR_CONFIG["values"]["early_abort"])
    capped_config["max_sidecar_bytes"] = 1
    capped_store = _store(tmp_path / "cap", bundle.release_identity, config=capped_config)
    capped = capped_store.append(record)
    assert capped.accepted is False
    assert capped.code is R009OutcomeCode.SIDECAR_WRITE_FAILED

    symlink_root = tmp_path / "symlink"
    symlink_root.mkdir()
    symlink_path = symlink_root / "r009-early-abort-shadow.jsonl"
    target = symlink_root / "target.jsonl"
    target.write_bytes(b"")
    os.symlink(target, symlink_path)
    symlink_store = _store(symlink_root, bundle.release_identity)
    assert symlink_store.load().outcomes[0].code is R009OutcomeCode.SIDECAR_READ_FAILED

    directory_root = tmp_path / "directory"
    directory_root.mkdir()
    (directory_root / "r009-early-abort-shadow.jsonl").mkdir()
    directory_store = _store(directory_root, bundle.release_identity)
    assert directory_store.load().outcomes[0].code is R009OutcomeCode.SIDECAR_READ_FAILED

    read_root = tmp_path / "read"
    read_root.mkdir()
    read_path = read_root / "r009-early-abort-shadow.jsonl"
    read_path.write_bytes(b"")
    monkeypatch.setattr(
        early_abort_module.Path,
        "read_bytes",
        lambda self: (_ for _ in ()).throw(OSError("read failure")),
    )
    read_result = _store(read_root, bundle.release_identity).load()
    assert read_result.outcomes[0].code is R009OutcomeCode.SIDECAR_READ_FAILED
    monkeypatch.undo()

    write_root = tmp_path / "write"
    write_root.mkdir()
    monkeypatch.setattr(
        early_abort_module.Path,
        "open",
        lambda self, *args, **kwargs: (_ for _ in ()).throw(OSError("write failure")),
    )
    write_result = _store(write_root, bundle.release_identity).append(record)
    assert write_result.accepted is False
    assert write_result.code is R009OutcomeCode.SIDECAR_WRITE_FAILED
    monkeypatch.undo()

    fsync_root = tmp_path / "sidecar-fsync"
    fsync_store = _store(fsync_root, bundle.release_identity)
    monkeypatch.setattr(
        early_abort_module.os,
        "fsync",
        lambda _fd: (_ for _ in ()).throw(OSError("fsync failure")),
    )
    fsync_result = fsync_store.append(record)
    assert fsync_result.accepted is False
    assert fsync_result.code is R009OutcomeCode.SIDECAR_WRITE_FAILED


class _TestEnum(Enum):
    VALUE = "value"


@dataclass(frozen=True)
class _TestMetadata:
    label: str
    count: int


def test_audit_metadata_is_bounded_deterministic_and_magicmock_safe() -> None:
    cycle: dict[str, Any] = {}
    cycle["self"] = cycle
    metadata = {
        "mock": MagicMock(name="stable-mock"),
        "set": {3, 1, 2},
        "bytes": b"\x00\xff\x01",
        "path": Path("a/b"),
        "enum": _TestEnum.VALUE,
        "dataclass": _TestMetadata("label", 3),
        "cycle": cycle,
        "nonfinite": float("nan"),
    }
    first = canonical_audit_metadata_bytes(metadata)
    second = canonical_audit_metadata_bytes(metadata)
    assert first == second
    assert b"0x" not in first
    json.loads(first)
    normalized = canonicalize_audit_metadata(
        {"deep": {"string": "abcdef", "items": [1, 2, 3]}},
        max_depth=1,
        max_items=2,
        max_string_chars=3,
        max_bytes=128,
    )
    bounded = canonical_audit_metadata_bytes(
        normalized,
        max_depth=4,
        max_items=64,
        max_string_chars=256,
        max_bytes=128,
    )
    assert len(bounded) <= 128
    assert "__truncated__" in bounded.decode("utf-8")


def test_audit_append_magicmock_and_fsync_failure_are_nonfatal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    audit_path = tmp_path / "audit.jsonl"
    event = R009AuditEvent(
        code=R009OutcomeCode.SIDECAR_MALFORMED_ROW,
        metadata={"mock": MagicMock(name="audit-mock"), "set": {2, 1}},
    )
    appended = append_audit_event(audit_path, event)
    assert appended.accepted is True
    assert appended.code is R009OutcomeCode.AUDIT_APPENDED
    json.loads(audit_path.read_text().splitlines()[0])

    fsync_path = tmp_path / "audit-fsync.jsonl"
    monkeypatch.setattr(
        early_abort_module.os,
        "fsync",
        lambda _fd: (_ for _ in ()).throw(OSError("fsync failure")),
    )
    failed = append_audit_event(fsync_path, event)
    assert failed.accepted is False
    assert failed.code is R009OutcomeCode.AUDIT_WRITE_FAILED


def test_audit_write_failure_paths_are_typed_and_do_not_raise(tmp_path: Path) -> None:
    directory = tmp_path / "audit-directory"
    directory.mkdir()
    event = R009AuditEvent(code=R009OutcomeCode.AUDIT_APPENDED, metadata={"x": 1})
    directory_result = append_audit_event(directory, event)
    assert directory_result.accepted is False
    assert directory_result.code is R009OutcomeCode.AUDIT_WRITE_FAILED

    target = tmp_path / "audit-target.jsonl"
    target.write_bytes(b"")
    symlink = tmp_path / "audit-symlink.jsonl"
    os.symlink(target, symlink)
    symlink_result = append_audit_event(symlink, event)
    assert symlink_result.accepted is False
    assert symlink_result.code is R009OutcomeCode.AUDIT_WRITE_FAILED


def test_current_magicmock_json_serialization_regression_is_repaired() -> None:
    event = R009AuditEvent(
        code=R009OutcomeCode.AUDIT_APPENDED,
        metadata={"value": MagicMock(name="json-safe")},
    )
    encoded = json.dumps(event.as_dict(), sort_keys=True, allow_nan=False)
    assert "0x" not in encoded
