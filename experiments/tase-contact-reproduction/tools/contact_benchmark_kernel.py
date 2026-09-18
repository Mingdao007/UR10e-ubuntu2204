"""Offline composition of one native law, common outer task and shared QP.

No transport. The legacy physical owner must separately admit timing, package,
frame, sensor guards and observer state before any proposed velocity is sent.
"""
from __future__ import annotations
import hashlib,json,time,math
from pathlib import Path
import numpy as np
from contact_laws import ContactLaw
from contact_qp import NativeContactQp
from contact_benchmark_outer import ContactOuterLoop


class KernelDeadlineError(RuntimeError):
    pass


class ContactKernel:
    def __init__(self, *, law: ContactLaw, qp_library: Path, anchor_m, task_basis,
                 target_rotation, raw_force_limit_n, raw_torque_limit_nm,
                 qp_deadline_s=.001, kernel_deadline_s=.0015):
        if law.dimension != 3 or law.dt_s != .002:
            raise ValueError('benchmark requires 3D law at 2ms')
        if kernel_deadline_s is not None and (not math.isfinite(kernel_deadline_s) or kernel_deadline_s<=0):
            raise ValueError('kernel deadline must be positive; None is offline only')
        self.kernel_deadline_s=kernel_deadline_s
        self.law=law
        self.qp=NativeContactQp(qp_library,deadline_s=qp_deadline_s)
        self.outer=ContactOuterLoop(law_step=lambda f,dt:law.step_elapsed(f,dt_s=dt).command,
                anchor_m=anchor_m,task_basis=task_basis,target_rotation=target_rotation,
                raw_force_limit_n=raw_force_limit_n,raw_torque_limit_nm=raw_torque_limit_nm)
        identity={'law':law.law,'parameters':law.parameters,'outer':self.outer.identity,
                  'qp_library_sha256':hashlib.sha256(Path(qp_library).read_bytes()).hexdigest()}
        self.identity=hashlib.sha256(json.dumps(identity,sort_keys=True).encode()).hexdigest()

    def snapshot(self):
        return {'identity':self.identity,'law':self.law.snapshot(),'outer':self.outer.snapshot(),
                'qp':self.qp.snapshot()}

    def restore(self,state):
        if state.get('identity') != self.identity:raise ValueError('kernel snapshot identity differs')
        old=self.snapshot()
        try:
            self.law.restore(state['law']);self.outer.restore(state['outer']);self.qp.restore(state['qp'])
        except Exception:
            self.law.restore(old['law']);self.outer.restore(old['outer']);self.qp.restore(old['qp']);raise

    def step(self, *, jacobian, joint_velocity_lower, joint_velocity_upper, **observation):
        started=time.perf_counter();before=self.snapshot()
        try:
            outer=self.outer.step(**observation)
            solved=self.qp.solve(jacobian,outer['twist_base'],joint_velocity_lower,joint_velocity_upper)
            elapsed=time.perf_counter()-started
            if self.kernel_deadline_s is not None and elapsed>self.kernel_deadline_s:
                raise KernelDeadlineError(f'full kernel deadline exceeded: {elapsed:.6f}s')
        except Exception:
            self.restore(before)
            raise
        return {**outer,'controller':self.law.law,'qdot_rad_s':solved.qdot,
                'qp_equality_residual':solved.equality_residual,'qp_bound_violation':solved.bound_violation,
                'qp_solve_wall_s':solved.elapsed_s,'kernel_wall_s':time.perf_counter()-started,
                'claim_scope':'offline proposal; no transport or live qualification'}
