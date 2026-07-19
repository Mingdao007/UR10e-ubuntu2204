from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT.parents[1] / "src" / "ur10e_experiment_runtime"))

import promote_step5d_autotune_v3_stopping_bound_evidence as promotion  # noqa: E402
import run_step5d_autotune_campaign as campaign_runner  # noqa: E402
from step5d_autotune_v3.arming import BridgeStartContext  # noqa: E402
from step5d_autotune_v3.identity_layers import (  # noqa: E402
    release_basis_fingerprint,
)
from step5d_autotune_v3.profile import active_identity_snapshot  # noqa: E402
from ur10e_experiment_runtime.authorization import (  # noqa: E402
    CERTIFICATION_PROCEDURES,
    CertificationMotionAuthorization,
    STEP5D_V3_STAGE_IDENTITY,
)
from ur10e_experiment_runtime.moving_sphere import (  # noqa: E402
    load_stopping_bound_artifact,
)


def _write(path: Path, payload: object) -> Path:
    path.write_text(
        json.dumps(payload, indent=2, sort_keys=True, allow_nan=False) + "\n",
        encoding="utf-8",
    )
    return path.resolve()


def _fixture(tmp_path: Path) -> dict[str, object]:
    identity = active_identity_snapshot()
    readback = (tmp_path / "readback.json").resolve()
    readback.write_bytes(
        (ROOT / "config/step5d_autotune_controller_readback_v3.json").read_bytes()
    )
    readback_sha = promotion._sha256_path(readback)
    runtime_environment = "d" * 64
    release_basis = release_basis_fingerprint(
        tick_semantics_fingerprint=identity["tick_semantics_fingerprint"],
        timing_harness_fingerprint=identity["timing_harness_fingerprint"],
        runtime_environment_fingerprint=runtime_environment,
        deployment_fingerprint=identity["deployment_fingerprint"],
        orchestration_fingerprint=identity["orchestration_fingerprint"],
        plant_epoch=7,
    )
    bridge_context = BridgeStartContext(
        tick_semantics_fingerprint=identity["tick_semantics_fingerprint"],
        timing_harness_fingerprint=identity["timing_harness_fingerprint"],
        runtime_environment_fingerprint=runtime_environment,
        deployment_fingerprint=identity["deployment_fingerprint"],
        orchestration_fingerprint=identity["orchestration_fingerprint"],
        release_basis_fingerprint=release_basis,
        local_triplet_sha256=identity["local_triplet_sha256"],
        plant_epoch=7,
        deployment_readback_sha256=readback_sha,
        timing_acceptance_sha256="e" * 64,
    )
    bridge_context_path = _write(
        tmp_path / "bridge-start-context.json",
        bridge_context.document(),
    )
    issued = datetime.now(timezone.utc) - timedelta(minutes=1)
    authorization = CertificationMotionAuthorization(
        stage_identity=STEP5D_V3_STAGE_IDENTITY,
        release_basis_fingerprint=release_basis,
        deployment_fingerprint=identity["deployment_fingerprint"],
        plant_epoch=7,
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
    authorization_path = _write(
        tmp_path / "authorization.json", authorization.document()
    )

    def trial(procedure: str, sample_index: int) -> dict[str, object]:
        offset = float(sample_index - 1)
        trigger = 10.0 + offset
        stop = trigger + 0.02
        stationary = trigger + 0.06
        return {
            "procedure": procedure,
            "sample_index": sample_index,
            "contact_observed": False,
            "trigger_controller_timestamp_s": trigger,
            "stop_transport_controller_timestamp_s": stop,
            "stationary_controller_timestamp_s": stationary,
            "speed_samples": [
                {"controller_timestamp_s": trigger, "tcp_speed_m_s": 0.040},
                {"controller_timestamp_s": trigger + 0.01, "tcp_speed_m_s": 0.041},
                {"controller_timestamp_s": stop, "tcp_speed_m_s": 0.042},
                {"controller_timestamp_s": trigger + 0.04, "tcp_speed_m_s": 0.021},
                {"controller_timestamp_s": stationary, "tcp_speed_m_s": 0.0005},
            ],
        }

    measurement = {
        "schema": promotion.RAW_SCHEMA,
        "candidate_stage_id": "step5d_strict_rnn_autotune_v3",
        "release_basis_fingerprint": release_basis,
        "deployment_fingerprint": identity["deployment_fingerprint"],
        "plant_epoch": 7,
        "deployment_readback_sha256": readback_sha,
        "certification_authorization_sha256": authorization.authorization_ref_sha256,
        "all_samples_retained": True,
        "trials": [
            trial(procedure, index)
            for procedure in promotion.STOP_PROCEDURES
            for index in range(1, 4)
        ],
    }
    measurement_path = _write(tmp_path / "measurement.json", measurement)
    return {
        "bridge_context": bridge_context,
        "bridge_context_path": bridge_context_path,
        "readback": readback,
        "readback_sha": readback_sha,
        "authorization": authorization,
        "authorization_path": authorization_path,
        "measurement": measurement,
        "measurement_path": measurement_path,
    }


def test_promoted_stopping_bound_round_trips_through_strict_loader(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    output = (tmp_path / "promoted.json").resolve()
    document = promotion.promote(
        measurement_path=fixture["measurement_path"],
        bridge_start_context_path=fixture["bridge_context_path"],
        authorization_path=fixture["authorization_path"],
        deployment_readback_path=fixture["readback"],
        output_path=output,
    )
    artifact = load_stopping_bound_artifact(
        output,
        expected_validity_domain=document["validity_domain"],
        expected_source_binding_sha256=document["source_binding_sha256"],
        expected_stop_transport_sha256=document["stop_transport_sha256"],
        expected_deployment_readback_sha256=fixture["readback_sha"],
        certification_authorization=fixture["authorization"],
        expected_plant_epoch=7,
    )
    assert artifact.certified is True
    assert artifact.reaction_latency_s == pytest.approx(0.02)
    assert artifact.acceleration_growth_m_s2 == pytest.approx(0.1)
    assert artifact.minimum_deceleration_m_s2 > 1.0
    assert artifact.numeric_margin_m == 0.0001


@pytest.mark.parametrize(
    "mutation",
    [
        lambda payload: payload.update(all_samples_retained=False),
        lambda payload: payload["trials"].pop(),
        lambda payload: payload["trials"][0].update(contact_observed=True),
        lambda payload: payload["trials"][0]["speed_samples"][3].update(
            tcp_speed_m_s=0.043
        ),
        lambda payload: payload.update(certification_authorization_sha256="f" * 64),
    ],
)
def test_stopping_promotion_fails_closed_on_measurement_mutation(
    tmp_path: Path, mutation
) -> None:
    fixture = _fixture(tmp_path)
    mutation(fixture["measurement"])
    measurement = _write(tmp_path / "mutated.json", fixture["measurement"])
    with pytest.raises(ValueError):
        promotion.promote(
            measurement_path=measurement,
            bridge_start_context_path=fixture["bridge_context_path"],
            authorization_path=fixture["authorization_path"],
            deployment_readback_path=fixture["readback"],
            output_path=(tmp_path / "promoted.json").resolve(),
        )


def test_stopping_promotion_rejects_nonfinite_and_readback_drift(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    nonfinite = (tmp_path / "nonfinite.json").resolve()
    nonfinite.write_text('{"value":NaN}', encoding="utf-8")
    with pytest.raises(ValueError, match="non-finite"):
        promotion.promote(
            measurement_path=nonfinite,
            bridge_start_context_path=fixture["bridge_context_path"],
            authorization_path=fixture["authorization_path"],
            deployment_readback_path=fixture["readback"],
            output_path=(tmp_path / "promoted.json").resolve(),
        )
    fixture["readback"].write_text("drift", encoding="utf-8")
    with pytest.raises(ValueError, match="deployment readback differs"):
        promotion.promote(
            measurement_path=fixture["measurement_path"],
            bridge_start_context_path=fixture["bridge_context_path"],
            authorization_path=fixture["authorization_path"],
            deployment_readback_path=fixture["readback"],
            output_path=(tmp_path / "promoted.json").resolve(),
        )


def test_strict_loader_rejects_bound_artifact_tamper(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    output = (tmp_path / "promoted.json").resolve()
    document = promotion.promote(
        measurement_path=fixture["measurement_path"],
        bridge_start_context_path=fixture["bridge_context_path"],
        authorization_path=fixture["authorization_path"],
        deployment_readback_path=fixture["readback"],
        output_path=output,
    )
    document["certification_binding_sha256"] = "0" * 64
    tampered = _write(tmp_path / "tampered.json", document)
    with pytest.raises(ValueError, match="certification binding"):
        load_stopping_bound_artifact(
            tampered,
            expected_validity_domain=document["validity_domain"],
            expected_source_binding_sha256=document["source_binding_sha256"],
            expected_stop_transport_sha256=document["stop_transport_sha256"],
            expected_deployment_readback_sha256=fixture["readback_sha"],
            certification_authorization=fixture["authorization"],
            expected_plant_epoch=7,
        )


def test_certification_authorization_cannot_authorize_a_campaign(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    with pytest.raises(RuntimeError, match="campaign arming context is invalid"):
        campaign_runner._campaign_authorization(
            fixture["authorization_path"],
            campaign=object(),
            campaign_fingerprint="a" * 64,
        )
