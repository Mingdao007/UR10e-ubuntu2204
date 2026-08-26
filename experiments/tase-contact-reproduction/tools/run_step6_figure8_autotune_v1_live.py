#!/usr/bin/env python3
"""Fail-closed Step6 Figure-eight campaign and live-owner entrypoint.

Validation and dry-run modes never discover a writer.  ``--calibrate-home``
retains the geometry-only acquisition owner.  ``--execute-live`` runs the V5
PRIMARY then isolated CORRECTION bundle through the resident single writer,
only after immutable package/read-back/canary/Home gates cold-read.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import sys
from typing import Any, Mapping, Protocol, Sequence

ROOT = Path(__file__).resolve().parents[1]
STEP6_RUNTIME_MANIFEST = ROOT / "config" / "step6" / "step6_figure8_runtime_manifest.json"
STEP6_RUNTIME_LINEAGE = "step6_figure8_autotuner_v5_layout607_v1"
STEP6_ENTRYPOINT_MODULE = Path(__file__).stem
STEP6_OFFLINE_ACTIONS = frozenset({"--validate", "--dry-run", "--status"})
STEP6_LIVE_ACTIONS = frozenset(
    {"--calibrate-home", "--probe-recipe", "--capability-only", "--execute-live"}
)
V5_RECOVERABLE_CONTINUATION_MARKER = "V5_RECOVERABLE_CONTINUATION_V1"


def _side_effect_level(argv: Sequence[str]) -> str:
    values = set(argv)
    if values & STEP6_LIVE_ACTIONS:
        return "live_mutation"
    if values & STEP6_OFFLINE_ACTIONS:
        return "offline"
    return "live_mutation"


def _runtime_request(argv: Sequence[str] = ()) -> Any:
    from step5d_managed_runtime import ExecutionRequestV1

    return ExecutionRequestV1(
        schema="step5d.execution-admission/request-v1",
        entrypoint=STEP6_ENTRYPOINT_MODULE,
        manifest_path=STEP6_RUNTIME_MANIFEST,
        route="host",
        required_imports=("numpy", "scipy", "pinocchio"),
        side_effect_level=_side_effect_level(argv),
        source_or_lineage_identity=STEP6_RUNTIME_LINEAGE,
    )


def _runtime_failure(receipt: Any) -> int:
    print(
        json.dumps(
            {"ok": False, "error": receipt.as_dict(), "live_executed": False},
            indent=2,
            sort_keys=True,
        ),
        file=sys.stderr,
    )
    return 2


def _bootstrap_managed_runtime(argv: Sequence[str]) -> int:
    from step5d_managed_runtime import admit_control_runtime, launch_manifest_route

    receipt = admit_control_runtime(_runtime_request(argv), root=ROOT)
    if receipt.status != "admitted":
        return _runtime_failure(receipt)
    child_launch_attempted = False
    try:
        child_launch_attempted = True
        return launch_manifest_route(
            STEP6_RUNTIME_MANIFEST,
            ("host", *tuple(argv)),
            root=ROOT,
            side_effect_level=_side_effect_level(argv),
            source_or_lineage_identity=STEP6_RUNTIME_LINEAGE,
        )
    except Exception as exc:
        print(
            json.dumps(
                {
                    "ok": False,
                    "error": {
                        "schema": "step5d.execution-admission/runtime-receipt-v1",
                        "status": "invalid",
                        "reason_code": getattr(exc, "reason_code", "CONTROL_RUNTIME_INVALID"),
                        "detail": str(exc),
                    },
                    # Once launch_manifest_route is entered, the managed
                    # child may have loaded/played and opened the owner even
                    # if the parent receives a timeout/transport exception.
                    # Do not publish a false negative; downstream recovery
                    # must inspect the state root and controller receipt.
                    "live_executed": (
                        "unknown_after_child_launch"
                        if child_launch_attempted
                        else False
                    ),
                },
                indent=2,
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 2


def _verify_managed_child_runtime() -> None:
    from step5d_managed_runtime import admit_control_runtime

    receipt = admit_control_runtime(_runtime_request(sys.argv[1:]), root=ROOT)
    if receipt.status != "admitted":
        raise SystemExit(_runtime_failure(receipt))
    os.environ["STEP5D_EXECUTION_ADMISSION_RECEIPT"] = json.dumps(
        receipt.as_dict(), sort_keys=True, separators=(",", ":")
    )
    startup_path = os.environ.get("STEP5D_MANAGED_STARTUP_RECEIPT")
    if startup_path:
        destination = Path(startup_path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        body = {
            "schema": "step5d.execution-admission/startup-receipt-v1",
            "version": 1,
            "status": "READY",
            "pid": os.getpid(),
            "entrypoint": STEP6_ENTRYPOINT_MODULE,
            "execution_admission_receipt": receipt.as_dict(),
        }
        temporary = destination.with_suffix(destination.suffix + ".part")
        temporary.write_text(
            json.dumps(body, sort_keys=True, separators=(",", ":")) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, destination)


def _print_fast_help() -> None:
    """Print CLI help without resolving the managed runtime attestation."""

    print(
        f"{__doc__}\n\n"
        "usage: run_step6_figure8_autotune_v1_live.py "
        "[--validate|--dry-run|--status|--calibrate-home|--probe-recipe|--capability-only|--execute-live] "
        "[options]\n\n"
        "Managed runtime attestation is intentionally skipped for --help."
    )


if __name__ == "__main__":
    if any(value in {"-h", "--help"} for value in sys.argv[1:]):
        _print_fast_help()
        raise SystemExit(0)
    if os.environ.get("STEP5D_EXECUTION_ADMISSION") != "admitted":
        raise SystemExit(_bootstrap_managed_runtime(sys.argv[1:]))
    _verify_managed_child_runtime()


try:
    from build_step6_figure8_autotune_v1 import (
        FIXED_STAMP,
        NUMERIC_SANITY_PATH,
        PROGRAM_NAME,
        CONTROLLER_DIR,
        build_input_sha256,
        numeric_sanity,
        validate_package,
    )
    from step6_figure8_autotune_v1 import (
        CampaignConfigV1,
        CompleteCandidateV1,
        FigureEightLaunchReceiptV1,
        FigureEightCampaignFingerprintV1,
        FigureEightError,
        FigureEightSchedulerV1,
        LaunchReceiptV1,
        OfflineCampaignV1,
        PersistedSobolCursorV1,
        StrictAdmissionV1,
        ProductionProposalUnavailable,
        TrialEvidenceV1,
        build_campaign_fingerprint,
        load_campaign_config,
        make_metric_result,
    )
except ModuleNotFoundError:  # pragma: no cover - import from repository root
    from tools.build_step6_figure8_autotune_v1 import (
        FIXED_STAMP,
        NUMERIC_SANITY_PATH,
        PROGRAM_NAME,
        CONTROLLER_DIR,
        build_input_sha256,
        numeric_sanity,
        validate_package,
    )
    from tools.step6_figure8_autotune_v1 import (
        CampaignConfigV1,
        CompleteCandidateV1,
        FigureEightLaunchReceiptV1,
        FigureEightCampaignFingerprintV1,
        FigureEightError,
        FigureEightSchedulerV1,
        LaunchReceiptV1,
        OfflineCampaignV1,
        PersistedSobolCursorV1,
        StrictAdmissionV1,
        ProductionProposalUnavailable,
        TrialEvidenceV1,
        build_campaign_fingerprint,
        load_campaign_config,
        make_metric_result,
    )

try:
    from step5d_autotune_v4_r013.live_owner import R013PathProfileV1
except ModuleNotFoundError:  # pragma: no cover - import from repository root
    from tools.step5d_autotune_v4_r013.live_owner import R013PathProfileV1
try:
    from step5d_autotune_v4_r013.timing_scheduler import (
        LATE_CONTROL_FIFO_PROFILE,
        QUOTA_SAFE_OTHER_PROFILE,
    )
except ModuleNotFoundError:  # pragma: no cover - import from repository root
    from tools.step5d_autotune_v4_r013.timing_scheduler import (
        LATE_CONTROL_FIFO_PROFILE,
        QUOTA_SAFE_OTHER_PROFILE,
    )

try:
    from ur10e_parallel import (
        ResourceProfile,
        formal_timing_lease,
        formal_timing_owner,
        notify_formal_timing_owner,
    )
except ModuleNotFoundError:  # pragma: no cover - import from repository root
    from tools.ur10e_parallel import (
        ResourceProfile,
        formal_timing_lease,
        formal_timing_owner,
        notify_formal_timing_owner,
    )

DEFAULT_STATE_DIR = ROOT / "runs" / "step6_figure8_autotune_v1_offline"
DEFAULT_CONFIG = ROOT / "config" / "step6" / "r013_figure8_direct_campaign_v1.json"
DEFAULT_V5_CAMPAIGN_CONFIG = (
    ROOT / "config" / "step6" / "autotuner_v5_campaign_v2.json"
)


@contextmanager
def _v5_formal_timing_lease(state_dir: Path):
    """Hold the shared formal-timing resource for the entire live bundle."""

    profile = ResourceProfile.from_env()
    lease = formal_timing_lease(
        profile,
        task=f"autotuner-v5-live:{Path(state_dir).resolve()}",
        blocking=False,
    )
    try:
        owner = lease.__enter__()
    except BlockingIOError as exc:
        current_owner = formal_timing_owner(profile)
        notice_sent = notify_formal_timing_owner(
            current_owner,
            f"Autotuner V5 live bundle is blocked by formal_timing: {Path(state_dir).resolve()}",
        )
        raise FigureEightError(
            "formal_timing resource is busy; "
            f"owner={current_owner!r}; notice_sent={notice_sent}; no live retry"
        ) from exc
    try:
        yield owner
    finally:
        lease.__exit__(None, None, None)


def _discover_default_readback_dir() -> Path:
    """Select the newest read-back whose local SHA set matches this triplet."""

    local_dir = ROOT / "programs" / "step6"
    expected = {
        f".{role}": __import__("hashlib").sha256(
            (local_dir / f"step6_figure8_autotune_v1.{role}").read_bytes()
        ).hexdigest()
        for role in ("script", "txt", "urp")
    }
    candidates = sorted(
        (
            path
            for path in ROOT.glob(
                "runs/controller_readback_step6_figure8_autotune_v*_20260816/"
                "controller_readback_step6_figure8_autotune_v1_*"
            )
            if path.is_dir()
        ),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    for candidate in candidates:
        try:
            manifest = json.loads((candidate / "manifest.json").read_text(encoding="utf-8"))
            local_hashes = manifest["sha256"]["local"]
            if all(local_hashes.get(ext) == digest for ext, digest in expected.items()):
                return candidate
        except (OSError, ValueError, KeyError, TypeError):
            continue
    return candidates[0] if candidates else ROOT / "runs" / "controller_readback_pending"


DEFAULT_READBACK_DIR = _discover_default_readback_dir()
DEFAULT_CANARY_DIR = ROOT / "runs" / "step6_figure8_no_contact_canary_v1_20260816_065032"


class CandidateWriter(Protocol):
    """Later live owner seam; no implementation is provided by this entrypoint."""

    def home(self) -> None: ...
    def arm_apply(self, candidate: Mapping[str, Any]) -> None: ...
    def path_60s(self, candidate: Mapping[str, Any]) -> Sequence[Mapping[str, Any]]: ...
    def seal(self, samples: Sequence[Mapping[str, Any]]) -> Any: ...
    def release_ownership(self, reason: str) -> None: ...


class SafetyFaultError(RuntimeError):
    """Latched physical fault: evidence/release only, never retry or Home."""


@dataclass
class FakeWriter:
    """Deterministic dry-run writer; it never connects to a robot or bridge."""

    complete: bool = True
    events: list[str] = field(default_factory=list)

    def home(self) -> None:
        self.events.append("Home")

    def arm_apply(self, candidate: Mapping[str, Any]) -> None:
        self.events.append("ARM/apply")

    def path_60s(self, candidate: Mapping[str, Any]) -> Sequence[Mapping[str, Any]]:
        self.events.append("60 s PATH")
        limit = 600 if self.complete else 599
        return tuple(
            {"time_s": round(index * 0.1, 10), "normal_load_n": 5.0}
            for index in range(limit)
        )

    def seal(self, samples: Sequence[Mapping[str, Any]]) -> Any:
        self.events.append("seal")
        return make_metric_result(samples)

    def release_ownership(self, reason: str) -> None:
        self.events.append(f"release:{reason}")


@dataclass
class MatureR013WriterAdapter:
    """Typed adapter; a live owner must supply the mature R013 context."""

    owner: Any
    profile: R013PathProfileV1 = field(default_factory=R013PathProfileV1.figure8)

    def _method(self, name: str) -> Any:
        method = getattr(self.owner, name, None)
        if not callable(method):
            raise FigureEightError(f"mature R013 owner lacks typed {name} seam")
        return method

    def home(self) -> None:
        self._method("home")()

    def arm_apply(self, candidate: Mapping[str, Any]) -> None:
        CompleteCandidateV1.from_mapping(candidate)
        self._method("arm_apply")(candidate)

    def path_60s(self, candidate: Mapping[str, Any]) -> Sequence[Mapping[str, Any]]:
        CompleteCandidateV1.from_mapping(candidate)
        return self._method("run_profile")(candidate, self.profile)

    def seal(self, samples: Sequence[Mapping[str, Any]]) -> Any:
        return self._method("seal_raw")(samples, self.profile)

    def recoverable_home(self) -> None:
        self._method("recoverable_home")()

    def release_ownership(self, reason: str) -> None:
        self._method("release_ownership")(reason)


def build_mature_writer_adapter(owner: Any) -> MatureR013WriterAdapter:
    """Bind a later R013 owner without discovering or opening live services."""

    if owner is None:
        raise FigureEightError("Step6 mature R013 owner is required for the live adapter")
    return MatureR013WriterAdapter(owner=owner, profile=R013PathProfileV1.figure8())


def orchestration_gate_status() -> dict[str, Any]:
    """Describe the guarded live sequence without executing any gate."""

    return {
        "sequence": [
            "geometry_only_home_receipt_cold_binding",
            "main_and_canary_same_round_upload_readback_byte_equality",
            "layout607_no_motion_recipe_probe",
            "fresh_60s_no_contact_protocol_canary",
            "same_candidate_single_rollover",
            "bounded_neighbor_single_rollover",
            "home_vs_rollover_same_candidate_five_pair_ci90",
            "four_rollover_chain_then_verified_home",
            "new_primary_fingerprint_exact_novel_200_with_censor_refill",
            "primary_top_three_total_n5",
            "isolated_correction_fingerprint_exact_novel_60",
            "matched_zero_corrected_n5_each_no_auto_promotion",
            "final_home_writer_release_and_html_report",
        ],
        "campaign_qualification": False,
        "epoch_qualification": False,
        "three_contact_qualification": False,
        "narrow_force_windows_blocking": False,
        "completed": [],
        "live_executed": False,
        "takeover_required": "typed_mature_r013_owner_and_immutable_gate_artifacts",
    }


def _package_paths() -> dict[str, Path]:
    return {suffix: ROOT / "programs" / "step6" / f"{PROGRAM_NAME}.{suffix}" for suffix in ("script", "txt", "urp")}


def _sha256(path: Path) -> str:
    import hashlib
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _load_home_materialization(
    home_calibration_receipt: Path | None,
) -> tuple[dict[str, Any] | None, tuple[float, ...], str | None]:
    if home_calibration_receipt is None:
        sanity = numeric_sanity()
        return None, tuple(float(value) for value in sanity["path"]["anchor_pose"]), None
    try:
        from step6_figure8_autotune_v1.live_composition import (
            load_figure8_home_calibration_receipt,
        )
    except ModuleNotFoundError:  # pragma: no cover - repository-root import
        from tools.step6_figure8_autotune_v1.live_composition import (
            load_figure8_home_calibration_receipt,
        )
    receipt = load_figure8_home_calibration_receipt(home_calibration_receipt)
    return (
        dict(receipt),
        tuple(float(value) for value in receipt["final_home_pose"]),
        str(receipt["receipt_sha256"]),
    )


def validate_offline_surfaces(
    config_path: Path = DEFAULT_CONFIG,
    *,
    controller_readback_dir: Path = DEFAULT_READBACK_DIR,
    canary_dir: Path = DEFAULT_CANARY_DIR,
    home_calibration_receipt: Path | None = None,
) -> dict[str, Any]:
    config = load_campaign_config(config_path)
    home_materialization, home_pose, home_receipt_sha = _load_home_materialization(
        home_calibration_receipt
    )
    paths = _package_paths()
    if not all(path.is_file() for path in paths.values()):
        missing = [str(path) for path in paths.values() if not path.is_file()]
        raise FigureEightError(f"Step6 local package triplet is incomplete: {missing}")
    script = paths["script"].read_text(encoding="utf-8")
    txt = paths["txt"].read_text(encoding="utf-8")
    input_sha = build_input_sha256(
        home_calibration_receipt=home_calibration_receipt
    )
    checks = validate_package(
        script,
        txt,
        paths["urp"].read_bytes(),
        FIXED_STAMP,
        home_pose=home_pose,
        home_calibration_receipt_sha256=home_receipt_sha,
        input_sha256=input_sha,
    )
    expected_sanity = numeric_sanity(
        home_pose=home_pose,
        home_calibration_receipt_sha256=home_receipt_sha,
    )
    actual_sanity = json.loads(NUMERIC_SANITY_PATH.read_text(encoding="utf-8"))
    if actual_sanity != expected_sanity:
        raise FigureEightError("Step6 numeric-sanity JSON is not deterministic")
    source_sha = str(config.raw["controller"]["source_parent_sha256"])
    triplet_sha = {role: _sha256(path) for role, path in paths.items()}
    fingerprint = build_campaign_fingerprint(
        config=config,
        source_sha256=source_sha,
        controller_triplet_sha256=triplet_sha,
        handoff_policy=str(config.raw["handoff"]["selected"]),
        home_materialization=home_materialization,
    )
    controller_readback_dir = Path(controller_readback_dir).resolve()
    canary_dir = Path(canary_dir).resolve()
    readback_manifest = controller_readback_dir / "manifest.json"
    controller_readback = False
    controller_readback_error: str | None = None
    try:
        from step6_figure8_autotune_v1.prepare_live import verify_controller_readback

        _manifest, readback_triplet = verify_controller_readback(
            root=ROOT,
            readback_dir=controller_readback_dir,
        )
        controller_readback = readback_triplet == triplet_sha
        if not controller_readback:
            controller_readback_error = "read-back SHA differs from local triplet"
    except Exception as exc:  # status surface remains fail-closed and diagnostic
        controller_readback_error = f"{type(exc).__name__}: {exc}"
    no_contact_canary = False
    no_contact_canary_error: str | None = None
    try:
        canary = canary_dir
        evidence = json.loads((canary / "evidence_receipt_v2.json").read_text(encoding="utf-8"))
        frame = json.loads((canary / "frame_receipt_v2.json").read_text(encoding="utf-8"))
        raw_trace = canary / "raw_rtde_trace.jsonl"
        if (
            evidence.get("passed") is not True
            or evidence.get("contact_executed") is not False
            or evidence.get("campaign_live_acceptance") is not False
            or frame.get("live_canary_passed") is not True
            or frame.get("schema") != "step6.figure8/frozen-home-frame-v1"
            or tuple(float(value) for value in frame.get("home_pose", ())) != home_pose
            or frame.get("home_calibration_receipt_sha256") != home_receipt_sha
            or not raw_trace.is_file()
        ):
            raise FigureEightError("no-contact canary is not a passed no-contact artifact")
        no_contact_canary = True
    except Exception as exc:  # status surface remains fail-closed and diagnostic
        no_contact_canary_error = f"{type(exc).__name__}: {exc}"
    return {
        "config": str(config_path),
        "program": PROGRAM_NAME,
        "controller_dir": CONTROLLER_DIR,
        "triplet_sha256": triplet_sha,
        "campaign_fingerprint_sha256": fingerprint.sha256,
        "package_validation": checks,
        "numeric_sanity": str(NUMERIC_SANITY_PATH),
        "home_status": (
            "contact_derived_final_home"
            if home_materialization is not None
            else "calibration_only_pending_contact_confirmation"
        ),
        "home_pose": list(home_pose),
        "home_calibration_receipt_sha256": home_receipt_sha,
        "upload_performed": False,
        "controller_readback": controller_readback,
        "controller_readback_manifest": str(readback_manifest),
        "controller_readback_error": controller_readback_error,
        "no_contact_canary": no_contact_canary,
        "no_contact_canary_dir": str(canary_dir),
        "no_contact_canary_error": no_contact_canary_error,
        "live_evidence": False,
        "live_ready": False,
        "takeover_required": "mature_r013_package_composition_and_typed_live_receipts",
    }


def load_fingerprint(
    config: CampaignConfigV1,
    *,
    home_calibration_receipt: Path | None = None,
) -> FigureEightCampaignFingerprintV1:
    home_materialization, _home_pose, _home_receipt_sha = _load_home_materialization(
        home_calibration_receipt
    )
    paths = _package_paths()
    return build_campaign_fingerprint(
        config=config,
        source_sha256=str(config.raw["controller"]["source_parent_sha256"]),
        controller_triplet_sha256={role: _sha256(path) for role, path in paths.items()},
        handoff_policy=str(config.raw["handoff"]["selected"]),
        home_materialization=home_materialization,
    )


def require_typed_launch_receipt(path: Path, fingerprint: FigureEightCampaignFingerprintV1) -> LaunchReceiptV1:
    # Manual JSON, including all-true booleans, is intentionally not an
    # authorization path.  A later owner must call the verifier with all
    # immutable artifacts and a materialized matched-A/B fingerprint.
    raise FigureEightError(
        "Step6 launch receipt requires derive_figure8_launch_receipt from immutable evidence"
    )


def execute_candidate_transaction(
    *,
    writer: CandidateWriter,
    campaign: OfflineCampaignV1,
    scheduler: FigureEightSchedulerV1,
    fingerprint: FigureEightCampaignFingerprintV1,
    candidate: Mapping[str, Any],
    trial_id: str,
    epoch_id: str,
    motion_gate: bool = True,
    timing_gate: bool = True,
) -> Any:
    """Execute one supplied writer transaction in the fixed observable order."""
    typed_candidate = CompleteCandidateV1.from_mapping(candidate)
    samples: list[Mapping[str, Any]] = []
    safety_fault = False
    try:
        writer.home()
        writer.arm_apply(typed_candidate.as_dict())
        samples.extend(writer.path_60s(typed_candidate.as_dict()))
        metric_result = None
        try:
            metric_result = writer.seal(tuple(samples))
        except Exception:
            # Raw samples/gaps remain evidence; no interpolation or synthetic
            # seal is attempted.
            metric_result = None
        trial = TrialEvidenceV1(
            epoch_id=epoch_id,
            trial_id=trial_id,
            fingerprint_sha256=fingerprint.sha256,
            motion_gate=motion_gate,
            timing_gate=timing_gate,
            metric_result=metric_result,
            raw_samples=tuple(samples),
        )
        receipt = campaign.tell_exact(candidate=typed_candidate.as_dict(), trial=trial)
        if scheduler.in_flight is not None:
            scheduler.complete(
                scheduler.in_flight,
                accepted=receipt.strict_passed,
                mae_n=receipt.sealed_mae_n,
                fingerprint_sha256=fingerprint.sha256,
            )
        if hasattr(writer, "recoverable_home"):
            writer.recoverable_home()
        else:
            writer.home()
        return receipt
    except SafetyFaultError as exc:
        safety_fault = True
        # Preserve the partial raw evidence and release ownership.  There is
        # deliberately no retry and no automatic Home motion on a latched
        # protective/force/torque/sensor fault.
        try:
            trial = TrialEvidenceV1(
                epoch_id=epoch_id,
                trial_id=trial_id,
                fingerprint_sha256=fingerprint.sha256,
                motion_gate=False,
                timing_gate=False,
                metric_result=None,
                raw_samples=tuple(samples),
                evidence={"safety_fault": str(exc), "latched": True},
            )
            receipt = campaign.tell_exact(candidate=typed_candidate.as_dict(), trial=trial)
            if scheduler.in_flight is not None:
                scheduler.complete(scheduler.in_flight, accepted=False, failure_signature=f"safety_fault:{exc}")
        finally:
            writer.release_ownership(str(exc))
        raise
    except Exception as exc:
        # Ordinary pre-motion failures may be repaired by a later unchanged
        # fingerprint; this transaction records the failure and uses only the
        # writer's mature recoverable Home seam when available.
        if scheduler.in_flight is not None:
            scheduler.complete(scheduler.in_flight, accepted=False, failure_signature=f"ordinary_failure:{type(exc).__name__}")
        if not safety_fault and hasattr(writer, "recoverable_home"):
            writer.recoverable_home()
        raise


def dry_run(
    config_path: Path,
    state_dir: Path,
    *,
    controller_readback_dir: Path = DEFAULT_READBACK_DIR,
    canary_dir: Path = DEFAULT_CANARY_DIR,
    home_calibration_receipt: Path | None = None,
    incomplete: bool = False,
    test_mode: bool = False,
) -> dict[str, Any]:
    if not test_mode:
        raise FigureEightError("FakeWriter is available only with explicit test_mode=true")
    surfaces = validate_offline_surfaces(
        config_path,
        controller_readback_dir=controller_readback_dir,
        canary_dir=canary_dir,
        home_calibration_receipt=home_calibration_receipt,
    )
    config = load_campaign_config(config_path)
    fingerprint = load_fingerprint(
        config,
        home_calibration_receipt=home_calibration_receipt,
    )
    state_dir.mkdir(parents=True, exist_ok=True)
    cursor = PersistedSobolCursorV1(state_dir / "sobol_state.json")
    scheduler = FigureEightSchedulerV1(cursor, state_path=state_dir / "scheduler_state.json")
    plan = scheduler.ask()
    if plan is None:
        raise FigureEightError("Step6 dry-run did not produce a candidate")
    optimizer_calls: list[tuple[Mapping[str, Any], float, float]] = []

    class Optimizer:
        def tell_exact(self, candidate: Mapping[str, Any], mae_n: float, yvar_n2: float) -> None:
            optimizer_calls.append((candidate, mae_n, yvar_n2))

    campaign = OfflineCampaignV1(
        StrictAdmissionV1(fingerprint, config.metric),
        state_dir / "receipts.jsonl",
        optimizer=Optimizer(),
    )
    writer = FakeWriter(complete=not incomplete)
    receipt = execute_candidate_transaction(
        writer=writer,
        campaign=campaign,
        scheduler=scheduler,
        fingerprint=fingerprint,
        candidate=plan.candidate,
        trial_id="dry-run-trial-001",
        epoch_id="dry-run-segment-001",
    )
    state = {
        "schema": "step6.autotune/figure8-offline-campaign-state-v1",
        "version": 1,
        "stage_id": config.raw["stage_id"],
        "fingerprint_sha256": fingerprint.sha256,
        "scheduler": scheduler.snapshot(),
        "sobol": cursor.snapshot(),
        "last_admission": receipt.as_dict(),
        "optimizer_tell_exact_calls": len(optimizer_calls),
        "live_executed": False,
    }
    (state_dir / "campaign_state.json").write_text(json.dumps(state, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")
    return {"surfaces": surfaces, "transaction_events": writer.events, "admission": receipt.as_dict(), "state": str(state_dir / "campaign_state.json"), "live_executed": False}


def status(state_dir: Path) -> dict[str, Any]:
    bundle_marker = state_dir / "bundle_identity.json"
    if bundle_marker.is_file():
        roles: dict[str, Any] = {}
        for role in ("primary", "correction"):
            phase = state_dir / role / "phase_runtime.json"
            roles[role.upper()] = (
                None
                if not phase.is_file()
                else json.loads(phase.read_text(encoding="utf-8"))
            )
        final = state_dir / "final_receipt.json"
        return {
            "state_dir": str(state_dir.resolve()),
            "state_exists": True,
            "state_kind": "autotuner_v5_production_bundle",
            "bundle_identity": json.loads(bundle_marker.read_text(encoding="utf-8")),
            "roles": roles,
            "final_receipt": (
                None
                if not final.is_file()
                else json.loads(final.read_text(encoding="utf-8"))
            ),
            "orchestration": orchestration_gate_status(),
        }
    production_state_path = state_dir / "production_runner_state.json"
    dry_run_state_path = state_dir / "campaign_state.json"
    state_path = (
        production_state_path
        if production_state_path.is_file()
        else dry_run_state_path
    )
    return {
        "state_dir": str(state_dir),
        "state_exists": state_path.is_file(),
        "state": None if not state_path.is_file() else json.loads(state_path.read_text(encoding="utf-8")),
        "state_kind": (
            "production" if state_path == production_state_path else "offline_dry_run"
        ),
        "orchestration": orchestration_gate_status(),
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    surface = parser.add_mutually_exclusive_group(required=True)
    surface.add_argument("--validate", action="store_true")
    surface.add_argument("--dry-run", action="store_true")
    surface.add_argument("--status", action="store_true")
    surface.add_argument("--calibrate-home", action="store_true")
    surface.add_argument(
        "--probe-recipe",
        action="store_true",
        help="open the exact V5 RTDE recipes without sending an input packet",
    )
    surface.add_argument(
        "--capability-only",
        action="store_true",
        help="run the release-bound rollover capability gate without Primary/Correction",
    )
    surface.add_argument("--execute-live", action="store_true")
    parser.add_argument(
        "--primary-only",
        action="store_true",
        help="with --execute-live, run Home-only Primary and skip correction",
    )
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument(
        "--v5-campaign-config",
        type=Path,
        default=DEFAULT_V5_CAMPAIGN_CONFIG,
    )
    parser.add_argument("--state-dir", type=Path, default=DEFAULT_STATE_DIR)
    parser.add_argument("--controller-readback-dir", type=Path, default=DEFAULT_READBACK_DIR)
    parser.add_argument(
        "--canary-controller-readback-dir",
        type=Path,
        help="fresh governed put/get read-back directory for the no-contact canary",
    )
    parser.add_argument("--canary-dir", type=Path, default=DEFAULT_CANARY_DIR)
    parser.add_argument(
        "--home-calibration-receipt",
        type=Path,
        help=(
            "cold-readable receipt from exactly three non-BO contact acquisitions; "
            "required for --execute-live"
        ),
    )
    parser.add_argument("--controller-host", default="192.168.1.18")
    parser.add_argument("--kunwei-host", default="192.168.50.25")
    parser.add_argument("--kunwei-port", type=int, default=5152)
    parser.add_argument(
        "--timing-scheduler-profile",
        choices=(LATE_CONTROL_FIFO_PROFILE, QUOTA_SAFE_OTHER_PROFILE),
        default=LATE_CONTROL_FIFO_PROFILE,
        help="Per-chain scheduler lease; helpers remain SCHED_OTHER.",
    )
    parser.add_argument(
        "--optimizer-python",
        type=Path,
        default=Path(
            "/home/andy/.local/share/step5d-autotune-v3/runtimes/"
            "8f980abdd3d9a4ebef460be259f6cd7c0c802015ee553201d3a888be83252475/"
            "optimizer/bin/python"
        ),
    )
    parser.add_argument(
        "--no-motion-recipe-receipt",
        type=Path,
        help="release-bound no-motion recipe receipt; required for --execute-live",
    )
    parser.add_argument(
        "--recipe-receipt-output",
        type=Path,
        help="fresh output path required for --probe-recipe",
    )
    parser.add_argument("--incomplete", action="store_true", help="dry-run only: preserve a formal gap and verify rejection")
    args = parser.parse_args(argv)
    if args.primary_only and not args.execute_live:
        parser.error("--primary-only requires --execute-live")
    live_started = False
    try:
        if args.execute_live:
            from step5d_managed_runtime import (
                load_runtime_manifest,
                resolve_managed_optimizer_runtime,
            )

            managed_manifest = load_runtime_manifest(STEP6_RUNTIME_MANIFEST, root=ROOT)
            managed_optimizer = resolve_managed_optimizer_runtime(managed_manifest)
            requested_optimizer = Path(args.optimizer_python).resolve(strict=True)
            managed_optimizer_path = managed_optimizer.python_executable.resolve(strict=True)
            if requested_optimizer != managed_optimizer_path:
                raise FigureEightError(
                    "optimizer runtime is not bound to the Step6 managed manifest: "
                    f"requested={requested_optimizer} managed={managed_optimizer_path}"
                )
            args.optimizer_python = managed_optimizer.python_executable
        elif args.capability_only:
            # Capability acceptance never invokes the optimizer worker, but
            # the typed production input still carries an existing executable
            # slot.  Keep it explicit and host-independent rather than using
            # the stale historical optimizer default.
            args.optimizer_python = Path("/usr/bin/python3")
        if args.validate:
            result = validate_offline_surfaces(
                args.config,
                controller_readback_dir=args.controller_readback_dir,
                canary_dir=args.canary_dir,
                home_calibration_receipt=args.home_calibration_receipt,
            )
        elif args.dry_run:
            result = dry_run(
                args.config,
                args.state_dir,
                controller_readback_dir=args.controller_readback_dir,
                canary_dir=args.canary_dir,
                home_calibration_receipt=args.home_calibration_receipt,
                incomplete=args.incomplete,
                test_mode=True,
            )
        elif args.status:
            result = status(args.state_dir)
        elif args.calibrate_home:
            from step6_figure8_autotune_v1.campaign_runner import (
                HomeCalibrationInputsV1,
                execute_figure8_home_calibration_v1,
            )

            result = execute_figure8_home_calibration_v1(
                HomeCalibrationInputsV1(
                    root=ROOT,
                    config_path=args.config,
                    state_root=args.state_dir,
                    controller_readback_dir=args.controller_readback_dir,
                    canary_dir=args.canary_dir,
                    robot_host=str(args.controller_host),
                    kunwei_host=str(args.kunwei_host),
                    kunwei_port=int(args.kunwei_port),
                )
            )
        elif args.probe_recipe:
            if args.recipe_receipt_output is None:
                raise FigureEightError(
                    "--probe-recipe requires a fresh --recipe-receipt-output"
                )
            from step6_figure8_autotune_v1.v5_capability_acceptance import (
                execute_no_motion_recipe_probe,
            )

            result = execute_no_motion_recipe_probe(
                root=ROOT,
                output_path=args.recipe_receipt_output,
                controller_host=str(args.controller_host),
            )
        else:
            if args.home_calibration_receipt is None:
                raise FigureEightError(
                    "--execute-live requires --home-calibration-receipt; "
                    "calibration-only z=0.0345 is never a campaign Home"
                )
            if args.no_motion_recipe_receipt is None:
                raise FigureEightError(
                    "--execute-live requires --no-motion-recipe-receipt"
                )
            if args.canary_controller_readback_dir is None:
                raise FigureEightError(
                    "--execute-live requires --canary-controller-readback-dir"
                )
            from step6_figure8_autotune_v1.v5_production_bundle import (
                V5ProductionBundleInputsV1,
                V5ProductionBundleV1,
            )

            bundle = V5ProductionBundleV1(
                V5ProductionBundleInputsV1(
                    root=ROOT,
                    state_root=args.state_dir,
                    controller_readback_dir=args.controller_readback_dir,
                    canary_controller_readback_dir=args.canary_controller_readback_dir,
                    canary_dir=args.canary_dir,
                    home_calibration_receipt=args.home_calibration_receipt,
                    no_motion_recipe_receipt=args.no_motion_recipe_receipt,
                    optimizer_python=args.optimizer_python,
                    robot_host=str(args.controller_host),
                    kunwei_host=str(args.kunwei_host),
                    kunwei_port=int(args.kunwei_port),
                    v5_campaign_config=args.v5_campaign_config,
                    compatibility_config=args.config,
                    timing_scheduler_profile=str(args.timing_scheduler_profile),
                ),
            )
            with _v5_formal_timing_lease(args.state_dir):
                # Set this only after all immutable package/read-back and
                # admission checks have passed.  If the resident bundle then
                # raises, the child must not publish a false ``live_executed``
                # negative to its managed parent.
                live_started = True
                if args.capability_only:
                    result = bundle.capability.run()
                elif args.primary_only:
                    result = bundle.run_primary_only()
                else:
                    result = bundle.run()
        print(json.dumps(result, indent=2, sort_keys=True, default=str))
        return 0
    except (FigureEightError, OSError, RuntimeError, ValueError) as exc:
        error_text = str(exc)
        print(
            json.dumps(
                {
                    "ok": False,
                    "error": error_text,
                    "live_executed": live_started,
                    "recoverable_continuation_marker": (
                        V5_RECOVERABLE_CONTINUATION_MARKER
                        if V5_RECOVERABLE_CONTINUATION_MARKER in error_text
                        else None
                    ),
                },
                indent=2,
            ),
            file=sys.stderr,
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
