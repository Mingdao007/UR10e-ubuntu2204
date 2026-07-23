"""Pure offline runtime planning utilities for the minimal remote-control path."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping

import yaml

from .core import load_r012_config


CanonicalArtifact = dict[str, str]
CANONICAL_JOINTS = (
    "shoulder_pan_joint",
    "shoulder_lift_joint",
    "elbow_joint",
    "wrist_1_joint",
    "wrist_2_joint",
    "wrist_3_joint",
)
ARTIFACT_FILES = (
    "params_snapshot.yaml",
    "trace.csv",
    "summary.json",
    "kunwei_raw_frames.bin",
    "kunwei_monitor.json",
)


def read_boot_id(*, path: str = "/proc/sys/kernel/random/boot_id") -> str:
    text = Path(path).read_text(encoding="utf-8").strip()
    if not text:
        raise ValueError("boot id is empty")
    return text


def latest_canary_summary(output_root: Path) -> Path | None:
    candidates = sorted(
        (p for p in (output_root / "canary").glob("*") if (p / "summary.json").is_file()),
        key=lambda p: (p / "summary.json").stat().st_mtime,
        reverse=True,
    )
    if not candidates:
        return None
    return candidates[0] / "summary.json"


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def resolve_current_root(experiment_root: Path | str | None = None) -> Path:
    if experiment_root is None:
        return Path(__file__).resolve().parents[2]
    return Path(experiment_root).resolve()


def resolve_params_path(experiment_root: Path | str, params_file: str | None = None) -> Path:
    root = resolve_current_root(experiment_root)
    if params_file is None:
        current_path = root / "config" / "step5d_remote" / "current.json"
        current = json.loads(current_path.read_text(encoding="utf-8"))
        params_file = str(current["params_file"])
    candidate = Path(params_file)
    if not candidate.is_absolute():
        candidate = root / candidate
    return candidate.resolve()


def load_runtime_config(
    experiment_root: Path | str, params_file: str | None = None
) -> tuple[dict[str, Any], Path, str]:
    params_path = resolve_params_path(experiment_root, params_file=params_file)
    cfg = load_r012_config(resolve_current_root(experiment_root), str(params_path))
    params_yaml = yaml.safe_dump(
        cfg,
        sort_keys=True,
        default_flow_style=False,
        allow_unicode=False,
    )
    params_sha256 = sha256_text(params_yaml)
    return cfg, params_path, params_sha256


def _artifact_paths(output_root: Path) -> CanonicalArtifact:
    return {name: str((output_root / name).resolve()) for name in ARTIFACT_FILES}


def write_runtime_artifacts(
    output_root: Path,
    cfg: Mapping[str, Any],
    params_path: Path,
    params_yaml: str,
    params_sha256: str,
    *,
    command: str,
    canary_ok: bool = False,
    canary_payload: Mapping[str, Any] | None = None,
) -> CanonicalArtifact:
    output_root.mkdir(parents=True, exist_ok=True)
    paths = _artifact_paths(output_root)
    (output_root / "params_snapshot.yaml").write_text(params_yaml, encoding="utf-8")
    (output_root / "trace.csv").write_text(
        "t_s,stage,phase,q0,q1,q2,q3,q4,q5,qd0,qd1,qd2,qd3,qd4,qd5,qcmd0,qcmd1,qcmd2,qcmd3,qcmd4,qcmd5,tcp_x,tcp_y,tcp_z,tcp_r,tcp_p,tcp_yaw,twist_x,twist_y,twist_z,twist_rx,twist_ry,twist_rz,wrench_fx,wrench_fy,wrench_fz,wrench_mx,wrench_my,wrench_mz,watchdog_status\n",
        encoding="utf-8",
    )
    (output_root / "kunwei_raw_frames.bin").write_bytes(b"")
    (output_root / "summary.json").write_text(
        json.dumps(
            {
                "command": command,
                "route": cfg["route"],
                "schema_version": 1,
                "params_file": str(params_path),
                "params_sha256": params_sha256,
                "canary_ok": canary_ok,
                "canary": dict(canary_payload or {}),
                "watchdog_controller_yaml": materialize_watchdog_param_yaml(cfg),
            },
            sort_keys=True,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    (output_root / "kunwei_monitor.json").write_text(
        json.dumps(
            {
                "status": "not_started",
                "live_motion": False,
                "command": command,
            },
            sort_keys=True,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    return paths


def materialize_watchdog_param_yaml(cfg: Mapping[str, Any]) -> str:
    payload = {
        "controller_manager": {
            "ros__parameters": {
                "update_rate": int(cfg["command_rate_hz"]),
                "step5d_watchdog_controller": {
                    "type": "ur10e_step5d_remote_watchdog/WatchdogController",
                },
            }
        },
        "step5d_watchdog_controller": {
            "ros__parameters": {
                "joints": list(CANONICAL_JOINTS),
                "max_abs_velocity_rad_s": float(cfg["watchdog"]["max_abs_velocity_rad_s"]),
                "max_acceleration_rad_s2": float(cfg["watchdog"]["max_acceleration_rad_s2"]),
                "stale_timeout_s": float(cfg["watchdog"]["command_stale_s"]),
            }
        },
    }
    return yaml.safe_dump(payload, sort_keys=True, default_flow_style=False)


def validate_canary_evidence(
    cfg: Mapping[str, Any],
    evidence_path: Path,
    *,
    params_sha256: str,
    current_boot_id: str | None = None,
    now_epoch_s: float | None = None,
) -> None:
    evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
    if not isinstance(evidence, Mapping):
        raise ValueError("canary evidence must be a json mapping")

    required_keys = {"ok", "params_sha256", "boot_id", "created_at_epoch_s", "stages"}
    if set(evidence.keys()) != required_keys:
        raise ValueError("canary evidence keys mismatch")
    if evidence.get("ok") is not True:
        raise ValueError("canary evidence not ok")
    if not isinstance(current_boot_id, str) or not current_boot_id:
        raise ValueError("current_boot_id is required")
    if str(evidence.get("params_sha256", "")) != params_sha256:
        raise ValueError("canary evidence params mismatch")
    if str(evidence.get("boot_id", "")) != current_boot_id:
        raise ValueError("canary evidence boot_id mismatch")
    created_at = float(evidence["created_at_epoch_s"])
    if not math_isfinite(created_at):
        raise ValueError("canary evidence missing created_at_epoch_s")
    now = float(time.time() if now_epoch_s is None else now_epoch_s)
    if not math_isfinite(now):
        raise ValueError("invalid now timestamp")
    if created_at > now:
        raise ValueError("canary evidence future timestamp")
    age = now - created_at
    if age > float(cfg["canary"]["evidence_max_age_s"]):
        raise ValueError("canary evidence expired")

    stages = evidence["stages"]
    if not isinstance(stages, Mapping):
        raise ValueError("canary evidence stages must be mapping")
    expected_stage_keys = {
        "zero",
        "free_space",
        "guarded_contact",
        "post_canary_safe_pose",
    }
    if set(stages.keys()) != expected_stage_keys:
        raise ValueError("canary evidence stages missing required entries")
    if not all(str(stages[name]) == "passed" for name in expected_stage_keys):
        raise ValueError("canary evidence requires all stages passed")


def math_isfinite(value: float) -> bool:
    return value == value and value != float("inf") and value != float("-inf")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Minimal offline remote-control runtime entry.")
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--experiment-root", default=None, type=str)
    common.add_argument("--params-file", default=None)
    common.add_argument("--live", action="store_true")

    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.required = True
    subparsers.add_parser("status", parents=[common], add_help=False)
    subparsers.add_parser("dry-run", parents=[common], add_help=False)
    subparsers.add_parser("canary", parents=[common], add_help=False)
    subparsers.add_parser("run", parents=[common], add_help=False)
    return parser


def validate_command_gate(args: argparse.Namespace) -> None:
    if args.command in {"status", "dry-run"} and args.live:
        raise ValueError(f"{args.command} cannot be run with --live")
    if args.command in {"canary", "run"} and not args.live:
        raise ValueError(f"{args.command} requires --live")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    return build_parser().parse_args(argv)


def _next_run_root(experiment_root: Path, command: str) -> Path:
    base = Path(
        os.environ.get(
            "STEP5D_REMOTE_RUN_ROOT",
            str(experiment_root / "runs" / "step5d_remote_control"),
        )
    )
    return base / command / datetime.now().strftime("%Y%m%dT%H%M%S_%f")


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    validate_command_gate(args)

    root = resolve_current_root(args.experiment_root)
    output_root = _next_run_root(root, args.command)
    cfg, params_path, params_sha256 = load_runtime_config(root, params_file=args.params_file)
    params_yaml = yaml.safe_dump(
        cfg,
        sort_keys=True,
        default_flow_style=False,
        allow_unicode=False,
    )
    write_runtime_artifacts(
        output_root,
        cfg,
        params_path,
        params_yaml,
        params_sha256,
        command=args.command,
    )

    if args.command in {"status", "dry-run"}:
        print(f"{args.command}=ok route={cfg['route']}")
        return 0

    from . import live

    return live.run_live(
        args.command,
        experiment_root=root,
        output_root=output_root,
        cfg=cfg,
        params_path=params_path,
        params_yaml=params_yaml,
        params_sha256=params_sha256,
    )


if __name__ == "__main__":
    raise SystemExit(main())
