#!/usr/bin/env bash
set -euo pipefail

ROOT="/home/andy/ur10e_ros2_ws/experiments/kunwei/closed-loop-straight-line/2026-06-04"
RUN_ROOT="${ROOT}/runs"
STAMP="$(date +%Y%m%d_%H%M%S)"
ROBOT_HOST="${ROBOT_HOST:-192.168.1.18}"
DASHBOARD_PORT="${DASHBOARD_PORT:-29999}"
WAIT_FOR_PLAY_S="${WAIT_FOR_PLAY_S:-45}"
AUTOWATCH_WAIT_FOR_PLAY_S="${AUTOWATCH_WAIT_FOR_PLAY_S:-30}"
BENCH_GATE="/home/andy/codex-private-skills/skills/ur10e-realsetup/scripts/check_ubuntu_network.py"
LONG_CHECK_TTL_S="${LONG_CHECK_TTL_S:-1800}"
LONG_CHECK_CACHE="${LONG_CHECK_CACHE:-${RUN_ROOT}/.step4e_long_checks_cache.json}"
STEP4E_VERSION="${STEP4E_VERSION:-v1}"
BRIDGE_DURATION_S="${BRIDGE_DURATION_S:-180}"
STEP4E_BACKGROUND_PUSH_AFTER_LIVE="${STEP4E_BACKGROUND_PUSH_AFTER_LIVE:-0}"
STEP4E_NORMAL_COMMAND_SIGN="${STEP4E_NORMAL_COMMAND_SIGN:-1}"
MAX_NORMAL_FORCE_N="${MAX_NORMAL_FORCE_N:-50}"
MAX_TORQUE_NORM_NM="${MAX_TORQUE_NORM_NM:-0.6}"
STEP4E_ORIENTATION_GAIN="${STEP4E_ORIENTATION_GAIN:-0.20}"
STEP4E_ORIENTATION_WX_SIGN="${STEP4E_ORIENTATION_WX_SIGN:-1}"
STEP4E_ORIENTATION_WY_SIGN="${STEP4E_ORIENTATION_WY_SIGN:-1}"
STEP4E_LINE_SPEED_M_S="${STEP4E_LINE_SPEED_M_S:-0.003}"
STEP4E_LINE_SETTLE_S="${STEP4E_LINE_SETTLE_S:-0.0}"
STEP4E_STAGE25_ONLY="${STEP4E_STAGE25_ONLY:-0}"
STEP4E_NORMAL_VELOCITY_LIMIT_M_S="${STEP4E_NORMAL_VELOCITY_LIMIT_M_S:-0.003}"
STEP4E_FORCE_P_GAIN="${STEP4E_FORCE_P_GAIN:-0.0007}"
STEP4E_FORCE_I_GAIN="${STEP4E_FORCE_I_GAIN:-0.00008}"
STEP4E_FORCE_DAMPING="${STEP4E_FORCE_DAMPING:-0.35}"
STEP4E_INTEGRAL_LIMIT_N_S="${STEP4E_INTEGRAL_LIMIT_N_S:-10.0}"
STEP4E_REACQUIRE_VELOCITY_M_S="${STEP4E_REACQUIRE_VELOCITY_M_S:-0.001}"
STEP4E_ANGULAR_LIMIT_RAD_S="${STEP4E_ANGULAR_LIMIT_RAD_S:-0.015}"
STEP4E_NORMAL_FOLLOW_MODE="${STEP4E_NORMAL_FOLLOW_MODE:-}"
STEP4E_NORMAL_FILTER_TAU_S="${STEP4E_NORMAL_FILTER_TAU_S:-0.35}"
STEP4E_NORMAL_FILTER_ALPHA="${STEP4E_NORMAL_FILTER_ALPHA:-0.35}"
STEP4E_NORMAL_MAX_RATE_RAD_S="${STEP4E_NORMAL_MAX_RATE_RAD_S:-0.010}"
STEP4E_NORMAL_MIN_FORCE_N="${STEP4E_NORMAL_MIN_FORCE_N:-2.0}"
STEP4E_NORMAL_MAX_ANGLE_FROM_LATCH_DEG="${STEP4E_NORMAL_MAX_ANGLE_FROM_LATCH_DEG:-20}"
STEP4E_NORMAL_FRICTION_PROJECTION="${STEP4E_NORMAL_FRICTION_PROJECTION:-on}"
if [[ -z "${STEP4E_NORMAL_FOLLOW_MODE}" ]]; then
  if [[ "${STEP4E_VERSION}" == "v30" || "${STEP4E_VERSION}" == "v31" ]]; then
    STEP4E_NORMAL_FOLLOW_MODE="filtered_live"
  else
    STEP4E_NORMAL_FOLLOW_MODE="locked"
  fi
