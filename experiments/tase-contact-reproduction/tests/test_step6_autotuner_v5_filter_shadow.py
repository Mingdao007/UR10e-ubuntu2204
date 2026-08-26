from __future__ import annotations

import json
import math
from pathlib import Path
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT / "tests"))

from step6_figure8_autotune_v1.v5_filter_shadow import (  # noqa: E402
    DEFAULT_TAU_S,
    FilterShadowJobQueueV1,
    FilterShadowJobV1,
    V5FilterShadowError,
    build_force_filter_shadow,
    cold_verify_force_filter_shadow,
)
from test_step6_autotuner_v5_lifecycle_ledger import _write_artifact  # noqa: E402


def _sealed(tmp_path: Path, name: str = "chain"):
    path = tmp_path / f"{name}.r013life"
    receipt, events = _write_artifact(path, attempt_count=1)
    return path, receipt, events


def test_filter_shadow_cold_reads_only_sealed_artifact_and_preserves_authority(tmp_path: Path):
    source, receipt, events = _sealed(tmp_path)
    output = tmp_path / "shadow.json"
    build = build_force_filter_shadow(source, receipt, events, output)
    artifact = json.loads(output.read_text(encoding="utf-8"))

    assert build["status"] == "COMPLETE"
    assert build["source_artifact_sha256"] == receipt["artifact_sha256"]
    assert artifact["source"]["artifact_sha256"] == receipt["artifact_sha256"]
    assert artifact["contract"]["input_authority"] == "sealed_r013life_artifact_only"
    assert artifact["contract"]["notch_50_hz_enabled"] is False
    assert artifact["contract"]["authority"] == "observation_only"
    assert artifact["contract"]["raw_signal_authority"] == ["safety", "evidence"]
    assert set(artifact["contract"]["forbidden_consumers"]) == {"controller", "arm", "censor", "tell_exact", "gp_training", "promotion"}
    assert len(artifact["rows"]) == receipt["artifact_row_count"]
    assert artifact["rows"][0]["actual_dt_one_pole_n"] == artifact["rows"][0]["raw_signed_normal_n"]

    previous = artifact["rows"][0]
    current = artifact["rows"][1]
    dt_s = current["monotonic_s"] - previous["monotonic_s"]
    alpha = 1.0 - math.exp(-dt_s / DEFAULT_TAU_S)
    expected = (1.0 - alpha) * previous["actual_dt_one_pole_n"] + alpha * current["raw_signed_normal_n"]
    assert current["actual_dt_one_pole_n"] == pytest.approx(expected)
    assert math.isfinite(current["same_cutoff_bessel2_n"])
    assert math.isfinite(current["same_cutoff_butterworth2_n"])
    verified = cold_verify_force_filter_shadow(output, build, expected_source_artifact_sha256=receipt["artifact_sha256"])
    assert verified["content_sha256"] == build["content_sha256"]

    original = output.read_bytes()
    tampered = json.loads(original)
    tampered["rows"][0]["raw_signed_normal_n"] += 1.0
    output.write_text(json.dumps(tampered, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8")
    with pytest.raises(V5FilterShadowError):
        cold_verify_force_filter_shadow(output, build)
    output.write_bytes(original)


def test_filter_shadow_rejects_unsealed_or_tampered_input(tmp_path: Path):
    source, receipt, events = _sealed(tmp_path)
    bad_receipt = dict(receipt)
    bad_receipt["artifact_sha256"] = "0" * 64
    with pytest.raises(V5FilterShadowError):
        build_force_filter_shadow(source, bad_receipt, events, tmp_path / "bad.json")
    wrong_suffix = tmp_path / "copy.bin"
    wrong_suffix.write_bytes(source.read_bytes())
    with pytest.raises(V5FilterShadowError):
        build_force_filter_shadow(wrong_suffix, receipt, events, tmp_path / "bad2.json")


def test_filter_shadow_queue_is_capacity_two_drop_newest_and_failure_isolated(tmp_path: Path):
    source, receipt, events = _sealed(tmp_path)
    queue = FilterShadowJobQueueV1(tmp_path / "queue")
    jobs = [
        FilterShadowJobV1(str(source), receipt, events, str(tmp_path / f"shadow-{index}.json"))
        for index in range(3)
    ]
    first = queue.submit(jobs[0])
    second = queue.submit(jobs[1])
    third = queue.submit(jobs[2])
    assert first["accepted"] and second["accepted"]
    assert not third["accepted"] and third["disposition"] == "DROP_NEWEST"
    assert third["physical_campaign_dependency"] is False

    completed = queue.run_one()
    assert completed is not None and completed["status"] == "COMPLETE"
    assert Path(completed["build"]["output_path"]).is_file()
    assert queue.submit(jobs[2])["accepted"] is True

    invalid = FilterShadowJobV1(str(tmp_path / "missing.r013life"), receipt, events, str(tmp_path / "never.json"))
    assert queue.run_one()["status"] == "COMPLETE"
    assert queue.submit(invalid)["accepted"] is True
    assert queue.run_one()["status"] == "COMPLETE"
    failed = queue.run_one()
    assert failed is not None and failed["status"] == "FAILED"
    assert failed["physical_campaign_dependency"] is False
    assert not (tmp_path / "never.json").exists()


def test_filter_shadow_source_has_no_live_reader_or_control_authority():
    source = (ROOT / "tools" / "step6_figure8_autotune_v1" / "v5_filter_shadow.py").read_text(encoding="utf-8")
    assert "import rtde" not in source.lower()
    assert "import kunwei" not in source.lower()
    assert "tell_exact(" not in source
    assert "notch_50_hz_enabled\": True" not in source
