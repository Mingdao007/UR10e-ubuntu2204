from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from step5d_autotune_v2.model import (
    AttemptTupleSpec,
    BatchSpec,
    CandidateSpec,
    DeploymentSpec,
    ModelError,
)
from step5d_autotune_v2.reducer import (
    LifecycleError,
    LifecycleEvent,
    LifecycleSnapshot,
    LifecycleState,
    reduce_lifecycle,
)
from step5d_autotune_v2.repository import Repository, RepositoryError
from step5d_autotune_v2.config import load_static_config
from step5d_autotune_v2.readiness import evaluate_preflight


MULTIPLIERS = ("10", "50", "100", "500", "1000")


def candidates(start: int = 1) -> tuple[CandidateSpec, ...]:
    return tuple(
        CandidateSpec.from_mapping(
            {
                "group_id": f"G{start + index}",
                "log2_p": "0.75",
                "log2_d": "0.25",
                "i_multiplier": multiplier,
            }
        )
        for index, multiplier in enumerate(MULTIPLIERS)
    )


def deployment(identity: str = "test-deployment") -> DeploymentSpec:
    return DeploymentSpec(
        deployment_id=identity,
        code_fingerprint="a" * 64,
        tp_fingerprint="b" * 64,
        guard_fingerprint="c" * 64,
        profile={
            "normal_max_rate_rad_s": "0.05",
            "host_qdot_slew_rad_s2": "0.5",
            "tp_speedj_accel_rad_s2": "0.5",
            "qdot_cap_rad_s": "0.5",
        },
        deployment_authorized=True,
        controller_readback_verified=True,
    )


def test_log2_and_coarse_i_are_canonical_decimal_text() -> None:
    row = candidates(10)[0]
    assert row.p == "0.001681792830507429"
    assert row.i == "0.0001"
    assert row.d == "8.324449805019047"
    assert row.log2_p == "0.75"
    with pytest.raises(ModelError, match="not a float"):
        CandidateSpec.from_mapping(
            {"group_id": "G20", "p": 0.001, "i": "0.00001", "d": "7"}
        )


def test_declared_coordinates_must_match_gains_and_stay_in_envelope() -> None:
    with pytest.raises(ModelError, match="does not match.*log2_p"):
        CandidateSpec.from_mapping(
            {
                "group_id": "G20",
                "p": "0.001",
                "log2_p": "0.25",
                "i": "0.00001",
                "log2_i": "0",
                "d": "7",
                "log2_d": "0",
            }
        )
    with pytest.raises(ModelError, match=r"inside \[-1,1\]"):
        CandidateSpec.from_mapping(
            {
                "group_id": "G20",
                "log2_p": "1.25",
                "log2_i": "0",
                "log2_d": "0",
            }
        )
    refined = CandidateSpec.from_mapping(
        {
            "group_id": "G20",
            "log2_p": "0.75",
            "log2_i": "10",
            "log2_d": "0.25",
        }
    )
    assert refined.i == "0.01024"


def test_normal_batch_is_five_and_bounded_recovery_is_four() -> None:
    BatchSpec("batch", "new five", candidates())
    with pytest.raises(ModelError, match="exactly 5"):
        BatchSpec("bad", "too short", candidates()[:4])
    BatchSpec(
        "recovery",
        "G11 already complete",
        candidates(12)[:4],
        recovery=True,
        completed_groups=("G11",),
    )
    rows = []
    for candidate in candidates():
        row = candidate.as_dict()
        row.pop("comparison_key")
        rows.append(row)
    with pytest.raises(ModelError, match="recovery must be a boolean"):
        BatchSpec.from_mapping(
            {
                "schema": "step5d.autotune.batch/v2",
                "batch_id": "bad-recovery-type",
                "source": "schema test",
                "recovery": "false",
                "candidates": rows,
            }
        )


def test_recovery_batch_requires_completed_g11_in_durable_state(tmp_path: Path) -> None:
    repository = Repository((tmp_path / "campaign.sqlite3").resolve())
    repository.initialize()
    recovery = BatchSpec(
        "recovery",
        "G11 already complete",
        candidates(12)[:4],
        recovery=True,
        completed_groups=("G11",),
    )
    with pytest.raises(RepositoryError, match="durable completed G11"):
        repository.enqueue_batch(recovery)


