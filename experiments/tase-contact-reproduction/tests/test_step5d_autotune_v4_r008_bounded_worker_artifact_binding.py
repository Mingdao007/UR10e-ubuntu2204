"""Offline tests for the r008 worker-side artifact-binding bound (2026-08-04).

Root cause fixed here: the frozen r006 ``optimizer_worker._artifact_binding``
unconditionally ``cold_read_verify``'s *every* row in the sidecar file on
*every* single ``ask()``/``fit_group_once()`` call -- the membership check
against the caller's requested rows only decides what to *keep*, after the
expensive per-row raw-sample recompute already ran. This is independent of
candidate count / batching: measured live 2026-08-03/04, cutting
``CANDIDATE_SOBOL`` and later batching the qLogNEI scoring loop left the
live seal->dispatch ask gap unchanged across four measurements (356s, 526s,
381s, 378s), while directly instrumenting ``_artifact_binding`` alone showed
it costing ~169s at 83 rows.

This bounds the expensive recompute to the newest ``tail_rows`` positions in
the sidecar body; older rows are trusted via the row-level hash chain
(checked unconditionally for every row, same as before) instead of
re-derived from raw samples. It never edits
``tools/step5d_autotune_v4_r006/optimizer_worker.py``.
"""

from __future__ import annotations

from pathlib import Path
import sys
import time

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from step5d_autotune_v4_r006.contracts import load_contract  # noqa: E402
from step5d_autotune_v4_r006.lattice import ANCHOR_POINT  # noqa: E402
from step5d_autotune_v4_r006.objective import build_receipt_from_samples  # noqa: E402
from step5d_autotune_v4_r006.optimizer_worker import (  # noqa: E402
    OptimizerWorkerError,
    _artifact_binding as oracle_artifact_binding,
)
from step5d_autotune_v4_r006.sidecar import R006ObjectiveSidecar  # noqa: E402
from step5d_autotune_v4_r008.bounded_worker_artifact_binding import (  # noqa: E402
    R008_RECEIPT_CACHE_MAX_ENTRIES,
    bounded_artifact_binding,
    clear_receipt_cache,
    receipt_cache_size,
)
from step5d_force_objective import ForcePathSample  # noqa: E402


def _sample(path_time_s: float, force_n: float, sequence: int) -> ForcePathSample:
    return ForcePathSample(
        path_time_s=path_time_s,
        path_phase=25,
        filtered_normal_n=force_n,
        source_sequences={"controller": sequence, "rtde": sequence},
        source_ages_s={"controller": 0.001, "rtde": 0.001},
        timestamp_s=1000.0 + path_time_s,
    )


def _complete_samples(force_n: float, *, sequence_offset: int = 0) -> tuple[ForcePathSample, ...]:
    samples = [_sample(0.05 + i * 0.1, force_n, sequence_offset + i + 1) for i in range(550)]
    samples.extend(
        _sample(55.05 + i * 0.1, force_n, sequence_offset + 550 + i + 1) for i in range(50)
    )
    return tuple(samples)


def _build_sidecar(tmp_path: Path, n: int) -> tuple[R006ObjectiveSidecar, object]:
    contract = load_contract()
    sidecar = R006ObjectiveSidecar(
        tmp_path / "r006-objectives.jsonl", campaign_fingerprint=contract.campaign_fingerprint
    )
    for sequence in range(1, n + 1):
        receipt = build_receipt_from_samples(
            _complete_samples(5.25, sequence_offset=sequence * 10_000),
            attempt_sequence=sequence,
            execution_id=f"r008-bound-test-{sequence}",
            campaign_fingerprint=contract.campaign_fingerprint,
            candidate_uid=ANCHOR_POINT.uid,
        )
        sidecar.append(
            receipt,
            epoch=1,
            candidate_uid=ANCHOR_POINT.uid,
            kind="SPACEFILL",
            point_key=list(ANCHOR_POINT.key),
        )
    return sidecar, contract


def _binding_value(sidecar: R006ObjectiveSidecar, contract, n: int) -> dict:
    import hashlib

    return {
        "sidecar_path": str(sidecar.path),
        "sidecar_sha256": hashlib.sha256(sidecar.path.read_bytes()).hexdigest(),
        "campaign_fingerprint": contract.campaign_fingerprint,
        "rows": [
            {"attempt_sequence": sequence, "execution_id": f"r008-bound-test-{sequence}"}
            for sequence in range(1, n + 1)
        ],
    }


@pytest.fixture(autouse=True)
def _clear_binding_receipt_cache() -> None:
    clear_receipt_cache()
    yield
    clear_receipt_cache()


