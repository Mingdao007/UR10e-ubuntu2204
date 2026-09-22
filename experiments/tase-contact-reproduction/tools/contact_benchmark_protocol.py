"""Preregistered task and scheduling, with no hardware access or live authority."""
from __future__ import annotations

from dataclasses import dataclass, asdict
import hashlib
from bisect import insort_right
import json
import math
import random

CONTROLLERS = ("LAC", "NAC", "SFC", "DSFC", "ISFC", "MSFC")
SCENARIOS = ("nominal", "normal_pulse", "tangent_release", "oblique_double", "human_push")
PERIOD_S = 2 * math.pi / 0.1
ENTRY_DURATION_S = 1.0
ENTRY_VELOCITY_M_S = (0.004, 0.002, 0.0)

# Contact benchmark freshness is deliberately separate from the R012 global
# default.  Twenty milliseconds identifies a fresh observation; the runtime
# fail-closed boundary is eighty milliseconds, matching the historical SFC
# latest-value/ZOH contract.
FRESH_AGE_S = 0.020
STALE_AGE_S = 0.080
FRESHNESS_SENSITIVITY_CUTOFFS_S = (0.020, 0.040, 0.060, 0.080)


def classify_sensor_age(age_s):
    """Classify a non-negative host-monotonic observation age.

    Boundaries are intentional: exactly 20 ms is held/delayed and exactly
    80 ms is stale.  A future timestamp is rejected by the caller before this
    helper is used.
    """
    try:
        age = float(age_s)
    except (TypeError, ValueError, OverflowError):
        raise ValueError("sensor age must be finite and non-negative") from None
    if not math.isfinite(age) or age < 0.0:
        raise ValueError("sensor age must be finite and non-negative")
    if age < FRESH_AGE_S:
        return "fresh"
    if age < STALE_AGE_S:
        return "held"
    return "stale"


def _percentile(values, fraction):
    if not values:
        return None
    ordered = sorted(float(value) for value in values)
    return _ordered_percentile(ordered, fraction)


def _ordered_percentile(ordered, fraction):
    if not ordered:
        return None
    position = (len(ordered) - 1) * float(fraction)
    lower = int(math.floor(position))
    upper = int(math.ceil(position))
    if lower == upper:
        return ordered[lower]
    weight = position - lower
    return ordered[lower] + weight * (ordered[upper] - ordered[lower])


