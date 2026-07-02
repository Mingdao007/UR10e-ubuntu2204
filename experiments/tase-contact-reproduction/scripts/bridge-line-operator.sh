#!/usr/bin/env bash
set -euo pipefail

SCRIPT_PATH="$(readlink -f "${BASH_SOURCE[0]}")"
SCRIPT_DIR="$(cd "$(dirname "${SCRIPT_PATH}")" && pwd)"
ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
RUN_ROOT="${ROOT}/runs"
STAMP="$(date +%Y%m%d_%H%M%S)"
ROBOT_HOST="${ROBOT_HOST:-192.168.1.18}"
DASHBOARD_PORT="${DASHBOARD_PORT:-29999}"
WAIT_FOR_PLAY_S="${WAIT_FOR_PLAY_S:-45}"
AUTOWATCH_WAIT_FOR_PLAY_S="${AUTOWATCH_WAIT_FOR_PLAY_S:-30}"
BENCH_GATE="/home/andy/codex-private-skills/skills/ur10e-realsetup/scripts/check_ubuntu_network.py"
LONG_CHECK_TTL_S="${LONG_CHECK_TTL_S:-7200}"
LONG_CHECK_CACHE="${LONG_CHECK_CACHE:-${RUN_ROOT}/.bridge_long_checks_cache.json}"
STEP5D_RUNTIME_INTERFACE="${ROOT}/tools/step5d_runtime_interface.py"
BRIDGE_PROFILE="${BRIDGE_PROFILE:-${STEP4E_VERSION:-v31}}"

current_step5d_profile() {
  python3 - "${ROOT}/config/current_stage.json" <<'PY'
import json
import sys
from pathlib import Path

try:
    current = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
except Exception:
    raise SystemExit(0)
program = current.get("program") or current.get("current_stage_id") or ""
if program.startswith("step5d_strict_rnn_liveprep_"):
    print(program)
PY
}

case "${BRIDGE_PROFILE}" in
  4f|f|cycloid|step4f)
    BRIDGE_PROFILE="step4f_v1"
    ;;
  4g|g|eight|figure8|figure-8|step4g)
    BRIDGE_PROFILE="step4g_v1"
    ;;
  5b|step5b|step5b_v1|contact-cycloid|contact_cycloid)
    BRIDGE_PROFILE="step5b_v1"
    ;;
  5d|step5d|step5d-liveprep|step5d_liveprep)
    BRIDGE_PROFILE="$(current_step5d_profile)"
    if [[ -z "${BRIDGE_PROFILE}" ]]; then
      echo "refusing Step5d alias: current_stage does not name a controller-readback-verified Step5d live-prep package"
      exit 40
    fi
    ;;
  5c-dry|5c-dryrun|step5c-dryrun|step5c_speedj_dryrun_v1|speedj-dryrun|speedj_dryrun)
    echo "refusing Step5c dry-run alias: DLS/Jacobian mapping is quarantined after wrong XY/Z live motion"
    exit 40
    ;;
  5c|5c-contact|step5c|step5c-contact|step5c_joint_rnn_cycloid_v1|joint-rnn-cycloid|joint_rnn_cycloid)
    echo "refusing Step5c contact alias: strict RNN is not implemented; use step5c-speedj-dryrun only"
    exit 40
    ;;
  6b|step6b|step6b_v1|contact-eight|contact_eight)
    BRIDGE_PROFILE="step6b_v1"
    ;;
  6b-v2|step6b-v2|step6b_v2|contact-eight-v2|contact_eight_v2)
    BRIDGE_PROFILE="step6b_v2"
    ;;
esac
BRIDGE_DURATION_S="${BRIDGE_DURATION_S:-${STEP5D_DURATION_S:-180}}"
BRIDGE_BASELINE_S="${BRIDGE_BASELINE_S:-${STEP5D_BASELINE_S:-5}}"
BRIDGE_REZERO_S="${BRIDGE_REZERO_S:-${STEP5D_REZERO_S:-1}}"
BRIDGE_RTDE_HZ="${BRIDGE_RTDE_HZ:-${STEP5D_RTDE_HZ:-500}}"
BRIDGE_SENSOR_STALE_S="${BRIDGE_SENSOR_STALE_S:-${STEP5D_SENSOR_STALE_S:-0.10}}"
BRIDGE_SOCKET_TIMEOUT_S="${BRIDGE_SOCKET_TIMEOUT_S:-${STEP5D_SOCKET_TIMEOUT_S:-0.0}}"
BRIDGE_BACKGROUND_PUSH_AFTER_LIVE="${BRIDGE_BACKGROUND_PUSH_AFTER_LIVE:-0}"
BRIDGE_NORMAL_COMMAND_SIGN="${BRIDGE_NORMAL_COMMAND_SIGN:-1}"
if [[ "${BRIDGE_PROFILE}" == "step5d_strict_rnn_liveprep_v18" || "${BRIDGE_PROFILE}" == "step5d_strict_rnn_liveprep_v19" || "${BRIDGE_PROFILE}" == "step5d_strict_rnn_liveprep_v20" || "${BRIDGE_PROFILE}" == "step5d_strict_rnn_liveprep_v21" || "${BRIDGE_PROFILE}" == "step5d_strict_rnn_liveprep_v22" || "${BRIDGE_PROFILE}" == "step5d_strict_rnn_liveprep_v23" ]]; then
  MAX_NORMAL_FORCE_N="${MAX_NORMAL_FORCE_N:-100}"
  MAX_FORCE_NORM_N="${MAX_FORCE_NORM_N:-100}"
  MAX_TORQUE_NORM_NM="${MAX_TORQUE_NORM_NM:-4.0}"
else
  MAX_NORMAL_FORCE_N="${MAX_NORMAL_FORCE_N:-50}"
  MAX_FORCE_NORM_N="${MAX_FORCE_NORM_N:-60}"
  MAX_TORQUE_NORM_NM="${MAX_TORQUE_NORM_NM:-3.0}"
fi
BRIDGE_ORIENTATION_GAIN="${BRIDGE_ORIENTATION_GAIN:-0.20}"
BRIDGE_ORIENTATION_WX_SIGN="${BRIDGE_ORIENTATION_WX_SIGN:-1}"
BRIDGE_ORIENTATION_WY_SIGN="${BRIDGE_ORIENTATION_WY_SIGN:-1}"
BRIDGE_LINE_SPEED_M_S="${BRIDGE_LINE_SPEED_M_S:-0.003}"
BRIDGE_LINE_SETTLE_S="${BRIDGE_LINE_SETTLE_S:-0.0}"
BRIDGE_STAGE25_ONLY="${BRIDGE_STAGE25_ONLY:-0}"
if [[ "${BRIDGE_PROFILE}" == "step5d_strict_rnn_liveprep_v18" || "${BRIDGE_PROFILE}" == "step5d_strict_rnn_liveprep_v19" || "${BRIDGE_PROFILE}" == "step5d_strict_rnn_liveprep_v20" || "${BRIDGE_PROFILE}" == "step5d_strict_rnn_liveprep_v21" || "${BRIDGE_PROFILE}" == "step5d_strict_rnn_liveprep_v22" || "${BRIDGE_PROFILE}" == "step5d_strict_rnn_liveprep_v23" ]]; then
  BRIDGE_TARGET_FORCE_N="${BRIDGE_TARGET_FORCE_N:-${STEP4E_TARGET_FORCE_N:-12.0}}"
else
  BRIDGE_TARGET_FORCE_N="${BRIDGE_TARGET_FORCE_N:-${STEP4E_TARGET_FORCE_N:-5}}"
fi
BRIDGE_MOTION_LIMIT_M_S="${BRIDGE_MOTION_LIMIT_M_S:-0.004}"
BRIDGE_TOTAL_LINEAR_LIMIT_M_S="${BRIDGE_TOTAL_LINEAR_LIMIT_M_S:-0.006}"
BRIDGE_NORMAL_VELOCITY_LIMIT_M_S="${BRIDGE_NORMAL_VELOCITY_LIMIT_M_S:-0.003}"
BRIDGE_FORCE_P_GAIN="${BRIDGE_FORCE_P_GAIN:-0.0007}"
BRIDGE_FORCE_I_GAIN="${BRIDGE_FORCE_I_GAIN:-0.00008}"
BRIDGE_FORCE_DAMPING="${BRIDGE_FORCE_DAMPING:-0.35}"
BRIDGE_INTEGRAL_LIMIT_N_S="${BRIDGE_INTEGRAL_LIMIT_N_S:-10.0}"
BRIDGE_REACQUIRE_VELOCITY_M_S="${BRIDGE_REACQUIRE_VELOCITY_M_S:-0.001}"
BRIDGE_ANGULAR_LIMIT_RAD_S="${BRIDGE_ANGULAR_LIMIT_RAD_S:-0.015}"
STEP5C_QDOT_LIMIT_RAD_S="${STEP5C_QDOT_LIMIT_RAD_S:-0.15}"
STEP5C_JOINT_DAMPING="${STEP5C_JOINT_DAMPING:-0.0001}"
STEP5C_JOINT_MODEL="${STEP5C_JOINT_MODEL:-/home/andy/ur10e_ros2_ws/experiments/archive/legacy/tase-mujoco-reproduction-2026-05-23/assets/mjcf/ur10e_nominal.xml}"
STEP5C_JOINT_SITE="${STEP5C_JOINT_SITE:-tcp_site_unverified_85mm}"
BRIDGE_NORMAL_FOLLOW_MODE="${BRIDGE_NORMAL_FOLLOW_MODE:-}"
BRIDGE_NORMAL_FILTER_TAU_S="${BRIDGE_NORMAL_FILTER_TAU_S:-0.35}"
BRIDGE_NORMAL_FILTER_ALPHA="${BRIDGE_NORMAL_FILTER_ALPHA:-0.35}"
BRIDGE_NORMAL_MAX_RATE_RAD_S="${BRIDGE_NORMAL_MAX_RATE_RAD_S:-0.010}"
BRIDGE_NORMAL_MIN_FORCE_N="${BRIDGE_NORMAL_MIN_FORCE_N:-2.0}"
BRIDGE_NORMAL_MAX_ANGLE_FROM_LATCH_DEG="${BRIDGE_NORMAL_MAX_ANGLE_FROM_LATCH_DEG:-20}"
BRIDGE_NORMAL_FRICTION_PROJECTION="${BRIDGE_NORMAL_FRICTION_PROJECTION:-on}"
BRIDGE_PATH_SHAPE="${BRIDGE_PATH_SHAPE:-line}"
if [[ "${BRIDGE_PROFILE}" == "step5c_speedj_dryrun_v1" ]]; then
  echo "refusing BRIDGE_PROFILE=step5c_speedj_dryrun_v1: DLS/Jacobian mapping is quarantined after wrong XY/Z live motion"
  exit 40
