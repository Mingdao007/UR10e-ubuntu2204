#!/usr/bin/env python3
"""Emit the source-bound, read-only v31 500 Hz formal timing program."""

from __future__ import annotations

import hashlib
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
TOOLS = ROOT / "tools"


def sha256_text(source: str) -> str:
    return hashlib.sha256(source.encode()).hexdigest()


def v31_harness() -> str:
    source = (TOOLS / "run_step5d_v30_remote_timing.py").read_text(encoding="utf-8")
    source = source.replace("from dataclasses import dataclass", "from dataclasses import dataclass, replace", 1)
    old_qdot = '"qdot_cap_rad_s": 0.05,'
    if source.count(old_qdot) != 2:
        raise RuntimeError("unexpected v30 qdot profile shape")
    source = source.replace(old_qdot, '"qdot_cap_rad_s": 0.5,')
    observation_tail = "            dt_s=0.002,\n        )\n\n    outer_state ="
    if source.count(observation_tail) != 1:
        raise RuntimeError("unexpected observation contract shape")
    source = source.replace(
        observation_tail,
        '            dt_s=0.002,\n            normal_motion_policy="frame_contract_only",\n        )\n\n    outer_state =',
    )
    safe_candidate = """                force_nonpressing_desired=True,
            )
            raw_candidate = policy.compute(observation)
"""
    safe_candidate_v31 = """                force_nonpressing_desired=True,
            )
            raw_candidate = policy.compute(observation)
            raw_candidate = replace(raw_candidate, solver_status="v31_structural_safe_hold_test")
"""
    if source.count(safe_candidate) != 2:
        raise RuntimeError("unexpected safe-hold candidate shape")
    source = source.replace(safe_candidate, safe_candidate_v31)
    warm_expect = 'decision.action != "safe_hold"\n                or command.qdot'
    loop_expect = 'decision.action != "safe_hold"\n                or command.qdot'
    if source.count(warm_expect) != 2:
        raise RuntimeError("unexpected rejection-branch expectation shape")
    source = source.replace(warm_expect, 'decision.action != "stop"\n                or command.qdot')
    register_expect = "or register_values[43] != 1.0\n                or register_values[28] != 0.0"
    if source.count(register_expect) != 2:
        raise RuntimeError("unexpected rejection register expectation shape")
    source = source.replace(
        register_expect,
        "or register_values[43] != 0.0\n                or register_values[28] != 1.0",
    )
    import_needle = "        step5d_omega_bounds,\n"
    source = source.replace(import_needle, import_needle + "        step5d_publish_action,\n", 1)
    stale_tail = """    )

    module_sha_fields = {
"""
    stale_v31 = """    )
    late_candidate = {
        **{name: 0.01 for name in BRIDGE_INPUT_NAMES[:6]},
        "step4e_cmd_valid": 1.0,
        "step4e_controller_state": float(STEP5D_STAGE25_JOINT_LAYOUT_CODE),
    }
    late_action = step5d_publish_action(
        late_candidate,
        robot_stage=25.0,
        v30_contract_profile=True,
        stop_dominant=False,
        schedule_late=True,
        publish_guard_approved_late_command=True,
        last_published_command={name: 0.0 for name in BRIDGE_INPUT_NAMES[:6]},
    )
    controller_stale_hold_fault_evidence.update({
        "pass": bool(controller_stale_hold_fault_evidence.get("pass")) and late_action == "fresh_command",
        "late_candidate_policy": "publish_when_guard_approved",
        "guard_approved_late_action": late_action,
        "continuous_stale_stop_s": 1.0,
        "heartbeat_stale_stop_s": 1.0,
    })

    module_sha_fields = {
"""
    if source.count(stale_tail) != 1:
        raise RuntimeError("unexpected stale-hold evidence shape")
    source = source.replace(stale_tail, stale_v31, 1)
    print_needle = '    print(json.dumps(payload, separators=(",", ":"), sort_keys=True))'
    payload_patch = """    payload["schema_version"] = "step5d_v31_formal_timing_raw_v1"
    payload["v31_profile_contract"] = {
        "profile": "step5d_strict_rnn_ablation_v31",
        "qdot_cap_rad_s": 0.5,
        "normal_motion_policy": "frame_contract_only",
        "backend": "cupy",
        "inner_iterations": 512,
        "control_hz": 500.0,
        "scheduler_policy_required": "SCHED_FIFO",
        "scheduler_priority_required": 20,
        "measured_rejection_branch": "structural_stop_zero_qdot",
    }
    print(json.dumps(payload, separators=(",", ":"), sort_keys=True))"""
    if source.count(print_needle) != 1:
        raise RuntimeError("unexpected timing payload output shape")
    return source.replace(print_needle, payload_patch, 1)


