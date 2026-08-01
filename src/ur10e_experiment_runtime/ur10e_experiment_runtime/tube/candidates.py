"""Sealed producer/consumer candidate artifacts for modular-tube acceptance."""

from __future__ import annotations

from dataclasses import dataclass, field
import math
from types import MappingProxyType
from typing import Any, Generic, Mapping, Sequence, TypeVar

from .contracts import (
    IdentityError,
    TubeContractError,
    _canonical_value,
    _finite_float,
    _identity_of,
    _sha256,
    _text,
    canonical_json_bytes,
    canonical_sha256,
)


class CandidateBindingError(TubeContractError):
    """Raised when an accepted-candidate artifact cannot be consumed safely."""


ParameterT = TypeVar("ParameterT", bound=Mapping[str, float])


def _parameter_items(parameters: Any) -> tuple[tuple[str, float], ...]:
    if not isinstance(parameters, Mapping):
        raise CandidateBindingError("candidate parameters must be a mapping")
    items: list[tuple[str, float]] = []
    for key, value in parameters.items():
        if not isinstance(key, str) or not key or key.strip() != key:
            raise CandidateBindingError("candidate parameter keys must be canonical strings")
        try:
            checked_value = _finite_float(value, name=f"parameter {key}")
        except TubeContractError as exc:
            raise CandidateBindingError(str(exc)) from exc
        items.append((key, checked_value))
    if not items:
        raise CandidateBindingError("candidate parameters cannot be empty")
    items.sort(key=lambda item: item[0])
    if len({key for key, _ in items}) != len(items):  # pragma: no cover - mapping proof
        raise CandidateBindingError("candidate parameters contain duplicate keys")
    return tuple(items)


def _closure_hashes(values: Any) -> tuple[str, ...]:
    if isinstance(values, (str, bytes)):
        raise CandidateBindingError("source_closure_hashes must be a sequence of hashes")
    try:
        material = tuple(values)
    except TypeError as exc:
        raise CandidateBindingError("source_closure_hashes must be a sequence of hashes") from exc
    if not material:
        raise CandidateBindingError("source_closure_hashes cannot be empty")
    checked = tuple(sorted(_sha256(value, name="source closure hash") for value in material))
    if len(set(checked)) != len(checked):
        raise CandidateBindingError("source_closure_hashes must not contain duplicates")
    return checked


def _identity_or_hash(value: Any, *, name: str) -> str:
    try:
        return _identity_of(value, name=name)
    except (IdentityError, TubeContractError, TypeError) as exc:
        raise CandidateBindingError(f"{name} must provide an exact SHA-256 identity") from exc