fi
if [[ "${BRIDGE_PROFILE}" == "step5c_joint_rnn_cycloid_v1" ]]; then
  echo "refusing BRIDGE_PROFILE=step5c_joint_rnn_cycloid_v1: strict RNN is not implemented; contact is quarantined"
  exit 40
fi
if [[ "${BRIDGE_PROFILE}" == "step4f_v1" ]]; then
  BRIDGE_PATH_SHAPE="cycloid"
elif [[ "${BRIDGE_PROFILE}" == "step4g_v1" ]]; then
  BRIDGE_PATH_SHAPE="eight"
elif [[ "${BRIDGE_PROFILE}" == "step5b_v1" ]]; then
  BRIDGE_PATH_SHAPE="cycloid"
elif [[ "${BRIDGE_PROFILE}" == "step5c_speedj_dryrun_v1" || "${BRIDGE_PROFILE}" == "step5c_joint_rnn_cycloid_v1" || "${BRIDGE_PROFILE}" == step5d_strict_rnn_liveprep_v* ]]; then
  BRIDGE_PATH_SHAPE="cycloid"
elif [[ "${BRIDGE_PROFILE}" == "step6b_v1" || "${BRIDGE_PROFILE}" == "step6b_v2" ]]; then
  BRIDGE_PATH_SHAPE="eight"
fi
if [[ -z "${BRIDGE_NORMAL_FOLLOW_MODE}" ]]; then
  if [[ "${BRIDGE_PROFILE}" == "v30" || "${BRIDGE_PROFILE}" == "v31" || "${BRIDGE_PROFILE}" == "step4f_v1" || "${BRIDGE_PROFILE}" == "step4g_v1" || "${BRIDGE_PROFILE}" == "step5b_v1" || "${BRIDGE_PROFILE}" == "step5c_joint_rnn_cycloid_v1" || "${BRIDGE_PROFILE}" == step5d_strict_rnn_liveprep_v* || "${BRIDGE_PROFILE}" == "step6b_v1" || "${BRIDGE_PROFILE}" == "step6b_v2" ]]; then
    BRIDGE_NORMAL_FOLLOW_MODE="filtered_live"
  else
    BRIDGE_NORMAL_FOLLOW_MODE="locked"
  fi
fi

PROGRAM_PREVIEW="/programs/andyl/kunwei/step4/step4e_preview_line_${BRIDGE_PROFILE}.urp"
PROGRAM_HOLD="/programs/andyl/kunwei/step4/step4e_contact_hold_line_${BRIDGE_PROFILE}.urp"
PROGRAM_LINE="/programs/andyl/kunwei/step4/step4e_line_outerloop_${BRIDGE_PROFILE}.urp"
PROGRAM_GEO="/programs/andyl/kunwei/step4/step4e_ball_first_contact_p0_v1.urp"
PROGRAM_WITNESS="/programs/andyl/kunwei/step4/step4e_ball_vs_cyl_contact_p0_v1.urp"
PROGRAM_AXIS_ISO="/programs/andyl/kunwei/step4/step4e_attitude_axis_iso_v1.urp"
if [[ "${BRIDGE_PROFILE}" == "v21" ]]; then
  PROGRAM_LINE="/programs/andyl/kunwei/step4/step4e/step4e_detached_movel_minrot_v21.urp"
fi
if [[ "${BRIDGE_PROFILE}" == "v22" ]]; then
  PROGRAM_LINE="/programs/andyl/kunwei/step4/step4e/step4e_seed_normal_loop_v22.urp"
fi
if [[ "${BRIDGE_PROFILE}" == "v23" ]]; then
  PROGRAM_LINE="/programs/andyl/kunwei/step4/step4e/step4e_seed_normal_loop_v23.urp"
fi
if [[ "${BRIDGE_PROFILE}" == "v24" ]]; then
  PROGRAM_LINE="/programs/andyl/kunwei/step4/step4e/step4e_seed_normal_loop_v24.urp"
fi
if [[ "${BRIDGE_PROFILE}" == "v25" ]]; then
  PROGRAM_LINE="/programs/andyl/kunwei/step4/step4e/step4e_seed_normal_loop_v25.urp"
fi
if [[ "${BRIDGE_PROFILE}" == "v26" ]]; then
  PROGRAM_LINE="/programs/andyl/kunwei/step4/step4e/step4e_seed_normal_loop_v26.urp"
fi
if [[ "${BRIDGE_PROFILE}" == "v27" ]]; then
  PROGRAM_LINE="/programs/andyl/kunwei/step4/step4e/step4e_seed_normal_loop_v27.urp"
fi
if [[ "${BRIDGE_PROFILE}" == "v28" ]]; then
  PROGRAM_LINE="/programs/andyl/kunwei/step4/step4e_seed_normal_loop_v28.urp"
fi
if [[ "${BRIDGE_PROFILE}" == "v29" ]]; then
  PROGRAM_LINE="/programs/andyl/kunwei/step4/step4e_seed_normal_loop_v29.urp"
fi
if [[ "${BRIDGE_PROFILE}" == "v30" ]]; then
  PROGRAM_LINE="/programs/andyl/kunwei/step4/step4e_seed_normal_loop_v30.urp"
fi
if [[ "${BRIDGE_PROFILE}" == "v31" ]]; then
  PROGRAM_LINE="/programs/andyl/kunwei/step4/step4e_seed_normal_loop_v31.urp"
fi
if [[ "${BRIDGE_PROFILE}" == "step4f_v1" ]]; then
  PROGRAM_LINE="/programs/andyl/kunwei/step4/step4f_cycloid_seed_normal_v1.urp"
fi
if [[ "${BRIDGE_PROFILE}" == "step4g_v1" ]]; then
  PROGRAM_LINE="/programs/andyl/kunwei/step4/step4g_eight_seed_normal_v1.urp"
fi
if [[ "${BRIDGE_PROFILE}" == "step5b_v1" ]]; then
  PROGRAM_LINE="/programs/andyl/kunwei/step5/step5b_contact_cycloid_baseline_v1.urp"
fi
if [[ "${BRIDGE_PROFILE}" == "step5c_speedj_dryrun_v1" ]]; then
  PROGRAM_LINE="/programs/andyl/kunwei/step5/step5c_speedj_dryrun_v1.urp"
fi
if [[ "${BRIDGE_PROFILE}" == "step5c_joint_rnn_cycloid_v1" ]]; then
  PROGRAM_LINE="/programs/andyl/kunwei/step5/step5c_joint_rnn_cycloid_v1.urp"
fi
if [[ "${BRIDGE_PROFILE}" == "step5d_strict_rnn_liveprep_v1" || "${BRIDGE_PROFILE}" == "step5d_strict_rnn_liveprep_v2" || "${BRIDGE_PROFILE}" == "step5d_strict_rnn_liveprep_v3" || "${BRIDGE_PROFILE}" == "step5d_strict_rnn_liveprep_v4" || "${BRIDGE_PROFILE}" == "step5d_strict_rnn_liveprep_v5" || "${BRIDGE_PROFILE}" == "step5d_strict_rnn_liveprep_v6" || "${BRIDGE_PROFILE}" == "step5d_strict_rnn_liveprep_v7" || "${BRIDGE_PROFILE}" == "step5d_strict_rnn_liveprep_v8" || "${BRIDGE_PROFILE}" == "step5d_strict_rnn_liveprep_v9" ]]; then
  PROGRAM_LINE="/programs/andyl/kunwei/step5/step5d/${BRIDGE_PROFILE}.urp"
fi
if [[ "${BRIDGE_PROFILE}" == "step5d_strict_rnn_liveprep_v10" || "${BRIDGE_PROFILE}" == "step5d_strict_rnn_liveprep_v11" ]]; then
  PROGRAM_LINE="/programs/andyl/kunwei/step5/step5d/${BRIDGE_PROFILE}.urp"
fi
if [[ "${BRIDGE_PROFILE}" == "step5d_strict_rnn_liveprep_v12" || "${BRIDGE_PROFILE}" == "step5d_strict_rnn_liveprep_v13" || "${BRIDGE_PROFILE}" == "step5d_strict_rnn_liveprep_v14" || "${BRIDGE_PROFILE}" == "step5d_strict_rnn_liveprep_v15" || "${BRIDGE_PROFILE}" == "step5d_strict_rnn_liveprep_v15a" || "${BRIDGE_PROFILE}" == "step5d_strict_rnn_liveprep_v16" || "${BRIDGE_PROFILE}" == "step5d_strict_rnn_liveprep_v17" || "${BRIDGE_PROFILE}" == "step5d_strict_rnn_liveprep_v18" || "${BRIDGE_PROFILE}" == "step5d_strict_rnn_liveprep_v19" || "${BRIDGE_PROFILE}" == "step5d_strict_rnn_liveprep_v20" || "${BRIDGE_PROFILE}" == "step5d_strict_rnn_liveprep_v21" || "${BRIDGE_PROFILE}" == "step5d_strict_rnn_liveprep_v22" || "${BRIDGE_PROFILE}" == "step5d_strict_rnn_liveprep_v23" ]]; then
  PROGRAM_LINE="/programs/andyl/kunwei/step5/${BRIDGE_PROFILE}.urp"
