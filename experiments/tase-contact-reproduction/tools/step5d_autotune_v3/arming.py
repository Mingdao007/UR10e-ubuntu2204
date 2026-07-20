"""Strict control-plane identity and authorization for Step5d V3 ARM.

All file I/O and JSON validation in this module happens before the 500 Hz
bridge loop is allowed to consume an ARM mailbox command.  A bridge-start
context is a machine binding only; it deliberately contains no capability to
move.  A campaign arming context becomes usable only after the final release,
certified evidence, and an external typed campaign authorization agree.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass
from datetime import datetime
import hashlib
import math
from pathlib import Path
from typing import Any, Mapping

from ur10e_experiment_runtime.authorization import (
    AuthorizationError,
    CampaignAuthorization,
    CertificationMotionAuthorization,
    STEP5D_V3_STAGE_IDENTITY,
    load_campaign_authorization,
)
from ur10e_experiment_runtime.identity import canonical_sha256, load_strict_json
from ur10e_experiment_runtime.moving_sphere import (
    STOPPING_BOUND_VALIDITY_DOMAIN,
    StoppingBoundArtifact,
    load_stopping_bound_artifact,
)
from ur10e_experiment_runtime.physical_prior import STEP5D_V3_PHYSICAL_PRIOR
from ur10e_experiment_runtime.return_route import RETURN_ROUTE_EVIDENCE_SCHEMA

from .identity_layers import (
    CONTROL_PROFILE_ID,
    RELEASE_STAGE_ID,
    TP_PROGRAM_ID,
    release_basis_fingerprint,
    release_fingerprint,
    runtime_environment_fingerprint,
)


BRIDGE_START_CONTEXT_SCHEMA = "step5d.autotune-v3/bridge-start-context-v2"
CAMPAIGN_ARMING_CONTEXT_SCHEMA = "step5d.autotune-v3/campaign-arming-context-v1"
EXPERIMENT_ROOT = Path(__file__).resolve().parents[2]
REPOSITORY_ROOT = EXPERIMENT_ROOT.parents[1]

_STOPPING_SOURCE_PATHS = {
    "bridge": EXPERIMENT_ROOT / "tools/kunwei_rtde_bridge.py",
    "moving_sphere": (
        REPOSITORY_ROOT
        / "src/ur10e_experiment_runtime/ur10e_experiment_runtime/moving_sphere.py"
    ),
    "stage_adapter": (
        REPOSITORY_ROOT
        / "src/ur10e_experiment_runtime/ur10e_experiment_runtime/stage_adapters.py"
    ),
}
_RETURN_SOURCE_PATHS = {
    "return_route": (
        REPOSITORY_ROOT
        / "src/ur10e_experiment_runtime/ur10e_experiment_runtime/return_route.py"
    ),
    "tp_generator": EXPERIMENT_ROOT / "tools/build_step5d_autotune_tp_v3.py",
    "bridge_capture": EXPERIMENT_ROOT / "tools/run_step5d_autotune_v3_bridge.py",
    "campaign_adapter": EXPERIMENT_ROOT / "tools/run_step5d_autotune_campaign.py",
    "closure_collector": (
        EXPERIMENT_ROOT / "tools/step5d_autotune_runtime_lifecycle.py"
    ),
}


class ArmingError(ValueError):
    """A bridge-start or campaign-arming identity is incomplete or stale."""


def _sha256_value(name: str, value: Any) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ArmingError(f"{name} must be a lowercase SHA256")
    return value


def _positive_int(name: str, value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ArmingError(f"{name} must be a positive integer")
    return value


def _exact(value: Any, fields: set[str], role: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or set(value) != fields:
        raise ArmingError(f"{role} fields differ")
    return value


def _file_sha256(path: Path, role: str) -> str:
    if not path.is_absolute() or path.is_symlink() or not path.is_file():
        raise ArmingError(f"{role} must be an absolute regular file")
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _reference(value: Any, role: str) -> tuple[Path, str]:
    row = _exact(value, {"path", "sha256"}, f"{role} reference")
    path_text = row["path"]
    if not isinstance(path_text, str) or not path_text:
        raise ArmingError(f"{role} path must be non-empty")
    path = Path(path_text)
    expected = _sha256_value(f"{role}.sha256", row["sha256"])
    if _file_sha256(path, role) != expected:
        raise ArmingError(f"{role} digest differs")
    return path, expected


def _authorization_reference(
    value: Any,
    role: str,
) -> tuple[Path, str, str]:
    row = _exact(
        value,
        {"path", "file_sha256", "authorization_ref_sha256"},
        f"{role} reference",
    )
    path_text = row["path"]
    if not isinstance(path_text, str) or not path_text:
        raise ArmingError(f"{role} path must be non-empty")
    path = Path(path_text)
    file_sha256 = _sha256_value(f"{role}.file_sha256", row["file_sha256"])
    authorization_ref_sha256 = _sha256_value(
        f"{role}.authorization_ref_sha256",
        row["authorization_ref_sha256"],
    )
    if _file_sha256(path, role) != file_sha256:
        raise ArmingError(f"{role} file digest differs")
    return path, file_sha256, authorization_ref_sha256


def _triplet(value: Any, role: str) -> dict[str, str]:
    row = _exact(value, {".script", ".txt", ".urp"}, role)
    return {
        suffix: _sha256_value(f"{role}.{suffix}", row[suffix])
        for suffix in (".script", ".txt", ".urp")
    }


@dataclass(frozen=True)
class BridgeStartContext:
    """Machine binding that can start V3 in NO_ARM but grants no motion."""

    tick_semantics_fingerprint: str
    timing_harness_fingerprint: str
    runtime_environment_fingerprint: str
    deployment_fingerprint: str
    orchestration_fingerprint: str
    release_basis_fingerprint: str
    local_triplet_sha256: Mapping[str, str]
    plant_epoch: int
    deployment_readback_sha256: str
    runtime_environment_manifest: Mapping[str, Any]

    def __post_init__(self) -> None:
        for name in (
            "tick_semantics_fingerprint",
            "timing_harness_fingerprint",
            "runtime_environment_fingerprint",
            "deployment_fingerprint",
            "orchestration_fingerprint",
            "release_basis_fingerprint",
            "deployment_readback_sha256",
        ):
            _sha256_value(name, getattr(self, name))
        _triplet(self.local_triplet_sha256, "local_triplet_sha256")
        _positive_int("plant_epoch", self.plant_epoch)
        runtime_manifest = _exact(
            self.runtime_environment_manifest,
            {"schema", "environment"},
            "runtime environment manifest",
        )
        if (
            runtime_manifest["schema"]
            != "step5d.autotune-v3/runtime-environment-identity-v1"
            or not isinstance(runtime_manifest["environment"], Mapping)
            or runtime_environment_fingerprint(runtime_manifest["environment"])
            != self.runtime_environment_fingerprint
        ):
            raise ArmingError("bridge-start runtime environment identity differs")
        object.__setattr__(
            self,
            "runtime_environment_manifest",
            copy.deepcopy(dict(runtime_manifest)),
        )
        expected = release_basis_fingerprint(
            tick_semantics_fingerprint=self.tick_semantics_fingerprint,
            timing_harness_fingerprint=self.timing_harness_fingerprint,
            runtime_environment_fingerprint=self.runtime_environment_fingerprint,
            deployment_fingerprint=self.deployment_fingerprint,
            orchestration_fingerprint=self.orchestration_fingerprint,
            plant_epoch=self.plant_epoch,
        )
        if self.release_basis_fingerprint != expected:
            raise ArmingError("bridge-start release basis differs")

    @property
    def identity(self) -> dict[str, str]:
        return {
            "tick_semantics_fingerprint": self.tick_semantics_fingerprint,
            "timing_harness_fingerprint": self.timing_harness_fingerprint,
            "runtime_environment_fingerprint": self.runtime_environment_fingerprint,
            "deployment_fingerprint": self.deployment_fingerprint,
            "orchestration_fingerprint": self.orchestration_fingerprint,
            "release_basis_fingerprint": self.release_basis_fingerprint,
        }

    def document(self) -> dict[str, Any]:
        return {
            "schema": BRIDGE_START_CONTEXT_SCHEMA,
            "stage_identity": STEP5D_V3_STAGE_IDENTITY.document(),
            "identity": self.identity,
            "local_triplet_sha256": dict(self.local_triplet_sha256),
            "plant_epoch": self.plant_epoch,
            "deployment_readback_sha256": self.deployment_readback_sha256,
            "runtime_environment_manifest": copy.deepcopy(
                dict(self.runtime_environment_manifest)
            ),
            "selected_release": RELEASE_STAGE_ID,
            "control_profile_provenance": CONTROL_PROFILE_ID,
            "tp_program_id": TP_PROGRAM_ID,
            "bridge_start_ready": True,
            "motion_authorized": False,
            "campaign_authorized": False,
        }

    @property
    def fingerprint(self) -> str:
        return canonical_sha256(self.document())

    @classmethod
    def from_document(cls, payload: Any) -> "BridgeStartContext":
        document = _exact(
            payload,
            {
                "schema",
                "stage_identity",
                "identity",
                "local_triplet_sha256",
                "plant_epoch",
                "deployment_readback_sha256",
                "runtime_environment_manifest",
                "selected_release",
                "control_profile_provenance",
                "tp_program_id",
                "bridge_start_ready",
                "motion_authorized",
                "campaign_authorized",
            },
            "bridge-start context",
        )
        if (
            document["schema"] != BRIDGE_START_CONTEXT_SCHEMA
            or document["stage_identity"] != STEP5D_V3_STAGE_IDENTITY.document()
            or document["selected_release"] != RELEASE_STAGE_ID
            or document["control_profile_provenance"] != CONTROL_PROFILE_ID
            or document["tp_program_id"] != TP_PROGRAM_ID
            or document["bridge_start_ready"] is not True
            or document["motion_authorized"] is not False
            or document["campaign_authorized"] is not False
        ):
            raise ArmingError("bridge-start context escaped NO_ARM")
        identity = _exact(
            document["identity"],
            {
                "tick_semantics_fingerprint",
                "timing_harness_fingerprint",
                "runtime_environment_fingerprint",
                "deployment_fingerprint",
                "orchestration_fingerprint",
                "release_basis_fingerprint",
            },
            "bridge-start identity",
        )
        return cls(
            **dict(identity),
            local_triplet_sha256=_triplet(
                document["local_triplet_sha256"], "local_triplet_sha256"
            ),
            plant_epoch=document["plant_epoch"],
            deployment_readback_sha256=document["deployment_readback_sha256"],
            runtime_environment_manifest=document["runtime_environment_manifest"],
        )


def load_bridge_start_context(
    path: str | Path,
    *,
    expected_static_identity: Mapping[str, Any] | None = None,
    expected_deployment_readback_sha256: str | None = None,
) -> BridgeStartContext:
    source = Path(path)
    _file_sha256(source, "bridge-start context")
    context = BridgeStartContext.from_document(load_strict_json(source))
    if expected_static_identity is not None:
        for name in (
            "tick_semantics_fingerprint",
            "timing_harness_fingerprint",
            "runtime_environment_fingerprint",
            "deployment_fingerprint",
            "orchestration_fingerprint",
        ):
            expected = expected_static_identity.get(name)
            if expected is not None and context.identity[name] != expected:
                raise ArmingError(f"bridge-start {name} differs from current runtime")
        expected_triplet = expected_static_identity.get("local_triplet_sha256")
        if expected_triplet is not None and dict(context.local_triplet_sha256) != dict(
            expected_triplet
        ):
            raise ArmingError("bridge-start triplet differs from current runtime")
    if (
        expected_deployment_readback_sha256 is not None
        and context.deployment_readback_sha256
        != _sha256_value(
            "expected_deployment_readback_sha256",
            expected_deployment_readback_sha256,
        )
    ):
        raise ArmingError("bridge-start deployment readback differs")
    return context


def _certification_provenance(
    path: Path,
    *,
    context: BridgeStartContext,
) -> CertificationMotionAuthorization:
    try:
        authorization = CertificationMotionAuthorization.from_document(
            load_strict_json(path)
        )
    except (AuthorizationError, ValueError) as exc:
        raise ArmingError(f"certification provenance is invalid: {exc}") from exc
    if (
        authorization.release_basis_fingerprint
        != context.release_basis_fingerprint
        or authorization.deployment_fingerprint != context.deployment_fingerprint
        or authorization.deployment_readback_sha256
        != context.deployment_readback_sha256
        or authorization.plant_epoch != context.plant_epoch
    ):
        raise ArmingError("certification provenance differs from bridge-start context")
    return authorization


def _current_source_sha256(paths: Mapping[str, Path], role: str) -> dict[str, str]:
    return {name: _file_sha256(path, f"{role} source {name}") for name, path in paths.items()}


def _load_certified_return_evidence(
    path: Path,
    *,
    context: BridgeStartContext,
    certification_authorization_sha256: str,
) -> tuple[Mapping[str, Any], str]:
    payload = load_strict_json(path)
    required = {
        "schema",
        "status",
        "certified",
        "optimizer_eligible",
        "source_binding_sha256",
        "source_sha256",
        "verifier_provenance",
        "local_triplet_sha256",
        "policy",
        "offline_guards",
        "certification_authorization_sha256",
        "certification_binding_sha256",
        "plant_epoch",
        "deployment_readback_sha256",
        "motion_capable_ursim_trace_sha256",
        "motion_capable_ursim_trace_binding",
        "attended_controller_readback_sha256",
        "source_exact_return_telemetry_sha256",
        "telemetry_summary",
        "live_effect",
    }
    evidence = _exact(payload, required, "certified return evidence")
    if (
        evidence["schema"] != RETURN_ROUTE_EVIDENCE_SCHEMA
        or evidence["status"] != "certified_attended_return_measurement"
        or evidence["certified"] is not True
        or evidence["optimizer_eligible"] is not False
        or evidence["live_effect"]
        != "return_route_angular_envelope_certified_for_exact_epoch"
    ):
        raise ArmingError("return evidence is not a promoted certification")
    expected_sources = _current_source_sha256(
        _RETURN_SOURCE_PATHS, "return-route"
    )
    if (
        evidence["source_sha256"] != expected_sources
        or evidence["source_binding_sha256"] != canonical_sha256(expected_sources)
    ):
        raise ArmingError("return evidence source binding differs")
    if _triplet(evidence["local_triplet_sha256"], "return local triplet") != dict(
        context.local_triplet_sha256
    ):
        raise ArmingError("return evidence triplet differs")
    if (
        evidence["plant_epoch"] != context.plant_epoch
        or evidence["deployment_readback_sha256"]
        != context.deployment_readback_sha256
        or evidence["certification_authorization_sha256"]
        != certification_authorization_sha256
    ):
        raise ArmingError("return evidence deployment binding differs")
    for name in (
        "motion_capable_ursim_trace_sha256",
        "attended_controller_readback_sha256",
        "source_exact_return_telemetry_sha256",
        "certification_binding_sha256",
    ):
        _sha256_value(f"return evidence {name}", evidence[name])
    expected_binding = canonical_sha256(
        {
            "schema": "ur-exp/step5d-return-route-certification-binding-v1",
            "source_binding_sha256": evidence["source_binding_sha256"],
            "triplet_sha256": dict(context.local_triplet_sha256),
            "deployment_readback_sha256": context.deployment_readback_sha256,
            "certification_authorization_sha256": (
                certification_authorization_sha256
            ),
            "plant_epoch": context.plant_epoch,
            "motion_capable_ursim_trace_sha256": evidence[
                "motion_capable_ursim_trace_sha256"
            ],
            "attended_controller_readback_sha256": evidence[
                "attended_controller_readback_sha256"
            ],
            "source_exact_return_telemetry_sha256": evidence[
                "source_exact_return_telemetry_sha256"
            ],
        }
    )
    if evidence["certification_binding_sha256"] != expected_binding:
        raise ArmingError("return evidence certification binding differs")
    summary = evidence["telemetry_summary"]
    if not isinstance(summary, Mapping) or not summary:
        raise ArmingError("return evidence telemetry summary is missing")
    maxima = summary.get("maxima")
    if not isinstance(maxima, Mapping) or any(
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
        for value in maxima.values()
    ):
        raise ArmingError("return evidence telemetry summary is nonfinite")
    return evidence, evidence["certification_binding_sha256"]


@dataclass(frozen=True)
class ArmingContext:
    bridge_start: BridgeStartContext
    campaign_id: str
    campaign_epoch: int
    campaign_fingerprint: str
    release_fingerprint: str
    certification_authorization_sha256: str
    stopping_bound: StoppingBoundArtifact
    return_evidence_fingerprint: str
    campaign_authorization: CampaignAuthorization
    context_file_sha256: str

    def __post_init__(self) -> None:
        if not isinstance(self.campaign_id, str) or not self.campaign_id:
            raise ArmingError("campaign_id must be non-empty")
        _positive_int("campaign_epoch", self.campaign_epoch)
        for name in (
            "campaign_fingerprint",
            "release_fingerprint",
            "certification_authorization_sha256",
            "return_evidence_fingerprint",
            "context_file_sha256",
        ):
            _sha256_value(name, getattr(self, name))
        if not self.stopping_bound.certified:
            raise ArmingError("arming context requires a certified stopping bound")


def load_campaign_arming_context(
    path: str | Path,
    *,
    expected_static_identity: Mapping[str, Any] | None = None,
    now: datetime | None = None,
) -> ArmingContext:
    source = Path(path)
    context_file_sha256 = _file_sha256(source, "campaign arming context")
    payload = _exact(
        load_strict_json(source),
        {
            "schema",
            "bridge_start_context",
            "certification_authorization",
            "stopping_bound",
            "return_evidence",
            "campaign_authorization",
            "campaign",
            "release_fingerprint",
            "physical_prior_fingerprint",
        },
        "campaign arming context",
    )
    if payload["schema"] != CAMPAIGN_ARMING_CONTEXT_SCHEMA:
        raise ArmingError("campaign arming context schema differs")
    if payload["physical_prior_fingerprint"] != STEP5D_V3_PHYSICAL_PRIOR.fingerprint:
        raise ArmingError("campaign arming physical prior differs")

    bridge_path, _ = _reference(payload["bridge_start_context"], "bridge-start context")
    bridge_start = load_bridge_start_context(
        bridge_path,
        expected_static_identity=expected_static_identity,
    )
    certification_path, _, certification_ref_sha = _authorization_reference(
        payload["certification_authorization"], "certification authorization"
    )
    certification = _certification_provenance(
        certification_path,
        context=bridge_start,
    )
    if certification.authorization_ref_sha256 != certification_ref_sha:
        raise ArmingError("certification authorization canonical digest differs")

    stopping_path, _ = _reference(payload["stopping_bound"], "stopping bound")
    stopping_sources = _current_source_sha256(
        _STOPPING_SOURCE_PATHS, "stopping-bound"
    )
    stopping = load_stopping_bound_artifact(
        stopping_path,
        expected_validity_domain=STOPPING_BOUND_VALIDITY_DOMAIN,
        expected_source_binding_sha256=canonical_sha256(stopping_sources),
        expected_stop_transport_sha256=stopping_sources["bridge"],
        expected_deployment_readback_sha256=(
            bridge_start.deployment_readback_sha256
        ),
        certification_authorization=certification,
        expected_plant_epoch=bridge_start.plant_epoch,
    )
    stopping_reference = _exact(
        payload["stopping_bound"], {"path", "sha256"}, "stopping-bound reference"
    )
    if stopping_reference["sha256"] != _file_sha256(stopping_path, "stopping bound"):
        raise ArmingError("stopping-bound reference digest differs")

    return_path, _ = _reference(payload["return_evidence"], "return evidence")
    _return_evidence, return_evidence_fingerprint = _load_certified_return_evidence(
        return_path,
        context=bridge_start,
        certification_authorization_sha256=certification_ref_sha,
    )
    final_release = release_fingerprint(
        release_basis_fingerprint=bridge_start.release_basis_fingerprint,
        stopping_bound_fingerprint=stopping.fingerprint,
        return_evidence_fingerprint=return_evidence_fingerprint,
    )
    if payload["release_fingerprint"] != final_release:
        raise ArmingError("campaign arming final release differs")

    campaign = _exact(
        payload["campaign"],
        {"campaign_id", "campaign_epoch", "campaign_fingerprint"},
        "campaign arming campaign",
    )
    campaign_id = campaign["campaign_id"]
    campaign_epoch = _positive_int("campaign_epoch", campaign["campaign_epoch"])
    campaign_fingerprint = _sha256_value(
        "campaign_fingerprint", campaign["campaign_fingerprint"]
    )
    authorization_path, _, authorization_ref_sha = _authorization_reference(
        payload["campaign_authorization"], "campaign authorization"
    )
    try:
        campaign_authorization = load_campaign_authorization(
            authorization_path,
            expected_campaign_id=campaign_id,
            expected_campaign_epoch=campaign_epoch,
            expected_campaign_fingerprint=campaign_fingerprint,
            expected_release_fingerprint=final_release,
            expected_deployment_fingerprint=bridge_start.deployment_fingerprint,
            expected_plant_epoch=bridge_start.plant_epoch,
            expected_deployment_readback_sha256=(
                bridge_start.deployment_readback_sha256
            ),
            now=now,
        )
    except (AuthorizationError, ValueError) as exc:
        raise ArmingError(f"campaign authorization is invalid: {exc}") from exc
    if campaign_authorization.authorization_ref_sha256 != authorization_ref_sha:
        raise ArmingError("campaign authorization canonical digest differs")
    return ArmingContext(
        bridge_start=bridge_start,
        campaign_id=campaign_id,
        campaign_epoch=campaign_epoch,
        campaign_fingerprint=campaign_fingerprint,
        release_fingerprint=final_release,
        certification_authorization_sha256=certification_ref_sha,
        stopping_bound=stopping,
        return_evidence_fingerprint=return_evidence_fingerprint,
        campaign_authorization=campaign_authorization,
        context_file_sha256=context_file_sha256,
    )


__all__ = [
    "ArmingContext",
    "ArmingError",
    "BRIDGE_START_CONTEXT_SCHEMA",
    "BridgeStartContext",
    "CAMPAIGN_ARMING_CONTEXT_SCHEMA",
    "load_bridge_start_context",
    "load_campaign_arming_context",
]
