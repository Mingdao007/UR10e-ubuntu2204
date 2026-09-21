#!/usr/bin/env python3
"""Generate an isolated contact-QP resident, never Load/Play or open a device."""
from pathlib import Path
import argparse,datetime,hashlib,json,re
import numpy as np
from build_step4e_p0p1_programs import build_urp
from step5d_autotune_v4_r012.controller_triplet import validate_urscript_block_balance
from contact_benchmark_protocol import Task

ROOT=Path(__file__).resolve().parents[1]
SOURCE=ROOT/'programs/step5/step5d/step5d_strict_rnn_autotune_v4_r012.script'
BASENAME='step5d_contact_six_qp_v1'
CONTROLLER_DIR='/programs/andyl/kunwei/step5'
PROTOCOL=618001
# Single-admission contact package: one continuous ten-second qualification
# feeds the following PATH attempt. This wire-semantic change gets a fresh
# readable runtime revision and a fresh controller read-back.
REVISION=21


def transform(source,home,stamp):
    if home.get('user_home_confirmed') is not True:raise ValueError('user-defined Home receipt required')
    obs=home['rtde'];pose=np.asarray(home['home_pose']);q=np.asarray(home['home_q'])
    if pose.shape!=(6,) or q.shape!=(6,) or not np.isfinite(pose).all() or not np.isfinite(q).all():raise ValueError('invalid Home')
    if np.linalg.norm(obs['actual_TCP_speed'])>.00001:raise ValueError('Home not stationary')
    if not np.allclose(obs['tcp_offset'],[0,0,.0874,0,0,0],atol=1e-9):raise ValueError('TCP differs')
    if not np.isclose(obs['payload'],.413,atol=1e-6) or not np.allclose(obs['payload_cog'],[.0011,.0031,.0163],atol=1e-6):raise ValueError('tool mass/CoG differs')
    if 'while path_elapsed_s < 60.000000000 and not r012_path_early_end:' not in source:raise ValueError('source resident differs')
    body=source.replace('step5d_strict_rnn_autotune_v4_r012',BASENAME)
    body=re.sub(r'^# VERSION: .*$',f'# VERSION: {stamp}',body,flags=re.M)
    body=body.replace('612012',str(PROTOCOL))
    body=re.sub(r'local runtime_revision = [-0-9]+',f'local runtime_revision = {REVISION}',body)
    body=body.replace('write_output_integer_register(33, 12)',f'write_output_integer_register(33, {REVISION})')
    home_text='p['+', '.join(f'{v:.12f}' for v in pose)+']'
    body=re.sub(r'local fixed_home_pose = p\[[^\]]+\]',f'local fixed_home_pose = {home_text}',body)
    body=re.sub(r'^# FIXED_HOME_POSE: .*$',f'# FIXED_HOME_POSE: {home_text}',body,flags=re.M)
    body=body.replace('while path_elapsed_s < 60.000000000 and',f'while path_elapsed_s < {Task().duration_s:.12f} and')
    # The active installation owns integer input24 for OnRobot's RTDE watchdog.
    # Remap only the native logical slot; float24 and output24 are different banks.
    if body.count('read_input_integer_register(24)') != 7:
        raise ValueError('native input24 read sites differ')
    body=body.replace('read_input_integer_register(24)', 'read_input_integer_register(36)')
    if 'read_input_integer_register(24)' in body or body.count('read_input_integer_register(36)') != 7:
        raise ValueError('native physical register allocation differs')
    # A stop-only packet has no sensor measurement. Explicit STOP still means
    # external_stop in active loops; preserve any earlier terminal fault.
    guard='  if packet_reason != 0:\n    return packet_reason\n  elif read_input_float_register(27) < 0.5'
    if body.count(guard)!=1:raise ValueError('native packet guard differs')
    body=body.replace(guard,'  if packet_reason != 0:\n    return packet_reason\n  elif read_input_integer_register(27) == 3:\n    return 4\n  elif read_input_float_register(27) < 0.5')
    # Narrow inherited speed bounds; leave the fault/return lifecycle intact.
    body=body.replace('codex_r006_finite(qdot, 5.000000000)','codex_r006_finite(qdot, 0.050000000)')
    body=body.replace('speedj(baseline_qdot, 40.000000000,','speedj(baseline_qdot, 5.000000000,')
    body=body.replace('speedj(path_qdot, 40.000000000,','speedj(path_qdot, 5.000000000,')
    body=body.replace('local d_near_start_travel_m = 0.011029311','local d_near_start_travel_m = 0.000000000')
    body=body.replace('local v_far_m_s = 0.005000000','local v_far_m_s = 0.000200000')
    body=body.replace('local v_near_m_s = 0.000500000','local v_near_m_s = 0.000200000')
    body=body.replace('local force_fuse_n = 50.000000000','local force_fuse_n = 20.000000000')
    body=body.replace('travel >= 0.025000000','travel >= 0.015000000')
    body=body.replace('packet_reason, 60.0, 100.0, 3.0','packet_reason, 20.0, 20.0, 2.0')
    body=body.replace('packet_reason, 100.0, 100.0, 3.0','packet_reason, 20.0, 20.0, 2.0')
    # Equivalent +/-pi rotation vectors must not falsely reject the taught Home.
    old_rotation = '  local rotation_error = sqrt((actual[3] - expected[3]) * (actual[3] - expected[3]) + (actual[4] - expected[4]) * (actual[4] - expected[4]) + (actual[5] - expected[5]) * (actual[5] - expected[5]))'
    if body.count(old_rotation) != 1:raise ValueError('Home orientation gate source differs')
    body=body.replace(old_rotation,'  local relative_pose = pose_trans(pose_inv(expected), actual)\n  local rotation_error = sqrt(relative_pose[3]*relative_pose[3] + relative_pose[4]*relative_pose[4] + relative_pose[5]*relative_pose[5])')
    recovery_guard_marker='\n\ndef codex_r006_pose_close('
    if body.count(recovery_guard_marker)!=1:raise ValueError('resident pose marker differs for recovery guard repair')
    recovery_guards='''

def codex_r006_recovery_packet_guard(packet_reason, abs_normal_limit, force_norm_limit, torque_limit):
  # An already-latched STOP command is allowed to remain present while the
  # bounded Home route runs. All sensor, qdot, tool and force guards remain.
  if packet_reason != 0:
    return packet_reason
  elif read_input_float_register(27) < 0.5 or read_input_float_register(29) < 0.5:
    return 3
  elif read_input_integer_register(27) != 0 and read_input_integer_register(27) != 1 and read_input_integer_register(27) != 3:
    return 66
  elif codex_r006_abs(read_input_float_register(24)) >= abs_normal_limit:
    return 5
  elif read_input_float_register(25) >= force_norm_limit:
    return 6
  elif read_input_float_register(30) >= torque_limit:
    return 7
  elif read_input_float_register(44) < 1.0 or read_input_float_register(44) > 5.0:
    return 49
  elif not codex_r006_qdot_ok():
    return 45
  end
  return 0
end

def codex_r006_recovery_stationary(required_s):
  local dwell_s = 0.0
  local elapsed_s = 0.0
  while dwell_s < required_s and elapsed_s < required_s * 5.0:
    local packet_reason = codex_r006_packet_observe()
    local guard = codex_r006_recovery_packet_guard(packet_reason, 20.0, 20.0, 2.0)
    local speed = get_actual_tcp_speed()
    local linear = sqrt(speed[0] * speed[0] + speed[1] * speed[1] + speed[2] * speed[2])
    local angular = sqrt(speed[3] * speed[3] + speed[4] * speed[4] + speed[5] * speed[5])
    if guard != 0:
      return False
    elif linear > 0.000500000 or angular > 0.005000000:
      dwell_s = 0.0
    else:
      dwell_s = dwell_s + get_steptime()
    end
    elapsed_s = elapsed_s + get_steptime()
    sync()
  end
  return dwell_s >= required_s
end
'''
    body=body.replace(recovery_guard_marker,recovery_guards+recovery_guard_marker,1)
    return_start=body.index('def codex_r006_return_home(')
    return_end=body.index('\n\ndef codex_r006_execute_attempt(',return_start)
    return_body=body[return_start:return_end]
    if return_body.count('codex_r006_packet_guard(')!=1 or return_body.count('codex_r006_stationary(')!=3:
        raise ValueError('return-home guard call sites differ')
    return_body=return_body.replace('codex_r006_packet_guard(','codex_r006_recovery_packet_guard(')
    return_body=return_body.replace('codex_r006_stationary(','codex_r006_recovery_stationary(')
    body=body[:return_start]+return_body+body[return_end:]
    # Every terminal fault must attempt the existing bounded return-to-Home
    # route.  The inherited R012 resident only latched STOPPED, which left a
    # failed contact attempt at its fault pose.  Keep the attempt terminal
    # after recovery: this is a physical safety return, not an auto-retry.
    auto_home_marker='\n\ndef codex_r006_execute_attempt('
    if body.count(auto_home_marker)!=1:raise ValueError('resident execute marker differs for automatic Home repair')
    auto_home='''

def codex_r006_recover_home_after_fault(home_pose, home_q, epoch, ordinal, token, kind, consumed, runtime_revision, runtime_extension, failure_reason, failure_guard):
  # Home is attempted for every commandable terminal fault.  A verified Home
  # does not turn the failed attempt into a successful or retryable attempt.
  local recovered = codex_r006_return_home(home_pose, home_q, epoch, ordinal, token, kind, consumed, runtime_revision, runtime_extension, failure_guard)
  if recovered:
    codex_r006_attempt_success = False
    codex_r006_attempt_reason = failure_reason
    codex_r006_attempt_guard = failure_guard
    codex_r006_echo(epoch, ordinal, 90, token, failure_reason, consumed, kind, failure_guard, runtime_revision, runtime_extension)
  end
  return recovered
end
'''
    body=body.replace(auto_home_marker,auto_home+auto_home_marker,1)
    # RTDE holds its last input image. Consume STOP once and latch the terminal
    # resident state until a new Play; repeated packets must not call stopl.
    stop_start = '    if session_command == 3 and session_sequence > consumed_session_sequence:\n'
    stop_end = '    elif integer_reason != 0 and session_command != 0:\n'
    if body.count(stop_start) != 1 or body.count(stop_end) != 1:
        raise ValueError('resident STOP branch source differs')
    begin=body.index(stop_start);end=body.index(stop_end,begin)
    old_body=body[begin+len(stop_start):end]
    body=body[:begin]+stop_start+'      consumed_session_sequence = session_sequence\n      if state != 90:\n'+''.join('  '+line+'\n' for line in old_body.splitlines())+'      end\n'+body[end:]
    body=body.replace(stop_end,'    elif integer_reason != 0 and session_command != 0 and state != 90:\n')
    stop_recovery='''        if not locked_home_q_valid:
          locked_home_q = get_actual_joint_positions()
          locked_home_q_valid = True
        end
        if not codex_r006_recover_home_after_fault(fixed_home_pose, locked_home_q, input_epoch, current_ordinal, current_token, current_kind, consumed_session_sequence, runtime_revision, runtime_extension, 4, 0):
          reason = codex_r006_attempt_reason
          return_guard = codex_r006_attempt_guard
        else:
          reason = 4
          return_guard = codex_r006_attempt_guard
        end
'''
    stop_marker='        return_guard = 0\n      end\n    elif integer_reason != 0 and session_command != 0 and state != 90:\n'
    if body.count(stop_marker)!=1:raise ValueError('resident STOP branch differs for automatic Home repair')
    body=body.replace(stop_marker,'        return_guard = 0\n'+stop_recovery+'      end\n    elif integer_reason != 0 and session_command != 0 and state != 90:\n',1)
    integer_old='''      state = 90
      reason = integer_reason
      return_guard = 0
'''
    if body.count(integer_old)!=1:raise ValueError('resident integer fault branch differs for automatic Home repair')
    integer_new='''      state = 90
      reason = integer_reason
      return_guard = 0
      if not locked_home_q_valid:
        locked_home_q = get_actual_joint_positions()
        locked_home_q_valid = True
      end
      if not codex_r006_recover_home_after_fault(fixed_home_pose, locked_home_q, input_epoch, current_ordinal, current_token, current_kind, session_sequence, runtime_revision, runtime_extension, integer_reason, 0):
        reason = codex_r006_attempt_reason
        return_guard = codex_r006_attempt_guard
      else:
        reason = integer_reason
        return_guard = codex_r006_attempt_guard
      end
'''
    body=body.replace(integer_old,integer_new,1)
    old_comment='''        # No Script2 auto-home is permitted.  A bad fixed-Home entry is a
        # terminal typed failure before ARM state or any contact motion.
'''
    if body.count(old_comment)!=1:raise ValueError('resident Home admission comment differs')
    body=body.replace(old_comment,
        '        # Every terminal fault attempts the existing bounded Home route.\n'
        '        # A bad fixed-Home entry remains terminal but must not strand the robot.\n',1)
    header_comment='# HOME_ENTRY_FAILURE: 69 ATTEMPT_ENTRY_NOT_CAPTURED_HOME; stop without Script2 auto-home'
    if body.count(header_comment)!=1:raise ValueError('resident Home-entry header differs')
    body=body.replace(header_comment,
        '# HOME_ENTRY_FAILURE: 69 ATTEMPT_ENTRY_NOT_CAPTURED_HOME; attempt bounded Home recovery',1)
    invalid_attempt='''      if input_epoch <= last_failed_epoch or input_epoch <= 0 or input_ordinal < 1 or input_ordinal <= current_ordinal or (active_epoch > 0 and input_epoch != active_epoch) or input_kind < 1 or input_kind > 4 or input_token <= 0:
        state = 90
        reason = 61
        last_failed_epoch = input_epoch
      elif False:  # r006 host owns phase sequencing; TP accepts positive monotonic attempts
        state = 90
        reason = 68
        last_failed_epoch = input_epoch
'''
    if body.count(invalid_attempt)!=1:raise ValueError('resident invalid ARM branch differs')
    invalid_recovery='''      if input_epoch <= last_failed_epoch or input_epoch <= 0 or input_ordinal < 1 or input_ordinal <= current_ordinal or (active_epoch > 0 and input_epoch != active_epoch) or input_kind < 1 or input_kind > 4 or input_token <= 0:
        stopl(0.250000000)
        state = 90
        reason = 61
        last_failed_epoch = input_epoch
        if not locked_home_q_valid:
          locked_home_q = get_actual_joint_positions()
          locked_home_q_valid = True
        end
        if not codex_r006_recover_home_after_fault(fixed_home_pose, locked_home_q, input_epoch, input_ordinal, input_token, input_kind, session_sequence, runtime_revision, runtime_extension, 61, 0):
          reason = codex_r006_attempt_reason
        end
        return_guard = codex_r006_attempt_guard
      elif False:  # r006 host owns phase sequencing; TP accepts positive monotonic attempts
        stopl(0.250000000)
        state = 90
        reason = 68
        last_failed_epoch = input_epoch
        if not locked_home_q_valid:
          locked_home_q = get_actual_joint_positions()
          locked_home_q_valid = True
        end
        if not codex_r006_recover_home_after_fault(fixed_home_pose, locked_home_q, input_epoch, input_ordinal, input_token, input_kind, session_sequence, runtime_revision, runtime_extension, 68, 0):
          reason = codex_r006_attempt_reason
        end
        return_guard = codex_r006_attempt_guard
'''
    body=body.replace(invalid_attempt,invalid_recovery,1)
    entry_old='''        if not codex_r006_entry_home_verified(fixed_home_pose, locked_home_q, locked_home_q_valid):
          stopl(0.250000000)
          session_active = False
          state = 90
          reason = 69
          return_guard = 0
          last_failed_epoch = input_epoch
'''
    if body.count(entry_old)!=1:raise ValueError('resident Home-entry fault branch differs')
    entry_new='''        if not codex_r006_entry_home_verified(fixed_home_pose, locked_home_q, locked_home_q_valid):
          stopl(0.250000000)
          session_active = False
          state = 90
          reason = 69
          return_guard = 0
          last_failed_epoch = input_epoch
          if not locked_home_q_valid:
            locked_home_q = get_actual_joint_positions()
            locked_home_q_valid = True
          end
          if not codex_r006_recover_home_after_fault(fixed_home_pose, locked_home_q, input_epoch, input_ordinal, input_token, input_kind, session_sequence, runtime_revision, runtime_extension, 69, 0):
            reason = codex_r006_attempt_reason
          end
          return_guard = codex_r006_attempt_guard
'''
    body=body.replace(entry_old,entry_new,1)
    execute_old='''          if not codex_r006_execute_attempt(fixed_home_pose, arm_home_q, active_epoch, current_ordinal, current_token, current_kind, consumed_session_sequence, runtime_revision, runtime_extension):
            session_active = False
            state = 90
            reason = codex_r006_attempt_reason
            return_guard = codex_r006_attempt_guard
            last_failed_epoch = active_epoch
'''
    if body.count(execute_old)!=1:raise ValueError('resident execute fault branch differs')
    execute_new='''          if not codex_r006_execute_attempt(fixed_home_pose, arm_home_q, active_epoch, current_ordinal, current_token, current_kind, consumed_session_sequence, runtime_revision, runtime_extension):
            session_active = False
            state = 90
            reason = codex_r006_attempt_reason
            return_guard = codex_r006_attempt_guard
            last_failed_epoch = active_epoch
            if not codex_r006_recover_home_after_fault(fixed_home_pose, arm_home_q, active_epoch, current_ordinal, current_token, current_kind, consumed_session_sequence, runtime_revision, runtime_extension, reason, return_guard):
              reason = codex_r006_attempt_reason
            end
            return_guard = codex_r006_attempt_guard
'''
    body=body.replace(execute_old,execute_new,1)
    arm='    elif not session_active and not completed and session_command == 1 and '
    if body.count(arm)!=1:raise ValueError('resident ARM branch source differs')
    body=body.replace(arm,'    elif state != 90 and not session_active and not completed and session_command == 1 and ')
    lines=[line for line in body.splitlines() if not line.startswith(('# R006_ACTIVE_CAPS:','# ROLE:','# CONTACT_SEARCH:'))]
    lines[1:1]=[f'# ROLE: six-law shared-QP preparation; requires matching host owner {PROTOCOL}',
        '# CONTACT_CAPS: qdot<=0.05rad/s; speedj_accel=5rad/s2; force_norm<20N; torque_norm<2Nm',
        '# CONTACT_SEARCH: constant 0.0002m/s; travel<=0.015m; timeout=90s; original Home XYZ with current aligned attitude',
        '# EOAT: payload=0.413kg CoG=[0.0011,0.0031,0.0163]m TCP=[0,0,0.0874,0,0,0]',
        '# HOST_BACKEND: native-six-law + osqp-codegen-c; legacy RNN qualification is not reusable',
        '# FAULT_RECOVERY: every terminal fault attempts bounded Home; host monitored Home is the fallback']
    lines.insert(6, '# NATIVE_INTEGER_INPUTS: logical24->physical36; 25..32 and 35 unchanged; OnRobot owns input24')
    body='\n'.join(lines)+'\n'
    validate_urscript_block_balance(body)
    for forbidden in ('set_tcp(', 'set_payload(', 'zero_ftsensor(', 'speedj(path_qdot, 40.'):
        if forbidden in body:raise ValueError(f'forbidden package effect: {forbidden}')
    return body


