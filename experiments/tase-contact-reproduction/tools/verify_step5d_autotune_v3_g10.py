#!/usr/bin/env python3
"""Replay frozen G10 through the v3 production launcher/control seam."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import os
import tempfile
from decimal import Decimal
from pathlib import Path
from typing import Any, Mapping, Sequence

from step5d_autotune_contract import ExecutionProfile, ForceCandidate
from step5d_autotune_replay import _run_candidate_bound_exact_replay
from step5d_autotune_v3.launcher import check_effective_config


ROOT = Path(__file__).resolve().parents[1]
GOLDEN = ROOT / "config/step5/golden_replay_g10_v1.json"
GOLDEN_SHA256 = "f56a3eef529494b6c209ca5534abec082519848446724a94426de522852260f8"
FROZEN_COMMIT = "6f9ef0912842ac003545eb1906b38d13c7552218"
RNN_COLUMNS = {
    "step5d_rnn_inner_iterations": "_step5d_rnn_inner_iterations",
    "step5d_rnn_backend": "_step5d_rnn_backend",
    "step5d_epsilon": "_step5d_rnn_epsilon",
    "step5d_sigr_exponent_r": "_step5d_rnn_sigr_exponent_r",
}


class GoldenReplayError(RuntimeError):
    """Frozen evidence or the executable production seam differs."""


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _unique_object(pairs: Sequence[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise GoldenReplayError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def load_json(path: Path, *, role: str) -> Mapping[str, Any]:
    try:
        payload = json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=_unique_object,
            parse_constant=lambda value: (_ for _ in ()).throw(
                GoldenReplayError(f"non-finite JSON constant: {value}")
            ),
        )
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise GoldenReplayError(f"{role} is not valid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise GoldenReplayError(f"{role} must be an object")
    return payload


def decimal_text(value: Any) -> str:
    rendered = format(Decimal(str(value)), "f")
    if "." in rendered:
        rendered = rendered.rstrip("0").rstrip(".")
    return "0" if rendered in {"", "-0"} else rendered


def integer(row: Mapping[str, str], name: str) -> int:
    try:
        value = float(row[name])
        if not math.isfinite(value) or not value.is_integer():
            raise ValueError(name)
        return int(value)
    except (KeyError, TypeError, ValueError) as exc:
        raise GoldenReplayError(f"G10 packet lacks exact integer {name}") from exc


def normalized_rnn_value(name: str, value: str) -> str:
    if name == "step5d_rnn_backend":
        return value
    if name == "step5d_rnn_inner_iterations":
        return str(integer({name: value}, name))
    return decimal_text(value)


def expected_rnn_profile(effective: Mapping[str, Any]) -> dict[str, str]:
    return {
        "step5d_rnn_inner_iterations": str(effective["step5d_rnn_inner_iterations"]),
        "step5d_rnn_backend": str(effective["step5d_rnn_backend"]),
        "step5d_epsilon": decimal_text(effective["step5d_epsilon"]),
        "step5d_sigr_exponent_r": decimal_text(effective["step5d_sigr_exponent_r"]),
    }


def verify_executable_metrics(metrics: Mapping[str, int | float]) -> None:
    values = [float(value) for value in metrics.values()]
    failed = (
        not values
        or any(not math.isfinite(value) for value in values)
        or int(metrics["replayed_rows"]) <= 0
        or int(metrics["accepted_rows"]) != int(metrics["replayed_rows"])
        or int(metrics["structural_failure_rows"]) != 0
        or float(metrics["rnn_oracle_qdot_delta_max_rad_s"]) > 1e-6
        or float(metrics["qdot_max_abs_rad_s"]) > 0.5 + 1e-12
        or float(metrics["slew_violation_max_rad_s"]) > 1e-12
    )
    if failed:
        raise GoldenReplayError(f"G10 executable replay differs: {dict(metrics)}")


def _verify_capture(
    csv_path: Path,
    *,
    golden: Mapping[str, Any],
    expected_profile: Mapping[str, str],
) -> tuple[int, list[tuple[int, ...]], dict[str, str]]:
    host_names = tuple(golden["host_packet"])
    expected_host = tuple(golden["host_packet"][name] for name in host_names)
    transitions: list[tuple[int, ...]] = []
    previous: tuple[int, ...] | None = None
    observed_profile: dict[str, set[str]] = {name: set() for name in RNN_COLUMNS}
    rows = 0
    with csv_path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        required = set(host_names) | {
            f"ur_output_int_register_{register}" for register in range(24, 31)
        } | set(RNN_COLUMNS.values())
        missing = sorted(required - set(reader.fieldnames or ()))
        if missing:
            raise GoldenReplayError(f"G10 capture columns are missing: {missing}")
        for row in reader:
            rows += 1
            host = tuple(integer(row, name) for name in host_names)
            if host != expected_host:
                raise GoldenReplayError(f"G10 host packet drift at row {rows}")
            packet = tuple(
                integer(row, f"ur_output_int_register_{register}")
                for register in range(24, 31)
            )
            if packet != previous:
                transitions.append(packet)
                previous = packet
            for name, column in RNN_COLUMNS.items():
                value = row[column]
                if value not in {"", "nan"}:
                    observed_profile[name].add(normalized_rnn_value(name, value))
    expected_transitions = [tuple(row) for row in golden["tp_transitions"]]
    if transitions != expected_transitions:
        raise GoldenReplayError(f"G10 TP transition order differs: {transitions}")
    normalized: dict[str, str] = {}
    for name, values in observed_profile.items():
        if len(values) != 1:
            raise GoldenReplayError(f"G10 has non-constant {name}: {sorted(values)}")
        normalized[name] = next(iter(values))
    if normalized != expected_profile:
        raise GoldenReplayError(
            f"G10 capture profile differs from production launcher: "
            f"expected={dict(expected_profile)} observed={normalized}"
        )
    return rows, transitions, normalized


def run(source_root: Path) -> dict[str, Any]:
    if sha256_file(GOLDEN) != GOLDEN_SHA256:
        raise GoldenReplayError("G10 specification digest differs")
    golden = load_json(GOLDEN, role="G10 specification")
    if golden.get("schema") != "step5d.autotune.golden-replay/v1":
        raise GoldenReplayError("G10 specification schema differs")
    source_root = source_root.expanduser().resolve(strict=True)
    if source_root.is_symlink() or not source_root.is_dir():
        raise GoldenReplayError("G10 source root must be a real directory")
    bundle_path = source_root / str(golden["trial_dir"]) / "immutable_trial_bundle.json"
    if bundle_path.is_symlink() or not bundle_path.is_file():
        raise GoldenReplayError("G10 immutable trial bundle is missing or unsafe")
    if sha256_file(bundle_path) != golden["bundle_sha256"]:
        raise GoldenReplayError("G10 immutable trial bundle digest differs")
    bundle = load_json(bundle_path, role="G10 immutable trial bundle")

    trial = bundle.get("trial") or {}
    candidate = trial.get("candidate") or {}
    candidate_values = {
        "force_p_gain": candidate.get("force_p_gain"),
        "force_i_gain": candidate.get("force_i_gain"),
        "force_damping": candidate.get("force_damping"),
    }
    actual_candidate = {
        "p": decimal_text(candidate_values["force_p_gain"]),
        "i": decimal_text(candidate_values["force_i_gain"]),
        "d": decimal_text(candidate_values["force_damping"]),
        "profile_id": (trial.get("execution_profile") or {}).get("profile_id"),
    }
    if actual_candidate != golden["candidate"]:
        raise GoldenReplayError("G10 candidate/profile identity differs")

    launch = check_effective_config(
        candidate_values,
        environ={},
        runtime_root=Path("/tmp/step5d-autotune-v3-g10-check"),
    )
    if launch["frozen_baseline"]["commit"] != FROZEN_COMMIT:
        raise GoldenReplayError("production launcher is not bound to frozen v1")
    effective = launch["effective_config"]
    if (
        effective["step5d_qdot_limit_rad_s"] != 0.5
        or effective["step5d_autotune_host_slew_rad_s2"] != 0.5
        or effective["step5d_autotune_speedj_acceleration_rad_s2"] != 0.5
    ):
        raise GoldenReplayError("G10 qdot/slew/TP acceleration contract differs")

    acceptance = golden["acceptance"]
    evaluation = bundle.get("evaluation") or {}
    capture = bundle.get("capture") or {}
    observed_acceptance = {
        "safe_closure": evaluation.get("safe_closure"),
        "complete_bins": evaluation.get("complete_bins"),
        "terminal_reason": capture.get("terminal_reason"),
        "returned_safe": capture.get("returned_safe"),
    }
    if observed_acceptance != acceptance:
        raise GoldenReplayError("G10 safe-closure acceptance differs")

    csv_row = (bundle.get("artifact_provenance") or {}).get("csv") or {}
    if csv_row.get("sha256") != golden["csv_sha256"]:
        raise GoldenReplayError("G10 bundle CSV identity differs")
    csv_path = Path(str(csv_row.get("path", ""))).resolve(strict=True)
    try:
        csv_path.relative_to(source_root)
    except ValueError as exc:
        raise GoldenReplayError("G10 CSV is outside the declared source root") from exc
    if csv_path.is_symlink() or not csv_path.is_file():
        raise GoldenReplayError("G10 CSV is missing or unsafe")
    if (
        csv_path.stat().st_size != golden["csv_size_bytes"]
        or csv_row.get("size_bytes") != golden["csv_size_bytes"]
        or sha256_file(csv_path) != golden["csv_sha256"]
    ):
        raise GoldenReplayError("G10 CSV digest or size differs")

    packet_rows, transitions, capture_profile = _verify_capture(
        csv_path,
        golden=golden,
        expected_profile=expected_rnn_profile(effective),
    )
    execution_profile = trial.get("execution_profile")
    if not isinstance(execution_profile, dict):
        raise GoldenReplayError("G10 execution profile is missing")
    replay = _run_candidate_bound_exact_replay(
        root=ROOT,
        trace_path=csv_path,
        candidate=ForceCandidate(
            force_p_gain=float(candidate_values["force_p_gain"]),
            force_i_gain=float(candidate_values["force_i_gain"]),
            force_damping=float(candidate_values["force_damping"]),
            target_force_n=float(candidate["target_force_n"]),
        ),
        execution_profile=ExecutionProfile(**execution_profile),
    )
    verify_executable_metrics(replay)
    return {
        "schema": "step5d.autotune-v3/g10-production-seam-result-v1",
        "ok": True,
        "no_motion": True,
        "group_id": golden["group_id"],
        "trial_uid": golden["trial_uid"],
        "golden_spec_sha256": GOLDEN_SHA256,
        "bundle_sha256": golden["bundle_sha256"],
        "csv_sha256": golden["csv_sha256"],
        "packet_rows": packet_rows,
        "tp_transitions": transitions,
        "control_fingerprint": launch["control_fingerprint"],
        "contract_sha256": launch["contract_sha256"],
        "launcher_rnn_profile": expected_rnn_profile(effective),
        "capture_rnn_profile": capture_profile,
        "executable_replay": replay,
    }


def write_atomic(path: Path, payload: Mapping[str, Any]) -> None:
    path = path.expanduser().absolute()
    if path.is_symlink() or path.parent.is_symlink():
        raise GoldenReplayError("G10 output path is unsafe")
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode("utf-8")
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary_path = Path(temporary)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
    finally:
        temporary_path.unlink(missing_ok=True)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    report = run(args.source_root)
    if args.output is not None:
        write_atomic(args.output, report)
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