fi
if [[ "${BRIDGE_PROFILE}" == "step6b_v1" ]]; then
  PROGRAM_LINE="/programs/andyl/kunwei/step6/step6b_contact_eight_baseline_v1.urp"
fi
if [[ "${BRIDGE_PROFILE}" == "step6b_v2" ]]; then
  PROGRAM_LINE="/programs/andyl/kunwei/step6/step6b_contact_eight_baseline_v2.urp"
fi
if [[ "${BRIDGE_PROFILE}" == "p0_geo_v1" ]]; then
  SEARCH_DESCRIPTION="P0-geo ball-first contact witness: vertical TCP entry, far 15 mm/s until 80 mm depth, then near 3 mm/s until first 1-1.5 N contact or 92 mm max depth; after contact it holds still for visual confirmation, retracts base-Z 2 mm, and never runs attitude, 5N acquisition, or line motion"
elif [[ "${BRIDGE_PROFILE}" == "p0_ball_vs_cyl_v1" ]]; then
  SEARCH_DESCRIPTION="P0 witness pair: ball pose first, then KSM-8N housing/cylindrical-face pose; each uses low-threshold 1-1.5 N contact, 8 s visual dwell, and no attitude, 5N acquisition, or line motion"
elif [[ "${BRIDGE_PROFILE}" == "v22" ]]; then
  SEARCH_DESCRIPTION="seed-normal TASE minimal loop: move once to the measured near-normal TCP pose, far/near search at 5/3 mm/s, latch first contact normal, lift 50 mm, optionally apply the bridge target rotvec when orientation error exceeds 10 deg, re-search up to 70 mm, acquire 5 N, then run the XY line"
elif [[ "${BRIDGE_PROFILE}" == "v23" ]]; then
  SEARCH_DESCRIPTION="failed/archive v23 seed-normal loop: high-Z near-normal orientation, path-start XY at high Z, fixed search-start z=98.35 mm, first far/near search, latch contact normal, lift 50 mm, angular speedl posture correction; archived after stop register 13 at stage 25.2"
elif [[ "${BRIDGE_PROFILE}" == "v24" ]]; then
  SEARCH_DESCRIPTION="evidence v24 Step4e/TASE flow: one-step entry XY plus target attitude at current Z, fixed first far/near transition, first-contact latch, lift 30 mm, angular speedl posture correction with 37..39 forced zero, second search, acquire 5 N, then run the XY line"
elif [[ "${BRIDGE_PROFILE}" == "v25" ]]; then
  SEARCH_DESCRIPTION="failed/archive v25 Step4e/TASE flow: one-step entry XY plus target attitude at current Z, dynamic first far/near search using the wrong target initial Z datum, first-contact latch, lift 30 mm, angular speedl posture correction, second search, acquire 5 N, then run the XY line"
elif [[ "${BRIDGE_PROFILE}" == "v26" ]]; then
  SEARCH_DESCRIPTION="failed/archive v26 Step4e/TASE flow: one-step entry XY plus target attitude at current Z, but first far/near search used the reference path contact-start Z instead of measured first-contact Z, causing near search to start too high and depth-limit before force jump"
elif [[ "${BRIDGE_PROFILE}" == "v27" ]]; then
  SEARCH_DESCRIPTION="failed/archive v27 Step4e/TASE flow: one-step entry XY plus target attitude at current Z, first far/near search using the v13/v16 force-jump first-contact Z evidence so near starts about 30 mm above first contact; archived after first-contact latch cmd_valid timeout stop register 12 at stage 25.05"
elif [[ "${BRIDGE_PROFILE}" == "v28" ]]; then
  SEARCH_DESCRIPTION="failed/archive v28 Step4e/TASE flow: v28 reached first contact and stage 25.05, but the active bridge runtime did not recognize v28 as an angular-speedl profile, so cmd_valid stayed 0"
elif [[ "${BRIDGE_PROFILE}" == "v29" ]]; then
  SEARCH_DESCRIPTION="current Step4e/TASE flow from STEP4E_FLOW.md: bridge-profile-fix + line-entry-gate release; one-step entry XY plus target attitude at current Z, first far/near search using the v13/v16 force-jump first-contact Z evidence so near starts about 20 mm above first contact, near descent 2.5 mm/s, raw-normal guard 50 N, first-contact latch, lift 20 mm, angular speedl posture correction after 37..39 settle to zero, second search, zero-linear 25.3 gate, then run the XY line"
elif [[ "${BRIDGE_PROFILE}" == "v30" ]]; then
  SEARCH_DESCRIPTION="previous Step4e/TASE conservative filtered-live-normal flow: same TP motion/search/25.2/25.3 line-entry gate as v29, but the bridge uses a gated filtered live normal only during 25.0 line control; force/admittance gains and tangential speed remain unchanged"
elif [[ "${BRIDGE_PROFILE}" == "v31" ]]; then
  SEARCH_DESCRIPTION="current Step4e/TASE simple-alpha-normal-follow flow: same TP motion/search/25.2/25.3 line-entry gate as v30, but the bridge uses direct alpha EMA filtered live normal only during 25.0 line control; alpha=0.35, min-force=2.0 N, force/admittance gains and tangential speed remain unchanged"
elif [[ "${BRIDGE_PROFILE}" == "step4f_v1" ]]; then
  SEARCH_DESCRIPTION="Step4f/TASE Experiment #1 cycloid small-surface reproduction: same v31 TP motion/search/25.2/25.3 line-entry gate and filtered-live normal loop, but stage 25.0 follows the paper cycloid XY reference for 60 s"
elif [[ "${BRIDGE_PROFILE}" == "step4g_v1" ]]; then
  SEARCH_DESCRIPTION="Step4g/TASE Experiment #2 8-shaped small-surface reproduction: same v31 TP motion/search/25.2/25.3 line-entry gate and filtered-live normal loop, but stage 25.0 follows the paper 8-shaped XY reference for 60 s"
elif [[ "${BRIDGE_PROFILE}" == "step5b_v1" ]]; then
  SEARCH_DESCRIPTION="Step5b contact cycloid baseline: same v31 contact search, first-contact normal latch, 20 mm lift, 25.2 attitude correction, second contact, and 25.3 line-entry gate; stage 25.0 follows the active Step5 table contact cycloid reference for 60 s with filtered-live normal"
elif [[ "${BRIDGE_PROFILE}" == "step5c_speedj_dryrun_v1" ]]; then
  SEARCH_DESCRIPTION="Step5c speedj dry-run: no-contact short Step5 cycloid subset; bridge reads actual_q and writes qd0..qd5 in registers 37..42; TP executes speedj and no contact search"
elif [[ "${BRIDGE_PROFILE}" == "step5c_joint_rnn_cycloid_v1" ]]; then
  SEARCH_DESCRIPTION="Step5c joint-space contact cycloid: same Step5b contact scaffold and filtered-live normal, but bridge solves bounded qdot from actual_q and TP executes speedj in Stage25 joint-control windows"
elif [[ "${BRIDGE_PROFILE}" == "step5d_strict_rnn_liveprep_v1" ]]; then
  SEARCH_DESCRIPTION="Step5d v1 live-prep evidence route: same Step5b contact scaffold, strict RNN qdot bridge, and Stage25 speedj executor; archived after 2026-06-14 entered Stage25 for 0.106 s then stopped from heartbeat stale"
elif [[ "${BRIDGE_PROFILE}" == "step5d_strict_rnn_liveprep_v2" ]]; then
  SEARCH_DESCRIPTION="Step5d v2 live-prep evidence route: same Step5b contact scaffold, skips lift/25.2 when first-contact orientation error is <=3 deg, warms strict RNN/Pinocchio before Stage25, then TP executes speedj qdot registers 37..42; superseded by v3 after v2 over-contact evidence"
elif [[ "${BRIDGE_PROFILE}" == "step5d_strict_rnn_liveprep_v3" ]]; then
  SEARCH_DESCRIPTION="Step5d v3 live-prep route: same strict RNN speedj path, but 25.3 must settle near 5N before Stage25, bridge limits live xdot_c before the RNN, and raw/force-norm hard guards are 100N"
elif [[ "${BRIDGE_PROFILE}" == "step5d_strict_rnn_liveprep_v5" ]]; then
  SEARCH_DESCRIPTION="Retained Step5d v5 route: semantic-fix strict RNN speedj path with Step5b/Step6b force-frame contract; live v5 reached 25.0 but the 2-15N engage gate blocked qdot at about 17-21N"
elif [[ "${BRIDGE_PROFILE}" == "step5d_strict_rnn_liveprep_v6" ]]; then
  SEARCH_DESCRIPTION="Retained Step5d v6 route: strict RNN speedj path with Step5b/Step6b force-frame contract and widened 2-40N window; live v6 entered 25.0 but exposed 24.3 re-contact overpressure at about 20-22N"
elif [[ "${BRIDGE_PROFILE}" == "step5d_strict_rnn_liveprep_v7" ]]; then
  SEARCH_DESCRIPTION="Retained Step5d v7 live-prep route: strict RNN speedj path with Step5b/Step6b force-frame contract, 4deg lift skip, slow-only 24.3/24.4 re-contact search, restored 2-15N contact window, semantic 25.0 orientation consistency gate, and 100N hard guards"
