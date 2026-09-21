"""Mature local TASE outer/RNN adapter for the existing contact writer.

No endpoints. Keeps the historical local RNN sign/discretization explicitly;
this is not the printed-plus offline variant. The shared writer still owns
raw-wrench limits, command slew/Jacobian checks and physical stopping.
"""
from __future__ import annotations

import copy
from dataclasses import asdict, dataclass, replace
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
from step5d_autotune_v4_r004.motion_profile import same_direction_qdot_rescale
from step5d_autotune_v4_r004.timing import MAX_FRESH_GAP_S
from step5d_paper_outer_loop import Step5dOuterLoopConfig


# A live safety transition, not a change to the paper gains.  The canonical
# The task target is 5 N; the 0.75 N margin gives the mature RNN time to shed a
# rising load before the shared 7 N readiness ceiling.  This is one hysteretic
# transition per force-rise episode; it does not change any hard force, timing,
# joint-velocity, slew, or readiness envelope.
TASE_FORCE_PREEMPT_THRESHOLD_N = 5.75
TASE_FORCE_PREEMPT_REARM_N = 5.0
# An earlier, rate-triggered guard limits only additional inward baseline
# realization while a measured force rise is already underway.
TASE_FORCE_RISE_GUARD_THRESHOLD_N = 4.0
TASE_FORCE_RISE_GUARD_DELTA_N = 0.5
TASE_FORCE_RISE_INWARD_CAP_M_S = 0.0005
# Keep a strict numerical margin below the shared host slew envelope.  The
# qualification layer checks the same limit with a strict ``>`` comparison;
# using the exact boundary can differ by one floating-point ulp after the
# provider's scalar rescale, turning a valid emergency transition into a
# false interface failure.
TASE_HOST_SLEW_NUMERIC_MARGIN = 1e-9
TASE_BASELINE_TANGENTIAL_TOLERANCE_M_S = 2e-6
TASE_BASELINE_ANGULAR_TOLERANCE_RAD_S = 2e-6
# Fixed contact-acquisition primitive.  This is deliberately outside the
# tunable TASE outer loop: the qualification ramp owns only a 1-to-5 N
# setpoint, while the primitive realizes a pure normal velocity capped at
# 0.5 mm/s and leaves the RNN/outer-loop state frozen until PATH.
# The contact-acquisition primitive is deliberately slower than the shared
# Cartesian safety ceiling.  The previous 0.5 mm/s realization repeatedly
# reached the ceiling before the readiness window could settle on the current
# canonical Figure-eight Home.  This bounded profile changes only the approach
# command; all force, timing, joint, slew, and readiness gates remain owned by
# their existing layers.
TASE_BASELINE_FORCE_P_GAIN_M_S_PER_N = 0.0001767766953
TASE_BASELINE_NORMAL_SPEED_CAP_M_S = 0.00025

TASE_PARAMETER_SCHEMA = 'tase.outer-parameters-v1'
TASE_OUTER_SEARCH_BOUNDS = {
    'Md_scalar': (12.0 * 2.0 ** -0.5, 12.0 * 2.0 ** 0.5),
    'Bd_scalar': (550.0 * 2.0 ** -0.5, 550.0 * 2.0 ** 0.5),
}


# Parameters copied from config/step5c_tase_paper_truth.json (Eq. 16/17).
# Runtime-only setpoint, actual dt, integral authority and normal-velocity
# safety limits are applied by V4CalibratedRuntime on each live tick.
TASE_PAPER_OUTER_CONFIG = Step5dOuterLoopConfig(
    kp=4.0,
    ko=5.0,
    kf=1.0,
    Md_scalar=12.0,
    Bd_scalar=550.0,
    force_target_n=5.0,
    force_integral_limit_n_s=5.0,
    delay_T_s=None,
    force_sign_convention='step5_step6_positive_normal_load',
)