def build(home_path,output):
    home=json.loads(home_path.read_text());stamp=datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%dT%H%MZ_CONTACT_SIX_QP_V1_618001')
    source=SOURCE.read_text();script=transform(source,home,stamp);output.mkdir(parents=True,exist_ok=True)
    txt=f'Contact six-law shared QP\nPROGRAM={BASENAME}\nVERSION={stamp}\nPROTOCOL={PROTOCOL}\nLIVE_QUALIFIED=false\n'
    for suffix,data in [('script',script.encode()),('txt',txt.encode()),('urp',build_urp(script,BASENAME,CONTROLLER_DIR))]:
        (output/f'{BASENAME}.{suffix}').write_bytes(data)
    manifest={'schema':'contact-qp-package-preparation-v1','basename':BASENAME,'stamp':stamp,'controller_directory':CONTROLLER_DIR,
              'revision':REVISION,'protocol':PROTOCOL,'home_receipt_sha256':hashlib.sha256(home_path.read_bytes()).hexdigest(),
              'source_sha256':hashlib.sha256(SOURCE.read_bytes()).hexdigest(),'live_qualified':False,
              'geometry':Task().sanity(),'source_owner':'R012 lifecycle; narrowed common caps, automatic Home-on-fault, and new Home/identity/full-period duration'}
    manifest['numeric_sanity']={'duration_s':Task().duration_s,'span_m':[.08,.02],
        'peak_nominal_linear_speed_bound_m_s':Task().sanity()['speed_upper_bound_m_s'],
        'host_tangent_speed_cap_m_s':.01,'tp_joint_speed_limit_rad_s':.05,
        'tp_speedj_acceleration_rad_s2':5.,'search_speed_m_s':.0002,
        'search_travel_limit_m':.015,'search_timeout_s':90.,'full_search_travel_time_s':75.,
        'raw_force_limit_n':20.,'raw_torque_limit_nm':2.,'fixed_orientation_approach_axis':[0,0,-1],
        'home_xyz_m':home['home_pose'][:3],
        'scope':'local package arithmetic; workspace clearance and owner dispatch pending'}
    (output/f'{BASENAME}.binding.json').write_text(json.dumps(manifest,indent=2)+'\n');return manifest

if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--home-receipt',type=Path,required=True);p.add_argument('--output',type=Path,required=True);a=p.parse_args();print(json.dumps(build(a.home_receipt,a.output),indent=2))