elif [[ "${BRIDGE_PROFILE}" == "step5d_strict_rnn_liveprep_v9" ]]; then
  SEARCH_DESCRIPTION="Retained Step5d v9 failure evidence: Stage 25.3 bridge force-PID settle fixed low-load dropout but hunted under point contact and timed out before Stage 25.0"
elif [[ "${BRIDGE_PROFILE}" == "step5d_strict_rnn_liveprep_v10" ]]; then
  SEARCH_DESCRIPTION="Retained Step5d v10 failure evidence: Stage 25.3 bridge admittance settle softened v9 direct PID but still saturated/flipped under point contact and did not satisfy the 3-8N plus settle-speed release gate"
elif [[ "${BRIDGE_PROFILE}" == "step5d_strict_rnn_liveprep_v11" ]]; then
  SEARCH_DESCRIPTION="Retained Step5d v11 incomplete evidence: Stage 25.3 deadband acquire released into Stage 25.0, but the live run ended incomplete with Dashboard PAUSED and Safetymode ROBOT_EMERGENCY_STOP"
elif [[ "${BRIDGE_PROFILE}" == "step5d_strict_rnn_liveprep_v12" ]]; then
  SEARCH_DESCRIPTION="Step5d v12 guarded live-prep: keeps v11 deadband acquire, then Stage25 strict RNN speedj uses 0.05 rad/s qdot cap plus bridge lost-contact, contact-window, TCP-speed, and qdot-slew guards"
elif [[ "${BRIDGE_PROFILE}" == "step5d_strict_rnn_liveprep_v13" ]]; then
  SEARCH_DESCRIPTION="Step5d v13 contact-safety live-prep: keeps v11 deadband acquire and v12 0.05 rad/s qdot cap/slew, then Stage25 holds cmd_valid=1 with zero qdot on benign low load, freezes path time, immediately stops predicted TCP speed danger, and stops actual TCP speed danger after 0.004s dwell"
elif [[ "${BRIDGE_PROFILE}" == "step5d_strict_rnn_liveprep_v14" ]]; then
  SEARCH_DESCRIPTION="Step5d v14 contact-safety live-prep: v13 plus actual-speed dwell first-sample zero-qdot hold, controller read-back gate, 50/60N TP hard guards, and 25N Stage25.3 recovery force stop"
elif [[ "${BRIDGE_PROFILE}" == "step5d_strict_rnn_liveprep_v15" ]]; then
  SEARCH_DESCRIPTION="Retained Step5d v15 controller-readback evidence with audit gaps: online cage and bounded hold counters were incomplete; superseded by v15a before any bridge run"
elif [[ "${BRIDGE_PROFILE}" == "step5d_strict_rnn_liveprep_v15a" ]]; then
  SEARCH_DESCRIPTION="Retained Step5d v15a live-run evidence: online broad AABB TCP cage/braking margin and bounded zero-qdot hold/reacquire stopped by hold_duty_limit on 2026-06-16; no current retry authorization"
elif [[ "${BRIDGE_PROFILE}" == "step5d_strict_rnn_liveprep_v16" ]]; then
  SEARCH_DESCRIPTION="Retained Step5d v16 live-run evidence: Step5b v3 no-lift/no-25.2/no-second-search scaffold, 5-20N entry window, stopped by hold_duty_limit"
elif [[ "${BRIDGE_PROFILE}" == "step5d_strict_rnn_liveprep_v17" ]]; then
  SEARCH_DESCRIPTION="Retained Step5d v17 live-run evidence: Step5b v3 no-lift/no-25.2/no-second-search scaffold, 8-18N filtered preload, stopped by contact-safety hold_duty_limit"
elif [[ "${BRIDGE_PROFILE}" == "step5d_strict_rnn_liveprep_v18" ]]; then
  SEARCH_DESCRIPTION="Retained Step5d v18 cage-primary diagnostic evidence: Step5b v3 no-lift/no-25.2/no-second-search scaffold, 8-18N filtered preload with 7.5-19N raw sanity, stopped by predicted TCP speed during active reacquire"
elif [[ "${BRIDGE_PROFILE}" == "step5d_strict_rnn_liveprep_v19" ]]; then
  SEARCH_DESCRIPTION="Retained Step5d v19 cage-primary diagnostic evidence: v19 kept 12N, 8-13N filtered preload, 7.5-14N raw sanity, Stage22/24 1.5x speedups, and a freeze_low_force active-reacquire speed cap, but live v19 still stopped on predicted TCP speed during locked-normal settle"
elif [[ "${BRIDGE_PROFILE}" == "step5d_strict_rnn_liveprep_v20" ]]; then
  SEARCH_DESCRIPTION="Current Step5d v20 cage-primary diagnostic: Step5b v3 no-lift/no-25.2/no-second-search scaffold, Stage22/24 gravity-down [pi,0,0] pre-contact search posture, Stage22 entry movel 0.060 m/s, Stage24 far search 0.0225 m/s, 8-13N filtered preload with 7.5-14N raw sanity, 10s strict RNN speedj diagnostic, 100N/4Nm hard sensor guards, and low-load active-reacquire predicted-speed cap at 0.035 m/s inside the online TCP cage"
elif [[ "${BRIDGE_PROFILE}" == "step5d_strict_rnn_liveprep_v21" ]]; then
  SEARCH_DESCRIPTION="Retained Step5d v21 failure evidence: bridge-time preload override could persist into Stage25.0 qdot registers and trigger stop_reason=13"
elif [[ "${BRIDGE_PROFILE}" == "step5d_strict_rnn_liveprep_v22" ]]; then
  SEARCH_DESCRIPTION="Retained Step5d v22 failure evidence: Stage25.95 qdot register clear barrier existed but used running qdot cap tolerance, and live v22 exposed post-RNN normal-direction over-load"
elif [[ "${BRIDGE_PROFILE}" == "step5d_strict_rnn_liveprep_v23" ]]; then
  SEARCH_DESCRIPTION="Current Step5d v23 cage-primary diagnostic: v22 gravity-down/preload/cage behavior retained, Stage25.95 near-zero qdot register clear barrier, post-RNN normal-direction guard, RNN qdot diagnostics, 10s strict RNN speedj diagnostic, 100N/4Nm sensor hard guards plus operational over-load stop"
elif [[ "${BRIDGE_PROFILE}" == "step5d_strict_rnn_liveprep_v8" ]]; then
  SEARCH_DESCRIPTION="Retained Step5d v8 failure evidence: Stage 25.3 bridge force-PID settle used Cartesian registers 37..39 but low-load dropout below 0.5N could stop with reason 17 before Stage 25.0"
elif [[ "${BRIDGE_PROFILE}" == "step5d_strict_rnn_liveprep_v4" ]]; then
  SEARCH_DESCRIPTION="Archived Step5d v4 route: tolerant 2-15N contact window, retained as evidence for the outer-loop force/frame sign bug"
elif [[ "${BRIDGE_PROFILE}" == "step6b_v1" ]]; then
  SEARCH_DESCRIPTION="Step6b contact 8-shaped baseline: same v31/Step5b contact search, first-contact normal latch, 20 mm lift, 25.2 attitude correction, second contact, and 25.3 line-entry gate; stage 25.0 follows the Step6 five-point safe-frame 8-shaped reference for 30 s with filtered-live normal"
elif [[ "${BRIDGE_PROFILE}" == "step6b_v2" ]]; then
  SEARCH_DESCRIPTION="Step6b v2 contact 8-shaped baseline: same v31/Step5b contact scaffold and Step6 reference, but bridge caps are 15 mm/s path, 15 mm/s total linear, 3 mm/s normal reserve, and 0.060 rad/s attitude"
elif [[ "${BRIDGE_PROFILE}" == "v20" ]]; then
  SEARCH_DESCRIPTION="two-stage search: v20 moves directly to entry XY with vertical TCP orientation [pi,0,0], searches far 15 mm/s then near 3 mm/s, latches first-contact normal only, lifts base-Z 2 mm, aligns attitude while detached, reacquires 5 N along the locked normal, then runs the 5 mm/s XY line"
elif [[ "${BRIDGE_PROFILE}" == "v21" ]]; then
  SEARCH_DESCRIPTION="two-stage search: v21 moves to vertical TCP orientation, searches far 15 mm/s for 80 mm max, then near 3 mm/s to 92 mm max depth, latches first-contact normal only, detaches along the locked normal, then previews the minimal-rotation target without executing contact-posture motion; no 5N acquisition and no XY line"
elif [[ "${BRIDGE_PROFILE}" == "v19" ]]; then
  SEARCH_DESCRIPTION="two-stage search: v19 first moves TCP orientation to vertical [pi,0,0], then XY entry and downward speedl-search; far 15 mm/s for 130 mm then near 3 mm/s up to 150 mm max depth; after contact latch it holds 5 N point contact and aligns TCP z to the contact normal before 5 mm/s XY line motion"
elif [[ "${BRIDGE_PROFILE}" == "v18" ]]; then
  SEARCH_DESCRIPTION="two-stage search: v18 first moves TCP orientation to vertical [pi,0,0], then XY entry and downward speedl-search; far 15 mm/s for 130 mm then near 3 mm/s up to 150 mm max depth; after contact latch it holds 5 N point contact and aligns TCP z to the contact normal before XY line motion"
elif [[ "${BRIDGE_PROFILE}" == "v17" ]]; then
  SEARCH_DESCRIPTION="two-stage search: v17 first moves TCP orientation to vertical [pi,0,0], then XY entry and downward speedl-search; far 15 mm/s for 130 mm then near 3 mm/s up to 150 mm max depth; bridge attitude outer-loop starts after contact latch"
elif [[ "${BRIDGE_PROFILE}" == "v16" ]]; then
  SEARCH_DESCRIPTION="two-stage search: v16 removes fixed-Z pre-search movel; after XY entry it directly speedl-searches downward, far 15 mm/s for 130 mm then near 3 mm/s up to 150 mm max depth; bridge normal/reacquire limit = 2 mm/s"
