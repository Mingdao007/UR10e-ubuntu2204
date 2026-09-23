"""Offline composition of a TASE normal solver and native tangential laws.

The selected TASE RNN or QP is the only Cartesian-to-joint realization. The
selected native tangent law receives measured tangential wrench plus the
existing path restoring term, while nominal path velocity is added as tangent
feedforward. Historical ``+SFC`` remains a separate identity from the frozen
native SFC/DSFC comparison identities. No surface geometry or evaluator truth
is consumed. This adapter is offline-only.
"""
from __future__ import annotations

from dataclasses import asdict
import math
from typing import Any, Mapping

import numpy as np

from contact_laws import ContactLawSnapshot
from contact_yield_laws import YieldLaw
from step5d_paper_outer_loop import Step5dOuterLoopInputs, rotvec_to_matrix
from tase_method_adapters import TaseOfflineMethodAdapter, _finite_vector
from tase_sfc_fusion import (
    COMPOSITION_ID,
    NATIVE_DSFC_COMPOSITION_ID,
    NATIVE_SFC_COMPOSITION_ID,
    FusionError,
    TaseSfcFusion,
    normal_tangent_projectors,
)


SCHEMA = "tase-sfc-composed-offline-adapter-v1"
NATIVE_SCHEMA = "tase-native-tangent-composed-offline-adapter-v1"
SUPPORTED_NORMAL_METHODS = ("TASE_RNN_MATURE", "TASE_QP")
SUPPORTED_TANGENTIAL_METHODS = ("SFC", "DSFC")
DEFAULT_PATH_STIFFNESS_N_PER_M = 120.0
NATIVE_COMPOSITION_IDS = {
    "SFC": NATIVE_SFC_COMPOSITION_ID,
    "DSFC": NATIVE_DSFC_COMPOSITION_ID,
}


class TaseSfcCompositionError(ValueError):
    """Invalid offline TASE/SFC composition or state transition."""


