"""Open, transport-free controller registry with a common observation contract.

No live writer or device is imported. The printed TASE sign and mature sign
are separate methods; their identical outer-loop QP ablation is explicit.
"""
from dataclasses import dataclass, asdict
from pathlib import Path
import copy
import hashlib
import math
import numpy as np


class RegistryError(ValueError):
    pass


@dataclass(frozen=True)
class MethodSpec:
    name: str
    role: str
    implementation: str
    qualification: str = 'software_only'


class MethodRegistry:
    def __init__(self):
        self._methods = {}

    def register(self, spec, factory):
        if not isinstance(spec, MethodSpec) or not spec.name or not callable(factory):
            raise RegistryError('typed method specification and callable factory required')
        if spec.name in self._methods:
            raise RegistryError('method is already registered: ' + spec.name)
        self._methods[spec.name] = (spec, factory)

    def describe(self):
        return [asdict(spec) for spec, _ in self._methods.values()]

    def initialize(self, name, **options):
        if name not in self._methods:
            raise RegistryError('unknown controller: ' + name)
        spec, factory = self._methods[name]
        backend, kind = factory(**options)
        return ControllerHandle(spec, backend, kind)


class ControllerHandle:
    def __init__(self, spec, backend, kind):
        self.spec, self.backend, self.kind = spec, backend, kind
        self.stopped = False
        self.last_time_s = None
        self.source_sha256 = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()

    def snapshot(self):
        return {'schema':'contact-method-state-v1', 'method':asdict(self.spec),
                'adapter_sha256':self.source_sha256, 'stopped':self.stopped,
                'last_time_s':self.last_time_s, 'backend':copy.deepcopy(self.backend.snapshot())}

    def restore(self, state):
        if (state.get('schema') != 'contact-method-state-v1'
            or state.get('method') != asdict(self.spec)
            or state.get('adapter_sha256') != self.source_sha256
            or not isinstance(state.get('stopped'),bool)):
            raise RegistryError('controller snapshot identity differs')
        stamp = state.get('last_time_s')
        if stamp is not None and (isinstance(stamp,bool) or not math.isfinite(stamp) or stamp < 0):
            raise RegistryError('invalid snapshot clock')
        self.backend.restore(copy.deepcopy(state['backend']))
        self.last_time_s, self.stopped = stamp, state['stopped']

    def stop(self):
        self.stopped = True
        stop = getattr(self.backend, 'stop', None)
        if callable(stop):
            stop('controller_handle_stop')

    def close(self):
        self.stop()
        close = getattr(self.backend, 'close', None)
        if close is not None:
            close()

    def step(self, observation, reference, dt):
        if self.stopped:
            raise RegistryError('controller is stopped')
        from contact_semantics import finite_vector3, finite_vector6
        from contact_yield_math import require_rotation, so3_log
        elapsed, stamp = float(dt), float(observation['time_s'])
        age = float(observation['state_age_s'])
        if (not all(math.isfinite(x) for x in (elapsed,stamp,age))
            or not 0 < elapsed <= .004 or stamp < 0 or not 0 <= age < .08):
            raise RegistryError('invalid observation time, dt, or age')
        if self.last_time_s is not None and not math.isclose(stamp-self.last_time_s,elapsed,abs_tol=1e-10,rel_tol=0):
            raise RegistryError('observation clock must advance by actual dt')
        for key in ('true_normal','true_outward_normal_base','true_inward_normal_base','gap_m','surface_height_m','evaluator'):
            if key in observation:
                raise RegistryError('controller observation contains evaluator truth: ' + key)
        if np.any(finite_vector3(observation.get('software_injection_base_n',(0.,0.,0.)), 'injection')):
            raise RegistryError('common method interface requires measured force; injection must be explicitly composed upstream')
        rotation = require_rotation(observation['rotation'],'observation rotation')
        position = finite_vector3(observation['position_m'],'position')
        force = finite_vector3(observation['raw_force_base_n'],'force')
        torque = finite_vector3(observation['raw_torque_base_nm'],'torque')
        if np.linalg.norm(force) >= 20. or np.linalg.norm(torque) >= 2.:
            raise RegistryError('shared wrench guard exceeded (20 N / 2 Nm)')
        target_force = float(reference['reference_force_n'])
        if not math.isfinite(target_force):
            raise RegistryError('finite target force required')
        joints = finite_vector6(observation['joint_position_rad'],'joints')
        lower = finite_vector6(observation['joint_velocity_lower'],'lower')
        upper = finite_vector6(observation['joint_velocity_upper'],'upper')
        if np.any(lower > upper):
            raise RegistryError('inverted velocity bounds')
        jac = np.asarray(observation['jacobian'],dtype=float)
        if jac.shape != (6,6) or not np.all(np.isfinite(jac)):
            raise RegistryError('finite 6x6 Jacobian required')
        velocity = finite_vector3(observation['linear_velocity_base_m_s'],'linear velocity')
        angular_velocity = finite_vector3(observation['angular_velocity_base_rad_s'],'angular velocity')
        before = self.snapshot()
        try:
            if self.kind == 'yield':
                result = self.backend.step(observation,{**reference,'force_n':target_force},elapsed)
            elif self.kind == 'tase':
                # Rotate the shared base wrench back to TCP so both TASE
                # solvers execute the same configured force-filter path.
                measured = {'joint_position_rad':joints,
                    'tcp_pose_base':np.concatenate((position,so3_log(rotation))),
                    'tcp_velocity_base':np.concatenate((velocity,angular_velocity)),
                    'wrench_tcp':np.concatenate((rotation.T@force,rotation.T@torque)),
                    'jacobian_base':jac,
                    'constraints':{'joint_velocity_lower':lower,'joint_velocity_upper':upper}}
                target = {'x_pd_base':reference['position_m'],
                    'xdot_pd_base':reference['velocity_m_s'],
                    'force_target_n':reference['reference_force_n']}
                result = asdict(self.backend.step(measured,target,elapsed))
            else:
                result = self.backend.step(observation,reference,elapsed)
            qdot = finite_vector6(result['qdot_rad_s'],'result qdot')
            # Validate, never clip: clipping would invalidate the backend's
            # task equality and conceal a failed candidate.
            from contact_qp import QP_BOUND_VALIDATION_TOLERANCE
            tolerance = QP_BOUND_VALIDATION_TOLERANCE
            if np.any(qdot < lower-tolerance) or np.any(qdot > upper+tolerance):
                raise RegistryError('controller output violates shared joint velocity bounds')
            self.last_time_s = stamp
            return {**result,'registered_method':asdict(self.spec), 'hardware_evidence':False}
        except Exception:
            self.restore(before)
            raise


