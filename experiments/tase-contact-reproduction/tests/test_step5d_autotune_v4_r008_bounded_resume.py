"""Offline tests for the r008 bounded-tail cold-resume ledger (2026-08-03).

Root cause fixed here: ``ObservationLedger.__init__``/``fresh_process_verify``
re-derive every non-QUALIFICATION row's force objective from its raw sample
file on every construction/resume, which grows with total ledger length and
empirically exceeded even the r008-raised 600s budget around ~30 formal rows,
blocking resume of a large campaign entirely (no live motion attempted; it
fails before dispatch). ``R008BoundedResumeObservationLedger`` bounds that
expensive recomputation to the newest few rows and trusts older sealed rows'
own stored fields, which are still protected by the (always-run, cheap)
row hash chain.

No robot I/O; pure offline unit tests using a synthetic ledger built the same
way ``ObservationLedger.append`` builds a real one.
"""

from __future__ import annotations

import json
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from step5d_autotune_v4_r005.observations import (  # noqa: E402
    ObservationError,
    ObservationRecord,
)
from step5d_autotune_v4_r006.live_adapter import R006Candidate  # noqa: E402
from step5d_force_objective import ForcePathSample  # noqa: E402
from step5d_autotune_v4_r008.bounded_resume_ledger import (  # noqa: E402
    R008BoundedResumeObservationLedger,
)


CAMPAIGN = "a" * 64
EOAT = "b" * 64


def _make_ledger(tmp_path: Path) -> R008BoundedResumeObservationLedger:
    return R008BoundedResumeObservationLedger(
        tmp_path / "r006-observations.jsonl",
        campaign_fingerprint=CAMPAIGN,
        eoat_sha256=EOAT,
    )


def _append_qualification(ledger: R008BoundedResumeObservationLedger, sequence: int) -> None:
    record = ObservationRecord(
        campaign_fingerprint=CAMPAIGN,
        epoch=1,
        attempt_sequence=sequence,
        kind="QUALIFICATION",
        candidate=R006Candidate(),
        safe_return=True,
        binding_ok=True,
        safety_gate=True,
        contact_gate=True,
        return_gate=True,
        motion_gate=False,
        timing_gate=True,
        identity_gate=True,
        qualification_passed=True,
        duration_s=0.0,
        metrics={"execution_id": f"exec-{sequence}", "outcome": "SAFE_NONTRAINABLE"},
    )
    ledger.append(record)


def _append_formal(
    ledger: R008BoundedResumeObservationLedger,
    sequence: int,
    *,
    kind: str = "STAIRCASE",
) -> None:
    dt = 0.1
    n_bins = 550
    samples = []
    t = 5.0
    for i in range(n_bins):
        samples.append(
            ForcePathSample(
                path_time_s=t,
                filtered_normal_n=5.0,
                source_sequences={"kunwei": i + 1},
                source_ages_s={"kunwei": 0.001},
                stage=25,
            )
        )
        t += dt
    record = ObservationRecord(
        campaign_fingerprint=CAMPAIGN,
        epoch=1,
        attempt_sequence=sequence,
        kind=kind,
        candidate=R006Candidate(),
        safe_return=True,
        binding_ok=True,
        safety_gate=True,
        contact_gate=True,
        return_gate=True,
        motion_gate=True,
        timing_gate=True,
        identity_gate=True,
        qualification_passed=True,
        duration_s=60.0,
        metrics={"execution_id": f"exec-{sequence}"},
        raw_path_samples=tuple(samples),
    )
    ledger.append(record)


def test_bounded_resume_reconstructs_all_records(tmp_path: Path) -> None:
    ledger = _make_ledger(tmp_path)
    for seq in range(1, 4):
        _append_qualification(ledger, seq)
    for seq in range(4, 12):
        _append_formal(ledger, seq)

    reopened = R008BoundedResumeObservationLedger(
        tmp_path / "r006-observations.jsonl",
        campaign_fingerprint=CAMPAIGN,
        eoat_sha256=EOAT,
        tail_rows=3,
    )
    assert len(reopened.records) == 11
    assert reopened.records[-1].attempt_sequence == 11
    assert reopened.records[-1].kind == "STAIRCASE"


def test_bounded_resume_detects_tampered_old_row(tmp_path: Path) -> None:
    ledger = _make_ledger(tmp_path)
    for seq in range(1, 4):
        _append_qualification(ledger, seq)
    for seq in range(4, 12):
        _append_formal(ledger, seq)
    del ledger

    path = tmp_path / "r006-observations.jsonl"
    lines = path.read_text().splitlines()
    row = json.loads(lines[4])  # an old, non-tail formal row
    row["mae_n"] = 0.001
    lines[4] = json.dumps(row, sort_keys=True, separators=(",", ":"))
    path.write_text("\n".join(lines) + "\n")

    with pytest.raises(ObservationError):
        R008BoundedResumeObservationLedger(
            path, campaign_fingerprint=CAMPAIGN, eoat_sha256=EOAT, tail_rows=3,
        )


def test_bounded_resume_rejects_nonpositive_tail_rows(tmp_path: Path) -> None:
    ledger = _make_ledger(tmp_path)
    for seq in range(1, 4):
        _append_qualification(ledger, seq)
    del ledger

    with pytest.raises(ObservationError):
        R008BoundedResumeObservationLedger(
            tmp_path / "r006-observations.jsonl",
            campaign_fingerprint=CAMPAIGN,
            eoat_sha256=EOAT,
            tail_rows=0,
        )
