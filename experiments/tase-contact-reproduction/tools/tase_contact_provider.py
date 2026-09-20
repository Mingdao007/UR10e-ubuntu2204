"""Mature local TASE outer/RNN adapter for the existing contact writer.

No endpoints. Keeps the historical local RNN sign/discretization explicitly;
this is not the printed-plus offline variant. The shared writer still owns
raw-wrench limits, command slew/Jacobian checks and physical stopping.
"""
from __future__ import annotations

import copy
from dataclasses import asdict, dataclass
from pathlib import Path
import hashlib
import json
import math
import numpy as np

from contact_benchmark_provider import ContactCommandProvider, ContactReadinessObserver, ContactForceObservation
from contact_benchmark_protocol import SensorFreshnessTracker
from contact_benchmark_runtime import validate_measured_observation
from contact_yield_protocol import Task, PERIOD_S
from contact_yield_task_frame import require_figure8_home
from step5c_calibrated_kinematics_audit import rotvec_to_matrix
from step5d_autotune_v4_r004.calibrated_runtime import V4CalibratedRuntime


@dataclass(frozen=True)
class TaseModelBinding:
    raw: dict
    model_hashes: dict
    schema: str = 'tase-mature-current-model-v1'


def current_model_binding():
    """Bind the retained calibrated platform, without rewriting V4 releases."""
    from step5c_calibrated_kinematics_audit import DEFAULT_XACRO_PATH, DEFAULT_CALIBRATION_YAML
    from yield_native_route import measure_actual_model, _require_desired_model
    root = Path(__file__).resolve().parents[1]
    expected = json.loads((root/'config/yield_native_route_v1.json').read_text())
    _require_desired_model(expected, measure_actual_model())
    paths = {'ur_xacro': DEFAULT_XACRO_PATH, 'calibration_yaml': DEFAULT_CALIBRATION_YAML,
        'strict_rnn': root/'tools/step5c_strict_rnn.py',
        'paper_outer': root/'tools/step5d_paper_outer_loop.py',
        'calibrated_runtime': root/'tools/step5d_autotune_v4_r004/calibrated_runtime.py'}
    hashes = {name: hashlib.sha256(Path(path).read_bytes()).hexdigest() for name,path in paths.items()}
    return TaseModelBinding({'robot_model_binding': {
        name+'_path': str(Path(path).resolve()) for name,path in paths.items()}}, hashes)