def _yield_factory(name):
    def create(**options):
        from contact_yield_controller import YieldController
        return YieldController(method=name,**options), 'yield'
    return create


def _tase_factory(solver, variant):
    def create(config=None, qp_library=None, outer_config=None):
        from contact_yield_method_registry import resolve_offline_method
        from tase_method_adapters import create_tase_offline_adapter

        method_name = 'TASE_RNN' if solver == 'rnn' and variant == 'printed_eq23_plus' else (
            'TASE_RNN_MATURE_MINUS' if solver == 'rnn' else 'TASE_QP'
        )
        try:
            resolve_offline_method(method_name)
            options = {} if config is None else dict(config)
            if 'lambda_variant' in options and options.pop('lambda_variant') != variant:
                raise RegistryError('choose the explicitly named TASE sign variant')
            return create_tase_offline_adapter(
                method_name=method_name,
                solver_name=solver,
                variant=variant,
                config=options,
                qp_library=qp_library,
                outer_config=outer_config,
            ), 'tase'
        except (ImportError, ValueError) as exc:
            raise RegistryError(f'{method_name} offline adapter is unavailable: {exc}') from exc
    return create


def default_registry():
    registry = MethodRegistry()
    for name,role in [('SFC','baseline'),('SFC_RADIAL','geometry_ablation'),('DSFC','proposal'),('MSFC','proposal')]:
        registry.register(MethodSpec(name,role,'YieldController'),_yield_factory(name))
    for name,solver,variant,role in [
        ('TASE_RNN','rnn','printed_eq23_plus','printed_sign_eq23_plus_offline_baseline_with_explicit_outer_adaptation'),
        ('TASE_RNN_MATURE_MINUS','rnn','mature_minus','explicit_lambda_minus_adaptation_reference'),
        ('TASE_QP','qp','matched_outer_qp','matched_outer_solver_ablation')]:
        registry.register(MethodSpec(name,role,'TaseOfflineMethodAdapter'),_tase_factory(solver,variant))
    return registry
