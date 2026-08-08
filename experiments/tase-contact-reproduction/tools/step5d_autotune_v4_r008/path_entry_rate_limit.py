"""STAGE25/PATH entry physical rate-limit ramp (wired; live OFF by default).

Model-independent second safety layer for the first ~100–200 ms after the
outer-loop mode switches from baseline/search into ``path``.  Hard-limits
commanded normal amplitude (and its slew) so a saturated Ki pocket cannot
dump full path-cap intrusion immediately at STAGE25 entry.

LIVE REDLINE
------------
``enabled`` defaults False.  Env ``R008_PATH_ENTRY_RATE_LIMIT`` must be
explicitly ``1``/``true`` for the ramp to arm.  Wired at ``HOOK_POINT`` in
``step5d_autotune_v4_r004.qualification`` (post paper outer-loop
``desired_twist``, pre ``command`` / envelope).  Do not touch seal /
``PACKET_STALE_S``.
"""

from __future__ import annotations

import math
import os
from dataclasses import dataclass, field
from typing import Any, Mapping

# Wired: after outer-loop xdot_c / commanded normal is computed, before
# calibrated_runtime.command / RTDE qdot packet (and before envelope if armed).
HOOK_POINT = (
    "post_outer_loop_xdot_c.normal → PathEntryRateLimitRamp.apply → "
    "pre_calibrated_runtime.command / ActiveMotionEnvelopeV3"
)

ENV_FLAG = "R008_PATH_ENTRY_RATE_LIMIT"

# Defaults from attempt10 LIMIT50 pathring evidence
# (live_20260806_002340… attempt_ordinal=10: 6.45N→61.24N in 0.2177s,
# mean |vz|≈2.7 mm/s, Δz≈0.59 mm).
#
# Entry window keeps amp near the baseline/search 0.5 mm/s primitive and only
# opens to 1.0 mm/s by window end — *not* the B6 5 mm/s path cap.  After the
# window this seam disengages; ActiveMotionEnvelopeV3 / profile caps resume.
DEFAULT_WINDOW_S = 0.150
DEFAULT_ENTRY_AMP_CAP_M_S = 0.0005
DEFAULT_FULL_AMP_CAP_M_S = 0.001
DEFAULT_MAX_SLEW_M_S2 = (
    (DEFAULT_FULL_AMP_CAP_M_S - DEFAULT_ENTRY_AMP_CAP_M_S) / DEFAULT_WINDOW_S
)
# Ease-in exponent for amp_ceil(e): higher = stay low longer inside the window.
DEFAULT_CEILING_GAMMA = 2.0

_PATH_MODE_TOKENS = frozenset({"path", "2"})
_NON_PATH_MODE_TOKENS = frozenset(
    {
        "baseline",
        "search",
        "contact_search",
        "hold",
        "retract",
        "stop",
        "0",
        "1",
        "3",
        "4",
    }
)


class PathEntryRateLimitError(ValueError):
    """Rate-limit seam refused unsafe inputs / config."""


def env_flag_enabled(environ: Mapping[str, str] | None = None) -> bool:
    """Return True only when the opt-in env flag is explicitly truthy."""

    env = os.environ if environ is None else environ
    raw = str(env.get(ENV_FLAG, "0")).strip().lower()
    return raw in {"1", "true", "yes", "on"}


def _finite(name: str, value: float) -> float:
    number = float(value)
    if not math.isfinite(number):
        raise PathEntryRateLimitError(f"{name} must be finite")
    return number


def _normalize_mode(mode: str | int | None) -> str:
    if mode is None:
        return ""
    if isinstance(mode, bool):
        raise PathEntryRateLimitError("mode must be str or int, not bool")
    if isinstance(mode, int):
        return str(mode)
    return str(mode).strip().lower()


def _is_path_mode(mode: str) -> bool:
    return mode in _PATH_MODE_TOKENS


def _is_non_path_mode(mode: str) -> bool:
    return mode in _NON_PATH_MODE_TOKENS or mode == ""