def test_bounded_matches_oracle_selection_and_trainability(tmp_path: Path, monkeypatch) -> None:
    sidecar, contract = _build_sidecar(tmp_path, 8)
    value = _binding_value(sidecar, contract, 8)

    bounded = bounded_artifact_binding(value, tail_rows=3)
    oracle = oracle_artifact_binding(value)

    assert len(bounded["rows"]) == len(oracle["rows"]) == 8
    # Regression pin for the 2026-08-04 bug: oracle_artifact_binding always
    # cold_read_verifies (flips verification_state -> "verified_raw_artifact"),
    # so if these fixtures are trainable at all, the oracle proves it -- and
    # the bounded (non-tail) rows must match, not silently read False on both
    # sides. Skip this assertion only if the fixture itself isn't trainable
    # (that would be a fixture bug, not a coverage gap), but assert loudly.
    assert any(row["receipt"].trainable for row in oracle["rows"]), (
        "fixture produced no trainable rows at all -- this test cannot "
        "distinguish the real bug from a fixture problem"
    )
    for bounded_row, oracle_row in zip(bounded["rows"], oracle["rows"]):
        assert bounded_row["attempt_sequence"] == oracle_row["attempt_sequence"]
        assert bounded_row["execution_id"] == oracle_row["execution_id"]
        assert bounded_row["receipt"].trainable == oracle_row["receipt"].trainable
        assert bounded_row["receipt"].objective == oracle_row["receipt"].objective
        assert bounded_row["receipt"].objective_mae_n == oracle_row["receipt"].objective_mae_n

    # Explicit, direct pin for the bug itself: a NON-tail row (position 0,
    # well before the tail_rows=3 boundary at position 5) must be trainable
    # whenever the oracle says it should be -- this is what silently broke
    # live (verification_state stayed "builder_sealed" instead of being
    # promoted to "verified_raw_artifact" for skipped-cold-read rows).
    assert bounded["rows"][0]["receipt"].verification_state == "verified_raw_artifact"
    assert bounded["rows"][0]["receipt"].trainable == oracle["rows"][0]["receipt"].trainable


def test_bounded_bounds_cold_read_verify_calls(tmp_path: Path, monkeypatch) -> None:
    sidecar, contract = _build_sidecar(tmp_path, 10)
    value = _binding_value(sidecar, contract, 10)

    from step5d_autotune_v4_r008 import bounded_worker_artifact_binding as mod

    call_count = {"n": 0}
    original = mod.cold_read_verify

    def counting_cold_read_verify(*args, **kwargs):
        call_count["n"] += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(mod, "cold_read_verify", counting_cold_read_verify)

    bounded_artifact_binding(value, tail_rows=3)

    assert call_count["n"] == 3


def test_bounded_still_catches_hash_chain_tampering(tmp_path: Path) -> None:
    sidecar, contract = _build_sidecar(tmp_path, 6)
    value = _binding_value(sidecar, contract, 6)

    # Tamper with an OLD (non-tail) row's stored fields without updating the
    # hash chain -- must still be caught even though it's outside the tail.
    lines = sidecar.path.read_text(encoding="utf-8").splitlines()
    import json as _json

    row1 = _json.loads(lines[1])
    row1["candidate_uid"] = "tampered"
    lines[1] = _json.dumps(row1, sort_keys=True, separators=(",", ":"))
    sidecar.path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    tampered_value = dict(value)
    import hashlib

    tampered_value["sidecar_sha256"] = hashlib.sha256(sidecar.path.read_bytes()).hexdigest()

    with pytest.raises(OptimizerWorkerError, match="hash chain differs"):
        bounded_artifact_binding(tampered_value, tail_rows=2)


def test_bounded_still_catches_artifact_byte_tampering_outside_tail(tmp_path: Path) -> None:
    sidecar, contract = _build_sidecar(tmp_path, 6)
    value = _binding_value(sidecar, contract, 6)

    artifact_root = sidecar.path.parent / "r006_raw_objectives"
    first_artifact = sorted(artifact_root.glob("*.json"))[0]
    payload = first_artifact.read_text(encoding="utf-8")
    first_artifact.write_text(payload[:-1] + " ", encoding="utf-8")

    with pytest.raises(OptimizerWorkerError, match="raw artifact bytes differ"):
        bounded_artifact_binding(value, tail_rows=2)


def test_tail_rows_must_be_reasonable(tmp_path: Path) -> None:
    sidecar, contract = _build_sidecar(tmp_path, 4)
    value = _binding_value(sidecar, contract, 4)
    # tail_rows larger than the row count is fine -- everything is fresh-verified.
    bounded = bounded_artifact_binding(value, tail_rows=100)
    assert len(bounded["rows"]) == 4


