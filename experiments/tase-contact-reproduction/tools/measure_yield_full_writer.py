#!/usr/bin/env python3
"""Offline full-writer compute diagnostic; no device IO, pacing, or timing claim."""
import argparse
import hashlib
import json
import os
import platform
import subprocess
import sys
import time
from pathlib import Path
from datetime import datetime, timezone

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "tests"), str(ROOT / "tools")]

from build_contact_qp import build  # noqa: E402
from yield_full_writer_offline import (  # noqa: E402
    METHODS,
    PRODUCTION_RUNTIME_DEADLINE_S,
    PROTOCOL_PATH,
    exercise_full_writer,
    protocol_parameters,
)

MEASUREMENT_BOUNDARY = (
    "whole-loop wall/thread-CPU is poll-start through pre-sleep on each "
    "active execute_attempt iteration; synthetic logical-clock sleep is excluded. "
    "The final no-sleep interval includes evidence finalization and fixture state capture; "
    "it is retained and classified separately. Native timings also retain the one "
    "explicit baseline prewarm call preceding execute_attempt. "
    "qualification_* is CanonicalQualificationControl.step; native_* is "
    "YieldContactRuntime.step. Logical sample clocks are simulated receive "
    "clocks. Real CPU/wall are still measured. Not formal timing "
    "qualification or IO latency."
)
SCOPE = (
    "offline unpaced stationary-observation composition of native "
    "YieldContactProvider + CanonicalQualificationControl.step inside "
    "LiveR004Writer.execute_attempt; in-memory transport; fixture-provided "
    "baseline-success; existing writer GC disable/restore; runtime "
    f"deadline_s=None (production remains {PRODUCTION_RUNTIME_DEADLINE_S} s); "
    "figure-8 envelope already used by the native qualification fixture; "
    "no robot IO, transport qualification, or physical task claim"
)


def _sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _summary(values_ns):
    if len(values_ns) == 0:
        return {"n": 0, "p50_ms": None, "p95_ms": None, "p99_ms": None, "max_ms": None, "over_2ms": 0}
    values = np.asarray(values_ns, dtype=np.int64)
    return {
        "n": int(values.size),
        "p50_ms": float(np.quantile(values, 0.5) / 1e6),
        "p95_ms": float(np.quantile(values, 0.95) / 1e6),
        "p99_ms": float(np.quantile(values, 0.99) / 1e6),
        "max_ms": float(values.max() / 1e6),
        "over_2ms": int(np.sum(values > 2_000_000)),
    }


def _git_head():
    try:
        return subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    except (OSError, subprocess.CalledProcessError):
        return None


def _phase_coverage(phases):
    return {
        "entry": int(sum(phase == "entry" for phase in phases)),
        "path": int(sum(phase == "path" for phase in phases)),
        "other": int(sum(phase not in {"entry", "path"} for phase in phases)),
        "n": len(phases),
    }