fi

PROGRAM_PREVIEW="/programs/andyl/kunwei/step4/step4e_preview_line_${STEP4E_VERSION}.urp"
PROGRAM_HOLD="/programs/andyl/kunwei/step4/step4e_contact_hold_line_${STEP4E_VERSION}.urp"
PROGRAM_LINE="/programs/andyl/kunwei/step4/step4e_line_outerloop_${STEP4E_VERSION}.urp"
PROGRAM_GEO="/programs/andyl/kunwei/step4/step4e_ball_first_contact_p0_v1.urp"
PROGRAM_WITNESS="/programs/andyl/kunwei/step4/step4e_ball_vs_cyl_contact_p0_v1.urp"
PROGRAM_AXIS_ISO="/programs/andyl/kunwei/step4/step4e_attitude_axis_iso_v1.urp"
if [[ "${STEP4E_VERSION}" == "v21" ]]; then
  PROGRAM_LINE="/programs/andyl/kunwei/step4/step4e/step4e_detached_movel_minrot_v21.urp"
fi
if [[ "${STEP4E_VERSION}" == "v22" ]]; then
  PROGRAM_LINE="/programs/andyl/kunwei/step4/step4e/step4e_seed_normal_loop_v22.urp"
fi
if [[ "${STEP4E_VERSION}" == "v23" ]]; then
  PROGRAM_LINE="/programs/andyl/kunwei/step4/step4e/step4e_seed_normal_loop_v23.urp"
fi
if [[ "${STEP4E_VERSION}" == "v24" ]]; then
  PROGRAM_LINE="/programs/andyl/kunwei/step4/step4e/step4e_seed_normal_loop_v24.urp"
fi
if [[ "${STEP4E_VERSION}" == "v25" ]]; then
  PROGRAM_LINE="/programs/andyl/kunwei/step4/step4e/step4e_seed_normal_loop_v25.urp"
fi
if [[ "${STEP4E_VERSION}" == "v26" ]]; then
  PROGRAM_LINE="/programs/andyl/kunwei/step4/step4e/step4e_seed_normal_loop_v26.urp"
fi
if [[ "${STEP4E_VERSION}" == "v27" ]]; then
  PROGRAM_LINE="/programs/andyl/kunwei/step4/step4e/step4e_seed_normal_loop_v27.urp"
fi
if [[ "${STEP4E_VERSION}" == "v28" ]]; then
  PROGRAM_LINE="/programs/andyl/kunwei/step4/step4e_seed_normal_loop_v28.urp"
fi
if [[ "${STEP4E_VERSION}" == "v29" ]]; then
  PROGRAM_LINE="/programs/andyl/kunwei/step4/step4e_seed_normal_loop_v29.urp"
fi
if [[ "${STEP4E_VERSION}" == "v30" ]]; then
  PROGRAM_LINE="/programs/andyl/kunwei/step4/step4e_seed_normal_loop_v30.urp"
fi
if [[ "${STEP4E_VERSION}" == "v31" ]]; then
  PROGRAM_LINE="/programs/andyl/kunwei/step4/step4e_seed_normal_loop_v31.urp"
fi
if [[ "${STEP4E_VERSION}" == "p0_geo_v1" ]]; then
  SEARCH_DESCRIPTION="P0-geo ball-first contact witness: vertical TCP entry, far 15 mm/s until 80 mm depth, then near 3 mm/s until first 1-1.5 N contact or 92 mm max depth; after contact it holds still for visual confirmation, retracts base-Z 2 mm, and never runs attitude, 5N acquisition, or line motion"
elif [[ "${STEP4E_VERSION}" == "p0_ball_vs_cyl_v1" ]]; then
  SEARCH_DESCRIPTION="P0 witness pair: ball pose first, then KSM-8N housing/cylindrical-face pose; each uses low-threshold 1-1.5 N contact, 8 s visual dwell, and no attitude, 5N acquisition, or line motion"