elif [[ "${BRIDGE_PROFILE}" == "v15" ]]; then
  SEARCH_DESCRIPTION="two-stage search: v15 keeps v14 force-safe line settings and adds stage25 command-valid grace to avoid first-cycle RTDE/URScript skew"
elif [[ "${BRIDGE_PROFILE}" == "v14" ]]; then
  SEARCH_DESCRIPTION="two-stage search: first move to fixed validated search-start z=98.35 mm, then far 15 mm/s for 80 mm, near 3 mm/s for final 12 mm, 92 mm max depth; v14 starts admittance integration only in stage25 and settles normal force before line motion"
elif [[ "${BRIDGE_PROFILE}" == "v13" ]]; then
  SEARCH_DESCRIPTION="two-stage search: first move to fixed validated search-start z=98.35 mm, then far 15 mm/s for 80 mm, near 3 mm/s for final 12 mm, 92 mm max depth; v13 fixes v12 high-start no-contact miss"
elif [[ "${BRIDGE_PROFILE}" == "v12" ]]; then
  SEARCH_DESCRIPTION="two-stage search: far 15 mm/s for 80 mm, then near 3 mm/s for the final 12 mm, 92 mm max depth; v12 keeps v11 attitude signs and fixes endpoint success/retract"
elif [[ "${BRIDGE_PROFILE}" == "v11" ]]; then
  SEARCH_DESCRIPTION="two-stage search: far 15 mm/s for 80 mm, then near 3 mm/s for the final 12 mm, 92 mm max depth; v11 keeps v10 force guards and uses independent attitude signs wx=+1, wy=-1"
elif [[ "${BRIDGE_PROFILE}" == "v10" ]]; then
  SEARCH_DESCRIPTION="two-stage search: far 15 mm/s for 80 mm, then near 3 mm/s for the final 12 mm, 92 mm max depth; v10 keeps v9 force guards and reverses the attitude outer-loop direction"
elif [[ "${BRIDGE_PROFILE}" == "v9" ]]; then
  SEARCH_DESCRIPTION="two-stage search: far 15 mm/s for 80 mm, then near 3 mm/s for the final 12 mm, 92 mm max depth; v9 keeps v8 latched-normal line control and raises raw normal guard to 100N / torque guard to 1.0Nm"
elif [[ "${BRIDGE_PROFILE}" == "v8" ]]; then
  SEARCH_DESCRIPTION="two-stage search: far 15 mm/s for 80 mm, then near 3 mm/s for the final 12 mm, 92 mm max depth; v8 keeps v7 stopl(0.1) and uses latched-normal 5N line control"
elif [[ "${BRIDGE_PROFILE}" == "v7" ]]; then
  SEARCH_DESCRIPTION="two-stage search: far 15 mm/s for 80 mm, then near 3 mm/s for the final 12 mm, 92 mm max depth; v7 keeps v6 normal command sign/30N guard and uses stopl(0.1)"
elif [[ "${BRIDGE_PROFILE}" == "v6" ]]; then
  SEARCH_DESCRIPTION="two-stage search: far 15 mm/s for 80 mm, then near 3 mm/s for the final 12 mm, 92 mm max depth; v6 flips normal command sign to unload after contact"
elif [[ "${BRIDGE_PROFILE}" == "v5" ]]; then
  SEARCH_DESCRIPTION="two-stage search: far 15 mm/s for 80 mm, then near 3 mm/s for the final 12 mm, 92 mm max depth"
elif [[ "${BRIDGE_PROFILE}" == "v4" ]]; then
  SEARCH_DESCRIPTION="two-stage search: far 10 mm/s for 80 mm, then near 3 mm/s for the final 10 mm, 90 mm max depth"
elif [[ "${BRIDGE_PROFILE}" == "v3" ]]; then
  SEARCH_DESCRIPTION="two-stage search: far 10 mm/s for 45 mm, then near 3 mm/s until 70 mm max depth"
else
  SEARCH_DESCRIPTION="deterministic 3 mm/s downward search"
fi

usage() {
  cat <<'USAGE'
Usage:
  bridge-line-operator.sh preview-autowatch
  bridge-line-operator.sh hold-autowatch
  bridge-line-operator.sh line-autowatch
  bridge-line-operator.sh axis-autowatch
  bridge-line-operator.sh geo-autowatch
  bridge-line-operator.sh witness-autowatch
  bridge-line-operator.sh preview-bridge
  bridge-line-operator.sh hold-bridge
  bridge-line-operator.sh line-bridge
  bridge-line-operator.sh line-bridge-fast
  bridge-line-operator.sh axis-bridge
  bridge-line-operator.sh geo-bridge
  bridge-line-operator.sh witness-bridge
  bridge-line-operator.sh prep-long-checks
  bridge-line-operator.sh live-ready

Teach Pendant programs:
  /programs/andyl/kunwei/step4/step4e_preview_line_${BRIDGE_PROFILE}.urp
  /programs/andyl/kunwei/step4/step4e_contact_hold_line_${BRIDGE_PROFILE}.urp
  /programs/andyl/kunwei/step4/step4e_line_outerloop_${BRIDGE_PROFILE}.urp
  /programs/andyl/kunwei/step4/step4e_ball_first_contact_p0_v1.urp
  /programs/andyl/kunwei/step4/step4e_ball_vs_cyl_contact_p0_v1.urp
  /programs/andyl/kunwei/step4/step4e_attitude_axis_iso_v1.urp
  current v31 line package lives under /programs/andyl/kunwei/step4/
  previous v30 line package lives under /programs/andyl/kunwei/step4/
  locked-normal fallback v29 line package lives under /programs/andyl/kunwei/step4/
  failed/archive v21/v22/v23/v24/v25/v26/v27 line packages live under /programs/andyl/kunwei/step4/step4e/
  failed/archive v28 line package lives under /programs/andyl/kunwei/step4/; it failed from the old bridge profile mismatch, not motion parameters.
  Step4f/Step4g current packages live under /programs/andyl/kunwei/step4/
  Step5b current contact package lives under /programs/andyl/kunwei/step5/
  Step5c current joint-space packages live under /programs/andyl/kunwei/step5/
  Step6b current contact package lives under /programs/andyl/kunwei/step6/
  canonical Step4e/TASE flow table: ${ROOT}/STEP4E_FLOW.md
  canonical Step5 flow table: ${ROOT}/STEP5_FLOW.md
  canonical Step6 flow table: ${ROOT}/STEP6_FLOW.md

Bridge lifecycle:
  * bridge starts Kunwei/RTDE bridge immediately, then waits up to 45 s for TP Play.
  * line-bridge-fast requires a fresh long-check cache and only runs short
    loaded-program/safety/no-old-bridge checks at trigger time.
  * BRIDGE_SKIP_BENCH_GATE=1 skips long bench-gate refresh for prepared live
    triggers; use prep-long-checks to refresh the cache when bench state changed.
  * autowatch waits for TP Play before starting the bridge; keep it for manual
    testing only, not for the normal 开bridge trigger.
  * bridge is stopped when TP program stops, safety is not NORMAL, or Dashboard is unreachable.

Step4e motion boundary:
  preview: no robot motion, echo Step4e command registers only.
  geo: P0 ball-first witness; touch once at low threshold, visual dwell, then 2 mm base-Z retract.
  witness: P0 pair witness; ball contact then housing/cylindrical-face contact in one TP program.
  axis: no-contact four-quadrant attitude axis isolation.
  hold: contact search, then 12 s force/orientation hold.
  line: contact search, then XY path from the selected Step4e/Step4f/Step4g bridge reference.
  force target = ${BRIDGE_TARGET_FORCE_N} N for hold/line only; geo contact witness triggers around 1-1.5 N.
  raw normal guard = ${MAX_NORMAL_FORCE_N} N, force norm guard = ${MAX_FORCE_NORM_N} N, torque guard = ${MAX_TORQUE_NORM_NM} Nm.
USAGE
}

