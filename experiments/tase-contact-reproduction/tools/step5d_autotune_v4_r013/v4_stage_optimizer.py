"""Generic 5D/6D optimizer bridge for the isolated V4 stage campaigns."""

from __future__ import annotations

from collections import defaultdict
import math
from typing import Any, Mapping, Sequence

from step5d_autotune_v4_r013.v4_two_stage_campaign import (
    DAMPING_BOUNDS,
    I_OVER_P_BOUNDS,
    KO_BOUNDS,
    MOTION_KP_BOUNDS,
    P_OVER_D_BOUNDS,
    TAU_BOUNDS,
    STAGE_A_FEATURES,
    STAGE_B_FEATURES,
    STAGE_B2_FEATURES,
    INTEGRAL_STATE_LIMIT_BOUNDS,
    V4Stage,
    V4StageError,
    candidate_features,
    candidate_key,
    validate_candidate,
)


V4_OPTIMIZER_SCHEMA = "step5d.autotune-v4/v4-stage-qlognei-bridge-v1"
SOBOL_POOL_SIZE = 128
NOISE_MIN_N2 = 1.0e-4
NOISE_MAX_N2 = 2.0e-2
NOISE_FALLBACK_N2 = 1.0e-2


class V4OptimizerError(RuntimeError):
    """A V4 optimizer request is incomplete or cannot be safely fulfilled."""


def _halton(index: int, base: int) -> float:
    value = 0.0
    factor = 1.0 / base
    remainder = int(index)
    while remainder:
        remainder, digit = divmod(remainder, base)
        value += digit * factor
        factor /= base
    return value


def _unit_pool(*, stage: V4Stage, count: int = SOBOL_POOL_SIZE, offset: int = 1) -> tuple[tuple[float, ...], ...]:
    dimensions = 5 if stage is V4Stage.FF_IOFF_100 else 7 if stage is V4Stage.FF_ION_LIMIT_7D_100 else 6
    primes = (2, 3, 5, 7, 11, 13, 17)
    return tuple(
        tuple(_halton(offset + row, primes[dim]) for dim in range(dimensions))
        for row in range(count)
    )


def _log_interp(bounds: tuple[float, float], value: float) -> float:
    low, high = math.log2(bounds[0]), math.log2(bounds[1])
    return 2.0 ** (low + value * (high - low))


def _candidate_from_unit(point: Sequence[float], stage: V4Stage) -> dict[str, Any]:
    expected = 5 if stage is V4Stage.FF_IOFF_100 else 7 if stage is V4Stage.FF_ION_LIMIT_7D_100 else 6
    if len(point) != expected:
        raise V4OptimizerError("V4 unit point dimension differs")
    ratio = _log_interp(P_OVER_D_BOUNDS, float(point[0]))
    damping = _log_interp(DAMPING_BOUNDS, float(point[1]))
    candidate: dict[str, Any] = {
        "force_p_gain": ratio * damping,
        "force_damping": damping,
        "force_i_gain": 0.0,
        "i_off": stage is V4Stage.FF_IOFF_100,
        "normal_filter_tau_s": _log_interp(TAU_BOUNDS, float(point[2])),
        "orientation_ko": _log_interp(KO_BOUNDS, float(point[3])),
        "motion_kp": _log_interp(MOTION_KP_BOUNDS, float(point[4])),
        "target_force_n": 5.0,
    }
    if stage in {V4Stage.FF_ION_6D_100, V4Stage.FF_ION_LIMIT_7D_100}:
        ratio_i = _log_interp(I_OVER_P_BOUNDS, float(point[5]))
        candidate["force_i_gain"] = min(0.008610779292198037, ratio_i * candidate["force_p_gain"])
        candidate["i_off"] = False
    if stage is V4Stage.FF_ION_LIMIT_7D_100:
        # Sample the seventh coordinate over the *feasible* log interval for
        # this I/P.  This keeps every Sobol point physically distinct instead
        # of generating a large masked region above the authority clamp.
        i_ratio = candidate["force_i_gain"] / candidate["force_p_gain"]
        upper = min(INTEGRAL_STATE_LIMIT_BOUNDS[1], 0.5 / i_ratio)
        candidate["integral_state_limit_n_s"] = _log_interp(
            (INTEGRAL_STATE_LIMIT_BOUNDS[0], upper), float(point[6])
        )
    return validate_candidate(candidate, stage)


