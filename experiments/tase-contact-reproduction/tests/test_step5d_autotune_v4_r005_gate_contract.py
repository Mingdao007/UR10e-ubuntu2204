from __future__ import annotations

import dataclasses
import hashlib
import json
import time
from pathlib import Path
from typing import Any

import pytest

from step5d_autotune_v4_r004.contracts import load_contract as load_r004_contract
from step5d_autotune_v4_r004_live_writer import LiveR004Writer
from step5d_autotune_v4_r005.contracts import load_contract
from step5d_autotune_v4_r005.live_adapter import (
    R005LiveAdapterError,
    R005LiveInputs,
    build_verified_mature_r005_writer,
)
from step5d_autotune_v4_r005.observations import ObservationLedger
from step5d_autotune_v4_r005.tp import render_script


ROOT = Path(__file__).resolve().parents[1]


def _write_admission_fixture(
    tmp_path: Path,
    *,
    controller_age_s: float = 2.0,
    script1_age_s: float = 2.0,
    runtime_age_s: float = 1.0,
    software_baseline_age_s: float = 1.5,
) -> tuple[Any, R005LiveInputs, dict[str, Path], float]:
    """Create only local admission evidence; no transport is constructed."""

    from step5d_autotune_v3.runtime_installation import current_pointer_path
    from step5d_optimizer_runtime import resolve_optimizer_runtime
    from step5d_autotune_v4_r004.contracts import runtime_identity_limbs
    from step5d_autotune_v4_r005.tp import RUNTIME_PROTOCOL

    contract = load_contract()
    parent = load_r004_contract()
    resolved = resolve_optimizer_runtime()
    now = time.time()
    runtime_hi, runtime_lo = runtime_identity_limbs(
        contract.raw["program"], contract.sha256, contract.campaign_fingerprint
    )
    program_root = ROOT / "programs/step5/step5d/step5d_strict_rnn_autotune_v4_r005"
    triplet = {
        role: hashlib.sha256(program_root.with_suffix(suffix).read_bytes()).hexdigest()
        for role, suffix in (("script", ".script"), ("txt", ".txt"), ("urp", ".urp"))
    }
    eoat = contract.raw["invariants"]["eoat_profile"]["sha256"]
    controller = {
        "observed_at_s": now - controller_age_s,
        "safety_mode": "NORMAL",
        "stationary": True,
        "program": contract.raw["program"],
        "controller_target": "/programs/andyl/kunwei/step5/step5d_strict_rnn_autotune_v4_r005.urp",
        "receipt_sha256": "b" * 64,
        "script_sha256": triplet["script"],
        "txt_sha256": triplet["txt"],
        "urp_sha256": triplet["urp"],
        "eoat_identity_sha256": eoat,
        "readback": {
            "actual_tcp_speed_m_s_rad_s": [0.0] * 6,
            "payload_kg": 0.413,
            "payload_cog_m": [0.0011, 0.0031, 0.0163],
            "tcp_offset_m_rad": [0.0, 0.0, 0.0874, 0.0, 0.0, 0.0],
        },
        "route_id": "r005-gate-test-route",
        "runtime_protocol": RUNTIME_PROTOCOL,
        "runtime_digest_hi": runtime_hi,
        "runtime_digest_lo": runtime_lo,
    }
    script1 = {
        "observed_at_s": now - script1_age_s,
        "safety_mode": "NORMAL",
        "stationary": True,
        "receipt_sha256": "c" * 64,
        "script_sha256": parent.script1_sha256["script"],
        "final_pose": [0.487834547, 0.129337053, 0.033, 3.120752062, 0.0, 0.068626833],
        "final_q": [0.0] * 6,
        "eoat_identity_sha256": eoat,
    }
    runtime = {
        "observed_at_s": now - runtime_age_s,
        "program": contract.raw["program"],
        "script_sha256": triplet["script"],
        "runtime_protocol": RUNTIME_PROTOCOL,
        "runtime_digest_hi": runtime_hi,
        "runtime_digest_lo": runtime_lo,
        "session_epoch": 5005,
        "resident_session_id": "r005-gate-test-resident",
        "program_running": True,
        "uninterrupted": True,
    }
    paths = {
        "controller": tmp_path / "controller.json",
        "script1": tmp_path / "script1.json",
        "runtime": tmp_path / "runtime.json",
        "software_baseline": tmp_path / "software-baseline.json",
    }
    for role, payload in (("controller", controller), ("script1", script1), ("runtime", runtime)):
        paths[role].write_text(json.dumps(payload), encoding="utf-8")
    paths["software_baseline"].write_text(
        json.dumps(
            {
                "schema": "step5d.autotune-v4/r005-software-baseline-v1",
                "observed_at_s": now - software_baseline_age_s,
                "sample_count": 900,
                "mean_wrench_n_nm": [1.0, 2.0, 3.0, 0.1, 0.2, 0.3],
                "parse_errors": 0,
                "dropped_bytes": 0,
                "zero_tare_config_write": False,
            }
        ),
        encoding="utf-8",
    )
    ledger_path = tmp_path / "observations.jsonl"
    ObservationLedger(ledger_path, campaign_fingerprint=contract.campaign_fingerprint, eoat_sha256=eoat)
    queue_root = tmp_path / "queue"
    authority_root = tmp_path / "authority"
    queue_root.mkdir()
    authority_root.mkdir()
    inputs = R005LiveInputs(
        controller_receipt=paths["controller"],
        script1_receipt=paths["script1"],
        runtime_evidence=paths["runtime"],
        runtime_attestation=Path(str(resolved.pointer["attestation_path"])),
        optimizer_pointer=current_pointer_path(),
        launch_profile_path=ROOT / "config/step5/step5d_autotune_v4_r005_launch_profile.json",
        release_manifest_sha256="a" * 64,
        baseline_ledger=ROOT / "config/step5d/autotune_v4_r005_baseline_ledger_genesis.json",
        software_baseline_receipt=paths["software_baseline"],
        ledger_path=ledger_path,
        queue_root=queue_root,
        authority_root=authority_root,
        controller_host="controller.test",
        kunwei_host="kunwei.test",
        kunwei_port=5152,
        route_id="r005-gate-test-route",
        attempt_id="r005-gate-test-attempt",
        resident_session_id="r005-gate-test-resident",
        session_epoch=5005,
        expected_triplet=triplet,
        eoat_sha256=eoat,
        campaign_fingerprint=contract.campaign_fingerprint,
        contract_sha256=contract.sha256,
        now_s=now,
    )
    return contract, inputs, paths, now