class TaseSfcComposedOfflineAdapter:
    offline_only = True
    live_eligible = False

    def __init__(
        self,
        *,
        method_name: str,
        tase_adapter: TaseOfflineMethodAdapter,
        tangential_method: str = "SFC",
        identity_mode: str = "legacy_sfc",
        tangential_parameters: Mapping[str, Any] | None = None,
        sfc_parameters: Mapping[str, Any] | None = None,
        path_stiffness_n_per_m: float = DEFAULT_PATH_STIFFNESS_N_PER_M,
        initial_normal: Any = (0.0, 0.0, 1.0),
        dt_s: float = 0.002,
        law_build_root: str | None = None,
    ) -> None:
        if tase_adapter.method_name not in SUPPORTED_NORMAL_METHODS:
            raise TaseSfcCompositionError("composition normal solver must be TASE_RNN_MATURE or TASE_QP")
        law = str(tangential_method)
        mode = str(identity_mode)
        if law not in SUPPORTED_TANGENTIAL_METHODS:
            raise TaseSfcCompositionError(f"unsupported tangent law: {law}")
        if tangential_parameters is not None and sfc_parameters is not None:
            raise TaseSfcCompositionError("supply one tangent parameter mapping")
        parameters = tangential_parameters if tangential_parameters is not None else sfc_parameters
        if mode == "legacy_sfc":
            if law != "SFC" or method_name != f"{tase_adapter.method_name}+SFC":
                raise TaseSfcCompositionError("legacy identity is reserved for the historical +SFC composition")
            composition_id = COMPOSITION_ID
            schema = SCHEMA
        elif mode == "frozen_native_yield_v1":
            expected = f"{tase_adapter.method_name}+{law}_YIELD_V1"
            if method_name != expected:
                raise TaseSfcCompositionError(f"frozen native composition identity must be {expected}")
            composition_id = NATIVE_COMPOSITION_IDS[law]
            schema = NATIVE_SCHEMA
        else:
            raise TaseSfcCompositionError("unknown composition identity mode")
        stiffness = float(path_stiffness_n_per_m)
        elapsed = float(dt_s)
        if not math.isfinite(stiffness) or stiffness <= 0.0:
            raise TaseSfcCompositionError("path stiffness must be positive and finite")
        if not math.isfinite(elapsed) or not 0.0 < elapsed <= 0.004:
            raise TaseSfcCompositionError("dt_s must be finite and in (0, 4ms]")
        try:
            self._initial_normal = tuple(float(value) for value in normal_tangent_projectors(initial_normal).normal)
        except (FusionError, TypeError, ValueError) as exc:
            raise TaseSfcCompositionError(f"invalid initial normal: {exc}") from exc
        self.method_name = method_name
        self.tangential_method = law
        self.identity_mode = mode
        self.composition_id = composition_id
        self.snapshot_schema = schema
        self.tase = tase_adapter
        self.path_stiffness_n_per_m = stiffness
        self.dt_s = elapsed
        self.tangent_law = YieldLaw(law, parameters, dt_s=elapsed, build_root=law_build_root)
        self.sfc_law = self.tangent_law if law == "SFC" else None
        self.fusion = TaseSfcFusion(self._initial_normal, composition_id=composition_id)
        self.stopped = False

    def snapshot(self) -> dict[str, Any]:
        law_snapshot = self.tangent_law.snapshot()
        law_state = {
            "method": self.tangential_method,
            "parameters": dict(self.tangent_law.parameters),
            "law_identity": self.tangent_law.identity,
            "build_fingerprint": self.tangent_law.build_fingerprint,
            "snapshot": {
                "values": list(law_snapshot.values),
                "binding_id": law_snapshot.binding_id,
            },
        }
        state = {
            "schema": self.snapshot_schema,
            "method_name": self.method_name,
            "composition_id": self.composition_id,
            "identity_mode": self.identity_mode,
            "offline_only": True,
            "live_eligible": False,
            "stopped": bool(self.stopped),
            "dt_s": self.dt_s,
            "initial_normal": list(self._initial_normal),
            "path_stiffness_n_per_m": self.path_stiffness_n_per_m,
            "tase": self.tase.snapshot(),
            "tangential_law": law_state,
            "fusion": self.fusion.snapshot(),
        }
        if self.identity_mode == "legacy_sfc":
            # Preserve the old v1 state field for retained SFC consumers.
            state.pop("tangential_law")
            state.pop("identity_mode")
            state["sfc"] = {**law_state, "method": "SFC"}
        return state

    def _restore_unchecked(self, state: Mapping[str, Any]) -> None:
        if (
            state.get("schema") != self.snapshot_schema
            or state.get("method_name") != self.method_name
            or state.get("composition_id") != self.composition_id
            or (self.identity_mode != "legacy_sfc" and state.get("identity_mode") != self.identity_mode)
            or state.get("offline_only") is not True
            or state.get("live_eligible") is not False
            or type(state.get("stopped")) is not bool
            or not math.isclose(float(state.get("dt_s")), self.dt_s, rel_tol=0.0, abs_tol=1e-12)
            or not math.isclose(
                float(state.get("path_stiffness_n_per_m")),
                self.path_stiffness_n_per_m,
                rel_tol=0.0,
                abs_tol=1e-12,
            )
        ):
            raise TaseSfcCompositionError("composition snapshot identity differs")
        if tuple(float(value) for value in state.get("initial_normal", ())) != self._initial_normal:
            raise TaseSfcCompositionError("composition snapshot initial normal differs")
        law_state = state.get("sfc") if self.identity_mode == "legacy_sfc" else state.get("tangential_law")
        if not isinstance(law_state, Mapping):
            raise TaseSfcCompositionError("composition snapshot tangent-law state is missing")
        if (
            law_state.get("method") != self.tangential_method
            or dict(law_state.get("parameters", {})) != dict(self.tangent_law.parameters)
            or law_state.get("law_identity") != self.tangent_law.identity
            or law_state.get("build_fingerprint") != self.tangent_law.build_fingerprint
        ):
            raise TaseSfcCompositionError("composition snapshot tangent-law identity differs")
        raw_snapshot = law_state.get("snapshot")
        if not isinstance(raw_snapshot, Mapping):
            raise TaseSfcCompositionError("composition snapshot native law token is missing")
        native_snapshot = ContactLawSnapshot(
            tuple(float(value) for value in raw_snapshot.get("values", ())),
            binding_id=raw_snapshot.get("binding_id"),
        )
        self.tase.restore(state.get("tase", {}))
        self.tangent_law.restore(native_snapshot)
        self.fusion.restore(state.get("fusion", {}))
        self.stopped = bool(state["stopped"])

    def restore(self, state: Mapping[str, Any]) -> None:
        before = self.snapshot()
        try:
            self._restore_unchecked(state)
        except Exception:
            try:
                self._restore_unchecked(before)
            except Exception as rollback_error:  # pragma: no cover - invariant breach
                raise RuntimeError("composition restore rollback failed") from rollback_error
            raise

    def reset(self) -> None:
        self.tase.reset()
        self.tangent_law.reset()
        self.fusion = TaseSfcFusion(self._initial_normal, composition_id=self.composition_id)
        self.stopped = False

    def stop(self, reason: str = "offline_stop") -> None:
        self.stopped = True
        self.tase.stop(reason)

    def close(self) -> None:
        self.stop("offline_close")
        self.tangent_law.close()
        self.tase.close()

    def _transform_twist(
        self,
        tase_twist: np.ndarray,
        inputs: Step5dOuterLoopInputs,
        measured: Mapping[str, Any],
        target: Mapping[str, Any],
        dt_s: float,
    ) -> tuple[np.ndarray, Mapping[str, Any]]:
        phase = str(target.get("phase", "path"))
        in_path = phase.lower() == "path"
        if in_path and self.fusion.phase.lower() != "path":
            self.tangent_law.reset()
        projectors = normal_tangent_projectors(inputs.control_reaction_normal_base)
        normal = np.asarray(projectors.normal, dtype=float)
        pose = _finite_vector(measured.get("tcp_pose_base"), 6, "tcp_pose_base")
        wrench = _finite_vector(measured.get("wrench_tcp"), 6, "wrench_tcp")
        position = pose[:3]
        ref_position = _finite_vector(target.get("x_pd_base"), 3, "x_pd_base")
        ref_velocity = _finite_vector(target.get("xdot_pd_base"), 3, "xdot_pd_base")
        rotation = rotvec_to_matrix(pose[3:])
        force_base = rotation @ wrench[:3]
        path_error = position - ref_position
        tangential_force = projectors.Pt @ force_base
        path_restoring_force = -self.path_stiffness_n_per_m * (projectors.Pt @ path_error)
        if in_path:
            sfc_input = tangential_force + path_restoring_force
            tangent_law_velocity = np.asarray(self.tangent_law.step(sfc_input, dt_s), dtype=float)
            if tangent_law_velocity.shape != (3,) or not np.isfinite(tangent_law_velocity).all():
                raise TaseSfcCompositionError(
                    f"{self.tangential_method} produced an invalid tangential task command"
                )
            tangent_feedforward = projectors.Pt @ ref_velocity
            sfc_task = np.concatenate(
                (projectors.Pt @ tangent_law_velocity + tangent_feedforward, np.zeros(3))
            )
        else:
            self.tangent_law.reset()
            sfc_input = np.zeros(3)
            tangent_law_velocity = np.zeros(3)
            tangent_feedforward = np.zeros(3)
            sfc_task = np.zeros(6)

        fused = self.fusion.fuse(tase_twist, sfc_task, normal, phase=phase)
        if fused.diagnostics["normal_jump"]:
            self.tangent_law.reset()
        if self.fusion.sfc_enabled:
            state_2d = self.fusion.tangent_basis.T @ np.asarray(self.tangent_law.state, dtype=float)
            self.fusion.update_sfc_state(state_2d)
        else:
            self.fusion.update_sfc_state((0.0, 0.0))

        diagnostics = {
            "schema": "tase-native-tangent-composed-adapter-diagnostics-v1",
            "composition_id": self.composition_id,
            "identity_mode": self.identity_mode,
            "method_name": self.method_name,
            "normal_solver": self.tase.method_name,
            "final_realizer": self.tase.solver_name,
            "final_realization_calls": 1,
            "normal_solver_config": asdict(self.tase.config),
            "normal_outer_config": asdict(self.tase.outer_config),
            "tangential_law": self.tangential_method,
            "tangential_parameters": dict(self.tangent_law.parameters),
            "tangential_law_identity": self.tangent_law.identity,
            "tangential_build_fingerprint": self.tangent_law.build_fingerprint,
            "path_stiffness_n_per_m": self.path_stiffness_n_per_m,
            "phase": phase,
            "sfc_enabled": fused.sfc_enabled,
            "tangential_force_input_n": [float(value) for value in tangential_force],
            "path_restoring_force_n": [float(value) for value in path_restoring_force],
            "tangential_law_input_n": [float(value) for value in sfc_input],
            "sfc_input_n": [float(value) for value in sfc_input],
            "tangential_law_velocity_m_s": [float(value) for value in tangent_law_velocity],
            "tangent_feedforward_m_s": [float(value) for value in tangent_feedforward],
            "tase_tangent_shadow_m_s": list(fused.tase_tangent_shadow[:3]),
            "tase_normal_and_orientation_m_s_rad_s": list(fused.tase_normal_twist),
            "tangential_tangent_twist_m_s_rad_s": list(fused.sfc_tangent_twist),
            "sfc_tangent_twist_m_s_rad_s": list(fused.sfc_tangent_twist),
            "normal": list(fused.diagnostics["normal"]),
            "normal_jump": bool(fused.diagnostics["normal_jump"]),
            "fusion_state": self.fusion.snapshot(),
        }
        if self.identity_mode == "legacy_sfc":
            diagnostics["schema"] = "tase-sfc-composed-adapter-diagnostics-v1"
            diagnostics.update({
                "sfc_law": "SFC",
                "sfc_parameters": diagnostics["tangential_parameters"],
                "sfc_law_identity": diagnostics["tangential_law_identity"],
                "sfc_build_fingerprint": diagnostics["tangential_build_fingerprint"],
                "sfc_law_velocity_m_s": diagnostics["tangential_law_velocity_m_s"],
            })
        return np.asarray(fused.twist, dtype=float), diagnostics

    def step(
        self,
        measured: Mapping[str, Any],
        target: Mapping[str, Any],
        dt_s: float,
    ) -> Any:
        if self.stopped:
            raise TaseSfcCompositionError("composed TASE/tangent-law adapter is stopped")
        before = self.snapshot()
        try:
            if not math.isclose(float(dt_s), self.dt_s, rel_tol=0.0, abs_tol=1e-12):
                raise TaseSfcCompositionError("composition dt_s differs from its law identity")
            return self.tase.step(
                measured,
                target,
                dt_s,
                twist_transform=self._transform_twist,
            )
        except Exception:
            self.restore(before)
            raise


TaseNativeTangentComposedOfflineAdapter = TaseSfcComposedOfflineAdapter

__all__ = [
    "SCHEMA", "NATIVE_SCHEMA", "SUPPORTED_NORMAL_METHODS", "SUPPORTED_TANGENTIAL_METHODS",
    "NATIVE_COMPOSITION_IDS", "TaseSfcCompositionError", "TaseSfcComposedOfflineAdapter",
    "TaseNativeTangentComposedOfflineAdapter",
]
