"""Ask must join async seal before hashing the objectives sidecar.

Regression for far005 live_20260805_120727: after seq34 BO_TRIAL async seal,
the next CUDA ask fail-closed with ``r006 artifact sidecar bytes differ`` when
binding sha B was computed before an in-flight tell append finished.
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from step5d_autotune_v4_r006.contracts import load_contract  # noqa: E402
from step5d_autotune_v4_r006.lattice import ANCHOR_POINT  # noqa: E402
from step5d_autotune_v4_r006.objective import build_receipt_from_samples  # noqa: E402
from step5d_autotune_v4_r006.optimizer_worker import OptimizerWorkerError  # noqa: E402
from step5d_autotune_v4_r006.sidecar import R006ObjectiveSidecar  # noqa: E402
from step5d_autotune_v4_r008.bounded_worker_artifact_binding import (  # noqa: E402
    bounded_artifact_binding,
)
from step5d_autotune_v4_r008.live_adapter import (  # noqa: E402
    R008HostLoop,
    R008ProductionOptimizer,
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


def _append_row(sidecar: R006ObjectiveSidecar, contract, sequence: int) -> None:
    receipt = build_receipt_from_samples(
        _complete_samples(5.25, sequence_offset=sequence * 10_000),
        attempt_sequence=sequence,
        execution_id=f"r008-ask-seal-{sequence}",
        campaign_fingerprint=contract.campaign_fingerprint,
        candidate_uid=ANCHOR_POINT.uid,
    )
    sidecar.append(
        receipt,
        epoch=1,
        candidate_uid=ANCHOR_POINT.uid,
        kind="BO_TRIAL",
        point_key=list(ANCHOR_POINT.key),
    )


def _binding(sidecar: R006ObjectiveSidecar, contract, n: int) -> dict:
    data = sidecar.path.read_bytes()
    return {
        "sidecar_path": str(sidecar.path),
        "sidecar_sha256": hashlib.sha256(data).hexdigest(),
        "sidecar_prefix_bytes": len(data),
        "campaign_fingerprint": contract.campaign_fingerprint,
        "rows": [
            {"attempt_sequence": sequence, "execution_id": f"r008-ask-seal-{sequence}"}
            for sequence in range(1, n + 1)
        ],
    }


def test_unrelated_append_after_binding_sha_no_longer_fails(tmp_path: Path) -> None:
    """2026-08-07 fix: prefix-scoped digest, not whole-file.

    Regression pin for the live TOCTOU race (r006 artifact sidecar bytes
    differ -> AUTHORITY_REVOKED, observed once in 025535_b3_phase5_raw2):
    a binding snapshot must survive an unrelated append after it was taken,
    as long as the rows it actually references are unchanged. This test used
    to assert the opposite (any append after the snapshot fails closed) --
    that was the bug, not a safety property.
    """

    contract = load_contract()
    sidecar = R006ObjectiveSidecar(
        tmp_path / "r006-objectives.jsonl", campaign_fingerprint=contract.campaign_fingerprint
    )
    _append_row(sidecar, contract, 1)
    stale = _binding(sidecar, contract, 1)
    _append_row(sidecar, contract, 2)
    bounded = bounded_artifact_binding(stale, tail_rows=2)
    assert len(bounded["rows"]) == 1


def test_tampering_within_referenced_prefix_still_fails(tmp_path: Path) -> None:
    """Prefix-scoping must not weaken tamper detection for referenced rows."""

    contract = load_contract()
    sidecar = R006ObjectiveSidecar(
        tmp_path / "r006-objectives.jsonl", campaign_fingerprint=contract.campaign_fingerprint
    )
    _append_row(sidecar, contract, 1)
    binding = _binding(sidecar, contract, 1)

    lines = sidecar.path.read_text(encoding="utf-8").splitlines()
    row0 = json.loads(lines[1])
    row0["candidate_uid"] = "tampered"
    lines[1] = json.dumps(row0, sort_keys=True, separators=(",", ":"))
    sidecar.path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    with pytest.raises(OptimizerWorkerError, match="sidecar bytes differ"):
        bounded_artifact_binding(binding, tail_rows=2)


def test_fresh_binding_after_append_passes(tmp_path: Path) -> None:
    contract = load_contract()
    sidecar = R006ObjectiveSidecar(
        tmp_path / "r006-objectives.jsonl", campaign_fingerprint=contract.campaign_fingerprint
    )
    _append_row(sidecar, contract, 1)
    _append_row(sidecar, contract, 2)
    fresh = _binding(sidecar, contract, 2)
    bounded = bounded_artifact_binding(fresh, tail_rows=2)
    assert len(bounded["rows"]) == 2


def test_optimizer_ask_runs_pre_binding_barrier() -> None:
    order: list[str] = []

    class _Client:
        def update_artifact_binding(self, binding):
            order.append("bind")

        def ask(self, **kwargs):
            order.append("ask")

            class _Ask:
                point = ANCHOR_POINT
                metadata = {"ok": True}

            return _Ask()

    class _Ledger:
        def sidecar_binding(self):
            order.append("sidecar_binding")
            return {
                "sidecar_path": "/dev/null",
                "sidecar_sha256": "0" * 64,
                "sidecar_prefix_bytes": 0,
                "campaign_fingerprint": "0" * 64,
                "rows": [],
            }

    opt = object.__new__(R008ProductionOptimizer)
    opt._box = type(
        "Box",
        (),
        {
            "log2_pd_min": -1.0,
            "log2_pd_max": 1.0,
            "log2_kd_min": -1.0,
            "log2_kd_max": 1.0,
            "log2_ki_min": -8.0,
            "log2_ki_max": -2.0,
            "log2_tau_min": -4.0,
            "log2_tau_max": 0.0,
            "log2_kf_min": -2.0,
            "log2_kf_max": 2.0,
            "log2_kp_min": -4.0,
            "log2_kp_max": 0.0,
        },
    )()
    # Minimal box API used by propose_candidates / neighbors — patch ask body
    # by stubbing heavy helpers via a thin override.
    opt._sobol_seed = 0
    opt._ask_count = 0
    opt._include_kf_off_fraction = 0.0
    opt._domain_stiffness_n_per_m = 40000.0
    opt._domain_rho = 1.0
    opt.client = _Client()
    opt.r006_ledger = _Ledger()
    opt.last_ask_metadata = {}

    def barrier():
        order.append("barrier")

    opt._pre_artifact_binding_barrier = barrier

    # Bypass Sobol/neighbors by monkeypatching ask's choice construction:
    # call the binding section directly through a minimal stand-in.
    from step5d_autotune_v4_r008 import live_adapter as mod

    original_propose = mod.propose_candidates
    original_neighbors = mod.neighbors
    original_unstable = mod.live_acquisition_unstable
    original_from_cand = mod._point_from_candidate
    original_from_point = mod._candidate_from_point

    try:
        mod.propose_candidates = lambda *a, **k: (ANCHOR_POINT,)  # type: ignore[assignment]
        mod.neighbors = lambda *a, **k: ()  # type: ignore[assignment]
        mod.live_acquisition_unstable = lambda **k: False  # type: ignore[assignment]
        mod._point_from_candidate = lambda c: ANCHOR_POINT  # type: ignore[assignment]
        mod._candidate_from_point = lambda p: p  # type: ignore[assignment]
        opt.ask(observations=(), pending=(), incumbent=object(), q=1)
    finally:
        mod.propose_candidates = original_propose
        mod.neighbors = original_neighbors
        mod.live_acquisition_unstable = original_unstable
        mod._point_from_candidate = original_from_cand
        mod._candidate_from_point = original_from_point

    assert order[:3] == ["barrier", "sidecar_binding", "bind"]
    assert "ask" in order


def test_join_seals_before_optimizer_ask_is_no_join() -> None:
    """Phase 2: ask must not join_all; BO uses already-sealed sidecar rows only."""

    loop = object.__new__(R008HostLoop)
    loop.events = []
    joined = {"n": 0}

    class _Seal:
        def join_all(self) -> None:
            joined["n"] += 1

    loop._async_seal = _Seal()  # type: ignore[attr-defined]
    loop._join_seals_before_optimizer_ask()
    assert joined["n"] == 0
    assert any(e.startswith("R008_ASK_SEAL_NO_JOIN:") for e in loop.events)


def test_ask_seal_join_helper_exists_in_source() -> None:
    src = Path(ROOT / "tools/step5d_autotune_v4_r008/live_adapter.py").read_text(encoding="utf-8")
    assert "def _join_seals_before_optimizer_ask" in src
    assert "_pre_artifact_binding_barrier" in src
    assert "R008_ASK_SEAL_NO_JOIN" in src
