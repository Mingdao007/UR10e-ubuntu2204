from __future__ import annotations

import hashlib
import json
from pathlib import Path
import stat
import sys
from typing import Any, Callable

import pytest


ROOT = Path(__file__).resolve().parents[1]
REPOSITORY_ROOT = ROOT.parents[1]
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(REPOSITORY_ROOT / "src/ur10e_experiment_runtime"))

import build_step5d_v3_timing_equivalence as equivalence  # noqa: E402


RAW = (
    ROOT
    / "config/step5/step5d_autotune_v3_formal_timing_raw_ede7bdb5.json"
)


def _load(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


def _write(path: Path, value: dict[str, Any], *, allow_nan: bool = False) -> None:
    path.write_text(
        json.dumps(
            value,
            allow_nan=allow_nan,
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )


def _materialize(
    tmp_path: Path,
    *,
    mutate_raw: Callable[[dict[str, Any]], None] | None = None,
    mutate_evaluation: Callable[[dict[str, Any]], None] | None = None,
    mutate_metadata: Callable[[dict[str, Any]], None] | None = None,
    allow_nan: bool = False,
) -> Path:
    raw = _load(RAW)
    evaluation = _load(RAW.with_suffix(".evaluation.json"))
    metadata = _load(RAW.with_suffix(".metadata.json"))
    if mutate_raw is not None:
        mutate_raw(raw)
    if mutate_evaluation is not None:
        mutate_evaluation(evaluation)
    if mutate_metadata is not None:
        mutate_metadata(metadata)

    raw_path = tmp_path / "capture.json"
    evaluation_path = raw_path.with_suffix(".evaluation.json")
    metadata_path = raw_path.with_suffix(".metadata.json")
    _write(raw_path, raw, allow_nan=allow_nan)
    raw_sha256 = hashlib.sha256(raw_path.read_bytes()).hexdigest()
    displayed = str(raw_path.resolve())
    evaluation["raw_path"] = displayed
    evaluation["raw_sha256"] = raw_sha256
    evaluation["base_evaluation"]["raw_path"] = displayed
    evaluation["base_evaluation"]["raw_sha256"] = raw_sha256
    evaluation["v3_moving_sphere"] = raw.get("step5d_v3_moving_sphere")
    metadata["output"] = displayed
    _write(evaluation_path, evaluation)
    _write(metadata_path, metadata)
    return raw_path


def test_real_capture_reuses_only_solver_and_safe_hold() -> None:
    result = equivalence.build_attestation(ROOT, RAW)
    assert {
        lane: item["reusable"] for lane, item in result["lane_reuse"].items()
    } == {
        "solver": True,
        "safe_hold": True,
        "full_tick": False,
    }
    assert result["subject_identity"]["tick_semantics"]["equivalent"] is True
    assert result["subject_identity"]["timing_harness"]["equivalent"] is True
    assert len(
        result["subject_identity"]["tick_semantics"][
            "target_layered_fingerprint"
        ]
    ) == 64
    assert (
        result["subject_identity"]["timing_harness"]["current_fingerprint"]
        == result["subject_identity"]["timing_harness"][
            "target_layered_fingerprint"
        ]
    )
    assert len(
        result["subject_identity"]["runtime_environment"]["fingerprint"]
    ) == 64
    assert result["next_required_lane"] == "full_tick"
    assert result["final_full_tick_required"] is True
    full_tick = result["lane_reuse"]["full_tick"]
    assert full_tick["blockers"] == [
        "max_consecutive_miss_exceeded",
        "p99_exceeds_limit",
        "schedule_miss_budget_exceeded",
    ]


def test_selector_verifier_and_builder_drift_is_provenance_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = equivalence._current_digest

    def current_digest(repository_root: Path, repository_relative: str) -> str:
        if repository_relative.endswith("build_step5d_v30_offline_readiness.py"):
            return "f" * 64
        return original(repository_root, repository_relative)

    monkeypatch.setattr(equivalence, "_current_digest", current_digest)
    result = equivalence.build_attestation(ROOT, RAW)
    provenance = result["provenance_only"]["excluded_from_subject_fingerprints"]
    assert result["subject_identity"]["tick_semantics"]["equivalent"] is True
    assert result["subject_identity"]["timing_harness"]["equivalent"] is True
    assert any(
        item["classification"] == "selector_or_document_provenance_only"
        and item["matches"] is False
        for item in provenance.values()
    )
    assert sum(
        item["classification"] == "verifier_or_builder_provenance_only"
        and item["matches"] is False
        for item in provenance.values()
    ) == 1


def test_timing_delivery_or_statistics_drift_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = equivalence._current_digest

    def current_digest(repository_root: Path, repository_relative: str) -> str:
        if repository_relative.endswith("step5d_v30_timing.py"):
            return "f" * 64
        return original(repository_root, repository_relative)

    monkeypatch.setattr(equivalence, "_current_digest", current_digest)
    with pytest.raises(
        equivalence.TimingEquivalenceError,
        match="timing harness source drift",
    ):
        equivalence.build_attestation(ROOT, RAW)


def test_tick_subject_digest_drift_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = equivalence._current_digest

    def current_digest(repository_root: Path, repository_relative: str) -> str:
        if repository_relative.endswith("tools/kunwei_rtde_bridge.py"):
            return "f" * 64
        return original(repository_root, repository_relative)

    monkeypatch.setattr(equivalence, "_current_digest", current_digest)
    with pytest.raises(
        equivalence.TimingEquivalenceError,
        match="tick subject source drift",
    ):
        equivalence.build_attestation(ROOT, RAW)


def test_raw_digest_mismatch_fails_closed(tmp_path: Path) -> None:
    raw_path = _materialize(
        tmp_path,
        mutate_evaluation=lambda value: value.update(raw_sha256="0" * 64),
    )
    evaluation_path = raw_path.with_suffix(".evaluation.json")
    evaluation = _load(evaluation_path)
    evaluation["raw_sha256"] = "0" * 64
    _write(evaluation_path, evaluation)
    with pytest.raises(
        equivalence.TimingEquivalenceError,
        match="evaluation raw_sha256 mismatch",
    ):
        equivalence.build_attestation(ROOT, raw_path)


def test_missing_metric_fails_closed(tmp_path: Path) -> None:
    def remove_metric(value: dict[str, Any]) -> None:
        del value["safe_hold"]["p99_ms"]

    raw_path = _materialize(tmp_path, mutate_raw=remove_metric)
    with pytest.raises(
        equivalence.TimingEquivalenceError,
        match=r"raw\.safe_hold\.p99_ms must be numeric",
    ):
        equivalence.build_attestation(ROOT, raw_path)


def test_nonfinite_metric_fails_closed(tmp_path: Path) -> None:
    def insert_nan(value: dict[str, Any]) -> None:
        value["solver"]["p99_ms"] = float("nan")

    raw_path = _materialize(
        tmp_path,
        mutate_raw=insert_nan,
        allow_nan=True,
    )
    with pytest.raises(
        equivalence.TimingEquivalenceError,
        match="non-finite JSON number",
    ):
        equivalence.build_attestation(ROOT, raw_path)


def test_immutable_sidecar_is_read_only_and_never_overwritten(
    tmp_path: Path,
) -> None:
    output = tmp_path / "capture.equivalence.json"
    equivalence.write_immutable_sidecar(output, {"schema": "fixture-v1"})
    assert stat.S_IMODE(output.stat().st_mode) == 0o444
    original = output.read_bytes()
    with pytest.raises(
        equivalence.TimingEquivalenceError,
        match="immutable sidecar already exists",
    ):
        equivalence.write_immutable_sidecar(output, {"schema": "changed"})
    assert output.read_bytes() == original