def test_receipt_cache_skips_json_loads_for_non_tail_on_repeat(tmp_path: Path, monkeypatch) -> None:
    sidecar, contract = _build_sidecar(tmp_path, 8)
    value = _binding_value(sidecar, contract, 8)

    from step5d_autotune_v4_r008 import bounded_worker_artifact_binding as mod

    loads_on_artifacts = {"n": 0}
    original_loads = mod.json.loads

    def counting_loads(data, *args, **kwargs):
        # Sidecar JSONL lines are small; artifact receipts are large mappings.
        if isinstance(data, (str, bytes)) and len(data) > 10_000:
            loads_on_artifacts["n"] += 1
        return original_loads(data, *args, **kwargs)

    monkeypatch.setattr(mod.json, "loads", counting_loads)

    first = bounded_artifact_binding(value, tail_rows=3)
    assert loads_on_artifacts["n"] == 8
    assert receipt_cache_size() == 8

    loads_on_artifacts["n"] = 0
    cold_reads = {"n": 0}
    original_cold = mod.cold_read_verify

    def counting_cold(*args, **kwargs):
        cold_reads["n"] += 1
        return original_cold(*args, **kwargs)

    monkeypatch.setattr(mod, "cold_read_verify", counting_cold)

    second = bounded_artifact_binding(value, tail_rows=3)
    # Unchanged artifacts: no re-parse and no re-cold_read on repeat.
    assert loads_on_artifacts["n"] == 0
    assert cold_reads["n"] == 0
    assert len(second["rows"]) == len(first["rows"]) == 8
    for left, right in zip(first["rows"], second["rows"]):
        assert left["receipt"].objective == right["receipt"].objective
        assert left["receipt"].trainable == right["receipt"].trainable
        assert left["receipt"].verification_state == "verified_raw_artifact"


def test_r006_request_uses_bounded_binder_via_batched_worker_patch(tmp_path: Path, monkeypatch) -> None:
    """r008 must not let r006._request fall back to unbounded cold_read_verify."""

    sidecar, contract = _build_sidecar(tmp_path, 8)
    value = _binding_value(sidecar, contract, 8)

    import step5d_autotune_v4_r006.optimizer_worker as r006
    import step5d_autotune_v4_r008.optimizer_worker_batched as batched
    from step5d_autotune_v4_r008 import bounded_worker_artifact_binding as mod

    assert r006._artifact_binding is mod.bounded_artifact_binding

    call_count = {"n": 0}
    original = mod.cold_read_verify

    def counting_cold_read_verify(*args, **kwargs):
        call_count["n"] += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(mod, "cold_read_verify", counting_cold_read_verify)

    # Shape-validation pass inside r006._request (result discarded).
    request = {
        "schema": "step5d.autotune-v4/r006-optimizer-request-v1",
        "profile": "optimizer",
        "expected_attestation": {
            "schema": "step5d.autotune-v4/r006-optimizer-attestation-v1",
            "bundle_id": "a" * 64,
            "runtime_attestation_sha256": "b" * 64,
            "profile": "optimizer",
            "environment_id": "c" * 64,
            "environment_hash": "d" * 64,
            "required_versions": {"torch": "x", "botorch": "x", "gpytorch": "x"},
            "cuda_available": True,
            "gpu_name": "x",
            "gpu_uuid": "GPU-x",
        },
        "payload": {
            "operation": "fit",
            "artifact_binding": value,
            "choices": [list(ANCHOR_POINT.key)],
            "pending": [],
            "incumbent": list(ANCHOR_POINT.key),
            "seed": 1,
            "fit_group": 1,
            "hyperparameters_frozen": False,
            "q": 1,
        },
    }
    # _request may reject attestation shape — call the binder the same way _request does.
    r006._artifact_binding(value)
    assert call_count["n"] == 5  # default R008_ARTIFACT_BINDING_TAIL_ROWS
    assert batched._artifact_binding is mod.bounded_artifact_binding


def test_receipt_cache_still_sees_artifact_byte_tampering(tmp_path: Path) -> None:
    sidecar, contract = _build_sidecar(tmp_path, 6)
    value = _binding_value(sidecar, contract, 6)
    bounded_artifact_binding(value, tail_rows=2)
    assert receipt_cache_size() == 6

    artifact_root = sidecar.path.parent / "r006_raw_objectives"
    first_artifact = sorted(artifact_root.glob("*.json"))[0]
    payload = first_artifact.read_text(encoding="utf-8")
    first_artifact.write_text(payload[:-1] + " ", encoding="utf-8")

    with pytest.raises(OptimizerWorkerError, match="raw artifact bytes differ"):
        bounded_artifact_binding(value, tail_rows=2)


