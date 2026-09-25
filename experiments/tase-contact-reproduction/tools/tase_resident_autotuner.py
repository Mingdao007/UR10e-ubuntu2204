"""Home-gated resident Md/Bd tuning on the current rate460 Figure-eight route.

The tuner proposes files and reads sealed results. Only figure8.sh owns the
robot, transport, TP, sensor, and recovery chain.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import re
import subprocess
import time
from typing import Any

from contact_yield_live import resident_candidate_can_continue
from tase_autotuner import Candidate, _candidate_for
from tase_integral_screening import (
    _campaign_sessions, _path_duration_matches, _session_verified_closed,
)


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG = ROOT / "config/tase_resident_autotuner_rate460_a_v1.json"
PROTOCOL = "figure8_window60_r013_rate460_v1"
LEGACY_RATE400_PROTOCOL = "figure8_window60_r013_rate400_v1"
RATE460_MIN_HZ = 460.0
RATE400_MIN_HZ = 400.0
TIMING_LAYERS = (
    "writer_publishes", "rtde_frames", "kunwei_frames", "tp_consumed_packet_echoes",
)
TOTAL = 24
SCREENING_ARM_INTEGRAL = {
    "A": ("legacy-clamp-v1", 1.0),
    "B": ("legacy-clamp-v1", 0.5),
    "C": ("legacy-clamp-v1", 0.1),
    "D": ("conditional-double-clamp-v1", 1.0),
    "E": ("integral-off-v1", 1.0),
}


def load_config(path: Path = DEFAULT_CONFIG, *,
                initial_attempt_result: Path | None = None) -> dict[str, Any]:
    config = json.loads(Path(path).read_text(encoding="utf-8"))
    if config.get("schema") != "tase.resident-autotuner-v1" or config.get("method") != "TASE_RNN_MATURE":
        raise ValueError("resident tuner schema or method differs")
    if config.get("formal_window_s") != [5.0, 60.0] or config.get("budget") != {"initial": 8, "bo": 12, "repeats": 4}:
        raise ValueError("resident tuner protocol window or budget differs")
    arm = config.get("selected_screening_arm")
    expected_integral = SCREENING_ARM_INTEGRAL.get(arm)
    if expected_integral is None or config.get("force_integral_authority_error_n") != 0.5:
        raise ValueError("resident tuner integral policy, selected arm, or authority differs")
    if (config.get("force_integral_policy"), config.get("force_integral_limit_n_s")) != expected_integral:
        raise ValueError("resident tuner integral configuration differs from selected screening arm")
    profile = config.get("protocol_id")
    if profile == PROTOCOL:
        expected_source = "first_live_a_baseline" if arm == "A" else "selected_screening_candidate"
        if (config.get("duration_token") != "r013_60_rate460"
            or config.get("initial_source") != expected_source
            or config.get("minimum_data_rate_hz", RATE460_MIN_HZ) != RATE460_MIN_HZ
            or (arm != "A" and not isinstance(config.get("screening_campaign"), str))):
            raise ValueError("rate460 resident tuner profile differs from its selected strategy")
        config["minimum_data_rate_hz"] = RATE460_MIN_HZ
    elif profile == LEGACY_RATE400_PROTOCOL:
        if (config.get("duration_token") != "r013_60_rate400"
            or arm != "B"
            or config.get("minimum_data_rate_hz", RATE400_MIN_HZ) != RATE400_MIN_HZ):
            raise ValueError("legacy rate400 B profile differs")
        config["minimum_data_rate_hz"] = RATE400_MIN_HZ
    else:
        raise ValueError("resident tuner protocol is unsupported")
    if initial_attempt_result is not None:
        config["initial_attempt_result"] = str(Path(initial_attempt_result).expanduser().resolve())
    source_path = config.get("initial_attempt_result")
    if source_path is not None:
        source = Path(source_path).expanduser()
        if source.is_symlink() or not source.is_file():
            raise ValueError("resident tuner baseline source must be a regular attempt-result file")
        source = source.resolve()
        seal = source.with_name("seal.json")
        if seal.is_symlink() or not seal.is_file():
            raise ValueError("resident tuner baseline source must have a regular seal.json")
        source_hash = hashlib.sha256(source.read_bytes()).hexdigest()
        seal_hash = hashlib.sha256(seal.read_bytes()).hexdigest()
        if config.get("initial_attempt_result_sha256") not in (None, source_hash):
            raise ValueError("resident tuner baseline receipt changed after it was frozen")
        if config.get("initial_seal_sha256") not in (None, seal_hash):
            raise ValueError("resident tuner baseline seal changed after it was frozen")
        config["initial_attempt_result"] = str(source)
        config["initial_attempt_result_sha256"] = source_hash
        config["initial_seal_sha256"] = seal_hash
    if config.get("initial_attempt_result") is None:
        config.pop("initial_attempt_result_sha256", None)
        config.pop("initial_seal_sha256", None)
    package = config.get("controller_readback_triplet")
    if package is not None and (
        not isinstance(package, dict)
        or set(package) != {"script", "txt", "urp"}
        or any(not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None
               for value in package.values())
    ):
        raise ValueError("resident tuner controller read-back identity is invalid")
    if config.get("center") != {"Md_scalar": 12.0, "Bd_scalar": 550.0}:
        raise ValueError("resident tuner search center differs")
    if config.get("log2_bounds") != {"Md_scalar": [-0.5, 0.5], "Bd_scalar": [-0.5, 0.5]}:
        raise ValueError("resident tuner absolute search box differs")
    if config.get("incumbent") != {"Md_scalar": 9.565272137974492,
                                     "Bd_scalar": 693.6559295653944}:
        raise ValueError("resident tuner incumbent differs")
    for key in ("seed", "bo_pool_size", "control_cpu"):
        if type(config.get(key)) is not int or config[key] < 0:
            raise ValueError(f"resident tuner {key} is invalid")
    if config["bo_pool_size"] < 1 or config.get("video_policy") not in {"required", "evidence-only"}:
        raise ValueError("resident tuner pool or video policy is invalid")
    for key in ("gp_length_scale", "observation_noise_n"):
        value = config.get(key)
        if not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
            raise ValueError(f"resident tuner {key} is invalid")
    return config


def candidate_payload(candidate: Candidate, config: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema": "tase.outer-parameters-v1",
        "candidate_id": candidate.candidate_id,
        "stage": candidate.stage,
        "index": candidate.index,
        "Md_scalar": candidate.Md_scalar,
        "Bd_scalar": candidate.Bd_scalar,
        "protocol_id": config["protocol_id"],
        "duration_token": config["duration_token"],
        "path_duration_s": 60.0,
        "frozen": {
            "kp": 4.0, "ko": 5.0, "kf": 1.0,
            "force_target_n": 5.0,
            "force_integral_limit_n_s": config["force_integral_limit_n_s"],
            "force_integral_policy": config["force_integral_policy"],
            "force_integral_authority_error_n": config["force_integral_authority_error_n"],
            "force_sign_convention": "step5_step6_positive_normal_load",
        },
    }


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        raise FileExistsError(f"candidate identity already exists: {path}")
    temporary = path.with_suffix(".tmp")
    with temporary.open("x", encoding="utf-8") as stream:
        json.dump(payload, stream, indent=2, sort_keys=True, allow_nan=False)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    temporary.rename(path)


def _dispatch_receipt_path(attempt_path: Path) -> Path:
    """Resolve the session receipt for attempts/<sequence>/attempt-result.json."""
    attempt_dir = attempt_path.parent
    if attempt_dir.parent.name == "attempts":
        return attempt_dir.parent.parent / "dispatch_receipt.json"
    return attempt_dir / "dispatch_receipt.json"


def _session_controller_triplet(
    attempt_path: Path,
    dispatch: dict[str, Any],
    expected: dict[str, str] | None,
) -> dict[str, str]:
    """Use the live read-back receipt while a resident session is still open.

    The session-level dispatch receipt is written only after the resident
    supervisor exits. A candidate can be sealed at Home earlier, so during a
    resident session the fresh controller receipt and fetched-back package
    files are the authoritative package identity available to the tuner.
    """

    triplet = dispatch.get("controller_readback_triplet")
    if isinstance(triplet, dict) and triplet:
        return dict(triplet)

    session_dir = attempt_path.parents[2]
    controller_path = session_dir / "controller_receipt.json"
    readback_result_path = session_dir / "readback-results.json"
    if not controller_path.is_file() or not readback_result_path.is_file():
        return {}
    try:
        controller = json.loads(controller_path.read_text(encoding="utf-8"))
        readback_result = json.loads(readback_result_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}

    program = "step5d_contact_six_qp_v1"
    target = f"/programs/andyl/kunwei/step5/{program}.urp"
    if (
        readback_result.get("pass") is not True
        or readback_result.get("scope") != "fresh GET of installed packages"
        or program not in (readback_result.get("basenames") or [])
        or controller.get("program") != program
        or controller.get("controller_target") != target
        or controller.get("safety_mode") != "NORMAL"
        or (controller.get("provenance") or {}).get("triplet") != "readback-results.json"
    ):
        return {}

    triplet = {
        "script": controller.get("script_sha256"),
        "txt": controller.get("txt_sha256"),
        "urp": controller.get("urp_sha256"),
    }
    if any(not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None
           for value in triplet.values()):
        return {}
    readback_dir = session_dir / "readback" / program
    for extension, key in (("script", "script"), ("txt", "txt"), ("urp", "urp")):
        path = readback_dir / f"{program}.{extension}"
        if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != triplet[key]:
            return {}
    if expected is not None and triplet != expected:
        return {}
    return triplet


def _screening_initial(config: dict[str, Any]) -> dict[str, Any]:
    protocol = config["protocol_id"]
    screen = (ROOT / config["screening_campaign"]).resolve()
    result = json.loads((screen / "screening-results.json").read_text(encoding="utf-8"))
    if (result.get("status") != "complete" or result.get("sessions_closed") is not True
        or result.get("protocol_id") != protocol
        or result.get("selected_arm") != config.get("selected_screening_arm")
        or result.get("attempted") != 25):
        raise ValueError("selected integral-screening evidence is incomplete")
    candidate_id = f"screen-{config['selected_screening_arm']}-00"
    matching = [row for row in result["attempts"] if row["candidate_id"] == candidate_id]
    if len(matching) != 1 or matching[0].get("status") != "complete":
        raise ValueError("selected screening initial-00 source is not complete")
    source = matching[0]
    session = Path(source["run_dir"]).expanduser().resolve()
    try:
        session.relative_to(screen)
    except ValueError as exc:
        raise ValueError("selected screening initial-00 run is outside its frozen campaign") from exc
    # The screening ordinal is global; a resumed session starts its local
    # sequence at 1. Resolve by exact physical candidate identity instead.
    candidates = []
    for path in session.glob("attempts/*/attempt-result.json"):
        payload = json.loads(path.read_text(encoding="utf-8"))
        binding = payload.get("parameter_binding") or (payload.get("state") or {}).get("parameter_binding") or {}
        if binding.get("candidate_id") == candidate_id:
            candidates.append(path)
    if len(candidates) != 1:
        raise ValueError("screening initial-00 physical source is missing or ambiguous")
    attempt_path = candidates[0]
    item = json.loads(attempt_path.read_text(encoding="utf-8"))
    binding = item.get("parameter_binding") or {}
    applied = item.get("applied_runtime_parameters") or {}
    metrics = (item.get("evidence") or {}).get("metrics") or {}
    timing = metrics.get("timing_evidence") or {}
    layer_rates = timing.get("layer_rates_hz") or {}
    minimum_rate = float(config.get("minimum_data_rate_hz", RATE400_MIN_HZ))
    required_rates_ok = (
        timing.get("acceptance_protocol_id") == protocol
        and timing.get("successful") is True
        and timing.get("minimum_rate_hz") == minimum_rate
        and all(
            isinstance(layer_rates.get(name), (int, float))
            and not isinstance(layer_rates.get(name), bool)
            and math.isfinite(float(layer_rates[name]))
            and float(layer_rates[name]) >= minimum_rate
            for name in TIMING_LAYERS
        )
    )
    dispatch_path = session / "dispatch_receipt.json"
    dispatch = json.loads(dispatch_path.read_text(encoding="utf-8")) if dispatch_path.is_file() else {}
    dispatch_live_path = dispatch.get("live_path") or {}
    dispatch_controller = dispatch.get("controller_identity") or {}
    controller_triplet = dispatch.get("controller_readback_triplet") or {}
    source_triplet = source.get("controller_readback_triplet_sha256")
    triplet_valid = (
        isinstance(controller_triplet, dict)
        and set(controller_triplet) == {"script", "txt", "urp"}
        and all(isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value)
                for value in controller_triplet.values())
    )
    dispatch_identity_valid = (
        dispatch.get("method") == "TASE_RNN_MATURE"
        and dispatch_controller.get("method") == "TASE_RNN_MATURE"
        and dispatch_live_path.get("protocol_id") == protocol
        and dispatch_live_path.get("path_duration_s") == 60.0
    )
    if (item.get("evidence_eligible") is not True
        or item.get("physical_dispatched") is not True
        or (item.get("lifecycle") or {}).get("path_complete") is not True
        or (item.get("lifecycle") or {}).get("home_verified") is not True
        or binding.get("protocol_id") != protocol
        or binding.get("Md_scalar") != config["incumbent"]["Md_scalar"]
        or binding.get("Bd_scalar") != config["incumbent"]["Bd_scalar"]
        or binding.get("force_integral_policy") != config["force_integral_policy"]
        or binding.get("force_integral_limit_n_s") != config["force_integral_limit_n_s"]
        or applied.get("force_integral_policy") != config["force_integral_policy"]
        or applied.get("force_integral_limit_n_s") != config["force_integral_limit_n_s"]
        or applied.get("force_integral_authority_error_n") != config["force_integral_authority_error_n"]
        or metrics.get("complete_bins") != 550
        or not _path_duration_matches(metrics, 60.0)
        or metrics.get("protocol_id") != protocol
        or metrics.get("timing_gate_passed") is not True
        or not required_rates_ok
        or not attempt_path.with_name("seal.json").is_file()
        or metrics.get("normal_force_mae_n") != source["mae_n"]
        or not dispatch_identity_valid
        or (protocol == PROTOCOL and (
            not triplet_valid
            or not isinstance(source_triplet, dict)
            or controller_triplet != source_triplet
            or config.get("controller_readback_triplet") not in (None, controller_triplet)
        ))):
        raise ValueError("screening initial-00 runtime does not match tuning initial-00")
    return {
        "ordinal": 0, "candidate_id": "initial-00", "source_candidate_id": candidate_id,
        "stage": "initial", "index": 0,
        "Md_scalar": binding["Md_scalar"], "Bd_scalar": binding["Bd_scalar"],
        "status": "complete", "mae_n": source["mae_n"],
        "source_attempt_result": str(attempt_path), "source_screening": str(screen),
        "protocol_id": protocol, "duration_token": config["duration_token"],
        "force_integral_policy": config["force_integral_policy"],
        "force_integral_limit_n_s": config["force_integral_limit_n_s"],
        "controller_readback_triplet": controller_triplet or None,
        "reused_screening_unit": True,
    }


def _baseline_initial(config: dict[str, Any]) -> dict[str, Any]:
    """Admit the exact first full A baseline as initial-00 without rewriting it."""
    raw_path = config.get("initial_attempt_result")
    if not isinstance(raw_path, str) or not raw_path:
        raise ValueError("rate460 A Autotuner requires the first sealed A baseline receipt")
    attempt_path = Path(raw_path).expanduser()
    if attempt_path.is_symlink() or not attempt_path.is_file():
        raise ValueError("rate460 A initial source is not a regular attempt-result file")
    attempt_path = attempt_path.resolve()
    seal_path = attempt_path.with_name("seal.json")
    if seal_path.is_symlink() or not seal_path.is_file():
        raise ValueError("rate460 A initial source has no regular seal.json")
    item = json.loads(attempt_path.read_text(encoding="utf-8"))
    seal = json.loads(seal_path.read_text(encoding="utf-8"))
    binding = item.get("parameter_binding") or (item.get("state") or {}).get("parameter_binding") or {}
    frozen = binding.get("frozen") or {}
    evidence = item.get("evidence") or {}
    metrics = evidence.get("metrics") or {}
    timing = metrics.get("timing_evidence") or {}
    lifecycle = item.get("lifecycle") or {}
    seal_lifecycle = seal.get("lifecycle") or {}
    dispatch_path = _dispatch_receipt_path(attempt_path)
    dispatch = json.loads(dispatch_path.read_text(encoding="utf-8")) if dispatch_path.is_file() else {}
    rates = timing.get("layer_rates_hz") or {}
    required_rates_ok = all(
        isinstance(rates.get(name), (int, float))
        and not isinstance(rates.get(name), bool)
        and math.isfinite(float(rates[name]))
        and float(rates[name]) >= 460.0
        for name in TIMING_LAYERS
    )
    required_rates_ok = required_rates_ok and timing.get("minimum_rate_hz") == RATE460_MIN_HZ
    controller_triplet = dispatch.get("controller_readback_triplet") or {}
    controller_triplet_ok = (
        isinstance(controller_triplet, dict)
        and set(controller_triplet) == {"script", "txt", "urp"}
        and all(isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value)
                for value in controller_triplet.values())
    )
    mae = metrics.get("normal_force_mae_n", metrics.get("mae_n"))
    checks = {
        "physical_dispatched": item.get("physical_dispatched") is True
            or dispatch.get("executed") is True,
        "method": dispatch.get("method") == "TASE_RNN_MATURE",
        "protocol": binding.get("protocol_id") == config["protocol_id"]
            and metrics.get("protocol_id") == config["protocol_id"]
            and timing.get("acceptance_protocol_id") == config["protocol_id"],
        "duration": binding.get("duration_token") == config["duration_token"]
            and metrics.get("path_duration_s") == 60.0
            and timing.get("duration_s") == 60.0,
        "A_parameters": (
            binding.get("Md_scalar") == config["incumbent"]["Md_scalar"]
            and binding.get("Bd_scalar") == config["incumbent"]["Bd_scalar"]
            and binding.get("force_integral_policy", frozen.get("force_integral_policy")) == "legacy-clamp-v1"
            and binding.get("force_integral_limit_n_s", frozen.get("force_integral_limit_n_s")) == 1.0
        ),
        "formal_window": metrics.get("formal_window_s") == [5.0, 60.0]
            and metrics.get("complete_bins") == 550
            and metrics.get("complete") is True,
        "timing": metrics.get("timing_gate_passed") is True and required_rates_ok,
        "lifecycle": item.get("evidence_eligible") is True
            and lifecycle.get("path_complete") is True
            and lifecycle.get("home_verified") is True
            and lifecycle.get("ready_for_next") is True
            and lifecycle.get("program_stopped") is True
            and (lifecycle.get("sealed") is True or seal_lifecycle.get("sealed") is True)
            and seal_lifecycle.get("path_complete") is True
            and seal_lifecycle.get("home_verified") is True
            and seal_lifecycle.get("program_stopped") is True,
        "seal": seal.get("schema") == "yield-live-entry/attempt-seal-v1"
            and seal_lifecycle.get("sealed") is True,
        "controller_readback": controller_triplet_ok,
        "finite_mae": isinstance(mae, (int, float))
            and not isinstance(mae, bool)
            and math.isfinite(float(mae),),
    }
    if not all(checks.values()):
        failed = [key for key, passed in checks.items() if not passed]
        raise ValueError("rate460 A initial attempt is not an admitted baseline: " + ", ".join(failed))
    source_candidate = str(binding.get("candidate_id") or "figure8_rate460_A_baseline")
    return {
        "ordinal": 0,
        "candidate_id": "initial-00",
        "source_candidate_id": source_candidate,
        "stage": "initial",
        "index": 0,
        "Md_scalar": binding["Md_scalar"],
        "Bd_scalar": binding["Bd_scalar"],
        "status": "complete",
        "mae_n": float(mae),
        "source_attempt_result": str(attempt_path),
        "protocol_id": config["protocol_id"],
        "duration_token": config["duration_token"],
        "force_integral_policy": "legacy-clamp-v1",
        "force_integral_limit_n_s": 1.0,
        "controller_readback_triplet": controller_triplet,
        "reused_first_live_A_baseline": True,
    }


def _initial_row(config: dict[str, Any]) -> dict[str, Any]:
    if config.get("initial_source") == "first_live_a_baseline":
        return _baseline_initial(config)
    return _screening_initial(config)


def _read_ledger(path: Path, *, protocol_id: str = PROTOCOL) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]
    if [row.get("ordinal") for row in rows] != list(range(len(rows))):
        raise ValueError("resident tuner ledger ordinals are not contiguous")
    if any(row.get("protocol_id") != protocol_id or row.get("status") not in {"complete", "failed"}
           for row in rows):
        raise ValueError("resident tuner ledger contains another protocol or nonterminal row")
    return rows


def _append_ledger(path: Path, row: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(row, sort_keys=True, allow_nan=False) + "\n")
        stream.flush()
        os.fsync(stream.fileno())


def _persist_ledger_correction(
    campaign_dir: Path,
    ledger_path: Path,
    ordinal: int,
    previous: dict[str, Any],
    replacement: dict[str, Any],
) -> None:
    """Record an evidence-backed score repair before atomically updating the ledger."""

    correction_path = campaign_dir / "ledger-corrections.jsonl"
    correction = {
        "schema": "tase.resident-autotuner-ledger-correction-v1",
        "recorded_at_unix_s": time.time(),
        "ordinal": ordinal,
        "candidate_id": previous["candidate_id"],
        "reason": "fresh controller triplet was sealed after the resident attempt score was first written",
        "previous": previous,
        "replacement": replacement,
    }
    existing_corrections = (
        [json.loads(line) for line in correction_path.read_text(encoding="utf-8").splitlines() if line]
        if correction_path.is_file() else []
    )
    prior_correction = next((row for row in existing_corrections
                             if row.get("ordinal") == ordinal
                             and row.get("candidate_id") == previous["candidate_id"]), None)
    if prior_correction is not None:
        if (prior_correction.get("previous") != previous
            or prior_correction.get("replacement") != replacement):
            raise ValueError("resident tuner score correction conflicts with prior audit")
    else:
        _append_ledger(correction_path, correction)

    ledger_rows = [json.loads(line) for line in ledger_path.read_text(encoding="utf-8").splitlines() if line]
    if ordinal >= len(ledger_rows):
        raise ValueError("resident tuner correction ordinal is absent from the ledger")
    if ledger_rows[ordinal] == replacement:
        return
    if ledger_rows[ordinal] != previous:
        raise ValueError("resident tuner ledger changed before the score correction")
    ledger_rows[ordinal] = replacement
    temporary = ledger_path.with_name(f"{ledger_path.name}.{os.getpid()}.tmp")
    with temporary.open("x", encoding="utf-8") as stream:
        for row in ledger_rows:
            stream.write(json.dumps(row, sort_keys=True, allow_nan=False) + "\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, ledger_path)
    directory_fd = os.open(ledger_path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def _score_item(path: Path, candidate: Candidate, ordinal: int,
                config: dict[str, Any]) -> dict[str, Any]:
    item = json.loads(path.read_text(encoding="utf-8"))
    binding = item.get("parameter_binding") or (item.get("state") or {}).get("parameter_binding") or {}
    applied = item.get("applied_runtime_parameters") or {}
    if item.get("physical_dispatched") is not True or binding.get("candidate_id") != candidate.candidate_id:
        raise ValueError("resident tuner physical candidate identity differs")
    expected = candidate_payload(candidate, config)
    if (binding.get("protocol_id") != config["protocol_id"]
        or binding.get("Md_scalar") != candidate.Md_scalar
        or binding.get("Bd_scalar") != candidate.Bd_scalar
        or binding.get("force_integral_policy") != expected["frozen"]["force_integral_policy"]
        or binding.get("force_integral_limit_n_s") != expected["frozen"]["force_integral_limit_n_s"]):
        raise ValueError("resident tuner requested parameters differ from applied binding")
    if item.get("partial") is not True and not item.get("failed_after_collector"):
        for key, value in {
            "Md_scalar": candidate.Md_scalar, "Bd_scalar": candidate.Bd_scalar,
            "force_integral_policy": config["force_integral_policy"],
            "force_integral_limit_n_s": config["force_integral_limit_n_s"],
            "force_integral_authority_error_n": config["force_integral_authority_error_n"],
        }.items():
            if applied.get(key) != value:
                raise ValueError(f"resident tuner runtime {key} was not applied")
    metrics = (item.get("evidence") or {}).get("metrics") or {}
    timing = metrics.get("timing_evidence") or {}
    layer_rates = timing.get("layer_rates_hz") or {}
    minimum_rate_hz = float(config.get("minimum_data_rate_hz", RATE460_MIN_HZ))
    required_rates_ok = (
        timing.get("acceptance_protocol_id") == config["protocol_id"]
        and isinstance(timing.get("minimum_rate_hz"), (int, float))
        and float(timing["minimum_rate_hz"]) == minimum_rate_hz
        and all(
            isinstance(layer_rates.get(name), (int, float))
            and not isinstance(layer_rates.get(name), bool)
            and math.isfinite(float(layer_rates[name]))
            and float(layer_rates[name]) >= minimum_rate_hz
            for name in TIMING_LAYERS
        )
    )
    dispatch_path = _dispatch_receipt_path(path)
    dispatch = json.loads(dispatch_path.read_text(encoding="utf-8")) if dispatch_path.is_file() else {}
    expected_package = config.get("controller_readback_triplet")
    package_identity = _session_controller_triplet(path, dispatch, expected_package)
    package_matches = (
        config["protocol_id"] == LEGACY_RATE400_PROTOCOL
        and expected_package is None
    ) or (isinstance(expected_package, dict) and package_identity == expected_package)
    eligible = bool(
        item.get("evidence_eligible") is True
        and item.get("failed_after_collector") is None
        and (item.get("lifecycle") or {}).get("path_complete") is True
        and (item.get("lifecycle") or {}).get("home_verified") is True
        and metrics.get("complete_bins") == 550
        and _path_duration_matches(metrics, 60.0)
        and metrics.get("protocol_id") == config["protocol_id"]
        and metrics.get("timing_gate_passed") is True
        and required_rates_ok
        and package_matches
        and path.with_name("seal.json").is_file()
        and isinstance(metrics.get("normal_force_mae_n"), (int, float))
        and math.isfinite(float(metrics["normal_force_mae_n"]))
    )
    lifecycle = item.get("lifecycle") or {}
    safe_closure = bool(
        lifecycle.get("path_complete") is True
        and lifecycle.get("home_verified") is True
        and lifecycle.get("ready_for_next") is True
        and path.with_name("seal.json").is_file()
        and metrics.get("complete_bins") == 550
    )
    safe_to_continue = bool(
        safe_closure
        and (
            item.get("evidence_eligible") is True
            or resident_candidate_can_continue(item, research_campaign=True)
        )
    )
    return {
        "ordinal": ordinal, "candidate_id": candidate.candidate_id,
        "stage": candidate.stage, "index": candidate.index,
        "Md_scalar": candidate.Md_scalar, "Bd_scalar": candidate.Bd_scalar,
        "protocol_id": config["protocol_id"], "duration_token": config["duration_token"],
        "status": "complete" if eligible else "failed",
        "mae_n": float(metrics["normal_force_mae_n"]) if eligible else None,
        "diagnostic_mae_n": metrics.get("normal_force_mae_n"),
        "failure": None if eligible else (item.get("evidence") or {}).get("failure")
                   or item.get("failed_after_collector") or metrics.get("failure")
                   or "evidence_ineligible",
        "complete_bins": metrics.get("complete_bins"),
        "home_verified": lifecycle.get("home_verified") is True,
        "safe_to_continue": safe_to_continue and package_matches,
        "controller_readback_triplet": package_identity,
        "source_attempt_result": str(path),
    }


def _reconcile_sealed_sessions(campaign_dir: Path, config: dict[str, Any],
                               ledger: Path, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Recover a sealed physical result if the tuner exited before ledger fsync."""

    observed = [path for session in _campaign_sessions(campaign_dir)
                for path in sorted(session.glob("attempts/*/attempt-result.json"))]
    recorded = rows[1:]
    if len(recorded) > len(observed):
        raise ValueError("resident tuner ledger claims attempts absent from sealed sessions")
    for index, path in enumerate(observed):
        if not path.with_name("seal.json").is_file():
            raise RuntimeError("resident tuner physical result lacks seal")
        ordinal = index + 1
        if index < len(recorded):
            previous = rows[ordinal]
            if Path(previous.get("source_attempt_result", "")) != path:
                raise ValueError("resident tuner ledger source path differs from physical order")
            if (previous.get("status") == "failed"
                and previous.get("failure") == "evidence_ineligible"
                and previous.get("controller_readback_triplet") == {}
                and _session_verified_closed(path.parents[2])):
                candidate = Candidate(
                    str(previous["candidate_id"]), str(previous["stage"]), int(previous["index"]),
                    float(previous["Md_scalar"]), float(previous["Bd_scalar"]),
                )
                rescored = _score_item(path, candidate, ordinal, config)
                if rescored["status"] == "complete":
                    rescored["score_correction"] = {
                        "previous_status": "failed",
                        "previous_failure": previous["failure"],
                        "reason": "attempt evidence passed after the verified session receipt supplied the fresh package triplet",
                    }
                    _persist_ledger_correction(
                        campaign_dir, ledger, ordinal, previous, rescored,
                    )
                    rows[ordinal] = rescored
            continue
        candidate = _candidate_for(config, rows, ordinal)
        row = _score_item(path, candidate, ordinal, config)
        _append_ledger(ledger, row)
        rows.append(row)
    return rows


