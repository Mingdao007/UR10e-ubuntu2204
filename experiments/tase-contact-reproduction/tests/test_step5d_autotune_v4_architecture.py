from __future__ import annotations

import hashlib
import copy
import json
import sys
from dataclasses import asdict, replace
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import step5d_force_search_canary_shared as force_search_canary_shared  # noqa: E402
import step5d_force_search_canary_r006 as r006  # noqa: E402
import step5d_force_search_canary_r007 as r007  # noqa: E402
import step5d_force_search_primitive as r005  # noqa: E402
import transition_step5d_lineage as lineage_transition  # noqa: E402
from step5d_autotune_v4 import contracts  # noqa: E402
from step5d_autotune_v4.baseline import (  # noqa: E402
    BaselineCommand,
    BaselineObservation,
    BaselinePhase,
    BaselineState,
)
from step5d_autotune_v4.baseline_ledger import (  # noqa: E402
    BaselineLedgerError,
    BaselineQualificationLedger,
    BaselineSuccessReceipt,
)
from step5d_autotune_v4.control import (  # noqa: E402
    RuntimeObservation,
    V4ControlPrimitive,
)
from step5d_autotune_v4.policies import (  # noqa: E402
    DefaultQdotGatePolicy,
    V4PolicyBundle,
)
from step5d_autotune_v4.runtime import (  # noqa: E402
    KinematicGateResult,
    RuntimeGuardError,
)
from step5d_eoat_profiles import (  # noqa: E402
    ControllerEOATReadback,
    EoatProfileError,
    apply_and_verify_eoat,
    load_new_eoat_profile,
    load_old_eoat_profile,
)


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class ImmediateSuccessBaseline:
    implementation_id = "test.baseline/immediate-success-v1"

    def step(self, candidate, state, observation):
        del state
        successful = BaselineState(phase=BaselinePhase.SUCCESS)
        return successful, BaselineCommand(
            phase=BaselinePhase.SUCCESS,
            internal_setpoint_n=5.0,
            candidate_target_force_n=candidate.target_force_n,
            approach_speed_m_s=0.0,
            xy_velocity_m_s=(0.0, 0.0),
            angular_velocity_rad_s=(0.0, 0.0, 0.0),
            stop=False,
            retract_allowed=observation.sensor_fresh and observation.stationary,
            auto_home=False,
            reason="",
        )


class StopOnSuccessBaseline(ImmediateSuccessBaseline):
    implementation_id = "test.baseline/stop-on-success-v1"

    def step(self, candidate, state, observation):
        successful, command = super().step(candidate, state, observation)
        return successful, replace(command, stop=True, reason="provider_stop")


class MaliciousQdotGate:
    implementation_id = "test.qdot/malicious-v1"

    def gate(self, *args, **kwargs):
        del args, kwargs
        return KinematicGateResult(
            allowed=True,
            qdot=(1.0,) * 6,
            twist=(1.0,) * 6,
            total_linear_m_s=1.0,
            normal_m_s=1.0,
            tangential_m_s=1.0,
            angular_rad_s=1.0,
            reason="",
        )


def _observation(time_s: float, heartbeat: float) -> RuntimeObservation:
    return RuntimeObservation(
        monotonic_s=time_s,
        heartbeat=heartbeat,
        one_newton_latched=True,
        filtered_normal_n=5.0,
        raw_normal_n=5.0,
        force_norm_n=5.0,
        torque_norm_nm=0.1,
        sensor_fresh=True,
        stationary=True,
    )


def _identity():
    return tuple(
        tuple(1.0 if row == column else 0.0 for column in range(6))
        for row in range(6)
    )


def _receipt(contract, attempt: str, digest: str = "a" * 64):
    return BaselineSuccessReceipt(
        attempt_id=attempt,
        terminal_stage=22,
        campaign_fingerprint=contract.campaign_fingerprint,
        eoat_sha256=contract.eoat_sha256,
        target_force_n=5.0,
        sensor_authority="kunwei_only",
        completion_sha256=digest,
    )


def _contract_with_provider(contract, role: str, implementation_id: str):
    raw = copy.deepcopy(contract.raw)
    raw["policy_binding"]["providers"][role] = implementation_id
    return replace(contract, raw=raw)