TASE_PAPER_OUTER_BINDING = {
    'source': 'config/step5c_tase_paper_truth.json',
    'equations': ['Eq16', 'Eq17'],
    'paper_parameters': {
        'kp': 4.0,
        'ko': 5.0,
        'kf': 1.0,
        'Md_scalar': 12.0,
        'Bd_scalar': 550.0,
    },
        'live_adaptations': [
            'force target from internal setpoint',
            'delay T from actual dt',
            'shared live integral and normal-velocity safety limits',
            'one-sided raw-normal rise envelope for live force protection',
            'hysteretic RNN warm-start on each measured force-rise episode above threshold_n',
        ],
    'live_force_measurement_envelope': {
        'schema': 'tase-live-force-rise-envelope-v1',
        'formula': 'control_normal=max(canonical_filtered_normal, measured_normal_load, measured_force_norm)',
        'purpose': 'prevent filter lag or tangential-load growth from commanding further inward motion near the shared force limit',
        'evidence_field': 'control_normal_n',
    },
    'fixed_baseline_contact_primitive': {
        'schema': 'tase-fixed-baseline-normal-v1',
        'force_error_gain_m_s_per_n': TASE_BASELINE_FORCE_P_GAIN_M_S_PER_N,
        'normal_speed_cap_m_s': TASE_BASELINE_NORMAL_SPEED_CAP_M_S,
        'profile_id': 'tase-fixed-baseline-normal-damped-v1',
        'previous_profile': {
            'force_error_gain_m_s_per_n': 0.0003535533906,
            'normal_speed_cap_m_s': 0.0005,
        },
        'xy_velocity_m_s': [0.0, 0.0],
        'angular_velocity_rad_s': [0.0, 0.0, 0.0],
        'rnn_state': 'frozen_until_path',
        'outer_loop_state': 'frozen_until_path',
    },
    'force_preemptive_rnn_warm_start': {
        'schema': 'tase-live-force-preempt-warm-start-v2',
        'threshold_n': TASE_FORCE_PREEMPT_THRESHOLD_N,
        'rearm_threshold_n': TASE_FORCE_PREEMPT_REARM_N,
        'condition': 'measured_force_norm crosses threshold_n after falling below rearm_threshold_n; one warm-start per force-rise episode',
        'purpose': 'remove strict-RNN state lag at each rising-load episode without raising the raw guard',
        'evidence_field': 'force_preempt_warm_start',
    },
    'force_rise_inward_cap': {
        'threshold_n': TASE_FORCE_RISE_GUARD_THRESHOLD_N,
        'delta_n': TASE_FORCE_RISE_GUARD_DELTA_N,
        'inward_cap_m_s': TASE_FORCE_RISE_INWARD_CAP_M_S,
        'purpose': 'limit additional inward baseline realization during a sharp measured force rise before the main preempt threshold',
        'evidence_field': 'force_rise_guard,force_rise_inward_cap_applied',
    },
}