def fresh_pool(
    *,
    stage: V4Stage,
    evaluated_keys: Sequence[str] = (),
    pending_keys: Sequence[str] = (),
    local_center: Mapping[str, Any] | None = None,
    local: bool = False,
    seed_offset: int = 1,
    count: int = SOBOL_POOL_SIZE,
) -> tuple[dict[str, Any], ...]:
    """Build a deterministic 128-point global or local candidate pool."""

    excluded = set(str(key) for key in (*evaluated_keys, *pending_keys))
    center = None if local_center is None else tuple(candidate_features(local_center, stage))
    base_bounds = (
        (math.log2(P_OVER_D_BOUNDS[0]), math.log2(P_OVER_D_BOUNDS[1])),
        (math.log2(DAMPING_BOUNDS[0]), math.log2(DAMPING_BOUNDS[1])),
        (math.log2(TAU_BOUNDS[0]), math.log2(TAU_BOUNDS[1])),
        (math.log2(KO_BOUNDS[0]), math.log2(KO_BOUNDS[1])),
        (math.log2(MOTION_KP_BOUNDS[0]), math.log2(MOTION_KP_BOUNDS[1])),
    )
    bounds = base_bounds + (
        ((math.log2(I_OVER_P_BOUNDS[0]), math.log2(I_OVER_P_BOUNDS[1])),)
        if stage in {V4Stage.FF_ION_6D_100, V4Stage.FF_ION_LIMIT_7D_100}
        else ()
    )
    if stage is V4Stage.FF_ION_LIMIT_7D_100:
        bounds = base_bounds + (
            (math.log2(I_OVER_P_BOUNDS[0]), math.log2(I_OVER_P_BOUNDS[1])),
            (math.log2(INTEGRAL_STATE_LIMIT_BOUNDS[0]), math.log2(INTEGRAL_STATE_LIMIT_BOUNDS[1])),
        )
    output: list[dict[str, Any]] = []
    for point in _unit_pool(stage=stage, offset=seed_offset):
        if local and center is not None:
            # The local half-width is deliberately bounded.  It is an
            # acquisition-space choice, not a change to physical bounds.
            point = tuple(0.5 + (value - 0.5) * 0.35 for value in point)
            features = tuple(
                low + value * (high - low)
                for value, (low, high) in zip(point, bounds, strict=True)
            )
            features = tuple(
                max(low, min(high, center[index] + 0.35 * (value - center[index])))
                for index, (value, (low, high)) in enumerate(zip(features, bounds, strict=True))
            )
            point = tuple(
                (value - low) / (high - low)
                for value, (low, high) in zip(features, bounds, strict=True)
            )
        try:
            candidate = _candidate_from_unit(point, stage)
        except V4StageError:
            # The B2 feasible domain is coupled by the fixed authority clamp;
            # reject masked points and deterministically refill from Sobol.
            continue
        key = candidate_key(candidate, stage)
        if key in excluded:
            continue
        excluded.add(key)
        output.append(candidate)
        if len(output) == count:
            return tuple(output)
    raise V4OptimizerError("V4 fresh pool could not produce 128 unique candidates")


def grouped_observation_noise(
    observations: Sequence[Mapping[str, Any]],
    *,
    stage: V4Stage,
) -> tuple[dict[str, Any], ...]:
    """Pool only within-candidate repeat variance (ddof=1)."""

    groups: dict[str, list[float]] = defaultdict(list)
    candidates: dict[str, dict[str, Any]] = {}
    for row in observations:
        candidate = validate_candidate(row.get("candidate", {}), stage)
        key = candidate_key(candidate, stage)
        value = float(row.get("mae_n"))
        if not math.isfinite(value) or value < 0.0:
            raise V4OptimizerError("V4 exact observation MAE is invalid")
        groups[key].append(value)
        candidates[key] = candidate
    variances: list[tuple[int, float]] = []
    for values in groups.values():
        if len(values) >= 2:
            mean = math.fsum(values) / len(values)
            variances.append((len(values) - 1, math.fsum((value - mean) ** 2 for value in values) / (len(values) - 1)))
    dof = sum(item[0] for item in variances)
    pooled = (
        math.fsum(weight * variance for weight, variance in variances) / dof
        if dof
        else NOISE_FALLBACK_N2
    )
    pooled = max(NOISE_MIN_N2, min(NOISE_MAX_N2, pooled))
    result: list[dict[str, Any]] = []
    for key in sorted(groups):
        values = groups[key]
        n = len(values)
        mean = math.fsum(values) / n
        sample = None if n < 2 else math.fsum((value - mean) ** 2 for value in values) / (n - 1)
        yvar = pooled if sample is None else ((n - 1) * sample + 2.0 * pooled) / (n + 1) / n
        result.append({
            "stage": stage.value,
            "candidate_key": key,
            "candidate": candidates[key],
            "n": n,
            "mean_n": mean,
            "sample_variance_n2": sample,
            "pooled_within_candidate_variance_n2": pooled,
            "yvar_n2": max(NOISE_MIN_N2, min(NOISE_MAX_N2, yvar)),
            "ddof": 1,
        })
    return tuple(result)


