#!/usr/bin/env python3
"""Stream the frozen G10 packet evidence and prove v2 control-source identity."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import subprocess
from decimal import Decimal
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
GOLDEN = ROOT / "config/step5/golden_replay_g10_v1.json"
FREEZE = ROOT / "config/step5/v1_freeze_manifest.json"
CONTROL_PATHS = {
    "experiments/tase-contact-reproduction/tools/step5d_runtime_interface.py",
    "experiments/tase-contact-reproduction/tools/step5d_paper_outer_loop.py",
    "experiments/tase-contact-reproduction/tools/step5d_control_contract.py",
    "experiments/tase-contact-reproduction/tools/contact_semantics.py",
    "experiments/tase-contact-reproduction/config/step5_safe_frame.json",
    "experiments/tase-contact-reproduction/config/step5d_autotune_i_scale_sanity_v1.json",
}
BRIDGE_PATH = "experiments/tase-contact-reproduction/tools/kunwei_rtde_bridge.py"
BRIDGE_ADAPTER_PATCH_SHA256 = "50bb4c25c3a65b33c9903e9e3984d93483219e9e97e5a86c6a32bbd26e8c7c5d"


def sha(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def integer(row: dict[str, str], name: str) -> int:
    try:
        return int(float(row[name]))
    except (KeyError, TypeError, ValueError) as exc:
        raise RuntimeError(f"golden packet lacks integer {name}") from exc


def decimal_text(value: object) -> str:
    rendered = format(Decimal(str(value)), "f")
    if "." in rendered:
        rendered = rendered.rstrip("0").rstrip(".")
    return "0" if rendered in {"", "-0"} else rendered


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    golden = json.loads(GOLDEN.read_text(encoding="utf-8"))
    freeze = json.loads(FREEZE.read_text(encoding="utf-8"))
    frozen_files = dict(freeze["files"])
    repo_top = ROOT.parents[1]
    for repo_relative in sorted(CONTROL_PATHS):
        expected = frozen_files.get(repo_relative)
        current = repo_top / repo_relative
        if expected is None or sha(current) != expected:
            raise SystemExit(f"control source differs from frozen v1: {repo_relative}")
    bridge_base_sha256 = frozen_files.get(BRIDGE_PATH)
    bridge_current = repo_top / BRIDGE_PATH
    if bridge_base_sha256 is None:
        raise SystemExit("frozen v1 bridge source binding is missing")
    completed = subprocess.run(
        [
            "git",
            "-C",
            str(repo_top),
            "diff",
            "--no-ext-diff",
            "--unified=3",
            str(freeze["tag"]),
            "--",
            BRIDGE_PATH,
        ],
        capture_output=True,
        check=False,
    )
    if completed.returncode != 0:
        raise SystemExit("cannot derive the reviewed v2 bridge adapter patch")
    bridge_patch_sha256 = hashlib.sha256(completed.stdout).hexdigest()
    if bridge_patch_sha256 != BRIDGE_ADAPTER_PATCH_SHA256:
        raise SystemExit("bridge differs from the reviewed frozen-v1 adapter patch")
    bundle_path = (args.source_root.resolve() / golden["trial_dir"] / "immutable_trial_bundle.json")
    if sha(bundle_path) != golden["bundle_sha256"]:
        raise SystemExit("golden immutable bundle checksum differs")
    bundle = json.loads(bundle_path.read_text(encoding="utf-8"))
    csv_row = bundle["artifact_provenance"]["csv"]
    csv_path = Path(csv_row["path"])
    if (
        sha(csv_path) != golden["csv_sha256"]
        or csv_path.stat().st_size != golden["csv_size_bytes"]
    ):
        raise SystemExit("golden packet capture checksum/size differs")
    trial = bundle["trial"]
    candidate = trial["candidate"]
    expected_candidate = golden["candidate"]
    actual_candidate = {
        "p": decimal_text(candidate["force_p_gain"]),
        "i": decimal_text(candidate["force_i_gain"]),
        "d": decimal_text(candidate["force_damping"]),
        "profile_id": trial["execution_profile"]["profile_id"],
    }
    if actual_candidate != expected_candidate:
        raise SystemExit("golden candidate/profile identity differs")
    acceptance = golden["acceptance"]
    if (
        bundle["evaluation"]["safe_closure"] is not acceptance["safe_closure"]
        or bundle["evaluation"]["complete_bins"] != acceptance["complete_bins"]
        or bundle["capture"]["terminal_reason"] != acceptance["terminal_reason"]
        or bundle["capture"]["returned_safe"] is not acceptance["returned_safe"]
    ):
        raise SystemExit("golden safe-closure acceptance differs")
    host_names = tuple(golden["host_packet"])
    expected_host = tuple(golden["host_packet"][name] for name in host_names)
    transitions: list[tuple[int, ...]] = []
    previous: tuple[int, ...] | None = None
    rows = 0
    with csv_path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            rows += 1
            host = tuple(integer(row, name) for name in host_names)
            if host != expected_host:
                raise SystemExit(f"golden host packet drift at row {rows}")
            packet = tuple(
                integer(row, f"ur_output_int_register_{register}")
                for register in range(24, 31)
            )
            if packet != previous:
                transitions.append(packet)
                previous = packet
    if transitions != [tuple(row) for row in golden["tp_transitions"]]:
        raise SystemExit(f"golden TP transition order differs: {transitions}")
    result: dict[str, Any] = {
        "schema": "step5d.autotune.golden-replay-result/v2",
        "passed": True,
        "golden_spec_sha256": sha(GOLDEN),
        "group_id": golden["group_id"],
        "trial_uid": golden["trial_uid"],
        "packet_rows": rows,
        "tp_transitions": transitions,
        "bundle_sha256": golden["bundle_sha256"],
        "csv_sha256": golden["csv_sha256"],
        "control_sources_byte_identical": sorted(CONTROL_PATHS),
        "bridge_frozen_v1_sha256": bridge_base_sha256,
        "bridge_v2_sha256": sha(bridge_current),
        "bridge_adapter_patch_sha256": bridge_patch_sha256,
    }
    encoded = json.dumps(result, indent=2, sort_keys=True) + "\n"
    if args.output is not None:
        args.output.write_text(encoded, encoding="utf-8")
    print(encoded, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
