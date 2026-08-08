"""Formal V4 episode-row composition over the accepted runtime primitives."""

from __future__ import annotations

from dataclasses import fields
import hashlib
import json
from typing import Mapping

from .contracts import (
    ForceAuthorityReceiptV1,
    FormalEpisodeManifestV1,
    ProductionDynamicsConformanceReceiptV1,
)
from .episode_composition import ActionLabel, ActionLabelContext
from .episode_recorder import (
    EpisodeFrameV2,
    EpisodeFrameV4,
    ExpertActionReceiptV1,
    ReferenceReceiptV1,
    TubeDecisionReceiptV1,
)
from .formal_dynamics import ProductionDynamicsRuntimeV1


def _canonical_sha256(payload: Mapping[str, object]) -> str:
    encoded = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


class FormalFrameComposerV1:
    """Upgrade eligible runtime rows to sealed V4 rows, fail closed.

    The first otherwise-eligible row only primes the corrected 42D history.
    It is intentionally not returned, because a formal 84D observation must
    contain a real previous tick with reconstructed internal wrench.
    """

    def __init__(
        self,
        *,
        manifest: FormalEpisodeManifestV1,
        semantic_context_fingerprint_sha256: str,
        dynamics_runtime: ProductionDynamicsRuntimeV1,
    ) -> None:
        if not isinstance(manifest, FormalEpisodeManifestV1):
            raise TypeError("formal frame composer requires a formal manifest")
        fingerprint = str(semantic_context_fingerprint_sha256)
        if len(fingerprint) != 64 or any(character not in "0123456789abcdef" for character in fingerprint):
            raise ValueError("formal semantic-context fingerprint is invalid")
        if not isinstance(dynamics_runtime, ProductionDynamicsRuntimeV1):
            raise TypeError("formal frame composer requires production dynamics")
        self.manifest = manifest
        self.semantic_context_fingerprint_sha256 = fingerprint
        self.dynamics_runtime = dynamics_runtime
        self.previous_corrected_slice_42d: tuple[float, ...] | None = None

    def compose(
        self,
        base: EpisodeFrameV2,
        *,
        previous_runtime_row: Mapping[str, object] | None,
        tube_payload: Mapping[str, object],
    ) -> EpisodeFrameV4 | None:
        if not isinstance(base, EpisodeFrameV2):
            raise TypeError("formal frame composer requires EpisodeFrameV2")
        if not base.candidate_window:
            return None
        if (
            previous_runtime_row is None
            or not base.external_lineage_valid
            or not base.action_echo_coherent
            or not base.echoed_action_valid
            or base.source_row_torn
            or base.source_row_invalid
            or not base.observation_history_valid
            or not base.reference_derivatives_valid
            or not base.expert_label_available
        ):
            return None
        dynamics_sample, dynamics_receipt = self.dynamics_runtime.produce(
            previous_runtime_row,
            sequence=base.control_sequence,
            timestamp_s=base.control_time_s,
        )
        current = list(float(value) for value in base.observation_84d[:42])
        current[6:12] = dynamics_receipt.internal_wrench_tcp_si
        corrected = tuple(current)
        previous = self.previous_corrected_slice_42d
        self.previous_corrected_slice_42d = corrected
        if previous is None:
            return None
        if (
            base.external_device_time_s is None
            or base.external_host_visible_time_s is None
            or base.external_sample_index is None
        ):
            return None
        source_sample_payload = {
            "sample_index": base.external_sample_index,
            "device_time_s": base.external_device_time_s,
            "host_visible_time_s": base.external_host_visible_time_s,
            "wrench_tcp_si": list(corrected[:6]),
        }
        force_receipt = ForceAuthorityReceiptV1(
            sequence=base.control_sequence,
            sample_index=base.external_sample_index,
            device_time_s=base.external_device_time_s,
            host_visible_time_s=base.external_host_visible_time_s,
            frame_id="tool0_tcp",
            authority=self.manifest.force_authority,
            source_sample_sha256=_canonical_sha256(source_sample_payload),
        )
        context = ActionLabelContext(
            sequence=base.control_sequence,
            timestamp_s=base.control_time_s,
            frame_id="tool0_tcp",
            applied_action_12d=base.applied_action_12d,
            echoed_action_12d=base.echoed_action_12d,
            semantic_context_fingerprint_sha256=self.semantic_context_fingerprint_sha256,
        )
        label = ActionLabel(
            expert_action_12d=base.expert_action_12d,
            available=True,
            source=base.expert_action_source,
            semantics=base.action_label_semantics,
            policy_id="deterministic_expert_formal_v4",
            sequence=base.control_sequence,
            timestamp_s=base.control_time_s,
            frame_id="tool0_tcp",
            context_fingerprint_sha256=context.as_json()["context_fingerprint_sha256"],
        )
        reference_payload = {
            "valid": True,
            "shadow_only": False,
            "reference_sample_id": base.reference_sample_id,
            "desired_pose_6d": list(base.desired_pose_6d or ()),
            "desired_twist_6d": list(base.desired_twist_6d or ()),
            "desired_acceleration_6d": list(base.desired_acceleration_6d or ()),
        }
        expert_payload = {
            "available": True,
            "shadow_only": False,
            "expert_action_12d": list(base.expert_action_12d),
            "policy_id": "deterministic_expert_formal_v4",
        }
        tube_receipt_payload = dict(tube_payload)
        tube_receipt_payload.update({"accepted": True, "shadow_only": False})
        base_values = {
            field.name: getattr(base, field.name)
            for field in fields(EpisodeFrameV2)
        }
        base_values.update(
            observation_84d=corrected + previous,
            internal_wrench_valid=True,
            source_row_invalid=False,
            dynamics_sample=dynamics_sample,
            dynamics_receipt=dynamics_receipt,
            action_label_context=context,
            action_label=label,
            reference_receipt=ReferenceReceiptV1(
                "formal_reference/v1", reference_payload
            ),
            identity_enabled=True,
            semantic_context_fingerprint_sha256=self.semantic_context_fingerprint_sha256,
            force_authority_receipt=force_receipt,
            production_dynamics_receipt=ProductionDynamicsConformanceReceiptV1(
                dynamics_receipt
            ),
            expert_action_receipt=ExpertActionReceiptV1(
                "formal_expert_action/v1", expert_payload
            ),
            tube_decision_receipt=TubeDecisionReceiptV1(
                "formal_tube_decision/v1", tube_receipt_payload
            ),
            formal_manifest=self.manifest,
        )
        return EpisodeFrameV4(**base_values)


__all__ = ["FormalFrameComposerV1"]