def load_tase_outer_config(path=None):
    """Load only the bounded research parameters for one frozen live run."""
    if path is None:
        return TASE_PAPER_OUTER_CONFIG, {
            'schema': TASE_PARAMETER_SCHEMA,
            'source': 'paper-default',
            'Md_scalar': TASE_PAPER_OUTER_CONFIG.Md_scalar,
            'Bd_scalar': TASE_PAPER_OUTER_CONFIG.Bd_scalar,
        }
    candidate_path = Path(path).expanduser().resolve()
    if candidate_path.is_symlink() or not candidate_path.is_file():
        raise ValueError(f'TASE parameter file is not a regular file: {candidate_path}')
    try:
        payload = json.loads(candidate_path.read_text(encoding='utf-8'))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f'TASE parameter file is unreadable: {candidate_path}') from exc
    if not isinstance(payload, dict) or payload.get('schema') != TASE_PARAMETER_SCHEMA:
        raise ValueError('TASE parameter schema differs')
    try:
        md = float(payload['Md_scalar'])
        bd = float(payload['Bd_scalar'])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError('TASE parameter file requires Md_scalar and Bd_scalar') from exc
    if not math.isfinite(md) or not math.isfinite(bd):
        raise ValueError('TASE Md/Bd must be finite')
    for name, value in (('Md_scalar', md), ('Bd_scalar', bd)):
        lo, hi = TASE_OUTER_SEARCH_BOUNDS[name]
        if not lo <= value <= hi:
            raise ValueError(f'TASE {name} is outside the bounded autotuner search box')
    frozen = payload.get('frozen')
    if frozen is not None and frozen != {
        'kp': 4.0, 'ko': 5.0, 'kf': 1.0, 'force_target_n': 5.0,
        'force_integral_limit_n_s': 5.0,
        'force_sign_convention': 'step5_step6_positive_normal_load',
    }:
        raise ValueError('TASE frozen outer-loop fields differ')
    binding = dict(payload)
    binding.update({'schema': TASE_PARAMETER_SCHEMA, 'source': str(candidate_path),
                    'Md_scalar': md, 'Bd_scalar': bd})
    return replace(TASE_PAPER_OUTER_CONFIG, Md_scalar=md, Bd_scalar=bd), binding


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
        'calibrated_runtime': root/'tools/step5d_autotune_v4_r004/calibrated_runtime.py',
        # The live adapter selects the paper-anchored outer loop here; bind
        # this seam too so a run cannot claim the old provider identity.
        'tase_provider': Path(__file__).resolve()}
    hashes = {name: hashlib.sha256(Path(path).read_bytes()).hexdigest() for name,path in paths.items()}
    return TaseModelBinding({'robot_model_binding': {
        name+'_path': str(Path(path).resolve()) for name,path in paths.items()}}, hashes)


