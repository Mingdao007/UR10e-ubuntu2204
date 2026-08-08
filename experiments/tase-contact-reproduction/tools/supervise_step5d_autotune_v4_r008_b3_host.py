#!/usr/bin/env python3
"""Long-lived supervisor parent for r008 B3 host (Wave-1 launch hygiene).

Launches ``run_step5d_autotune_v4_r008_b3_two_stage live`` as a **direct child**
and waits until it exits, so authority ``os.getppid()`` stays bound to this
process. Prevents the bare-``nohup`` orphan path that masked true reason 61 as
a transport/lease cleanup error (132847 vs supervised 133033).

Does **not** upload, prepare, play, or arm by default. Operator must prepare
the empty run dir and thresholds first; this tool only supervises host.

By default also attaches the finance body_observer watch (diagnose-only; never
kills the formal host). Use ``--no-body-observer`` to disable.

See: ~/claude_handoffs/step5d-autotune-v4-b3-wave1-launch-sop-20260804.md
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Mapping, Sequence

ROOT = Path(__file__).resolve().parents[1]

DEFAULT_CONTROL_PY = Path(
    "/home/andy/.local/share/step5d-autotune-v3/runtimes/"
    "8f980abdd3d9a4ebef460be259f6cd7c0c802015ee553201d3a888be83252475/"
    "control/bin/python"
)
DEFAULT_RELEASE_MANIFEST_SHA256 = (
    "15f2098b8d2bf8689ff36abbc375ffa40a7e8954ae66fa6e95324c61e12aa802"
)
DEFAULT_EOAT_SHA256 = (
    "1979482cd8377b92555fcee72f120928838ed8d57fda8af1e6c1aaebc3ebccf4"
)
PROGRAM = "step5d_strict_rnn_autotune_v4_r008_b3_two_stage"
LAUNCH_PROFILE = (
    "config/step5/step5d_autotune_v4_r008_b3_two_stage_launch_profile.json"
)


def _die(msg: str, code: int = 2) -> int:
    print(f"supervise_b3_host: {msg}", file=sys.stderr)
    return code


def _load_ids(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("ids-json must be an object")
    required = (
        "route_id",
        "attempt_id",
        "session_id",
        "session_epoch",
        "script",
        "txt",
        "urp",
        "campaign",
        "contract",
    )
    missing = [k for k in required if k not in data]
    if missing:
        raise ValueError(f"ids-json missing keys: {', '.join(missing)}")
    resident = data.get("resident_session_id")
    if resident is not None and resident != data["session_id"]:
        raise ValueError("ids-json resident_session_id differs from session_id")
    return data


def _host_argv(
    *,
    control_python: Path,
    run_dir: Path,
    ids: Mapping[str, Any],
    controller_host: str,
    kunwei_host: str,
    kunwei_port: int,
    release_manifest_sha256: str,
    eoat_sha256: str,
    timing_canary: bool = False,
) -> list[str]:
    programs = ROOT / "programs/step5/step5d"
    argv = [
        str(control_python),
        "-u",
        "-m",
        "run_step5d_autotune_v4_r008_b3_two_stage",
        "live",
        "--controller-host",
        controller_host,
        "--kunwei-host",
        kunwei_host,
        "--kunwei-port",
        str(kunwei_port),
        "--controller-receipt",
        str(run_dir / "controller_receipt.json"),
        "--script1-receipt",
        str(run_dir / "script1_receipt.json"),
        "--runtime-evidence",
        str(run_dir / "runtime_evidence.json"),
        "--software-baseline-receipt",
        str(run_dir / "software_baseline.json"),
        "--ledger",
        str(run_dir / "r006-observations.jsonl"),
        "--queue-root",
        str(run_dir / "r006-queue"),
        "--authority-root",
        str(run_dir / "authority"),
        "--thresholds-receipt",
        str(run_dir / "thresholds_receipt.json"),
        "--launch-profile",
        LAUNCH_PROFILE,
        "--script",
        str(programs / f"{PROGRAM}.script"),
        "--txt",
        str(programs / f"{PROGRAM}.txt"),
        "--urp",
        str(programs / f"{PROGRAM}.urp"),
        "--route-id",
        str(ids["route_id"]),
        "--attempt-id",
        str(ids["attempt_id"]),
        # Mandatory name: --resident-session-id (not --session-id).
        "--resident-session-id",
        str(ids["session_id"]),
        "--session-epoch",
        str(int(ids["session_epoch"])),
        "--expected-script-sha256",
        str(ids["script"]),
        "--expected-txt-sha256",
        str(ids["txt"]),
        "--expected-urp-sha256",
        str(ids["urp"]),
        "--release-manifest-sha256",
        release_manifest_sha256,
        "--eoat-sha256",
        eoat_sha256,
        "--campaign-fingerprint",
        str(ids["campaign"]),
        "--contract-sha256",
        str(ids["contract"]),
    ]
    if timing_canary:
        argv.append("--timing-canary")
    return argv


def _preflight(run_dir: Path) -> None:
    if "1113_stage_d" in str(run_dir):
        raise RuntimeError("refusing mainline 1113_stage_d run directory")
    if not run_dir.is_dir():
        raise RuntimeError(f"run-dir does not exist: {run_dir}")
    required = [
        "controller_receipt.json",
        "script1_receipt.json",
        "runtime_evidence.json",
        "software_baseline.json",
        "thresholds_receipt.json",
        "launch_context.json",
    ]
    missing = [name for name in required if not (run_dir / name).is_file()]
    if missing:
        raise RuntimeError(
            "run-dir missing prepare/threshold artifacts (refusing to invent them): "
            + ", ".join(missing)
        )


def _write_supervisor_log(run_dir: Path, lines: Sequence[str]) -> None:
    path = run_dir / "supervisor.log"
    with path.open("a", encoding="utf-8") as fh:
        for line in lines:
            fh.write(line.rstrip() + "\n")


def _check_parentage(host_pid: int, supervisor_pid: int, timeout_s: float = 8.0) -> None:
    """Best-effort: host PPid must stay this supervisor (not 1)."""
    deadline = time.time() + timeout_s
    last_ppid: int | None = None
    while time.time() < deadline:
        status = Path(f"/proc/{host_pid}/status")
        if not status.is_file():
            time.sleep(0.2)
            continue
        for line in status.read_text(encoding="utf-8").splitlines():
            if line.startswith("PPid:"):
                last_ppid = int(line.split()[1])
                break
        if last_ppid == supervisor_pid:
            return
        if last_ppid == 1:
            raise RuntimeError(
                f"host pid {host_pid} reparented to init (PPid=1); "
                "authority will orphan — abort (do not use bare nohup)"
            )
        time.sleep(0.2)
    if last_ppid != supervisor_pid:
        raise RuntimeError(
            f"host PPid={last_ppid} did not match supervisor {supervisor_pid} "
            f"within {timeout_s:g}s"
        )


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Example (after prepare + thresholds; keep this process alive):\n"
            "  CONTROL_PY=.../control/bin/python\n"
            "  $CONTROL_PY -u tools/supervise_step5d_autotune_v4_r008_b3_host.py \\\n"
            "    --run-dir runs/step5d_autotune_v4_r008/live_YYYYMMDD_HHMMSS_b3_wave1 \\\n"
            "    --ids-json /tmp/r008_b3_wave1_ids.json \\\n"
            "    --control-python $CONTROL_PY\n"
            "\n"
            "ids-json keys: route_id, attempt_id, session_id, session_epoch,\n"
            "  script, txt, urp, campaign, contract  (same shape as /tmp/r008_b3_ids.json)\n"
            "\n"
            "This helper never uploads or plays. Forbidden: bare nohup on the host module."
        ),
    )
    p.add_argument("--run-dir", type=Path, required=True)
    p.add_argument(
        "--ids-json",
        type=Path,
        required=True,
        help="JSON with route/attempt/session + digests from prepare",
    )
    p.add_argument(
        "--control-python",
        type=Path,
        default=DEFAULT_CONTROL_PY,
        help="CONTROL_PY used for 133033 careful retry (default: that exact path)",
    )
    p.add_argument("--controller-host", default="192.168.1.18")
    p.add_argument("--kunwei-host", default="192.168.50.25")
    p.add_argument("--kunwei-port", type=int, default=5152)
    p.add_argument(
        "--release-manifest-sha256",
        default=DEFAULT_RELEASE_MANIFEST_SHA256,
    )
    p.add_argument("--eoat-sha256", default=DEFAULT_EOAT_SHA256)
    p.add_argument(
        "--skip-parentage-check",
        action="store_true",
        help="Do not poll /proc/<host>/status PPid (not recommended)",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Print host argv and exit 0 without launching",
    )
    p.add_argument(
        "--timing-canary",
        action="store_true",
        help="Forward --timing-canary to host (skip QUAL; formal autotune must omit)",
    )
    p.add_argument(
        "--no-body-observer",
        action="store_true",
        help="Do not attach finance body_observer watch (default: attach)",
    )
    p.add_argument(
        "--body-observer-interval",
        type=float,
        default=1.0,
        help="body_observer poll interval seconds (default 1.0)",
    )
    return p


def _spawn_body_observer(
    *,
    run_dir: Path,
    controller_host: str,
    interval_s: float,
    env: Mapping[str, str],
) -> subprocess.Popen[Any]:
    """Finance journal: diagnose-only; never fail-closed on formal campaigns."""

    log_path = run_dir / "body_observer.supervisor.log"
    argv = [
        sys.executable,
        "-u",
        str(ROOT / "tools" / "step5d_r008_body_observer.py"),
        "watch",
        "--run-dir",
        str(run_dir),
        "--interval",
        str(float(interval_s)),
        "--also-dashboard",
        str(controller_host),
        "--jsonl",
    ]
    log_fh = log_path.open("ab")
    return subprocess.Popen(
        argv,
        cwd=str(ROOT),
        env=dict(env),
        stdout=log_fh,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(list(argv) if argv is not None else None)
    run_dir = args.run_dir.resolve()
    control_python = args.control_python.resolve()

    if not control_python.is_file():
        return _die(f"control python missing: {control_python}")
    try:
        ids = _load_ids(args.ids_json.resolve())
        _preflight(run_dir)
    except (OSError, ValueError, RuntimeError) as exc:
        return _die(str(exc))

    argv_host = _host_argv(
        control_python=control_python,
        run_dir=run_dir,
        ids=ids,
        controller_host=args.controller_host,
        kunwei_host=args.kunwei_host,
        kunwei_port=args.kunwei_port,
        release_manifest_sha256=args.release_manifest_sha256,
        eoat_sha256=args.eoat_sha256,
        timing_canary=bool(args.timing_canary),
    )
    if args.dry_run:
        print(json.dumps({"status": "dry_run", "argv": argv_host}, indent=2))
        return 0

    env = os.environ.copy()
    # Same PYTHONPATH shape as /tmp/r008_b3_supervisor.sh (133033 careful retry).
    extra = [
        str(ROOT / "tools"),
        "/home/andy/.codex-worktrees/step5d-v4-r004-20260801/src/ur10e_experiment_runtime",
        "/opt/ros/humble/lib/python3.10/site-packages",
        "/opt/ros/humble/local/lib/python3.10/dist-packages",
    ]
    prev = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = os.pathsep.join(extra + ([prev] if prev else []))
    env["PYTHONUNBUFFERED"] = "1"

    supervisor_pid = os.getpid()
    host_log = run_dir / "host.log"
    _write_supervisor_log(
        run_dir,
        [
            f"supervisor_pid={supervisor_pid} host_parent_will_be={supervisor_pid}",
            f"ids_json={args.ids_json.resolve()}",
            "note=direct_child_wait_no_nohup_orphan",
        ],
    )

    with host_log.open("ab") as log_fh:
        # Start host as direct child; this process remains parent until wait.
        proc = subprocess.Popen(
            argv_host,
            cwd=str(ROOT),
            env=env,
            stdout=log_fh,
            stderr=subprocess.STDOUT,
            start_new_session=False,
        )
    host_pid = proc.pid
    _write_supervisor_log(run_dir, [f"host_pid={host_pid}"])

    observer_proc: subprocess.Popen[Any] | None = None
    if not args.no_body_observer:
        try:
            observer_proc = _spawn_body_observer(
                run_dir=run_dir,
                controller_host=str(args.controller_host),
                interval_s=float(args.body_observer_interval),
                env=env,
            )
            _write_supervisor_log(
                run_dir,
                [
                    f"body_observer_pid={observer_proc.pid}",
                    "body_observer_role=finance_diagnose_only",
                ],
            )
        except OSError as exc:
            _write_supervisor_log(run_dir, [f"body_observer_spawn_failed={exc}"])

    print(
        json.dumps(
            {
                "status": "supervising",
                "supervisor_pid": supervisor_pid,
                "host_pid": host_pid,
                "body_observer_pid": (
                    None if observer_proc is None else int(observer_proc.pid)
                ),
                "run_dir": str(run_dir),
                "host_log": str(host_log),
            },
            sort_keys=True,
        ),
        flush=True,
    )

    if not args.skip_parentage_check:
        try:
            _check_parentage(host_pid, supervisor_pid)
        except RuntimeError as exc:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
            if observer_proc is not None and observer_proc.poll() is None:
                observer_proc.terminate()
            _write_supervisor_log(run_dir, [f"parentage_abort={exc}"])
            return _die(str(exc), code=3)

    exit_code = int(proc.wait())
    if observer_proc is not None and observer_proc.poll() is None:
        observer_proc.terminate()
        try:
            observer_proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            observer_proc.kill()
    _write_supervisor_log(run_dir, [f"host_exit={exit_code}"])
    print(
        json.dumps(
            {
                "status": "host_exited",
                "host_exit": exit_code,
                "supervisor_pid": supervisor_pid,
                "host_pid": host_pid,
            },
            sort_keys=True,
        ),
        flush=True,
    )
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
