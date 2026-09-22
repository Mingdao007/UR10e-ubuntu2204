"""Canonical TASE figure-eight launcher.

Both one-off runs and autotuner candidates delegate to the existing Step5d
supervisor/runner.  This module only binds the trajectory, protocol and mature
provider identity; it does not open a controller or send motion itself.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence


ROOT = Path(__file__).resolve().parents[1]
CANONICAL_LAUNCHER = ROOT / "scripts" / "step5d-autotune-v3.sh"
PROVIDER_ID = "TASE_RNN_MATURE"
TRAJECTORY_ID = "figure8"


@dataclass(frozen=True)
class Protocol:
    protocol_id: str
    duration_s: float
    score_start_s: float
    score_end_s: float
    required_bins: int
    complete_period_s: float | None


PROTOCOLS = {
    "r013-60s": Protocol("r013-60s", 60.0, 5.0, 60.0, 550, None),
    "full-cycle": Protocol("full-cycle", 62.831853, 0.0, 62.831853, 550, 62.831853),
}


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="figure8.sh",
        description="Run one TASE figure-eight experiment or autotuner candidate through the canonical Step5d supervisor.",
    )
    p.add_argument("--protocol", choices=tuple(PROTOCOLS), default="r013-60s")
    p.add_argument("--output-root", type=Path)
    p.add_argument("--campaign-root", type=Path)
    p.add_argument("--print-chain", action="store_true")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--prepare-only", action="store_true")
    return p


def runtime_environment(protocol: Protocol, base: dict[str, str] | None = None) -> dict[str, str]:
    env = dict(os.environ if base is None else base)
    env.update(
        {
            "TASE_CONTROL_PROVIDER": PROVIDER_ID,
            "TASE_TRAJECTORY": TRAJECTORY_ID,
            "TASE_PATH_SHAPE": "eight",
            "TASE_PROTOCOL_ID": protocol.protocol_id,
            "TASE_PROTOCOL_DURATION_S": str(protocol.duration_s),
            "TASE_SCORE_WINDOW_START_S": str(protocol.score_start_s),
            "TASE_SCORE_WINDOW_END_S": str(protocol.score_end_s),
            "TASE_REQUIRED_COMPLETE_BINS": str(protocol.required_bins),
        }
    )
    if protocol.complete_period_s is not None:
        env["TASE_COMPLETE_PERIOD_S"] = str(protocol.complete_period_s)
    else:
        env.pop("TASE_COMPLETE_PERIOD_S", None)
    return env


def canonical_command(protocol: Protocol, forwarded: Sequence[str]) -> list[str]:
    command = [str(CANONICAL_LAUNCHER), "bridge-live"]
    if protocol.protocol_id == "full-cycle":
        command.extend(["--ready-timeout-s", "30"])
    command.extend(forwarded)
    return command


def chain_receipt(protocol: Protocol) -> dict[str, object]:
    return {
        "schema": "tase/figure8-entrypoint-v1",
        "entrypoint": "figure8.sh",
        "supervisor": "run_step5d_autotune_v3_live.py",
        "provider": PROVIDER_ID,
        "writer": "single supervisor-owned parameter campaign writer",
        "trajectory": TRAJECTORY_ID,
        "protocol": {
            "id": protocol.protocol_id,
            "duration_s": protocol.duration_s,
            "score_window_s": [protocol.score_start_s, protocol.score_end_s],
            "required_complete_bins": protocol.required_bins,
            "complete_period_s": protocol.complete_period_s,
        },
        "lifecycle": [
            "Home",
            "qualification",
            "entry",
            "PATH",
            "stop",
            "unload_or_recover",
            "verified Home",
            "result",
        ],
    }


def main(argv: Iterable[str] | None = None) -> int:
    raw = list(sys.argv[1:] if argv is None else argv)
    args, forwarded = parser().parse_known_args(raw)
    protocol = PROTOCOLS[args.protocol]
    if args.print_chain:
        print(json.dumps(chain_receipt(protocol), indent=2, sort_keys=True))
        return 0
    if args.output_root is not None:
        forwarded = ["--output-root", str(args.output_root), *forwarded]
    if args.campaign_root is not None:
        forwarded = ["--campaign-root", str(args.campaign_root), *forwarded]
    if args.prepare_only:
        # The canonical shell intentionally rejects its worker-only
        # --prepare-only flag. Treat this public probe as a no-I/O dry-run.
        args.dry_run = True
    command = canonical_command(protocol, forwarded)
    env = runtime_environment(protocol)
    if args.dry_run:
        if protocol.complete_period_s is not None and not args.prepare_only:
            # Full-cycle scoring remains an explicitly separate offline
            # protocol until the current release carries its scorer contract.
            pass
        print(json.dumps({"command": command, "environment": {key: env[key] for key in env if key.startswith("TASE_")}}, indent=2, sort_keys=True))
        return 0
    if protocol.complete_period_s is not None:
        print("full-cycle is retained as a separate offline protocol; use --dry-run or --prepare-only", file=sys.stderr)
        return 64
    os.execve(command[0], command, env)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
