"""Offline tests for the r008 bounded sidecar cold-read patch (2026-08-03).

Root cause fixed here: ``R006ObjectiveSidecar.append`` re-verifies every
historical row (one subprocess per row) on every single new append, so
per-attempt overhead grows with total campaign length even though older rows
were already verified once. Live evidence: the wall-clock gap between
consecutive formal trials grew from ~156s to ~307s over 30 trials in
``live_20260803_1113_stage_d`` while the formal window itself stayed a fixed
60s -- the growth was pure per-append re-verification cost. This bounds that
cost to the newest few rows.

No robot I/O; uses real (cheap) sidecar receipts, so genuine subprocesses are
spawned (small JSON, not the large raw force artifacts) -- this is
representative of the actual code path, just call-counted.
"""

from __future__ import annotations

from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from step5d_autotune_v4_r006.contracts import load_contract  # noqa: E402
from step5d_autotune_v4_r006.lattice import ANCHOR_POINT  # noqa: E402
from step5d_autotune_v4_r006.objective import build_receipt_from_samples  # noqa: E402
from step5d_autotune_v4_r006.sidecar import R006ObjectiveSidecar  # noqa: E402
from step5d_autotune_v4_r008.bounded_sidecar_verify import (  # noqa: E402
    R008_SIDECAR_TAIL_ROWS,
    r008_bounded_sidecar_scope,
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


def _receipt(contract, sequence: int, *, force_n: float = 5.25):
    return build_receipt_from_samples(
        _complete_samples(force_n, sequence_offset=sequence * 10_000),
        attempt_sequence=sequence,
        execution_id=f"r008-bounded-sidecar-test-{sequence}",
        campaign_fingerprint=contract.campaign_fingerprint,
        candidate_uid=ANCHOR_POINT.uid,
    )


def _append_n(sidecar: R006ObjectiveSidecar, contract, n: int, *, start: int = 1) -> None:
    for sequence in range(start, start + n):
        sidecar.append(
            _receipt(contract, sequence),
            epoch=1,
            candidate_uid=ANCHOR_POINT.uid,
            kind="SPACEFILL",
            point_key=list(ANCHOR_POINT.key),
        )


def test_bounded_sidecar_bounds_subprocess_calls_on_append(tmp_path: Path, monkeypatch) -> None:
    contract = load_contract()
    call_count = {"n": 0}

    from step5d_autotune_v4_r006 import sidecar as mod

    with r008_bounded_sidecar_scope(tail_rows=3):
        # Patch after scope installs the r008 fresh-verify (in-process / binary).
        original = mod._fresh_verify_artifact

        def counting_fresh_verify(*args, **kwargs):
            call_count["n"] += 1
            return original(*args, **kwargs)

        monkeypatch.setattr(mod, "_fresh_verify_artifact", counting_fresh_verify)
        sidecar = R006ObjectiveSidecar(
            tmp_path / "r006-objectives.jsonl", campaign_fingerprint=contract.campaign_fingerprint,
        )
        _append_n(sidecar, contract, 10)
        calls_after_ten = call_count["n"]
        call_count["n"] = 0
        _append_n(sidecar, contract, 1, start=11)
        calls_for_eleventh_append = call_count["n"]

    # Append fast-path (2026-08-05): one fresh-verify for the new row only —
    # no trailing _verify_rows(cold_read=True) re-spawn on the same artifact.
    assert calls_for_eleventh_append == 1
    assert calls_for_eleventh_append < calls_after_ten


def test_bounded_sidecar_bounds_subprocess_calls_on_cold_construction(
    tmp_path: Path, monkeypatch
) -> None:
    """Regression for the 2026-08-03 evening cold-resume stall: a brand-new
    process has no warm ``_cached`` yet, so the first ``fresh_process_verify()``
    call must still only fresh-verify the tail, not silently fall through to
    the expensive subprocess path for every historical row."""

    contract = load_contract()
    call_count = {"n": 0}

    from step5d_autotune_v4_r006 import sidecar as mod

    path = tmp_path / "r006-objectives.jsonl"
    with r008_bounded_sidecar_scope(tail_rows=3):
        original = mod._fresh_verify_artifact

        def counting_fresh_verify(*args, **kwargs):
            call_count["n"] += 1
            return original(*args, **kwargs)

        monkeypatch.setattr(mod, "_fresh_verify_artifact", counting_fresh_verify)
        warm_sidecar = R006ObjectiveSidecar(path, campaign_fingerprint=contract.campaign_fingerprint)
        _append_n(warm_sidecar, contract, 10)

        call_count["n"] = 0
        # A brand-new sidecar object over the same file: no self._cached yet,
        # exactly like a fresh live-host process resuming a large campaign.
        # R006ObjectiveSidecar.__init__ itself calls _verify_rows(cold_read=
        # True) twice before self._cached is ever assigned (once to validate
        # the file, once to populate the cache; frozen r006 code, not
        # touched here) -- each of those, plus the explicit call below, pays
        # the tail bound once: 3 cycles x tail_rows=3 = 9. Without this fix
        # it would instead be 3 cycles x all 10 rows = 30, and that 3x grows
        # with total campaign length, not just tail_rows.
        cold_sidecar = R006ObjectiveSidecar(path, campaign_fingerprint=contract.campaign_fingerprint)
        cold_rows = cold_sidecar.fresh_process_verify()

    assert call_count["n"] <= 9
    assert len(cold_rows) == 10
    assert all(row["trainable"] for row in cold_rows)


def test_bounded_sidecar_reconstructs_same_rows_as_unbounded(tmp_path: Path) -> None:
    contract = load_contract()
    with r008_bounded_sidecar_scope(tail_rows=3):
        sidecar = R006ObjectiveSidecar(
            tmp_path / "r006-objectives.jsonl", campaign_fingerprint=contract.campaign_fingerprint,
        )
        _append_n(sidecar, contract, 6)
        bounded_rows = sidecar.fresh_process_verify()

    unbounded_sidecar = R006ObjectiveSidecar(
        tmp_path / "r006-objectives.jsonl", campaign_fingerprint=contract.campaign_fingerprint,
    )
    unbounded_rows = unbounded_sidecar.fresh_process_verify()

    assert len(bounded_rows) == len(unbounded_rows) == 6
    for bounded, unbounded in zip(bounded_rows, unbounded_rows):
        assert bounded["attempt_sequence"] == unbounded["attempt_sequence"]
        assert bounded["objective_mae_n"] == unbounded["objective_mae_n"]
        assert bounded["trainable"] == unbounded["trainable"]


def test_bounded_sidecar_scope_is_removed_after_exit(tmp_path: Path) -> None:
    from step5d_autotune_v4_r006 import sidecar as mod

    original = mod.R006ObjectiveSidecar._verify_rows
    with r008_bounded_sidecar_scope():
        assert mod.R006ObjectiveSidecar._verify_rows is not original
    assert mod.R006ObjectiveSidecar._verify_rows is original


def test_default_tail_rows_is_one() -> None:
    assert R008_SIDECAR_TAIL_ROWS == 1


def test_bounded_append_single_fresh_verify_keeps_trainable(
    tmp_path: Path, monkeypatch
) -> None:
    """Append fast-path: one subprocess verify; cache keeps trainable=True."""

    contract = load_contract()
    call_count = {"n": 0}
    from step5d_autotune_v4_r006 import sidecar as mod

    with r008_bounded_sidecar_scope(tail_rows=1):
        original = mod._fresh_verify_artifact

        def counting_fresh_verify(*args, **kwargs):
            call_count["n"] += 1
            return original(*args, **kwargs)

        monkeypatch.setattr(mod, "_fresh_verify_artifact", counting_fresh_verify)
        sidecar = R006ObjectiveSidecar(
            tmp_path / "r006-objectives.jsonl",
            campaign_fingerprint=contract.campaign_fingerprint,
        )
        call_count["n"] = 0
        _append_n(sidecar, contract, 1)
        assert call_count["n"] == 1
        assert sidecar.rows[-1]["trainable"] is True
        assert sidecar.rows[-1]["attempt_sequence"] == 1


def test_tail_rows_one_second_cold_verify_only_newest(
    tmp_path: Path, monkeypatch
) -> None:
    """With tail_rows=1, a second cold verify on a warm cache fresh-verifies
    only the newest row; older rows reuse ``cached_by_identity``."""

    contract = load_contract()
    call_count = {"n": 0}
    from step5d_autotune_v4_r006 import sidecar as mod

    with r008_bounded_sidecar_scope(tail_rows=1):
        original = mod._fresh_verify_artifact

        def counting_fresh_verify(*args, **kwargs):
            call_count["n"] += 1
            return original(*args, **kwargs)

        monkeypatch.setattr(mod, "_fresh_verify_artifact", counting_fresh_verify)
        sidecar = R006ObjectiveSidecar(
            tmp_path / "r006-objectives.jsonl",
            campaign_fingerprint=contract.campaign_fingerprint,
        )
        _append_n(sidecar, contract, 6)
        call_count["n"] = 0
        rows = sidecar._verify_rows(cold_read=True)
        assert len(rows) == 6
        assert call_count["n"] == 1
        call_count["n"] = 0
        rows2 = sidecar._verify_rows(cold_read=True)
        assert len(rows2) == 6
        assert call_count["n"] == 1
        assert rows2[-1]["attempt_sequence"] == 6