select_mode() {
  local requested="$1"
  case "${requested}" in
    preview-autowatch|preview-bridge)
      EXPECTED_PROGRAM="${PROGRAM_PREVIEW}"
      EXPECTED_BASENAME="step4e_preview_line_${BRIDGE_PROFILE}.urp"
      BRIDGE_MODE="preview"
      RUN_LABEL="step4e_preview_line_${BRIDGE_PROFILE}"
      ;;
    hold-autowatch|hold-bridge)
      EXPECTED_PROGRAM="${PROGRAM_HOLD}"
      EXPECTED_BASENAME="step4e_contact_hold_line_${BRIDGE_PROFILE}.urp"
      BRIDGE_MODE="hold"
      RUN_LABEL="step4e_contact_hold_line_${BRIDGE_PROFILE}"
      ;;
    line-autowatch|line-bridge|line-bridge-fast)
      EXPECTED_PROGRAM="${PROGRAM_LINE}"
      if [[ "${BRIDGE_PROFILE}" == "v21" ]]; then
        EXPECTED_BASENAME="step4e_detached_movel_minrot_v21.urp"
      elif [[ "${BRIDGE_PROFILE}" == "step4f_v1" ]]; then
        EXPECTED_BASENAME="step4f_cycloid_seed_normal_v1.urp"
      elif [[ "${BRIDGE_PROFILE}" == "step4g_v1" ]]; then
        EXPECTED_BASENAME="step4g_eight_seed_normal_v1.urp"
      elif [[ "${BRIDGE_PROFILE}" == "step5b_v1" ]]; then
        EXPECTED_BASENAME="step5b_contact_cycloid_baseline_v1.urp"
      elif [[ "${BRIDGE_PROFILE}" == step5d_strict_rnn_liveprep_v* ]]; then
        EXPECTED_BASENAME="${BRIDGE_PROFILE}.urp"
      elif [[ "${BRIDGE_PROFILE}" == "step6b_v1" ]]; then
        EXPECTED_BASENAME="step6b_contact_eight_baseline_v1.urp"
      elif [[ "${BRIDGE_PROFILE}" == "step6b_v2" ]]; then
        EXPECTED_BASENAME="step6b_contact_eight_baseline_v2.urp"
      elif [[ "${BRIDGE_PROFILE}" == "v22" || "${BRIDGE_PROFILE}" == "v23" || "${BRIDGE_PROFILE}" == "v24" || "${BRIDGE_PROFILE}" == "v25" || "${BRIDGE_PROFILE}" == "v26" || "${BRIDGE_PROFILE}" == "v27" || "${BRIDGE_PROFILE}" == "v28" || "${BRIDGE_PROFILE}" == "v29" || "${BRIDGE_PROFILE}" == "v30" || "${BRIDGE_PROFILE}" == "v31" ]]; then
        EXPECTED_BASENAME="step4e_seed_normal_loop_${BRIDGE_PROFILE}.urp"
      else
        EXPECTED_BASENAME="step4e_line_outerloop_${BRIDGE_PROFILE}.urp"
      fi
      BRIDGE_MODE="line"
      if [[ "${BRIDGE_PROFILE}" == "v21" ]]; then
        RUN_LABEL="step4e_detached_movel_minrot_v21"
      elif [[ "${BRIDGE_PROFILE}" == "step4f_v1" ]]; then
        RUN_LABEL="step4f_cycloid_seed_normal_v1"
      elif [[ "${BRIDGE_PROFILE}" == "step4g_v1" ]]; then
        RUN_LABEL="step4g_eight_seed_normal_v1"
      elif [[ "${BRIDGE_PROFILE}" == "step5b_v1" ]]; then
        RUN_LABEL="step5b_contact_cycloid_baseline_v1"
      elif [[ "${BRIDGE_PROFILE}" == step5d_strict_rnn_liveprep_v* ]]; then
        RUN_LABEL="${BRIDGE_PROFILE}"
      elif [[ "${BRIDGE_PROFILE}" == "step6b_v1" ]]; then
        RUN_LABEL="step6b_contact_eight_baseline_v1"
      elif [[ "${BRIDGE_PROFILE}" == "step6b_v2" ]]; then
        RUN_LABEL="step6b_contact_eight_baseline_v2"
      elif [[ "${BRIDGE_PROFILE}" == "v22" || "${BRIDGE_PROFILE}" == "v23" || "${BRIDGE_PROFILE}" == "v24" || "${BRIDGE_PROFILE}" == "v25" || "${BRIDGE_PROFILE}" == "v26" || "${BRIDGE_PROFILE}" == "v27" || "${BRIDGE_PROFILE}" == "v28" || "${BRIDGE_PROFILE}" == "v29" || "${BRIDGE_PROFILE}" == "v30" || "${BRIDGE_PROFILE}" == "v31" ]]; then
        RUN_LABEL="step4e_seed_normal_loop_${BRIDGE_PROFILE}"
      else
        RUN_LABEL="step4e_line_outerloop_${BRIDGE_PROFILE}"
      fi
      ;;
    axis-autowatch|axis-bridge)
      EXPECTED_PROGRAM="${PROGRAM_AXIS_ISO}"
      EXPECTED_BASENAME="step4e_attitude_axis_iso_v1.urp"
      BRIDGE_MODE="axis_iso"
      RUN_LABEL="step4e_attitude_axis_iso_v1"
      CONFIRM_LABEL="axis"
      ;;
    geo-autowatch|geo-bridge)
      EXPECTED_PROGRAM="${PROGRAM_GEO}"
      EXPECTED_BASENAME="step4e_ball_first_contact_p0_v1.urp"
      BRIDGE_MODE="off"
      RUN_LABEL="step4e_ball_first_contact_p0_v1"
      CONFIRM_LABEL="geo"
      ;;
    witness-autowatch|witness-bridge)
      EXPECTED_PROGRAM="${PROGRAM_WITNESS}"
      EXPECTED_BASENAME="step4e_ball_vs_cyl_contact_p0_v1.urp"
      BRIDGE_MODE="off"
      RUN_LABEL="step4e_ball_vs_cyl_contact_p0_v1"
      CONFIRM_LABEL="witness"
      ;;
    *)
      usage
      exit 2
      ;;
  esac
}

run_bench_gate() {
  python3 "${BENCH_GATE}" --include-kunwei --json-only
}

long_gate_cache_valid() {
  python3 - "${ROOT}" "${LONG_CHECK_CACHE}" "${LONG_CHECK_TTL_S}" "${ROBOT_HOST}" <<'PY'
import sys
from pathlib import Path

root = Path(sys.argv[1])
sys.path.insert(0, str(root / "tools"))
from step5d_runtime_interface import long_check_cache_status

cache = Path(sys.argv[2])
ttl_s = float(sys.argv[3])
host = sys.argv[4]
status = long_check_cache_status(cache, robot_host=host, ttl_s=ttl_s)
if status.get("ok"):
    print(f"[operator] long-check cache hit: age={float(status['age_s']):.1f}s ttl={ttl_s:.1f}s {cache}")
    raise SystemExit(0)
raise SystemExit(1)
PY
}

step5d_live_ready() {
  if [[ "${BRIDGE_PROFILE}" == step5d_strict_rnn_liveprep_* ]]; then
    python3 "${STEP5D_RUNTIME_INTERFACE}" \
      --root "${ROOT}" \
      --program "${BRIDGE_PROFILE}" \
      --robot-host "${ROBOT_HOST}" \
      --long-check-cache "${LONG_CHECK_CACHE}" \
      --long-check-ttl-s "${LONG_CHECK_TTL_S}" \
      live-ready
  fi
}

refresh_bench_gate_cache() {
  mkdir -p "$(dirname "${LONG_CHECK_CACHE}")"
  local tmp
  tmp="$(mktemp)"
  if run_bench_gate | tee "${tmp}"; then
    python3 - "${tmp}" "${LONG_CHECK_CACHE}" "${ROBOT_HOST}" <<'PY'
import json
import subprocess
import sys
import time
from pathlib import Path

def run_json(args):
    completed = subprocess.run(args, text=True, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, check=False)
    if completed.returncode != 0 or not completed.stdout.strip():
        return []
    try:
        return json.loads(completed.stdout)
    except Exception:
        return []

def route_get(host):
    completed = subprocess.run(["ip", "route", "get", host], text=True, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, check=False)
    return completed.stdout.strip() if completed.returncode == 0 else ""

def boot_id():
    try:
        return Path("/proc/sys/kernel/random/boot_id").read_text(encoding="utf-8").strip()
    except Exception:
        return ""

def current_fingerprint(gate):
    device = gate.get("device", "enp3s0")
    kunwei = gate.get("kunwei") or {}
    kunwei_host = kunwei.get("sensor_host", "")
    return {
        "boot_id": boot_id(),
        "device": device,
        "ipv4_addresses": run_json(["ip", "-j", "-4", "addr", "show", "dev", device]),
        "default_routes": run_json(["ip", "-j", "route", "show", "default"]),
        "kunwei_route_get": route_get(kunwei_host) if kunwei_host else "",
    }

source = Path(sys.argv[1])
cache = Path(sys.argv[2])
host = sys.argv[3]
try:
    gate = json.loads(source.read_text(encoding="utf-8"))
except Exception:
    gate = {"raw": source.read_text(encoding="utf-8", errors="replace")}
payload = {
    "ok": True,
    "checked_at_epoch": time.time(),
    "robot_host": host,
    "gate": gate,
    "fingerprint": current_fingerprint(gate),
}
tmp = cache.with_suffix(cache.suffix + ".tmp")
tmp.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
tmp.replace(cache)
print(f"[operator] long-check cache refreshed: {cache}")
PY
    rm -f "${tmp}"
    return 0
  fi
  local rc="$?"
  rm -f "${tmp}"
  return "${rc}"
}

run_bench_gate_cached() {
  if [[ "${BRIDGE_SKIP_BENCH_GATE:-0}" == "1" || "${BRIDGE_SKIP_LONG_CHECKS:-0}" == "1" ]]; then
    echo "[operator] skipping long bench gate by request (BRIDGE_SKIP_BENCH_GATE=${BRIDGE_SKIP_BENCH_GATE:-0}, BRIDGE_SKIP_LONG_CHECKS=${BRIDGE_SKIP_LONG_CHECKS:-0})"
    return 0
  fi
  if long_gate_cache_valid; then
    return 0
  fi
  refresh_bench_gate_cache
}

require_bench_gate_cache() {
  if long_gate_cache_valid; then
    return 0
  fi
  echo "refusing fast bridge: long-check cache is missing or older than ${LONG_CHECK_TTL_S}s"
  echo "run: BRIDGE_PROFILE=${BRIDGE_PROFILE} ${BASH_SOURCE[0]} prep-long-checks"
  exit 24
}

ensure_no_existing_bridge() {
  if pgrep -f "${ROOT}/tools/kunwei_rtde_bridge.py" >/dev/null 2>&1; then
    echo "refusing: an existing Kunwei RTDE bridge process is already active"
    pgrep -af "${ROOT}/tools/kunwei_rtde_bridge.py" || true
    exit 3
  fi
}

dashboard_snapshot_py='
import socket
import sys

expected_program = sys.argv[1]
expected_basename = sys.argv[2]
host = sys.argv[3]
port = int(sys.argv[4])

def read_once(sock, timeout=1.0):
    sock.settimeout(timeout)
    try:
        return sock.recv(4096).decode("utf-8", errors="replace").strip()
    except socket.timeout:
        return ""

