from __future__ import annotations

import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from step5d_autotune_v2.report import METRICS, render_trial_report, write_trial_report
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
