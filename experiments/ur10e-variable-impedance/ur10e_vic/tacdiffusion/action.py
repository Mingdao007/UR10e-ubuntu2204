"""12D force-plus-variable-impedance action contract and guards."""

from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Callable, Iterable, Sequence


def _six(values: Iterable[float], name: str) -> tuple[float, ...]:
    result = tuple(float(value) for value in values)
    if len(result) != 6 or not all(math.isfinite(value) for value in result):
        raise ValueError(f"{name} must contain six finite values")
    return result


@dataclass(frozen=True)
class ActionProfile:
    frame_id: str = "tool0_tcp"
    force_units: str = "N,Nm"
    stiffness_units: str = "N/m,Nm/rad"
    damping_units: str = "N s/m,Nm s/rad"
    force_component_abs_max: tuple[float, ...] = (50.0, 50.0, 50.0, 4.0, 4.0, 4.0)
    force_norm_max_n: float = 50.0
    torque_norm_max_nm: float = 4.0
    stiffness_baseline: tuple[float, ...] = (600.0, 600.0, 600.0, 30.0, 30.0, 30.0)
    stiffness_min: tuple[float, ...] = (25.0, 25.0, 25.0, 0.5, 0.5, 0.5)
    stiffness_max: tuple[float, ...] = (1000.0, 1000.0, 1000.0, 60.0, 60.0, 60.0)
    stiffness_slew_per_s: tuple[float, ...] = (400.0, 400.0, 400.0, 20.0, 20.0, 20.0)
    force_slew_per_s: tuple[float, ...] = (100.0, 100.0, 100.0, 10.0, 10.0, 10.0)
    virtual_mass: tuple[float, ...] = (2.0, 2.0, 2.0, 0.2, 0.2, 0.2)
    damping_ratio: float = 1.0

    def __post_init__(self) -> None:
        for name in ("force_component_abs_max", "stiffness_baseline", "stiffness_min", "stiffness_max", "stiffness_slew_per_s", "force_slew_per_s", "virtual_mass"):
            object.__setattr__(self, name, _six(getattr(self, name), name))
        if not self.frame_id.strip() or not math.isfinite(self.damping_ratio) or self.damping_ratio <= 0.0:
            raise ValueError("action profile frame/damping ratio is invalid")
        if self.force_units != "N,Nm" or self.stiffness_units != "N/m,Nm/rad" or self.damping_units != "N s/m,Nm s/rad":
            raise ValueError("action force/stiffness/damping units are incompatible")
        if any(lo > hi for lo, hi in zip(self.stiffness_min, self.stiffness_max)):
            raise ValueError("stiffness bounds are inverted")
        if any(value <= 0.0 for value in self.virtual_mass + self.stiffness_slew_per_s + self.force_slew_per_s):
            raise ValueError("mass and slew limits must be positive")


@dataclass(frozen=True)
class TacDiffusionAction:
    raw_f_df: tuple[float, ...] | Sequence[float]
    stiffness: tuple[float, ...] | Sequence[float]
    frame_id: str = "tool0_tcp"
    schema_version: str = "ur10e_tacdiffusion_action/v2"

    def __post_init__(self) -> None:
        object.__setattr__(self, "raw_f_df", _six(self.raw_f_df, "raw_f_df"))
        object.__setattr__(self, "stiffness", _six(self.stiffness, "stiffness"))
        if not self.frame_id.strip():
            raise ValueError("action frame_id must be non-empty")
        if self.schema_version != "ur10e_tacdiffusion_action/v2":
            raise ValueError("unsupported 12D action schema")

    @property
    def vector12(self) -> tuple[float, ...]:
        return self.raw_f_df + self.stiffness


def derive_damping(stiffness: Sequence[float], profile: ActionProfile = ActionProfile()) -> tuple[float, ...]:
    k = _six(stiffness, "stiffness")
    return tuple(2.0 * profile.damping_ratio * math.sqrt(mass * value) for mass, value in zip(profile.virtual_mass, k))


ActionGuard = Callable[[TacDiffusionAction], TacDiffusionAction]


