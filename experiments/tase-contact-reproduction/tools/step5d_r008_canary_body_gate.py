#!/usr/bin/env python3
"""Canary body gate — poll run-dir and abort on sustained contacted_stalled.

Default is **formal/diagnose-only**: evaluates stall, never kills supervisor/host.
Formal ``supervise_step5d_autotune_v4_r008_b3_host.py`` is unchanged.

Canary sidecar (replaces bare ``step5d_r008_body_observer watch`` in /tmp scripts):

  PYTHONPATH=tools python3 -u tools/step5d_r008_canary_body_gate.py watch \\
    --run-dir runs/step5d_autotune_v4_r008/live_YYYYMMDD_... \\
    --mode canary --interval 1.0 --also-dashboard 192.168.1.18 --jsonl

Formal autotune (diagnose only — never auto-revoke):

  PYTHONPATH=tools python3 -u tools/step5d_r008_canary_body_gate.py watch \\
    --run-dir runs/step5d_autotune_v4_r008/live_YYYYMMDD_... \\
    --mode formal --interval 1.0 --jsonl
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import sys
from pathlib import Path


def _find_supervisor_pid(run_dir: Path) -> int | None:
    log = run_dir / "supervisor.log"
    if log.is_file():
        for line in log.read_text(encoding="utf-8", errors="replace").splitlines():
            if line.startswith("supervisor_pid="):
                try:
                    return int(line.split("=", 1)[1].strip().split()[0])
                except ValueError:
                    pass
    needle = str(run_dir.resolve())
    proc = Path("/proc")
    if not proc.is_dir():
        return None
    for entry in proc.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            cmdline = (entry / "cmdline").read_bytes().replace(b"\0", b" ").decode(
                "utf-8", "ignore"
            )
        except OSError:
            continue
        if needle not in cmdline:
            continue
        if "supervise_step5d_autotune_v4_r008_b3_host" in cmdline:
            return int(entry.name)
    return None


def _revoke_supervisor(run_dir: Path) -> dict:
    pid = _find_supervisor_pid(run_dir)
    out: dict = {"supervisor_pid": pid, "signal": None, "error": None}
    if pid is None:
        out["error"] = "supervisor_pid_not_found"
        return out
    try:
        os.kill(pid, signal.SIGTERM)
        out["signal"] = "SIGTERM"
    except OSError as exc:
        out["error"] = str(exc)
    return out


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="step5d_r008_canary_body_gate",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    watch = sub.add_parser("watch", help="Poll run-dir until interrupted or canary abort")
    watch.add_argument("--run-dir", type=Path, required=True)
    watch.add_argument("--interval", type=float, default=1.0)
    watch.add_argument("--jsonl", action="store_true", help="Print observer events to stdout")
    watch.add_argument(
        "--also-dashboard",
        metavar="HOST",
        default=None,
        help="Optional dashboard host for PLAYING vs host-dead (no RTDE)",
    )
    watch.add_argument("--count", type=int, default=0, help="Stop after N ticks (0=forever)")
    watch.add_argument(
        "--mode",
        choices=("canary", "formal"),
        default="formal",
        help="formal=diagnose-only (default); canary=SIGTERM supervisor on sustained stall",
    )
    watch.add_argument(
        "--stall-s",
        "--sustained-s",
        dest="stall_s",
        type=float,
        default=None,
        help="Sustained stall threshold seconds (default 8)",
    )

    tick = sub.add_parser("tick", help="Single poll; JSON decision")
    tick.add_argument("--run-dir", type=Path, required=True)
    tick.add_argument("--mode", choices=("canary", "formal"), default="formal")
    tick.add_argument("--stall-s", type=float, default=None)

    return p


def main(argv: list[str] | None = None) -> int:
    tools_root = Path(__file__).resolve().parent
    if str(tools_root) not in sys.path:
        sys.path.insert(0, str(tools_root))

    from step5d_autotune_v4_r008.body_observer import (
        DEFAULT_CANARY_STALL_ABORT_S,
        watch_canary_stall_abort,
        write_canary_stall_abort_artifact,
    )
    from step5d_autotune_v4_r008.canary_body_gate import (
        StallGateState,
        canary_stall_abort_policy,
    )

    args = _build_parser().parse_args(argv)
    stall_s = (
        float(args.stall_s)
        if getattr(args, "stall_s", None) is not None
        else DEFAULT_CANARY_STALL_ABORT_S
    )

    if args.cmd == "tick":
        decision = canary_stall_abort_policy(
            args.run_dir,
            StallGateState(),
            mode=args.mode,
            stall_abort_s=stall_s,
        )
        print(json.dumps(decision.as_dict(), indent=2, sort_keys=True, allow_nan=False))
        return 1 if decision.abort else 0

    if args.cmd == "watch":
        run_dir = args.run_dir.resolve()
        canary_mode = args.mode == "canary"
        state = StallGateState()
        try:
            for events, abort_doc in watch_canary_stall_abort(
                run_dir,
                canary_mode=canary_mode,
                sustained_s=stall_s,
                interval_s=args.interval,
                robot_host=args.also_dashboard,
                write_jsonl=True,
                count=int(args.count),
            ):
                if args.jsonl:
                    for ev in events:
                        print(json.dumps(ev, sort_keys=True, allow_nan=False), flush=True)
                if abort_doc is None:
                    continue
                path = write_canary_stall_abort_artifact(run_dir, abort_doc)
                decision = canary_stall_abort_policy(
                    run_dir, state, mode=args.mode, stall_abort_s=stall_s
                )
                revoke = _revoke_supervisor(run_dir) if canary_mode else None
                out = {
                    **decision.as_dict(),
                    "artifact": str(path),
                    "revoke": revoke,
                }
                print(json.dumps(out, sort_keys=True, allow_nan=False), flush=True)
                if canary_mode:
                    return 1
        except KeyboardInterrupt:
            return 0
        return 0

    return 2


if __name__ == "__main__":
    raise SystemExit(main())
