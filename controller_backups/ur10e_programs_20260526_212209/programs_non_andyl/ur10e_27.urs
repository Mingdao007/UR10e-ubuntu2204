# ============================================================
# UR10e DEMO - movel based (mode1_to4 movement) + Z-only force_mode
# URSim / PolyScope 5.x
# ============================================================

# ---------------------------
# RTDE Register Map
# ---------------------------
IN_CMD = 18   # 0=Stop, 1..4=Mode
IN_SPEED_X100 = 19   # optional speed scaling (not mandatory)
IN_FORCE_X10 = 20   # target contact force in N*10 (e.g. 25=2.5N, 50=5N, 80=8N)
IN_DURATION_S = 21   # duration in seconds
IN_CMD_SEQ = 22
 # IN_FORCE_ENABLE register not used here (RTDE IO supports only 18-22)

OUT_STATE = 12 # 0=IDLE, 1=RUNNING
OUT_ERROR_CODE = 13 # 0=OK, 1=Unknown cmd, 2=Overforce stop
OUT_CURRENT_MODE = 14
OUT_PROGRESS = 15
OUT_ACK_SEQ = 16
OUT_CAL_STATUS = 17

STATE_IDLE = 0
STATE_RUNNING = 1

ERR_OK = 0
ERR_UNKNOWN_CMD = 1
ERR_OVERFORCE = 2

# ---------------------------
# Motion parameters (SAFE & SLOW)
# ---------------------------
A_SLOW = 0.40
V_SLOW = 0.3
R_BLEND = 0.005   # 5mm blend for smooth continuous motion
R_BLEND_VIB = 0.001  # 1mm blend for micro-vibration mode
DT = 0.05   # logical time step for movement math (movel is blocking anyway)

# ---------------------------
# RG2 parameters (mode 4)
# ---------------------------
RG2_CLOSE_WIDTH = 40
RG2_CLOSE_FORCE = 10.0
RG2_OPEN_WIDTH = 80
RG2_OPEN_FORCE = 10.0
RG2_STEP_UP_M = 0.050
RG2_STEP_Y_M = 0.100
RG2_GRIPPER_CYCLES = 1
# Estimated seconds per full A->B->C->D->A cycle in mode 4.
# Tune this to match your actual robot timing.
MODE4_CYCLE_EST_S = 8.0

# ---------------------------
# Force control parameters
# ---------------------------
FZ_HARD_LIMIT = 25.0      # N, emergency stop threshold (positive when pressing calf)
FORCE_Z_DEV = 0.10      # meters, allowed Z deviation in force_mode (0.10m = ±100mm)
# NOTE: recommended safer start: 0.01~0.02 (±10~20mm)

# ============================================================
# Helpers
# ============================================================
def set_outputs(state, mode, prog, ack, err):
  write_output_integer_register(OUT_STATE, state)
  write_output_integer_register(OUT_CURRENT_MODE, mode)
  write_output_integer_register(OUT_PROGRESS, prog)
  write_output_integer_register(OUT_ACK_SEQ, ack)
  write_output_integer_register(OUT_ERROR_CODE, err)
  write_output_integer_register(OUT_CAL_STATUS, 0)
end

def stop_motion():
  stopj(0.5)
end

def end_force_safe():
  # end_force_mode() is safe even if not in force mode (on most versions)
  end_force_mode()
end

def overforce_check_and_stop():
  f = get_tcp_force()
  fz = f[2]
  if fz > FZ_HARD_LIMIT:
    end_force_safe()
    stop_motion()
    return True
  end
  return False
end

def apply_force_mode_z(task_frame, fz_target):
  # Z-only compliant, keep others stiff
  sel = [0, 0, 1, 0, 0, 0]
  wrench = [0, 0, fz_target, 0, 0, 0]   # Fz positive (you confirmed contact force is positive)
  # limits: [x,y,z,rx,ry,rz] max deviation / speed constraints under force mode
  # We only allow Z to move (±FORCE_Z_DEV), others 0
  lim = [0.0, 0.0, FORCE_Z_DEV, 0.0, 0.0, 0.0]

  # type=2 commonly used in examples for "force frame/task frame" behavior.
  # If you observe direction reversed, change type to 1 or flip wrench sign.
  force_mode(task_frame, sel, wrench, 2, lim)
end

