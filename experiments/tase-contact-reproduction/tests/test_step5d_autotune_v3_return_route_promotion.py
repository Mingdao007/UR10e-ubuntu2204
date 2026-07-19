from __future__ import annotations

from datetime import datetime, timedelta, timezone
import copy
import json
from pathlib import Path
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT.parents[1] / "src" / "ur10e_experiment_runtime"))

import build_step5d_autotune_v3_return_route_evidence as baseline  # noqa: E402
import promote_step5d_autotune_v3_return_route_evidence as promotion  # noqa: E402
from step5d_autotune_v3.profile import control_fingerprint, load_contract  # noqa: E402
from step5d_autotune_v3.state import orchestration_fingerprint  # noqa: E402
from ur10e_experiment_runtime.authorization import (  # noqa: E402
    CERTIFICATION_PROCEDURES,
    CertificationMotionAuthorization,
    STEP5D_V3_STAGE_IDENTITY,
)
from ur10e_experiment_runtime.return_route import (  # noqa: E402
    RETURN_TELEMETRY_SCHEMA,
    URSIM_RETURN_TRACE_SCHEMA,
)


def _write(path: Path, payload: object) -> Path:
    path.write_text(
        json.dumps(payload, indent=2, sort_keys=True, allow_nan=False) + "\n",
        encoding="utf-8",
    )
    return path.resolve()


def _fixture(tmp_path: Path) -> dict[str, object]:
    base = baseline.build_document(include_ursim_trace=False)
    control = control_fingerprint(load_contract())
    orchestration = orchestration_fingerprint(ROOT)
    readback = _write(tmp_path / "readback.json", {"schema": "fixture-readback-v1"})
    readback_sha = promotion._sha256(readback)
    issued = datetime.now(timezone.utc) - timedelta(minutes=1)
    authorization = CertificationMotionAuthorization(
        stage_identity=STEP5D_V3_STAGE_IDENTITY,
        control_fingerprint=control,
        orchestration_fingerprint=orchestration,
        plant_epoch=9,
        deployment_readback_sha256=readback_sha,
        allowed_procedures=CERTIFICATION_PROCEDURES,
        max_linear_speed_m_s=0.09,
        max_linear_acceleration_m_s2=0.135,
        max_angular_speed_rad_s=0.05,
        max_angular_acceleration_rad_s2=0.1,
        numeric_margin_m=0.0001,
        authorization_source="test owner",
        authorized_at=issued.isoformat(),
        expires_at=(issued + timedelta(hours=1)).isoformat(),
    )
    authorization_path = _write(tmp_path / "authorization.json", authorization.document())
    segments = [1] * 7 + [2] * 7 + [3] * 6
    ursim_samples = [
        {
            "controller_timestamp_s": 100.0 + index * 0.002,
            "actual_tcp_pose": [0.45 + 0.0003 * index, 0.1, 0.1, 3.12, 0.0, 0.001 * index],
            "actual_tcp_speed": [0.01, 0.0, 0.0, 0.0, 0.0, 0.02],
            "actual_qd": [0.01] * 6,
            "return_phase_echo": 40.0 + segments[index] / 10.0,
            "return_segment_id": segments[index],
            "completion_state": 104 if index == 19 else 100 + segments[index],
            "safety_mode": 1,
        }
        for index in range(20)
    ]
    ursim = {
        "schema": URSIM_RETURN_TRACE_SCHEMA,
        "status": "pass",
        "claim_boundary": "isolated_ursim_motion_only_not_live_certification",
        "identity": {
            "control_fingerprint": control,
            "orchestration_fingerprint": orchestration,
        },
        "source_binding_sha256": base["source_binding_sha256"],
        "triplet_sha256": base["local_triplet_sha256"],
        "image": {
            "reference": "universalrobots/ursim-e:5@sha256:" + "a" * 64,
            "image_id": "sha256:" + "b" * 64,
            "polyscope_version": "5.25.2",
            "robot_model": "UR10",
        },
        "network": {
            "internal": True,
            "host_ports_published": False,
            "real_robot_network_connected": False,
        },
        "route": {
            "controller_function": "codex_autotune_bounded_return_segment",
            "controller_function_sha256": "c" * 64,
            "segment_order": [1, 2, 3],
            "vertical_transfer_vertical": True,
            "relative_test_motion_only": True,
            "no_contact": True,
        },
        "samples": ursim_samples,
        "summary": {
            "sample_count": len(ursim_samples),
            "motion_observed": True,
            "max_position_excursion_m": 0.006,
            "max_angular_excursion_rad": 0.019,
            "max_angular_speed_rad_s": 0.02,
            "max_angular_acceleration_rad_s2": 0.1,
            "max_abs_qd_rad_s": 0.01,
            "segment_order_observed": [1, 2, 3],
            "completion_state": 104,
            "forbidden_action_count": 0,
        },
        "cleanup": {
            "container_removed": True,
            "network_removed": True,
            "program_stopped": True,
        },
    }
    ursim_path = _write(tmp_path / "ursim.json", ursim)
    telemetry_samples = [
        {
            "controller_timestamp_s": 200.0 + index * 0.002,
            "return_phase_echo": 40.0 + segments[index] / 10.0,
            "return_segment_id": segments[index],
            "angular_speed_rad_s": 0.02,
            "angular_acceleration_rad_s2": 0.1,
            "max_angular_speed_rad_s": 0.02,
            "max_angular_acceleration_rad_s2": 0.1,
            "max_sample_gap_s": 0.002,
            "guard_reason": 0,
            "safety_mode": "NORMAL",
        }
        for index in range(20)
    ]
    telemetry = {
        "schema": RETURN_TELEMETRY_SCHEMA,
        "candidate_stage_id": "step5d_strict_rnn_autotune_v3",
        "control_fingerprint": control,
        "orchestration_fingerprint": orchestration,
        "source_binding_sha256": base["source_binding_sha256"],
        "triplet_sha256": base["local_triplet_sha256"],
        "plant_epoch": 9,
        "deployment_readback_sha256": readback_sha,
        "certification_authorization_sha256": authorization.authorization_ref_sha256,
        "all_samples_retained": True,
        "no_contact": True,
        "samples": telemetry_samples,
        "final_readback": {
            "completed": True,
            "return_phase_echo": 40.3,
            "return_segment_id": 3,
            "return_guard_mask": 0x7F,
            "max_angular_speed_rad_s": 0.02,
            "max_angular_acceleration_rad_s2": 0.1,
            "max_sample_gap_s": 0.002,
            "safety_mode": "NORMAL",
        },
    }
    telemetry_path = _write(tmp_path / "telemetry.json", telemetry)
    return {
        "base": base,
        "readback": readback,
        "authorization": authorization,
        "authorization_path": authorization_path,
        "ursim": ursim,
        "ursim_path": ursim_path,
        "telemetry": telemetry,
        "telemetry_path": telemetry_path,
    }


