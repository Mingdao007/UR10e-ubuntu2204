"""Regression test for the r008 GP training-set deduplication (2026-08-04).

Root cause fixed here: repeated candidates (ANCHOR/STAIRCASE validation
repeats, revisited BO_TRIAL points) share the exact same feature vector.
A kernel is a deterministic function of (x1, x2), so duplicate rows in the
training X force duplicate rows/columns in the covariance matrix K(X, X),
making it exactly singular. Measured live 2026-08-04: 89 trainable rows had
only 60 unique feature vectors (25 duplicate groups), which pushed
Cholesky through repeated jitter retries (1e-8 up to 1e-3) into botorch's
eigendecomposition fallback -- costing ~200-260s per ask(), independent of
candidate count, batching, or the artifact-binding cost fixed earlier the
same night.

This bounds the GP's actual training set to one row per unique feature
vector (mean objective across repeats; noise variance from the empirical
between-repeat spread, floored at the fixed 2.5e-5 measurement-noise
estimate) before constructing train_x/train_y/train_yvar. It only touches
``tools/step5d_autotune_v4_r008/optimizer_worker_batched.py`` (r008-owned);
the frozen r006 oracle worker is untouched and still trains on raw
(non-deduplicated) rows, which is why oracle-vs-batched selection
equivalence is only asserted for duplicate-free fixtures elsewhere
(test_step5d_autotune_v4_r008_batched_qlognei.py).
"""

from __future__ import annotations

import hashlib
import sys
import tempfile
from pathlib import Path
from typing import Any, Mapping

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT.parents[1] / "src" / "ur10e_experiment_runtime"))

torch = pytest.importorskip("torch")
pytest.importorskip("botorch")
pytest.importorskip("gpytorch")

cuda_available = bool(torch.cuda.is_available())

from step5d_autotune_v4_r005.observations import ObservationLedger  # noqa: E402
from step5d_autotune_v4_r006.contracts import load_contract  # noqa: E402
from step5d_autotune_v4_r006.lattice import ANCHOR_POINT, ParameterPoint  # noqa: E402
from step5d_autotune_v4_r006.live_adapter import R006ObservationLedger  # noqa: E402
from step5d_autotune_v4_r006.objective import build_receipt_from_samples  # noqa: E402
from step5d_force_objective import ForcePathSample  # noqa: E402
from step5d_optimizer_runtime import ATTESTATION_SCHEMA, REQUEST_SCHEMA  # noqa: E402

import step5d_autotune_v4_r008.optimizer_worker_batched as batched_worker  # noqa: E402


def _sample(path_time_s: float, force_n: float, sequence: int) -> ForcePathSample:
    return ForcePathSample(
        path_time_s=path_time_s,
        path_phase=25,
        filtered_normal_n=force_n,
        source_sequences={"controller": sequence, "rtde": sequence},
        source_ages_s={"controller": 0.001, "rtde": 0.001},
        timestamp_s=1000.0 + path_time_s,
    )


def _complete_samples(force_n: float, *, sequence_offset: int = 0):
    samples = [_sample(0.05 + i * 0.1, force_n, sequence_offset + i + 1) for i in range(550)]
    samples.extend(
        _sample(55.05 + i * 0.1, force_n, sequence_offset + 550 + i + 1) for i in range(50)
    )
    return tuple(samples)


def _fake_attestation() -> dict[str, Any]:
    return {
        "schema": ATTESTATION_SCHEMA,
        "bundle_id": "a" * 64,
        "runtime_attestation_sha256": "b" * 64,
        "profile": "optimizer",
        "environment_id": "c" * 64,
        "environment_hash": "d" * 64,
        "required_versions": {"torch": "test", "botorch": "test", "gpytorch": "test"},
        "cuda_available": True,
        "gpu_name": "test-gpu",
        "gpu_uuid": "GPU-test",
    }


def _envelope(payload: Mapping[str, Any], expected: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "schema": REQUEST_SCHEMA,
        "profile": "optimizer",
        "expected_attestation": dict(expected),
        "payload": dict(payload),
    }