def test_reducer_enforces_raw_seal_before_ack_and_analysis_after_closure() -> None:
    snapshot = LifecycleSnapshot()
    snapshot = reduce_lifecycle(snapshot, LifecycleEvent.PERSIST_ARM)
    snapshot = reduce_lifecycle(snapshot, LifecycleEvent.PUBLISH_COMMAND)
    snapshot = reduce_lifecycle(snapshot, LifecycleEvent.OBSERVE_TP_CONSUMED)
    snapshot = reduce_lifecycle(snapshot, LifecycleEvent.OBSERVE_RUN)
    snapshot = reduce_lifecycle(
        snapshot, LifecycleEvent.VERIFY_HOME, {"safe_home_verified": True}
    )
    with pytest.raises(LifecycleError, match="requires raw_sealed"):
        reduce_lifecycle(snapshot, LifecycleEvent.PERSIST_ACK)
    snapshot = reduce_lifecycle(
        snapshot, LifecycleEvent.SEAL_RAW, {"artifact_sha256": "a" * 64}
    )
    for event in (
        LifecycleEvent.PERSIST_ACK,
        LifecycleEvent.PUBLISH_ACK,
        LifecycleEvent.OBSERVE_READY_HOME,
        LifecycleEvent.CLOSE_PHYSICAL,
    ):
        snapshot = reduce_lifecycle(snapshot, event)
    assert snapshot.physical_closed is True
    failed = reduce_lifecycle(
        snapshot, LifecycleEvent.ANALYSIS_FAILED, {"reason": "png failed"}
    )
    assert failed.state is LifecycleState.ANALYSIS_FAILED
    assert failed.ack_published is True


def test_repository_wal_event_chain_and_global_tuple_dedup(tmp_path: Path) -> None:
    repository = Repository((tmp_path / "campaign.sqlite3").resolve())
    repository.initialize()
    repository.register_deployment(deployment())
    repository.enqueue_batch(BatchSpec("b1", "first", candidates()))
    with sqlite3.connect(repository.path) as connection:
        assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    duplicate = list(candidates(20))
    with pytest.raises(RepositoryError, match="append-only uniqueness"):
        repository.enqueue_batch(BatchSpec("b2", "duplicates", tuple(duplicate)))
    assert repository.latest_batch_id() == "b1"
    repository.integrity_check()
    with sqlite3.connect(repository.path) as connection:
        connection.execute("UPDATE events SET payload_json='{}' WHERE event_id=1")
    with pytest.raises(RepositoryError, match="content hash"):
        repository.integrity_check()


def test_parameter_enqueue_does_not_mutate_deployment_identity(tmp_path: Path) -> None:
    repository = Repository((tmp_path / "campaign.sqlite3").resolve())
    repository.initialize()
    repository.register_deployment(deployment())
    with sqlite3.connect(repository.path) as connection:
        before = connection.execute(
            "SELECT code_fingerprint,tp_fingerprint,guard_fingerprint FROM deployments"
        ).fetchall()
    repository.enqueue_batch(BatchSpec("b1", "parameter only", candidates()))
    with sqlite3.connect(repository.path) as connection:
        after = connection.execute(
            "SELECT code_fingerprint,tp_fingerprint,guard_fingerprint FROM deployments"
        ).fetchall()
    assert after == before


def test_replay_is_separate_and_cannot_replace_search_identity(tmp_path: Path) -> None:
    repository = Repository((tmp_path / "campaign.sqlite3").resolve())
    repository.initialize()
    repository.enqueue_batch(BatchSpec("b1", "first", candidates(10)))
    replay = repository.create_replay(group_id="G10", reason="video", nonce="nonce-1")
    assert replay.purpose == "replay"
    assert replay.comparison_key == candidates(10)[0].comparison_key
    assert len(repository.list_candidates()) == 6
    assert repository.next_pending_candidate()["candidate_id"] == replay.candidate_id
    assert repository.status()["next_candidate"]["purpose"] == "replay"