class SensorFreshnessTracker:
    """Small per-trial evidence collector for age bands and stop causes."""

    def __init__(self):
        self._ages = []
        # Exact order statistics; retain original acquisition order separately.
        # List insertion can move O(n) pointers, but avoids three Python history
        # traversals/sorts on every tick. No quantization or downsampling.
        self._ordered_ages = []
        self._counts = {"fresh": 0, "held": 0, "stale": 0}
        self._held_streak = 0
        self._longest_hold_samples = 0
        self._longest_hold_s = 0.0
        self._stale_stop_count = 0
        self._geometric_latency_reject_count = 0

    def observe(self, age_s):
        age = float(age_s)
        band = classify_sensor_age(age)
        self._ages.append(age)
        insort_right(self._ordered_ages, age)
        self._counts[band] += 1
        if band == "held":
            self._held_streak += 1
            self._longest_hold_samples = max(self._longest_hold_samples, self._held_streak)
            self._longest_hold_s = max(self._longest_hold_s, age)
        else:
            self._held_streak = 0
        return band

    def checkpoint(self):
        return {'count': len(self._ages), 'counts': dict(self._counts),
                'held_streak': self._held_streak,
                'longest_hold_samples': self._longest_hold_samples,
                'longest_hold_s': self._longest_hold_s,
                'stale_stop_count': self._stale_stop_count,
                'geometric_latency_reject_count': self._geometric_latency_reject_count}

    def restore(self, state):
        from bisect import bisect_left
        count = int(state['count'])
        if not 0 <= count <= len(self._ages):
            raise ValueError('freshness checkpoint is not an earlier observation boundary')
        if count == 0:
            self._ages.clear()
            self._ordered_ages.clear()
        else:
            for age in self._ages[count:]:
                del self._ordered_ages[bisect_left(self._ordered_ages, age)]
            del self._ages[count:]
        self._counts = dict(state['counts'])
        for key in ('held_streak', 'longest_hold_samples', 'longest_hold_s',
                    'stale_stop_count', 'geometric_latency_reject_count'):
            setattr(self, '_' + key, state[key])

    def stale_stop(self, age_s):
        band = self.observe(age_s)
        if band != "stale":
            raise ValueError("stale stop requires a stale observation")
        self._stale_stop_count += 1
        return band

    def geometric_latency_reject(self):
        self._geometric_latency_reject_count += 1

    def as_dict(self):
        count = len(self._ages)
        return {
            "fresh_age_s": FRESH_AGE_S,
            "stale_age_s": STALE_AGE_S,
            "observation_count": count,
            "fresh_count": self._counts["fresh"],
            "held_count": self._counts["held"],
            "stale_count": self._counts["stale"],
            "fresh_fraction": self._counts["fresh"] / count if count else 0.0,
            "held_fraction": self._counts["held"] / count if count else 0.0,
            "stale_fraction": self._counts["stale"] / count if count else 0.0,
            "age_p50_s": _ordered_percentile(self._ordered_ages, 0.50),
            "age_p95_s": _ordered_percentile(self._ordered_ages, 0.95),
            "age_p99_s": _ordered_percentile(self._ordered_ages, 0.99),
            "age_max_s": self._ordered_ages[-1] if self._ordered_ages else None,
            # Largest single held observation age; consecutive held samples
            # are reported separately so this field is not mistaken for an
            # interpolated duration.
            "longest_hold_s": self._longest_hold_s,
            "longest_hold_samples": self._longest_hold_samples,
            "stale_stop_count": self._stale_stop_count,
            "geometric_latency_reject_count": self._geometric_latency_reject_count,
            "policy": "fresh_lt_20ms_held_lt_80ms_stale_ge_80ms",
        }


def quintic_entry(time_s, *, duration_s=ENTRY_DURATION_S,
                  terminal_velocity_m_s=ENTRY_VELOCITY_M_S):
    """Return the bounded smooth reference used before formal PATH.

    The scalar profile is ``h(s)=-4s^3+7s^4-3s^5``.  It starts at zero
    position, velocity, and acceleration, and ends at zero position with the
    formal PATH initial velocity and zero acceleration.  This is a reference
    generator only; it carries no live or physical authority.
    """
    if (not math.isfinite(time_s) or not math.isfinite(duration_s)
            or duration_s <= 0 or not 0 <= time_s <= duration_s + 1e-10):
        raise ValueError("time outside smooth PATH entry")
    try:
        terminal = tuple(float(value) for value in terminal_velocity_m_s)
    except (TypeError, ValueError):
        raise ValueError("invalid entry terminal velocity") from None
    if len(terminal) != 3 or not all(math.isfinite(value) for value in terminal):
        raise ValueError("invalid entry terminal velocity")
    t = min(float(time_s), duration_s)
    s = t / duration_s
    h = -4*s**3 + 7*s**4 - 3*s**5
    hp = -12*s**2 + 28*s**3 - 15*s**4
    hpp = -24*s + 84*s**2 - 60*s**3
    return {
        "position_m": tuple(duration_s*value*h for value in terminal),
        "velocity_m_s": tuple(value*hp for value in terminal),
        "acceleration_m_s2": tuple(value/duration_s*hpp for value in terminal),
    }


