#!/usr/bin/env python3
"""Single-process Step5d upload, promotion, and fresh-readback coordinator."""
from __future__ import annotations
import argparse
from pathlib import Path
import promote_step5d_current as promote
import upload_ur_tp_package as upload
import verify_current_stage_readback as readback
from ur10e_mutation_lock import acquire_controller_mutation_locks, release_controller_mutation_locks

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("program")
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--local-dir", type=Path, required=True)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--readback-root", type=Path)
    args = parser.parse_args(argv)
    upload_args = [args.program, "--local-dir", str(args.local_dir),
                   "--allow-local-candidate-promote"]
    if args.dry_run:
        upload_args += ["--dry-run", "--readback-root", str(args.readback_root)]
        return upload._main(upload_args)
    handles = acquire_controller_mutation_locks()
    try:
        rc = upload._main(upload_args)
        if rc:
            return rc
        rc = promote._main(["--root", str(args.root), "--program", args.program,
                            "--local-dir", str(args.local_dir), "--json"])
        if rc:
            return rc
        return readback.main(["--root", str(args.root), "--program", args.program, "--json"])
    finally:
        release_controller_mutation_locks(handles)

if __name__ == "__main__":
    raise SystemExit(main())