@dataclass(frozen=True)
class PathEntryRateLimitConfig:
    """Feature-flagged ramp parameters (defaults keep live behaviour unchanged)."""

    enabled: bool = False
    window_s: float = DEFAULT_WINDOW_S
    entry_amp_cap_m_s: float = DEFAULT_ENTRY_AMP_CAP_M_S
    full_amp_cap_m_s: float = DEFAULT_FULL_AMP_CAP_M_S
    max_slew_m_s2: float = DEFAULT_MAX_SLEW_M_S2
    ceiling_gamma: float = DEFAULT_CEILING_GAMMA
    require_env_flag: bool = True

    def __post_init__(self) -> None:
        window = _finite("window_s", self.window_s)
        if not (0.100 - 1e-12 <= window <= 0.200 + 1e-12):
            raise PathEntryRateLimitError(
                "window_s must be inside [0.100, 0.200] (PATH entry ramp)"
            )
        entry = _finite("entry_amp_cap_m_s", self.entry_amp_cap_m_s)
        full = _finite("full_amp_cap_m_s", self.full_amp_cap_m_s)
        slew = _finite("max_slew_m_s2", self.max_slew_m_s2)
        gamma = _finite("ceiling_gamma", self.ceiling_gamma)
        if entry <= 0.0 or full <= 0.0 or slew <= 0.0:
            raise PathEntryRateLimitError("caps and slew must be positive")
        if entry > full:
            raise PathEntryRateLimitError(
                "entry_amp_cap_m_s must be <= full_amp_cap_m_s"
            )
        if gamma < 1.0:
            raise PathEntryRateLimitError("ceiling_gamma must be >= 1")

    @classmethod
    def offline_enabled(cls, **overrides: Any) -> "PathEntryRateLimitConfig":
        """Unit-test / offline harness helper (does not require env flag)."""

        base: dict[str, Any] = {"enabled": True, "require_env_flag": False}
        base.update(overrides)
        return cls(**base)

    @classmethod
    def from_environ(
        cls,
        environ: Mapping[str, str] | None = None,
        **overrides: Any,
    ) -> "PathEntryRateLimitConfig":
        """Build config; ``enabled`` follows the env flag unless overridden."""

        flagged = env_flag_enabled(environ)
        base: dict[str, Any] = {"enabled": bool(flagged), "require_env_flag": True}
        base.update(overrides)
        return cls(**base)

    def is_armed(self, environ: Mapping[str, str] | None = None) -> bool:
        """Live-safe gate: config.enabled AND (optional) env flag."""

        if not self.enabled:
            return False
        if self.require_env_flag and not env_flag_enabled(environ):
            return False
        return True


@dataclass(frozen=True)
class PathEntryRateLimitResult:
    commanded_normal_m_s: float
    limited_normal_m_s: float
    amp_ceiling_m_s: float
    elapsed_s: float | None
    active: bool
    clipped: bool
    reason: str


def amp_ceiling_m_s(elapsed_s: float, config: PathEntryRateLimitConfig) -> float:
    """Ease-in amplitude ceiling ramp over the entry window.

    ``amp_ceil(e) = entry + (full - entry) * saturate(e / window) ** gamma``

    ``gamma=1`` is linear; default ``gamma=2`` stays near the entry cap longer
    (stronger protection against Ki-pocket intrusion at STAGE25 open).
    """

    e = max(0.0, float(elapsed_s))
    w = float(config.window_s)
    if e >= w:
        return float(config.full_amp_cap_m_s)
    alpha = min(1.0, e / w) ** float(config.ceiling_gamma)
    return float(config.entry_amp_cap_m_s) + alpha * (
        float(config.full_amp_cap_m_s) - float(config.entry_amp_cap_m_s)
    )