def test_policy_bundle_is_injected_and_alternate_baseline_runs_without_parent_edit() -> None:
    contract = contracts.load_contract()
    bundle = V4PolicyBundle.defaults()
    bundle.baseline = ImmediateSuccessBaseline()
    control = V4ControlPrimitive(
        _contract_with_provider(
            contract, "baseline", bundle.baseline.implementation_id
        ),
        contracts.V4Candidate(),
        policies=bundle,
        attempt_id="alternate-baseline",
    )
    common = dict(
        proposed_qdot=(0.0,) * 6,
        jacobian_6x6=_identity(),
        normal_base=(0.0, 0.0, 1.0),
        observed_model_hashes=contract.model_hashes,
    )
    control.step(_observation(0.0, 0.0), **common)
    control.step(_observation(0.01, 1.0), **common)
    decision = control.step(_observation(0.02, 2.0), **common)

    assert decision.phase == "success"
    assert decision.retract_allowed
    assert control.successful_baseline_holds == 1
    assert bundle.baseline.implementation_id == "test.baseline/immediate-success-v1"


def test_baseline_provider_stop_is_stop_dominant_even_on_success() -> None:
    contract = contracts.load_contract()
    bundle = V4PolicyBundle.defaults()
    bundle.baseline = StopOnSuccessBaseline()
    control = V4ControlPrimitive(
        _contract_with_provider(
            contract, "baseline", bundle.baseline.implementation_id
        ),
        contracts.V4Candidate(),
        policies=bundle,
        attempt_id="provider-stop",
    )
    common = dict(
        proposed_qdot=(0.0001,) * 6,
        jacobian_6x6=_identity(),
        normal_base=(0.0, 0.0, 1.0),
        observed_model_hashes=contract.model_hashes,
    )
    control.step(_observation(0.0, 0.0), **common)
    control.step(_observation(0.01, 1.0), **common)
    decision = control.step(_observation(0.02, 2.0), **common)

    assert decision.stop
    assert decision.qdot == (0.0,) * 6
    assert decision.reason == "provider_stop"


def test_missing_non_safety_provider_degrades_to_zero_qdot_no_motion() -> None:
    contract = contracts.load_contract()
    degraded = V4PolicyBundle.safe_degraded(missing=("qdot_gate",))
    control = V4ControlPrimitive(
        _contract_with_provider(
            contract, "qdot_gate", degraded.qdot_gate.implementation_id
        ),
        contracts.V4Candidate(),
        policies=degraded,
    )
    common = dict(
        proposed_qdot=(0.0,) * 6,
        jacobian_6x6=_identity(),
        normal_base=(0.0, 0.0, 1.0),
        observed_model_hashes=contract.model_hashes,
    )
    control.step(_observation(0.0, 0.0), **common)
    control.step(_observation(0.01, 1.0), **common)
    decision = control.step(_observation(0.02, 2.0), **common)

    assert decision.stop
    assert decision.qdot == (0.0,) * 6
    assert decision.reason == "qdot_gate_provider_unavailable"


def test_missing_unused_behavior_provider_is_also_machine_blocked_no_motion() -> None:
    contract = contracts.load_contract()
    degraded = V4PolicyBundle.safe_degraded(missing=("force_search",))
    control = V4ControlPrimitive(
        _contract_with_provider(
            contract, "force_search", degraded.force_search.implementation_id
        ),
        contracts.V4Candidate(),
        policies=degraded,
    )
    common = dict(
        proposed_qdot=(0.0001,) * 6,
        jacobian_6x6=_identity(),
        normal_base=(0.0, 0.0, 1.0),
        observed_model_hashes=contract.model_hashes,
    )
    decision = control.step(_observation(0.0, 0.0), **common)

    assert decision.stop
    assert decision.qdot == (0.0,) * 6
    assert decision.reason == "force_search_provider_unavailable"