def _binding_with_repeats(tmp_path: Path) -> tuple[dict[str, Any], list[tuple[ParameterPoint, float]]]:
    contract = load_contract()
    ledger = ObservationLedger(
        tmp_path / "observations.jsonl",
        campaign_fingerprint=contract.campaign_fingerprint,
        eoat_sha256="f" * 64,
    )
    sidecar = R006ObservationLedger(ledger, campaign_fingerprint=contract.campaign_fingerprint)
    # ANCHOR_POINT repeated 4x with slightly different force readings (as a
    # real anchor-validation repeat would produce), plus 2 distinct points.
    points_and_forces = [
        (ANCHOR_POINT, 5.58),
        (ANCHOR_POINT, 5.59),
        (ANCHOR_POINT, 5.57),
        (ANCHOR_POINT, 5.60),
        (ParameterPoint(p_step=1), 3.2),
        (ParameterPoint(d_step=1), 4.1),
    ]
    for sequence, (point, force) in enumerate(points_and_forces, start=1):
        receipt = build_receipt_from_samples(
            _complete_samples(force, sequence_offset=sequence * 10_000),
            attempt_sequence=sequence,
            execution_id=f"dedup-test-{sequence}",
            campaign_fingerprint=contract.campaign_fingerprint,
            candidate_uid=point.uid,
        )
        sidecar.sidecar.append(
            receipt, epoch=1, candidate_uid=point.uid, kind="ANCHOR", point_key=list(point.key)
        )
    rows = sidecar.sidecar.fresh_process_verify()
    sidecar_data = sidecar.sidecar.path.read_bytes()
    binding = {
        "sidecar_path": str(sidecar.sidecar.path.resolve(strict=True)),
        "sidecar_sha256": hashlib.sha256(sidecar_data).hexdigest(),
        "sidecar_prefix_bytes": len(sidecar_data),
        "campaign_fingerprint": contract.campaign_fingerprint,
        "rows": [
            {"attempt_sequence": int(r["attempt_sequence"]), "execution_id": str(r["execution_id"])}
            for r in rows
        ],
    }
    return binding, points_and_forces


@pytest.mark.skipif(not cuda_available, reason="CUDA required for the r008 batched worker")
def test_repeated_points_collapse_to_one_training_row(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    expected = _fake_attestation()
    monkeypatch.setattr(batched_worker, "_self_attest", lambda value: dict(value))
    binding, points_and_forces = _binding_with_repeats(tmp_path)

    fit = batched_worker.run(
        _envelope(
            {
                "operation": "fit",
                "artifact_binding": binding,
                "choices": [],
                "pending": [],
                "incumbent": list(ANCHOR_POINT.key),
                "seed": 6008,
                "fit_group": 1,
                "hyperparameters_frozen": False,
                "q": 1,
            },
            expected,
        )
    )
    metadata = fit["metadata"]
    # 6 raw trainable rows (4x ANCHOR_POINT repeat + 2 distinct) -> 3 unique
    # feature vectors after dedup.
    assert metadata["raw_trainable_row_count"] == 6
    assert metadata["observation_count"] == 3
    assert metadata["deduplicated_row_count"] == 3


@pytest.mark.skipif(not cuda_available, reason="CUDA required for the r008 batched worker")
def test_dedup_does_not_crash_ask_and_selects_a_valid_choice(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    expected = _fake_attestation()
    monkeypatch.setattr(batched_worker, "_self_attest", lambda value: dict(value))
    binding, _ = _binding_with_repeats(tmp_path)

    fit = batched_worker.run(
        _envelope(
            {
                "operation": "fit",
                "artifact_binding": binding,
                "choices": [],
                "pending": [],
                "incumbent": list(ANCHOR_POINT.key),
                "seed": 6008,
                "fit_group": 1,
                "hyperparameters_frozen": False,
                "q": 1,
            },
            expected,
        )
    )
    choices = [
        list(ParameterPoint(p_step=1, d_step=1).key),
        list(ParameterPoint(ko_step=1).key),
        list(ParameterPoint(kp_step=1).key),
    ]
    ask = batched_worker.run(
        _envelope(
            {
                "operation": "ask",
                "artifact_binding": binding,
                "choices": choices,
                "pending": [],
                "incumbent": list(ANCHOR_POINT.key),
                "seed": 6008,
                "fit_group": "frozen",
                "hyperparameters_frozen": True,
                "frozen_model_state": fit["model_state"],
                "q": 1,
            },
            expected,
        )
    )
    assert ask["selected_point_key"] in choices
    assert ask["metadata"]["observation_count"] == 3
