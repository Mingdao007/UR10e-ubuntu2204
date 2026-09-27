from __future__ import annotations

import copy
import json
import sys
from pathlib import Path

import numpy as np
import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from contact_laws import ContactLawSnapshot  # noqa: E402
from contact_yield_method_registry import (  # noqa: E402
    MethodUnavailableError,
    load_method_records,
    resolve_method,
)
from tase_sfc_tangent_augmentation import (  # noqa: E402
    METHOD_ID,
    PHASE_TEMPLATE_METHOD_ID,
    PHASE_TEMPLATE_SCHEMA,
    SCHEMA,
    TaseSfcTangentAugmentationError,
    TaseSfcTangentAugmentationRuntime,
    load_frozen_sfc_parameters,
    load_phase_force_template,
)


class _FakeLaw:
    def __init__(self, _method, parameters, *, dt_s):
        self.parameters = dict(parameters)
        self.dt_s = dt_s
        self.calls = []
        self.state = 0.0
        self.closed = False
        self.velocity = (0.00005, 0.00004, -0.00003)

    def step(self, force, dt_s):
        self.calls.append((tuple(force), dt_s))
        self.state += float(np.linalg.norm(force)) * dt_s
        return self.velocity

    def snapshot(self):
        values = [0.0] * 22
        values[0] = self.state
        return ContactLawSnapshot(tuple(values), binding_id="a" * 64)

    def restore(self, token):
        if token.binding_id != "a" * 64:
            raise ValueError("law binding differs")
        self.state = token.values[0]

    def reset(self):
        self.state = 0.0
        self.calls.clear()

    def close(self):
        self.closed = True


class _FakeRuntime:
    def __init__(self):
        self.state = 0
        self._pending_outer_feedback = {}
        self.closed = False
        self.base_twist = (0.0001, 0.0002, -0.0003, 0.01, 0.02, 0.03)

    def desired_twist(self, **_kwargs):
        self.state += 1
        self._pending_outer_feedback = {
            "force_error_n": 0.2,
            "requested_twist": self.base_twist,
            "explicit_velocity_limit_active": False,
        }
        return self.base_twist

    def dynamic_state_snapshot(self):
        return {"state": self.state, "pending": copy.deepcopy(self._pending_outer_feedback)}

    def restore_dynamic_state(self, state):
        self.state = int(state["state"])
        self._pending_outer_feedback = copy.deepcopy(state["pending"])

    def reset_dynamic_state(self):
        self.state = 0
        self._pending_outer_feedback = {}

    def close(self):
        self.closed = True


class _FakeProvider:
    reference_phase = "path"
    command_normal_base = (0.0, 0.0, 1.0)


def _overlay(*, phase="path", phase_force_template_base_n=None, phase_template_binding=None):
    provider = _FakeProvider()
    provider.reference_phase = phase
    runtime = _FakeRuntime()
    law_holder = {}

    def factory(*args, **kwargs):
        law_holder["law"] = _FakeLaw(*args, **kwargs)
        return law_holder["law"]

    overlay = TaseSfcTangentAugmentationRuntime(
        runtime,
        provider,
        parameters=load_frozen_sfc_parameters(),
        law_factory=factory,
        phase_force_template_base_n=phase_force_template_base_n,
        phase_template_binding=phase_template_binding,
    )
    return overlay, runtime, provider, law_holder["law"]


def test_frozen_profile_is_the_source_bound_native_sfc_set():
    assert load_frozen_sfc_parameters() == {
        "m": 4.0,
        "mu": 393.0,
        "n": 3.0,
        "g": 0.052126826121414976,
    }


def test_live_registry_records_candidate_but_keeps_it_unavailable():
    records = load_method_records()
    record = records[METHOD_ID]
    assert record.family == "tase_sfc_tangent_augment"
    assert record.available is False
    with pytest.raises(MethodUnavailableError, match="live timing"):
        resolve_method(METHOD_ID)


def test_path_overlay_adds_only_tangent_velocity_and_preserves_tase_normal_posture():
    overlay, _base, _provider, law = _overlay()
    result = overlay.desired_twist(
        mode="path",
        actual_tcp_pose=(0.0, 0.0, 0.0, 0.0, 0.0, 0.0),
        force_tcp_n=(1.0, 2.0, 3.0),
        actual_dt_s=0.002,
    )

    np.testing.assert_allclose(result, (0.00015, 0.00024, -0.0003, 0.01, 0.02, 0.03), atol=1e-12)
    assert len(law.calls) == 1
    assert law.calls[0][0] == pytest.approx((1.0, 2.0, 0.0))
    assert law.calls[0][1] == pytest.approx(0.002)
    diagnostics = overlay._last_diagnostics
    assert diagnostics["method_id"] == METHOD_ID
    assert diagnostics["path_spring"] is None
    assert diagnostics["normal_leak_m_s"] <= 1e-12
    assert diagnostics["extra_final_realizer_calls"] == 0
    assert diagnostics["force_source"].startswith("baseline-corrected Kunwei")


