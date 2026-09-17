"""Preregistered task and scheduling, with no hardware access or live authority."""
from __future__ import annotations

from dataclasses import dataclass, asdict
import hashlib
import json
import math
import random

CONTROLLERS = ("LAC", "NAC", "SFC", "DSFC", "ISFC", "MSFC")
SCENARIOS = ("nominal", "normal_pulse", "tangent_release", "oblique_double", "human_push")
PERIOD_S = 2 * math.pi / 0.1


@dataclass(frozen=True)
class Task:
    normal_force_n: float = 5.0
    along_amplitude_m: float = 0.04
    lateral_amplitude_m: float = 0.01
    omega_rad_s: float = 0.1

    @property
    def duration_s(self):
        return 2 * math.pi / self.omega_rad_s

    def reference(self, time_s):
        if not math.isfinite(time_s) or not 0 <= time_s <= self.duration_s + 1e-10:
            raise ValueError("time outside complete task period")
        a, b, w = self.along_amplitude_m, self.lateral_amplitude_m, self.omega_rad_s
        t = min(time_s, self.duration_s)
        return {"position_m": (a * math.sin(w*t), b * math.sin(2*w*t), 0.),
                "velocity_m_s": (a*w*math.cos(w*t), 2*b*w*math.cos(2*w*t), 0.),
                "acceleration_m_s2": (-a*w*w*math.sin(w*t), -4*b*w*w*math.sin(2*w*t), 0.),
                "reference_force_n": self.normal_force_n}

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
            "scenarios":list(SCENARIOS),"budget":evaluation_budget(),"repeats":5,
            "controller_switching_within_trial":False,"raw_sensor_guard_precedes_injection":True,
            "synthetic_disturbance_is_physical_impact":False,"rpsfc_selectable":False,
            "research_parameters_are_contact_qualified":False,
            "free_guidance_5_to_30_50_70_is_separate":True}
    result["sha256"]=hashlib.sha256(json.dumps(result,sort_keys=True,separators=(",",":")).encode()).hexdigest()
    return result
