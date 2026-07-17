from __future__ import annotations

import hashlib
import sqlite3
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from step5d_autotune_v2.report import (
    METRICS,
    reconcile_missing_trial_reports,
    render_trial_report,
    write_trial_report,
)
from step5d_autotune_v2.repository import RepositoryError
from step5d_autotune_v2.supervisor import CampaignSupervisor
from test_step5d_autotune_v2_supervisor import FakePort, deployment, repository


def test_report_is_exactly_parameter_table_plus_metric_rows_table(tmp_path: Path) -> None:
    repo = repository(tmp_path)
    supervisor = CampaignSupervisor(repo, FakePort(tmp_path))
    first = supervisor.run_pending(deployment_id=deployment().deployment_id, maximum=1)[0]
    second = supervisor.run_pending(deployment_id=deployment().deployment_id, maximum=1)[0]
    report = render_trial_report(repo, trial_id=second.trial_id, batch_id="fake-five")
    assert len([line for line in report.splitlines() if line.startswith("|---")]) == 2
    assert "| 组 | P | I | D |" in report
    assert (
        "| Metric | 当前 G11 | 同 deployment/profile 最佳 G10 | Δ |"
        in report
    )
    assert len([line for line in report.splitlines() if line.startswith("| ")]) == 2 + 5 + len(METRICS)
    assert first.trial_id != second.trial_id


def test_report_file_is_parameter_named_immutable_and_registered(tmp_path: Path) -> None:
    repo = repository(tmp_path)
    outcome = CampaignSupervisor(repo, FakePort(tmp_path)).run_pending(
        deployment_id=deployment().deployment_id, maximum=1
    )[0]
    content, path = write_trial_report(
        repo, trial_id=outcome.trial_id, output_root=tmp_path / "reports"
    )
    assert path.name.startswith("G10_P=0.001681792830507429_I=0.0001_D=8.324449805019047_")
    assert path.read_text(encoding="utf-8") == content
    assert repo.artifact_for_trial(
        outcome.trial_id, role="trial_markdown_report"
    )["path"] == str(path)
    assert write_trial_report(
        repo, trial_id=outcome.trial_id, output_root=tmp_path / "reports"
    ) == (content, path)


def test_replay_report_with_null_batch_contains_only_the_replay_candidate(
    tmp_path: Path,
) -> None:
    repo = repository(tmp_path)
    supervisor = CampaignSupervisor(repo, FakePort(tmp_path))
    supervisor.run_pending(deployment_id=deployment().deployment_id, maximum=1)
    replay = repo.create_replay(group_id="G10", reason="audit", nonce="replay-1")
    outcome = supervisor.run_pending(
        deployment_id=deployment().deployment_id, maximum=1
    )[0]

    assert repo.trial_detail(outcome.trial_id)["batch_id"] is None
    assert repo.trial_detail(outcome.trial_id)["candidate_id"] == replay.candidate_id
    report = render_trial_report(repo, trial_id=outcome.trial_id)
    assert len([line for line in report.splitlines() if line.startswith("| ")]) == (
        2 + 1 + len(METRICS)
    )
    assert "| G10 |" in report
    assert "| G11 |" not in report


def test_reconciliation_handles_regular_and_replay_reports_idempotently(
    tmp_path: Path,
) -> None:
    repo = repository(tmp_path)
    supervisor = CampaignSupervisor(repo, FakePort(tmp_path))
    regular = supervisor.run_pending(
        deployment_id=deployment().deployment_id, maximum=1
    )[0]
    regular_report, regular_path = write_trial_report(
        repo, trial_id=regular.trial_id, output_root=tmp_path / "reports"
    )
    later_regular = supervisor.run_pending(
        deployment_id=deployment().deployment_id, maximum=1
    )[0]
    repo.create_replay(group_id="G10", reason="audit", nonce="replay-1")
    replay = supervisor.run_pending(
        deployment_id=deployment().deployment_id, maximum=1
    )[0]

    recovered = reconcile_missing_trial_reports(
        repo,
        deployment_id=deployment().deployment_id,
        output_root=tmp_path / "reports",
    )
    assert len(recovered) == 2
    assert regular_path.read_text(encoding="utf-8") == regular_report
    assert any(later_regular.trial_id[:12] in path.name for _content, path in recovered)
    replay_report = next(content for content, path in recovered if replay.trial_id[:12] in path.name)
    assert len([line for line in regular_report.splitlines() if line.startswith("| ")]) == (
        2 + 5 + len(METRICS)
    )
    assert len([line for line in replay_report.splitlines() if line.startswith("| ")]) == (
        2 + 1 + len(METRICS)
    )
    assert reconcile_missing_trial_reports(
        repo,
        deployment_id=deployment().deployment_id,
        output_root=tmp_path / "reports",
    ) == []