elif [[ "${STEP4E_VERSION}" == "v22" ]]; then
  SEARCH_DESCRIPTION="seed-normal TASE minimal loop: move once to the measured near-normal TCP pose, far/near search at 5/3 mm/s, latch first contact normal, lift 50 mm, optionally apply the bridge target rotvec when orientation error exceeds 10 deg, re-search up to 70 mm, acquire 5 N, then run the XY line"
elif [[ "${STEP4E_VERSION}" == "v23" ]]; then
  SEARCH_DESCRIPTION="failed/archive v23 seed-normal loop: high-Z near-normal orientation, path-start XY at high Z, fixed search-start z=98.35 mm, first far/near search, latch contact normal, lift 50 mm, angular speedl posture correction; archived after stop register 13 at stage 25.2"
elif [[ "${STEP4E_VERSION}" == "v24" ]]; then
  SEARCH_DESCRIPTION="evidence v24 Step4e/TASE flow: one-step entry XY plus target attitude at current Z, fixed first far/near transition, first-contact latch, lift 30 mm, angular speedl posture correction with 37..39 forced zero, second search, acquire 5 N, then run the XY line"
elif [[ "${STEP4E_VERSION}" == "v25" ]]; then
  SEARCH_DESCRIPTION="failed/archive v25 Step4e/TASE flow: one-step entry XY plus target attitude at current Z, dynamic first far/near search using the wrong target initial Z datum, first-contact latch, lift 30 mm, angular speedl posture correction, second search, acquire 5 N, then run the XY line"
elif [[ "${STEP4E_VERSION}" == "v26" ]]; then
  SEARCH_DESCRIPTION="failed/archive v26 Step4e/TASE flow: one-step entry XY plus target attitude at current Z, but first far/near search used the reference path contact-start Z instead of measured first-contact Z, causing near search to start too high and depth-limit before force jump"
elif [[ "${STEP4E_VERSION}" == "v27" ]]; then
  SEARCH_DESCRIPTION="failed/archive v27 Step4e/TASE flow: one-step entry XY plus target attitude at current Z, first far/near search using the v13/v16 force-jump first-contact Z evidence so near starts about 30 mm above first contact; archived after first-contact latch cmd_valid timeout stop register 12 at stage 25.05"
elif [[ "${STEP4E_VERSION}" == "v28" ]]; then
  SEARCH_DESCRIPTION="failed/archive v28 Step4e/TASE flow: v28 reached first contact and stage 25.05, but the active bridge runtime did not recognize v28 as an angular-speedl profile, so cmd_valid stayed 0"
elif [[ "${STEP4E_VERSION}" == "v29" ]]; then
  SEARCH_DESCRIPTION="current Step4e/TASE flow from STEP4E_FLOW.md: bridge-profile-fix + line-entry-gate release; one-step entry XY plus target attitude at current Z, first far/near search using the v13/v16 force-jump first-contact Z evidence so near starts about 20 mm above first contact, near descent 2.5 mm/s, raw-normal guard 50 N, first-contact latch, lift 20 mm, angular speedl posture correction after 37..39 settle to zero, second search, zero-linear 25.3 gate, then run the XY line"
elif [[ "${STEP4E_VERSION}" == "v30" ]]; then
  SEARCH_DESCRIPTION="previous Step4e/TASE conservative filtered-live-normal flow: same TP motion/search/25.2/25.3 line-entry gate as v29, but the bridge uses a gated filtered live normal only during 25.0 line control; force/admittance gains and tangential speed remain unchanged"
elif [[ "${STEP4E_VERSION}" == "v31" ]]; then
  SEARCH_DESCRIPTION="current Step4e/TASE simple-alpha-normal-follow flow: same TP motion/search/25.2/25.3 line-entry gate as v30, but the bridge uses direct alpha EMA filtered live normal only during 25.0 line control; alpha=0.35, min-force=2.0 N, force/admittance gains and tangential speed remain unchanged"
elif [[ "${STEP4E_VERSION}" == "v20" ]]; then
  SEARCH_DESCRIPTION="two-stage search: v20 moves directly to entry XY with vertical TCP orientation [pi,0,0], searches far 15 mm/s then near 3 mm/s, latches first-contact normal only, lifts base-Z 2 mm, aligns attitude while detached, reacquires 5 N along the locked normal, then runs the 5 mm/s XY line"