with socket.create_connection((host, port), timeout=1.0) as sock:
    read_once(sock)

    def dash_cmd(cmd):
        sock.sendall((cmd + "\n").encode("ascii"))
        return read_once(sock)

    running = dash_cmd("running")
    loaded = dash_cmd("get loaded program")
    state = dash_cmd("programState")
    safety = dash_cmd("safetymode")
print("\n".join([running, loaded, state, safety]))
loaded_ok = expected_program in loaded or expected_basename in loaded
running_ok = "true" in running.lower()
safety_ok = "NORMAL" in safety.upper()
stopped = "STOPPED" in state.upper() or "false" in running.lower()
if not safety_ok:
    raise SystemExit(20)
if not loaded_ok:
    raise SystemExit(21)
if running_ok:
    raise SystemExit(10)
if stopped:
    raise SystemExit(11)
raise SystemExit(12)
'

dashboard_snapshot() {
  python3 -c "${dashboard_snapshot_py}" "${EXPECTED_PROGRAM}" "${EXPECTED_BASENAME}" "${ROBOT_HOST}" "${DASHBOARD_PORT}"
}

require_rtde_quick_probe() {
  python3 - "${ROBOT_HOST}" <<'PY'
import socket
import sys

host = sys.argv[1]
try:
    with socket.create_connection((host, 30004), timeout=1.0):
        pass
except OSError as exc:
    print(f"refusing fast bridge: RTDE 30004 is not reachable on {host}: {type(exc).__name__}: {exc}")
    raise SystemExit(25)
print(f"[operator] RTDE quick probe passed: {host}:30004")
PY
}

trigger_dashboard_check() {
  local rc
  set +e
  dashboard_snapshot >/tmp/step4e_dash_snapshot.txt 2>&1
  rc="$?"
  cat /tmp/step4e_dash_snapshot.txt || true
  case "${rc}" in
    10)
      echo "[operator] expected program is already running; starting bridge late with already_running=1"
      return 10
      ;;
    11)
      echo "[operator] trigger check passed: expected program loaded, safety NORMAL, program not running"
      return 0
      ;;
    20)
      echo "refusing: safety mode is not NORMAL"
      return 20
      ;;
    21)
      echo "refusing: loaded program is not expected Step4e ${BRIDGE_MODE}"
      return 21
      ;;
    *)
      echo "refusing: Dashboard state is not ready for fast bridge trigger (rc=${rc})"
      return "${rc}"
      ;;
  esac
}

stop_bridge_process() {
  local bridge_pid="$1"
  local reason="$2"
  if ! kill -0 "${bridge_pid}" 2>/dev/null; then
    return 0
  fi

  echo "[operator] stopping bridge: ${reason}"
  kill -INT "${bridge_pid}" 2>/dev/null || true
  local i
  for i in 1 2 3 4; do
    sleep 0.5
    if ! kill -0 "${bridge_pid}" 2>/dev/null; then
      return 0
    fi
  done

  echo "[operator] bridge ignored SIGINT after 2 s; sending SIGTERM"
  kill -TERM "${bridge_pid}" 2>/dev/null || true
  for i in 1 2 3 4; do
    sleep 0.5
    if ! kill -0 "${bridge_pid}" 2>/dev/null; then
      return 0
    fi
  done

  echo "[operator] bridge ignored SIGTERM after 2 s; sending SIGKILL"
  kill -KILL "${bridge_pid}" 2>/dev/null || true
}

wait_for_tp_play_autowatch() {
  echo "[autowatch] Watching for TP Play on:"
  echo "  ${EXPECTED_PROGRAM}"
  local start now rc
  start="$(python3 - <<'PY'
import time
print(time.monotonic())
PY
)"
  while true; do
    if dashboard_snapshot >/tmp/step4e_dash_snapshot.txt 2>&1; then
      true
    else
      rc="$?"
      cat /tmp/step4e_dash_snapshot.txt || true
      if [[ "${rc}" == "10" ]]; then
        echo "[autowatch] expected Step4e ${BRIDGE_MODE} program is running; starting bridge."
        return 0
      elif [[ "${rc}" == "20" || "${rc}" == "21" ]]; then
        echo "[autowatch] refusing: safety is not NORMAL or loaded program is not expected Step4e ${BRIDGE_MODE}"
        return "${rc}"
      fi
    fi
    now="$(python3 - <<'PY'
import time
print(time.monotonic())
PY
)"
    if python3 - "$start" "$now" "$AUTOWATCH_WAIT_FOR_PLAY_S" <<'PY'
import sys
raise SystemExit(0 if float(sys.argv[2]) - float(sys.argv[1]) > float(sys.argv[3]) else 1)
PY
    then
      echo "[autowatch] timed out waiting for TP Play after ${AUTOWATCH_WAIT_FOR_PLAY_S} s"
      return 7
    fi
    sleep 0.25
  done
}

monitor_bridge() {
  local bridge_pid="$1"
  local seen_running="${2:-0}"
  local start now rc
  start="$(python3 - <<'PY'
import time
print(time.monotonic())
PY
)"
  while kill -0 "${bridge_pid}" 2>/dev/null; do
    if dashboard_snapshot >/tmp/step4e_dash_snapshot.txt 2>&1; then
      true
    else
      rc="$?"
      cat /tmp/step4e_dash_snapshot.txt || true
      if [[ "${rc}" == "10" ]]; then
        seen_running=1
      elif [[ "${rc}" == "11" && "${seen_running}" == "1" ]]; then
        echo "[operator] TP program stopped"
        stop_bridge_process "${bridge_pid}" "TP program stopped"
        return 0
      elif [[ "${rc}" == "20" ]]; then
        echo "[operator] safety mode is not NORMAL"
        stop_bridge_process "${bridge_pid}" "safety mode is not NORMAL"
        return 0
      elif [[ "${rc}" == "21" ]]; then
        if [[ "${seen_running}" == "1" ]]; then
          echo "[operator] loaded program is no longer expected Step4e ${BRIDGE_MODE}"
          stop_bridge_process "${bridge_pid}" "loaded program mismatch"
          return 0
        fi
      fi
    fi
    if [[ "${seen_running}" == "0" ]]; then
      now="$(python3 - <<'PY'
import time
print(time.monotonic())
PY
)"
      if python3 - "$start" "$now" "$WAIT_FOR_PLAY_S" <<'PY'
import sys
raise SystemExit(0 if float(sys.argv[2]) - float(sys.argv[1]) > float(sys.argv[3]) else 1)
PY
      then
        echo "[operator] expected program did not start within ${WAIT_FOR_PLAY_S} s"
        stop_bridge_process "${bridge_pid}" "TP Play timeout"
        return 0
      fi
    fi
    sleep 0.25
  done
}

postprocess_run() {
  local out_dir="$1"
  local bridge_csv="${out_dir}/bridge_rtde_500hz.csv"
  if [[ -f "${bridge_csv}" ]]; then
    python3 "${ROOT}/tools/summarize_stage_frequency.py" "${bridge_csv}" --output "${out_dir}/stage_frequency_summary.json" || true
  else
    echo "[operator] no bridge CSV found for postprocess: ${bridge_csv}"
  fi
}

wait_for_bridge_output_started() {
  local out_dir="$1"
  local bridge_pid="$2"
  local bridge_csv="${out_dir}/bridge_rtde_500hz.csv"
  local metadata="${out_dir}/metadata.json"
  local i
  for i in $(seq 1 30); do
    if ! kill -0 "${bridge_pid}" 2>/dev/null; then
      echo "[operator] bridge process exited before output-start confirmation"
      return 1
    fi
    if [[ -s "${bridge_csv}" || -s "${metadata}" ]]; then
      echo "[operator] bridge output started: ${out_dir}"
      return 0
    fi
    sleep 0.1
  done
  echo "[operator] bridge output not observed within 3s; continuing monitor"
  return 1
}

maybe_start_background_push() {
  local out_dir="$1"
  if [[ "${BRIDGE_BACKGROUND_PUSH_AFTER_LIVE}" != "1" ]]; then
    return 0
  fi
  local log="${out_dir}/background_git_push.log"
  (
    cd /home/andy/ur10e_ros2_ws
    git push
  ) >"${log}" 2>&1 &
  echo "[operator] background git push started: pid=$! log=${log}"
}

