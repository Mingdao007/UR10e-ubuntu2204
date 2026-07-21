from __future__ import annotations

import hashlib
from pathlib import Path
import re
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import build_step5d_autotune_tp_v3 as builder  # noqa: E402
from step5d_autotune_contract import ExecutionProfile  # noqa: E402
from step5d_autotune_live_driver import (  # noqa: E402
    MailboxError,
    execution_profile_id_for,
)
from step5d_autotune_supervisor import execution_profile_integer_id  # noqa: E402
from step5d_autotune_v3.runtime_identity import (  # noqa: E402
    MAX_31BIT,
    RUNTIME_IDENTITY_REGISTERS,
    RuntimeIdentityError,
    bind_final_script,
    canonicalize_script_identity,
    validate_rtde_output_recipe,
)
from step5d_autotune_v3.runtime_gate import (  # noqa: E402
    RuntimeGateError,
    validate_tp_runtime_identity,
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


def test_ordered_identity_register_publication_is_fail_closed_until_complete() -> None:
    script = builder.build_package_script(STAMP)
    identity, manifest_identity = bind_final_script(
        script,
        program_id=builder.PROGRAM_NAME,
        protocol_id=builder.PROTOCOL_ID,
    )
    start = script.index("def codex_step5d_publish_runtime_identity():")
    end = script.index("\nend", start)
    publisher = script[start:end]
    write_order = tuple(
        int(register)
        for register in re.findall(
            r"write_output_integer_register\((\d+),", publisher
        )
    )
    assert write_order == (35, 36, 37)

    expected = identity.register_values
    observed = {
        f"output_int_register_{register}": 0 if value != 0 else 1
        for register, value in expected.items()
    }
    for committed_count, next_register in ((0, 35), (1, 36), (2, 37)):
        for register in write_order[:committed_count]:
            observed[f"output_int_register_{register}"] = expected[register]
        with pytest.raises(
            RuntimeGateError,
            match=rf"mismatch at output_int_register_{next_register}$",
        ):
            validate_tp_runtime_identity(observed, manifest_identity)

    observed["output_int_register_37"] = expected[37]
    validate_tp_runtime_identity(observed, manifest_identity)


def test_host_network_profile_domain_exactly_matches_tp_acceptance_domain() -> None:
    script = builder.build_package_script(STAMP)
    start = script.index("def codex_autotune_network_profile_valid(")
    end = script.index("\nend", start) + len("\nend")
    tp_predicate = script[start:end]
    assert tp_predicate == "\n".join(
        (
            "def codex_autotune_network_profile_valid(execution_profile_id):",
            "  local normal_level = floor(execution_profile_id / 100.0)",
            "  local remainder = execution_profile_id - 100 * normal_level",
            "  local host_slew_level = floor(remainder / 10.0)",
            "  local tp_accel_level = remainder - 10 * host_slew_level",
            "  return (normal_level >= 1 and normal_level <= 3 or normal_level == 5 or normal_level == 6) and host_slew_level >= 1 and host_slew_level <= 3 and tp_accel_level >= 1 and tp_accel_level <= 3",
            "end",
        )
    )

    tp_domain = {
        profile_id
        for profile_id in range(100, 1000)
        if (
            profile_id // 100 in {1, 2, 3, 5, 6}
            and (profile_id // 10) % 10 in {1, 2, 3}
            and profile_id % 10 in {1, 2, 3}
        )
    }
    host_domain: set[int] = set()
    for normal in (0.010, 0.015, 0.020, 0.050, 0.100):
        for host_slew in (0.1, 0.2, 0.5):
            for tp_accel in (0.1, 0.2, 0.5):
                profile = ExecutionProfile(
                    (
                        f"nf{round(normal * 1000):03d}"
                        f"-slew{round(host_slew * 100):03d}"
                        f"-a{round(tp_accel * 100):03d}"
                    ),
                    normal,
                    host_slew,
                    tp_accel,
                )
                encoded = execution_profile_id_for(profile, network_mode=True)
                assert execution_profile_integer_id(profile) == encoded
                host_domain.add(encoded)

    assert host_domain == tp_domain
    assert len(host_domain) == 45
    assert 633 in host_domain and 633 in tp_domain
    offline = ExecutionProfile("nf030-offline", 0.030, live_eligible=False)
    assert execution_profile_integer_id(offline) == 411
    assert 411 not in tp_domain
    with pytest.raises(MailboxError, match="offline_only"):
        execution_profile_id_for(offline, network_mode=True)