@dataclass(frozen=True, slots=True, init=False)
class AcceptedCandidateV1(Generic[ParameterT]):
    """An immutable, content-addressed candidate accepted by a producer.

    The class is intentionally sealed: callers receive a value artifact and
    a consumer must recompute its canonical payload and all expected bindings
    before using it.  The class is not a command and has no runtime feedback.
    """

    accepted: bool
    candidate_uid: str
    campaign_fingerprint: str
    ledger_root: str
    eoat_profile: str
    proxy_sha256: str
    tube_policy_sha256: str
    source_closure_hashes: tuple[str, ...]
    _parameter_items: tuple[tuple[str, float], ...] = field(repr=False, compare=True)
    _parameters_view: Mapping[str, float] = field(init=False, repr=False, compare=False)
    _canonical_sha256: str = field(init=False, repr=False, compare=False)

    def __init_subclass__(cls, **kwargs: Any) -> None:
        raise TypeError("AcceptedCandidateV1 is sealed")

    def __init__(
        self,
        accepted: bool,
        candidate_uid: str,
        parameters: Mapping[str, float],
        campaign_fingerprint: str,
        ledger_root: str | None = None,
        eoat_profile: Any = None,
        proxy: Any = None,
        tube_policy: Any = None,
        source_closure_hashes: Sequence[str] = (),
        *,
        ledger_root_sha256: str | None = None,
        eoat_profile_sha256: str | None = None,
        proxy_sha256: str | None = None,
        tube_policy_sha256: str | None = None,
    ) -> None:
        if not isinstance(accepted, bool) or not accepted:
            raise CandidateBindingError("AcceptedCandidateV1 requires accepted=true")
        identifier = _text(candidate_uid, name="candidate_uid")
        if ledger_root is not None and ledger_root_sha256 is not None and ledger_root != ledger_root_sha256:
            raise CandidateBindingError("conflicting ledger-root aliases")
        selected_ledger = ledger_root if ledger_root is not None else ledger_root_sha256
        if selected_ledger is None:
            raise CandidateBindingError("ledger_root is required")
        if eoat_profile is not None and eoat_profile_sha256 is not None:
            profile_identity = _identity_or_hash(eoat_profile, name="eoat_profile")
            if profile_identity != eoat_profile_sha256:
                raise CandidateBindingError("conflicting EOAT profile identities")
        selected_profile = eoat_profile if eoat_profile is not None else eoat_profile_sha256
        if selected_profile is None:
            raise CandidateBindingError("eoat_profile is required")
        if proxy is not None and proxy_sha256 is not None:
            proxy_identity = _identity_or_hash(proxy, name="proxy")
            if proxy_identity != proxy_sha256:
                raise CandidateBindingError("conflicting proxy identities")
        selected_proxy = proxy if proxy is not None else proxy_sha256
        if selected_proxy is None:
            raise CandidateBindingError("proxy is required")
        if tube_policy is not None and tube_policy_sha256 is not None:
            policy_identity = _identity_or_hash(tube_policy, name="tube_policy")
            if policy_identity != tube_policy_sha256:
                raise CandidateBindingError("conflicting tube-policy identities")
        selected_policy = tube_policy if tube_policy is not None else tube_policy_sha256
        if selected_policy is None:
            raise CandidateBindingError("tube_policy is required")

        checked_campaign = _sha256(campaign_fingerprint, name="campaign_fingerprint")
        checked_ledger = _sha256(selected_ledger, name="ledger_root")
        checked_profile = _identity_or_hash(selected_profile, name="eoat_profile")
        checked_proxy = _identity_or_hash(selected_proxy, name="proxy")
        checked_policy = _identity_or_hash(selected_policy, name="tube_policy")
        checked_closure = _closure_hashes(source_closure_hashes)
        checked_parameters = _parameter_items(parameters)
        object.__setattr__(self, "accepted", True)
        object.__setattr__(self, "candidate_uid", identifier)
        object.__setattr__(self, "campaign_fingerprint", checked_campaign)
        object.__setattr__(self, "ledger_root", checked_ledger)
        object.__setattr__(self, "eoat_profile", checked_profile)
        object.__setattr__(self, "proxy_sha256", checked_proxy)
        object.__setattr__(self, "tube_policy_sha256", checked_policy)
        object.__setattr__(self, "source_closure_hashes", checked_closure)
        object.__setattr__(self, "_parameter_items", checked_parameters)
        object.__setattr__(
            self,
            "_parameters_view",
            MappingProxyType(dict(checked_parameters)),
        )
        object.__setattr__(self, "_canonical_sha256", self._compute_canonical_sha256())

    @property
    def parameters(self) -> Mapping[str, float]:
        return self._parameters_view

    @property
    def parameter_items(self) -> tuple[tuple[str, float], ...]:
        return self._parameter_items

    @property
    def identity_sha256(self) -> str:
        return self._canonical_sha256

    @property
    def sha256(self) -> str:
        return self._canonical_sha256

    @property
    def canonical_sha256(self) -> str:
        return self._canonical_sha256

    @property
    def ledger_root_sha256(self) -> str:
        return self.ledger_root

    @property
    def eoat_profile_sha256(self) -> str:
        return self.eoat_profile

    def canonical_payload(self) -> dict[str, Any]:
        return {
            "schema": "ur10e.modular-tube/accepted-candidate/v1",
            "accepted": self.accepted,
            "candidate_uid": self.candidate_uid,
            "parameters": {
                key: value for key, value in self._parameter_items
            },
            "campaign_fingerprint": self.campaign_fingerprint,
            "ledger_root": self.ledger_root,
            "eoat_profile": self.eoat_profile,
            "proxy_sha256": self.proxy_sha256,
            "tube_policy_sha256": self.tube_policy_sha256,
            "source_closure_hashes": list(self.source_closure_hashes),
        }

    def canonical_bytes(self) -> bytes:
        return canonical_json_bytes(self.canonical_payload())

    def _compute_canonical_sha256(self) -> str:
        return canonical_sha256(self.canonical_payload())

    def to_artifact(self, *, include_canonical_json: bool = False) -> dict[str, Any]:
        artifact = self.canonical_payload()
        artifact["canonical_sha256"] = self._canonical_sha256
        if include_canonical_json:
            artifact["canonical_json"] = self.canonical_bytes().decode("ascii")
        return artifact

    @classmethod
    def from_artifact(cls, artifact: Mapping[str, Any]) -> "AcceptedCandidateV1[Mapping[str, float]]":
        if not isinstance(artifact, Mapping):
            raise CandidateBindingError("candidate artifact must be a mapping")
        allowed = {
            "schema",
            "accepted",
            "candidate_uid",
            "parameters",
            "campaign_fingerprint",
            "ledger_root",
            "eoat_profile",
            "proxy_sha256",
            "tube_policy_sha256",
            "source_closure_hashes",
            "canonical_sha256",
            "canonical_json",
        }
        if set(artifact) - allowed:
            raise CandidateBindingError("candidate artifact contains noncanonical fields")
        if artifact.get("schema") != "ur10e.modular-tube/accepted-candidate/v1":
            raise CandidateBindingError("candidate artifact schema mismatch")
        if artifact.get("accepted") is not True:
            raise CandidateBindingError("candidate artifact is not accepted")
        supplied_hash = artifact.get("canonical_sha256")
        if not isinstance(supplied_hash, str):
            raise CandidateBindingError("candidate artifact lacks canonical_sha256")
        payload = dict(artifact)
        payload.pop("canonical_sha256", None)
        supplied_json = payload.pop("canonical_json", None)
        canonical_bytes = canonical_json_bytes(payload)
        computed_hash = canonical_sha256(payload)
        if supplied_hash != computed_hash:
            raise IdentityError("candidate artifact canonical hash drift")
        if supplied_json is not None:
            if not isinstance(supplied_json, str) or supplied_json.encode("ascii", "strict") != canonical_bytes:
                raise CandidateBindingError("candidate artifact JSON is not canonical")
        candidate = cls(
            accepted=True,
            candidate_uid=payload.get("candidate_uid"),
            parameters=payload.get("parameters"),
            campaign_fingerprint=payload.get("campaign_fingerprint"),
            ledger_root=payload.get("ledger_root"),
            eoat_profile=payload.get("eoat_profile"),
            proxy=payload.get("proxy_sha256"),
            tube_policy=payload.get("tube_policy_sha256"),
            source_closure_hashes=payload.get("source_closure_hashes"),
        )
        if candidate.canonical_sha256 != supplied_hash:
            raise IdentityError("candidate artifact fields changed during decode")
        return candidate

    def verify_binding(
        self,
        *,
        candidate_uid: str | None = None,
        campaign_fingerprint: str | None = None,
        ledger_root: str | None = None,
        eoat_profile: Any = None,
        proxy: Any = None,
        tube_policy: Any = None,
        source_closure_hashes: Sequence[str] | None = None,
    ) -> None:
        if self.accepted is not True:
            raise CandidateBindingError("candidate is not accepted")
        recomputed = self._compute_canonical_sha256()
        if recomputed != self._canonical_sha256:
            raise IdentityError("candidate canonical hash drift")
        if candidate_uid is not None and self.candidate_uid != candidate_uid:
            raise CandidateBindingError("candidate UID binding mismatch")
        if campaign_fingerprint is not None and self.campaign_fingerprint != _sha256(campaign_fingerprint, name="campaign_fingerprint"):
            raise CandidateBindingError("campaign fingerprint binding mismatch")
        if ledger_root is not None and self.ledger_root != _sha256(ledger_root, name="ledger_root"):
            raise CandidateBindingError("ledger-root binding mismatch")
        if eoat_profile is not None and self.eoat_profile != _identity_or_hash(eoat_profile, name="eoat_profile"):
            raise CandidateBindingError("EOAT profile binding mismatch")
        if proxy is not None and self.proxy_sha256 != _identity_or_hash(proxy, name="proxy"):
            raise CandidateBindingError("proxy binding mismatch")
        if tube_policy is not None and self.tube_policy_sha256 != _identity_or_hash(tube_policy, name="tube_policy"):
            raise CandidateBindingError("tube-policy binding mismatch")
        if source_closure_hashes is not None and self.source_closure_hashes != _closure_hashes(source_closure_hashes):
            raise CandidateBindingError("source-closure binding mismatch")

    def verify(self, **kwargs: Any) -> None:
        self.verify_binding(**kwargs)