elif [[ "${STEP4E_VERSION}" == "v21" ]]; then
  SEARCH_DESCRIPTION="two-stage search: v21 moves to vertical TCP orientation, searches far 15 mm/s for 80 mm max, then near 3 mm/s to 92 mm max depth, latches first-contact normal only, detaches along the locked normal, then previews the minimal-rotation target without executing contact-posture motion; no 5N acquisition and no XY line"
elif [[ "${STEP4E_VERSION}" == "v19" ]]; then
  SEARCH_DESCRIPTION="two-stage search: v19 first moves TCP orientation to vertical [pi,0,0], then XY entry and downward speedl-search; far 15 mm/s for 130 mm then near 3 mm/s up to 150 mm max depth; after contact latch it holds 5 N point contact and aligns TCP z to the contact normal before 5 mm/s XY line motion"
elif [[ "${STEP4E_VERSION}" == "v18" ]]; then
  SEARCH_DESCRIPTION="two-stage search: v18 first moves TCP orientation to vertical [pi,0,0], then XY entry and downward speedl-search; far 15 mm/s for 130 mm then near 3 mm/s up to 150 mm max depth; after contact latch it holds 5 N point contact and aligns TCP z to the contact normal before XY line motion"
elif [[ "${STEP4E_VERSION}" == "v17" ]]; then
  SEARCH_DESCRIPTION="two-stage search: v17 first moves TCP orientation to vertical [pi,0,0], then XY entry and downward speedl-search; far 15 mm/s for 130 mm then near 3 mm/s up to 150 mm max depth; bridge attitude outer-loop starts after contact latch"
elif [[ "${STEP4E_VERSION}" == "v16" ]]; then
  SEARCH_DESCRIPTION="two-stage search: v16 removes fixed-Z pre-search movel; after XY entry it directly speedl-searches downward, far 15 mm/s for 130 mm then near 3 mm/s up to 150 mm max depth; bridge normal/reacquire limit = 2 mm/s"
elif [[ "${STEP4E_VERSION}" == "v15" ]]; then
  SEARCH_DESCRIPTION="two-stage search: v15 keeps v14 force-safe line settings and adds stage25 command-valid grace to avoid first-cycle RTDE/URScript skew"
elif [[ "${STEP4E_VERSION}" == "v14" ]]; then
  SEARCH_DESCRIPTION="two-stage search: first move to fixed validated search-start z=98.35 mm, then far 15 mm/s for 80 mm, near 3 mm/s for final 12 mm, 92 mm max depth; v14 starts admittance integration only in stage25 and settles normal force before line motion"
elif [[ "${STEP4E_VERSION}" == "v13" ]]; then
  SEARCH_DESCRIPTION="two-stage search: first move to fixed validated search-start z=98.35 mm, then far 15 mm/s for 80 mm, near 3 mm/s for final 12 mm, 92 mm max depth; v13 fixes v12 high-start no-contact miss"
elif [[ "${STEP4E_VERSION}" == "v12" ]]; then
  SEARCH_DESCRIPTION="two-stage search: far 15 mm/s for 80 mm, then near 3 mm/s for the final 12 mm, 92 mm max depth; v12 keeps v11 attitude signs and fixes endpoint success/retract"
elif [[ "${STEP4E_VERSION}" == "v11" ]]; then
  SEARCH_DESCRIPTION="two-stage search: far 15 mm/s for 80 mm, then near 3 mm/s for the final 12 mm, 92 mm max depth; v11 keeps v10 force guards and uses independent attitude signs wx=+1, wy=-1"
elif [[ "${STEP4E_VERSION}" == "v10" ]]; then
  SEARCH_DESCRIPTION="two-stage search: far 15 mm/s for 80 mm, then near 3 mm/s for the final 12 mm, 92 mm max depth; v10 keeps v9 force guards and reverses the attitude outer-loop direction"
elif [[ "${STEP4E_VERSION}" == "v9" ]]; then
  SEARCH_DESCRIPTION="two-stage search: far 15 mm/s for 80 mm, then near 3 mm/s for the final 12 mm, 92 mm max depth; v9 keeps v8 latched-normal line control and raises raw normal guard to 100N / torque guard to 1.0Nm"