class TaseContactProvider(ContactCommandProvider):
    """Complete baseline/entry/figure-eight provider over one calibrated RNN."""

    # The provider owns the complete TASE outer loop, but its final command
    # still passes through the canonical V4 J*qdot envelope. The qualification
    # seam applies a same-direction bounded projection for this explicit opt-in;
    # it never raises a safety cap or changes the RNN state law.
    allow_bounded_gate_projection = True

    def __init__(self, *, contract, candidate, motion_profile, home_pose,
                 solver_profile, outer_loop_config=None, parameter_binding=None):
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
        self.force_preempt_warm_started = False
        self.force_preempt_armed = True
        self.force_preempt_episode = 0
        self.force_preempt_direction_retry_count = 0
        self.last_measured_force_norm = None
        self.lifecycle_observer = ContactReadinessObserver(candidate.normal_filter_tau_s,
            max_dt_s=MAX_FRESH_GAP_S, strict_dt_upper=True)
        self.runtime = V4CalibratedRuntime(
            contract, candidate, motion_profile=motion_profile,
            solver_profile=solver_profile, path_reference=self.reference,
            target_rotvec=pose[3:], force_normal_velocity_limit_m_s=.003,
            outer_loop_config=(TASE_PAPER_OUTER_CONFIG if outer_loop_config is None
                               else outer_loop_config))
        self.contract = contract
        self.model_hashes = dict(self.runtime.model_hashes)
        self.solver_profile = solver_profile
        self.command_timeline = []
        self.parameter_binding = (dict(parameter_binding) if parameter_binding is not None
                                  else {'schema': TASE_PARAMETER_SCHEMA,
                                        'source': 'paper-default',
                                        'Md_scalar': self.runtime.outer_loop_config.Md_scalar,
                                        'Bd_scalar': self.runtime.outer_loop_config.Bd_scalar})

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

    def formal_reference(self, time_s):
        """World reference for evidence joined to an actually consumed packet."""
        ref = self.task.reference(time_s)
        return {'position_m': self.anchor + self.basis @ np.asarray(ref['position_m']),
                'velocity_m_s': self.basis @ np.asarray(ref['velocity_m_s'])}

    def snapshot(self):
        return copy.deepcopy({
            'runtime': self.runtime.dynamic_state_snapshot(), 'phase': self.phase,
            'reference_phase': self.reference_phase, 'last_sample_s': self.last_sample_s,
            'last_controller_timestamp': self.last_controller_timestamp,
            'last_sensor_timestamp': self.last_sensor_timestamp,
            'last_result': self.last_result,
            'last_pause': self.last_pause, 'command_history': self.command_history,
            'force_preempt_warm_started': self.force_preempt_warm_started,
            'force_preempt_armed': self.force_preempt_armed,
            'force_preempt_episode': self.force_preempt_episode,
            'force_preempt_direction_retry_count': self.force_preempt_direction_retry_count,
            'last_measured_force_norm': self.last_measured_force_norm,
            'parameter_binding': copy.deepcopy(self.parameter_binding),
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

    def _observe(self, output, sensor, now, dt, *, maximum=MAX_FRESH_GAP_S):
        if not math.isfinite(dt) or not 0 < dt < maximum:
            raise ValueError('TASE actual interval outside mature timing bound')
        if sensor.stop_request or not output.safety_normal:
            raise ValueError('TASE observation requests stop')
        if self.last_sample_s is not None and not math.isclose(now-self.last_sample_s, dt, abs_tol=1e-7, rel_tol=0):
            raise ValueError('TASE owner clock discontinuity')
        robot = {'observed_at_s': output.received_monotonic_s, 'timestamp': output.timestamp,
            'safety_mode': output.safety_mode, 'tcp_offset': output.tcp_offset_m_rad,
            'payload': output.payload_kg, 'payload_cog': output.payload_cog_m,
            'actual_q': output.q_rad, 'actual_qd': output.qd_rad_s,
            'actual_TCP_pose': output.tcp_pose_m_rad, 'actual_TCP_speed': output.tcp_speed_m_s_rad_s}
        obs = validate_measured_observation(
            robot=robot,
            wrench_tcp=sensor.wrench,
            # ``sensor.wrench`` is baseline-corrected and remains the control
            # signal.  The hard raw-sensor envelope must inspect the native
            # Kunwei sample when available; manually constructed seam fixtures
            # without that field retain the fail-closed legacy fallback.
            guard_wrench_tcp=sensor.raw_wrench,
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

    def record_gate_projection(self, *, scale, original_gate):
        """Attach final-envelope projection evidence to the accepted tick."""

        if not isinstance(self.last_result, dict):
            raise ValueError('TASE gate projection requires a committed command result')
        self.last_result['gate_projection_scale'] = float(scale)
        self.last_result['gate_projection_applied'] = bool(float(scale) < 1.0)
        self.last_result['gate_projection_reason'] = str(original_gate.reason)
        self.last_result['gate_projection_original_metrics'] = {
            'total_linear_m_s': float(original_gate.total_linear_m_s),
            'normal_m_s': float(original_gate.normal_m_s),
            'tangential_m_s': float(original_gate.tangential_m_s),
            'angular_rad_s': float(original_gate.angular_rad_s),
        }

    def record_final_qdot(self, qdot):
        """Bind the final projected Jqdot to the applied provider command."""

        if not isinstance(self.last_result, dict):
            raise ValueError('TASE final qdot requires a committed command result')
        values = tuple(float(value) for value in qdot)
        if len(values) != 6 or not all(math.isfinite(value) for value in values):
            raise ValueError('TASE final qdot is invalid')
        self.last_result.setdefault(
            'provider_qdot_rad_s', tuple(self.last_result.get('qdot_rad_s', ()))
        )
        self.last_result['qdot_rad_s'] = values

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
            if not math.isfinite(float(filtered)) or not math.isfinite(
                float(sensor.normal_load_n)
            ):
                raise ValueError('TASE force measurement is nonfinite')
            # The canonical V4 filter remains the qualification/evidence
            # signal.  The TASE force loop additionally gets a one-sided
            # measured-load envelope: when either the normal load or the full
            # corrected force norm rises faster than the readiness filter, it
            # must not continue commanding into the contact until the filter
            # catches up.  Using the norm here is a conservative live safety
            # adaptation; the raw wrench and canonical filtered channels are
            # still retained separately for evidence and hard stopping.
            control_normal = max(
                float(filtered),
                float(sensor.normal_load_n),
                float(sensor.force_norm_n),
            )
            measured_force_norm = float(sensor.force_norm_n)
            previous_measured_force_norm = self.last_measured_force_norm
            force_rise_guard = bool(
                previous_measured_force_norm is not None
                and measured_force_norm >= TASE_FORCE_RISE_GUARD_THRESHOLD_N
                and measured_force_norm - float(previous_measured_force_norm)
                >= TASE_FORCE_RISE_GUARD_DELTA_N
            )
            force_preempt_warm_start = False
            force_preempt_direction_retry = False
            force_preempt_approach_before_retry = None
            baseline_primitive_speed_m_s = 0.0
            # Hysteresis is deliberately based on the measured full-force
            # channel.  The canonical filtered channel remains the evidence
            # signal, while this conservative transition prevents an already
            # rising load from being hidden by filter lag.  A low-force
            # observation re-arms exactly one warm-start for the next episode.
            if measured_force_norm < TASE_FORCE_PREEMPT_REARM_N:
                self.force_preempt_armed = True
            # The early rate guard is deliberately projection-only.  It must
            # not repeatedly reset the recurrent state on noisy rise ticks;
            # the hysteretic main threshold remains the sole warm-start/retry
            # transition.  This keeps the safety response bounded without
            # turning a force transient into controller-state chatter.
            if mode == 'baseline':
                # The qualification contact stage is intentionally independent
                # of the tuned TASE law.  A one-dimensional fixed primitive
                # follows the canonical 1-to-5 N ramp and uses the conservative
                # measured envelope, then realizes pure base-Z motion through
                # the calibrated Jacobian without touching RNN state.
                baseline_error = float(internal_setpoint_n) - control_normal
                baseline_primitive_speed_m_s = float(np.clip(
                    TASE_BASELINE_FORCE_P_GAIN_M_S_PER_N * baseline_error,
                    -TASE_BASELINE_NORMAL_SPEED_CAP_M_S,
                    TASE_BASELINE_NORMAL_SPEED_CAP_M_S,
                ))
                twist = (0.0, 0.0, -baseline_primitive_speed_m_s, 0.0, 0.0, 0.0)
                command = self.runtime.pure_normal_command(
                    actual_q=output.q_rad,
                    normal_speed_m_s=baseline_primitive_speed_m_s,
                    actual_dt_s=actual_dt_s,
                    path_time_s=t,
                )
            else:
                twist = self.runtime.desired_twist(actual_tcp_pose=output.tcp_pose_m_rad,
                    actual_tcp_speed=output.tcp_speed_m_s_rad_s, force_tcp_n=sensor.wrench[:3],
                    filtered_normal_n=control_normal, internal_setpoint_n=internal_setpoint_n,
                    actual_dt_s=actual_dt_s, mode=mode, path_time_s=t)
                if (
                    self.force_preempt_armed
                    and measured_force_norm >= TASE_FORCE_PREEMPT_THRESHOLD_N
                ):
                    force_preempt_warm_start = bool(self.runtime.warm_start_for_twist(
                        actual_q=output.q_rad,
                        desired_twist=twist,
                        mode=mode,
                    ))
                    if force_preempt_warm_start:
                        self.force_preempt_warm_started = True
                        self.force_preempt_armed = False
                        self.force_preempt_episode += 1
                command = self.runtime.command(actual_q=output.q_rad, actual_qd=output.qd_rad_s,
                    actual_tcp_pose=output.tcp_pose_m_rad, desired_twist=twist,
                    actual_dt_s=actual_dt_s, mode=mode, path_time_s=t)
                # A warm-start at the force threshold fixes the initial RNN
                # transient, but the recurrent state can drift back into contact
                # while a high load persists.  Inspect the actual solved J*qdot;
                # if it is still pressing during a high-force observation, reset
                # once for this tick and solve the same desired twist again.
                if measured_force_norm >= TASE_FORCE_PREEMPT_THRESHOLD_N:
                    preliminary_twist = np.asarray(command.jacobian_6x6, dtype=float) @ np.asarray(
                        command.qdot, dtype=float
                    )
                    force_preempt_approach_before_retry = float(-preliminary_twist[2])
                    if force_preempt_approach_before_retry > 0.0:
                        direction_warm_start = bool(self.runtime.warm_start_for_twist(
                            actual_q=output.q_rad,
                            desired_twist=twist,
                            mode=mode,
                        ))
                        if direction_warm_start:
                            command = self.runtime.command(
                                actual_q=output.q_rad,
                                actual_qd=output.qd_rad_s,
                                actual_tcp_pose=output.tcp_pose_m_rad,
                                desired_twist=twist,
                                actual_dt_s=actual_dt_s,
                                mode=mode,
                                path_time_s=t,
                            )
                            force_preempt_direction_retry = True
                            self.force_preempt_direction_retry_count += 1
                            self.force_preempt_warm_started = True
            # Baseline is a one-dimensional normal-force primitive.  The
            # strict RNN can carry a small tangential/angular residual even
            # when the requested baseline twist has those components set to
            # zero.  The canonical qualification path already holds a zero
            # packet in that case; apply the same state-preserving physical
            # hold here for the provider-owned TASE path.  The solver state
            # continues to advance and the residual is retained in evidence,
            # so this does not hide a controller failure or widen an envelope.
            baseline_residual_hold = False
            baseline_residual_tangential_m_s = 0.0
            baseline_residual_angular_rad_s = 0.0
            baseline_normal_projection_applied = False
            baseline_normal_projection_target_m_s = 0.0
            baseline_normal_projection_original_m_s = 0.0
            baseline_normal_direction_correction = False
            baseline_normal_unload_boost = False
            force_rise_inward_cap_applied = False
            force_rise_inward_original_m_s = 0.0
            force_rise_inward_target_m_s = 0.0
            if mode == 'baseline':
                baseline_twist = np.asarray(command.jacobian_6x6, dtype=float) @ np.asarray(
                    command.qdot, dtype=float
                )
                baseline_residual_tangential_m_s = float(np.linalg.norm(baseline_twist[:2]))
                baseline_residual_angular_rad_s = float(np.linalg.norm(baseline_twist[3:]))
                force_rise_unload_needed = bool(
                    measured_force_norm >= TASE_FORCE_PREEMPT_THRESHOLD_N
                    and float(twist[2]) > 0.0
                    and abs(float(twist[2])) > abs(float(baseline_twist[2])) + 1e-12
                )
                force_rise_inward_cap_needed = bool(
                    force_rise_guard
                    and -float(baseline_twist[2]) > TASE_FORCE_RISE_INWARD_CAP_M_S
                )
                if (
                    baseline_residual_tangential_m_s > TASE_BASELINE_TANGENTIAL_TOLERANCE_M_S
                    or baseline_residual_angular_rad_s > TASE_BASELINE_ANGULAR_TOLERANCE_RAD_S
                    or force_rise_unload_needed
                    or force_rise_inward_cap_needed
                ):
                    # The baseline contract permits only normal motion.  A
                    # zero-vector hold removed the residual motion but also
                    # discarded the normal component that maintains contact;
                    # on the bench that made the qualification alternate
                    # between over-load and contact loss.  Keep the bounded
                    # normal magnitude of the solved J*qdot, align its sign
                    # with the already-authorized outer-loop normal command,
                    # and realize it with the same calibrated Jacobian.  This is a
                    # projection at the provider boundary, not a gain or
                    # envelope change.  If the calibrated Jacobian cannot
                    # realize that bounded normal-only command, fail closed.
                    jacobian = np.asarray(command.jacobian_6x6, dtype=float)
                    if jacobian.shape != (6, 6) or not np.all(np.isfinite(jacobian)):
                        raise ValueError('baseline normal projection Jacobian is invalid')
                    normal_target = np.zeros(6, dtype=float)
                    original_normal = float(baseline_twist[2])
                    desired_normal = float(twist[2])
                    if abs(desired_normal) <= 1e-12:
                        normal_target[2] = 0.0
                    else:
                        # The mature RNN can carry a one-tick normal sign
                        # reversal while its state catches up.  Preserve its
                        # bounded magnitude but follow the already-authorized
                        # outer-loop normal direction; otherwise a baseline
                        # acquisition can repeatedly unload after contact.
                        target_magnitude = abs(original_normal)
                        if measured_force_norm >= TASE_FORCE_PREEMPT_THRESHOLD_N:
                            # The existing force-preempt threshold already
                            # marks a rising-load episode.  If the mature RNN
                            # has lagged to a tiny outward realization, use
                            # the outer-loop's bounded outward magnitude for
                            # this baseline safety transition; do not raise
                            # any task, joint, slew, or wrench envelope.
                            desired_magnitude = abs(desired_normal)
                            if desired_magnitude > target_magnitude + 1e-12:
                                target_magnitude = desired_magnitude
                                baseline_normal_unload_boost = True
                        if force_rise_inward_cap_needed:
                            target_magnitude = min(
                                target_magnitude,
                                TASE_FORCE_RISE_INWARD_CAP_M_S,
                            )
                            force_rise_inward_cap_applied = True
                            force_rise_inward_original_m_s = original_normal
                            force_rise_inward_target_m_s = -target_magnitude
                        normal_target[2] = math.copysign(target_magnitude, desired_normal)
                    try:
                        projected_qdot = np.linalg.solve(jacobian, normal_target)
                    except np.linalg.LinAlgError as exc:
                        raise ValueError('baseline normal projection is infeasible') from exc
                    if not np.all(np.isfinite(projected_qdot)):
                        raise ValueError('baseline normal projection is nonfinite')
                    projected_twist = jacobian @ projected_qdot
                    if not np.all(np.isfinite(projected_twist)) or float(
                        np.max(np.abs(projected_twist - normal_target))
                    ) > 1e-9:
                        raise ValueError('baseline normal projection residual is nonzero')
                    qdot_limit = min(
                        float(self.solver_profile.qdot_limit_rad_s),
                        0.15
                        if self.runtime.motion_profile is None
                        else float(self.runtime.motion_profile.qdot_cap_rad_s),
                    )
                    if float(np.max(np.abs(projected_qdot))) > qdot_limit + 1e-12:
                        raise ValueError(
                            'baseline normal projection exceeds joint velocity envelope'
                        )
                    command = replace(command, qdot=tuple(float(value) for value in projected_qdot))
                    baseline_residual_hold = True
                    baseline_normal_projection_applied = True
                    baseline_normal_projection_target_m_s = float(normal_target[2])
                    baseline_normal_projection_original_m_s = original_normal
                    baseline_normal_direction_correction = bool(
                        abs(original_normal) > 1e-12
                        and abs(desired_normal) > 1e-12
                        and math.copysign(1.0, original_normal)
                        != math.copysign(1.0, desired_normal)
                    )
            # The mature writer intentionally rejects provider output that
            # exceeds its typed host-slew envelope.  TASE owns the complete
            # outer loop, so apply the same-direction scalar ramp at this
            # provider boundary instead of asking the generic qualification
            # layer to silently rescale a TASE command.  The ramp is a shared
            # execution safety adaptation; its scale is retained in evidence.
            host_slew_scale = 1.0
            host_slew_limit = None
            if self.command_history is not None:
                ramp = same_direction_qdot_rescale(
                    command.qdot,
                    self.command_history.get('previous_qdot'),
                    dt_s=actual_dt_s,
                    max_slew_rad_s2=(
                        float(self.command_history['slew_rad_s2'])
                        * (1.0 - TASE_HOST_SLEW_NUMERIC_MARGIN)
                    ),
                )
                host_slew_scale = float(ramp.scale)
                host_slew_limit = float(ramp.delta_limit_rad_s)
                if host_slew_scale < 1.0:
                    command = replace(command, qdot=tuple(ramp.qdot))
            self._commit_clock(obs)
            self.phase = phase
            predicted_twist = np.asarray(command.jacobian_6x6, dtype=float) @ np.asarray(
                command.qdot, dtype=float
            )
            approach_normal_velocity = float(-predicted_twist[2])
            self.last_result = {'phase': phase, 'sample_time_s': monotonic_s,
                'qdot_rad_s': command.qdot,
                'host_slew_scale': host_slew_scale,
                'host_slew_limit_rad_s': host_slew_limit,
                'filtered_normal_n': float(filtered),
                'control_normal_n': float(control_normal),
                'measured_normal_n': float(sensor.normal_load_n),
                'measured_force_norm_n': float(sensor.force_norm_n),
                'force_preempt_warm_start': force_preempt_warm_start,
                'force_preempt_warm_started': self.force_preempt_warm_started,
                'force_preempt_armed': self.force_preempt_armed,
                'force_preempt_episode': self.force_preempt_episode,
                'force_preempt_direction_retry': force_preempt_direction_retry,
                'force_preempt_direction_retry_count': self.force_preempt_direction_retry_count,
                'force_rise_guard': force_rise_guard,
                'force_rise_inward_cap_applied': force_rise_inward_cap_applied,
                'force_rise_inward_original_m_s': force_rise_inward_original_m_s,
                'force_rise_inward_target_m_s': force_rise_inward_target_m_s,
                'force_preempt_approach_before_retry_m_s': force_preempt_approach_before_retry,
                'baseline_residual_hold': baseline_residual_hold,
                'baseline_residual_tangential_m_s': baseline_residual_tangential_m_s,
                'baseline_residual_angular_rad_s': baseline_residual_angular_rad_s,
                'baseline_normal_projection_applied': baseline_normal_projection_applied,
                'baseline_normal_projection_target_m_s': baseline_normal_projection_target_m_s,
                'baseline_normal_projection_original_m_s': baseline_normal_projection_original_m_s,
                'baseline_normal_direction_correction': baseline_normal_direction_correction,
                'baseline_normal_unload_boost': baseline_normal_unload_boost,
                'baseline_primitive_speed_m_s': baseline_primitive_speed_m_s,
                'predicted_twist_m_s_rad_s': tuple(float(value) for value in predicted_twist),
                'predicted_approach_normal_velocity_m_s': approach_normal_velocity,
                'entry_time_s': t if phase == 'entry' else None,
                'formal_time_s': t if phase == 'path' else None,
                'actual_dt_s': actual_dt_s, 'solver': copy.deepcopy(self.runtime.last_solver_diagnostics),
                'implementation': 'mature_local_tase_rnn',
                'parameter_binding': copy.deepcopy(self.parameter_binding),
                'outer_loop_binding': copy.deepcopy(TASE_PAPER_OUTER_BINDING)}
            self.last_measured_force_norm = measured_force_norm
            return command
        except BaseException:
            self.restore(checkpoint)
            raise

    def close(self):
        self.runtime.solver.freeze()
