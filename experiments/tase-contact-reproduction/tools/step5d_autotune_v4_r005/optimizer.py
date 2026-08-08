"""V4 optimizer adapter over the V3 CUDA qLogNEI implementation."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Sequence

from step5d_managed_runtime import (
    ManagedRuntimeError,
    ManagedRuntimeManifest,
    resolve_managed_optimizer_runtime,
)

from .contracts import Candidate, full_domain, validate_transition
from .observations import ObservationLedger, ObservationRecord
from step5d_optimizer_runtime import (
    OptimizerProfileDeclaration,
    OptimizerRuntimeError,
    OptimizerSubprocessClient,
)


class OptimizerError(RuntimeError):
    """The optimizer contract or its evidence binding is invalid."""


class OptimizerUnavailable(OptimizerError):
    """The required CUDA qLogNEI backend is unavailable; no fallback exists."""


class OptimizerBindingError(OptimizerError):
    """A proposal, pending set, or told observation cannot be bound safely."""


AskImplementation = Callable[
    [Sequence[Any], Sequence[Any], Sequence[Any], int, int], tuple[Any, Mapping[str, Any]]
]


def _trial_uid(record: ObservationRecord) -> str:
    return hashlib.sha256(
        (
            f"r005:{record.campaign_fingerprint}:{record.epoch}:"
            f"{record.attempt_sequence}:{record.candidate_uid}"
        ).encode("utf-8")
    ).hexdigest()


@dataclass(frozen=True)
class OptimizerAsk:
    candidate: Candidate
    metadata: Mapping[str, Any]


class V4BoAdapter:
    """Typed V4 plane using V3's CUDA qLogNEI primitives.

    The mapping into the V3 candidate type is coordinate-only: it preserves
    the V4 named coordinates while never importing V3's target-force value or
    candidate identity into an r005 queue/observation.
    """

    def __init__(
        self,
        *,
        seed: int = 5005,
        ask_impl: AskImplementation | None = None,
        ledger: ObservationLedger | None = None,
        optimizer_profile: str = "optimizer",
        runtime_manifest: ManagedRuntimeManifest | None = None,
    ) -> None:
        if isinstance(seed, bool) or not isinstance(seed, int) or seed < 0:
            raise OptimizerBindingError("optimizer seed must be a non-negative integer")
        self.seed = seed
        self._ask_impl = ask_impl
        self._ledger = ledger
        if runtime_manifest is not None and not isinstance(runtime_manifest, ManagedRuntimeManifest):
            raise OptimizerBindingError("optimizer runtime manifest is not typed")
        self._runtime_manifest = runtime_manifest
        self._optimizer_profile = optimizer_profile
        try:
            self._optimizer_declaration = OptimizerProfileDeclaration(
                profile=optimizer_profile
            )
        except OptimizerRuntimeError as exc:
            raise OptimizerBindingError(str(exc)) from exc
        self._told: dict[int, ObservationRecord] = {}
        self.last_ask_metadata: Mapping[str, Any] = {}

    def _managed_optimizer_binding(self) -> Any:
        """Resolve the manifest-declared V3 optimizer profile at the CUDA seam."""

        manifest = self._runtime_manifest
        if manifest is None:
            try:
                from .contracts import load_contract

                manifest = load_contract().runtime_manifest
            except Exception as exc:
                raise OptimizerBindingError(
                    "production optimizer runtime manifest is unavailable"
                ) from exc
        if manifest.optimizer_profile != self._optimizer_profile:
            raise OptimizerBindingError("optimizer profile differs from the bound runtime manifest")
        try:
            return resolve_managed_optimizer_runtime(manifest)
        except ManagedRuntimeError as exc:
            raise OptimizerBindingError(
                f"managed V3 optimizer runtime admission failed: {exc}"
            ) from exc

    @staticmethod
    def _to_v3(candidate: Candidate) -> Any:
        try:
            from step5d_autotune_contract import ForceCandidate as V3ForceCandidate
        except ImportError as exc:
            raise OptimizerUnavailable("V3 optimizer contract is unavailable") from exc
        p, damping, tau, i_on, i_off, ko, kp = candidate.named7d
        try:
            return V3ForceCandidate.from_log2(
                p=p,
                damping=damping,
                filter_tau=tau,
                i=i_on,
                i_off=bool(i_off),
                orientation=ko,
                motion=kp,
            )
        except (TypeError, ValueError) as exc:
            raise OptimizerBindingError("V4 to V3 optimizer coordinate mapping failed") from exc

    @staticmethod
    def _from_v3(candidate: Any) -> Candidate:
        try:
            from step5d_autotune_optimizer import candidate_vector
        except ImportError as exc:
            raise OptimizerUnavailable("V3 optimizer vector helper is unavailable") from exc
        try:
            return Candidate.from_named7d(tuple(candidate_vector(candidate)))
        except (TypeError, ValueError) as exc:
            raise OptimizerBindingError("V3 proposal is outside the V4 named domain") from exc

    @staticmethod
    def _to_v3_observation(record: ObservationRecord) -> Any:
        try:
            from step5d_autotune_contract import Evaluation, TrialDisposition
            from step5d_autotune_v3.optimizer_types import OutcomeRecord
        except ImportError as exc:
            raise OptimizerUnavailable("V3 optimizer observation types are unavailable") from exc
        try:
            record.validate_verified_evidence()
        except Exception as exc:
            raise OptimizerBindingError(
                "optimizer observation is not fresh raw-artifact verified"
            ) from exc
        eligible = record.eligible
        if eligible:
            try:
                record.validate_for_optimizer()
            except Exception as exc:
                raise OptimizerBindingError(
                    "optimizer observation failed the sealed objective boundary"
                ) from exc
        failures = tuple(
            name
            for name in (
                "safe_return",
                "binding_ok",
                "safety_gate",
                "contact_gate",
                "return_gate",
                "timing_gate",
                "identity_gate",
            )
            if not getattr(record, name)
        )
        # A motion failure remains a diagnostic on non-trainable V3 history,
        # but it cannot become a structural veto after r005 eligibility has
        # accepted the raw force objective for BO training.
        if not eligible and not record.motion_gate:
            failures += ("motion_gate",)
        evaluation = Evaluation(
            trial_uid=_trial_uid(record),
            backend_id="autotune_v4_r005",
            eligible=eligible,
            disposition=TrialDisposition.OBJECTIVE if eligible else TrialDisposition.PARAMETER_EVENT,
            objective_mae_n=float(record.mae_n) if eligible and record.mae_n is not None else None,
            force_bias_n=0.0 if eligible else None,
            force_std_n=0.0 if eligible else None,
            coverage_12_plus_minus_1_ratio=1.0 if eligible else None,
            complete_bins=550 if eligible else 0,
            safe_closure=record.safe_return,
            structural_failures=failures,
            metrics={"r005_candidate_uid": record.candidate_uid, **dict(record.metrics)},
        )
        return OutcomeRecord(
            candidate=V4BoAdapter._to_v3(record.candidate),
            evaluation=evaluation,
            profile_id="step5d_strict_rnn_autotune_v4_r005",
            plant_epoch=record.epoch,
        )

    def _choices(
        self,
        *,
        observations: Sequence[ObservationRecord],
        pending: Sequence[Candidate],
        incumbent: Candidate,
    ) -> tuple[Candidate, ...]:
        observed = {record.candidate_uid for record in observations}
        pending_uids = [candidate.candidate_uid for candidate in pending]
        if len(pending_uids) > 2 or len(set(pending_uids)) != len(pending_uids):
            raise OptimizerBindingError("X_pending must contain at most two unique candidates")
        if any(uid in observed for uid in pending_uids):
            raise OptimizerBindingError("X_pending contains an observed candidate")
        choices: list[Candidate] = []
        for candidate in full_domain():
            if candidate.candidate_uid in observed or candidate.candidate_uid in pending_uids:
                continue
            if candidate.candidate_uid == incumbent.candidate_uid:
                continue
            try:
                validate_transition(incumbent, candidate)
            except Exception:
                continue
            choices.append(candidate)
        if not choices:
            raise OptimizerUnavailable("V4 named domain is exhausted from the incumbent")
        return tuple(choices)

    def ask(
        self,
        *,
        observations: Sequence[ObservationRecord],
        pending: Sequence[Candidate],
        incumbent: Candidate,
        q: int = 1,
    ) -> OptimizerAsk:
        if q != 1:
            raise OptimizerBindingError("r005 refill is sequential and requires q=1")
        if len(pending) > 2:
            raise OptimizerBindingError("r005 pending bound is two")
        for record in observations:
            try:
                record.validate_verified_evidence()
            except Exception as exc:
                raise OptimizerBindingError(
                    "optimizer received an unverified or forged observation"
                ) from exc
        trainable = tuple(record for record in observations if record.eligible)
        if len(trainable) < 6:
            raise OptimizerUnavailable("CUDA qLogNEI requires six sealed trainable observations")
        choices = self._choices(
            observations=observations,
            pending=pending,
            incumbent=incumbent,
        )
        if self._ask_impl is None:
            selected, metadata = self._cuda_qlognei(
                observations,
                choices,
                pending,
                incumbent,
            )
        else:
            v3_observations = tuple(
                self._to_v3_observation(record) for record in observations
            )
            v3_choices = tuple(self._to_v3(candidate) for candidate in choices)
            v3_pending = tuple(self._to_v3(candidate) for candidate in pending)
            try:
                selected, metadata = self._ask_impl(
                    v3_observations,
                    v3_choices,
                    v3_pending,
                    1,
                    self.seed,
                )
            except Exception as exc:
                raise OptimizerError("injected optimizer failed; no fallback is permitted") from exc
        if isinstance(selected, (tuple, list)):
            if len(selected) != 1:
                raise OptimizerBindingError("q=1 optimizer returned more than one candidate")
            selected_v3 = selected[0]
        else:
            selected_v3 = selected
        candidate = selected_v3 if isinstance(selected_v3, Candidate) else self._from_v3(selected_v3)
        if candidate.candidate_uid in {record.candidate_uid for record in observations}:
            raise OptimizerBindingError("optimizer returned an observed candidate")
        if candidate.candidate_uid in {item.candidate_uid for item in pending}:
            raise OptimizerBindingError("optimizer returned an X_pending candidate")
        try:
            validate_transition(incumbent, candidate)
        except Exception as exc:
            raise OptimizerBindingError("optimizer returned an invalid one-coordinate transition") from exc
        self.last_ask_metadata = {
            "adapter": "v4_named7d_over_v3_qlognei",
            "target_force_n": 5.0,
            "pending_count": len(pending),
            "choice_count": len(choices),
            **dict(metadata),
        }
        return OptimizerAsk(candidate=candidate, metadata=self.last_ask_metadata)

    @staticmethod
    def _observation_ref(record: ObservationRecord) -> dict[str, Any]:
        return {
            "attempt_sequence": record.attempt_sequence,
            "observation_uid": record.observation_uid,
        }

    def _ledger_binding(
        self,
        observations: Sequence[ObservationRecord],
    ) -> dict[str, Any]:
        """Freshly bind the production child to this exact durable ledger."""

        if self._ledger is None:
            raise OptimizerBindingError(
                "production CUDA qLogNEI requires its bound ObservationLedger"
            )
        if not isinstance(self._ledger, ObservationLedger):
            raise OptimizerBindingError("optimizer ledger binding is not the r005 ObservationLedger")
        expected_artifact_root = (
            self._ledger.path.parent / "raw_force_evidence"
        ).resolve()
        if self._ledger.artifact_root != expected_artifact_root:
            raise OptimizerBindingError(
                "production optimizer requires the canonical ledger artifact root"
            )
        try:
            before = self._ledger.read_only_identity()
            audit = self._ledger.fresh_process_verify_details()
            after = self._ledger.read_only_identity()
        except Exception as exc:
            raise OptimizerBindingError(
                "parent fresh ledger verification failed before optimizer launch"
            ) from exc
        if (
            before["ledger_path"] != after["ledger_path"]
            or before["ledger_byte_sha256"] != after["ledger_byte_sha256"]
            or audit.get("ledger_sha256") != after["ledger_byte_sha256"]
        ):
            raise OptimizerBindingError("parent ledger changed during fresh verification")
        ledger_records = self._ledger.records
        requested_refs = tuple(self._observation_ref(record) for record in observations)
        ledger_refs = tuple(self._observation_ref(record) for record in ledger_records)
        if requested_refs != ledger_refs:
            raise OptimizerBindingError(
                "optimizer observations differ from the fresh verified ledger refs"
            )
        return {
            "ledger_path": after["ledger_path"],
            "ledger_byte_sha256": after["ledger_byte_sha256"],
            "campaign_fingerprint": after["campaign_fingerprint"],
            "eoat_sha256": after["eoat_sha256"],
            "refs": [dict(ref) for ref in ledger_refs],
        }

    def _cuda_qlognei(
        self,
        observations: Sequence[ObservationRecord],
        choices: Sequence[Candidate],
        pending: Sequence[Candidate],
        incumbent: Candidate,
    ) -> tuple[Any, Mapping[str, Any]]:
        binding = self._managed_optimizer_binding()
        request = {
            "ledger_binding": self._ledger_binding(observations),
            "choices": [candidate.canonical for candidate in choices],
            "pending": [candidate.canonical for candidate in pending],
            "incumbent": incumbent.canonical,
            "seed": self.seed,
        }
        client = OptimizerSubprocessClient(
            declaration=OptimizerProfileDeclaration(profile=binding.profile),
            worker_module=binding.worker_module,
            runtime_resolver=lambda: binding.runtime,
        )
        try:
            response = client.request(request)
        except OptimizerRuntimeError as exc:
            raise OptimizerUnavailable(
                f"isolated CUDA qLogNEI worker unavailable: {exc}"
            ) from exc
        if set(response) != {"selected", "metadata"}:
            raise OptimizerBindingError("isolated qLogNEI response fields differ")
        selected_payload = response.get("selected")
        metadata = response.get("metadata")
        if not isinstance(selected_payload, Mapping) or not isinstance(metadata, Mapping):
            raise OptimizerBindingError("isolated qLogNEI response is not typed")
        try:
            selected = Candidate(**{
                key: value
                for key, value in dict(selected_payload).items()
                if key != "i_off"
            })
        except (TypeError, ValueError) as exc:
            raise OptimizerBindingError("isolated qLogNEI returned an invalid V4 candidate") from exc
        return selected, {
            **dict(metadata),
            "child_attestation": dict(client.last_child_attestation or {}),
        }

    def tell(self, record: ObservationRecord) -> None:
        if not isinstance(record, ObservationRecord) or not record.sealed:
            raise OptimizerBindingError("only sealed typed observations may be told")
        if self._ledger is not None:
            # ObservationLedger construction/append and explicit cold-read
            # audit are the fresh-verification seams.  The tell seam accepts
            # only the exact immutable record object currently owned by that
            # verified ledger; a structurally equal caller copy is rejected.
            if not any(candidate is record for candidate in self._ledger.records):
                raise OptimizerBindingError(
                    "optimizer tell requires the exact fresh ledger observation reference"
                )
        try:
            record.validate_for_optimizer()
        except Exception as exc:
            raise OptimizerBindingError(
                "optimizer tell requires a fresh raw-artifact eligible observation"
            ) from exc
        if record.attempt_sequence in self._told:
            raise OptimizerBindingError("optimizer received a duplicate attempt")
        self._told[record.attempt_sequence] = record

    @property
    def told(self) -> tuple[ObservationRecord, ...]:
        return tuple(self._told[key] for key in sorted(self._told))


__all__ = [
    "OptimizerAsk",
    "OptimizerBindingError",
    "OptimizerError",
    "OptimizerUnavailable",
    "V4BoAdapter",
]