elif [[ "${STEP4E_VERSION}" == "v8" ]]; then
  SEARCH_DESCRIPTION="two-stage search: far 15 mm/s for 80 mm, then near 3 mm/s for the final 12 mm, 92 mm max depth; v8 keeps v7 stopl(0.1) and uses latched-normal 5N line control"
elif [[ "${STEP4E_VERSION}" == "v7" ]]; then
  SEARCH_DESCRIPTION="two-stage search: far 15 mm/s for 80 mm, then near 3 mm/s for the final 12 mm, 92 mm max depth; v7 keeps v6 normal command sign/30N guard and uses stopl(0.1)"
elif [[ "${STEP4E_VERSION}" == "v6" ]]; then
  SEARCH_DESCRIPTION="two-stage search: far 15 mm/s for 80 mm, then near 3 mm/s for the final 12 mm, 92 mm max depth; v6 flips normal command sign to unload after contact"
elif [[ "${STEP4E_VERSION}" == "v5" ]]; then
  SEARCH_DESCRIPTION="two-stage search: far 15 mm/s for 80 mm, then near 3 mm/s for the final 12 mm, 92 mm max depth"
elif [[ "${STEP4E_VERSION}" == "v4" ]]; then
  SEARCH_DESCRIPTION="two-stage search: far 10 mm/s for 80 mm, then near 3 mm/s for the final 10 mm, 90 mm max depth"
elif [[ "${STEP4E_VERSION}" == "v3" ]]; then
  SEARCH_DESCRIPTION="two-stage search: far 10 mm/s for 45 mm, then near 3 mm/s until 70 mm max depth"
else
  SEARCH_DESCRIPTION="deterministic 3 mm/s downward search"
fi

usage() {
  cat <<'USAGE'
Usage:
  step4e-line-v1-operator.sh preview-autowatch
  step4e-line-v1-operator.sh hold-autowatch
  step4e-line-v1-operator.sh line-autowatch
  step4e-line-v1-operator.sh axis-autowatch
  step4e-line-v1-operator.sh geo-autowatch
  step4e-line-v1-operator.sh witness-autowatch
  step4e-line-v1-operator.sh preview-bridge
  step4e-line-v1-operator.sh hold-bridge
  step4e-line-v1-operator.sh line-bridge
  step4e-line-v1-operator.sh line-bridge-fast
  step4e-line-v1-operator.sh axis-bridge
  step4e-line-v1-operator.sh geo-bridge
  step4e-line-v1-operator.sh witness-bridge
  step4e-line-v1-operator.sh prep-long-checks

Teach Pendant programs:
  /programs/andyl/kunwei/step4/step4e_preview_line_${STEP4E_VERSION}.urp
  /programs/andyl/kunwei/step4/step4e_contact_hold_line_${STEP4E_VERSION}.urp
  /programs/andyl/kunwei/step4/step4e_line_outerloop_${STEP4E_VERSION}.urp
  /programs/andyl/kunwei/step4/step4e_ball_first_contact_p0_v1.urp
  /programs/andyl/kunwei/step4/step4e_ball_vs_cyl_contact_p0_v1.urp
  /programs/andyl/kunwei/step4/step4e_attitude_axis_iso_v1.urp
  current v31 line package lives under /programs/andyl/kunwei/step4/
  previous v30 line package lives under /programs/andyl/kunwei/step4/
  locked-normal fallback v29 line package lives under /programs/andyl/kunwei/step4/
  failed/archive v21/v22/v23/v24/v25/v26/v27 line packages live under /programs/andyl/kunwei/step4/step4e/
  failed/archive v28 line package lives under /programs/andyl/kunwei/step4/; it failed from the old bridge profile mismatch, not motion parameters.
  canonical Step4e/TASE flow table: ${ROOT}/STEP4E_FLOW.md

Bridge lifecycle:
  * bridge starts Kunwei/RTDE bridge immediately, then waits up to 45 s for TP Play.
  * line-bridge-fast requires a fresh long-check cache and only runs short
    loaded-program/safety/no-old-bridge checks at trigger time.
  * autowatch waits for TP Play before starting the bridge; keep it for manual
    testing only, not for the normal 开bridge trigger.
  * bridge is stopped when TP program stops, safety is not NORMAL, or Dashboard is unreachable.

Step4e motion boundary:
  preview: no robot motion, echo Step4e command registers only.
  geo: P0 ball-first witness; touch once at low threshold, visual dwell, then 2 mm base-Z retract.
  witness: P0 pair witness; ball contact then housing/cylindrical-face contact in one TP program.
  axis: no-contact four-quadrant attitude axis isolation.
  hold: contact search, then 12 s force/orientation hold.
  line: contact search, then straight XY line from the two TP screenshot points.
  force target = 5 N for hold/line only; geo contact witness triggers around 1-1.5 N.
  raw normal guard = ${MAX_NORMAL_FORCE_N} N, force norm guard = 50 N, torque guard = ${MAX_TORQUE_NORM_NM} Nm.
USAGE
}