@dataclass(frozen=True)
class Task:
    normal_force_n: float = 5.0
    along_amplitude_m: float = 0.04
    lateral_amplitude_m: float = 0.01
    omega_rad_s: float = 0.1

    @property
    def duration_s(self):
        return 2 * math.pi / self.omega_rad_s

    @property
    def execution_duration_s(self):
        """One smooth entry second followed by the unchanged formal period."""
        return ENTRY_DURATION_S + self.duration_s

    @property
    def initial_velocity_m_s(self):
        """Formal PATH velocity at time zero, used as the entry endpoint."""
        return (self.along_amplitude_m*self.omega_rad_s,
                2*self.lateral_amplitude_m*self.omega_rad_s, 0.)

    def reference(self, time_s):
        if not math.isfinite(time_s) or not 0 <= time_s <= self.duration_s + 1e-10:
            raise ValueError("time outside complete task period")
        a, b, w = self.along_amplitude_m, self.lateral_amplitude_m, self.omega_rad_s
        t = min(time_s, self.duration_s)
        return {"position_m": (a * math.sin(w*t), b * math.sin(2*w*t), 0.),
                "velocity_m_s": (a*w*math.cos(w*t), 2*b*w*math.cos(2*w*t), 0.),
                "acceleration_m_s2": (-a*w*w*math.sin(w*t), -4*b*w*w*math.sin(2*w*t), 0.),
                "reference_force_n": self.normal_force_n}

    def entry_reference(self, time_s):
        """Return the shared one-second entry reference in task coordinates."""
        return {**quintic_entry(time_s, terminal_velocity_m_s=self.initial_velocity_m_s),
                "reference_force_n": self.normal_force_n}

    def execution_reference(self, elapsed):
        """Map execution time to entry or formal PATH without changing PATH time.

        ``formal_time_s`` is deliberately ``None`` during entry.  At the seam
        ``elapsed == ENTRY_DURATION_S`` the formal reference starts at zero,
        so the reference position, velocity, and acceleration are continuous.
        """
        if (not math.isfinite(elapsed)
                or not 0 <= elapsed <= self.execution_duration_s + 1e-10):
            raise ValueError("time outside complete execution")
        elapsed = min(float(elapsed), self.execution_duration_s)
        if elapsed < ENTRY_DURATION_S:
            return {"stage": "entry", "formal_time_s": None,
                    **self.entry_reference(elapsed)}
        formal_time = elapsed - ENTRY_DURATION_S
        return {"stage": "formal", "formal_time_s": formal_time,
                **self.reference(formal_time)}

    def sanity(self):
        return {"duration_s": self.duration_s,
                "span_m": [2*self.along_amplitude_m, 2*self.lateral_amplitude_m],
                "speed_upper_bound_m_s": math.hypot(self.along_amplitude_m*self.omega_rad_s,
                                                    2*self.lateral_amplitude_m*self.omega_rad_s),
                "acceleration_upper_bound_m_s2": math.hypot(self.along_amplitude_m*self.omega_rad_s**2,
                                                           4*self.lateral_amplitude_m*self.omega_rad_s**2),
                "force_setpoint_n": self.normal_force_n,
                "qualification": "geometry only; actual joint trajectory, frame, limits and package require separate checks"}


def disturbance(scenario, time_s, *, amplitude_n):
    """Shared software wrench port in task coordinates (tangent, lateral, reaction).

    Explicit amplitude is mandatory: no offline waveform silently authorizes a
    physical amplitude. The raised-cosine pulses are continuous at boundaries.
    Actual hand pushes return no synthetic wrench and require measured evidence.
    """
    if scenario not in SCENARIOS or not math.isfinite(time_s) or time_s < 0:
        raise ValueError("invalid disturbance scenario/time")
    if not math.isfinite(amplitude_n) or amplitude_n < 0:
        raise ValueError("amplitude must be finite and nonnegative")
    def pulse(start, width):
        x = (time_s-start)/width
        return .5*(1-math.cos(2*math.pi*x)) if 0 < x < 1 else 0.
    if scenario in ("nominal", "human_push"):
        return (0.,0.,0.)
    if scenario == "normal_pulse":
        return (0.,0.,amplitude_n*pulse(20.,.5))
    if scenario == "tangent_release":
        t=time_s
        shape = .5*(1-math.cos(math.pi*(t-20.)/.5)) if 20. < t < 20.5 else (1. if 20.5 <= t <= 24.5 else (.5*(1+math.cos(math.pi*(t-24.5)/.5)) if 24.5 < t < 25. else 0.))
        return (amplitude_n*shape,0.,0.)
    value=amplitude_n*(pulse(20.,.5)+pulse(21.,.5))/math.sqrt(2)
    return (value,0.,value)


