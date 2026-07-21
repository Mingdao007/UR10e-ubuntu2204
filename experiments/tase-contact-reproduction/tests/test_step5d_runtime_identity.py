from __future__ import annotations

import hashlib
from pathlib import Path
import re
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import build_step5d_autotune_tp_v3 as builder  # noqa: E402
from step5d_autotune_v3.runtime_identity import (  # noqa: E402
    MAX_31BIT,
    RUNTIME_IDENTITY_REGISTERS,
    RuntimeIdentityError,
    bind_final_script,
    canonicalize_script_identity,
    validate_rtde_output_recipe,
)


STAMP = "2026-07-21T1200HKT_STEP5D_STRICT_RNN_AUTOTUNE_V3_R010"


def _increment_digest_hi(script: str) -> str:
    pattern = r"(global codex_step5d_runtime_digest_hi = )(\d+)"
    match = re.search(pattern, script)
    assert match is not None
    replacement = f"{match.group(1)}{int(match.group(2)) + 1}"
    return script[: match.start()] + replacement + script[match.end() :]


def test_r010_identity_round_trips_without_a_hash_fixed_point() -> None:
    script = builder.build_package_script(STAMP)
    identity, payload = bind_final_script(
        script,
        program_id=builder.PROGRAM_NAME,
        protocol_id=builder.PROTOCOL_ID,
    )
    basis, observed = canonicalize_script_identity(script)

    assert observed == (
        identity.protocol_version,
        identity.digest_hi,
        identity.digest_lo,
    )
    assert identity.script_basis_sha256 == hashlib.sha256(basis).hexdigest()
    assert payload["script_artifact_sha256"] == hashlib.sha256(
        script.encode("utf-8")
    ).hexdigest()
    assert payload["script_basis_sha256"] != payload["script_artifact_sha256"]
    assert payload["registers"] == RUNTIME_IDENTITY_REGISTERS
    assert all(0 <= value <= MAX_31BIT for value in (identity.digest_hi, identity.digest_lo))


@pytest.mark.parametrize(
    "mutation",
    [
        lambda script: script + "# semantic tamper\n",
        _increment_digest_hi,
    ],
)
def test_runtime_identity_rejects_script_or_literal_tamper(mutation) -> None:
    script = builder.build_package_script(STAMP)
    with pytest.raises(RuntimeIdentityError):
        bind_final_script(
            mutation(script),
            program_id=builder.PROGRAM_NAME,
            protocol_id=builder.PROTOCOL_ID,
        )


@pytest.mark.parametrize(
    "wrong_program",
    [
        "step5d_strict_rnn_autotune_v3_r008",
        "step5d_strict_rnn_autotune_v3_r009",
    ],
)
def test_r008_and_r009_cannot_impersonate_r010_runtime_identity(
    wrong_program: str,
) -> None:
    script = builder.build_package_script(STAMP)
    with pytest.raises(RuntimeIdentityError, match="literals differ"):
        bind_final_script(
            script,
            program_id=wrong_program,
            protocol_id=builder.PROTOCOL_ID,
        )


def test_rtde_recipe_proves_cardinality_and_int32_capability() -> None:
    fields = ["timestamp"] + [
        f"output_int_register_{index}" for index in range(24, 38)
    ]
    types = ["DOUBLE"] + ["INT32"] * 14
    observed = validate_rtde_output_recipe(fields, types)
    assert observed["output_int_register_35"] == "INT32"
    assert observed["output_int_register_37"] == "INT32"

    with pytest.raises(RuntimeIdentityError, match="cardinality"):
        validate_rtde_output_recipe(fields, types[:-1])
    wrong = list(types)
    wrong[fields.index("output_int_register_36")] = "UINT32"
    with pytest.raises(RuntimeIdentityError, match="types differ"):
        validate_rtde_output_recipe(fields, wrong)
