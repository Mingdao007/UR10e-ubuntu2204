#!/usr/bin/env python3
"""Verify the immutable v2 migration/TP evidence reused by inactive v3."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
from pathlib import Path
from typing import Any, Mapping, Sequence


ROOT = Path(__file__).resolve().parents[1]
V3_STAGE_ID = "step5d_strict_rnn_autotune_v3"
V1_STAGE_ID = "step5d_strict_rnn_autotune_v1"
TP_FINGERPRINT = "c2761f200c58d9825e37dc76907b5e25b2d9982cda7dff1cf7de9699dbad7bb9"
LEDGER_SHA256 = "19cf2241ea070e3dc8eccfbe118660104f4c3f8e40ea25cb6f0efecabc7acf99"
URSIM_IMAGE = (
    "universalrobots/ursim_e-series@sha256:"
    "730c20b9609279a50a5bc5d16503e3fd0b096534c671818f7987547609558ab3"
)
URSIM_RAW_SHA256 = "83df91fbc6256d7380bd5f6761326fd9885511dbb858604e2a87324b09cc6dc3"
URSIM_RESULT_SHA256 = "f4dcd36dc1a593c1f8b7c20be321904a71199e0c4281f8c31b2960c490046fb9"
EXPECTED_SHA256 = {
    "programs/step5/step5d/step5d_strict_rnn_autotune_v2.script":
        "d3e52cbb341adad5c6924c155e3e4c15890379f0a251647f4f1de0235fc1ad05",
    "programs/step5/step5d/step5d_strict_rnn_autotune_v2.txt":
        "f734706e65c75a0f25c30d655f69523f6c32637efd5914a78261981b97c0c6cc",
    "programs/step5/step5d/step5d_strict_rnn_autotune_v2.urp":
        "3287e9913e8ba07f9d86549e7441f383f644e64cf33f956c7221e44df6621153",
    "programs/step5/step5d/step5d_strict_rnn_autotune_v2.deploy-manifest.json":
        TP_FINGERPRINT,
    "config/step5/tp_watchdog_v2.json":
        "fdea0f91364b7bf67c162b352e634b439671fdce2b42a4b07f531c05ad6c2750",
    "config/step5/tp_watchdog_blocks/host_heartbeat_fail_closed_v2.script":
        "fe0c5a2e991fcc065efde75760f5c164b14f161cc5c2b8668941c64fbf4aa8e4",
    "config/step5d_autotune_controller_readback_v2.json":
        "d29c98a1746c06c0fd18b928c15b24113b2abe824f00351f444eec83ec7ee06b",
    "config/step5/step5d_autotune_v3_attempt_ledger.json": LEDGER_SHA256,
    "config/step5/golden_replay_g10_v1.json":
        "f56a3eef529494b6c209ca5534abec082519848446724a94426de522852260f8",
    "config/step5/golden_replay_g10_v3_result.json":
        "faab489644a52f554d01c30e4d51916173aa0b0701332438d7358e04bfca7567",
    "config/step5/step5d_autotune_v3_ursim_hold_raw.json": URSIM_RAW_SHA256,
    "config/step5/step5d_autotune_v3_ursim_hold_result.json": URSIM_RESULT_SHA256,
    "config/step5/step5d_autotune_v3_offline_validation.json":
        "aace17206d5f64623e8b13f59ccda3231b264ab17eceac2801107001e611ae4d",
}
TRIPLET_SHA256 = {
    ".script": EXPECTED_SHA256[
        "programs/step5/step5d/step5d_strict_rnn_autotune_v2.script"
    ],
    ".txt": EXPECTED_SHA256[
        "programs/step5/step5d/step5d_strict_rnn_autotune_v2.txt"
    ],
    ".urp": EXPECTED_SHA256[
        "programs/step5/step5d/step5d_strict_rnn_autotune_v2.urp"
    ],
}
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class ArtifactVerificationError(RuntimeError):
    """The immutable evidence bundle or inactive selector is inconsistent."""


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _unique_object(pairs: Sequence[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ArtifactVerificationError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _load_json(path: Path, *, role: str) -> Mapping[str, Any]:
    try:
        payload = json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=_unique_object,
            parse_constant=lambda value: (_ for _ in ()).throw(
                ArtifactVerificationError(f"non-finite JSON constant: {value}")
            ),
        )
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ArtifactVerificationError(f"{role} is not valid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise ArtifactVerificationError(f"{role} must be an object")
    return payload


def _require_equal(actual: Any, expected: Any, role: str) -> None:
    if actual != expected:
        raise ArtifactVerificationError(
            f"{role} differs: expected={expected!r}, observed={actual!r}"
        )


def _verify_exact_files(root: Path) -> None:
    for relative, expected in EXPECTED_SHA256.items():
        path = root / relative
        if path.is_symlink() or not path.is_file():
            raise ArtifactVerificationError(f"immutable artifact missing or unsafe: {relative}")
        observed = _sha256(path)
        if observed != expected:
            raise ArtifactVerificationError(
                f"immutable artifact digest differs: {relative}: {observed}"
            )


def verify(root: Path = ROOT) -> dict[str, Any]:
    root = root.expanduser().resolve(strict=True)
    _verify_exact_files(root)

    deploy = _load_json(
        root / "programs/step5/step5d/step5d_strict_rnn_autotune_v2.deploy-manifest.json",
        role="TP deploy manifest",
    )
    _require_equal(deploy.get("schema_version"), 1, "TP deploy schema")
    _require_equal(deploy.get("basename"), "step5d_strict_rnn_autotune_v2", "TP basename")
    expected_artifacts = [
        {
            "filename": f"step5d_strict_rnn_autotune_v2{extension}",
            "sha256": digest,
            "source": f"step5d_strict_rnn_autotune_v2{extension}",
        }
        for extension, digest in TRIPLET_SHA256.items()
    ]
    _require_equal(deploy.get("artifacts"), expected_artifacts, "TP deploy artifacts")

    readback = _load_json(
        root / "config/step5d_autotune_controller_readback_v2.json",
        role="controller readback attestation",
    )
    _require_equal(readback.get("schema"), "step5d.autotune.controller-readback/v2", "readback schema")
    _require_equal(readback.get("verified"), True, "historical readback verification")
    _require_equal(readback.get("tp_fingerprint"), TP_FINGERPRINT, "TP fingerprint")
    _require_equal(readback.get("triplet_sha256"), TRIPLET_SHA256, "readback triplet")

    watchdog = _load_json(
        root / "config/step5/tp_watchdog_v2.json", role="TP watchdog manifest"
    )
    for field in ("control_math_changed", "trajectory_changed", "waypoint_changed"):
        _require_equal(watchdog.get(field), False, f"watchdog {field}")
    _require_equal(
        watchdog.get("auto_home_after_heartbeat_loss"),
        False,
        "watchdog auto-home policy",
    )
    delivery = watchdog.get("controller_delivery") or {}
    if not isinstance(delivery, dict) or set(delivery.values()) != {True}:
        raise ArtifactVerificationError("watchdog controller-delivery proof is incomplete")
    blocks = (watchdog.get("watchdog_diff_gate") or {}).get("required_blocks")
    expected_block = [{
        "block_id": "host_heartbeat_fail_closed_v2",
        "source": "config/step5/tp_watchdog_blocks/host_heartbeat_fail_closed_v2.script",
        "sha256": EXPECTED_SHA256[
            "config/step5/tp_watchdog_blocks/host_heartbeat_fail_closed_v2.script"
        ],
        "required_in": [".script", ".urp"],
    }]
    _require_equal(blocks, expected_block, "watchdog reviewed block")

    current = _load_json(root / "config/current_stage.json", role="current selector")
    _require_equal(current.get("current_stage_id"), V1_STAGE_ID, "current v1 selector")
    _require_equal(current.get("program"), V1_STAGE_ID, "current v1 program")

    table = _load_json(root / "config/step5_stage_table.json", role="Step5 stage table")
    rows = [row for row in table.get("stages", []) if isinstance(row, dict)]
    matches = [row for row in rows if row.get("id") == V3_STAGE_ID]
    if len(matches) != 1:
        raise ArtifactVerificationError("inactive v3 selector row must exist exactly once")
    v3 = matches[0]
    for field, expected in (("active", False), ("blocked", True), ("bridge", False)):
        _require_equal(v3.get(field), expected, f"v3 selector {field}")
    binding = v3.get("current_binding") or {}
    _require_equal(binding.get("is_current"), False, "v3 current binding")
    _require_equal(binding.get("live_authorized"), False, "v3 live authorization")
    package = v3.get("package_delivery") or {}
    _require_equal(package.get("controller_uploaded_by_v3"), False, "v3 upload claim")
    _require_equal(package.get("tp_fingerprint"), TP_FINGERPRINT, "v3 TP binding")
    _require_equal(package.get("sha256"), TRIPLET_SHA256, "v3 triplet binding")
    migration = v3.get("migration") or {}
    _require_equal(migration.get("ledger_sha256"), LEDGER_SHA256, "v3 ledger binding")
    _require_equal(
        migration.get("pending_or_automatic_retry_imported"),
        False,
        "v3 migration retry policy",
    )

    g10 = _load_json(
        root / "config/step5/golden_replay_g10_v3_result.json",
        role="G10 formal replay result",
    )
    _require_equal(g10.get("ok"), True, "G10 formal replay result")
    _require_equal(g10.get("no_motion"), True, "G10 no-motion boundary")
    _require_equal(g10.get("group_id"), "G10", "G10 group identity")
    metrics = g10.get("executable_replay") or {}
    if not isinstance(metrics, dict):
        raise ArtifactVerificationError("G10 formal replay metrics are missing")
    if any(
        (
            metrics.get("structural_failure_rows") != 0,
            float(metrics.get("rnn_oracle_qdot_delta_max_rad_s", math.inf)) > 1e-6,
            float(metrics.get("qdot_max_abs_rad_s", math.inf)) > 0.5,
            float(metrics.get("slew_violation_max_rad_s", math.inf)) > 1e-12,
        )
    ):
        raise ArtifactVerificationError("G10 formal replay thresholds differ")

    validation = _load_json(
        root / "config/step5/step5d_autotune_v3_offline_validation.json",
        role="offline validation decision",
    )
    decision = validation.get("decision") or {}
    _require_equal(decision.get("go_no_go"), "go", "v3 offline decision")
    _require_equal(
        decision.get("acceptance_scope"),
        "offline_tooling_and_ursim_hold_only",
        "v3 acceptance scope",
    )
    _require_equal(decision.get("rollout_authorized"), False, "v3 rollout authorization")
    _require_equal(decision.get("current_selector"), V1_STAGE_ID, "rollback selector")
    _require_equal(decision.get("v3_active"), False, "v3 inactive decision")

    ursim = _load_json(
        root / "config/step5/step5d_autotune_v3_ursim_hold_result.json",
        role="immutable URSim HOLD result",
    )
    _require_equal(ursim.get("status"), "pass", "URSim HOLD result")
    _require_equal(
        ursim.get("acceptance_scope"),
        "offline_tooling_and_ursim_hold_only",
        "URSim acceptance scope",
    )
    identity = ursim.get("identity") or {}
    validation_identity = validation.get("identity") or {}
    for field in ("control_fingerprint", "orchestration_fingerprint", "contract_sha256"):
        _require_equal(
            identity.get(field),
            validation_identity.get(field),
            f"URSim validation identity {field}",
        )
    image = ursim.get("image") or {}
    _require_equal(image.get("reference"), URSIM_IMAGE, "URSim image digest")
    _require_equal(image.get("entrypoint"), ["/entrypoint.sh"], "URSim entrypoint")
    _require_equal(image.get("entrypoint_overridden"), False, "URSim entrypoint policy")
    _require_equal(image.get("robot_model"), "UR10", "URSim robot model")
    network = ursim.get("network") or {}
    for field, expected in (
        ("internal", True),
        ("external_network_connected", False),
        ("robot_network_connected", False),
        ("host_ports_published", False),
        ("removed_after_gate", True),
    ):
        _require_equal(network.get(field), expected, f"URSim network {field}")
    container = ursim.get("container_lifecycle") or {}
    for field, expected in (
        ("privileged", False),
        ("mounts", []),
        ("devices", []),
        ("removed_after_gate", True),
    ):
        _require_equal(container.get(field), expected, f"URSim container {field}")
    lifecycle = ursim.get("service_lifecycle") or {}
    _require_equal(
        (lifecycle.get("observed") or [])[:3],
        ["STOPPED", "STARTING", "READY_HOME"],
        "URSim service lifecycle prefix",
    )
    for field in ("hardware_enabled", "bridge_started", "controller_touched", "tp_started"):
        _require_equal(lifecycle.get(field), False, f"URSim service {field}")
    hold = ursim.get("hold") or {}
    _require_equal(hold.get("dashboard_program_state"), "STOPPED", "URSim program state")
    _require_equal(hold.get("dashboard_running"), False, "URSim running state")
    _require_equal(hold.get("rtde_output_recipe_only"), True, "URSim RTDE recipe")
    _require_equal(hold.get("rtde_input_recipe_created"), False, "URSim RTDE input recipe")
    for field in (
        "max_abs_actual_qd_rad_s",
        "max_abs_actual_tcp_speed",
        "play_count",
        "arm_count",
        "motion_count",
        "controller_write_count",
        "forbidden_action_count",
    ):
        _require_equal(hold.get(field), 0, f"URSim HOLD {field}")
    watchdog_result = ursim.get("watchdog") or {}
    _require_equal(watchdog_result.get("hold_monitor_passed"), True, "URSim watchdog")
    _require_equal(
        watchdog_result.get("immutable_tp_watchdog_artifact_verified"),
        True,
        "URSim immutable TP watchdog",
    )
    rollback = ursim.get("rollback") or {}
    _require_equal(rollback.get("service_exit_code"), 0, "URSim service rollback exit")
    _require_equal(rollback.get("service_final_phase"), "stopped", "URSim rollback phase")
    _require_equal(rollback.get("v1_selector_before"), V1_STAGE_ID, "URSim rollback before")
    _require_equal(rollback.get("v1_selector_after"), V1_STAGE_ID, "URSim rollback after")
    _require_equal(rollback.get("v3_active_after"), False, "URSim rollback v3 state")
    raw = ursim.get("raw_evidence") or {}
    _require_equal(raw.get("sha256"), URSIM_RAW_SHA256, "URSim raw evidence binding")
    _require_equal(raw.get("byte_identical_copy_verified"), True, "URSim raw evidence copy")
    ursim_decision = ursim.get("decision") or {}
    _require_equal(ursim_decision.get("offline_acceptance"), "go", "URSim offline decision")
    _require_equal(
        ursim_decision.get("live_robot_acceptance"),
        "not_run_not_authorized",
        "URSim live boundary",
    )
    _require_equal(ursim_decision.get("rollout_authorized"), False, "URSim rollout")
    _require_equal(ursim_decision.get("v3_active"), False, "URSim v3 activity")
    _require_equal(ursim_decision.get("current_selector"), V1_STAGE_ID, "URSim selector")
    ursim_gate = (validation.get("gates") or {}).get("ursim_hold_only") or {}
    _require_equal(ursim_gate.get("status"), "pass", "offline URSim gate")
    _require_equal(ursim_gate.get("result_sha256"), URSIM_RESULT_SHA256, "URSim result binding")

    matrix = _load_json(
        root / "config/step5d_autotune_v3_test_matrix.json",
        role="Step5d v3 test matrix",
    )
    _require_equal(
        matrix.get("claim_boundary"),
        "offline_tooling_and_ursim_hold_only",
        "test matrix claim boundary",
    )
    large = (matrix.get("lanes") or {}).get("large_ursim") or {}
    _require_equal(large.get("execution_status"), "pass", "large URSim lane status")
    _require_equal(large.get("container_image"), URSIM_IMAGE, "large URSim image")
    _require_equal(large.get("immutable_result_sha256"), URSIM_RESULT_SHA256, "large URSim result")
    _require_equal(large.get("raw_evidence_sha256"), URSIM_RAW_SHA256, "large URSim raw evidence")
    for field, expected in (
        ("external_network_allowed", False),
        ("robot_network_allowed", False),
        ("host_port_publication_allowed", False),
        ("rtde_input_recipe_allowed", False),
        ("controller_access_allowed", False),
        ("live_writer_allowed", False),
        ("arm_allowed", False),
        ("motion_allowed", False),
        ("cleanup_completed", True),
    ):
        _require_equal(large.get(field), expected, f"large URSim {field}")

    fingerprint_input = json.dumps(
        {
            "tp_fingerprint": TP_FINGERPRINT,
            "ledger_sha256": LEDGER_SHA256,
            "immutable_artifacts": EXPECTED_SHA256,
        },
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return {
        "schema": "step5d.autotune-v3/immutable-artifact-report-v1",
        "ok": True,
        "current_stage_id": V1_STAGE_ID,
        "v3_stage_id": V3_STAGE_ID,
        "v3_active": False,
        "acceptance_scope": "offline_tooling_and_ursim_hold_only",
        "rollout_authorized": False,
        "live_robot_acceptance": "not_run_not_authorized",
        "ursim_result_sha256": URSIM_RESULT_SHA256,
        "tp_fingerprint": TP_FINGERPRINT,
        "attempt_ledger_sha256": LEDGER_SHA256,
        "artifact_set_fingerprint": hashlib.sha256(fingerprint_input).hexdigest(),
        "verified_paths": sorted(EXPECTED_SHA256),
    }


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--json", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    report = verify(args.root)
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print("step5d_autotune_v3_artifacts=pass")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
