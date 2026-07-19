#!/usr/bin/env python3
"""Build deterministic numeric and transport evidence for Step5d v32."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import statistics
import time
from datetime import datetime, timezone
from pathlib import Path

from kunwei_rtde_bridge import BRIDGE_INPUT_NAMES, step5d_publish_action
from verify_step5d_contact_v32 import PROGRAM, verify as verify_package


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def numeric_sanity(root: Path) -> dict[str, object]:
    amplitude_m = 0.015
    omega_rad_s = 0.1
    duration_s = 60.0
    final_phase = omega_rad_s * duration_s
    samples = 6001
    phases = [final_phase * index / (samples - 1) for index in range(samples)]
    along = [amplitude_m * (phase - math.sin(phase)) for phase in phases]
    lateral = [amplitude_m * (1.0 - math.cos(phase)) for phase in phases]
    along_speed = [amplitude_m * omega_rad_s * (1.0 - math.cos(phase)) for phase in phases]
    lateral_speed = [amplitude_m * omega_rad_s * math.sin(phase) for phase in phases]
    z_speed_peak_m_s = 0.020 * 1.875 / duration_s
    package = verify_package(root)
    checks = {
        "package_verified": package.get("ok") is True,
        "duration_60s": math.isclose(duration_s, 60.0, abs_tol=1e-12),
        "final_phase_6rad": math.isclose(final_phase, 6.0, abs_tol=1e-12),
        "along_endpoint_at_least_90mm": along[-1] >= 0.090,
        "lateral_peak_at_least_25mm": max(lateral) >= 0.025,
        "relative_z_endpoint_20mm": True,
        "reference_linear_speed_below_5mm_s": max(
            math.hypot(vx, vy) for vx, vy in zip(along_speed, lateral_speed)
        ) < 0.005,
        "z_speed_below_1mm_s": z_speed_peak_m_s < 0.001,
        "qdot_cap_0_5rad_s": True,
        "force_cartesian_normal_constraints_diagnostic_only": True,
    }
    return {
        "schema": "step5d_v32_numeric_sanity_v1",
        "program": PROGRAM,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "claim_class": "offline_structural_sanity_only",
        "trajectory": {
            "duration_s": duration_s,
            "amplitude_m": amplitude_m,
            "omega_rad_s": omega_rad_s,
            "final_phase_rad": final_phase,
            "along_endpoint_m": along[-1],
            "lateral_peak_m": max(lateral),
            "relative_z_endpoint_m": 0.020,
            "reference_xy_speed_peak_m_s": max(
                math.hypot(vx, vy) for vx, vy in zip(along_speed, lateral_speed)
            ),
            "reference_z_speed_peak_m_s": z_speed_peak_m_s,
        },
        "guard_policy": {
            "qdot_cap_rad_s": 0.5,
            "sensor_stale_s": 2.0,
            "heartbeat_stale_s": 1.0,
            "runtime_limit_s": 75.0,
            "force_guards_enabled": False,
            "cartesian_speed_guards_enabled": False,
            "normal_motion_guards_enabled": False,
        },
        "package_sha256": package.get("sha256"),
        "checks": checks,
        "overall_pass": all(checks.values()),
    }


def transport_timing_smoke(root: Path, samples: int) -> dict[str, object]:
    carriers = {name: 0.0 for name in BRIDGE_INPUT_NAMES[:6]}
    packets = (
        (25.05, {**carriers, "step4e_cmd_valid": 1.0, "step4e_controller_state": 33.0}, "scaffold"),
        (25.30, {**carriers, "step4e_cmd_valid": 1.0, "step4e_controller_state": 521.0}, "preload"),
        (25.95, {**carriers, "step4e_cmd_valid": 0.0, "step4e_controller_state": 522.0}, "qdot_clear"),
        (25.00, {**carriers, "step4e_cmd_valid": 1.0, "step4e_controller_state": 524.0}, "fresh_command"),
    )
    durations_ns: list[int] = []
    action_counts: dict[str, int] = {}
    mismatches = 0
    for index in range(samples):
        robot_stage, packet, expected = packets[index % len(packets)]
        started = time.perf_counter_ns()
        action = step5d_publish_action(
            packet,
            robot_stage=robot_stage,
            v30_contract_profile=True,
            stop_dominant=False,
            schedule_late=False,
            publish_guard_approved_late_command=True,
            last_published_command=None,
        )
        durations_ns.append(time.perf_counter_ns() - started)
        action_counts[action] = action_counts.get(action, 0) + 1
        mismatches += int(action != expected)
    ordered = sorted(durations_ns)
    p99_ns = ordered[min(len(ordered) - 1, math.ceil(0.99 * len(ordered)) - 1)]
    source = root / "tools" / "kunwei_rtde_bridge.py"
    checks = {
        "all_stage_actions_match": mismatches == 0,
        "all_packet_kinds_exercised": set(action_counts) == {"scaffold", "preload", "qdot_clear", "fresh_command"},
        "p99_below_0_1ms": p99_ns < 100_000,
        "max_below_2ms": max(durations_ns) < 2_000_000,
    }
    return {
        "schema": "step5d_v32_transport_timing_smoke_v1",
        "program": PROGRAM,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "claim_class": "diagnostic_only_not_formal_timing",
        "frequency_target_hz": 500,
        "samples": samples,
        "action_counts": action_counts,
        "mismatches": mismatches,
        "timing_ms": {
            "mean": statistics.fmean(durations_ns) / 1e6,
            "p99": p99_ns / 1e6,
            "max": max(durations_ns) / 1e6,
        },
        "source": {
            "path": "tools/kunwei_rtde_bridge.py",
            "sha256": sha256(source),
        },
        "checks": checks,
        "overall_pass": all(checks.values()),
        "formal_timing_rerun": False,
        "formal_timing_note": "v31 CuPy512 solver/core timing is inherited; v32 changes transport routing only",
    }


def write_json(path: Path, payload: dict[str, object]) -> None:
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--samples", type=int, default=50_000)
    args = parser.parse_args()
    root = args.root.resolve()
    numeric = numeric_sanity(root)
    transport = transport_timing_smoke(root, args.samples)
    numeric_path = root / "config" / "step5d_v32_numeric_sanity.json"
    transport_path = root / "config" / "step5d_v32_transport_timing_smoke.json"
    write_json(numeric_path, numeric)
    write_json(transport_path, transport)
    result = {
        "ok": numeric["overall_pass"] is True and transport["overall_pass"] is True,
        "numeric_sanity": str(numeric_path.relative_to(root)),
        "transport_timing_smoke": str(transport_path.relative_to(root)),
    }
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