def _next_generation(campaign_dir: Path) -> int:
    used = [int(match.group(1)) for path in campaign_dir.iterdir()
            if (match := re.fullmatch(r"session-([0-9]+)(?:-candidates)?", path.name))]
    return max(used, default=0) + 1


def summarize(campaign_dir: Path, config: dict[str, Any]) -> dict[str, Any]:
    rows = _read_ledger(campaign_dir / "ledger.jsonl", protocol_id=config["protocol_id"])
    sessions = _campaign_sessions(campaign_dir)
    closed = bool(sessions) and all(_session_verified_closed(session) for session in sessions)
    complete = [row for row in rows if row["status"] == "complete"]
    tuned = [row for row in complete if row["stage"] in {"initial", "bo"}]
    best = min(tuned, key=lambda row: row["mae_n"]) if tuned else None
    result = {
        "schema": "tase.resident-autotuner-summary-v1",
        "protocol_id": config["protocol_id"],
        "duration_token": config["duration_token"],
        "selected_screening_arm": config.get("selected_screening_arm"),
        "budget": config["budget"],
        "attempted": len(rows), "complete": len(complete),
        "failed": len(rows) - len(complete),
        "score_corrections": len(list((campaign_dir / "ledger-corrections.jsonl").open()))
            if (campaign_dir / "ledger-corrections.jsonl").is_file() else 0,
        "resident_sessions_closed": closed,
        "status": "complete" if len(rows) == TOTAL and closed else "incomplete",
        "best": best,
        "claim_scope": "tuning only; paired confirmation is separate",
    }
    (campaign_dir / "summary.json").write_text(
        json.dumps(result, indent=2, sort_keys=True, allow_nan=False) + "\n", encoding="utf-8"
    )
    return result


