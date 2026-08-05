"""Real os.fork multi-append integration test for Tier-0 r006 parent-cache fix.

Canary live_20260805_011258 failed with ``r006 sidecar row 3 hash chain differs``
because each fork child computed ``previous_sha256`` from a stale parent
``_cached`` that was never refreshed after a successful append. This test
drives two real ``run_in_fork`` appends (no mock) and asserts the on-disk chain.
"""

from __future__ import annotations

import os
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from step5d_autotune_v4_r006.contracts import load_contract  # noqa: E402
from step5d_autotune_v4_r006.lattice import ANCHOR_POINT  # noqa: E402
from step5d_autotune_v4_r006.objective import build_receipt_from_samples  # noqa: E402
from step5d_autotune_v4_r006.sidecar import GENESIS_SHA256, R006ObjectiveSidecar  # noqa: E402
from step5d_autotune_v4_r008.gil_isolation import run_in_fork  # noqa: E402
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


def _receipt(contract, sequence: int, *, force_n: float = 5.25):
    return build_receipt_from_samples(
        _complete_samples(force_n, sequence_offset=sequence * 10_000),
        attempt_sequence=sequence,
        execution_id=f"r008-fork-chain-{sequence}",
        campaign_fingerprint=contract.campaign_fingerprint,
        candidate_uid=ANCHOR_POINT.uid,
    )


@pytest.mark.skipif(not hasattr(os, "fork"), reason="os.fork required")
def test_two_real_fork_appends_chain_when_parent_r006_refreshed(tmp_path: Path) -> None:
    """Parent refresh between forks keeps previous_sha256 linked (011258 fix)."""

    contract = load_contract()
    path = tmp_path / "r006-objectives.jsonl"
    sidecar = R006ObjectiveSidecar(path, campaign_fingerprint=contract.campaign_fingerprint)
    parent_pid = os.getpid()

    def _append_in_child(sequence: int) -> dict[str, str]:
        assert os.getpid() != parent_pid
        row = sidecar.append(
            _receipt(contract, sequence, force_n=5.0 + sequence),
            epoch=1,
            candidate_uid=ANCHOR_POINT.uid,
            kind="SPACEFILL",
            point_key=list(ANCHOR_POINT.key),
        )
        return {
            "previous_sha256": str(row["previous_sha256"]),
            "row_sha256": str(row["row_sha256"]),
            "attempt_sequence": str(row["attempt_sequence"]),
        }

    first = run_in_fork(lambda: _append_in_child(1))
    assert first["previous_sha256"] == GENESIS_SHA256
    # Mirror live_adapter._record_and_tell parent refresh after waitpid.
    sidecar.fresh_process_verify()

    second = run_in_fork(lambda: _append_in_child(2))
    assert second["previous_sha256"] == first["row_sha256"]
    assert second["previous_sha256"] != GENESIS_SHA256

    cold = R006ObjectiveSidecar(path, campaign_fingerprint=contract.campaign_fingerprint)
    rows = cold.fresh_process_verify()
    assert len(rows) == 2
    assert rows[0]["previous_sha256"] == GENESIS_SHA256
    assert rows[1]["previous_sha256"] == rows[0]["row_sha256"]


@pytest.mark.skipif(not hasattr(os, "fork"), reason="os.fork required")
def test_stale_parent_cache_reproduces_011258_genesis_bug(tmp_path: Path) -> None:
    """Without parent refresh, the second fork rewrites previous=GENESIS.

    ``append`` itself cold-verifies after write, so the second fork raises
    inside the child once the broken row is on disk — same failure mode as
    canary 011258. Assert the on-disk previous fields, not a clean return.
    """

    import json

    contract = load_contract()
    path = tmp_path / "r006-objectives.jsonl"
    sidecar = R006ObjectiveSidecar(path, campaign_fingerprint=contract.campaign_fingerprint)
    parent_pid = os.getpid()

    def _append_in_child(sequence: int) -> str:
        assert os.getpid() != parent_pid
        row = sidecar.append(
            _receipt(contract, sequence, force_n=4.0 + sequence),
            epoch=1,
            candidate_uid=ANCHOR_POINT.uid,
            kind="SPACEFILL",
            point_key=list(ANCHOR_POINT.key),
        )
        return str(row["previous_sha256"])

    first_prev = run_in_fork(lambda: _append_in_child(1))
    assert first_prev == GENESIS_SHA256
    # Intentionally skip parent refresh — reproduces canary 011258.
    with pytest.raises(Exception, match="hash chain differs"):
        run_in_fork(lambda: _append_in_child(2))

    rows = [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    objectives = [row for row in rows if row.get("record_type") == "objective_artifact"]
    assert len(objectives) == 2
    assert objectives[0]["previous_sha256"] == GENESIS_SHA256
    assert objectives[1]["previous_sha256"] == GENESIS_SHA256
