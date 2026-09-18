#!/usr/bin/env python3
"""Build compact, provenance-bound fixtures for the Step5b shadow replay.

The historical bridge CSVs live under an ignored ``runs/`` archive and are
therefore not present in every checkout.  This tool projects only the columns
consumed by ``contact_cycloid_shadow`` into a portable test fixture.  It does
not rewrite or replace the raw evidence; the manifest records each source
path, SHA256, row count, and projected field set.
"""

from __future__ import annotations

import argparse
import csv
import gzip
import hashlib
import io
import json
from pathlib import Path
from typing import Any


RUN_NAMES = (
    "bridge_step5b_contact_cycloid_baseline_v1_20260612_082352",
    "bridge_step5b_contact_cycloid_baseline_v1_20260614_222058",
    "bridge_step5b_contact_cycloid_baseline_v1_20260614_222309",
)

# These are exactly the fields read by contact_cycloid_shadow._trace_row.
# Keeping the projection explicit prevents an accidental raw-data copy.
PROJECTED_FIELDS = (
    "t_monotonic_s",
    "normal_force_n",
    "force_norm_n",
    "step4e_cmd_valid",
    "_step4e_path_error_x_m",
    "_step4e_path_error_y_m",
    "_step4e_normal_load_n",
    "ur_actual_TCP_pose_0",
    "ur_actual_TCP_pose_1",
    "ur_actual_TCP_pose_2",
    "_step4e_actual_speed_norm_m_s",
    "ur_actual_TCP_speed_0",
    "ur_actual_TCP_speed_1",
    "ur_actual_TCP_speed_2",
    "_step4e_desired_x_m",
    "_step4e_desired_y_m",
    "_step4e_desired_vx_m_s",
    "_step4e_desired_vy_m_s",
    "step4e_cmd_vx_m_s",
    "step4e_cmd_vy_m_s",
    "step4e_cmd_vz_m_s",
    "step4e_cmd_wx_rad_s",
    "step4e_cmd_wy_rad_s",
    "step4e_cmd_wz_rad_s",
    "step4e_progress_m",
    "_step4e_path_time_s",
)

SCHEMA = "ur10e.step5b-shadow-derived-fixture-v1"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def build_fixtures(*, source_root: Path, output_root: Path) -> dict[str, Any]:
    source_root = source_root.resolve()
    output_root = output_root.resolve()
    output_root.mkdir(parents=True, exist_ok=True)
    entries: list[dict[str, Any]] = []
    for run_name in RUN_NAMES:
        source = source_root / run_name / "bridge_rtde_500hz.csv"
        if not source.is_file():
            raise FileNotFoundError(f"historical Step5b CSV is missing: {source}")
        destination = output_root / run_name / "bridge_rtde_500hz.csv.gz"
        destination.parent.mkdir(parents=True, exist_ok=True)
        row_count = 0
        with source.open(newline="", encoding="utf-8") as input_stream:
            reader = csv.DictReader(input_stream)
            fieldnames = tuple(reader.fieldnames or ())
            missing = [field for field in PROJECTED_FIELDS if field not in fieldnames]
            if missing:
                raise ValueError(f"{source} lacks projected fields: {missing}")
            # A fixed gzip mtime makes the fixture hash reproducible when the
            # same raw source is projected again on another machine.
            with destination.open("wb") as compressed_stream:
                with gzip.GzipFile(fileobj=compressed_stream, mode="wb", mtime=0) as gzip_stream:
                    with io.TextIOWrapper(gzip_stream, encoding="utf-8", newline="") as output_stream:
                        writer = csv.DictWriter(output_stream, fieldnames=PROJECTED_FIELDS, extrasaction="ignore")
                        writer.writeheader()
                        for row in reader:
                            writer.writerow({field: row.get(field, "") for field in PROJECTED_FIELDS})
                            row_count += 1
        entries.append({
            "run_name": run_name,
            "source_path": str(source.relative_to(source_root)),
            "source_sha256": _sha256(source),
            "source_rows": row_count,
            "fixture_path": str(destination.relative_to(output_root)),
            "fixture_sha256": _sha256(destination),
            "fixture_rows": row_count,
        })
    manifest = {
        "schema": SCHEMA,
        "purpose": "portable Step5b contact-cycloid shadow replay plumbing test",
        "source_policy": "derived projection; raw historical evidence remains external",
        "projected_fields": list(PROJECTED_FIELDS),
        "runs": entries,
    }
    manifest_path = output_root / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    manifest["manifest_path"] = str(manifest_path)
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument(
        "--output-root",
        type=Path,
        default=Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "step5b_contact_cycloid",
    )
    args = parser.parse_args()
    print(json.dumps(build_fixtures(source_root=args.source_root, output_root=args.output_root), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