def apply_guard_policy(
    action: TacDiffusionAction,
    guards: Iterable[ActionGuard] | None = (),
    *,
    enabled: bool = True,
) -> TacDiffusionAction:
    """Apply an optional ordered guard policy without changing disabled/empty paths.

    The identity behavior is intentional: an empty policy and a disabled policy
    return the exact input action and do not invoke any guard.  This keeps the
    policy seam algebraically neutral for offline shadow paths.
    """

    if not enabled:
        return action
    if guards is None:
        return action
    result = action
    for guard in tuple(guards):
        result = guard(result)
    return result


def guard_action(action: TacDiffusionAction, *, previous: TacDiffusionAction | None = None, dt_s: float = 0.002, profile: ActionProfile = ActionProfile()) -> TacDiffusionAction:
    if dt_s <= 0.0 or not math.isfinite(dt_s):
        raise ValueError("dt_s must be positive and finite")
    if action.frame_id != profile.frame_id:
        raise ValueError("action frame mismatch")
    force = list(action.raw_f_df)
    stiffness = list(action.stiffness)
    component_limits = profile.force_component_abs_max
    force = [max(-limit, min(limit, value)) for value, limit in zip(force, component_limits)]
    force_norm = math.sqrt(sum(value * value for value in force[:3]))
    if force_norm > profile.force_norm_max_n:
        scale = profile.force_norm_max_n / force_norm
        force[:3] = [value * scale for value in force[:3]]
    torque_norm = math.sqrt(sum(value * value for value in force[3:]))
    if torque_norm > profile.torque_norm_max_nm:
        scale = profile.torque_norm_max_nm / torque_norm
        force[3:] = [value * scale for value in force[3:]]
    for index in range(6):
        stiffness[index] = max(profile.stiffness_min[index], min(profile.stiffness_max[index], stiffness[index]))
    if previous is not None:
        if previous.frame_id != action.frame_id:
            raise ValueError("action slew frame mismatch")
        for index in range(6):
            force[index] = max(previous.raw_f_df[index] - profile.force_slew_per_s[index] * dt_s, min(previous.raw_f_df[index] + profile.force_slew_per_s[index] * dt_s, force[index]))
            stiffness[index] = max(previous.stiffness[index] - profile.stiffness_slew_per_s[index] * dt_s, min(previous.stiffness[index] + profile.stiffness_slew_per_s[index] * dt_s, stiffness[index]))
    result = TacDiffusionAction(tuple(force), tuple(stiffness), action.frame_id)
    if not all(math.isfinite(value) for value in result.vector12):
        raise ValueError("guarded action is non-finite")
    derive_damping(result.stiffness, profile)
    return result


MODEL_MODE_FIXED_K_V1 = "fixed_k_v1"
MODEL_MODE_VARIABLE_K_V1 = "variable_k_v1"
CANONICAL_CONTROLLER_ACTION_DIMENSION = 12


@dataclass(frozen=True)
class TypedModelOutputV1:
    """Dimension-checked model output before K/action expansion."""

    mode: str
    values: tuple[float, ...] | Sequence[float]
    schema_version: str = "ur10e_tacdiffusion_model_output/v1"

    def __post_init__(self) -> None:
        if self.schema_version != "ur10e_tacdiffusion_model_output/v1":
            raise ValueError("unsupported typed model output schema")
        expected = {MODEL_MODE_FIXED_K_V1: 6, MODEL_MODE_VARIABLE_K_V1: 7}.get(self.mode)
        if expected is None:
            raise ValueError("unsupported TacDiffusion model mode")
        try:
            raw = tuple(float(value) for value in self.values)
        except (TypeError, ValueError) as exc:
            raise ValueError("model output must be numeric") from exc
        if len(raw) != expected or not all(math.isfinite(value) for value in raw):
            raise ValueError(f"{self.mode} model output must contain {expected} finite values")
        if self.mode == MODEL_MODE_VARIABLE_K_V1:
            # Canonicalize an unconstrained network scalar before it can enter
            # the isotropic K expansion seam.
            raw = raw[:6] + (max(400.0, min(800.0, raw[6])),)
        object.__setattr__(self, "values", _six(raw[:6], "model force output") + raw[6:])