def test_receipt_cache_drops_raw_bundle_after_verify(tmp_path: Path) -> None:
    """Track A2: cached/returned receipts must not retain ~18MB raw_bundle."""

    sidecar, contract = _build_sidecar(tmp_path, 6)
    value = _binding_value(sidecar, contract, 6)
    bounded = bounded_artifact_binding(value, tail_rows=2)

    assert receipt_cache_size() == 6
    assert any(row["receipt"].trainable for row in bounded["rows"])
    for row in bounded["rows"]:
        receipt = row["receipt"]
        assert receipt.raw_bundle == {}
        assert receipt.verification_state == "verified_raw_artifact"
        # Double-bind / trainability fix must survive slimming.
        assert receipt.trainable is True
        assert receipt.objective is not None
        assert row.get("point_key") is not None

    # Warm hit must still serve slim, trainable receipts (no re-inflate).
    warm = bounded_artifact_binding(value, tail_rows=2)
    for row in warm["rows"]:
        assert row["receipt"].raw_bundle == {}
        assert row["receipt"].trainable is True
        assert row["receipt"].objective == pytest.approx(
            next(
                r["receipt"].objective
                for r in bounded["rows"]
                if r["attempt_sequence"] == row["attempt_sequence"]
            )
        )


def test_receipt_cache_lru_row_bound(tmp_path: Path, monkeypatch) -> None:
    """Track A2: cache length is capped; LRU eviction still leaves ask usable."""

    from step5d_autotune_v4_r008 import bounded_worker_artifact_binding as mod

    monkeypatch.setattr(mod, "R008_RECEIPT_CACHE_MAX_ENTRIES", 3)
    assert R008_RECEIPT_CACHE_MAX_ENTRIES >= 3  # imported default for other tests

    sidecar, contract = _build_sidecar(tmp_path, 8)
    value = _binding_value(sidecar, contract, 8)

    first = bounded_artifact_binding(value, tail_rows=2)
    assert receipt_cache_size() == 3
    assert len(first["rows"]) == 8
    assert all(row["receipt"].trainable for row in first["rows"])
    assert all(row["receipt"].raw_bundle == {} for row in first["rows"])

    # Newest three inserts remain; re-binding only those must be warm hits.
    warm_value = _binding_value(sidecar, contract, 8)
    warm_value["rows"] = value["rows"][-3:]
    loads_on_artifacts = {"n": 0}
    original_loads = mod.json.loads

    def counting_loads(data, *args, **kwargs):
        if isinstance(data, (str, bytes)) and len(data) > 10_000:
            loads_on_artifacts["n"] += 1
        return original_loads(data, *args, **kwargs)

    monkeypatch.setattr(mod.json, "loads", counting_loads)
    warm = bounded_artifact_binding(warm_value, tail_rows=2)
    assert receipt_cache_size() == 3
    assert loads_on_artifacts["n"] == 0
    assert len(warm["rows"]) == 3
    assert all(row["receipt"].trainable for row in warm["rows"])

    # Binding the oldest three (evicted) must re-parse and still train.
    loads_on_artifacts["n"] = 0
    cold_value = dict(value)
    cold_value["rows"] = value["rows"][:3]
    cold = bounded_artifact_binding(cold_value, tail_rows=2)
    assert loads_on_artifacts["n"] == 3
    assert receipt_cache_size() == 3
    assert all(row["receipt"].trainable for row in cold["rows"])
    for left, right in zip(first["rows"][:3], cold["rows"]):
        assert left["receipt"].objective == right["receipt"].objective


def test_receipt_cache_warm_bind_timing_smoke(tmp_path: Path) -> None:
    """Offline ask/bind smoke: warm cache must not pay cold JSON-parse cost.

    Does not touch GPU / host / worker. Builds a larger sidecar so cold vs
    warm wall time is distinguishable without relying on live artifacts.
    """

    n = 24
    sidecar, contract = _build_sidecar(tmp_path, n)
    value = _binding_value(sidecar, contract, n)

    t0 = time.perf_counter()
    cold = bounded_artifact_binding(value, tail_rows=3)
    cold_s = time.perf_counter() - t0
    assert receipt_cache_size() == n
    assert all(row["receipt"].trainable for row in cold["rows"])
    assert all(row["receipt"].raw_bundle == {} for row in cold["rows"])

    t1 = time.perf_counter()
    warm = bounded_artifact_binding(value, tail_rows=3)
    warm_s = time.perf_counter() - t1
    assert len(warm["rows"]) == n
    assert all(row["receipt"].trainable for row in warm["rows"])

    # Warm path skips ~n large JSON parses; require a clear gap vs cold.
    # Absolute floors stay loose for loaded CI hosts; the ratio is the pin.
    assert warm_s < cold_s
    assert warm_s < max(0.25, cold_s * 0.35)
    # Sanity: cold paid real parse work (not a no-op fixture).
    assert cold_s > 0.05
