from __future__ import annotations

import csv
import hashlib
import json
from pathlib import Path
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from step5d_parameter_outbox import (  # noqa: E402
    PostprocessError,
    _matching_float,
    enqueue_postprocess_task,
    process_pending_tasks,
    process_postprocess_task,
)


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _fixture(
    tmp_path: Path,
    *,
    capture_trial_uid: str = "trial-7",
    capture_root_override: Path | None = None,
) -> dict[str, Path]:
    campaign_root = tmp_path / "campaign"
    outbox_root = campaign_root / "parameter_outbox"
    capture_root = tmp_path / "captures"
    capture_root.mkdir()
    capture_parent = capture_root_override or capture_root
    capture_path = capture_parent / "bridge" / "capture.csv"
    capture_path.parent.mkdir(parents=True)
    dispatch_sequence = 7
    dispatch_identity = "dispatch:v1:test-seven"
    candidate_uid = "control:v3:test-seven"
    request_uid = "request:v1:test-seven"
    trial_uid = "trial-7"
    packet = {
        "campaign_epoch": 1,
        "trial_id": 7,
        "candidate_token": 700,
        "execution_profile_id": 744,
        "command_seq": 7,
        "logical_batch_sequence": dispatch_sequence,
    }
    overlay = {
        "control_candidate_uid": candidate_uid,
        "execution_profile_id": "nf500-slew250-a250",
        "force_p_gain": 0.002,
        "force_i_gain": 5e-6,
        "force_damping": 14.0,
        "normal_filter_tau_s": 0.7,
        "orientation_ko": 0.4,
        "motion_kp": 1.5,
    }
    dispatch = {
        "schema": "step5d.parameter-receiver/dispatch-v1",
        "dispatch_identity": dispatch_identity,
        "dispatch_sequence": dispatch_sequence,
        "packet": packet,
        "request": {
            "schema": "step5d.parameter-receiver/request-v1",
            "request_uid": request_uid,
            "control_candidate_uid": candidate_uid,
            "source": "test",
            "overlay": overlay,
        },
    }
    dispatch_path = (
        campaign_root
        / "control"
        / "parameter_receiver"
        / "dispatches"
        / "000000000007.json"
    )
    _write_json(dispatch_path, dispatch)

    fieldnames = [
        "t_monotonic_s",
        "_step4e_path_time_s",
        "ur_output_double_register_35",
        "_step4e_force_b_x",
        "_step4e_force_b_y",
        "_step4e_force_b_z",
        "_step4e_normal_load_n",
        "campaign_epoch",
        "trial_id",
        "candidate_token",
        "execution_profile_id",
        "command_seq",
        "ur_output_int_register_34",
        "autotune_trial_uid",
        "autotune_backend_id",
        "autotune_control_candidate_uid",
        "autotune_force_p_gain",
        "autotune_force_i_gain",
        "autotune_force_damping",
        "autotune_orientation_ko",
        "autotune_motion_kp",
        "autotune_normal_filter_tau_s",
        "_step5d_applied_force_p_gain",
        "_step5d_applied_force_i_gain",
        "_step5d_applied_force_damping",
        "_step5d_applied_orientation_ko",
        "_step5d_applied_motion_kp",
        "_step5d_normal_filter_tau_s",
        "_step5d_contact_orientation_error_rad",
        "_step5d_normal_rate_limiter_active",
        "_step5d_rnn_qdot_max_abs_raw_rad_s",
        "_step5d_qdot_cap_rad_s",
        "_step5d_qdot_slew_limiter_active",
        *[f"ur_actual_qdd_{index}" for index in range(6)],
    ]
    with capture_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for index in range(550):
            writer.writerow(
                {
                    "t_monotonic_s": 5.05 + index * 0.1,
                    "_step4e_path_time_s": 5.05 + index * 0.1,
                    "ur_output_double_register_35": 25,
                    "_step4e_force_b_x": 0,
                    "_step4e_force_b_y": 0,
                    "_step4e_force_b_z": 12.2,
                    "_step4e_normal_load_n": 12.2,
                    "campaign_epoch": 1,
                    "trial_id": 7,
                    "candidate_token": 700,
                    "execution_profile_id": 744,
                    "command_seq": 7,
                    "ur_output_int_register_34": dispatch_sequence,
                    "autotune_trial_uid": capture_trial_uid,
                    "autotune_backend_id": "test-backend",
                    "autotune_control_candidate_uid": candidate_uid,
                    "autotune_force_p_gain": 0.002,
                    "autotune_force_i_gain": 5e-6,
                    "autotune_force_damping": 14.0,
                    "autotune_orientation_ko": 0.4,
                    "autotune_motion_kp": 1.5,
                    "autotune_normal_filter_tau_s": 0.7,
                    "_step5d_applied_force_p_gain": 0.002,
                    "_step5d_applied_force_i_gain": 5e-6,
                    "_step5d_applied_force_damping": 14.0,
                    "_step5d_applied_orientation_ko": 0.4,
                    "_step5d_applied_motion_kp": 1.5,
                    "_step5d_normal_filter_tau_s": 0.7,
                    "_step5d_contact_orientation_error_rad": 0.01,
                    "_step5d_normal_rate_limiter_active": 0,
                    "_step5d_rnn_qdot_max_abs_raw_rad_s": 0.01,
                    "_step5d_qdot_cap_rad_s": 0.15,
                    "_step5d_qdot_slew_limiter_active": 0,
                    **{f"ur_actual_qdd_{axis}": 0.1 for axis in range(6)},
                }
            )

    result_key = hashlib.sha256(dispatch_identity.encode("utf-8")).hexdigest()
    result_path = campaign_root / "parameter_results" / f"{result_key}.json"
    task_path = enqueue_postprocess_task(
        outbox_root,
        dispatch_sequence=dispatch_sequence,
        dispatch_identity=dispatch_identity,
        trial_uid=trial_uid,
        capture_path=capture_path,
        result_path=result_path,
    )
    _write_json(
        result_path,
        {
            "schema": "step5d.parameter-receiver/trial-result-v3",
            "status": "SUCCEEDED",
            "dispatch_identity": dispatch_identity,
            "dispatch_sequence": dispatch_sequence,
            "request_uid": request_uid,
            "trial_uid": trial_uid,
            "capture": str(capture_path),
            "receipt": {
                "status": "SUCCEEDED",
                "request_uid": request_uid,
            },
        },
    )
    campaign_config = tmp_path / "campaign-config.json"
    _write_json(
        campaign_config,
        {
            "baseline": {
                "target_force_n": 12,
                "f0_shadow_reaction_normal_base": [0, 0, 1],
            }
        },
    )
    control_contract = tmp_path / "control-contract.json"
    _write_json(
        control_contract,
        {
            "execution_profile_id": "nf500-slew250-a250",
            "effective_fields": {
                "control_invariant": {"target_force_n": 12},
                "safety_invariant": {
                    "bridge_normal_max_rate_rad_s": 0.5,
                    "step5d_autotune_speedj_acceleration_rad_s2": 2.5,
                },
            },
        },
    )
    return {
        "outbox_root": outbox_root,
        "capture_root": capture_root,
        "task_path": task_path,
        "campaign_config": campaign_config,
        "control_contract": control_contract,
    }


