from __future__ import annotations

import hashlib
import sqlite3
import sys
from pathlib import Path
from typing import Any, Mapping

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from step5d_autotune_v2.model import BatchSpec, CandidateSpec, DeploymentSpec
from step5d_autotune_v2.repository import Repository
from step5d_autotune_v2.supervisor import (
    AnalysisResult,
    ArtifactSeal,
    CampaignSupervisor,
    RuntimeFailure,
)


def deployment() -> DeploymentSpec:
    return DeploymentSpec(
        deployment_id="fake-live",
        code_fingerprint="1" * 64,
        tp_fingerprint="2" * 64,
        guard_fingerprint="3" * 64,
        profile={
            "normal_max_rate_rad_s": "0.05",
            "host_qdot_slew_rad_s2": "0.5",
            "tp_speedj_accel_rad_s2": "0.5",
            "qdot_cap_rad_s": "0.5",
        },
        deployment_authorized=True,
        controller_readback_verified=True,
    )


def batch() -> BatchSpec:
    return BatchSpec(
        "fake-five",
        "one Play five candidates",
        tuple(
            CandidateSpec.from_mapping(
                {
                    "group_id": f"G{10 + index}",
                    "log2_p": "0.75",
                    "log2_d": "0.25",
                    "i_multiplier": multiplier,
                }
            )
            for index, multiplier in enumerate(("10", "50", "100", "500", "1000"))
        ),
    )


class FakePort:
    def __init__(self, root: Path, *, fail_analysis_at: int | None = None) -> None:
        self.root = root
        self.fail_analysis_at = fail_analysis_at
        self.arm_ids: list[str] = []
        self.arm_sequences: list[int] = []
        self.acks: list[str] = []
        self.ack_sequences: list[int] = []
        self.analysis_count = 0

    def publish_arm(
        self, *, trial_id: str, sequence: int, candidate: Mapping[str, Any]
    ) -> Mapping[str, Any]:
        self.arm_ids.append(trial_id)
        self.arm_sequences.append(sequence)
        return {"sequence": sequence}

    def wait_tp_consumed(self, *, trial_id: str) -> Mapping[str, Any]:
        return {"consumed": True}

    def wait_run_started(self, *, trial_id: str) -> Mapping[str, Any]:
        return {"running": True}

    def wait_home_verified(self, *, trial_id: str) -> Mapping[str, Any]:
        return {"measured_home": True}

    def seal_raw(self, *, trial_id: str) -> ArtifactSeal:
        path = self.root / f"{trial_id}.raw"
        path.write_bytes(trial_id.encode("ascii"))
        return ArtifactSeal(path, hashlib.sha256(path.read_bytes()).hexdigest())

    def publish_ack(
        self, *, trial_id: str, sequence: int, artifact: ArtifactSeal
    ) -> Mapping[str, Any]:
        self.acks.append(trial_id)
        self.ack_sequences.append(sequence)
        return {"ack": True, "sequence": sequence}

    def wait_ready_home(self, *, trial_id: str) -> Mapping[str, Any]:
        return {"ready_home": True}

    def analyze(self, *, trial_id: str, artifact: ArtifactSeal) -> AnalysisResult:
        self.analysis_count += 1
        if self.analysis_count == self.fail_analysis_at:
            raise RuntimeError("plot failed")
        index = self.analysis_count
        return AnalysisResult(
            metrics={
                "force_mae_n": 0.5 + index / 100,
                "complete_bins": 550,
                "correlation": 0.98,
                "lag_s": 0.01,
                "nrmse": 0.04,
                "orientation_p95_rad": 0.02,
                "orientation_max_rad": 0.03,
                "normal_filter_saturation": 0.1,
                "tp_accel_saturation": 0.08,
                "host_slew_saturation": 0.0,
                "safe_closure": True,
                "eligible_objective": False,
            },
            diagnostic_eligible=True,
            objective_eligible=False,
        )


