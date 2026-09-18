"""Common contact task for six laws, independent of transport and device access.

A law consumes environment-on-tool force minus the desired reaction wrench.
Tangential tracking and fixed posture are shared. The law never switches at a
perturbation. Research law seeds require contact tuning and physical validation.
"""
from __future__ import annotations
from dataclasses import dataclass
import math
import hashlib
import json
from typing import Callable
import numpy as np
import pinocchio as pin
from contact_semantics import approach_normal_from_reaction, signed_normal_load_n, force_error_n
from contact_benchmark_protocol import (
    ENTRY_DURATION_S,
    FRESH_AGE_S,
    STALE_AGE_S,
    Task,
    classify_sensor_age,
)
from step5d_autotune_v4_r012.safety_filter import SafetyFilterConfig, filter_path_error_twist


def finite(value, shape, role):
    a=np.asarray(value,dtype=float)
    if a.shape != shape or not np.isfinite(a).all():
        raise ValueError(f"invalid {role}")
    return a


@dataclass(frozen=True)
class CommonSettings:
    path_kp_s_inv: float = 4.
    orientation_kp_s_inv: float = .1
    filter_tau_s: float = .02
    normal_speed_cap_m_s: float = .003
    tangent_speed_cap_m_s: float = .01
    angular_speed_cap_rad_s: float = .05
    # Guards are mandatory explicit inputs from the bound bench contract.

    def __post_init__(self):
        if any(not math.isfinite(v) or v <= 0 for v in self.__dict__.values()):
            raise ValueError('common settings must be finite positive values')