def measure(out):
    lib = build(out / "build")
    result = {
        "scope": SCOPE,
        "measurement_boundary": MEASUREMENT_BOUNDARY,
        "period_ns": 2_000_000,
        "platform": platform.platform(),
        "python": sys.version,
        "cpu_affinity": sorted(os.sched_getaffinity(0)),
        "base_commit": _git_head(),
        "protocol_path": str(PROTOCOL_PATH),
        "protocol_sha256": _sha256(PROTOCOL_PATH),
        "production_runtime_deadline_s": PRODUCTION_RUNTIME_DEADLINE_S,
        "offline_runtime_deadline_s": None,
        "gc_policy": "existing LiveR004Writer.execute_attempt disable/restore; fixture gc.collect once as arm() equivalent; no production GC change",
        "methods": {},
        "failures": [],
        "limitations": [
            "stationary calibrated-home observations; not closed-loop task evidence",
            "in-memory transport; no real RTDE/Kunwei admission",
            "simulated receive clocks; measured CPU/wall are host compute only",
            "baseline-success and startup latch are fixture-provided",
            "CanonicalQualificationControl is initialized as in the native qualification fixture; unstubbed construction fails here because frozen ur_xacro sha256 differs from /opt/ros/humble/share/ur_description/urdf/ur.urdf.xacro",
            "motion envelope is the existing native qualification figure-8 profile",
            "runtime deadline_s=None records outliers rather than hiding them",
            "not formal timing qualification or IO latency",
        ],
    }
    (out / "results.json").write_text(json.dumps(result, indent=2) + "\n")
    for method in METHODS:
        parameters = protocol_parameters(method)
        started = time.time()
        with pytest.MonkeyPatch.context() as monkeypatch:
            run = exercise_full_writer(
                out / method.lower(),
                monkeypatch,
                qp_library=lib,
                method=method,
                measure=True,
            )
        wall = np.asarray(run.loop_wall_ns, dtype=np.int64)
        cpu = np.asarray(run.loop_cpu_ns, dtype=np.int64)
        native = np.asarray(run.native_wall_ns, dtype=np.int64)
        native_cpu = np.asarray(run.native_cpu_ns, dtype=np.int64)
        qual = np.asarray(run.qualification_wall_ns, dtype=np.int64)
        np.save(out / f"{method}-loop-wall-ns.npy", wall)
        np.save(out / f"{method}-loop-cpu-ns.npy", cpu)
        np.save(out / f"{method}-native-wall-ns.npy", native)
        np.save(out / f"{method}-native-cpu-ns.npy", native_cpu)
        np.save(out / f"{method}-qualification-wall-ns.npy", qual)
        np.save(out / f"{method}-qualification-cpu-ns.npy", np.asarray(run.qualification_cpu_ns, dtype=np.int64))
        assert len(run.loop_kinds)==len(wall)==len(cpu)
        (out / f"{method}-loop-kinds.json").write_text(json.dumps(run.loop_kinds)+'\n')
        active=np.asarray([k=='active_to_presleep' for k in run.loop_kinds],dtype=bool)
        (out / f"{method}-gc.json").write_text(json.dumps(run.gc_events, indent=2) + "\n")
        failure = None if run.error is None else f"{type(run.error).__name__}: {run.error}"
        if failure is not None:
            result["failures"].append({"method": method, "error": failure})
        metrics = None if run.evidence is None else dict(run.evidence.metrics)
        payload = {
            "parameters": parameters,
            "runtime_identity": run.runtime_identity,
            "controller_identity_payload": run.runtime.controller.identity_payload,
            "model_urdf_sha256": run.runtime.model_urdf_sha256,
            "solver_profile": run.runtime.solver_profile.as_dict(),
            "deadline_s": run.deadline_s,
            "motion_profile_xy_path_speed_m_s": run.motion_profile_xy_path_speed_m_s,
            "provider_id": run.provider_id,
            "packet_count": run.packet_count,
            "evidence_samples": len(run.samples),
            "published_command_packets": len(run.published),
            "phase_coverage": _phase_coverage(run.phases),
            "loop_wall": _summary(wall),
            "loop_cpu": _summary(cpu),
            "active_loop_wall": _summary(wall[active]),
            "active_loop_cpu": _summary(cpu[active]),
            "terminal_with_finalization_wall": _summary(wall[~active]),
            "terminal_with_finalization_cpu": _summary(cpu[~active]),
            "native_wall": _summary(native),
            "native_cpu": _summary(native_cpu),
            "qualification_wall": _summary(qual),
            "qualification_cpu": _summary(run.qualification_cpu_ns),
            "gc_event_count": len(run.gc_events),
            "gc_states_in_qualification_step": sorted(set(run.gc_states_in_loop)),
            "gc_enabled_after": run.gc_enabled_after,
            "eligible": None if run.evidence is None else run.evidence.eligible,
            "evidence_metrics": metrics,
            "error": failure,
            "wall_s": time.time() - started,
            "logical_clock_end_s": run.clock.t,
            "no_discarded_warmup_or_outliers": True,
        }
        result["methods"][method] = payload
        (out / "results.json").write_text(json.dumps(result, indent=2) + "\n")
        print(method, payload["loop_wall"], payload["phase_coverage"], payload["error"], flush=True)
    (out / "bindings.json").write_text(
        json.dumps(
            {
                "protocol_sha256": result["protocol_sha256"],
                "methods": {method: protocol_parameters(method) for method in METHODS},
                "production_runtime_deadline_s": PRODUCTION_RUNTIME_DEADLINE_S,
                "offline_runtime_deadline_s": None,
                "source_sha256": {
                    "yield_full_writer_offline.py": _sha256(ROOT / "tests/yield_full_writer_offline.py"),
                    "measure_yield_full_writer.py": _sha256(Path(__file__)),
                    "yield_contact_provider.py": _sha256(ROOT / "tools/yield_contact_provider.py"),
                    "yield_contact_runtime.py": _sha256(ROOT / "tools/yield_contact_runtime.py"),
                    "qualification.py": _sha256(ROOT / "tools/step5d_autotune_v4_r004/qualification.py"),
                    "step5d_autotune_v4_r004_live_writer.py": _sha256(
                        ROOT / "tools/step5d_autotune_v4_r004_live_writer.py"
                    ),
                },
                "note": "MSFC uses FT-v1 g50, never the original protocol seed. Timing is host compute with fake logical clocks.",
            },
            indent=2,
        )
        + "\n"
    )
    return 1 if result['failures'] else 0


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,required=True)
    out=parser.parse_args(argv).output.resolve()
    out.mkdir(parents=True,exist_ok=False)
    manifest={'contract_id':'ur10e_concurrency_contract_v1','task':'native full-writer composition diagnostic',
        'dependencies':[str(PROTOCOL_PATH)],'resource_lane':'CPU throughput','workers':1,
        'claim_class':'offline unpaced development diagnostic','live_executed':False,
        'started_at':datetime.now(timezone.utc).isoformat(),'output_paths':[str(out)],'exit_code':None}
    (out/'start.json').write_text(json.dumps(manifest,indent=2)+'\n')
    code=1
    try:
        code=measure(out)
        return code
    finally:
        manifest.update(exit_code=code,finished_at=datetime.now(timezone.utc).isoformat())
        (out/'parallel_run_manifest.json').write_text(json.dumps(manifest,indent=2)+'\n')


if __name__ == "__main__":
    raise SystemExit(main())