def _json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _function_body(source: str, name: str) -> str:
    start = source.index(f"def {name}(")
    next_def = source.find("\ndef ", start + 5)
    return source[start:] if next_def < 0 else source[start:next_def]


def test_r005_renderer_removes_only_reason23_initial_gate() -> None:
    from step5d_autotune_v4_r004.tp import render_script as render_r004

    r004 = render_r004()
    r005 = render_script()
    execute = _function_body(r005, "codex_r005_execute_attempt")
    before_contact = execute.split("  local contact_start", 1)[0]
    return_home = _function_body(r005, "codex_r005_return_home")

    assert "consumed, 23," in r004
    assert "consumed, 23," not in execute
    assert "codex_r005_stationary(0.250000000)" not in before_contact
    assert "codex_r005_packet_guard(packet_reason, 60.0, 100.0, 3.0)" in before_contact
    assert return_home.count("codex_r005_stationary(0.250000000)") == 4
    assert "codex_r005_home_close(home_pose, home_q)" in return_home
    assert "stopl(0.010000000)\n  if not codex_r005_stationary(0.250000000)" in execute
    assert "codex_r005_entry_home_verified(fixed_home_pose, locked_home_q, locked_home_q_valid)" in r005

    # V3 r025 keeps READY_HOME_NEXT stationary until a fresh ARM; its r005
    # host-side equivalent still revalidates Home/Safety/stationary before ARM.
    import inspect

    prearm = inspect.getsource(LiveR004Writer._prearm_output)
    prearm_home = inspect.getsource(LiveR004Writer._assert_prearm_home_boundary)
    assert "output.stationary" in prearm
    assert "_assert_prearm_home_boundary(output)" in prearm
    assert "require_stationary=True" in prearm_home