class ContactOuterLoop:
    def __init__(self, *, law_step: Callable, anchor_m, task_basis, target_rotation,
                 raw_force_limit_n: float, raw_torque_limit_nm: float,
                 settings=CommonSettings()):
        self.law_step=law_step; self.settings=settings; self.task=Task()
        self.anchor=finite(anchor_m,(3,),'anchor').copy()
        self.basis=finite(task_basis,(3,3),'task basis').copy()
        self.target=finite(target_rotation,(3,3),'target rotation').copy()
        for R in (self.basis,self.target):
            if not np.allclose(R.T@R,np.eye(3),atol=1e-8) or np.linalg.det(R)<.999999:
                raise ValueError('task/target must be right-handed orthonormal rotations')
        self.reaction=self.basis[:,2]
        self.approach=approach_normal_from_reaction(self.reaction)
        if float(self.target[:,2]@self.approach)<.999:
            raise ValueError('fixed tool +Z disagrees with contact-search approach')
        if any(not math.isfinite(v) or v<=0 for v in (raw_force_limit_n,raw_torque_limit_nm)):
            raise ValueError('raw guard limits must be explicit finite positive values')
        self.force_limit=raw_force_limit_n;self.torque_limit=raw_torque_limit_nm
        self.filtered=np.zeros(3);self.initialized=False;self.previous_xy=(0.,0.)
        self.last_time=None;self.phase=None;self.last_entry_time=None;self.last_path_time=None
        # Inscribed box makes every projected XY command obey the norm cap.
        xy_cap=settings.tangent_speed_cap_m_s/math.sqrt(2.)
        self.safety=SafetyFilterConfig(velocity_min_m_s=(-xy_cap,-xy_cap),
                                       velocity_max_m_s=(xy_cap,xy_cap),
                                       max_state_age_s=STALE_AGE_S)
        binding={'settings':settings.__dict__,'anchor':self.anchor.tolist(),
                 'basis':self.basis.tolist(),'target':self.target.tolist(),
                 'force_limit':self.force_limit,'torque_limit':self.torque_limit,
                 'fresh_age_s':FRESH_AGE_S,'stale_age_s':STALE_AGE_S,
                 'held_policy':'latest_value_zero_order_hold_no_interpolation',
                 'geometric_latency_guard_is_independent':True}
        self.identity=hashlib.sha256(json.dumps(binding,sort_keys=True).encode()).hexdigest()

    def snapshot(self):
        # Native law history is separately mandatory in the composed snapshot.
        return {'identity':self.identity,'filtered_force_base_n':self.filtered.tolist(),'initialized':self.initialized,
                'previous_xy_m_s':list(self.previous_xy),'last_time_s':self.last_time,
                'phase':self.phase,'last_entry_time_s':self.last_entry_time,
                'last_path_time_s':self.last_path_time}

    def restore(self, state):
        if state.get('identity') != self.identity or not isinstance(state.get('initialized'),bool):
            raise ValueError('outer state identity differs')
        f=finite(state['filtered_force_base_n'],(3,),'filtered state').copy()
        v=finite(state['previous_xy_m_s'],(2,),'previous command').copy()
        t=state['last_time_s']
        if t is not None and (not math.isfinite(t) or t < 0):
            raise ValueError('invalid snapshot time')
        phase=state['phase'];entry_t=state.get('last_entry_time_s');path_t=state['last_path_time_s']
        if phase not in (None,'baseline','entry','path'):
            raise ValueError('invalid snapshot phase')
        if (phase=='path') != (path_t is not None):
            raise ValueError('snapshot path clock differs from phase')
        if entry_t is not None and (not math.isfinite(entry_t)
                                    or not 0 <= entry_t <= ENTRY_DURATION_S + 1e-10):
            raise ValueError('invalid snapshot entry clock')
        if phase == 'entry' and entry_t is None:
            raise ValueError('snapshot entry phase has no entry clock')
        if phase in (None,'baseline') and entry_t is not None:
            raise ValueError('snapshot baseline clock differs from phase')
        if phase == 'path' and entry_t is not None and not math.isclose(
                entry_t, ENTRY_DURATION_S, rel_tol=0., abs_tol=1e-10):
            raise ValueError('snapshot PATH has incomplete entry clock')
        if entry_t is not None:
            self.task.entry_reference(entry_t)
        if path_t is not None:self.task.reference(path_t)
        self.filtered=f;self.previous_xy=tuple(v);self.last_time=t;self.initialized=state['initialized']
        self.phase=phase;self.last_entry_time=entry_t;self.last_path_time=path_t

    def step(self, *, time_s, dt_s, position_m, rotation, raw_force_base_n,
             raw_torque_base_nm, injection_task_n=(0.,0.,0.), state_age_s=0.,
             phase='path', path_time_s=None, force_reference_n=None,
             entry_time_s=None):
        if not math.isfinite(dt_s) or not 0 < dt_s <= .004:
            raise ValueError('elapsed sample interval must be in (0, 4ms]')
        if not math.isfinite(time_s) or time_s < 0:
            raise ValueError('invalid sample clock')
        if not math.isfinite(state_age_s) or state_age_s < 0:
            raise ValueError('invalid observation age')
        age_band=classify_sensor_age(state_age_s)
        if age_band == 'stale':
            raise ValueError('stale observation at 80ms')
        if self.last_time is not None and time_s <= self.last_time:
            raise ValueError('task clock must advance; no implicit state reset')
        if phase not in ('baseline','entry','path'):
            raise ValueError('invalid phase transition; no return from path to baseline')
        if self.phase == 'path' and phase != 'path':
            raise ValueError('invalid phase transition; no return from path to baseline')
        if phase == 'entry' and self.phase not in ('baseline','entry'):
            raise ValueError('entry requires baseline phase')
        if phase == 'path' and self.phase == 'entry' and (
                self.last_entry_time is None
                or not math.isclose(self.last_entry_time, ENTRY_DURATION_S,
                                    rel_tol=0., abs_tol=1e-10)):
            raise ValueError('formal PATH requires completed entry')
        target_force=self.task.normal_force_n if force_reference_n is None else float(force_reference_n)
        if not math.isfinite(target_force) or not 1. <= target_force <= self.task.normal_force_n:
            raise ValueError('contact reference must remain within 1 to 5 N')
        if phase=='path':
            if entry_time_s is not None:
                raise ValueError('PATH has no entry clock')
            if target_force != self.task.normal_force_n:raise ValueError('PATH requires 5 N reference')
            path_t=time_s if path_time_s is None else path_time_s
            if self.last_path_time is not None and path_t <= self.last_path_time:
                raise ValueError('path clock must advance')
            reference=self.task.reference(path_t)
            entry_t=self.last_entry_time
        elif phase=='entry':
            if path_time_s is not None and entry_time_s is not None:
                if not math.isclose(path_time_s, entry_time_s, rel_tol=0., abs_tol=1e-12):
                    raise ValueError('entry clocks disagree')
            entry_t=entry_time_s if entry_time_s is not None else path_time_s
            if entry_t is None:
                raise ValueError('entry clock is required')
            if (not math.isfinite(entry_t)
                    or not 0 <= entry_t <= ENTRY_DURATION_S + 1e-10):
                raise ValueError('entry clock outside one-second entry')
            if self.phase == 'baseline' and entry_t > 1e-10:
                raise ValueError('entry clock must start at zero')
            if self.last_entry_time is not None and entry_t <= self.last_entry_time:
                raise ValueError('entry clock must advance')
            if target_force != self.task.normal_force_n:
                raise ValueError('entry requires 5 N reference')
            path_t=None
            reference=self.task.entry_reference(entry_t)
        else:
            if path_time_s is not None or entry_time_s is not None:
                raise ValueError('baseline has no path clock')
            path_t=None
            entry_t=None
            reference={'position_m':(0.,0.,0.),'velocity_m_s':(0.,0.,0.)}
        # R012 predicts 2ms; reserve its existing 1mm latency tightening for
        # sample age and at most 2ms extra hold. Both robot/reference speeds
        # are bounded here; this is a geometric bound, not timing qualification.
        relative_speed=self.settings.tangent_speed_cap_m_s+self.task.sanity()['speed_upper_bound_m_s']
        if (state_age_s+.002)*relative_speed > self.safety.latency_error_bound_m:
            raise ValueError('latency uncertainty exceeds safety tightening')
        pos=finite(position_m,(3,),'position'); R=finite(rotation,(3,3),'rotation')
        if not np.allclose(R.T@R,np.eye(3),atol=1e-6) or np.linalg.det(R)<.999:
            raise ValueError('invalid observed rotation')
        raw=finite(raw_force_base_n,(3,),'raw force')
        torque=finite(raw_torque_base_nm,(3,),'raw torque')
        injection=finite(injection_task_n,(3,),'software disturbance')
        if phase != 'path' and np.any(injection):
            raise ValueError('disturbance requires formal PATH phase')
        # Mandatory guard BEFORE filtering or adding a software disturbance.
        if np.linalg.norm(raw)>=self.force_limit or np.linalg.norm(torque)>=self.torque_limit:
            raise ValueError('raw sensor guard rejected observation')
        local_pos=self.basis.T@(pos-self.anchor)
        ref=np.asarray(reference['position_m']); ref_v=np.asarray(reference['velocity_m_s'])
        ref_a=np.asarray(reference.get('acceleration_m_s2',(0.,0.,0.)))
        error=local_pos-ref
        rho=float((error[0]/.025)**2+(error[1]/.015)**2)
        if rho >= 1.:
            raise ValueError('hard PATH error ellipse violated')
        alpha=-math.expm1(-dt_s/self.settings.filter_tau_s)
        filtered=raw.copy() if not self.initialized else self.filtered+alpha*(raw-self.filtered)
        force_input=self.basis.T@filtered - np.array([0.,0.,target_force])+injection
        law_velocity=finite(self.law_step(force_input,dt_s),(3,),'law velocity')
        proposal=law_velocity.copy()
        proposal[:2]+=ref_v[:2]-self.settings.path_kp_s_inv*error[:2]
        capped=proposal.copy();capped[2]=np.clip(capped[2],-self.settings.normal_speed_cap_m_s,self.settings.normal_speed_cap_m_s)
        speed=np.linalg.norm(capped[:2])
        if speed>self.settings.tangent_speed_cap_m_s:
            capped[:2]*=self.settings.tangent_speed_cap_m_s/speed
        cap_intervention=float(np.linalg.norm(capped-proposal))
        soft_rho=sum((error[i]/self.safety.tightened_axes_m[i])**2 for i in range(2))
        intervention=0.
        if soft_rho >= self.safety.engage_deadband:
            filtered_twist,outcome=filter_path_error_twist(tuple(local_pos[:2]),tuple(ref[:2]),
                tuple(ref_v[:2]),tuple(capped)+(0.,0.,0.),self.previous_xy,
                state_age_s=state_age_s,config=self.safety)
            if not outcome.valid: raise ValueError('PATH safety projection infeasible')
            capped[:2]=filtered_twist[:2];intervention=outcome.intervention_norm_m_s
        omega=self.settings.orientation_kp_s_inv*np.asarray(pin.log3(self.target@R.T))
        wn=np.linalg.norm(omega)
        if wn>self.settings.angular_speed_cap_rad_s: omega*=self.settings.angular_speed_cap_rad_s/wn
        self.filtered=filtered;self.initialized=True;self.previous_xy=tuple(capped[:2]);self.last_time=time_s
        self.phase=phase
        if phase == 'entry':self.last_entry_time=entry_t
        self.last_path_time=path_t
        return {'twist_base':tuple(self.basis@capped)+tuple(omega),
                'raw_force_base_n':tuple(raw),'filtered_force_base_n':tuple(filtered),
                'injection_task_n':tuple(injection),'law_input_task_n':tuple(force_input),
                'phase':phase,'stage':'formal' if phase=='path' else phase,
                'sample_time_s':time_s,'entry_time_s':entry_t,
                'observation_age_s':state_age_s,'age_band':age_band,
                'formal_time_s':path_t if phase=='path' else None,
                'path_time_s':path_t,
                'elapsed_dt_s':dt_s,'reference_force_n':target_force,
                'normal_load_n':signed_normal_load_n(raw,self.reaction),
                'force_error_n':force_error_n(target_load_n=target_force,
                                              normal_load_n=signed_normal_load_n(raw,self.reaction)),
                'reference_position_base_m':tuple(self.anchor+self.basis@ref),
                'reference_velocity_task_m_s':tuple(ref_v),
                'reference_acceleration_task_m_s2':tuple(ref_a),
                'path_error_task_m':tuple(error),'law_velocity_task_m_s':tuple(law_velocity),
                'unconstrained_task_velocity_m_s':tuple(proposal),
                'capped_task_velocity_m_s':tuple(capped),'path_cbf_intervention_m_s':intervention,
                'velocity_cap_intervention_m_s':cap_intervention}