select_mode() {
  local requested="$1"
  case "${requested}" in
    preview-autowatch|preview-bridge)
      EXPECTED_PROGRAM="${PROGRAM_PREVIEW}"
      EXPECTED_BASENAME="step4e_preview_line_${STEP4E_VERSION}.urp"
      STEP4E_MODE="preview"
      RUN_LABEL="step4e_preview_line_${STEP4E_VERSION}"
      ;;
    hold-autowatch|hold-bridge)
      EXPECTED_PROGRAM="${PROGRAM_HOLD}"
      EXPECTED_BASENAME="step4e_contact_hold_line_${STEP4E_VERSION}.urp"
      STEP4E_MODE="hold"
      RUN_LABEL="step4e_contact_hold_line_${STEP4E_VERSION}"
      ;;
    line-autowatch|line-bridge|line-bridge-fast)
      EXPECTED_PROGRAM="${PROGRAM_LINE}"
      if [[ "${STEP4E_VERSION}" == "v21" ]]; then
        EXPECTED_BASENAME="step4e_detached_movel_minrot_v21.urp"
      elif [[ "${STEP4E_VERSION}" == "v22" || "${STEP4E_VERSION}" == "v23" || "${STEP4E_VERSION}" == "v24" || "${STEP4E_VERSION}" == "v25" || "${STEP4E_VERSION}" == "v26" || "${STEP4E_VERSION}" == "v27" || "${STEP4E_VERSION}" == "v28" || "${STEP4E_VERSION}" == "v29" || "${STEP4E_VERSION}" == "v30" || "${STEP4E_VERSION}" == "v31" ]]; then
        EXPECTED_BASENAME="step4e_seed_normal_loop_${STEP4E_VERSION}.urp"
      else
        EXPECTED_BASENAME="step4e_line_outerloop_${STEP4E_VERSION}.urp"
      fi
      STEP4E_MODE="line"
      if [[ "${STEP4E_VERSION}" == "v21" ]]; then
        RUN_LABEL="step4e_detached_movel_minrot_v21"
      elif [[ "${STEP4E_VERSION}" == "v22" || "${STEP4E_VERSION}" == "v23" || "${STEP4E_VERSION}" == "v24" || "${STEP4E_VERSION}" == "v25" || "${STEP4E_VERSION}" == "v26" || "${STEP4E_VERSION}" == "v27" || "${STEP4E_VERSION}" == "v28" || "${STEP4E_VERSION}" == "v29" || "${STEP4E_VERSION}" == "v30" || "${STEP4E_VERSION}" == "v31" ]]; then
        RUN_LABEL="step4e_seed_normal_loop_${STEP4E_VERSION}"
      else
        RUN_LABEL="step4e_line_outerloop_${STEP4E_VERSION}"
      fi
      ;;
    axis-autowatch|axis-bridge)
      EXPECTED_PROGRAM="${PROGRAM_AXIS_ISO}"
      EXPECTED_BASENAME="step4e_attitude_axis_iso_v1.urp"
      STEP4E_MODE="axis_iso"
      RUN_LABEL="step4e_attitude_axis_iso_v1"
      CONFIRM_LABEL="axis"
      ;;
    geo-autowatch|geo-bridge)
      EXPECTED_PROGRAM="${PROGRAM_GEO}"
      EXPECTED_BASENAME="step4e_ball_first_contact_p0_v1.urp"
      STEP4E_MODE="off"
      RUN_LABEL="step4e_ball_first_contact_p0_v1"
      CONFIRM_LABEL="geo"
      ;;
    witness-autowatch|witness-bridge)
      EXPECTED_PROGRAM="${PROGRAM_WITNESS}"
      EXPECTED_BASENAME="step4e_ball_vs_cyl_contact_p0_v1.urp"
      STEP4E_MODE="off"
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
  python3 - "${LONG_CHECK_CACHE}" "${LONG_CHECK_TTL_S}" "${ROBOT_HOST}" <<'PY'