def test_writer_lease_rejects_a_second_live_owner(tmp_path: Path) -> None:
    repository = Repository((tmp_path / "campaign.sqlite3").resolve())
    repository.initialize()
    repository.claim_writer(token="owner-a")
    with pytest.raises(RepositoryError, match="another live writer"):
        repository.claim_writer(token="owner-b")
    repository.release_writer("owner-a")
    repository.claim_writer(token="owner-b")


def test_dead_same_host_writer_can_be_recovered_without_freshness_delay(
    tmp_path: Path,
) -> None:
    repository = Repository((tmp_path / "campaign.sqlite3").resolve())
    repository.initialize()
    repository.claim_writer(token="dead-owner")
    with sqlite3.connect(repository.path) as connection:
        connection.execute(
            "UPDATE writer_lease SET pid=? WHERE singleton=1", (2_147_483_647,)
        )
    repository.claim_writer(token="replacement", stale_after_s=60)
    repository.release_writer("replacement")


def test_imported_partial_attempt_marks_a_mapped_pending_tuple_nonrepeatable(
    tmp_path: Path,
) -> None:
    repository = Repository((tmp_path / "campaign.sqlite3").resolve())
    repository.initialize()
    planned = candidates(10)
    repository.enqueue_batch(BatchSpec("b1", "mapped pending", planned))
    evidence = tmp_path / "capture.csv.part"
    evidence.write_text("partial\n", encoding="utf-8")
    retained = repository.add_attempt_tombstone(
        candidate=AttemptTupleSpec(planned[0].p, planned[0].i, planned[0].d),
        disposition="uncertain_attempt",
        evidence_path=evidence,
        evidence_sha256="d" * 64,
    )
    assert retained is True
    mapped = repository.candidate(planned[0].candidate_id)
    assert mapped["status"] == "uncertain_attempt"
    assert mapped["physical_attempted_at"] is not None
    assert repository.next_pending_candidate()["group_id"] == "G11"


def test_static_authorization_never_implies_fresh_runtime_ready(tmp_path: Path) -> None:
    config = load_static_config(ROOT)
    assert config.payload["controller"]["host"] == "192.168.1.18"
    assert config.payload["controller"]["required_ports"] == [29999, 30004]
    repository = Repository((tmp_path / "campaign.sqlite3").resolve())
    repository.initialize()
    repository.register_deployment(config.deployment)
    report = evaluate_preflight(config, repository)
    assert report.deployment_authorized is False
    assert report.ready_to_launch is False
    assert report.primary_blocker == "tp_v2_controller_readback_missing"
    repository.set_runtime_status(
        deployment_authorized=False,
        runtime_ready=False,
        primary_blocker=report.primary_blocker,
        details=report.details,
    )
    assert repository.status()["runtime_ready"] is False


def test_every_valid_lifecycle_prefix_roundtrips_as_json() -> None:
    events = (
        (LifecycleEvent.PERSIST_ARM, {}),
        (LifecycleEvent.PUBLISH_COMMAND, {}),
        (LifecycleEvent.OBSERVE_TP_CONSUMED, {}),
        (LifecycleEvent.OBSERVE_RUN, {}),
        (LifecycleEvent.VERIFY_HOME, {"safe_home_verified": True}),
        (LifecycleEvent.SEAL_RAW, {"artifact_sha256": "a" * 64}),
        (LifecycleEvent.PERSIST_ACK, {}),
        (LifecycleEvent.PUBLISH_ACK, {}),
        (LifecycleEvent.OBSERVE_READY_HOME, {}),
        (LifecycleEvent.CLOSE_PHYSICAL, {}),
    )
    snapshot = LifecycleSnapshot()
    for event, payload in events:
        snapshot = reduce_lifecycle(snapshot, event, payload)
        snapshot = LifecycleSnapshot.from_mapping(json.loads(json.dumps(snapshot.as_dict())))
    assert snapshot.state is LifecycleState.PHYSICAL_CLOSED