def run_campaign(config_path: Path, campaign_dir: Path, *, resume: bool = False,
                 baseline_attempt_result: Path | None = None) -> dict[str, Any]:
    config = load_config(config_path, initial_attempt_result=baseline_attempt_result)
    campaign_dir = Path(campaign_dir).expanduser().resolve()
    if campaign_dir.exists() and not resume:
        raise FileExistsError("resident tuner campaign exists; use --resume")
    frozen_config = campaign_dir / "config-frozen.json"
    if frozen_config.is_file():
        frozen = json.loads(frozen_config.read_text(encoding="utf-8"))
        if resume:
            immutable_keys = set(config) - {
                "initial_attempt_result", "initial_attempt_result_sha256", "initial_seal_sha256",
                "controller_readback_triplet",
            }
            if any(frozen.get(key) != config.get(key) for key in immutable_keys):
                raise ValueError("resident tuner static profile changed across resume")
            frozen_initial = frozen.get("initial_attempt_result")
            config = load_config(
                config_path,
                initial_attempt_result=Path(frozen_initial) if isinstance(frozen_initial, str) else None,
            )
            if any(config.get(key) != frozen.get(key) for key in (
                "initial_attempt_result", "initial_attempt_result_sha256", "initial_seal_sha256",
            ) if key in frozen):
                raise ValueError("resident tuner frozen baseline identity changed")
        elif frozen != config:
            raise ValueError("resident tuner config changed across resume")
    elif resume:
        raise FileNotFoundError("resident tuner resume requires config-frozen.json")
    initial_row = _initial_row(config)
    source_identity = initial_row.get("controller_readback_triplet")
    if config["protocol_id"] == PROTOCOL:
        if not isinstance(source_identity, dict):
            raise ValueError("rate460 campaign baseline lacks controller read-back identity")
        if config.get("controller_readback_triplet") not in (None, source_identity):
            raise ValueError("rate460 campaign baseline controller identity differs")
        config["controller_readback_triplet"] = source_identity
        if frozen_config.is_file() and frozen_config.read_text(encoding="utf-8").strip():
            frozen = json.loads(frozen_config.read_text(encoding="utf-8"))
            if frozen.get("controller_readback_triplet") != source_identity:
                raise ValueError("resident tuner frozen controller read-back identity changed")
    campaign_dir.mkdir(parents=True, exist_ok=True)
    if not frozen_config.is_file():
        _atomic_json(frozen_config, config)
    ledger = campaign_dir / "ledger.jsonl"
    rows = _read_ledger(ledger, protocol_id=config["protocol_id"])
    if not rows:
        _append_ledger(ledger, initial_row)
        rows = _read_ledger(ledger, protocol_id=config["protocol_id"])
    use_baseline_receipt = config.get("initial_source") == "first_live_a_baseline"
    expected_initial_source = (
        initial_row.get("source_attempt_result") if use_baseline_receipt
        else initial_row.get("source_candidate_id")
    )
    actual_initial_source = (
        rows[0].get("source_attempt_result") if use_baseline_receipt
        else rows[0].get("source_candidate_id")
    )
    if actual_initial_source != expected_initial_source:
        raise ValueError("resident tuner initial-00 source differs")
    sessions = _campaign_sessions(campaign_dir)
    if sessions and not all(_session_verified_closed(session) for session in sessions):
        raise RuntimeError("resident tuner previous owner lacks final Home and STOPPED")
    rows = _reconcile_sealed_sessions(campaign_dir, config, ledger, rows)
    if len(rows) >= TOTAL:
        return summarize(campaign_dir, config)
    # A failed launch can leave a candidate directory without ever creating
    # its live session. Preserve it as evidence and use a fresh generation.
    generation = _next_generation(campaign_dir)
    session_dir = campaign_dir / f"session-{generation:02d}"
    candidate_dir = campaign_dir / f"session-{generation:02d}-candidates"
    candidate_dir.mkdir(exist_ok=False)
    start_ordinal = len(rows)
    first = _candidate_for(config, rows, start_ordinal)
    _atomic_json(candidate_dir / "candidate-0001.json", candidate_payload(first, config))
    command = [
        str(ROOT / "scripts/figure8.sh"), "--method", "TASE_RNN_MATURE",
        "--duration", config["duration_token"], "--control-cpu", str(config["control_cpu"]),
        "--video-policy", config["video_policy"],
        "--resident-attempts", str(TOTAL - start_ordinal),
        "--resident-candidate-dir", str(candidate_dir),
        "--run-dir", str(session_dir),
    ]
    process = subprocess.Popen(command, cwd=str(ROOT))
    ordinal = start_ordinal
    sequence = 1
    try:
        while ordinal < TOTAL:
            candidate = _candidate_for(config, rows, ordinal)
            path = session_dir / "attempts" / f"{sequence:04d}" / "attempt-result.json"
            while not (path.is_file() and path.with_name("seal.json").is_file()):
                if process.poll() is not None:
                    process.wait()
                    if path.is_file() and path.with_name("seal.json").is_file():
                        break
                    raise RuntimeError(f"resident owner exited without sealed ordinal {ordinal}")
                time.sleep(0.1)
            row = _score_item(path, candidate, ordinal, config)
            _append_ledger(ledger, row)
            rows.append(row)
            ordinal += 1
            sequence += 1
            summarize(campaign_dir, config)
            if ordinal >= TOTAL or not row["safe_to_continue"] or process.poll() is not None:
                break
            next_candidate = _candidate_for(config, rows, ordinal)
            _atomic_json(
                candidate_dir / f"candidate-{sequence:04d}.json",
                candidate_payload(next_candidate, config),
            )
        process.wait()
    except BaseException:
        # A tuner-side exception must not orphan a resident TP/writer waiting
        # at Home. The existing public stop path owns the joint-Home closure.
        if process.poll() is None:
            subprocess.run([str(ROOT / "scripts/figure8.sh"), "--stop", "--run-dir",
                            str(session_dir)], cwd=str(ROOT), check=False)
            process.wait()
        raise
    summary = summarize(campaign_dir, config)
    summary["last_owner_returncode"] = process.returncode
    (campaign_dir / "summary.json").write_text(
        json.dumps(summary, indent=2, sort_keys=True, allow_nan=False) + "\n", encoding="utf-8"
    )
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--campaign-dir", type=Path, required=True)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--baseline-attempt-result", type=Path)
    args = parser.parse_args()
    print(json.dumps(run_campaign(args.config, args.campaign_dir, resume=args.resume,
                                  baseline_attempt_result=args.baseline_attempt_result),
                     sort_keys=True, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