def test_r005_accepts_old_same_prepare_ordered_receipts_through_both_admission_seams(
    tmp_path: Path,
) -> None:
    from step5d_autotune_v4_r004.home import HomeReference

    old_age_s = 24.0 * 60.0 * 60.0
    contract, inputs, _paths, now = _write_admission_fixture(
        tmp_path,
        controller_age_s=old_age_s,
        script1_age_s=old_age_s,
        software_baseline_age_s=old_age_s - 0.5,
        runtime_age_s=old_age_s - 1.0,
    )

    inputs.validate(contract=contract)
    mature = build_verified_mature_r005_writer(inputs, contract=contract)
    # This is the production writer's pre-transport prerequisite seam.  It
    # must retain content/EOAT/identity checks without reintroducing r004 TTL.
    mature.writer.prerequisites.validate(now_s=now)
    mature.writer.session.play(
        controller_receipt=mature.writer.prerequisites.controller,
        script1_receipt=mature.writer.prerequisites.script1,
        now_s=now,
        epoch=inputs.session_epoch,
        session_id=inputs.resident_session_id,
        expected_triplet=dict(inputs.expected_triplet),
        home=HomeReference(q=(0.0,) * 6),
    )
    assert mature.writer.session.phase.value == "ready_home_next"


@pytest.mark.parametrize(
    ("mutation", "message"),
    (
        ("controller_triplet", "controller readback triplet differs"),
        ("expected_hash", "controller readback triplet differs"),
        ("route", "r005 controller receipt route differs"),
        ("eoat", "controller readback EOAT identity differs"),
        ("runtime_identity", "runtime evidence r005 runtime identity differs"),
        ("runtime_session", "r005 runtime epoch differs"),
        ("runtime_session_id", "r005 resident session identity differs"),
        ("runtime_program", "r005 runtime program identity differs"),
        ("runtime_digest", "runtime evidence r005 runtime identity differs"),
    ),
)
def test_r005_admission_rejects_tampered_hash_route_eoat_and_identity(
    tmp_path: Path,
    mutation: str,
    message: str,
) -> None:
    contract, inputs, paths, _now = _write_admission_fixture(
        tmp_path,
        controller_age_s=24.0 * 60.0 * 60.0,
        script1_age_s=24.0 * 60.0 * 60.0,
    )
    if mutation == "controller_triplet":
        payload = _json(paths["controller"])
        payload["script_sha256"] = "f" * 64
        paths["controller"].write_text(json.dumps(payload), encoding="utf-8")
    elif mutation == "expected_hash":
        inputs = dataclasses.replace(
            inputs,
            expected_triplet={**inputs.expected_triplet, "script": "0" * 64},
        )
    elif mutation == "route":
        payload = _json(paths["controller"])
        payload["route_id"] = "r005-other-route"
        paths["controller"].write_text(json.dumps(payload), encoding="utf-8")
    elif mutation == "eoat":
        payload = _json(paths["controller"])
        payload["eoat_identity_sha256"] = "e" * 64
        paths["controller"].write_text(json.dumps(payload), encoding="utf-8")
    elif mutation == "runtime_identity":
        payload = _json(paths["runtime"])
        payload["runtime_protocol"] += 1
        paths["runtime"].write_text(json.dumps(payload), encoding="utf-8")
    elif mutation == "runtime_session":
        payload = _json(paths["runtime"])
        payload["session_epoch"] += 1
        paths["runtime"].write_text(json.dumps(payload), encoding="utf-8")
    elif mutation == "runtime_session_id":
        payload = _json(paths["runtime"])
        payload["resident_session_id"] = "r005-other-resident"
        paths["runtime"].write_text(json.dumps(payload), encoding="utf-8")
    elif mutation == "runtime_program":
        payload = _json(paths["runtime"])
        payload["program"] = "step5d_strict_rnn_autotune_v4_r005_wrong"
        paths["runtime"].write_text(json.dumps(payload), encoding="utf-8")
    elif mutation == "runtime_digest":
        payload = _json(paths["runtime"])
        payload["runtime_digest_hi"] += 1
        paths["runtime"].write_text(json.dumps(payload), encoding="utf-8")
    else:  # pragma: no cover - parameter list is exhaustive
        raise AssertionError(mutation)

    with pytest.raises((R005LiveAdapterError, RuntimeError), match=message):
        inputs.validate(contract=contract)