import json
import sys
import time
from pathlib import Path

cache = Path(sys.argv[1])
ttl_s = float(sys.argv[2])
host = sys.argv[3]
if ttl_s <= 0 or not cache.is_file():
    raise SystemExit(1)
try:
    payload = json.loads(cache.read_text(encoding="utf-8"))
except Exception:
    raise SystemExit(1)
age_s = time.time() - float(payload.get("checked_at_epoch", 0.0))
if payload.get("ok") is True and payload.get("robot_host") == host and 0.0 <= age_s <= ttl_s:
    print(f"[operator] long-check cache hit: age={age_s:.1f}s ttl={ttl_s:.1f}s {cache}")
    raise SystemExit(0)
raise SystemExit(1)
PY
}

refresh_bench_gate_cache() {
  mkdir -p "$(dirname "${LONG_CHECK_CACHE}")"
  local tmp
  tmp="$(mktemp)"
  if run_bench_gate | tee "${tmp}"; then
    python3 - "${tmp}" "${LONG_CHECK_CACHE}" "${ROBOT_HOST}" <<'PY'
import json
import sys
import time
from pathlib import Path

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
  echo "run: STEP4E_VERSION=${STEP4E_VERSION} ${BASH_SOURCE[0]} prep-long-checks"
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

def dash_cmd(cmd, timeout=1.0):
    with socket.create_connection((host, port), timeout=timeout) as s:
        s.settimeout(timeout)
        try:
            s.recv(4096)
        except socket.timeout:
            pass
        s.sendall((cmd + "\n").encode("ascii"))
        return s.recv(4096).decode("utf-8", errors="replace").strip()

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
      echo "refusing: loaded program is not expected Step4e ${STEP4E_MODE}"
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
        echo "[autowatch] expected Step4e ${STEP4E_MODE} program is running; starting bridge."
        return 0
      elif [[ "${rc}" == "20" || "${rc}" == "21" ]]; then
        echo "[autowatch] refusing: safety is not NORMAL or loaded program is not expected Step4e ${STEP4E_MODE}"
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
        echo "[operator] loaded program is no longer expected Step4e ${STEP4E_MODE}"
        stop_bridge_process "${bridge_pid}" "loaded program mismatch"
        return 0
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
  if [[ "${STEP4E_BACKGROUND_PUSH_AFTER_LIVE}" != "1" ]]; then
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
  if [[ "${STEP4E_STAGE25_ONLY}" == "1" ]]; then
    stage25_only_args+=(--step4e-integrate-stage25-only)
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
    --baseline-s 5 \
    --rezero-s 1 \
    --duration-s "${BRIDGE_DURATION_S}" \
    --rtde-hz 500 \
    --socket-timeout-s 0.0 \
    --sensor-stale-s 0.10 \
    --target-force-n 5 \
    --normal-axis fz \
    --normal-sign 1 \
    --max-normal-force-n "${MAX_NORMAL_FORCE_N}" \
    --max-force-norm-n 50 \
    --max-torque-norm-nm "${MAX_TORQUE_NORM_NM}" \
    --step4e-mode "${STEP4E_MODE}" \
    --step4e-version "${STEP4E_VERSION}" \
    --step4e-line-speed-m-s "${STEP4E_LINE_SPEED_M_S}" \
    --step4e-line-settle-s "${STEP4E_LINE_SETTLE_S}" \
    "${stage25_only_args[@]}" \
    --step4e-path-p-gain 1.5 \
    --step4e-motion-limit-m-s 0.004 \
    --step4e-total-linear-limit-m-s 0.006 \
    --step4e-normal-velocity-limit-m-s "${STEP4E_NORMAL_VELOCITY_LIMIT_M_S}" \
    --step4e-force-p-gain "${STEP4E_FORCE_P_GAIN}" \
    --step4e-force-i-gain "${STEP4E_FORCE_I_GAIN}" \
    --step4e-force-damping "${STEP4E_FORCE_DAMPING}" \
    --step4e-normal-command-sign "${STEP4E_NORMAL_COMMAND_SIGN}" \
    --step4e-integral-limit-n-s "${STEP4E_INTEGRAL_LIMIT_N_S}" \
    --step4e-min-force-for-control-n 1.0 \
    --step4e-acquire-grace-s 0.25 \
    --step4e-reacquire-velocity-m-s "${STEP4E_REACQUIRE_VELOCITY_M_S}" \
    --step4e-orientation-gain "${STEP4E_ORIENTATION_GAIN}" \
    --step4e-orientation-wx-sign "${STEP4E_ORIENTATION_WX_SIGN}" \
    --step4e-orientation-wy-sign "${STEP4E_ORIENTATION_WY_SIGN}" \
    --step4e-angular-limit-rad-s "${STEP4E_ANGULAR_LIMIT_RAD_S}" \
    --step4e-contact-offset-min-fz-n 1.0 \
    --step4e-normal-follow-mode "${STEP4E_NORMAL_FOLLOW_MODE}" \
    --step4e-normal-filter-tau-s "${STEP4E_NORMAL_FILTER_TAU_S}" \
    --step4e-normal-filter-alpha "${STEP4E_NORMAL_FILTER_ALPHA}" \
    --step4e-normal-max-rate-rad-s "${STEP4E_NORMAL_MAX_RATE_RAD_S}" \
    --step4e-normal-min-force-n "${STEP4E_NORMAL_MIN_FORCE_N}" \
    --step4e-normal-max-angle-from-latch-deg "${STEP4E_NORMAL_MAX_ANGLE_FROM_LATCH_DEG}" \
    --step4e-normal-friction-projection "${STEP4E_NORMAL_FRICTION_PROJECTION}" \
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
select_mode "${mode}"
CONFIRM_TOKEN="${CONFIRM_LABEL:-${STEP4E_MODE}}"
CONFIRM_TOKEN="${CONFIRM_TOKEN^^}"

case "${mode}" in
  *-autowatch)
    cat <<WARNING