@dataclass(frozen=True)
class ExpandedControllerActionV1:
    """Canonical 12D action plus typed K/D expansion metadata."""

    mode: str
    model_output: tuple[float, ...]
    action: TacDiffusionAction
    damping: tuple[float, ...]
    variable_k_output_n_m: float | None = None
    active: bool = False
    shadow_only: bool = True
    schema_version: str = "ur10e_tacdiffusion_expanded_action/v1"

    def __post_init__(self) -> None:
        if self.schema_version != "ur10e_tacdiffusion_expanded_action/v1":
            raise ValueError("unsupported expanded action schema")
        if self.mode not in {MODEL_MODE_FIXED_K_V1, MODEL_MODE_VARIABLE_K_V1}:
            raise ValueError("expanded action mode is invalid")
        expected = 6 if self.mode == MODEL_MODE_FIXED_K_V1 else 7
        if len(self.model_output) != expected or not all(math.isfinite(value) for value in self.model_output):
            raise ValueError("expanded action model output dimension is invalid")
        if len(self.action.vector12) != CANONICAL_CONTROLLER_ACTION_DIMENSION:
            raise ValueError("expanded controller action must be 12D")
        object.__setattr__(self, "damping", _six(self.damping, "damping"))
        if self.variable_k_output_n_m is not None and not math.isfinite(self.variable_k_output_n_m):
            raise ValueError("variable-K model K output must be finite")
        if self.mode == MODEL_MODE_VARIABLE_K_V1:
            if self.variable_k_output_n_m is None:
                raise ValueError("variable-K expansion must retain the learned seventh K output")
            if not 400.0 <= self.variable_k_output_n_m <= 800.0:
                raise ValueError("variable-K model K output must be bounded to 400..800 N/m")
            if not math.isclose(self.variable_k_output_n_m, self.model_output[6], rel_tol=0.0, abs_tol=1e-12):
                raise ValueError("variable-K metadata must bind the learned seventh output")
        if self.active is not False or self.shadow_only is not True:
            raise ValueError("formal action expansion remains inactive and shadow-only")

    @property
    def vector12(self) -> tuple[float, ...]:
        return self.action.vector12

    @property
    def stiffness(self) -> tuple[float, ...]:
        return self.action.stiffness

    @property
    def raw_f_df(self) -> tuple[float, ...]:
        return self.action.raw_f_df

    def as_json(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "mode": self.mode,
            "model_output": list(self.model_output),
            "action_12d": list(self.vector12),
            "damping_6d": list(self.damping),
            "variable_k_output_n_m": self.variable_k_output_n_m,
            "active": self.active,
            "shadow_only": self.shadow_only,
        }


def expand_model_output_to_controller_action(
    model_output: Sequence[float] | TypedModelOutputV1,
    *,
    mode: str,
    previous_stiffness: Sequence[float] | float | None = None,
    dt_s: float = 1.0 / 500.0,
    profile: ActionProfile = ActionProfile(),
) -> ExpandedControllerActionV1:
    """Expand fixed-6D or variable-7D output into the canonical 12D action.

    Variable-K stiffness is expanded exclusively from the learned seventh
    model output.  The deterministic observable formula belongs to training
    label generation and is intentionally absent from this seam.
    """

    typed = model_output if isinstance(model_output, TypedModelOutputV1) else TypedModelOutputV1(mode, model_output)
    if typed.mode != mode:
        raise ValueError("typed model output mode does not match expansion mode")
    if mode == MODEL_MODE_FIXED_K_V1:
        from .expert import FixedKExpertV1

        expert = FixedKExpertV1()
        stiffness = expert.stiffness()
        model_k = None
    elif mode == MODEL_MODE_VARIABLE_K_V1:
        from .expert import VariableKExpertV1

        expert = VariableKExpertV1()
        model_k = typed.values[6]
        stiffness = expert.stiffness_from_model_output(
            model_k,
            previous_stiffness=previous_stiffness,
            dt_s=dt_s,
        )
    else:
        raise ValueError("unsupported TacDiffusion model mode")
    action = TacDiffusionAction(typed.values[:6], stiffness, profile.frame_id)
    return ExpandedControllerActionV1(
        mode=mode,
        model_output=typed.values,
        action=action,
        damping=derive_damping(stiffness, profile),
        variable_k_output_n_m=model_k,
    )


# Short compatibility spelling for callers that already use ``expand_action``
# in offline tests.  It has exactly one implementation and one authority.
expand_action = expand_model_output_to_controller_action
