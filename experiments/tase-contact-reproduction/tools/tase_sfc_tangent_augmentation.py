"""TASE plus frozen native-SFC tangent augmentation candidate.

This adapter keeps the live TASE path/normal/posture command and adds only a
projected Kunwei tangential SFC velocity before the existing single TASE joint
realization. It introduces no path spring, second solver, sensor fallback, or
new numeric gain. The live registry must keep this candidate unavailable until
its sealed-trace replay and current UR10e gates pass.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
import copy
import json
import math
from pathlib import Path
from typing import Any, Callable

import numpy as np

from contact_laws import ContactLawSnapshot
from contact_yield_math import finite_scalar
from contact_yield_laws import YieldLaw
from step5d_autotune_v4_r004.timing import MAX_FRESH_GAP_S
from step5d_paper_outer_loop import rotvec_to_matrix


SCHEMA = "tase-sfc-tangent-augmentation-runtime-v1"
METHOD_ID = "TASE_RNN_MATURE+SFC_TANGENT_AUGMENT_V1"
PHASE_TEMPLATE_METHOD_ID = "TASE_RNN_MATURE+SFC_TANGENT_PHASE_TEMPLATE_RESIDUAL_V1"
PHASE_TEMPLATE_SCHEMA = "tase.sfc-path-phase-template-v1"
PHASE_TEMPLATE_CONFIG_REL = "config/tase_sfc_phase_template_a_v1.json"
LAW_CONFIG_REL = "config/yield_native_route_v1.json"
MAX_NATIVE_STEP_S = 0.004
_PARAMETER_NAMES = ("m", "mu", "n", "g")
_DEFAULT_TOTAL_LINEAR_CAP_M_S = 0.0005
_DEFAULT_NORMAL_LINEAR_CAP_M_S = 0.00035
_DEFAULT_TANGENTIAL_CAP_M_S = 0.00035
_BOUND_TOLERANCE_M_S = 1e-12


class TaseSfcTangentAugmentationError(ValueError):
    """Invalid SFC augmentation input or state transition."""


def load_frozen_sfc_parameters(root: Path | None = None) -> dict[str, float]:
    """Load the existing source-bound SFC parameters without retuning them."""
    experiment_root = Path(root) if root is not None else Path(__file__).resolve().parents[1]
    path = experiment_root / LAW_CONFIG_REL
    document = json.loads(path.read_text(encoding="utf-8"))
    if document.get("schema") != "yield-native-route-v1":
        raise TaseSfcTangentAugmentationError("native law route schema differs")
    raw = document.get("law_parameters", {}).get("SFC")
    if not isinstance(raw, Mapping) or set(raw) != set(_PARAMETER_NAMES):
        raise TaseSfcTangentAugmentationError("frozen SFC parameter set differs")
    parameters = {name: finite_scalar(raw[name], f"SFC {name}") for name in _PARAMETER_NAMES}
    if any(value <= 0.0 for value in parameters.values()) or parameters["n"] <= 1.0:
        raise TaseSfcTangentAugmentationError("frozen SFC parameters are outside their native domain")
    return parameters


def load_phase_force_template(
    path: Path | None = None,
    *,
    allow_leave_one_out: bool = False,
) -> tuple[np.ndarray, dict[str, Any]]:
    """Load the frozen 600-bin KWR75 tangent-force template for offline replay.

    This template is an additional model input, not a force-truth signal or a
    fallback sensor. Its source attempts and phase convention travel with the
    values so replay receipts can identify the exact learned table.
    """
    template_path = (
        Path(path)
        if path is not None
        else Path(__file__).resolve().parents[1] / PHASE_TEMPLATE_CONFIG_REL
    )
    try:
        document = json.loads(template_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise TaseSfcTangentAugmentationError(
            f"SFC phase-force template is unreadable: {exc}"
        ) from exc
    if (
        not isinstance(document, Mapping)
        or document.get("schema") != PHASE_TEMPLATE_SCHEMA
        or document.get("protocol_id") != "figure8_window60_r013_rate460_v1"
        or document.get("window_s") != [0.0, 60.0]
        or document.get("bin_width_s") != 0.1
        or document.get("bin_count") != 600
        or document.get("frame") != "base"
        or document.get("force_source") != "baseline-corrected Kunwei KWR75"
    ):
        raise TaseSfcTangentAugmentationError(
            "SFC phase-force template identity or grid differs"
        )
    template_id = document.get("template_id")
    sources = document.get("source_attempts")
    physical_binding = document.get("physical_binding")
    values = _finite_vector_table(
        document.get("phase_mean_tangent_force_base_n"),
        (600, 3),
        "phase_mean_tangent_force_base_n",
    )
    if not isinstance(template_id, str) or not template_id:
        raise TaseSfcTangentAugmentationError("SFC phase-force template id is missing")
    if (
        not isinstance(physical_binding, Mapping)
        or not isinstance(physical_binding.get("home_profile_id"), str)
        or not isinstance(physical_binding.get("eoat_identity_sha256"), str)
        or len(physical_binding["eoat_identity_sha256"]) != 64
        or not isinstance(physical_binding.get("controller_identity"), Mapping)
        or not isinstance(physical_binding.get("controller_readback_triplet"), Mapping)
    ):
        raise TaseSfcTangentAugmentationError(
            "SFC phase-force template physical binding is missing or invalid"
        )
    accepted_source_counts = {4, 5} if allow_leave_one_out else {5}
    if not isinstance(sources, list) or len(sources) not in accepted_source_counts:
        raise TaseSfcTangentAugmentationError(
            "SFC phase-force template source count is invalid for this route"
        )
    expected_scope = (
        "five-attempt-frozen-template"
        if len(sources) == 5 else "four-attempt-leave-one-out-template"
    )
    if document.get("training_scope") != expected_scope:
        raise TaseSfcTangentAugmentationError("SFC phase-force template training scope differs")
    for source in sources:
        if (
            not isinstance(source, Mapping)
            or not isinstance(source.get("attempt_id"), str)
            or not source["attempt_id"]
            or not isinstance(source.get("timeline_sha256"), str)
            or len(source["timeline_sha256"]) != 64
            or any(c not in "0123456789abcdef" for c in source["timeline_sha256"])
        ):
            raise TaseSfcTangentAugmentationError(
                "SFC phase-force template source binding is invalid"
            )
    values.setflags(write=False)
    binding = {
        "schema": PHASE_TEMPLATE_SCHEMA,
        "template_id": template_id,
        "training_scope": expected_scope,
        "source_file": str(template_path),
        "protocol_id": document["protocol_id"],
        "window_s": list(document["window_s"]),
        "bin_width_s": float(document["bin_width_s"]),
        "bin_count": int(document["bin_count"]),
        "frame": document["frame"],
        "force_source": document["force_source"],
        "training_attempt_ids": [source["attempt_id"] for source in sources],
        "training_timeline_sha256": [source["timeline_sha256"] for source in sources],
        "physical_binding": copy.deepcopy(dict(physical_binding)),
    }
    return values, binding


def _finite_vector_table(value: Any, shape: tuple[int, int], name: str) -> np.ndarray:
    try:
        table = np.asarray(value, dtype=float)
    except (TypeError, ValueError, OverflowError) as exc:
        raise TaseSfcTangentAugmentationError(f"{name} must be a finite matrix") from exc
    if table.shape != shape or not np.isfinite(table).all():
        raise TaseSfcTangentAugmentationError(
            f"{name} must be a finite matrix with shape {shape}"
        )
    return table.copy()


def _finite_vector(value: Any, width: int, name: str) -> np.ndarray:
    try:
        result = np.asarray(value, dtype=float)
    except (TypeError, ValueError, OverflowError) as exc:
        raise TaseSfcTangentAugmentationError(f"{name} must be a finite vector") from exc
    if result.shape != (width,) or not np.isfinite(result).all():
        raise TaseSfcTangentAugmentationError(f"{name} must be a finite vector of width {width}")
    return result.copy()


class TaseSfcTangentAugmentationRuntime:
    """Runtime proxy that augments TASE's existing twist before its solver.

    `provider` supplies the actual phase and the TASE fixed reaction normal.
    All unmodified runtime operations and the sole final `command()` call are
    delegated to the original calibrated runtime.
    """

    _LOCAL = {
        "_runtime", "_provider", "_law", "_parameters", "_parameter_source",
        "_active_path", "_last_diagnostics", "_closed", "_phase_force_template",
        "_phase_template_binding",
    }

    def __init__(
        self,
        runtime: Any,
        provider: Any,
        *,
        parameters: Mapping[str, Any] | None = None,
        root: Path | None = None,
        law_factory: Callable[..., Any] = YieldLaw,
        phase_force_template_base_n: Any | None = None,
        phase_template_binding: Mapping[str, Any] | None = None,
    ) -> None:
        object.__setattr__(self, "_runtime", runtime)
        object.__setattr__(self, "_provider", provider)
        selected = load_frozen_sfc_parameters(root) if parameters is None else {
            name: finite_scalar(parameters[name], f"SFC {name}")
            for name in _PARAMETER_NAMES
        }
        if set(selected) != set(_PARAMETER_NAMES):
            raise TaseSfcTangentAugmentationError("frozen SFC parameter set differs")
        frozen = load_frozen_sfc_parameters(root)
        if selected != frozen:
            raise TaseSfcTangentAugmentationError("SFC parameters differ from the frozen native route")
        if phase_force_template_base_n is None:
            if phase_template_binding is not None:
                raise TaseSfcTangentAugmentationError(
                    "phase-template binding was supplied without the template"
                )
            phase_template = None
            phase_binding = None
        else:
            phase_template = _finite_vector_table(
                phase_force_template_base_n,
                (600, 3),
                "phase_force_template_base_n",
            )
            if not isinstance(phase_template_binding, Mapping):
                raise TaseSfcTangentAugmentationError(
                    "phase-template binding is required with the template"
                )
            if (
                phase_template_binding.get("schema") != PHASE_TEMPLATE_SCHEMA
                or phase_template_binding.get("protocol_id") != "figure8_window60_r013_rate460_v1"
                or not isinstance(phase_template_binding.get("template_id"), str)
                or not phase_template_binding.get("template_id")
            ):
                raise TaseSfcTangentAugmentationError(
                    "phase-template binding identity differs"
                )
            phase_template.setflags(write=False)
            phase_binding = copy.deepcopy(dict(phase_template_binding))
        object.__setattr__(self, "_parameters", dict(selected))
        object.__setattr__(self, "_parameter_source", LAW_CONFIG_REL + "#law_parameters.SFC")
        object.__setattr__(self, "_active_path", False)
        object.__setattr__(self, "_last_diagnostics", None)
        object.__setattr__(self, "_closed", False)
        object.__setattr__(self, "_phase_force_template", phase_template)
        object.__setattr__(self, "_phase_template_binding", phase_binding)
        object.__setattr__(self, "_law", law_factory("SFC", selected, dt_s=MAX_NATIVE_STEP_S))

    def __getattr__(self, name: str) -> Any:
        return getattr(self._runtime, name)

    def __setattr__(self, name: str, value: Any) -> None:
        if name in self._LOCAL:
            object.__setattr__(self, name, value)
        else:
            setattr(self._runtime, name, value)

    @property
    def composition_binding(self) -> dict[str, Any]:
        phase_template = self._phase_template_binding
        binding = {
            "schema": SCHEMA,
            "method_id": METHOD_ID if phase_template is None else PHASE_TEMPLATE_METHOD_ID,
            "tangential_law": "SFC",
            "tangential_parameters": dict(self._parameters),
            "parameter_source": self._parameter_source,
            "normal_controller": "TASE_RNN_MATURE",
            "normal_axis_source": "TaseContactProvider.command_normal_base",
            "force_source": (
                "baseline-corrected Kunwei KWR75 sensor.wrench"
                if phase_template is None
                else "baseline-corrected Kunwei KWR75 tangent residual after path-phase template subtraction"
            ),
            "path_semantics": (
                "retain TASE twist and add only its projected tangent SFC velocity"
                if phase_template is None
                else "retain TASE twist and add projected tangent SFC velocity driven by the frozen phase-template residual"
            ),
            "path_spring": None,
            "final_joint_realizer": "existing TASE runtime command, one call per provider command",
        }
        if phase_template is not None:
            binding["phase_force_template"] = copy.deepcopy(phase_template)
        return binding

    def _path_projection(self, normal_value: Any) -> tuple[np.ndarray, np.ndarray]:
        normal = _finite_vector(normal_value, 3, "TASE command normal")
        magnitude = float(np.linalg.norm(normal))
        if magnitude <= 1e-12:
            raise TaseSfcTangentAugmentationError("TASE command normal is degenerate")
        normal /= magnitude
        projector = np.eye(3) - np.outer(normal, normal)
        return normal, projector

    @staticmethod
    def _max_scale_inside_ball(base: np.ndarray, delta: np.ndarray, radius: float) -> float:
        """Largest alpha in [0,1] for which ||base + alpha*delta|| <= radius."""
        base_norm = float(np.linalg.norm(base))
        if base_norm > radius + _BOUND_TOLERANCE_M_S:
            raise TaseSfcTangentAugmentationError(
                "existing TASE task exceeds its bound before SFC augmentation"
            )
        a = float(np.dot(delta, delta))
        if a <= 1e-30:
            return 1.0
        b = 2.0 * float(np.dot(base, delta))
        c = base_norm * base_norm - radius * radius
        discriminant = b * b - 4.0 * a * c
        if discriminant < 0.0:
            return 0.0
        upper_root = (-b + math.sqrt(max(0.0, discriminant))) / (2.0 * a)
        return min(1.0, max(0.0, upper_root))

    def desired_twist(self, **kwargs: Any) -> tuple[float, ...]:
        if self._closed:
            raise TaseSfcTangentAugmentationError("SFC tangent augmentation is closed")
        base_twist = _finite_vector(self._runtime.desired_twist(**kwargs), 6, "TASE desired twist")
        phase = str(getattr(self._provider, "reference_phase", "baseline")).lower()
        return self.augment_twist(
            base_twist,
            phase=phase,
            mode=str(kwargs.get("mode", "")).lower(),
            actual_tcp_pose=kwargs.get("actual_tcp_pose"),
            force_tcp_n=kwargs.get("force_tcp_n"),
            actual_dt_s=kwargs.get("actual_dt_s"),
            path_time_s=kwargs.get("path_time_s"),
            update_outer_feedback=True,
        )

    def augment_twist(
        self,
        base_twist: Any,
        *,
        phase: str,
        mode: str,
        actual_tcp_pose: Any,
        force_tcp_n: Any,
        actual_dt_s: Any,
        path_time_s: Any | None = None,
        update_outer_feedback: bool = False,
    ) -> tuple[float, ...]:
        """Apply the same production SFC projection to a recorded TASE twist.

        This method also supports sealed-trace replay where the TASE outer
        loop request is an observed input and must not be recomputed from a
        candidate's changed hypothetical state.
        """
        if self._closed:
            raise TaseSfcTangentAugmentationError("SFC tangent augmentation is closed")
        base_twist = _finite_vector(base_twist, 6, "TASE desired twist")
        phase = str(phase).lower()
        mode = str(mode).lower()
        if phase != "path" or mode != "path":
            if self._active_path:
                self._law.reset()
            self._active_path = False
            self._last_diagnostics = {
                "schema": SCHEMA,
                "phase": phase,
                "active": False,
                "method_id": self.composition_binding["method_id"],
            }
            return tuple(float(value) for value in base_twist)

        if not self._active_path:
            self._law.reset()
        self._active_path = True
        elapsed = finite_scalar(actual_dt_s, "SFC augmentation actual_dt_s")
        if not 0.0 < elapsed <= MAX_FRESH_GAP_S:
            raise TaseSfcTangentAugmentationError("SFC augmentation dt exceeds the live freshness interval")
        pose = _finite_vector(actual_tcp_pose, 6, "actual TCP pose")
        force_tcp = _finite_vector(force_tcp_n, 3, "Kunwei force_tcp_n")
        normal, tangent_projector = self._path_projection(
            getattr(self._provider, "command_normal_base")
        )
        rotation = np.asarray(rotvec_to_matrix(pose[3:]), dtype=float)
        if rotation.shape != (3, 3) or not np.isfinite(rotation).all():
            raise TaseSfcTangentAugmentationError("actual TCP rotation is invalid")
        force_base = rotation @ force_tcp
        raw_tangent_force = tangent_projector @ force_base
        phase_template_force = np.zeros(3, dtype=float)
        tangent_force = raw_tangent_force.copy()
        if self._phase_force_template is not None:
            try:
                phase_time = finite_scalar(path_time_s, "SFC phase-template path_time_s")
            except (TypeError, ValueError, OverflowError) as exc:
                raise TaseSfcTangentAugmentationError(
                    "SFC phase-template path_time_s must be finite"
                ) from exc
            if not 0.0 <= phase_time < 60.0:
                raise TaseSfcTangentAugmentationError(
                    "SFC phase-template time must lie inside [0,60) seconds"
                )
            phase_bin = int(math.floor(phase_time / 0.1 + 1e-9))
            if not 0 <= phase_bin < 600:
                raise TaseSfcTangentAugmentationError(
                    "SFC phase-template bin is outside its frozen 600-bin table"
                )
            phase_template_force = tangent_projector @ self._phase_force_template[phase_bin]
            tangent_force -= phase_template_force
        step_count = max(1, int(math.ceil(elapsed / MAX_NATIVE_STEP_S)))
        step_dt_s = elapsed / step_count
        tangent_velocity = np.zeros(3, dtype=float)
        for _ in range(step_count):
            tangent_velocity = np.asarray(self._law.step(tangent_force, step_dt_s), dtype=float)
        if tangent_velocity.shape != (3,) or not np.isfinite(tangent_velocity).all():
            raise TaseSfcTangentAugmentationError("native SFC produced an invalid velocity")
        tangent_velocity = tangent_projector @ tangent_velocity
        requested_tangent_velocity = tangent_velocity.copy()
        motion_profile = getattr(self._runtime, "motion_profile", None)
        total_cap = (
            _DEFAULT_TOTAL_LINEAR_CAP_M_S
            if motion_profile is None else float(motion_profile.total_linear_cap_m_s)
        )
        normal_cap = (
            _DEFAULT_NORMAL_LINEAR_CAP_M_S
            if motion_profile is None else float(motion_profile.normal_linear_cap_m_s)
        )
        tangent_cap = (
            _DEFAULT_TANGENTIAL_CAP_M_S
            if motion_profile is None else float(motion_profile.tangential_cap_m_s)
        )
        if not all(math.isfinite(value) and value > 0.0 for value in (
            total_cap, normal_cap, tangent_cap,
        )):
            raise TaseSfcTangentAugmentationError("existing task-space velocity caps are invalid")
        base_linear = base_twist[:3].copy()
        base_normal_velocity = float(np.dot(normal, base_linear))
        if abs(base_normal_velocity) > normal_cap + _BOUND_TOLERANCE_M_S:
            raise TaseSfcTangentAugmentationError(
                "existing TASE normal command exceeds its bound before SFC augmentation"
            )
        base_tangent = tangent_projector @ base_linear
        tangent_scale = self._max_scale_inside_ball(
            base_tangent, tangent_velocity, tangent_cap,
        )
        total_scale = self._max_scale_inside_ball(
            base_linear, tangent_velocity, total_cap,
        )
        tangent_scale = min(tangent_scale, total_scale)
        tangent_velocity *= tangent_scale
        combined = base_twist.copy()
        combined[:3] += tangent_velocity
        normal_leak = abs(float(np.dot(normal, tangent_velocity)))
        if not np.isfinite(combined).all() or normal_leak > 1e-12:
            raise TaseSfcTangentAugmentationError("SFC tangent projection changed the normal task")
        self._last_diagnostics = {
            **self.composition_binding,
            "phase": phase,
            "active": True,
            "actual_dt_s": elapsed,
            "native_substeps": step_count,
            "native_substep_dt_s": step_dt_s,
            "kunwei_force_tcp_n": force_tcp.tolist(),
            "kunwei_force_base_n": force_base.tolist(),
            "raw_tangent_force_base_n": raw_tangent_force.tolist(),
            "phase_template_force_base_n": phase_template_force.tolist(),
            "tangent_force_base_n": tangent_force.tolist(),
            "requested_sfc_velocity_base_m_s": requested_tangent_velocity.tolist(),
            "sfc_velocity_base_m_s": tangent_velocity.tolist(),
            "sfc_velocity_scale_from_existing_caps": tangent_scale,
            "existing_total_linear_cap_m_s": total_cap,
            "existing_normal_linear_cap_m_s": normal_cap,
            "existing_tangential_cap_m_s": tangent_cap,
            "normal_leak_m_s": normal_leak,
            "tase_twist_before_augmentation": base_twist.tolist(),
            "combined_twist_before_existing_task_limits": combined.tolist(),
            "extra_final_realizer_calls": 0,
        }
        pending = getattr(self._runtime, "_pending_outer_feedback", None)
        if update_outer_feedback:
            if not isinstance(pending, dict):
                raise TaseSfcTangentAugmentationError(
                    "live TASE outer output feedback is missing"
                )
            requested_twist = _finite_vector(
                pending.get("requested_twist"), 6, "TASE requested twist feedback",
            )
            requested_twist[:3] += requested_tangent_velocity
            pending["requested_twist"] = tuple(float(value) for value in requested_twist)
            pending["explicit_velocity_limit_active"] = bool(
                pending.get("explicit_velocity_limit_active", False)
                or tangent_scale < 1.0
            )
            pending["tangential_augmentation"] = copy.deepcopy(self._last_diagnostics)
        return tuple(float(value) for value in combined)

    def dynamic_state_snapshot(self) -> dict[str, Any]:
        law_state = self._law.snapshot()
        return {
            "schema": SCHEMA,
            "method_id": self.composition_binding["method_id"],
            "composition_binding": self.composition_binding,
            "runtime": self._runtime.dynamic_state_snapshot(),
            "law_state": {
                "values": list(law_state.values),
                "binding_id": law_state.binding_id,
            },
            "active_path": bool(self._active_path),
            "last_diagnostics": copy.deepcopy(self._last_diagnostics),
            "closed": bool(self._closed),
        }

    def restore_dynamic_state(self, state: Mapping[str, Any]) -> None:
        before = self.dynamic_state_snapshot()
        try:
            if (
                state.get("schema") != SCHEMA
                or state.get("method_id") != self.composition_binding["method_id"]
                or state.get("composition_binding") != self.composition_binding
                or type(state.get("active_path")) is not bool
                or type(state.get("closed")) is not bool
            ):
                raise TaseSfcTangentAugmentationError("SFC augmentation snapshot identity differs")
            law_state = state.get("law_state")
            if not isinstance(law_state, Mapping):
                raise TaseSfcTangentAugmentationError("SFC augmentation law state is missing")
            token = ContactLawSnapshot(
                tuple(float(value) for value in law_state.get("values", ())),
                binding_id=law_state.get("binding_id"),
            )
            self._runtime.restore_dynamic_state(copy.deepcopy(state.get("runtime")))
            self._law.restore(token)
            self._active_path = state["active_path"]
            self._last_diagnostics = copy.deepcopy(state.get("last_diagnostics"))
            self._closed = state["closed"]
        except Exception:
            self._runtime.restore_dynamic_state(copy.deepcopy(before["runtime"]))
            before_law = before["law_state"]
            self._law.restore(ContactLawSnapshot(
                tuple(before_law["values"]), binding_id=before_law["binding_id"],
            ))
            self._active_path = before["active_path"]
            self._last_diagnostics = copy.deepcopy(before["last_diagnostics"])
            self._closed = before["closed"]
            raise

    def reset_dynamic_state(self, *args: Any, **kwargs: Any) -> Any:
        result = self._runtime.reset_dynamic_state(*args, **kwargs)
        self._law.reset()
        self._active_path = False
        self._last_diagnostics = None
        return result

    def close(self) -> None:
        if self._closed:
            return
        try:
            self._law.close()
        finally:
            close = getattr(self._runtime, "close", None)
            if callable(close):
                close()
            self._closed = True


def install_tase_sfc_tangent_augmentation(provider: Any) -> TaseSfcTangentAugmentationRuntime:
    """Wrap one TASE provider's existing runtime; do not add a solver call."""
    if getattr(provider, "protocol_id", None) != "figure8_window60_r013_rate460_v1":
        raise TaseSfcTangentAugmentationError("SFC augmentation requires the rate460 Figure-eight protocol")
    if getattr(provider, "solver_profile", None) is None or getattr(
        provider.solver_profile, "as_dict", lambda: {},
    )().get("backend") not in {"numpy", "cupy"}:
        raise TaseSfcTangentAugmentationError("SFC augmentation requires the mature TASE-RNN solver")
    runtime = TaseSfcTangentAugmentationRuntime(provider.runtime, provider)
    provider.runtime = runtime
    provider.tangential_augmentation_binding = runtime.composition_binding
    provider.parameter_binding["tangential_augmentation"] = runtime.composition_binding
    return runtime


__all__ = [
    "SCHEMA", "METHOD_ID", "PHASE_TEMPLATE_METHOD_ID", "PHASE_TEMPLATE_SCHEMA",
    "PHASE_TEMPLATE_CONFIG_REL", "LAW_CONFIG_REL", "TaseSfcTangentAugmentationError",
    "TaseSfcTangentAugmentationRuntime", "install_tase_sfc_tangent_augmentation",
    "load_frozen_sfc_parameters", "load_phase_force_template",
]