STEP4e ${STEP4E_MODE} ${STEP4E_VERSION} autowatch.
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
    require_bench_gate_cache
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
STEP4e ${STEP4E_MODE} ${STEP4E_VERSION} lifecycle bridge.
This sends Kunwei 48 AA 0D 0A and writes UR RTDE input registers.
It does not send URScript from Ubuntu.

Before pressing Play, open this Teach Pendant program:
  ${EXPECTED_PROGRAM}

Motion/control:
  preview = no motion, geo/hold/line setup = ${SEARCH_DESCRIPTION}
  line XY speed command = ${STEP4E_LINE_SPEED_M_S} m/s, max Cartesian command = 6 mm/s
  speedl acceleration = 300 mm/s^2, hold time = 2 ms
  force target = 5 N for hold/line only; geo contact witness triggers around 1-1.5 N
  raw normal guard = ${MAX_NORMAL_FORCE_N} N, force norm guard = 50 N, torque guard = ${MAX_TORQUE_NORM_NM} Nm
  attitude proxy = bounded wx/wy velocity command, gain = ${STEP4E_ORIENTATION_GAIN}, angular limit = ${STEP4E_ANGULAR_LIMIT_RAD_S} rad/s, wx sign = ${STEP4E_ORIENTATION_WX_SIGN}, wy sign = ${STEP4E_ORIENTATION_WY_SIGN}, yaw frozen
  normal follow = ${STEP4E_NORMAL_FOLLOW_MODE}, tau = ${STEP4E_NORMAL_FILTER_TAU_S}s, max rate = ${STEP4E_NORMAL_MAX_RATE_RAD_S} rad/s, min force = ${STEP4E_NORMAL_MIN_FORCE_N} N, gate = ${STEP4E_NORMAL_MAX_ANGLE_FROM_LATCH_DEG} deg, friction projection = ${STEP4E_NORMAL_FRICTION_PROJECTION}

Type START_STEP4E_${CONFIRM_TOKEN}_${STEP4E_VERSION^^} to continue:
WARNING
    read -r confirm
    if [[ "${confirm}" != "START_STEP4E_${CONFIRM_TOKEN}_${STEP4E_VERSION^^}" ]]; then
      echo "aborted"
      exit 2
    fi
    run_bench_gate_cached
    ensure_no_existing_bridge
    run_bridge_for_mode "${RUN_ROOT}/bridge_${RUN_LABEL}_${STAMP}" 0
    ;;
esac
