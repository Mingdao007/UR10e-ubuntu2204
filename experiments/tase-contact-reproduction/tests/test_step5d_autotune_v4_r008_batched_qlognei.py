"""Stage A offline equivalence gate: r006 sequential qLogNEI vs r008 batched.

Compares ``step5d_autotune_v4_r006.optimizer_worker.run`` (oracle) against
``step5d_autotune_v4_r008.optimizer_worker_batched.run`` on a synthetic
minimal sidecar. No robot I/O.
"""

from __future__ import annotations

import hashlib
import itertools
import sys
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

import step5d_autotune_v4_r006.optimizer_worker as oracle_worker  # noqa: E402
import step5d_autotune_v4_r008.optimizer_worker_batched as batched_worker  # noqa: E402

from step5d_autotune_v4_r006.optimizer_worker import run as oracle_run  # noqa: E402
from step5d_autotune_v4_r008.optimizer_worker_batched import run as batched_run  # noqa: E402


SCORE_TOL = 1e-10


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
    samples = [
        _sample(0.05 + index * 0.1, force_n, sequence_offset + index + 1)
        for index in range(550)
    ]
    samples.extend(
        _sample(55.05 + index * 0.1, force_n, sequence_offset + 550 + index + 1)
        for index in range(50)
    )
    return tuple(samples)


def _receipt(contract, point: ParameterPoint, sequence: int, *, force_n: float = 5.25):
    return build_receipt_from_samples(
        _complete_samples(force_n, sequence_offset=sequence * 10_000),
        attempt_sequence=sequence,
        execution_id=f"r008-batched-equiv-{sequence}",
        campaign_fingerprint=contract.campaign_fingerprint,
        candidate_uid=point.uid,
    )


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