def holdout_schedule(seed: int, repeats: int = 5):
    """One randomized complete block per repeat, identical conditions for all laws."""
    if isinstance(repeats,bool) or not isinstance(repeats,int) or repeats < 1:
        raise ValueError("repeats must be a positive integer")
    rng=random.Random(seed); rows=[]
    for block in range(repeats):
        conditions=[(c,s) for c in CONTROLLERS for s in SCENARIOS]
        rng.shuffle(conditions)
        offset=len(rows)
        rows.extend({"ordinal":offset+i,"block":block,"controller":c,"scenario":s,
                     "stage":"frozen_holdout", "human_force_matching_claim":False}
                    for i,(c,s) in enumerate(conditions))
    return rows


def evaluation_budget():
    return {"per_controller_units":24,"initial_units":8,"bo_units":12,"repeat_units":4,
            "trials_per_unit":["nominal","disturbed"],"failed_attempt_consumes_unit":True,
            "reuse_physical_attempt_id_for_metrics":True,"new_retry_requires_new_attempt_id":True,
            "holdout_used_for_tuning":False}


def nominal_noninferiority(candidate, lac):
    """Only comparable complete trials may reach this preregistered criterion."""
    if not candidate["objective_eligible"] or not lac["objective_eligible"]:
        return {"pass":False,"reason":"incomplete_evidence"}
    for key in ("interval_start_s","interval_end_s","max_gap_s"):
        if candidate[key] != lac[key]:
            raise ValueError("metric intervals/gap policies differ")
    f_limit=lac["force_mae_n"]+max(.1*lac["force_mae_n"],.05)
    p_limit=lac["path_rms_m"]+max(.1*lac["path_rms_m"],.0001)
    return {"pass": candidate["force_mae_n"] <= f_limit and candidate["path_rms_m"] <= p_limit,
            "force_mae_limit_n":f_limit,"path_rms_limit_m":p_limit,
            "claim_scope":"paired metric criterion only; no statistical or physical acceptance"}


def protocol():
    task=Task()
    result={"schema":"contact-six-benchmark-v1","controllers":list(CONTROLLERS),
            "task":{**asdict(task),"duration_s":task.duration_s,"orientation":"fixed_task_frame"},
            "entry":{"profile":"quintic_h=-4s^3+7s^4-3s^5",
                     "duration_s":ENTRY_DURATION_S,
                     "terminal_velocity_m_s":list(task.initial_velocity_m_s)},
            "execution":{"entry_duration_s":ENTRY_DURATION_S,
                         "formal_start_s":ENTRY_DURATION_S,
                         "formal_duration_s":task.duration_s,
                         "duration_s":task.execution_duration_s,
                         "formal_metrics_start_s":ENTRY_DURATION_S,
                         "formal_metrics_duration_s":task.duration_s},
            "scenarios":list(SCENARIOS),"budget":evaluation_budget(),"repeats":5,
            "controller_switching_within_trial":False,"raw_sensor_guard_precedes_injection":True,
            "synthetic_disturbance_is_physical_impact":False,"rpsfc_selectable":False,
            "research_parameters_are_contact_qualified":False,
            "free_guidance_5_to_30_50_70_is_separate":True,
            "freshness": {
                "fresh_age_s": FRESH_AGE_S,
                "stale_age_s": STALE_AGE_S,
                "bands": {
                    "fresh": "0 <= age < 0.020",
                    "held": "0.020 <= age < 0.080",
                    "stale": "age >= 0.080",
                },
                "held_policy": "latest_value_zero_order_hold_no_interpolation",
                "stale_policy": "fail_closed_stop_zero_and_censor_trial",
                "comparison_policy": "same_capture_session_and_raw_sensor_stream",
                "geometric_latency_guard_is_independent": True,
            },
            "freshness_sensitivity_cutoffs_s": list(FRESHNESS_SENSITIVITY_CUTOFFS_S)}
    result["sha256"]=hashlib.sha256(json.dumps(result,sort_keys=True,separators=(",",":")).encode()).hexdigest()
    return result