def repository(tmp_path: Path) -> Repository:
    result = Repository((tmp_path / "campaign.sqlite3").resolve())
    result.initialize()
    result.register_deployment(deployment())
    result.enqueue_batch(batch())
    return result


class SimulatedProcessCrash(BaseException):
    pass


class CrashAfterPort:
    """Crash after a runtime side effect but before the matching DB transition."""

    def __init__(self, delegate: FakePort, crash_after: str) -> None:
        self.delegate = delegate
        self.crash_after = crash_after

    def _return_or_crash(self, operation: str, value: Any) -> Any:
        if operation == self.crash_after:
            raise SimulatedProcessCrash(operation)
        return value

    def publish_arm(
        self, *, trial_id: str, sequence: int, candidate: Mapping[str, Any]
    ) -> Mapping[str, Any]:
        return self._return_or_crash(
            "publish_arm",
            self.delegate.publish_arm(
                trial_id=trial_id, sequence=sequence, candidate=candidate
            ),
        )

    def wait_tp_consumed(self, *, trial_id: str) -> Mapping[str, Any]:
        return self._return_or_crash(
            "wait_tp_consumed", self.delegate.wait_tp_consumed(trial_id=trial_id)
        )

    def wait_run_started(self, *, trial_id: str) -> Mapping[str, Any]:
        return self._return_or_crash(
            "wait_run_started", self.delegate.wait_run_started(trial_id=trial_id)
        )

    def wait_home_verified(self, *, trial_id: str) -> Mapping[str, Any]:
        return self._return_or_crash(
            "wait_home_verified", self.delegate.wait_home_verified(trial_id=trial_id)
        )

    def seal_raw(self, *, trial_id: str) -> ArtifactSeal:
        return self._return_or_crash(
            "seal_raw", self.delegate.seal_raw(trial_id=trial_id)
        )

    def publish_ack(
        self, *, trial_id: str, sequence: int, artifact: ArtifactSeal
    ) -> Mapping[str, Any]:
        return self._return_or_crash(
            "publish_ack",
            self.delegate.publish_ack(
                trial_id=trial_id, sequence=sequence, artifact=artifact
            ),
        )

    def wait_ready_home(self, *, trial_id: str) -> Mapping[str, Any]:
        return self._return_or_crash(
            "wait_ready_home", self.delegate.wait_ready_home(trial_id=trial_id)
        )

    def analyze(self, *, trial_id: str, artifact: ArtifactSeal) -> AnalysisResult:
        return self._return_or_crash(
            "analyze", self.delegate.analyze(trial_id=trial_id, artifact=artifact)
        )


def test_one_service_session_advances_five_distinct_candidates(tmp_path: Path) -> None:
    repo = repository(tmp_path)
    port = FakePort(tmp_path)
    outcomes = CampaignSupervisor(repo, port).run_pending(deployment_id="fake-live")
    assert len(outcomes) == 5
    assert len(set(port.arm_ids)) == 5
    assert len(port.acks) == 5
    assert all(outcome.state == "complete" for outcome in outcomes)
    assert all(row["status"] == "complete" for row in repo.list_candidates())


def test_concurrent_bridge_revocation_and_waiter_failure_coalesce(tmp_path: Path) -> None:
    repo = repository(tmp_path)

    class RevokingPort(FakePort):
        def wait_tp_consumed(self, *, trial_id: str) -> Mapping[str, Any]:
            repo.revoke_runtime_for_bridge_loss(
                deployment_authorized=True,
                primary_blocker="bridge_liveness_lost",
                details={"error": "health snapshot race"},
            )
            raise RuntimeFailure("bridge heartbeat is unhealthy")

    outcomes = CampaignSupervisor(repo, RevokingPort(tmp_path)).run_pending(
        deployment_id="fake-live"
    )
    assert len(outcomes) == 1
    assert outcomes[0].state == "uncertain_attempt"
    assert outcomes[0].primary_blocker == "bridge_liveness_lost"


