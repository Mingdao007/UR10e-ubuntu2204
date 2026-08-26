from __future__ import annotations

from pathlib import Path
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from step6_figure8_autotune_v1.core import (  # noqa: E402
    CompleteCandidateV1,
    PersistedSobolCursorV1,
    ProductionProposalUnavailable,
)
from step6_figure8_autotune_v1.optimizer_worker import propose  # noqa: E402
from step6_figure8_autotune_v1.optimizer_bridge import (  # noqa: E402
    SubprocessFigureEightProposalProviderV1,
)


def test_optimizer_bridge_preserves_virtualenv_launcher_symlink(tmp_path: Path) -> None:
    launcher = tmp_path / "venv-python"
    launcher.symlink_to(Path(sys.executable))
    provider = SubprocessFigureEightProposalProviderV1(
        optimizer_python=launcher,
        state_dir=tmp_path / "optimizer-state",
    )
    assert provider.optimizer_python == launcher.absolute()
    assert provider.optimizer_python_target == Path(sys.executable).resolve()


def test_optimizer_bridge_preserves_incomplete_request_and_advances_serial(
    tmp_path: Path,
) -> None:
    state = tmp_path / "optimizer-state"
    state.mkdir()
    abandoned = state / "qlognei-0001-request.json"
    abandoned.write_text('{"incomplete":true}\n', encoding="utf-8")
    provider = SubprocessFigureEightProposalProviderV1(
        optimizer_python=Path(sys.executable),
        state_dir=state,
    )
    assert provider.invocation_count == 1
    assert abandoned.read_text(encoding="utf-8") == '{"incomplete":true}\n'

    (state / "qlognei-0002-response.json").write_text("{}\n", encoding="utf-8")
    with pytest.raises(ProductionProposalUnavailable, match="response lacks its request"):
        SubprocessFigureEightProposalProviderV1(
            optimizer_python=Path(sys.executable),
            state_dir=state,
        )


@pytest.mark.gpu
def test_cuda_worker_scores_exactly_128_with_group_yvar(tmp_path: Path) -> None:
    cursor = PersistedSobolCursorV1(tmp_path / "sobol.json")
    observed_batch = cursor.fresh_pool(domain="controller_path")
    observed = observed_batch[:16]
    pending = observed_batch[16]
    pool = cursor.fresh_pool(domain="controller_path")
    fingerprint = "a" * 64
    observations = [
        {
            "candidate": candidate,
            "candidate_key": f"observed-{index}",
            "mae_n": 0.4 + 0.005 * abs(index - 7),
            "yvar_n2": 0.001 + 0.00001 * index,
            "fingerprint_sha256": fingerprint,
            "n": 1,
        }
        for index, candidate in enumerate(observed)
    ]
    result = propose(
        {
            "pool": pool,
            "observations": observations,
            "block": "controller_path",
            "pending_candidates": [pending],
            "pending_keys": [
                CompleteCandidateV1.from_mapping(pending).candidate_key
            ],
            "seed": 6016,
        }
    )
    assert result["schema"] == "step6.autotune/figure8-qlognei-proposal-v1"
    assert result["candidate_key"] in {
        row["candidate_key"] for row in result["scored_pool"]
    }
    assert len(result["scored_pool"]) == 128
    assert len(result["observed_posterior_probability"]) == 16
    assert len(result["observed_posterior_mean_n"]) == 16
    assert len(result["observed_posterior_variance_n2"]) == 16
    assert all(
        value > 0.0
        for value in result["observed_posterior_variance_n2"].values()
    )
    assert all(
        0.0 <= value <= 1.0
        for value in result["observed_posterior_probability"].values()
    )
    assert result["fit_receipt"]["train_yvar_count"] == 16
    assert result["fit_receipt"]["device"] == "cuda"
    assert result["fit_receipt"]["pending_candidate_count"] == 1
    assert result["fit_receipt"]["asynchronous_pending_fantasy"] is True