@dataclass
class PathEntryRateLimitRamp:
    """Stateful PATH-entry ramp.  Safe no-op when not armed."""

    config: PathEntryRateLimitConfig = field(
        default_factory=PathEntryRateLimitConfig
    )
    _prev_mode: str = ""
    _prev_u_m_s: float = 0.0
    _elapsed_s: float | None = None
    _entry_mono_s: float | None = None
    _window_active: bool = False

    def reset(self) -> None:
        self._prev_mode = ""
        self._prev_u_m_s = 0.0
        self._elapsed_s = None
        self._entry_mono_s = None
        self._window_active = False

    def _enter_path(self, *, mono: float | None, environ: Mapping[str, str] | None) -> None:
        self._elapsed_s = 0.0
        self._entry_mono_s = mono
        self._window_active = self.config.is_armed(environ)

    def _leave_path(self) -> None:
        self._elapsed_s = None
        self._entry_mono_s = None
        self._window_active = False

    def apply(
        self,
        *,
        commanded_normal_m_s: float,
        mode: str | int,
        dt_s: float,
        monotonic_s: float | None = None,
        environ: Mapping[str, str] | None = None,
    ) -> PathEntryRateLimitResult:
        """Rate-limit normal command amplitude during the PATH entry window.

        Parameters
        ----------
        commanded_normal_m_s:
            Signed normal speed command (m/s) from the outer loop / path
            controller *before* this seam (and ideally before the B6 envelope).
        mode:
            Controller mode string (``baseline``/``path``/…) or TP
            ``command_mode`` int (2 = PATH).
        dt_s:
            Actual control dt for the slew clamp.
        monotonic_s:
            Optional mono clock for elapsed; when omitted, elapsed += dt each
            path tick after the rising edge.
        """

        u_cmd = _finite("commanded_normal_m_s", commanded_normal_m_s)
        dt = _finite("dt_s", dt_s)
        if dt <= 0.0 or dt >= 0.08:
            raise PathEntryRateLimitError("dt_s must be inside (0, 80ms)")

        mode_s = _normalize_mode(mode)
        mono = None if monotonic_s is None else _finite("monotonic_s", monotonic_s)

        rising_into_path = _is_path_mode(mode_s) and not _is_path_mode(self._prev_mode)
        if rising_into_path:
            self._enter_path(mono=mono, environ=environ)
        elif not _is_path_mode(mode_s):
            self._leave_path()
            self._prev_mode = mode_s
            self._prev_u_m_s = u_cmd
            return PathEntryRateLimitResult(
                commanded_normal_m_s=u_cmd,
                limited_normal_m_s=u_cmd,
                amp_ceiling_m_s=float(self.config.full_amp_cap_m_s),
                elapsed_s=None,
                active=False,
                clipped=False,
                reason="non_path_mode",
            )

        self._prev_mode = mode_s

        if not self.config.is_armed(environ):
            self._prev_u_m_s = u_cmd
            elapsed = self._current_elapsed(mono=mono, dt=dt, advance=False)
            return PathEntryRateLimitResult(
                commanded_normal_m_s=u_cmd,
                limited_normal_m_s=u_cmd,
                amp_ceiling_m_s=float(self.config.full_amp_cap_m_s),
                elapsed_s=elapsed,
                active=False,
                clipped=False,
                reason="disabled",
            )

        elapsed = self._current_elapsed(
            mono=mono, dt=dt, advance=not rising_into_path
        )
        assert elapsed is not None

        if elapsed >= float(self.config.window_s) or not self._window_active:
            self._window_active = False
            self._prev_u_m_s = u_cmd
            return PathEntryRateLimitResult(
                commanded_normal_m_s=u_cmd,
                limited_normal_m_s=u_cmd,
                amp_ceiling_m_s=float(self.config.full_amp_cap_m_s),
                elapsed_s=float(elapsed),
                active=False,
                clipped=False,
                reason="window_elapsed",
            )

        ceiling = amp_ceiling_m_s(elapsed, self.config)
        max_du = float(self.config.max_slew_m_s2) * dt
        u_slew = min(u_cmd, self._prev_u_m_s + max_du)
        u_slew = max(u_slew, self._prev_u_m_s - max_du)
        u_lim = min(max(u_slew, -ceiling), ceiling)
        clipped = abs(u_lim - u_cmd) > 1e-15
        self._prev_u_m_s = u_lim
        return PathEntryRateLimitResult(
            commanded_normal_m_s=u_cmd,
            limited_normal_m_s=float(u_lim),
            amp_ceiling_m_s=float(ceiling),
            elapsed_s=float(elapsed),
            active=True,
            clipped=clipped,
            reason="entry_ramp" if clipped else "entry_ramp_passthrough",
        )

    def _current_elapsed(
        self,
        *,
        mono: float | None,
        dt: float,
        advance: bool,
    ) -> float | None:
        if self._elapsed_s is None and self._entry_mono_s is None:
            return None
        if mono is not None and self._entry_mono_s is not None:
            elapsed = max(0.0, mono - float(self._entry_mono_s))
            self._elapsed_s = elapsed
            return elapsed
        if self._elapsed_s is None:
            self._elapsed_s = 0.0
        elif advance:
            self._elapsed_s = float(self._elapsed_s) + dt
        return float(self._elapsed_s)


def reconstruct_pi_normal_speed_m_s(
    *,
    force_p_gain: float,
    force_i_gain: float,
    filtered_normal_n: float,
    force_integral_n_s: float,
    setpoint_n: float = 5.0,
    normal_limit_m_s: float | None = None,
) -> float:
    """Offline PI normal-speed reconstruction (path_controller-shaped)."""

    error = float(setpoint_n) - float(filtered_normal_n)
    speed = float(force_p_gain) * error + float(force_i_gain) * float(
        force_integral_n_s
    )
    if normal_limit_m_s is not None:
        lim = abs(float(normal_limit_m_s))
        speed = max(-lim, min(lim, speed))
    return float(speed)


def estimate_spring_force_rise_n(
    *,
    velocities_m_s: list[float],
    dt_values_s: list[float],
    k_eff_n_per_m: float,
    f0_n: float,
) -> list[float]:
    """Integrate ``F -= k * v * dt`` (v>0 = retract along +Z / reduce load).

    For attempt10, measured ``tcp_z`` decreases while force rises, so callers
    should pass the signed Z velocity (negative into the surface).
    """

    if len(velocities_m_s) != len(dt_values_s):
        raise PathEntryRateLimitError("velocities/dt length mismatch")
    forces: list[float] = []
    f = float(f0_n)
    k = float(k_eff_n_per_m)
    for v, dt in zip(velocities_m_s, dt_values_s, strict=True):
        f = f - k * float(v) * float(dt)
        forces.append(f)
    return forces


__all__ = [
    "DEFAULT_CEILING_GAMMA",
    "DEFAULT_ENTRY_AMP_CAP_M_S",
    "DEFAULT_FULL_AMP_CAP_M_S",
    "DEFAULT_MAX_SLEW_M_S2",
    "DEFAULT_WINDOW_S",
    "ENV_FLAG",
    "HOOK_POINT",
    "PathEntryRateLimitConfig",
    "PathEntryRateLimitError",
    "PathEntryRateLimitRamp",
    "PathEntryRateLimitResult",
    "amp_ceiling_m_s",
    "env_flag_enabled",
    "estimate_spring_force_rise_n",
    "reconstruct_pi_normal_speed_m_s",
]