class AcceptedCandidateProducerV1(Generic[ParameterT]):
    """Producer-side seal operation; no candidate is emitted when rejected."""

    __slots__ = ()

    def seal(
        self,
        *,
        accepted: bool,
        candidate_uid: str,
        parameters: ParameterT,
        campaign_fingerprint: str,
        ledger_root: str,
        eoat_profile: Any,
        proxy: Any,
        tube_policy: Any,
        source_closure_hashes: Sequence[str],
    ) -> AcceptedCandidateV1[ParameterT]:
        return AcceptedCandidateV1(
            accepted=accepted,
            candidate_uid=candidate_uid,
            parameters=parameters,
            campaign_fingerprint=campaign_fingerprint,
            ledger_root=ledger_root,
            eoat_profile=eoat_profile,
            proxy=proxy,
            tube_policy=tube_policy,
            source_closure_hashes=source_closure_hashes,
        )

    def produce(self, **kwargs: Any) -> AcceptedCandidateV1[ParameterT]:
        return self.seal(**kwargs)


class AcceptedCandidateConsumerV1(Generic[ParameterT]):
    """Consumer-side verifier for an accepted candidate artifact."""

    __slots__ = (
        "candidate_uid",
        "campaign_fingerprint",
        "ledger_root",
        "eoat_profile",
        "proxy",
        "tube_policy",
        "source_closure_hashes",
    )

    def __init__(
        self,
        *,
        candidate_uid: str | None = None,
        campaign_fingerprint: str | None = None,
        ledger_root: str | None = None,
        eoat_profile: Any = None,
        proxy: Any = None,
        tube_policy: Any = None,
        source_closure_hashes: Sequence[str] | None = None,
    ) -> None:
        self.candidate_uid = candidate_uid
        self.campaign_fingerprint = campaign_fingerprint
        self.ledger_root = ledger_root
        self.eoat_profile = eoat_profile
        self.proxy = proxy
        self.tube_policy = tube_policy
        self.source_closure_hashes = None if source_closure_hashes is None else tuple(source_closure_hashes)

    def consume(
        self,
        candidate: AcceptedCandidateV1[ParameterT] | Mapping[str, Any],
    ) -> AcceptedCandidateV1[ParameterT]:
        selected = (
            AcceptedCandidateV1.from_artifact(candidate)
            if isinstance(candidate, Mapping)
            else candidate
        )
        if not isinstance(selected, AcceptedCandidateV1):
            raise CandidateBindingError("consumer requires AcceptedCandidateV1")
        selected.verify_binding(
            candidate_uid=self.candidate_uid,
            campaign_fingerprint=self.campaign_fingerprint,
            ledger_root=self.ledger_root,
            eoat_profile=self.eoat_profile,
            proxy=self.proxy,
            tube_policy=self.tube_policy,
            source_closure_hashes=self.source_closure_hashes,
        )
        return selected

    def accept(self, candidate: AcceptedCandidateV1[ParameterT] | Mapping[str, Any]) -> AcceptedCandidateV1[ParameterT]:
        return self.consume(candidate)


AcceptedCandidateProducer = AcceptedCandidateProducerV1
AcceptedCandidateConsumer = AcceptedCandidateConsumerV1


__all__ = [
    "AcceptedCandidateConsumer",
    "AcceptedCandidateConsumerV1",
    "AcceptedCandidateProducer",
    "AcceptedCandidateProducerV1",
    "AcceptedCandidateV1",
    "CandidateBindingError",
]