run_bridge_for_mode() {
  local out_dir="$1"
  local already_running="$2"
  mkdir -p "${out_dir}"
  local bridge_pid=""
  local stage25_only_args=()
  if [[ "${BRIDGE_STAGE25_ONLY}" == "1" ]]; then
    stage25_only_args+=(--bridge-integrate-stage25-only)
  fi
  cleanup() {
    if [[ -n "${bridge_pid}" ]] && kill -0 "${bridge_pid}" 2>/dev/null; then
      stop_bridge_process "${bridge_pid}" "operator cleanup"
    fi
  }
  trap cleanup INT TERM EXIT

  python3 "${ROOT}/tools/kunwei_rtde_bridge.py" \
    --allow-kunwei-stream-command \
    --write-rtde-inputs \
    --baseline-s "${BRIDGE_BASELINE_S}" \
    --rezero-s "${BRIDGE_REZERO_S}" \
    --duration-s "${BRIDGE_DURATION_S}" \
    --rtde-hz "${BRIDGE_RTDE_HZ}" \
    --socket-timeout-s "${BRIDGE_SOCKET_TIMEOUT_S}" \
    --sensor-stale-s "${BRIDGE_SENSOR_STALE_S}" \
    --target-force-n "${BRIDGE_TARGET_FORCE_N}" \
    --normal-axis fz \
    --normal-sign 1 \
    --max-normal-force-n "${MAX_NORMAL_FORCE_N}" \
    --max-force-norm-n "${MAX_FORCE_NORM_N}" \
    --max-torque-norm-nm "${MAX_TORQUE_NORM_NM}" \
    --bridge-mode "${BRIDGE_MODE}" \
    --bridge-profile "${BRIDGE_PROFILE}" \
    --bridge-path-shape "${BRIDGE_PATH_SHAPE}" \
    --bridge-line-speed-m-s "${BRIDGE_LINE_SPEED_M_S}" \
    --bridge-line-settle-s "${BRIDGE_LINE_SETTLE_S}" \
    "${stage25_only_args[@]}" \
    --bridge-path-p-gain 1.5 \
    --bridge-motion-limit-m-s "${BRIDGE_MOTION_LIMIT_M_S}" \
    --bridge-total-linear-limit-m-s "${BRIDGE_TOTAL_LINEAR_LIMIT_M_S}" \
    --bridge-normal-velocity-limit-m-s "${BRIDGE_NORMAL_VELOCITY_LIMIT_M_S}" \
    --bridge-force-p-gain "${BRIDGE_FORCE_P_GAIN}" \
    --bridge-force-i-gain "${BRIDGE_FORCE_I_GAIN}" \
    --bridge-force-damping "${BRIDGE_FORCE_DAMPING}" \
    --bridge-normal-command-sign "${BRIDGE_NORMAL_COMMAND_SIGN}" \
    --bridge-integral-limit-n-s "${BRIDGE_INTEGRAL_LIMIT_N_S}" \
    --bridge-min-force-for-control-n 1.0 \
    --bridge-acquire-grace-s 0.25 \
    --bridge-reacquire-velocity-m-s "${BRIDGE_REACQUIRE_VELOCITY_M_S}" \
    --bridge-orientation-gain "${BRIDGE_ORIENTATION_GAIN}" \
    --bridge-orientation-wx-sign "${BRIDGE_ORIENTATION_WX_SIGN}" \
    --bridge-orientation-wy-sign "${BRIDGE_ORIENTATION_WY_SIGN}" \
    --bridge-angular-limit-rad-s "${BRIDGE_ANGULAR_LIMIT_RAD_S}" \
    --bridge-contact-offset-min-fz-n 1.0 \
    --bridge-normal-follow-mode "${BRIDGE_NORMAL_FOLLOW_MODE}" \
    --bridge-normal-filter-tau-s "${BRIDGE_NORMAL_FILTER_TAU_S}" \
    --bridge-normal-filter-alpha "${BRIDGE_NORMAL_FILTER_ALPHA}" \
    --bridge-normal-max-rate-rad-s "${BRIDGE_NORMAL_MAX_RATE_RAD_S}" \
    --bridge-normal-min-force-n "${BRIDGE_NORMAL_MIN_FORCE_N}" \
    --bridge-normal-max-angle-from-latch-deg "${BRIDGE_NORMAL_MAX_ANGLE_FROM_LATCH_DEG}" \
    --bridge-normal-friction-projection "${BRIDGE_NORMAL_FRICTION_PROJECTION}" \
    --step5c-qdot-limit-rad-s "${STEP5C_QDOT_LIMIT_RAD_S}" \
    --step5c-joint-damping "${STEP5C_JOINT_DAMPING}" \
    --step5c-joint-model "${STEP5C_JOINT_MODEL}" \
    --step5c-joint-site "${STEP5C_JOINT_SITE}" \
    --step5d-preload-filtered-min-n "${STEP5D_PRELOAD_FILTERED_MIN_N:-7.5}" \
    --step5d-preload-filtered-max-n "${STEP5D_PRELOAD_FILTERED_MAX_N:-14.0}" \
    --step5d-preload-raw-min-n "${STEP5D_PRELOAD_RAW_MIN_N:-7.0}" \
    --step5d-preload-raw-max-n "${STEP5D_PRELOAD_RAW_MAX_N:-15.0}" \
    --step5d-preload-force-norm-max-n "${STEP5D_PRELOAD_FORCE_NORM_MAX_N:-25.0}" \
    --step5d-preload-hold-s "${STEP5D_PRELOAD_HOLD_S:-0.100}" \
    --step5d-preload-timeout-s "${STEP5D_PRELOAD_TIMEOUT_S:-10.0}" \
    --output-dir "${out_dir}" &
  bridge_pid="$!"
  wait_for_bridge_output_started "${out_dir}" "${bridge_pid}" || true
  maybe_start_background_push "${out_dir}"

  monitor_bridge "${bridge_pid}" "${already_running}" || true
  wait "${bridge_pid}" || true
  trap - INT TERM EXIT

  echo "[operator] bridge output: ${out_dir}"
  local quiet_json="${out_dir}/kunwei_quiet_stream.json"
  if python3 "${ROOT}/tools/kunwei_quiet_stream.py" --json-only --output-json "${quiet_json}"; then
    echo "[operator] Kunwei quiet stop passed: ${quiet_json}"
  else
    echo "[operator] Kunwei quiet stop reported issue: ${quiet_json}"
  fi
  [[ -f "${quiet_json}" ]] && cat "${quiet_json}"
  postprocess_run "${out_dir}"
}

mode="${1:-}"
if [[ "${mode}" == "prep-long-checks" ]]; then
  refresh_bench_gate_cache
  exit 0
fi
if [[ "${mode}" == "live-ready" || "${mode}" == "status" ]]; then
  step5d_live_ready
  exit 0
fi
select_mode "${mode}"
CONFIRM_TOKEN="${CONFIRM_LABEL:-${BRIDGE_MODE}}"
CONFIRM_TOKEN="${CONFIRM_TOKEN^^}"

case "${mode}" in
  *-autowatch)
    cat <<WARNING
Bridge ${BRIDGE_MODE} ${BRIDGE_PROFILE} autowatch.
This mode waits for Teach Pendant Play first.
It does not start Kunwei streaming or write RTDE inputs while waiting.

Open this Teach Pendant program first:
  ${EXPECTED_PROGRAM}

Then run this mode and press Play on the Teach Pendant.
The bridge will start automatically only after Dashboard reports that exact Step4e program running.
WARNING
    run_bench_gate_cached
    ensure_no_existing_bridge
    wait_for_tp_play_autowatch
    run_bridge_for_mode "${RUN_ROOT}/bridge_${RUN_LABEL}_autowatch_${STAMP}" 1
    ;;
  *-bridge-fast)
    if [[ "${mode}" != "line-bridge-fast" ]]; then
      echo "fast trigger is currently implemented only for line-bridge-fast"
      exit 2
    fi
    step5d_live_ready
    require_bench_gate_cache
    require_rtde_quick_probe
    ensure_no_existing_bridge
    trigger_rc=0
    set +e
    trigger_dashboard_check
    trigger_rc="$?"
    set -e
    if [[ "${trigger_rc}" == "10" ]]; then
      run_bridge_for_mode "${RUN_ROOT}/bridge_${RUN_LABEL}_${STAMP}" 1
    elif [[ "${trigger_rc}" == "0" ]]; then
      run_bridge_for_mode "${RUN_ROOT}/bridge_${RUN_LABEL}_${STAMP}" 0
    else
      exit "${trigger_rc}"
    fi
    ;;
  *-bridge)
    cat <<WARNING
Bridge ${BRIDGE_MODE} ${BRIDGE_PROFILE} lifecycle bridge.
This sends Kunwei 48 AA 0D 0A and writes UR RTDE input registers.
It does not send URScript from Ubuntu.

Before pressing Play, open this Teach Pendant program:
  ${EXPECTED_PROGRAM}

Motion/control:
  preview = no motion, geo/hold/line setup = ${SEARCH_DESCRIPTION}
  path shape = ${BRIDGE_PATH_SHAPE}; line XY speed command = ${BRIDGE_LINE_SPEED_M_S} m/s for line shape, path cap = ${BRIDGE_MOTION_LIMIT_M_S} m/s, total linear cap = ${BRIDGE_TOTAL_LINEAR_LIMIT_M_S} m/s
  speedl acceleration = 300 mm/s^2, hold time = 2 ms
  force target = ${BRIDGE_TARGET_FORCE_N} N for hold/line only; geo contact witness triggers around 1-1.5 N
  raw normal guard = ${MAX_NORMAL_FORCE_N} N, force norm guard = ${MAX_FORCE_NORM_N} N, torque guard = ${MAX_TORQUE_NORM_NM} Nm
  attitude proxy = bounded wx/wy velocity command, gain = ${BRIDGE_ORIENTATION_GAIN}, angular limit = ${BRIDGE_ANGULAR_LIMIT_RAD_S} rad/s, wx sign = ${BRIDGE_ORIENTATION_WX_SIGN}, wy sign = ${BRIDGE_ORIENTATION_WY_SIGN}, yaw frozen
  normal follow = ${BRIDGE_NORMAL_FOLLOW_MODE}, tau = ${BRIDGE_NORMAL_FILTER_TAU_S}s, max rate = ${BRIDGE_NORMAL_MAX_RATE_RAD_S} rad/s, min force = ${BRIDGE_NORMAL_MIN_FORCE_N} N, gate = ${BRIDGE_NORMAL_MAX_ANGLE_FROM_LATCH_DEG} deg, friction projection = ${BRIDGE_NORMAL_FRICTION_PROJECTION}

Type START_BRIDGE_${CONFIRM_TOKEN}_${BRIDGE_PROFILE^^} to continue:
WARNING
    read -r confirm
    if [[ "${confirm}" != "START_BRIDGE_${CONFIRM_TOKEN}_${BRIDGE_PROFILE^^}" ]]; then
      echo "aborted"
      exit 2
    fi
    run_bench_gate_cached
    ensure_no_existing_bridge
    run_bridge_for_mode "${RUN_ROOT}/bridge_${RUN_LABEL}_${STAMP}" 0
    ;;
esac