def rg2_close_open():
  rg_grip(RG2_CLOSE_WIDTH, RG2_CLOSE_FORCE, tool_index = 0, blocking = True, depth_comp = False, popupmsg = True)
  rg_payload_set(mass = 0.0, tool_index = 0, use_guard = True)
  rg_grip(RG2_OPEN_WIDTH, RG2_OPEN_FORCE, tool_index = 0, blocking = True, depth_comp = False, popupmsg = True)
end

def rg2_cycles():
  # Perform 3 close/open cycles at current position
  i = 0
  while i < RG2_GRIPPER_CYCLES:
    rg2_close_open()
    i = i + 1
  end
end

# ============================================================
# Main loop
# ============================================================

active_mode = 0
last_seq = -1
last_force_x10 = 0

set_outputs(STATE_IDLE, 0, 0, 0, ERR_OK)

# Capture Position A once at program start
pose_A = get_actual_tcp_pose()

while True:

  cmd = read_input_integer_register(IN_CMD)
  seq = read_input_integer_register(IN_CMD_SEQ)

  # ----------------------------------------------------------
  # New command (seq/ack handshake)
  # ----------------------------------------------------------
  if seq != last_seq:
    last_seq = seq
    end_force_safe()

    if cmd == 0:
      stop_motion()
      active_mode = 0
      set_outputs(STATE_IDLE, 0, 0, last_seq, ERR_OK)

    elif (cmd >= 1) and (cmd <= 4):
      active_mode = cmd
      set_outputs(STATE_RUNNING, active_mode, 0, last_seq, ERR_OK)

    else:
      active_mode = 0
      set_outputs(STATE_IDLE, 0, 0, last_seq, ERR_UNKNOWN_CMD)
    end
  end

  # ----------------------------------------------------------
  # Run active mode (movel based, with Z-only force_mode)
  # ----------------------------------------------------------
  if active_mode > 0:

    # use Position A as the fixed start pose for every task
    start_pose = pose_A
    rx = start_pose[3]
    ry = start_pose[4]
    rz = start_pose[5]

    duration = read_input_integer_register(IN_DURATION_S)
    if duration <= 0:
      duration = 30
    end
    elapsed = 0.0
    prog = 0

    # Use start_pose as task frame reference
    task_frame = start_pose

    # Always move to Position A before starting a new task
    movej(get_inverse_kin(start_pose), a = A_SLOW, v = V_SLOW, r = R_BLEND)
    sync()

    completed_normally = True
    while elapsed < duration:

      # allow STOP between steps
      if read_input_integer_register(IN_CMD) == 0:
        end_force_safe()
        stop_motion()
        active_mode = 0
        completed_normally = False
        set_outputs(STATE_IDLE, 0, 0, last_seq, ERR_OK)
        break
      end

      fx10 = read_input_integer_register(IN_FORCE_X10)
      if fx10 != last_force_x10:
        if fx10 < 0:
          textmsg("force_active=1 fx10=" + to_str(fx10))
        else:
          textmsg("force_active=0 fx10=" + to_str(fx10))
        end
        last_force_x10 = fx10
      end

      if fx10 < 0:
        # NOTE: URScript has no built-in abs(); use explicit if/else for safety.
        fz_target = (-fx10) / 10.0
        apply_force_mode_z(task_frame, fz_target)
        if overforce_check_and_stop():
          active_mode = 0
          completed_normally = False
          set_outputs(STATE_IDLE, 0, prog, last_seq, ERR_OVERFORCE)
          break
        end
      else:
        end_force_safe()
      end

      if active_mode == 4:
        # Position 1: close/open gripper (blocking - wait until complete)
        rg2_cycles()

        # Move to Position 2: up 50mm, Y +100mm, down 50mm
        target = p[start_pose[0], start_pose[1], start_pose[2] + RG2_STEP_UP_M, rx, ry, rz]
        movej(get_inverse_kin(target), a = A_SLOW, v = V_SLOW, r = R_BLEND)
        target = p[start_pose[0], start_pose[1] + RG2_STEP_Y_M, start_pose[2], rx, ry, rz]
        movej(get_inverse_kin(target), a = A_SLOW, v = V_SLOW, r = R_BLEND)

        # Position 2: close/open gripper (blocking - wait until complete)
        rg2_cycles()

        # Move to Position 3: up 50mm, Y +100mm, down 50mm
        target = p[start_pose[0], start_pose[1] + RG2_STEP_Y_M, start_pose[2] + RG2_STEP_UP_M, rx, ry, rz]
        movej(get_inverse_kin(target), a = A_SLOW, v = V_SLOW, r = R_BLEND)
        target = p[start_pose[0], start_pose[1] + (2.0 * RG2_STEP_Y_M), start_pose[2], rx, ry, rz]
        movej(get_inverse_kin(target), a = A_SLOW, v = V_SLOW, r = R_BLEND)

        # Position 3: close/open gripper (blocking - wait until complete)
        rg2_cycles()

        # Move to Position 4: up 50mm, Y +100mm, down 50mm
        target = p[start_pose[0], start_pose[1] + (2.0 * RG2_STEP_Y_M), start_pose[2] + RG2_STEP_UP_M, rx, ry, rz]
        movej(get_inverse_kin(target), a = A_SLOW, v = V_SLOW, r = R_BLEND)
        target = p[start_pose[0], start_pose[1] + (3.0 * RG2_STEP_Y_M), start_pose[2], rx, ry, rz]
        movej(get_inverse_kin(target), a = A_SLOW, v = V_SLOW, r = R_BLEND)

        # Position 4: close/open gripper (blocking - wait until complete)
        rg2_cycles()

        # Return to Position 1: up 50mm, back to start
        target = p[start_pose[0], start_pose[1] + (3.0 * RG2_STEP_Y_M), start_pose[2] + RG2_STEP_UP_M, rx, ry, rz]
        movej(get_inverse_kin(target), a = A_SLOW, v = V_SLOW, r = R_BLEND)
        movej(get_inverse_kin(start_pose), a = A_SLOW, v = V_SLOW, r = R_BLEND)
        sync()
      else:
        # Time variable for patterns (logical)
        t = elapsed

        # ----------------------
        # Mode 1: slow circle
        # ----------------------
        if active_mode == 1:
          r = 0.02        # 20mm
          f = 0.5         # 0.5Hz
          dx = r * cos(2 * 3.1415926 * f * t)
          dy = r * sin(2 * 3.1415926 * f * t)
          dz = 0.0

          # ----------------------
          # Mode 2: slow push (Y)
          # ----------------------
        elif active_mode == 2:
          a = 0.03        # 30mm
          f = 0.8
          dx = 0.0
          dy = a * sin(2 * 3.1415926 * f * t)
          dz = 0.0

          # ----------------------
          # Mode 3: spiral knead
          # ----------------------
        else:
          max_r = 0.025   # 25mm
          f = 0.6
          p = elapsed / duration
          if p > 1.0:
            p = 1.0
          end
          r = max_r * p
          dx = r * cos(2 * 3.1415926 * f * t)
          dy = r * sin(2 * 3.1415926 * f * t)
          dz = 0.0
        end

        # target pose keeps orientation fixed (stable like mode1_to4)
        target = p[
          start_pose[0] + dx,
          start_pose[1] + dy,
          start_pose[2] + dz,
          rx, ry, rz
        ]

        # Smoothness: use blend radius so the TCP doesn't decel-to-zero at every waypoint
        movel(target, a = A_SLOW, v = V_SLOW, r = R_BLEND)
        sync()
      end

      # Post-move safety check too (catch transient spike)
      if overforce_check_and_stop():
        active_mode = 0
        completed_normally = False
        set_outputs(STATE_IDLE, 0, prog, last_seq, ERR_OVERFORCE)
        break
      end

      if active_mode == 4:
        elapsed = elapsed + MODE4_CYCLE_EST_S
      else:
        elapsed = elapsed + DT
      end

      prog = floor(100 * elapsed / duration)
      if prog > 99:
        prog = 99
      end
      set_outputs(STATE_RUNNING, active_mode, prog, last_seq, ERR_OK)

    end

    # ensure force mode cleared when finishing normally
    end_force_safe()

    # Return to start pose (A) after a normal completion so next task starts at A
    if completed_normally:
      movej(get_inverse_kin(start_pose), a = A_SLOW, v = V_SLOW, r = R_BLEND)
      sync()
    end

    if active_mode != 0:
      active_mode = 0
      set_outputs(STATE_IDLE, 0, 0, last_seq, ERR_OK)
    end

  else:
    sync()
  end

end