def _patch_self_attest(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    expected = _fake_attestation()

    def _attest(value: Mapping[str, Any]) -> dict[str, Any]:
        return dict(value)

    monkeypatch.setattr(oracle_worker, "_self_attest", _attest)
    monkeypatch.setattr(batched_worker, "_self_attest", _attest)
    return expected


def _synthetic_binding(tmp_path: Path) -> tuple[dict[str, Any], ParameterPoint]:
    contract = load_contract()
    ledger = ObservationLedger(
        tmp_path / "observations.jsonl",
        campaign_fingerprint=contract.campaign_fingerprint,
        eoat_sha256="f" * 64,
    )
    sidecar = R006ObservationLedger(ledger, campaign_fingerprint=contract.campaign_fingerprint)
    train_points = (
        ANCHOR_POINT,
        ParameterPoint(p_step=1),
        ParameterPoint(d_step=1),
        ParameterPoint(tau_step=1),
    )
    for sequence, point in enumerate(train_points, start=1):
        receipt = _receipt(contract, point, sequence, force_n=5.20 + sequence * 0.03)
        sidecar.sidecar.append(
            receipt,
            epoch=1,
            candidate_uid=point.uid,
            kind="WARM_START_1",
            point_key=list(point.key),
        )
    raw_rows = sidecar.sidecar.fresh_process_verify()
    binding = {
        "sidecar_path": str(sidecar.sidecar.path.resolve(strict=True)),
        "sidecar_sha256": hashlib.sha256(sidecar.sidecar.path.read_bytes()).hexdigest(),
        "campaign_fingerprint": contract.campaign_fingerprint,
        "rows": [
            {
                "attempt_sequence": int(row["attempt_sequence"]),
                "execution_id": str(row["execution_id"]),
            }
            for row in raw_rows
        ],
    }
    return binding, ANCHOR_POINT


def _choice_keys() -> list[list[Any]]:
    return [
        list(ParameterPoint(p_step=1, d_step=1).key),
        list(ParameterPoint(p_step=1).key),
        list(ParameterPoint(d_step=1).key),
        list(ParameterPoint(tau_step=1).key),
        list(ParameterPoint(ko_step=1).key),
        list(ParameterPoint(kp_step=1).key),
    ]


def _sequential_scores(
    acquisition: Any,
    choices: tuple[Any, ...],
    requested_q: int,
    *,
    device: Any,
    torch_mod: Any,
) -> list[float]:
    """Mirror frozen r006 per-combination scoring (including per-call .item())."""

    scored: list[float] = []
    with torch_mod.no_grad():
        for batch in itertools.combinations(choices, requested_q):
            batch_x = torch_mod.tensor(
                [[oracle_worker._features(point) for point in batch]],
                dtype=torch_mod.double,
                device=device,
            )
            value = acquisition(batch_x)
            scored.append(float(value.detach().cpu().item()))
    return scored


def _scores_from_result(result: Mapping[str, Any]) -> list[float] | None:
    metadata = result.get("metadata")
    if not isinstance(metadata, Mapping):
        return None
    for key in ("scores", "score_list", "qlognei_scores"):
        value = metadata.get(key)
        if isinstance(value, list) and value and all(isinstance(x, (int, float)) for x in value):
            return [float(x) for x in value]
    return None


@pytest.mark.skipif(not cuda_available, reason="CUDA required for r006/r008 qLogNEI workers")
def test_batched_qlognei_selection_matches_oracle(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    expected = _patch_self_attest(monkeypatch)
    binding, incumbent = _synthetic_binding(tmp_path)
    choices = _choice_keys()
    pending = [list(ParameterPoint(p_step=1).key)]

    fit = oracle_run(
        _envelope(
            {
                "operation": "fit",
                "artifact_binding": binding,
                "choices": [],
                "pending": [],
                "incumbent": list(incumbent.key),
                "seed": 6006,
                "fit_group": 1,
                "hyperparameters_frozen": False,
                "q": 1,
            },
            expected,
        )
    )
    assert "model_state" in fit
    frozen_state = fit["model_state"]

    ask_payload = {
        "operation": "ask",
        "artifact_binding": binding,
        "choices": choices,
        "pending": pending,
        "incumbent": list(incumbent.key),
        "seed": 6006,
        "fit_group": "frozen",
        "hyperparameters_frozen": True,
        "q": 1,
        "frozen_model_state": frozen_state,
    }
    oracle = oracle_run(_envelope(ask_payload, expected))
    candidate = batched_run(_envelope(ask_payload, expected))

    assert oracle["selected_point_key"] == candidate["selected_point_key"]
    assert oracle["selected_point_keys"] == candidate["selected_point_keys"]
    assert candidate["metadata"].get("scoring", "").startswith("batched_tbatch")

    oracle_scores = _scores_from_result(oracle)
    candidate_scores = _scores_from_result(candidate)
    if oracle_scores is not None and candidate_scores is not None:
        assert len(oracle_scores) == len(candidate_scores)
        max_abs = max(abs(a - b) for a, b in zip(oracle_scores, candidate_scores, strict=True))
        assert max_abs <= SCORE_TOL


@pytest.mark.skipif(not cuda_available, reason="CUDA required for r006/r008 qLogNEI workers")
def test_batched_qlognei_score_vector_matches_sequential(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Hard score gate via captured acquisition: max|Δ| ≤ 1e-10."""

    expected = _patch_self_attest(monkeypatch)
    binding, incumbent = _synthetic_binding(tmp_path)
    choices = _choice_keys()
    pending = [list(ParameterPoint(p_step=1).key)]

    fit = oracle_run(
        _envelope(
            {
                "operation": "fit",
                "artifact_binding": binding,
                "choices": [],
                "pending": [],
                "incumbent": list(incumbent.key),
                "seed": 6006,
                "fit_group": 1,
                "hyperparameters_frozen": False,
                "q": 1,
            },
            expected,
        )
    )
    held: dict[str, Any] = {}
    original = batched_worker._score_combination_batches

    def _capture(acquisition, choice_points, requested_q, *, device, torch):
        selected, score_list, scoring = original(
            acquisition, choice_points, requested_q, device=device, torch=torch
        )
        held.update(
            {
                "acquisition": acquisition,
                "choices": choice_points,
                "q": requested_q,
                "device": device,
                "torch": torch,
                "batched_scores": score_list,
            }
        )
        return selected, score_list, scoring

    monkeypatch.setattr(batched_worker, "_score_combination_batches", _capture)
    result = batched_run(
        _envelope(
            {
                "operation": "ask",
                "artifact_binding": binding,
                "choices": choices,
                "pending": pending,
                "incumbent": list(incumbent.key),
                "seed": 6006,
                "fit_group": "frozen",
                "hyperparameters_frozen": True,
                "q": 1,
                "frozen_model_state": fit["model_state"],
            },
            expected,
        )
    )
    assert held.get("batched_scores"), "batched scorer did not capture scores"
    sequential = _sequential_scores(
        held["acquisition"],
        held["choices"],
        held["q"],
        device=held["device"],
        torch_mod=held["torch"],
    )
    assert len(sequential) == len(held["batched_scores"])
    max_abs = max(abs(a - b) for a, b in zip(sequential, held["batched_scores"], strict=True))
    assert max_abs <= SCORE_TOL
    assert result["metadata"].get("scoring", "").startswith("batched_tbatch")


@pytest.mark.skipif(not cuda_available, reason="CUDA required for r006/r008 qLogNEI workers")
def test_batched_qlognei_q4_selection_matches_oracle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    expected = _patch_self_attest(monkeypatch)
    binding, incumbent = _synthetic_binding(tmp_path)
    choices = _choice_keys()  # 6 points → C(6,4)=15 batches (small)

    fit = oracle_run(
        _envelope(
            {
                "operation": "fit",
                "artifact_binding": binding,
                "choices": [],
                "pending": [],
                "incumbent": list(incumbent.key),
                "seed": 6006,
                "fit_group": 1,
                "hyperparameters_frozen": False,
                "q": 1,
            },
            expected,
        )
    )
    ask_payload = {
        "operation": "ask",
        "artifact_binding": binding,
        "choices": choices,
        "pending": [],
        "incumbent": list(incumbent.key),
        "seed": 6006,
        "fit_group": "frozen",
        "hyperparameters_frozen": True,
        "q": 4,
        "frozen_model_state": fit["model_state"],
    }
    oracle = oracle_run(_envelope(ask_payload, expected))
    candidate = batched_run(_envelope(ask_payload, expected))
    assert oracle["selected_point_key"] == candidate["selected_point_key"]
    assert oracle["selected_point_keys"] == candidate["selected_point_keys"]
    assert len(candidate["selected_point_keys"]) == 4