def build_bundle() -> str:
    modules = {
        "contact_semantics": (TOOLS / "contact_semantics.py").read_text(encoding="utf-8"),
        "step5c_strict_rnn": (TOOLS / "step5c_strict_rnn.py").read_text(encoding="utf-8"),
        "step5d_paper_outer_loop": (TOOLS / "step5d_paper_outer_loop.py").read_text(encoding="utf-8"),
        "step5d_control_contract": (TOOLS / "step5d_control_contract.py").read_text(encoding="utf-8"),
        "step5d_runtime_interface": (TOOLS / "step5d_runtime_interface.py").read_text(encoding="utf-8"),
        "step5c_calibrated_kinematics_audit": (TOOLS / "step5c_calibrated_kinematics_audit.py").read_text(encoding="utf-8"),
        "kunwei_rtde_bridge": (TOOLS / "kunwei_rtde_bridge.py").read_text(encoding="utf-8"),
    }
    harness = v31_harness()
    auxiliary = {
        "bundler_sha256": sha256_text(Path(__file__).read_text(encoding="utf-8")),
        "aggregator_sha256": sha256_text((TOOLS / "summarize_step5d_v31_timing.py").read_text(encoding="utf-8")),
        "readiness_builder_sha256": sha256_text((TOOLS / "build_step5d_v31_review_binding.py").read_text(encoding="utf-8")),
    }
    lines = [
        "import sys as _sys, types as _types",
        "from pathlib import Path as _Path",
        "_bundle_root = _Path.cwd()",
        "if '--experiment-root' in _sys.argv:",
        "    _bundle_root = _Path(_sys.argv[_sys.argv.index('--experiment-root') + 1]).resolve()",
        "_sys.path.insert(0, str(_bundle_root / 'tools'))",
        "def _install_v30_module(_name, _source, _source_sha):",
        "    _module = _types.ModuleType(_name)",
        "    _module.__file__ = str(_bundle_root / 'tools' / (_name + '.py'))",
        "    _module.__package__ = ''",
        "    _sys.modules[_name] = _module",
        "    exec(compile(_source, _module.__file__, 'exec'), _module.__dict__)",
        "    _module.__v30_source_sha256__ = _source_sha",
        "    _module.__v30_source_delivery__ = 'stdin_bundle'",
    ]
    for name, source in modules.items():
        lines.append(f"_install_v30_module({name!r}, {source!r}, {sha256_text(source)!r})")
    lines.extend([
        f"_harness_source = {harness!r}",
        "_harness_globals = {'__name__': '__main__', '__file__': '<v31-stdin-bundle>/run_step5d_v31_formal_timing.py',",
        f" '__v30_source_sha256__': {sha256_text(harness)!r}, '__v30_source_delivery__': 'stdin_bundle',",
        f" '__v30_auxiliary_source_binding__': {auxiliary!r}}}",
        "exec(compile(_harness_source, _harness_globals['__file__'], 'exec'), _harness_globals)",
    ])
    return "\n".join(lines) + "\n"


if __name__ == "__main__":
    print(build_bundle(), end="")