def test_phase_template_file_binds_exact_rate460_grid_and_five_a_sources():
    table, binding = load_phase_force_template()

    assert table.shape == (600, 3)
    assert not table.flags.writeable
    assert binding["schema"] == PHASE_TEMPLATE_SCHEMA
    assert binding["protocol_id"] == "figure8_window60_r013_rate460_v1"
    assert len(binding["training_attempt_ids"]) == 5
    assert len(binding["training_timeline_sha256"]) == 5
    assert binding["physical_binding"]["home_profile_id"] == "step6.autotune/figure8-contact-derived-home-v1"
    assert len(binding["physical_binding"]["eoat_identity_sha256"]) == 64


def test_phase_template_loader_allows_four_run_only_for_explicit_leave_one_out(tmp_path):
    source = ROOT / "config/tase_sfc_phase_template_a_v1.json"
    document = json.loads(source.read_text())
    document["template_id"] = "test-four-run-loo"
    document["training_scope"] = "four-attempt-leave-one-out-template"
    document["source_attempts"] = document["source_attempts"][:4]
    path = tmp_path / "four-run-template.json"
    path.write_text(json.dumps(document))

    with pytest.raises(TaseSfcTangentAugmentationError, match="source count"):
        load_phase_force_template(path)
    table, binding = load_phase_force_template(path, allow_leave_one_out=True)
    assert table.shape == (600, 3)
    assert binding["training_scope"] == "four-attempt-leave-one-out-template"
    assert len(binding["training_attempt_ids"]) == 4


def test_phase_template_subtracts_the_phase_bin_in_base_tangent_frame():
    table, binding = load_phase_force_template()
    table = table.copy()
    table[123] = (0.1, 0.2, 0.5)
    overlay, _base, _provider, law = _overlay(
        phase_force_template_base_n=table,
        phase_template_binding=binding,
    )

    overlay.augment_twist(
        (0.0001, 0.0002, -0.0003, 0.01, 0.02, 0.03),
        phase="path",
        mode="path",
        actual_tcp_pose=(0.0, 0.0, 0.0, 0.0, 0.0, 0.0),
        force_tcp_n=(1.0, 2.0, 3.0),
        actual_dt_s=0.002,
        path_time_s=12.345,
    )

    assert law.calls[0][0] == pytest.approx((0.9, 1.8, 0.0))
    assert overlay.composition_binding["method_id"] == PHASE_TEMPLATE_METHOD_ID
    assert overlay._last_diagnostics["raw_tangent_force_base_n"] == pytest.approx([1.0, 2.0, 0.0])
    assert overlay._last_diagnostics["phase_template_force_base_n"] == pytest.approx([0.1, 0.2, 0.0])
    assert overlay._last_diagnostics["tangent_force_base_n"] == pytest.approx([0.9, 1.8, 0.0])


def test_phase_template_requires_a_path_time_inside_the_frozen_table():
    table, binding = load_phase_force_template()
    overlay, _base, _provider, _law = _overlay(
        phase_force_template_base_n=table,
        phase_template_binding=binding,
    )

    with pytest.raises(TaseSfcTangentAugmentationError, match="path_time_s"):
        overlay.augment_twist(
            (0.0,) * 6,
            phase="path",
            mode="path",
            actual_tcp_pose=(0.0,) * 6,
            force_tcp_n=(1.0, 0.0, 0.0),
            actual_dt_s=0.002,
        )
    with pytest.raises(TaseSfcTangentAugmentationError, match=r"inside \[0,60\)"):
        overlay.augment_twist(
            (0.0,) * 6,
            phase="path",
            mode="path",
            actual_tcp_pose=(0.0,) * 6,
            force_tcp_n=(1.0, 0.0, 0.0),
            actual_dt_s=0.002,
            path_time_s=60.0,
        )


def test_phase_template_snapshot_binds_composition_and_restores_all_state():
    table, binding = load_phase_force_template()
    overlay, runtime, _provider, law = _overlay(
        phase_force_template_base_n=table,
        phase_template_binding=binding,
    )
    checkpoint = overlay.dynamic_state_snapshot()
    assert checkpoint["method_id"] == PHASE_TEMPLATE_METHOD_ID
    overlay.augment_twist(
        (0.0,) * 6,
        phase="path",
        mode="path",
        actual_tcp_pose=(0.0,) * 6,
        force_tcp_n=(1.0, 0.0, 0.0),
        actual_dt_s=0.002,
        path_time_s=10.0,
    )
    assert runtime.state == 0
    assert law.state > 0.0
    overlay.restore_dynamic_state(checkpoint)
    assert runtime.state == 0
    assert law.state == 0.0
    bad = copy.deepcopy(checkpoint)
    bad["method_id"] = METHOD_ID
    with pytest.raises(TaseSfcTangentAugmentationError, match="identity"):
        overlay.restore_dynamic_state(bad)
    assert runtime.state == 0
    assert law.state == 0.0


