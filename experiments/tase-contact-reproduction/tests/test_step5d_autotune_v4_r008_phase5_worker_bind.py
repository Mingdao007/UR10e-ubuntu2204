"""Phase-5: worker artifact binding must cold-read via obj_mod under binary scope."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from step5d_autotune_v4_r006.contracts import load_contract  # noqa: E402
from step5d_autotune_v4_r006.lattice import ANCHOR_POINT  # noqa: E402
from step5d_autotune_v4_r006.objective import R006ObjectiveBuilder  # noqa: E402
from step5d_autotune_v4_r006.sidecar import R006ObjectiveSidecar  # noqa: E402
from step5d_autotune_v4_r008.binary_seal import (  # noqa: E402
    R008_OBJECTIVE_RECEIPT_VERSION,
    r008_binary_seal_scope,
)
from step5d_autotune_v4_r008.bounded_sidecar_verify import (  # noqa: E402
    r008_bounded_sidecar_scope,
)
from step5d_autotune_v4_r008.bounded_worker_artifact_binding import (  # noqa: E402
    bounded_artifact_binding,
    clear_receipt_cache,
)
from step5d_autotune_v4_r008.raw_force_binary import SOURCE_KEYS  # noqa: E402
from step5d_force_objective import ForcePathSample  # noqa: E402


def _sample(path_time_s: float, force_n: float, sequence: int) -> ForcePathSample:
    return ForcePathSample(
        path_time_s=path_time_s,
        path_phase=25,
        filtered_normal_n=force_n,
        source_sequences={key: sequence for key in SOURCE_KEYS},
        source_ages_s={key: 0.001 for key in SOURCE_KEYS},
        commanded_qdot=(0.01, -0.02, 0.03, -0.04, 0.05, -0.06),
        actual_qd=(0.0, 0.0, 0.0, 0.0, 0.0, 0.0),
        timestamp_s=1000.0 + path_time_s,
    )


def _complete_samples(force_n: float = 5.15) -> tuple[ForcePathSample, ...]:
    samples = [_sample(0.05 + i * 0.1, force_n, i + 1) for i in range(550)]
    samples.extend(_sample(55.05 + i * 0.1, force_n, 551 + i) for i in range(50))
    return tuple(samples)


def test_bounded_artifact_binding_accepts_phase5_raw2(tmp_path: Path) -> None:
    """Regression for Claude audit problem 2: stock-bound cold_read on empty samples."""

    clear_receipt_cache()
    contract = load_contract(verify_source_closure=False)
    samples = _complete_samples()
    with r008_binary_seal_scope():
        builder = R006ObjectiveBuilder(
            attempt_sequence=1,
            execution_id="p5-worker-bind",
            campaign_fingerprint=contract.campaign_fingerprint,
            candidate_uid=ANCHOR_POINT.uid,
        )
        for sample in samples:
            builder.add(sample)
        receipt = builder.finalize()
    assert receipt.version == R008_OBJECTIVE_RECEIPT_VERSION
    assert list(receipt.raw_bundle.get("samples") or []) == []

    with r008_bounded_sidecar_scope(tail_rows=1):
        sidecar = R006ObjectiveSidecar(
            tmp_path / "r006-objectives.jsonl",
            campaign_fingerprint=contract.campaign_fingerprint,
        )
        row = sidecar.append(
            receipt,
            epoch=1,
            candidate_uid=ANCHOR_POINT.uid,
            kind="STAIRCASE",
            point_key=list(ANCHOR_POINT.key),
        )
    artifact = Path(row["artifact_path"])
    assert artifact.with_suffix(".r008raw").is_file()
    stub = json.loads(artifact.read_text(encoding="utf-8"))
    assert stub["raw_bundle"]["samples"] == []
    assert stub["version"] == R008_OBJECTIVE_RECEIPT_VERSION

    sidecar_data = sidecar.path.read_bytes()
    binding = {
        "sidecar_path": str(sidecar.path),
        "sidecar_sha256": hashlib.sha256(sidecar_data).hexdigest(),
        "sidecar_prefix_bytes": len(sidecar_data),
        "campaign_fingerprint": contract.campaign_fingerprint,
        "rows": [
            {
                "attempt_sequence": 1,
                "execution_id": "p5-worker-bind",
            }
        ],
    }
    # Must succeed without importing stock cold_read_verify by name.
    out = bounded_artifact_binding(binding, tail_rows=1)
    assert len(out["rows"]) == 1
    verified = out["rows"][0]["receipt"]
    assert verified.trainable is True
    assert verified.objective_mae_n == receipt.objective_mae_n
    assert verified.version == R008_OBJECTIVE_RECEIPT_VERSION
