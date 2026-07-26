#!/usr/bin/env python3
"""Build the exact 1/10/100 offline production-composition certificate."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from typing import Any, Mapping


ROOT = Path(__file__).resolve().parents[1]
TOOLS = ROOT / "tools"
RUNTIME_SRC = ROOT.parents[1] / "src" / "ur10e_experiment_runtime"
RUNNER = TOOLS / "run_step5d_parameter_campaign.py"
ADAPTER = TOOLS / "run_step5d_parameter_campaign_offline_certificate.py"
BRIDGE = TOOLS / "step5d_offline_composition_bridge.py"
QUEUE = TOOLS / "step5d_parameter_queue.py"
MAILBOX = TOOLS / "step5d_autotune_live_driver.py"
CSV = TOOLS / "step5d_production_csv.py"
LAUNCH_PROFILE = ROOT / "config/step5/step5d_autotune_v3_launch_profile.json"
ALLOWED_OUTPUT_PARENT = ROOT / "runs/step5d_autotune_v3/offline-composition-runs"
COUNTS = (1, 10, 100)
SCHEMA = "step5d.autotune-v3/production-composition-certificate-v1"

sys.path.insert(0, str(RUNTIME_SRC))
sys.path.insert(0, str(TOOLS))

from step5d_autotune_v3.release_certificate import (  # noqa: E402
    load_release_certificate,
)
from step5d_autotune_v3.release_contract import (  # noqa: E402
    release_contract_scope_for_release,
)
from step5d_autotune_v3.release_identity import (  # noqa: E402
    load_local_release_candidate,
)
from step5d_parameter_queue import (  # noqa: E402
    initialize,
    status as receiver_status,
    submit_manifest,
)


class ProductionCompositionError(RuntimeError):
    pass


def _canonical_bytes(value: Mapping[str, Any]) -> bytes:
    return (
        json.dumps(
            dict(value),
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")


def _sha256_file(path: Path) -> str:
    if path.is_symlink() or not path.is_file():
        raise ProductionCompositionError(f"bound source is missing or unsafe: {path}")
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_once(path: Path, payload: Mapping[str, Any]) -> None:
    encoded = _canonical_bytes(payload)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.exists() or path.is_symlink():
        if path.is_symlink() or path.read_bytes() != encoded:
            raise ProductionCompositionError(f"immutable output differs: {path}")
        return
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def _new_output_root(path: Path) -> Path:
    allowed = ALLOWED_OUTPUT_PARENT.resolve()
    unresolved = path.expanduser()
    if unresolved.exists() or unresolved.is_symlink():
        raise ProductionCompositionError("output root must be a new path")
    resolved = unresolved.resolve()
    try:
        resolved.relative_to(allowed)
    except ValueError as exc:
        raise ProductionCompositionError(
            "output root must stay inside offline-composition-runs"
        ) from exc
    resolved.mkdir(parents=True, mode=0o700)
    return resolved


def _environment() -> dict[str, str]:
    environment = dict(os.environ)
    environment["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
    environment["PYTHONPATH"] = os.pathsep.join(
        (str(RUNTIME_SRC), str(TOOLS), environment.get("PYTHONPATH", ""))
    ).rstrip(os.pathsep)
    return environment


def _wait_file(
    path: Path,
    process: subprocess.Popen[str],
    role: str,
    timeout_s: float = 30.0,
) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if path.is_file() and not path.is_symlink():
            return
        if process.poll() is not None:
            stdout, stderr = process.communicate()
            raise ProductionCompositionError(
                f"{role} process exited rc={process.returncode}: {stdout} {stderr}"
            )
        time.sleep(0.01)
    raise ProductionCompositionError(f"timed out waiting for {role}")


def _binding(release: Any, count: int) -> dict[str, Any]:
    identity = {
        "release_manifest_sha256": release.manifest_sha256,
        "trial_count": count,
        "claim_class": "offline_no_motion_production_composition",
    }
    fingerprint = hashlib.sha256(_canonical_bytes(identity)).hexdigest()
    return {
        "schema": "step5d.offline-no-motion/campaign-binding-v1",
        "campaign_id": f"offline-composition-{release.manifest_sha256[:12]}-{count}",
        "campaign_epoch": 1,
        "campaign_fingerprint": fingerprint,
        **identity,
    }


def _run_lane(
    *,
    output_root: Path,
    release: Any,
    candidate_path: Path,
    certificate_path: Path,
    count: int,
) -> dict[str, Any]:
    lane = output_root / f"trials-{count:03d}"
    lane.mkdir(mode=0o700)
    bridge_run = lane / "bridge"
    campaign_root = lane / "campaign"
    receiver_root = lane / "receiver"
    runtime_root = lane / "runtime"
    runtime_root.mkdir(mode=0o700)
    campaign_root.mkdir(mode=0o700)
    mailbox = runtime_root / "command.json"
    runner_ready = runtime_root / "runner-ready.json"
    binding_path = runtime_root / "campaign-binding.json"
    binding = _binding(release, count)
    _write_once(binding_path, binding)
    initialize(
        receiver_root,
        campaign_id=binding["campaign_id"],
        release_manifest_sha256=release.manifest_sha256,
        launch_profile_path=LAUNCH_PROFILE,
    )
    submit_manifest(
        receiver_root,
        launch_profile_path=LAUNCH_PROFILE,
        rows=tuple(
            {
                "force_p_gain": 0.0008408964152537145,
                "force_i_gain": 0.00001,
                "force_damping": 4.949747468305833,
                "orientation_ko": 0.4,
                "source": f"offline-composition-{count}-{index + 1}",
                "occurrence_nonce": f"{index + 1:032x}",
            }
            for index in range(count)
        ),
    )
    bridge_command = [
        sys.executable,
        str(BRIDGE),
        "--bridge-run",
        str(bridge_run),
        "--mailbox",
        str(mailbox),
        "--launch-profile",
        str(LAUNCH_PROFILE),
        "--runner-ready",
        str(runner_ready),
        "--runner-status",
        str(campaign_root / "parameter_receiver_status.json"),
        "--trial-count",
        str(count),
    ]
    bridge = subprocess.Popen(
        bridge_command,
        cwd=ROOT,
        env=_environment(),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    runner_command = [
        sys.executable,
        str(ADAPTER),
        "--lane-root",
        str(lane),
        "--release-candidate",
        str(candidate_path),
        "--release-certificate",
        str(certificate_path),
        "--trial-count",
        str(count),
        "--experiment-root",
        str(ROOT),
        "--bridge-run",
        str(bridge_run),
        "--campaign-root",
        str(campaign_root),
        "--receiver-root",
        str(receiver_root),
        "--mailbox",
        str(mailbox),
        "--runner-ready-file",
        str(runner_ready),
        "--campaign-binding",
        str(binding_path),
        "--campaign-lease",
        str(runtime_root / "unused-lease.json"),
        "--release-manifest-sha256",
        release.manifest_sha256,
        "--v3-launch-profile",
        str(LAUNCH_PROFILE),
        "--v3-program-id",
        release.program_id,
        "--launch-basis",
        str(runtime_root / "unused-launch-basis.json"),
        "--launch-basis-sha256",
        "0" * 64,
        "--delivery-observation",
        str(runtime_root / "unused-delivery.json"),
        "--campaign-prepare",
        str(runtime_root / "unused-campaign-prepare.json"),
        "--admission",
        str(runtime_root / "unused-admission.json"),
        "--canonical-owner-pid",
        "0",
        "--canonical-owner-starttime",
        "0",
        "--authority-epoch",
        "0",
    ]
    try:
        _wait_file(bridge_run / "bridge_ready.json", bridge, "offline bridge readiness")
        runner = subprocess.run(
            runner_command,
            cwd=ROOT,
            env=_environment(),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=max(120.0, count * 3.0),
            check=False,
        )
        bridge_stdout, bridge_stderr = bridge.communicate(timeout=30.0)
        if runner.returncode != 0 or bridge.returncode != 0:
            raise ProductionCompositionError(
                f"lane {count} failed runner={runner.returncode} bridge={bridge.returncode}; "
                f"runner stderr={runner.stderr}; bridge stdout={bridge_stdout}; "
                f"bridge stderr={bridge_stderr}"
            )
    finally:
        if bridge.poll() is None:
            bridge.terminate()
            bridge.wait(timeout=5.0)
    state = receiver_status(receiver_root)
    receipts = sorted((receiver_root / "receipts").glob("*.json"))
    bridge_stats = json.loads(
        (bridge_run / "offline_bridge_stats.json").read_text(encoding="utf-8")
    )
    if (
        state["attempted_count"] != count
        or state["pending_count"] != 0
        or state["inflight"] is not None
        or len(receipts) != count
        or bridge_stats["arm_command_sequences"] != list(range(1, count + 1))
    ):
        raise ProductionCompositionError(
            f"lane {count} did not close exact composition: {state}"
        )
    return {
        "trial_count": count,
        "attempted_count": state["attempted_count"],
        "receipt_count": len(receipts),
        "succeeded_count": sum(
            json.loads(path.read_text(encoding="utf-8"))["status"] == "SUCCEEDED"
            for path in receipts
        ),
        "arm_command_sequences_sha256": hashlib.sha256(
            _canonical_bytes({"sequences": bridge_stats["arm_command_sequences"]})
        ).hexdigest(),
        "controller_io": False,
        "live_authorization": False,
    }


def run(
    candidate_path: Path,
    certificate_path: Path,
    *,
    output_root: Path,
) -> dict[str, Any]:
    release, descriptor = load_local_release_candidate(ROOT, candidate_path)
    expected_scope = release_contract_scope_for_release(ROOT, release)
    release_certificate, evidence_path, _evidence = load_release_certificate(
        ROOT / "runs/step5d_autotune_v3",
        certificate_path,
        expected_scope=expected_scope,
    )
    root = _new_output_root(output_root)
    bindings = {
        path.relative_to(ROOT).as_posix(): _sha256_file(path)
        for path in (RUNNER, QUEUE, MAILBOX, CSV, ADAPTER, BRIDGE)
    }
    lanes = tuple(
        _run_lane(
            output_root=root,
            release=release,
            candidate_path=candidate_path.resolve(strict=True),
            certificate_path=certificate_path.resolve(strict=True),
            count=count,
        )
        for count in COUNTS
    )
    core = {
        "schema": SCHEMA,
        "claim_class": "offline_no_motion_production_composition",
        "release": {
            "program_id": release.program_id,
            "manifest_sha256": release.manifest_sha256,
            "candidate_path": str(candidate_path.resolve(strict=True)),
            "candidate_descriptor_sha256": hashlib.sha256(
                _canonical_bytes(descriptor)
            ).hexdigest(),
            "release_certificate_path": str(certificate_path.resolve(strict=True)),
            "release_certificate_sha256": _sha256_file(certificate_path),
            "contract_evidence_path": str(evidence_path),
            "contract_evidence_sha256": release_certificate["contract_evidence"][
                "sha256"
            ],
        },
        "trial_counts": list(COUNTS),
        "lanes": list(lanes),
        "source_bindings": bindings,
        "adapter_boundaries": [
            "_validate_authority",
            "_validate_launch_identity",
            "_wait_next_dispatch_queue_exhaustion",
        ],
        "production_send_unchanged": True,
        "production_run_trial_unchanged": True,
        "controller_io": False,
        "network_started": False,
        "live_authorization": False,
        "motion_capable": False,
    }
    certificate_id = hashlib.sha256(_canonical_bytes(core)).hexdigest()
    payload = {"certificate_id": certificate_id, **core}
    path = root / "production-composition-certificate.json"
    _write_once(path, payload)
    return {**payload, "certificate_path": str(path)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidate", type=Path, required=True)
    parser.add_argument("--release-certificate", type=Path, required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    args = parser.parse_args()
    try:
        result = run(
            args.candidate,
            args.release_certificate,
            output_root=args.output_root,
        )
    except Exception as exc:
        print(
            f"production composition blocked: {type(exc).__name__}:{exc}",
            file=sys.stderr,
        )
        return 2
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