def test_reconciliation_restores_a_missing_registered_report_from_canonical_bytes(
    tmp_path: Path,
) -> None:
    repo = repository(tmp_path)
    outcome = CampaignSupervisor(repo, FakePort(tmp_path)).run_pending(
        deployment_id=deployment().deployment_id, maximum=1
    )[0]
    content, path = write_trial_report(
        repo, trial_id=outcome.trial_id, output_root=tmp_path / "reports"
    )
    artifact = repo.artifact_record_for_trial(
        outcome.trial_id, role="trial_markdown_report"
    )
    path.unlink()

    recovered = reconcile_missing_trial_reports(
        repo,
        deployment_id=deployment().deployment_id,
        output_root=tmp_path / "reports",
    )
    assert recovered == [(content, path)]
    assert path.read_bytes() == content.encode("utf-8")
    assert hashlib.sha256(path.read_bytes()).hexdigest() == artifact["sha256"]


def test_reconciliation_rejects_corrupt_or_symlinked_registered_report(
    tmp_path: Path,
) -> None:
    repo = repository(tmp_path)
    outcome = CampaignSupervisor(repo, FakePort(tmp_path)).run_pending(
        deployment_id=deployment().deployment_id, maximum=1
    )[0]
    _content, path = write_trial_report(
        repo, trial_id=outcome.trial_id, output_root=tmp_path / "reports"
    )
    path.write_text("corrupt\n", encoding="utf-8")
    with pytest.raises(RepositoryError, match="bytes do not match its hash"):
        reconcile_missing_trial_reports(
            repo,
            deployment_id=deployment().deployment_id,
            output_root=tmp_path / "reports",
        )

    path.unlink()
    target = tmp_path / "target.md"
    target.write_text("not canonical\n", encoding="utf-8")
    path.symlink_to(target)
    with pytest.raises(RepositoryError, match="unsafe|regular non-symlink"):
        reconcile_missing_trial_reports(
            repo,
            deployment_id=deployment().deployment_id,
            output_root=tmp_path / "reports",
        )


@pytest.mark.parametrize(
    ("column", "value"),
    (
        ("sha256", "f" * 64),
        ("path", "/tmp/conflicting-step5d-report.md"),
        ("immutable", 0),
    ),
)
def test_reconciliation_rejects_conflicting_registered_artifact_binding(
    tmp_path: Path, column: str, value: object
) -> None:
    repo = repository(tmp_path)
    outcome = CampaignSupervisor(repo, FakePort(tmp_path)).run_pending(
        deployment_id=deployment().deployment_id, maximum=1
    )[0]
    _content, path = write_trial_report(
        repo, trial_id=outcome.trial_id, output_root=tmp_path / "reports"
    )
    before = path.read_bytes()
    with sqlite3.connect(repo.path) as connection:
        connection.execute(
            f"UPDATE artifacts SET {column}=? WHERE trial_id=? AND role='trial_markdown_report'",
            (value, outcome.trial_id),
        )

    with pytest.raises(RepositoryError, match="conflicts with canonical"):
        reconcile_missing_trial_reports(
            repo,
            deployment_id=deployment().deployment_id,
            output_root=tmp_path / "reports",
        )
    assert path.read_bytes() == before