class TaseContactProvider(ContactCommandProvider):
    """Complete baseline/entry/figure-eight provider over one calibrated RNN."""
    def __init__(self, *, contract, candidate, motion_profile, home_pose, solver_profile):
        pose = np.asarray(home_pose, dtype=float)
        self.basis = require_figure8_home(pose)
        self.anchor = pose[:3].copy()
        self.task = Task()
        self.phase = None
        self.reference_phase = 'baseline'
        self.last_sample_s = self.last_controller_timestamp = self.last_sensor_timestamp = None
        self.freshness = SensorFreshnessTracker()
        self.last_result = self.last_pause = None
        self.command_history = None
        self.lifecycle_observer = ContactReadinessObserver(candidate.normal_filter_tau_s)
        self.runtime = V4CalibratedRuntime(
            contract, candidate, motion_profile=motion_profile,
            solver_profile=solver_profile, path_reference=self.reference,
            target_rotvec=pose[3:], force_normal_velocity_limit_m_s=.003)
        self.contract = contract
        self.model_hashes = dict(self.runtime.model_hashes)
        self.solver_profile = solver_profile
        self.command_timeline = []

    def reference(self, stage_id, pose_xy, elapsed_s):
        if stage_id != 'step5d_strict_rnn_autotune_v1':
            raise ValueError('TASE path stage differs')
        t = float(elapsed_s)
        if self.reference_phase == 'entry':
            ref = self.task.entry_reference(t)
        else:
            if not 0 <= t <= PERIOD_S + .08:
                raise ValueError('TASE figure-eight clock outside full period')
            ref = self.task.reference(t)
        position = self.anchor + self.basis @ np.asarray(ref['position_m'])
        velocity = self.basis @ np.asarray(ref['velocity_m_s'])
        return {'desired_xy': tuple(position[:2]),
                'desired_velocity_xy': tuple(velocity[:2]),
                'path_error_xy': tuple(position[:2]-np.asarray(pose_xy))}

    @property
    def command_normal_base(self):
        # Same base reaction normal as the mature local TASE force projection.
        return (0., 0., 1.)

    def snapshot(self):
        return copy.deepcopy({
            'runtime': self.runtime.dynamic_state_snapshot(), 'phase': self.phase,
            'reference_phase': self.reference_phase, 'last_sample_s': self.last_sample_s,
            'last_controller_timestamp': self.last_controller_timestamp,
            'last_sensor_timestamp': self.last_sensor_timestamp,
            'last_result': self.last_result,
            'last_pause': self.last_pause, 'command_history': self.command_history,
            'filter': {'filtered_normal_n': self.lifecycle_observer.filtered_normal_n,
                'last_log': None if self.lifecycle_observer.last_log is None else asdict(self.lifecycle_observer.last_log)}})

    def restore(self, state):
        state = copy.deepcopy(state)
        self.runtime.restore_dynamic_state(state.pop('runtime'))
        observation = state.pop('filter')
        self.lifecycle_observer.filtered_normal_n = observation['filtered_normal_n']
        self.lifecycle_observer.last_log = (None if observation['last_log'] is None
            else ContactForceObservation(**observation['last_log']))
        for name, value in state.items():
            setattr(self, name, value)

    def _observe(self, output, sensor, now, dt, *, maximum=.004):
        if sensor.stop_request or not output.safety_normal:
            raise ValueError('TASE observation requests stop')
        if self.last_sample_s is not None and not math.isclose(now-self.last_sample_s, dt, abs_tol=1e-7, rel_tol=0):
            raise ValueError('TASE owner clock discontinuity')
        robot = {'observed_at_s': output.received_monotonic_s, 'timestamp': output.timestamp,
            'safety_mode': output.safety_mode, 'tcp_offset': output.tcp_offset_m_rad,
            'payload': output.payload_kg, 'payload_cog': output.payload_cog_m,
            'actual_q': output.q_rad, 'actual_qd': output.qd_rad_s,
            'actual_TCP_pose': output.tcp_pose_m_rad, 'actual_TCP_speed': output.tcp_speed_m_s_rad_s}
        obs = validate_measured_observation(robot=robot, wrench_tcp=sensor.wrench,
            sensor_observed_at_s=sensor.observed_at_s, sample_time_s=now,
            last_sample_s=self.last_sample_s, last_controller_timestamp=self.last_controller_timestamp,
            last_sensor_timestamp=self.last_sensor_timestamp, freshness=self.freshness,
            model=self.runtime.model, max_dt_s=maximum, strict_dt_upper=maximum>.004)
        return obs

    def _commit_clock(self, obs):
        self.last_sample_s = obs['now']
        self.last_controller_timestamp = obs['controller_t']
        self.last_sensor_timestamp = obs['sensor_t']

    def bind_command_history(self, previous_qdot, slew_rad_s2):
        self.command_history = {'previous_qdot': tuple(previous_qdot), 'slew_rad_s2': float(slew_rad_s2)}

    def pause(self, *, output, sensor, monotonic_s, actual_dt_s, reason):
        obs = self._observe(output, sensor, monotonic_s, actual_dt_s)
        if self.phase in ('entry', 'path'):
            raise ValueError('TASE cannot pause its active task clock')
        self._commit_clock(obs)
        self.last_pause = {'reason': reason, 'sample_time_s': monotonic_s}
        return self.last_pause

    def hold_pre_path_late_cycle(self, *, output, sensor, monotonic_s, actual_dt_s, reason):
        if self.phase in ('entry', 'path') or not output.stationary:
            raise ValueError('TASE late hold requires stationary pre-PATH state')
        obs = self._observe(output, sensor, monotonic_s, actual_dt_s, maximum=.020)
        self._commit_clock(obs)
        self.last_pause = {'reason': reason, 'sample_time_s': monotonic_s,
            'actual_dt_s': actual_dt_s, 'native_law_dt_s': .002,
            'filtered_normal_n': self.lifecycle_observer.filtered_normal_n
                if self.lifecycle_observer.filtered_normal_n is not None else sensor.filtered_normal_n}
        return self.last_pause

    def execution_path_errors(self, *, actual_tcp_pose, path_time_s, motion_kp):
        self.reference_phase = 'entry' if path_time_s < 1. else 'path'
        t = path_time_s if path_time_s < 1. else path_time_s-1.
        return self.runtime.path_errors(actual_tcp_pose=actual_tcp_pose, path_time_s=t, motion_kp=motion_kp)

    path_errors = execution_path_errors

    def execution_command(self, **kwargs):
        return self.command(**kwargs)

    def command(self, *, output, sensor, monotonic_s, actual_dt_s, mode,
                internal_setpoint_n, path_time_s=None):
        if mode not in ('baseline', 'path'):
            raise ValueError('unknown TASE phase')
        checkpoint = self.snapshot()
        try:
            obs = self._observe(output, sensor, monotonic_s, actual_dt_s)
            elapsed = 0. if path_time_s is None else float(path_time_s)
            phase = 'baseline' if mode == 'baseline' else ('entry' if elapsed < 1. else 'path')
            t = elapsed-1. if phase == 'path' else elapsed
            self.reference_phase = phase
            if phase in ('entry', 'path'):
                ref = self.reference('step5d_strict_rnn_autotune_v1', output.tcp_pose_m_rad[:2], t)
                error_base = np.array((*ref['path_error_xy'], 0.))
                error = (self.basis.T @ error_base)[:2]
                # Independent shared path stop, including observation-age margin.
                axes = np.asarray((.025, .015)) - .015*obs['age']
                if np.any(axes <= 0) or np.sum((error/axes)**2) >= 1:
                    raise ValueError('TASE path error exceeds current geometric boundary')
            filtered = self.lifecycle_observer.filtered_normal_n
            if filtered is None:
                filtered = sensor.filtered_normal_n
            twist = self.runtime.desired_twist(actual_tcp_pose=output.tcp_pose_m_rad,
                actual_tcp_speed=output.tcp_speed_m_s_rad_s, force_tcp_n=sensor.wrench[:3],
                filtered_normal_n=filtered, internal_setpoint_n=internal_setpoint_n,
                actual_dt_s=actual_dt_s, mode=mode, path_time_s=t)
            command = self.runtime.command(actual_q=output.q_rad, actual_qd=output.qd_rad_s,
                actual_tcp_pose=output.tcp_pose_m_rad, desired_twist=twist,
                actual_dt_s=actual_dt_s, mode=mode, path_time_s=t)
            self._commit_clock(obs)
            self.phase = phase
            self.last_result = {'phase': phase, 'sample_time_s': monotonic_s,
                'qdot_rad_s': command.qdot, 'filtered_normal_n': filtered,
                'entry_time_s': t if phase == 'entry' else None,
                'formal_time_s': t if phase == 'path' else None,
                'actual_dt_s': actual_dt_s, 'solver': copy.deepcopy(self.runtime.last_solver_diagnostics),
                'implementation': 'mature_local_tase_rnn'}
            return command
        except BaseException:
            self.restore(checkpoint)
            raise

    def close(self):
        self.runtime.solver.freeze()
