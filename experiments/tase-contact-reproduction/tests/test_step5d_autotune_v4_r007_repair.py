"""Focused offline regression for the r007 native-ledger seam."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import step5d_runtime_codec  # noqa: E402
from step5d_autotune_v4_r005.runtime import CampaignPhase  # noqa: E402
from step5d_autotune_v4_r006.fake_rtde import TimingRegression  # noqa: E402
from step5d_autotune_v4_r006.motion_profile import (  # noqa: E402
    ACTIVE_MOTION_ENVELOPE_V2,
)
from step5d_autotune_v4_r006.live_adapter import (  # noqa: E402
    R006Candidate,
    R006HostLoop,
    R006LiveAdapterError,
    R006ProductionOptimizer,
)
from step5d_autotune_v4_r007 import timing as r007_timing  # noqa: E402
from step5d_autotune_v4_r007.native_ledger import (  # noqa: E402
    R007NativeObservationLedger,
    open_r007_r006_ledger,
)

R007_CONTRACT_PATH = ROOT / "config/step5d/autotune_v4_r007.json"


LEDGER_PATH = (
    ROOT
    / "runs/step5d_autotune_v4_r006/live_20260802_2258_wire_gate/r006-observations.jsonl"
)
CAMPAIGN_FINGERPRINT = "1db4f9bf587826e7562dccef7040f627e660e540f847a2e8b92f63879870cc4a"
EOAT_SHA256 = "1979482cd8377b92555fcee72f120928838ed8d57fda8af1e6c1aaebc3ebccf4"
SEQ18_KO = 0.08408964152537146


def _native_ledger() -> R007NativeObservationLedger:
    assert LEDGER_PATH.is_file()
    return R007NativeObservationLedger(
        LEDGER_PATH,
        campaign_fingerprint=CAMPAIGN_FINGERPRINT,
        eoat_sha256=EOAT_SHA256,
    )


@dataclass
class _OfflineQueue:
    entries: list[object]

    @property
    def inflight(self) -> None:
        return None

    def pending(self) -> tuple[object, ...]:
        return tuple(self.entries)

    def enqueue(self, candidate: R006Candidate, *, kind: str, epoch: int) -> None:
        assert isinstance(candidate, R006Candidate)
        self.entries.append(type("Entry", (), {"candidate": candidate, "kind": kind})())


def test_seq18_sealed_cold_read_sidecar_tell_and_refill_stay_native() -> None:
    ledger = _native_ledger()
    record = next(row for row in ledger.records if row.attempt_sequence == 18)

    assert record.sealed
    assert isinstance(record.candidate, R006Candidate)
    assert record.candidate.orientation_ko == pytest.approx(SEQ18_KO)
    assert record.candidate.canonical["i_off"] is True
    assert record.candidate.as_point.ko_step == -1

    paired = open_r007_r006_ledger(
        LEDGER_PATH,
        campaign_fingerprint=CAMPAIGN_FINGERPRINT,
        eoat_sha256=EOAT_SHA256,
    )
    sidecar_rows = paired.fresh_process_verify()
    assert any(
        row["attempt_sequence"] == 18
        and row["candidate_uid"] == record.candidate_uid
        for row in sidecar_rows
    )

    optimizer = object.__new__(R006ProductionOptimizer)
    optimizer.tell(record)

    loop = object.__new__(R006HostLoop)
    loop.phase = CampaignPhase.QUALIFICATION
    loop.epoch = 1
    loop.cursor = record.candidate
    loop.qualification_passes = 0
    loop.events = []
    loop.queue = _OfflineQueue([])
    R006HostLoop._refill(loop)

    assert len(loop.queue.entries) == 2
    assert all(isinstance(entry.candidate, R006Candidate) for entry in loop.queue.entries)
    assert loop.queue.entries[0].candidate.as_point == R006Candidate().as_point


@pytest.mark.parametrize(
    "ko",
    [
        0.084,
        float("nan"),
        -0.1,
    ],
    ids=["off_lattice", "nonfinite", "outside_r006_domain"],
)
def test_r006_native_ko_domain_still_rejects(ko: float) -> None:
    candidate = next(
        row.candidate
        for row in _native_ledger().records
        if row.attempt_sequence == 18
    )
    payload = dict(candidate.canonical)
    payload["orientation_ko"] = ko
    with pytest.raises(R006LiveAdapterError):
        R006Candidate.from_canonical(payload)


def test_r007_contract_is_parent_bound_and_repair_module_scope_is_offline() -> None:
    contract = json.loads(R007_CONTRACT_PATH.read_text(encoding="utf-8"))

    assert contract["version"] == "r007-native-ledger-repair-v1"
    assert contract["r006_mutation"] is False
    assert contract["timing"]["average_rate_used_for_eligibility"] is False
    assert contract["timing"]["legacy_average_rate_policy"] == "telemetry_only"
    # The repair modules themselves stay pure; only the host route runs live.
    assert not any(contract["offline_boundary"].values())

    route = contract["activation"]["live_route"]
    assert route["release_contract"] == "config/step5d/autotune_v4_r006.json"
    assert route["campaign_identity"] == "r006_unchanged"
    assert route["controller_package_rotation"] is False
    assert route["host_module"] != route["r006_host_module_untouched"]

    # The declared parent bytes must still be exactly what r006 sealed with.
    assert len(contract["parent_hashes"]) == 5
    for relative, expected in contract["parent_hashes"].items():
        actual = hashlib.sha256((ROOT / relative).read_bytes()).hexdigest()
        assert actual == expected, relative


def test_r007_host_route_activation_fails_closed(tmp_path: Path) -> None:
    import run_step5d_autotune_v4_r007 as r007_host

    assert r007_host.load_r007_activation()["version"] == "r007-native-ledger-repair-v1"

    contract = json.loads(R007_CONTRACT_PATH.read_text(encoding="utf-8"))
    for mutate in (
        lambda doc: doc.update(version="r008-something-else"),
        lambda doc: doc.update(r006_mutation=True),
        lambda doc: doc["activation"].update(live_activation=False),
        lambda doc: doc["parent_hashes"].update(
            {"tools/step5d_runtime_codec.py": "0" * 64}
        ),
    ):
        document = json.loads(json.dumps(contract))
        mutate(document)
        path = tmp_path / "r007.json"
        path.write_text(json.dumps(document), encoding="utf-8")
        with pytest.raises(r007_host.R007HostError):
            r007_host.load_r007_activation(path)


def test_r007_host_route_binds_the_frozen_r006_release() -> None:
    import run_step5d_autotune_v4_r006 as r006_host
    import run_step5d_autotune_v4_r007 as r007_host
    from step5d_managed_runtime import load_runtime_manifest

    manifest = load_runtime_manifest(
        ROOT / "config/step5d/autotune_v4_r007_runtime_manifest.json"
    )
    assert manifest.routes["host"] == "run_step5d_autotune_v4_r007"
    assert str(manifest.release_contract_path) == "config/step5d/autotune_v4_r006.json"
    assert manifest.release_revision == 6

    # r007 reuses the r006 CLI surface verbatim rather than restating flags.
    assert r007_host.main.__module__ == "run_step5d_autotune_v4_r007"
    live_action = next(
        action
        for action in r006_host.build_parser()._subparsers._group_actions  # type: ignore[union-attr]
    )
    assert "live" in live_action.choices


def test_r006_legacy_average_rate_gate_is_left_intact() -> None:
    """r007 relaxes eligibility in its own scope only; r006 keeps its own veto."""

    legacy = TimingRegression(
        duration_s=60.0,
        writer_count=27583,
        rtde_count=27583,
        tp_echo_count=27583,
        writer_rate_hz=459.7167,
        rtde_rate_hz=459.7167,
        echo_backlog=0,
    )
    assert not legacy.passes


def test_r007_timing_reuses_the_real_v3_cadence_predicate() -> None:
    """No offline re-implementation, and profile_id stays execution identity."""

    assert r007_timing.cadence_eligible is step5d_runtime_codec.cadence_eligible
    assert r007_timing.CadenceEvidence is step5d_runtime_codec.CadenceEvidence

    profile_id = ACTIVE_MOTION_ENVELOPE_V2.parent_execution_profile_id
    assert profile_id
    assert profile_id != r007_timing.R007_TIMING_VERSION
