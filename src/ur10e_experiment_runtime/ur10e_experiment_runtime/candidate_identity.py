"""Single owner for Step5d parameter/control/occurrence/transport identities.

Active rolling artifacts persist domain-prefixed identifiers.  The explicit
``from_legacy`` path exists only so historical evidence can still be decoded;
active producers must use the material-owning factories below.
"""

from __future__ import annotations

import hashlib
import json
import math
from typing import Any, Mapping


CONTROL_FIELDS = (
    "force_p_gain",
    "force_i_gain",
    "force_damping",
    "orientation_ko",
)


def _canonical_digest(material: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        material,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("ascii")
    return hashlib.sha256(encoded).hexdigest()


def _lower_sha256(value: Any, *, name: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ValueError(f"{name} must be a lowercase SHA-256")
    return value


def _finite(value: Any, *, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be numeric")
    result = float(value)
    if not math.isfinite(result):
        raise ValueError(f"{name} must be finite")
    return result


class _DomainUid(str):
    prefix = "uid:v1:"
    _factory_token = object()

    def __new__(cls, value: str, *, _factory: object | None = None) -> "_DomainUid":
        if _factory is not cls._factory_token:
            raise TypeError(
                f"{cls.__name__} cannot wrap a raw digest; use its material factory or parse()"
            )
        return str.__new__(cls, value)

    @classmethod
    def _from_digest(cls, digest: str) -> "_DomainUid":
        checked = _lower_sha256(digest, name=cls.__name__)
        return cls(cls.prefix + checked, _factory=cls._factory_token)

    @classmethod
    def from_legacy(cls, digest: str) -> "_DomainUid":
        """Decode one historical raw digest without making it active truth."""

        checked = _lower_sha256(digest, name=f"legacy {cls.__name__}")
        return cls(checked, _factory=cls._factory_token)

    @classmethod
    def parse(cls, value: Any, *, allow_legacy: bool = False) -> "_DomainUid":
        if not isinstance(value, str):
            raise ValueError(f"{cls.__name__} must be a string")
        if value.startswith(cls.prefix):
            digest = value[len(cls.prefix) :]
            _lower_sha256(digest, name=cls.__name__)
            return cls(value, _factory=cls._factory_token)
        if allow_legacy:
            return cls.from_legacy(value)
        raise ValueError(f"{cls.__name__} must use the {cls.prefix!r} domain prefix")

    @property
    def is_legacy(self) -> bool:
        return not self.startswith(self.prefix)

    @property
    def digest(self) -> str:
        return str(self) if self.is_legacy else self[len(self.prefix) :]


class ParameterUid(_DomainUid):
    prefix = "parameter:v1:"

    @classmethod
    def from_candidate_digest(cls, candidate_digest: str) -> "ParameterUid":
        return cls._from_digest(candidate_digest)  # type: ignore[return-value]


class ControlCandidateUid(_DomainUid):
    prefix = "control:v2:"

    @classmethod
    def from_overlay(cls, overlay: Mapping[str, Any]) -> "ControlCandidateUid":
        values = {
            field: _finite(overlay[field], name=field) for field in CONTROL_FIELDS
        }
        return cls._from_digest(  # type: ignore[return-value]
            _canonical_digest(
                {
                    "schema": "step5d.autotune-v3/control-candidate/v2",
                    **values,
                }
            )
        )


class OccurrenceUid(_DomainUid):
    prefix = "occurrence:v2:"

    @classmethod
    def from_control(
        cls,
        control_uid: ControlCandidateUid,
        *,
        protocol: str,
        logical_batch_sequence: int,
        row_index: int,
        plan_revision: int,
        selection_role: str,
        replicate_ordinal: int,
    ) -> "OccurrenceUid":
        if type(control_uid) is not ControlCandidateUid or control_uid.is_legacy:
            raise TypeError("active occurrence requires a domain-prefixed control UID")
        return cls._from_digest(  # type: ignore[return-value]
            _canonical_digest(
                {
                    "schema": "step5d.autotune-v3/occurrence/v2",
                    "protocol": protocol,
                    "logical_batch_sequence": logical_batch_sequence,
                    "row_index": row_index,
                    "plan_revision": plan_revision,
                    "selection_role": selection_role,
                    "replicate_ordinal": replicate_ordinal,
                    "control_candidate_uid": str(control_uid),
                }
            )
        )


class TransportCandidateUid(_DomainUid):
    prefix = "transport:v2:"

    @classmethod
    def from_occurrence(
        cls,
        occurrence_uid: OccurrenceUid,
        *,
        parameter_uid: ParameterUid,
        protocol: str,
    ) -> "TransportCandidateUid":
        if type(occurrence_uid) is not OccurrenceUid or occurrence_uid.is_legacy:
            raise TypeError("active transport requires a domain-prefixed occurrence UID")
        if type(parameter_uid) is not ParameterUid or parameter_uid.is_legacy:
            raise TypeError("active transport requires a domain-prefixed parameter UID")
        return cls._from_digest(  # type: ignore[return-value]
            _canonical_digest(
                {
                    "schema": "step5d.autotune-v3/transport-candidate/v2",
                    "protocol": protocol,
                    "occurrence_uid": str(occurrence_uid),
                    "parameter_uid": str(parameter_uid),
                }
            )
        )


__all__ = [
    "CONTROL_FIELDS",
    "ControlCandidateUid",
    "OccurrenceUid",
    "ParameterUid",
    "TransportCandidateUid",
]