def test_recorded_twist_entrypoint_uses_the_same_frozen_sfc_augmentation():
    overlay, runtime, _provider, law = _overlay()
    result = overlay.augment_twist(
        (0.0001, 0.0002, -0.0003, 0.01, 0.02, 0.03),
        phase="path",
        mode="path",
        actual_tcp_pose=(0.0, 0.0, 0.0, 0.0, 0.0, 0.0),
        force_tcp_n=(1.0, 2.0, 3.0),
        actual_dt_s=0.002,
    )
    np.testing.assert_allclose(
        result,
        (0.00015, 0.00024, -0.0003, 0.01, 0.02, 0.03),
        atol=1e-12,
    )
    assert runtime.state == 0  # replay bypasses the live outer-loop recomputation
    assert len(law.calls) == 1


def test_entry_and_baseline_do_not_advance_sfc_and_path_entry_starts_fresh():
    overlay, _base, provider, law = _overlay(phase="entry")
    kwargs = {
        "mode": "path",
        "actual_tcp_pose": (0.0, 0.0, 0.0, 0.0, 0.0, 0.0),
        "force_tcp_n": (3.0, 1.0, 2.0),
        "actual_dt_s": 0.002,
    }
    first = overlay.desired_twist(**kwargs)
    assert first == (0.0001, 0.0002, -0.0003, 0.01, 0.02, 0.03)
    assert law.calls == []

    provider.reference_phase = "path"
    second = overlay.desired_twist(**kwargs)
    assert second == pytest.approx((0.00015, 0.00024, -0.0003, 0.01, 0.02, 0.03))
    assert len(law.calls) == 1

    provider.reference_phase = "entry"
    overlay.desired_twist(**kwargs)
    assert overlay._active_path is False
    assert law.state == 0.0


def test_native_step_is_split_at_existing_four_millisecond_limit():
    overlay, _base, _provider, law = _overlay()
    overlay.desired_twist(
        mode="path",
        actual_tcp_pose=(0.0, 0.0, 0.0, 0.0, 0.0, 0.0),
        force_tcp_n=(1.0, 2.0, 0.0),
        actual_dt_s=0.010,
    )
    assert len(law.calls) == 3
    assert all(0.0 < dt <= 0.004 for _force, dt in law.calls)
    assert sum(dt for _force, dt in law.calls) == pytest.approx(0.010)


def test_sfc_delta_is_scaled_inside_existing_tangent_cap_without_changing_normal():
    overlay, base, _provider, law = _overlay()
    base.base_twist = (0.00034, 0.0, -0.0003, 0.0, 0.0, 0.0)
    law.velocity = (0.0002, 0.0, 0.0)
    result = overlay.desired_twist(
        mode="path",
        actual_tcp_pose=(0.0, 0.0, 0.0, 0.0, 0.0, 0.0),
        force_tcp_n=(1.0, 0.0, 0.0),
        actual_dt_s=0.002,
    )
    assert result[0] == pytest.approx(0.00035, abs=1e-12)
    assert result[2] == pytest.approx(-0.0003, abs=1e-12)
    assert overlay._last_diagnostics["sfc_velocity_scale_from_existing_caps"] == pytest.approx(0.05)
    pending = overlay._runtime._pending_outer_feedback
    assert pending["explicit_velocity_limit_active"] is True
    assert pending["requested_twist"][2] == pytest.approx(-0.0003)


def test_dynamic_snapshot_restores_base_and_native_law_as_one_state():
    overlay, base, provider, law = _overlay()
    checkpoint = overlay.dynamic_state_snapshot()
    overlay.desired_twist(
        mode="path",
        actual_tcp_pose=(0.0, 0.0, 0.0, 0.0, 0.0, 0.0),
        force_tcp_n=(1.0, 2.0, 0.0),
        actual_dt_s=0.002,
    )
    assert base.state == 1
    assert law.state > 0
    overlay.restore_dynamic_state(checkpoint)
    assert base.state == 0
    assert law.state == 0.0
    assert overlay._active_path is False

    bad = copy.deepcopy(checkpoint)
    bad["method_id"] = "TASE_RNN_MATURE"
    with pytest.raises(TaseSfcTangentAugmentationError, match="identity"):
        overlay.restore_dynamic_state(bad)
    assert base.state == 0
    assert law.state == 0.0
    assert provider.reference_phase == "path"


def test_runtime_proxy_delegates_single_final_command_interface():
    overlay, base, _provider, _law = _overlay()
    overlay.outer_loop_config = "same delegated config slot"
    assert base.outer_loop_config == "same delegated config slot"
    assert overlay.composition_binding["final_joint_realizer"].startswith("existing TASE runtime")