def test_malicious_provider_cannot_bypass_fixed_invariant_envelope() -> None:
    contract = contracts.load_contract()
    bundle = V4PolicyBundle.defaults()
    bundle.qdot_gate = MaliciousQdotGate()
    control = V4ControlPrimitive(
        _contract_with_provider(
            contract, "qdot_gate", bundle.qdot_gate.implementation_id
        ),
        contracts.V4Candidate(),
        policies=bundle,
    )
    common = dict(
        proposed_qdot=(0.0,) * 6,
        jacobian_6x6=_identity(),
        normal_base=(0.0, 0.0, 1.0),
        observed_model_hashes=contract.model_hashes,
    )
    control.step(_observation(0.0, 0.0), **common)
    control.step(_observation(0.01, 1.0), **common)
    decision = control.step(_observation(0.02, 2.0), **common)

    assert decision.stop and decision.freeze_bo
    assert decision.qdot == (0.0,) * 6
    assert decision.reason.startswith("kinematic_gate:")


def test_baseline_ledger_is_idempotent_unique_owner_and_third_receipt_unlocks() -> None:
    contract = contracts.load_contract()
    ledger = BaselineQualificationLedger(contract)

    assert ledger.record_success(_receipt(contract, "a"))
    assert not ledger.record_success(_receipt(contract, "a"))
    assert ledger.record_success(_receipt(contract, "b", "b" * 64))
    assert not ledger.snapshot().full_path_allowed
    assert ledger.record_success(_receipt(contract, "c", "c" * 64))
    assert ledger.snapshot().full_path_allowed
    with pytest.raises(BaselineLedgerError, match="binding"):
        ledger.record_success(
            replace(_receipt(contract, "d", "d" * 64), eoat_sha256="0" * 64)
        )


def test_baseline_failure_before_stage22_resets_but_later_safety_freezes() -> None:
    contract = contracts.load_contract()
    ledger = BaselineQualificationLedger(contract)
    ledger.record_success(_receipt(contract, "a"))
    ledger.record_failure(
        attempt_id="b", terminal_stage=21, reason="stale", safety_or_structural=False
    )
    assert ledger.snapshot().consecutive_successes == 0
    ledger.record_success(_receipt(contract, "c", "c" * 64))
    ledger.record_failure(
        attempt_id="d", terminal_stage=90, reason="safety", safety_or_structural=True
    )
    snapshot = ledger.snapshot()
    assert snapshot.consecutive_successes == 1
    assert snapshot.frozen and not snapshot.full_path_allowed


class FakeController:
    def __init__(self) -> None:
        self.payload = 0.0
        self.cog = (0.0, 0.0, 0.0)
        self.tcp = (0.0,) * 6
        self.safety = "NORMAL"
        self.single_writer = True

    def set_target_payload(self, payload_kg, cog_m):
        self.payload = float(payload_kg)
        self.cog = tuple(cog_m)

    def set_tcp(self, tcp_m_rad):
        self.tcp = tuple(tcp_m_rad)

    def fresh_get_eoat(self):
        return ControllerEOATReadback(
            payload_kg=self.payload,
            cog_m=self.cog,
            tcp_m_rad=self.tcp,
            actual_tcp_speed_m_s_rad_s=(0.0,) * 6,
            safety_mode=self.safety,
            single_writer=self.single_writer,
        )


@pytest.mark.parametrize("loader", [load_old_eoat_profile, load_new_eoat_profile])
def test_old_and_new_eoat_profiles_use_same_apply_verify_primitive(loader) -> None:
    profile = loader()
    receipt = apply_and_verify_eoat(profile, FakeController())

    assert receipt.passed
    assert receipt.profile_sha256 == profile.profile_sha256
    assert profile.program_z_delta_m == 0.0


def test_old_eoat_profile_preserves_current_v3_controller_identity() -> None:
    profile = load_old_eoat_profile()

    assert profile.payload_kg == 0.404
    assert profile.cog_m == (-0.00044, -0.00094, 0.0268)
    assert profile.controller_tcp_m_rad == (0.0, 0.0, 0.1221, 0.0, 0.0, 0.0)


def test_apply_verify_eoat_rejects_non_normal_or_missing_single_writer() -> None:
    controller = FakeController()
    controller.safety = "PROTECTIVE_STOP"
    with pytest.raises(EoatProfileError, match="verification failed"):
        apply_and_verify_eoat(load_new_eoat_profile(), controller)