def test_postprocess_failure_occurs_after_ack_and_pauses_next_candidate(tmp_path: Path) -> None:
    repo = repository(tmp_path)
    port = FakePort(tmp_path, fail_analysis_at=1)
    outcomes = CampaignSupervisor(repo, port).run_pending(deployment_id="fake-live")
    assert len(outcomes) == 1
    assert outcomes[0].state == "analysis_failed"
    assert outcomes[0].physical_closed is True
    assert port.acks == [outcomes[0].trial_id]
    assert sum(row["status"] == "pending" for row in repo.list_candidates()) == 4


def test_transfer_failure_is_retryable_and_does_not_change_trial_completion(tmp_path: Path) -> None:
    repo = repository(tmp_path)
    outcome = CampaignSupervisor(repo, FakePort(tmp_path)).run_pending(
        deployment_id="fake-live", maximum=1
    )[0]
    with sqlite3.connect(repo.path) as connection:
        artifact_id = connection.execute(
            "SELECT artifact_id FROM artifacts WHERE trial_id=?", (outcome.trial_id,)
        ).fetchone()[0]
    transfer_id = repo.queue_transfer(artifact_id=artifact_id, destination="mac://plots")
    repo.record_transfer_attempt(transfer_id, success=False, error="offline")
    assert repo.pending_transfers()[0]["status"] == "retry_pending"
    assert repo.trial_detail(outcome.trial_id)["state"] == "complete"


def test_diagnostic_and_official_objective_incumbents_are_separate(tmp_path: Path) -> None:
    repo = repository(tmp_path)
    CampaignSupervisor(repo, FakePort(tmp_path)).run_pending(
        deployment_id="fake-live", maximum=1
    )

    class OfficialPort(FakePort):
        def analyze(self, *, trial_id: str, artifact: ArtifactSeal) -> AnalysisResult:
            return AnalysisResult(
                metrics={
                    "force_mae_n": 0.9,
                    "objective_mae_n": 0.4,
                    "complete_bins": 550,
                    "safe_closure": True,
                    "eligible_objective": True,
                },
                diagnostic_eligible=True,
                objective_eligible=True,
            )

    CampaignSupervisor(repo, OfficialPort(tmp_path)).run_pending(
        deployment_id="fake-live", maximum=1
    )
    assert repo.diagnostic_incumbent(
        deployment_id="fake-live", profile_id=deployment().profile_id
    )["group_id"] == "G10"
    assert repo.objective_incumbent(
        deployment_id="fake-live", profile_id=deployment().profile_id
    )["group_id"] == "G11"


@pytest.mark.parametrize(
    "crash_after",
    (
        "publish_arm",
        "wait_tp_consumed",
        "wait_run_started",
        "wait_home_verified",
        "seal_raw",
        "publish_ack",
        "wait_ready_home",
        "analyze",
    ),
)
def test_restart_resumes_every_external_crash_cut_without_new_trial(
    tmp_path: Path, crash_after: str
) -> None:
    repo = repository(tmp_path)
    first_port = FakePort(tmp_path)
    with pytest.raises(SimulatedProcessCrash, match=crash_after):
        CampaignSupervisor(repo, CrashAfterPort(first_port, crash_after)).run_pending(
            deployment_id="fake-live", maximum=1
        )

    trial_id = repo.active_trial_id()
    assert trial_id is not None
    persisted = repo.trial_detail(trial_id)
    recovered_port = FakePort(tmp_path)
    outcome = CampaignSupervisor(repo, recovered_port).run_pending(
        deployment_id="fake-live", maximum=1
    )[0]

    assert outcome.trial_id == trial_id
    assert outcome.state == "complete"
    with sqlite3.connect(repo.path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM trials").fetchone()[0] == 1
    all_arm_sequences = first_port.arm_sequences + recovered_port.arm_sequences
    assert set(all_arm_sequences) == {persisted["arm_sequence"]}
    detail = repo.trial_detail(trial_id)
    all_ack_sequences = first_port.ack_sequences + recovered_port.ack_sequences
    assert set(all_ack_sequences) == {detail["ack_sequence"]}
    assert sum(row["status"] == "pending" for row in repo.list_candidates()) == 4