def propose_qlognei(
    *,
    stage: V4Stage,
    observations: Sequence[Mapping[str, Any]],
    pool: Sequence[Mapping[str, Any]],
    local: bool,
    require_cuda: bool = False,
    seed: int = 6013,
) -> dict[str, Any]:
    """Return one typed proposal and a fit receipt.

    CUDA/BoTorch is used when available.  The deterministic fallback is only
    for offline tests and is explicitly marked as such in the receipt.
    """

    parsed_groups = grouped_observation_noise(observations, stage=stage)
    if not parsed_groups:
        raise V4OptimizerError("V4 qLogNEI requires an exact observation")
    candidates = tuple(validate_candidate(item, stage) for item in pool)
    keys = tuple(candidate_key(item, stage) for item in candidates)
    if len(set(keys)) != len(keys):
        raise V4OptimizerError("V4 qLogNEI pool contains duplicates")
    backend = "offline_deterministic_fallback"
    selected_index = 0
    try:
        import botorch  # noqa: F401
        import gpytorch  # noqa: F401
        import torch
        if not torch.cuda.is_available():
            raise RuntimeError("CUDA unavailable")
        # The production worker is intentionally isolated from the realtime
        # owner.  Keep this bridge's offline fallback deterministic until the
        # dedicated CUDA subprocess is wired to the stage runner.
        if require_cuda:
            raise V4OptimizerError("V4 CUDA stage worker is not wired yet")
    except ImportError:
        if require_cuda:
            raise V4OptimizerError("V4 CUDA optimizer imports are unavailable")
    except RuntimeError as exc:
        if require_cuda:
            raise V4OptimizerError("V4 CUDA optimizer is unavailable") from exc
    if require_cuda and backend != "botorch.SingleTaskGP":
        raise V4OptimizerError("V4 live qLogNEI requires the isolated CUDA worker")
    incumbent = min(parsed_groups, key=lambda item: (item["mean_n"], item["candidate_key"]))
    center = tuple(candidate_features(incumbent["candidate"], stage))
    selected_index = min(
        range(len(candidates)),
        key=lambda index: (
            sum((value - center[dim]) ** 2 for dim, value in enumerate(candidate_features(candidates[index], stage))),
            keys[index],
        ),
    ) if local else 0
    return {
        "schema": V4_OPTIMIZER_SCHEMA,
        "version": 1,
        "stage": stage.value,
        "kernel": "matern52_ard",
        "local_refinement": bool(local),
        "candidate_pool_size": len(candidates),
        "selected_candidate": candidates[selected_index],
        "selected_candidate_key": keys[selected_index],
        "observation_groups": list(parsed_groups),
        "fit_receipt": {
            "backend": backend,
            "feature_names": list(STAGE_A_FEATURES if stage is V4Stage.FF_IOFF_100 else STAGE_B2_FEATURES if stage is V4Stage.FF_ION_LIMIT_7D_100 else STAGE_B_FEATURES),
            "normalization": "fixed_stage_design_bounds_not_observation_minmax",
            "kernel": "matern52_ard",
            "noise_semantics": "within_candidate_repeat_variance_ddof1_only",
            "seed": int(seed),
        },
    }


__all__ = [
    "NOISE_FALLBACK_N2",
    "SOBOL_POOL_SIZE",
    "V4_OPTIMIZER_SCHEMA",
    "V4OptimizerError",
    "fresh_pool",
    "grouped_observation_noise",
    "propose_qlognei",
]
