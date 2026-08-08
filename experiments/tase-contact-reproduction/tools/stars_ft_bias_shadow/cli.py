#!/usr/bin/env python3
"""CLI for the offline STARS FT bias shadow.

Every command is read-only with respect to the historical input and live
systems.  Replay writes only a new immutable shadow-output directory.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys


def _experiment_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _error_document(exc: Exception) -> dict[str, object]:
    document: dict[str, object] = {"ok": False, "error": str(exc)}
    code = getattr(exc, "code", None)
    details = getattr(exc, "details", None)
    if isinstance(code, str):
        document["error_code"] = code
    if isinstance(details, dict) and details:
        document["details"] = details
    return document


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="STARS FT bias shadow (offline replay only)")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_replay = sub.add_parser("replay", help="Replay one completed historical live_* run directory")
    p_replay.add_argument("--run-dir", type=Path, required=True)
    p_replay.add_argument("--config", type=Path, default=None)
    p_replay.add_argument("--out-dir", type=Path, default=None)
    p_replay.add_argument("--experiment-root", type=Path, default=None)

    p_inv = sub.add_parser("inventory", help="Run strict offline preflight without writing outputs")
    p_inv.add_argument("--run-dir", type=Path, required=True)
    p_inv.add_argument("--config", type=Path, default=None)
    p_inv.add_argument("--experiment-root", type=Path, default=None)

    p_receipt = sub.add_parser("validate-receipt", help="Cold-read a sealed shadow output")
    p_receipt.add_argument("--out-dir", type=Path, required=True)

    args = parser.parse_args(argv)
    tools_dir = Path(__file__).resolve().parents[1]
    if str(tools_dir) not in sys.path:
        sys.path.insert(0, str(tools_dir))

    from stars_ft_bias_shadow.adapters.r008_run_dir import (  # noqa: WPS433
        R008RunDirError,
        preflight_run_dir,
    )
    from stars_ft_bias_shadow.replay import (  # noqa: WPS433
        load_config,
        replay_run_dir,
        validate_completion_receipt,
    )

    if args.cmd == "validate-receipt":
        try:
            summary = validate_completion_receipt(args.out_dir)
        except (R008RunDirError, OSError, ValueError) as exc:
            print(json.dumps(_error_document(exc), indent=2, ensure_ascii=True), file=sys.stderr)
            return 2
        print(json.dumps({"ok": True, "summary": summary}, indent=2, ensure_ascii=True))
        return 0

    experiment_root = (args.experiment_root or _experiment_root()).resolve()
    try:
        cfg = load_config(args.config, experiment_root)
        limits = cfg["limits"]
        if args.cmd == "inventory":
            snapshot = preflight_run_dir(
                args.run_dir,
                experiment_root=experiment_root,
                min_input_age_s=float(limits["min_input_age_s"]),
                max_input_file_bytes=int(limits["max_input_file_bytes"]),
                max_total_input_bytes=int(limits["max_total_input_bytes"]),
                max_contiguous_dt_s=float(limits["max_contiguous_dt_s"]),
                allow_missing_state25=bool(cfg["source_coverage"]["allow_missing_state25"]),
            )
            print(
                json.dumps(
                    {
                        "ok": True,
                        "run_id": snapshot.run_id,
                        "terminal_status": snapshot.terminal_status,
                        "formal_live_complete": snapshot.formal_live_complete,
                        "host_non_json_line_count": snapshot.host_non_json_line_count,
                        "host_non_object_line_count": snapshot.host_non_object_line_count,
                        "host_blank_line_count": snapshot.host_blank_line_count,
                        "terminal_evidence": snapshot.terminal_evidence,
                        "source_coverage": snapshot.source_coverage,
                        "input_manifest_sha256": snapshot.input_manifest_sha256,
                        "science_not_promoted": True,
                    },
                    indent=2,
                    ensure_ascii=True,
                )
            )
            return 0

        if args.cmd == "replay":
            result = replay_run_dir(
                args.run_dir,
                experiment_root=experiment_root,
                config_path=args.config,
                out_dir=args.out_dir,
            )
            print(
                json.dumps(
                    {
                        "ok": True,
                        "run_dir": str(result.run_dir),
                        "out_dir": str(result.out_dir),
                        "bias_est": str(result.bias_est_path),
                        "summary": str(result.summary_path),
                        "completion_receipt": str(result.completion_receipt_path),
                        "rows_written": result.rows_written,
                        "update_coverage": result.summary.get("update_coverage"),
                        "science_not_promoted": True,
                    },
                    indent=2,
                    ensure_ascii=True,
                )
            )
            return 0
    except (R008RunDirError, OSError, ValueError) as exc:
        print(json.dumps(_error_document(exc), indent=2, ensure_ascii=True), file=sys.stderr)
        return 2

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
