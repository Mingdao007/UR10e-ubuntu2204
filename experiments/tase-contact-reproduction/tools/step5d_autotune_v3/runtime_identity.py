"""Content-bound TP runtime identity carried in output integer registers 35..37.

The identity literals live in the ``.script`` they identify, so hashing the
final script and then writing that hash back into the same bytes would require
an impractical hash fixed point.  The protocol instead hashes one canonical
identity basis: the final script with only the three identity literals replaced
by fixed placeholders.  The independent release verifier performs the inverse
normalization and also verifies the ordinary SHA-256 of the exact final script.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import re
from typing import Any, Mapping


RUNTIME_IDENTITY_SCHEMA = "step5d.autotune-v3/tp-runtime-identity-v1"
RUNTIME_IDENTITY_DERIVATION_SCHEMA = (
    "step5d.autotune-v3/tp-runtime-identity-derivation-v1"
)
RUNTIME_PROTOCOL_VERSION = 1
RUNTIME_IDENTITY_REGISTERS = {
    "protocol_version": 35,
    "digest_hi": 36,
    "digest_lo": 37,
}
MAX_31BIT = (1 << 31) - 1

_PROTOCOL_PLACEHOLDER = "__STEP5D_RUNTIME_PROTOCOL_VERSION__"
_DIGEST_HI_PLACEHOLDER = "__STEP5D_RUNTIME_DIGEST_HI__"
_DIGEST_LO_PLACEHOLDER = "__STEP5D_RUNTIME_DIGEST_LO__"

_IDENTITY_ASSIGNMENTS = re.compile(
    r"^global codex_step5d_runtime_protocol_version = (?P<protocol>\d+)\n"
    r"global codex_step5d_runtime_digest_hi = (?P<digest_hi>\d+)\n"
    r"global codex_step5d_runtime_digest_lo = (?P<digest_lo>\d+)$",
    flags=re.MULTILINE,
)


class RuntimeIdentityError(RuntimeError):
    """The generated script or declared runtime identity is inconsistent."""


def _sha256_text(value: Any, role: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise RuntimeIdentityError(f"{role} must be a lowercase SHA-256")
    return value


def _strict_segment(value: Any, role: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= MAX_31BIT:
        raise RuntimeIdentityError(f"{role} must be an unsigned 31-bit integer")
    return value


def canonical_derivation_bytes(
    *, program_id: str, protocol_id: str, script_identity_basis_sha256: str
) -> bytes:
    if not isinstance(program_id, str) or not program_id:
        raise RuntimeIdentityError("runtime identity program_id is missing")
    if not isinstance(protocol_id, str) or not protocol_id:
        raise RuntimeIdentityError("runtime identity protocol_id is missing")
    basis_sha256 = _sha256_text(
        script_identity_basis_sha256, "script identity basis SHA-256"
    )
    return json.dumps(
        {
            "program_id": program_id,
            "protocol_id": protocol_id,
            "schema": RUNTIME_IDENTITY_DERIVATION_SCHEMA,
            "script_identity_basis_sha256": basis_sha256,
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("ascii")


@dataclass(frozen=True)
class TpRuntimeIdentity:
    program_id: str
    protocol_id: str
    protocol_version: int
    digest_hi: int
    digest_lo: int
    script_basis_sha256: str

    def __post_init__(self) -> None:
        if not isinstance(self.program_id, str) or not self.program_id:
            raise RuntimeIdentityError("runtime identity program_id is missing")
        if not isinstance(self.protocol_id, str) or not self.protocol_id:
            raise RuntimeIdentityError("runtime identity protocol_id is missing")
        if self.protocol_version != RUNTIME_PROTOCOL_VERSION:
            raise RuntimeIdentityError("runtime identity protocol version differs")
        _strict_segment(self.digest_hi, "runtime identity digest_hi")
        _strict_segment(self.digest_lo, "runtime identity digest_lo")
        _sha256_text(
            self.script_basis_sha256,
            "runtime identity script basis SHA-256",
        )

    @property
    def register_values(self) -> dict[int, int]:
        return {
            RUNTIME_IDENTITY_REGISTERS["protocol_version"]: self.protocol_version,
            RUNTIME_IDENTITY_REGISTERS["digest_hi"]: self.digest_hi,
            RUNTIME_IDENTITY_REGISTERS["digest_lo"]: self.digest_lo,
        }

    def manifest_payload(self, *, script_artifact_sha256: str) -> dict[str, Any]:
        return {
            "schema": RUNTIME_IDENTITY_SCHEMA,
            "program_id": self.program_id,
            "protocol_id": self.protocol_id,
            "protocol_version": self.protocol_version,
            "digest_hi": self.digest_hi,
            "digest_lo": self.digest_lo,
            "script_basis_sha256": self.script_basis_sha256,
            "script_artifact_sha256": _sha256_text(
                script_artifact_sha256,
                "runtime identity script artifact SHA-256",
            ),
            "registers": dict(RUNTIME_IDENTITY_REGISTERS),
        }


def derive_runtime_identity(
    *, program_id: str, protocol_id: str, script_identity_basis: bytes
) -> TpRuntimeIdentity:
    basis_sha256 = hashlib.sha256(script_identity_basis).hexdigest()
    digest = hashlib.sha256(
        canonical_derivation_bytes(
            program_id=program_id,
            protocol_id=protocol_id,
            script_identity_basis_sha256=basis_sha256,
        )
    ).digest()
    first_62_bits = int.from_bytes(digest[:8], "big") >> 2
    return TpRuntimeIdentity(
        program_id=program_id,
        protocol_id=protocol_id,
        protocol_version=RUNTIME_PROTOCOL_VERSION,
        digest_hi=(first_62_bits >> 31) & MAX_31BIT,
        digest_lo=first_62_bits & MAX_31BIT,
        script_basis_sha256=basis_sha256,
    )


def runtime_identity_assignment_block(
    identity: TpRuntimeIdentity | None,
) -> str:
    if identity is None:
        protocol: int | str = _PROTOCOL_PLACEHOLDER
        digest_hi: int | str = _DIGEST_HI_PLACEHOLDER
        digest_lo: int | str = _DIGEST_LO_PLACEHOLDER
    else:
        protocol = identity.protocol_version
        digest_hi = identity.digest_hi
        digest_lo = identity.digest_lo
    return (
        f"global codex_step5d_runtime_protocol_version = {protocol}\n"
        f"global codex_step5d_runtime_digest_hi = {digest_hi}\n"
        f"global codex_step5d_runtime_digest_lo = {digest_lo}"
    )


def canonicalize_script_identity(script: str) -> tuple[bytes, tuple[int, int, int]]:
    matches = list(_IDENTITY_ASSIGNMENTS.finditer(script))
    if len(matches) != 1:
        raise RuntimeIdentityError(
            "TP script must contain exactly one runtime identity assignment block"
        )
    for symbol in (
        "codex_step5d_runtime_protocol_version",
        "codex_step5d_runtime_digest_hi",
        "codex_step5d_runtime_digest_lo",
    ):
        assignments = re.findall(
            rf"^(?:global\s+)?{re.escape(symbol)}\s*=",
            script,
            flags=re.MULTILINE,
        )
        if len(assignments) != 1:
            raise RuntimeIdentityError(
                f"TP script must assign runtime identity constant once: {symbol}"
            )
    match = matches[0]
    values = tuple(
        _strict_segment(int(match.group(name)), f"script runtime identity {name}")
        for name in ("protocol", "digest_hi", "digest_lo")
    )
    canonical = (
        script[: match.start()]
        + runtime_identity_assignment_block(None)
        + script[match.end() :]
    )
    return canonical.encode("utf-8"), values


def bind_final_script(
    script: str, *, program_id: str, protocol_id: str
) -> tuple[TpRuntimeIdentity, dict[str, Any]]:
    basis, observed = canonicalize_script_identity(script)
    identity = derive_runtime_identity(
        program_id=program_id,
        protocol_id=protocol_id,
        script_identity_basis=basis,
    )
    expected = (
        identity.protocol_version,
        identity.digest_hi,
        identity.digest_lo,
    )
    if observed != expected:
        raise RuntimeIdentityError(
            "TP script runtime identity literals differ from the canonical identity basis"
        )
    script_sha256 = hashlib.sha256(script.encode("utf-8")).hexdigest()
    return identity, identity.manifest_payload(
        script_artifact_sha256=script_sha256
    )


def identity_from_manifest(payload: Mapping[str, Any]) -> tuple[TpRuntimeIdentity, str]:
    required = {
        "schema",
        "program_id",
        "protocol_id",
        "protocol_version",
        "digest_hi",
        "digest_lo",
        "script_basis_sha256",
        "script_artifact_sha256",
        "registers",
    }
    if set(payload) != required or payload.get("schema") != RUNTIME_IDENTITY_SCHEMA:
        raise RuntimeIdentityError("runtime identity manifest fields or schema differ")
    if payload.get("registers") != RUNTIME_IDENTITY_REGISTERS:
        raise RuntimeIdentityError("runtime identity register map differs")
    identity = TpRuntimeIdentity(
        program_id=payload["program_id"],
        protocol_id=payload["protocol_id"],
        protocol_version=payload["protocol_version"],
        digest_hi=payload["digest_hi"],
        digest_lo=payload["digest_lo"],
        script_basis_sha256=payload["script_basis_sha256"],
    )
    return identity, _sha256_text(
        payload["script_artifact_sha256"],
        "runtime identity script artifact SHA-256",
    )


def validate_rtde_output_recipe(
    fields: list[str] | tuple[str, ...],
    type_names: list[str] | tuple[str, ...],
) -> dict[str, str]:
    """Prove recipe cardinality and INT32 support for output registers 24..37."""

    if len(fields) != len(type_names):
        raise RuntimeIdentityError("RTDE output recipe fields/type cardinality differs")
    if len(set(fields)) != len(fields):
        raise RuntimeIdentityError("RTDE output recipe repeats a field")
    required = [f"output_int_register_{index}" for index in range(24, 38)]
    missing = [field for field in required if field not in fields]
    if missing:
        raise RuntimeIdentityError(
            f"RTDE output recipe lacks integer registers 24..37: {missing}"
        )
    observed = {field: type_names[fields.index(field)] for field in required}
    wrong_types = {
        field: type_name
        for field, type_name in observed.items()
        if type_name != "INT32"
    }
    if wrong_types:
        raise RuntimeIdentityError(
            f"RTDE output recipe integer register types differ: {wrong_types}"
        )
    return observed


__all__ = [
    "MAX_31BIT",
    "RUNTIME_IDENTITY_DERIVATION_SCHEMA",
    "RUNTIME_IDENTITY_REGISTERS",
    "RUNTIME_IDENTITY_SCHEMA",
    "RUNTIME_PROTOCOL_VERSION",
    "RuntimeIdentityError",
    "TpRuntimeIdentity",
    "bind_final_script",
    "canonical_derivation_bytes",
    "canonicalize_script_identity",
    "derive_runtime_identity",
    "identity_from_manifest",
    "runtime_identity_assignment_block",
    "validate_rtde_output_recipe",
]