def test_v4_activation_evidence_is_independent_of_active_v3_eoat_receipt(
    tmp_path: Path,
) -> None:
    contract = contracts.load_contract(contracts.R002_CONTRACT)
    eoat_receipt = apply_and_verify_eoat(
        load_new_eoat_profile(), FakeController()
    )
    evidence = {
        "schema": "step5d.autotune-v4/activation-evidence-v1",
        "campaign_fingerprint": contract.campaign_fingerprint,
        "r006_controller_readback_verified": True,
        "r006_live_success": True,
        "three_5n_baseline_successes": True,
        "formal_review_v3_passed": True,
        "v4_triplet_controller_readback_verified": True,
        "fresh_owner_route_gates": True,
        "baseline_success_receipts": [
            asdict(_receipt(contract, "a")),
            asdict(_receipt(contract, "b", "b" * 64)),
            asdict(_receipt(contract, "c", "c" * 64)),
        ],
        "v4_eoat_apply_verify_receipt": asdict(eoat_receipt),
        "v4_release_pointer": {
            "path": "config/step5d/autotune_v4_r002.json",
            "sha256": contract.sha256,
        },
    }
    evidence_path = tmp_path / "activation-evidence.json"
    evidence_path.write_text(
        json.dumps(evidence, sort_keys=True, allow_nan=False) + "\n",
        encoding="utf-8",
    )

    inspection = lineage_transition.inspect_transition(evidence_path)
    assert inspection["ready"]
    assert inspection["blockers"] == []
    assert inspection["active_eoat"]["live_compatible_now"] is False


def test_force_search_wrappers_share_engine_and_r006_triplet_bytes_are_golden() -> None:
    assert "ForceSearchEngine" in Path(
        force_search_canary_shared.__file__
    ).read_text(encoding="utf-8")
    assert "canary_stop_reason" in Path(r006.__file__).read_text(encoding="utf-8")
    assert "canary_stop_reason" in Path(r007.__file__).read_text(encoding="utf-8")
    assert "render_force_search_canary_script" in Path(
        r006.__file__
    ).read_text(encoding="utf-8")
    assert "render_force_search_canary_script" in Path(
        r007.__file__
    ).read_text(encoding="utf-8")
    assert r006.stop_reason is force_search_canary_shared.canary_stop_reason
    assert r007.stop_reason is force_search_canary_shared.canary_stop_reason
    assert (
        r006.render_script.__globals__["render_force_search_canary_script"]
        is force_search_canary_shared.render_force_search_canary_script
    )
    assert (
        r007.render_script.__globals__["render_force_search_canary_script"]
        is force_search_canary_shared.render_force_search_canary_script
    )
    triplet = ROOT / "programs/step5/step5d"
    assert _sha(triplet / "step5d_force_search_canary_r006.script") == (
        "de8d071885d21274cd44b1b33408e51b52caf7342aecb099c12f75c8fbd62922"
    )
    assert _sha(triplet / "step5d_force_search_canary_r006.txt") == (
        "4910584992b62e8fccee1849a4650d5f287a831b8345734dcbb936204c593698"
    )
    assert _sha(triplet / "step5d_force_search_canary_r006.urp") == (
        "bc9044e7621ffcfa7aa785b04affbaab5a558137aa015006c7c6b2cff3175c26"
    )


def test_v4_r001_is_superseded_and_v3_bytes_remain_unchanged() -> None:
    marker = ROOT / (
        "programs/step5/step5d/"
        "step5d_strict_rnn_autotune_v4_r001.SUPERSEDED.json"
    )
    assert '"load_or_play_allowed": false' in marker.read_text(encoding="utf-8")
    assert _sha(ROOT / "config/step5d/current.json") == (
        "646edefaf6fbbd76b0cbfe86bc00b5d8ac26b787231f9ac345425256bbc78f5e"
    )
    assert _sha(
        ROOT / "programs/step5/step5d/step5d_strict_rnn_autotune_v3_r034.script"
    ) == "6a6af44ebdb79553307c2acb45ba6a2914c26f6dd7b05f393a6c1eb2c2d11f1d"