def test_return_route_promotion_binds_all_three_evidence_classes(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    output = (tmp_path / "promoted.json").resolve()
    document = promotion.promote(
        ursim_trace_path=fixture["ursim_path"],
        telemetry_path=fixture["telemetry_path"],
        authorization_path=fixture["authorization_path"],
        deployment_readback_path=fixture["readback"],
        plant_epoch=9,
        output_path=output,
    )
    assert document["certified"] is True
    assert document["optimizer_eligible"] is False
    assert document["telemetry_summary"]["segment_order"] == [1, 2, 3]
    assert len(document["certification_binding_sha256"]) == 64
    assert output.is_file()


@pytest.mark.parametrize(
    ("artifact", "mutation"),
    [
        ("ursim", lambda value: value["network"].update(internal=False)),
        ("ursim", lambda value: value["summary"].update(motion_observed=False)),
        ("ursim", lambda value: value["summary"].update(segment_order_observed=[1, 3])),
        ("telemetry", lambda value: value.update(all_samples_retained=False)),
        ("telemetry", lambda value: value["samples"][0].update(guard_reason=17)),
        ("telemetry", lambda value: value["samples"][0].update(max_sample_gap_s=0.005)),
        ("telemetry", lambda value: value["final_readback"].update(return_guard_mask=0)),
    ],
)
def test_return_route_promotion_fails_closed_on_evidence_mutation(
    tmp_path: Path, artifact: str, mutation
) -> None:
    fixture = _fixture(tmp_path)
    changed = copy.deepcopy(fixture[artifact])
    mutation(changed)
    changed_path = _write(tmp_path / f"changed-{artifact}.json", changed)
    kwargs = {
        "ursim_trace_path": fixture["ursim_path"],
        "telemetry_path": fixture["telemetry_path"],
        "authorization_path": fixture["authorization_path"],
        "deployment_readback_path": fixture["readback"],
        "plant_epoch": 9,
        "output_path": (tmp_path / "promoted.json").resolve(),
    }
    kwargs["ursim_trace_path" if artifact == "ursim" else "telemetry_path"] = changed_path
    with pytest.raises(ValueError):
        promotion.promote(**kwargs)
