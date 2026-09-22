#!/usr/bin/env python3
"""Build the Figure-eight Home package from the historical joint target."""

from __future__ import annotations

import argparse
import datetime as dt
import json
from pathlib import Path

from build_step4e_p0p1_programs import build_urp


PROGRAM = "step5d_joint_figure8_home_v1"
CONTROLLER_DIR = "/programs/andyl/kunwei/step5"
HOME_Q = [
    0.7451654076576233,
    -1.8181091747679652,
    -2.5626940727233887,
    -0.3122711938670655,
    1.5276236534118652,
    -0.8238123098956507,
]
HOME_POSE = [0.4620551816, 0.1778825964, 0.03408876139925415, 3.120752062, 0.0, 0.068626833]
INITIAL_Q_TOL = 0.02
# A failed contact TP can leave a small residual speedj image after Dashboard
# STOP.  Recovery Home accepts only this bounded pre-stop envelope, issues a
# bounded stopj, and then takes over with the approved joint-space move.  The
# controller's RTDE qdot image can oscillate while the tool is still loaded;
# requiring a strict zero-qdot sample before movej would leave the tool on the
# surface and make recovery impossible.
INITIAL_QD_PRESTOP_TOL = 0.010
FINAL_QD_TOL = 0.001
FINAL_Q_TOL = 0.02


def _script(initial_q: list[float], stamp: str) -> str:
    target = ", ".join(f"{float(value):.12f}" for value in HOME_Q)
    initial = ", ".join(f"{float(value):.12f}" for value in initial_q)
    pose = ", ".join(f"{float(value):.12f}" for value in HOME_POSE)
    return f'''# VERSION: {stamp}
# ROLE: independent one-shot ordinary Figure-eight Home joint motion
# TARGET_JOINTS_RAD: [{target}]
# TARGET_TCP_POSE: p[{pose}]
# MOTION: one joint-space movej, a=0.100 rad/s^2, v=0.100 rad/s
# HOME_IDENTITY: historical Step6 Figure-eight contact-derived final_q

def codex_joint_home_abs(value):
  if value < 0.0:
    return -value
  end
  return value
end

def codex_joint_home():
  local expected_initial_q = [{initial}]
  local target_q = [{target}]
  local target_pose = p[{pose}]
  local actual_q = get_actual_joint_positions()
  local actual_qd = get_actual_joint_speeds()
  local initial_q_error = 0.0
  local initial_qd_max = 0.0
  local index = 0
  while index < 6:
    local q_error = codex_joint_home_abs(actual_q[index] - expected_initial_q[index])
    if q_error > initial_q_error:
      initial_q_error = q_error
    end
    local qd_abs = codex_joint_home_abs(actual_qd[index])
    if qd_abs > initial_qd_max:
      initial_qd_max = qd_abs
    end
    index = index + 1
  end
  if initial_q_error > {INITIAL_Q_TOL:.3f} or initial_qd_max > {INITIAL_QD_PRESTOP_TOL:.3f}:
    textmsg("joint_home: initial joint state changed; halted")
    halt
  end
  stopj(20.0)
  sleep(0.10)
  movej(target_q, a=0.100, v=0.100, t=0.0, r=0.0)
  stopj(1.0)
  sleep(0.20)
  actual_q = get_actual_joint_positions()
  actual_qd = get_actual_joint_speeds()
  local final_q_error = 0.0
  local final_qd_max = 0.0
  index = 0
  while index < 6:
    local q_error = codex_joint_home_abs(actual_q[index] - target_q[index])
    if q_error > final_q_error:
      final_q_error = q_error
    end
    local qd_abs = codex_joint_home_abs(actual_qd[index])
    if qd_abs > final_qd_max:
      final_qd_max = qd_abs
    end
    index = index + 1
  end
  local final_pose = get_actual_tcp_pose()
  local final_delta = pose_trans(pose_inv(target_pose), final_pose)
  local final_position_error = sqrt(final_delta[0] * final_delta[0] + final_delta[1] * final_delta[1] + final_delta[2] * final_delta[2])
  local final_orientation_error = sqrt(final_delta[3] * final_delta[3] + final_delta[4] * final_delta[4] + final_delta[5] * final_delta[5])
  if final_q_error > {FINAL_Q_TOL:.3f} or final_qd_max > 0.001 or final_position_error > 0.001 or final_orientation_error > 0.005:
    textmsg("joint_home: final joint or TCP verification failed; halted")
    halt
  end
  textmsg("joint_home: Figure-eight joint Home verified")
  halt
end

codex_joint_home()
'''


def build(output: Path, initial_q: list[float]) -> dict:
    if len(initial_q) != 6:
        raise ValueError("initial_q must have six values")
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H%MZ_JOINT_FIGURE8_HOME_V1")
    output.mkdir(parents=True, exist_ok=False)
    script = _script(initial_q, stamp)
    (output / f"{PROGRAM}.script").write_text(script, encoding="utf-8")
    (output / f"{PROGRAM}.txt").write_text(
        f"Joint Figure-eight Home\n{stamp}\n{PROGRAM}\none movej a=0.100 rad/s2 v=0.100 rad/s\n",
        encoding="utf-8",
    )
    (output / f"{PROGRAM}.urp").write_bytes(build_urp(script, PROGRAM, CONTROLLER_DIR))
    binding = {
        "program": PROGRAM,
        "controller_target": f"{CONTROLLER_DIR}/{PROGRAM}.urp",
        "home_q": HOME_Q,
        "home_pose": HOME_POSE,
        "initial_q": initial_q,
        "initial_q_tolerance_rad": INITIAL_Q_TOL,
        "final_q_tolerance_rad": FINAL_Q_TOL,
        "motion": {"type": "stopj_then_movej", "stopj_acceleration_rad_s2": 2.0,
                   "acceleration_rad_s2": 0.1, "speed_rad_s": 0.1,
                   "initial_qd_prestop_tolerance_rad_s": INITIAL_QD_PRESTOP_TOL,
                   "final_qd_tolerance_rad_s": FINAL_QD_TOL},
        "historical_source": "August R013 Step6 Figure-eight contact-derived final_q receipt",
        "live_executed": False,
    }
    (output / "binding.json").write_text(json.dumps(binding, indent=2) + "\n", encoding="utf-8")
    return binding


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--initial-q", type=float, nargs=6, required=True)
    args = parser.parse_args()
    print(json.dumps(build(args.output, list(args.initial_q)), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