def test_r005_accepts_old_runtime_evidence_without_wall_clock_ttl(tmp_path: Path) -> None:
    old_age_s = 24.0 * 60.0 * 60.0
    contract, inputs, _paths, _now = _write_admission_fixture(
        tmp_path,
        controller_age_s=old_age_s,
        script1_age_s=old_age_s,
        software_baseline_age_s=old_age_s - 0.5,
        runtime_age_s=old_age_s - 1.0,
    )

    inputs.validate(contract=contract)


def test_r005_runtime_evidence_still_rejects_future_timestamp(tmp_path: Path) -> None:
    contract, inputs, _paths, _now = _write_admission_fixture(
        tmp_path,
        runtime_age_s=-1.0,
    )

    with pytest.raises(
        R005LiveAdapterError,
        match="runtime evidence timestamp is from the future",
    ):
        inputs.validate(contract=contract)


def test_r005_software_baseline_timestamp_still_rejects_future(tmp_path: Path) -> None:
    contract, inputs, _paths, _now = _write_admission_fixture(
        tmp_path,
        software_baseline_age_s=-1.0,
    )

    with pytest.raises(
        R005LiveAdapterError,
        match="software baseline receipt timestamp is from the future",
    ):
        inputs.validate(contract=contract)


def test_r005_managed_prepare_rejects_baseline_before_script1(tmp_path: Path) -> None:
    contract, inputs, _paths, _now = _write_admission_fixture(
        tmp_path,
        script1_age_s=2.0,
        software_baseline_age_s=3.0,
        runtime_age_s=1.0,
    )

    with pytest.raises(R005LiveAdapterError, match="managed prepare ordering requires"):
        inputs.validate(contract=contract)


def test_r005_managed_prepare_rejects_baseline_after_runtime_evidence(tmp_path: Path) -> None:
    contract, inputs, _paths, _now = _write_admission_fixture(
        tmp_path,
        script1_age_s=2.0,
        software_baseline_age_s=0.5,
        runtime_age_s=1.0,
    )

    with pytest.raises(R005LiveAdapterError, match="managed prepare ordering requires"):
        inputs.validate(contract=contract)


@pytest.mark.parametrize(
    ("mutation", "message"),
    (
        ("dirty", "r005 software baseline transport evidence is not clean"),
        ("short", "r005 software baseline has too few physical frames"),
        ("nonfinite", "r005 software baseline values must be finite"),
    ),
)
def test_r005_software_baseline_integrity_gates_remain_strict(
    tmp_path: Path,
    mutation: str,
    message: str,
) -> None:
    contract, inputs, paths, _now = _write_admission_fixture(tmp_path)
    payload = _json(paths["software_baseline"])
    if mutation == "dirty":
        payload["parse_errors"] = 1
    elif mutation == "short":
        payload["sample_count"] = 899
    elif mutation == "nonfinite":
        payload["mean_wrench_n_nm"][0] = float("nan")
    else:  # pragma: no cover - parameter list is exhaustive
        raise AssertionError(mutation)
    paths["software_baseline"].write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(R005LiveAdapterError, match=message):
        inputs.validate(contract=contract)


def test_r005_ttl_exempt_receipts_still_reject_future_timestamps(tmp_path: Path) -> None:
    contract, inputs, _paths, _now = _write_admission_fixture(
        tmp_path,
        controller_age_s=-1.0,
    )

    with pytest.raises(R005LiveAdapterError, match="controller receipt timestamp is from the future"):
        inputs.validate(contract=contract)