def _process(paths: dict[str, Path]) -> Path:
    return process_postprocess_task(
        paths["task_path"],
        outbox_root=paths["outbox_root"],
        capture_root=paths["capture_root"],
        campaign_config_path=paths["campaign_config"],
        control_contract_path=paths["control_contract"],
    )


def test_consumer_validates_identity_and_writes_immutable_metrics(
    tmp_path: Path,
):
    paths = _fixture(tmp_path)
    result_path = _process(paths)
    payload = json.loads(result_path.read_text(encoding="utf-8"))

    assert payload["status"] == "SUCCEEDED"
    assert payload["objective"]["complete_bins"] == 550
    assert payload["objective"]["required_bins"] == 550
    assert payload["objective"]["mae_n"] == pytest.approx(0.2)
    assert payload["candidate"]["normal_filter_tau_s"] == 0.7
    assert payload["identity"]["logical_batch_sequence"] == 7
    assert payload["optimizer"] == {
        "required": False,
        "status": "NOT_REQUESTED",
    }
    assert Path(payload["diagnostic_plot"]["path"]).is_file()
    assert _process(paths) == result_path


def test_parameter_echo_accepts_nine_digit_transport_rounding_but_not_drift():
    rows = [{"_step5d_applied_force_damping": "8.32444981"}]
    expected = 8.324449805019047

    assert _matching_float(
        rows,
        field="_step5d_applied_force_damping",
        expected=expected,
    ) == pytest.approx(8.32444981)
    with pytest.raises(PostprocessError, match="differs from immutable"):
        _matching_float(
            rows,
            field="_step5d_applied_force_damping",
            expected=8.3244,
        )


def test_consumer_rejects_capture_trial_identity_mismatch(tmp_path: Path):
    paths = _fixture(tmp_path, capture_trial_uid="trial-other")

    with pytest.raises(PostprocessError, match="trial UID differs"):
        _process(paths)

    assert not (paths["outbox_root"] / "results").exists()


def test_consumer_rejects_capture_outside_allowed_root(tmp_path: Path):
    outside = tmp_path / "outside"
    paths = _fixture(tmp_path, capture_root_override=outside)

    with pytest.raises(PostprocessError, match="escapes its allowed root"):
        _process(paths)

    assert not (paths["outbox_root"] / "results").exists()


def test_pending_runner_canonicalizes_relative_cli_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    paths = _fixture(tmp_path)
    monkeypatch.chdir(tmp_path)

    result = process_pending_tasks(
        outbox_root=paths["outbox_root"].relative_to(tmp_path),
        capture_root=paths["capture_root"].relative_to(tmp_path),
        campaign_config_path=paths["campaign_config"].relative_to(tmp_path),
        control_contract_path=paths["control_contract"].relative_to(tmp_path),
        limit=1,
    )

    assert result["status"] == "SUCCEEDED"
    assert result["tasks_seen"] == 1
    assert len(result["completed"]) == 1
