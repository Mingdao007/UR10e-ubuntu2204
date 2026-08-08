"""Focused offline acceptance tests for the isolated R009 reason-43 scope."""

from __future__ import annotations

import hashlib
import re
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from build_step5d_autotune_v4_r009 import build as build_r009  # noqa: E402
from step5d_autotune_v4_r009.diagnostics import (  # noqa: E402
    R009DiagnosticError,
    R009DiagnosticRecorder,
)
from step5d_autotune_v4_r009.fake_rtde import (  # noqa: E402
    FakeRTDE,
    R009TerminalSubtypeLatch,
    R009TPPropagation,
)
from step5d_autotune_v4_r009.freshness import (  # noqa: E402
    PACKET_FRESHNESS_FAILURE_REASON,
    PACKET_STALE_S,
    Reason43Subtype,
    R009FreshnessError,
    validate_reason43_diagnostic,
)
from step5d_autotune_v4_r009.identity import (  # noqa: E402
    R009IdentityError,
    R009SourceSet,
    build_behavior_manifest_from_r006,
    default_source_set,
)
from step5d_autotune_v4_r009.contracts import (  # noqa: E402
    build_contract,
    validate_contract,
)
from step5d_autotune_v4_r009.transport import (  # noqa: E402
    OUTPUT_DOUBLE_FIELDS,
    OUTPUT_INTEGER_FIELDS,
    R009TransportError,
    parse_output,
    validate_output_recipe,
)
from step5d_autotune_v4_r009.tp import (  # noqa: E402
    numeric_sanity,
    render_script,
    validate_urscript_block_balance,
)


def _manifest():
    return build_behavior_manifest_from_r006(source_set=default_source_set())


def test_newer_and_exact_equal_packets_are_accepted_with_subtype_zero() -> None:
    newer = FakeRTDE.run_case("newer")
    exact = FakeRTDE.run_case("exact_equal")
    assert newer.accepted and not newer.stopped
    assert newer.reason_code == 0
    assert newer.reason43_subtype is Reason43Subtype.NONE
    assert newer.cache_updated and not newer.reused_cached_payload
    assert exact.accepted and not exact.stopped
    assert exact.reason_code == 0
    assert exact.reason43_subtype is Reason43Subtype.NONE
    assert not exact.cache_updated and exact.reused_cached_payload
    assert exact.observed_packet_sequence == exact.cached_packet_sequence == 7
    assert exact.cache_age_s < PACKET_STALE_S


@pytest.mark.parametrize(
    ("case", "subtype"),
    [
        ("sequence_regression", Reason43Subtype.SEQUENCE_REGRESSION),
        (
            "equal_sequence_payload_changed",
            Reason43Subtype.EQUAL_SEQUENCE_PAYLOAD_CHANGED,
        ),
        ("held_age_timeout", Reason43Subtype.HELD_AGE_TIMEOUT),
    ],
)
def test_each_reason43_branch_is_independently_produced(case: str, subtype: Reason43Subtype) -> None:
    decision = FakeRTDE.run_case(case)
    assert not decision.accepted and decision.stopped
    assert decision.reason_code == PACKET_FRESHNESS_FAILURE_REASON
    assert decision.reason43_subtype is subtype
    assert decision.observed_packet_sequence is not None
    assert decision.cached_packet_sequence is not None
    assert decision.diagnostic["stopped"] is True
    assert decision.diagnostic["cache_updated"] is False


def test_r009_parser_accepts_all_branches_and_rejects_diagnostic_drift() -> None:
    for case in (
        "newer",
        "exact_equal",
        "sequence_regression",
        "equal_sequence_payload_changed",
        "held_age_timeout",
    ):
        decision = FakeRTDE.run_case(case)
        snapshot = parse_output(FakeRTDE.output_mapping(decision), observed_at_s=1.0)
        assert snapshot.reason43_subtype is decision.reason43_subtype
        assert snapshot.consumed_packet_sequence == snapshot.cached_packet_sequence

    normal = FakeRTDE.output_mapping(FakeRTDE.run_case("exact_equal"))
    normal["output_int_register_35"] = 1
    with pytest.raises(R009TransportError):
        parse_output(normal)

    missing = FakeRTDE.output_mapping(FakeRTDE.run_case("sequence_regression"))
    missing.pop("output_double_register_25")
    with pytest.raises(R009TransportError):
        parse_output(missing)

    unknown = FakeRTDE.output_mapping(FakeRTDE.run_case("sequence_regression"))
    unknown["output_int_register_35"] = 9
    with pytest.raises(R009TransportError):
        parse_output(unknown)

    inconsistent = FakeRTDE.output_mapping(FakeRTDE.run_case("sequence_regression"))
    inconsistent["output_double_register_25"] = inconsistent["output_double_register_26"]
    with pytest.raises(R009TransportError):
        parse_output(inconsistent)

    age_inconsistent = FakeRTDE.output_mapping(FakeRTDE.run_case("held_age_timeout"))
    age_inconsistent["output_double_register_27"] = PACKET_STALE_S - 0.001
    with pytest.raises(R009TransportError):
        parse_output(age_inconsistent)


def test_all_three_reason43_branches_are_parsed_and_recorded_in_event_order() -> None:
    recorder = R009DiagnosticRecorder(max_events=4)
    snapshots = []
    for index, case in enumerate(
        (
            "sequence_regression",
            "equal_sequence_payload_changed",
            "held_age_timeout",
        )
    ):
        decision = FakeRTDE.run_case(case)
        snapshot = parse_output(FakeRTDE.output_mapping(decision), observed_at_s=10.0 + index)
        recorder.record_output(snapshot, timestamp_s=10.0 + index)
        snapshots.append(snapshot)
    assert [int(item.reason43_subtype) for item in snapshots] == [1, 2, 3]
    assert [
        event.payload["reason43_subtype"]
        for event in recorder.snapshot().events
    ] == [1, 2, 3]


def test_r009_output_recipe_owns_extended_registers_without_r004_collision() -> None:
    assert OUTPUT_DOUBLE_FIELDS == {
        24: "consumed_or_cached_packet_sequence",
        25: "observed_packet_sequence",
        26: "cached_packet_sequence",
        27: "cache_age_seconds",
    }
    assert OUTPUT_INTEGER_FIELDS[35] == "reason43_subtype"
    validate_output_recipe(
        [
            "DOUBLE",
            "DOUBLE",
            "VECTOR3D",
            "VECTOR6D",
            "VECTOR6D",
            "VECTOR6D",
            "VECTOR6D",
            "VECTOR6D",
            "INT32",
            "INT32",
            "INT32",
            "DOUBLE",
            "DOUBLE",
            "DOUBLE",
            "DOUBLE",
            *(["INT32"] * 12),
        ]
    )


def test_r009_tp_render_contains_distinct_subtypes_and_extended_publication() -> None:
    script = render_script(_manifest())
    validate_urscript_block_balance(script)
    sanity = numeric_sanity(script)
    assert sanity["passed"]
    assert script.count("codex_r009_reason43_subtype = 1") == 1
    assert script.count("codex_r009_reason43_subtype = 2") == 1
    assert script.count("codex_r009_reason43_subtype = 3") == 1
    for marker in (
        "write_output_float_register(24, codex_r009_cache_sequence)",
        "write_output_float_register(25, codex_r009_observed_sequence)",
        "write_output_float_register(26, codex_r009_cache_sequence)",
        "write_output_float_register(27, codex_r009_cache_age_s)",
        "write_output_integer_register(35, codex_r009_reason43_subtype)",
    ):
        assert marker in script
    assert "R009_BEHAVIOR_MANIFEST_SHA256:" in script
    assert "R009_CAMPAIGN_FINGERPRINT:" in script
    assert "V4_CONTRACT_SHA256:" not in script
    assert "r006" not in script.lower()


def test_r009_tp_reason43_latch_uses_one_guarded_first_write_seam() -> None:
    script = render_script(_manifest())
    helper = (
        "def codex_r009_latch_reason43_subtype(subtype):\n"
        "  if codex_r009_terminal_reason43_subtype == 0:\n"
        "    codex_r009_terminal_reason43_subtype = subtype\n"
        "  end\n"
        "end\n"
    )
    assert helper in script
    assert script.count("codex_r009_latch_reason43_subtype(") == 4
    for subtype in (1, 2, 3):
        assert (
            f"codex_r009_reason43_subtype = {subtype}\n"
            f"    codex_r009_latch_reason43_subtype({subtype})"
            in script
        )
        assert f"codex_r009_terminal_reason43_subtype = {subtype}" not in script


def test_r009_terminal_subtype_latch_is_first_fault_wins_until_non43_reset() -> None:
    for first_subtype in (
        Reason43Subtype.SEQUENCE_REGRESSION,
        Reason43Subtype.EQUAL_SEQUENCE_PAYLOAD_CHANGED,
    ):
        latch = R009TerminalSubtypeLatch()
        assert latch.observe(43, first_subtype) is first_subtype
        assert latch.observe(43, Reason43Subtype.HELD_AGE_TIMEOUT) is first_subtype
        assert latch.instantaneous_subtype is Reason43Subtype.HELD_AGE_TIMEOUT
        assert latch.terminal_subtype is first_subtype
        assert latch.observe(4, Reason43Subtype.NONE) is Reason43Subtype.NONE
        assert latch.terminal_subtype is Reason43Subtype.NONE
        assert latch.observe(43, Reason43Subtype.HELD_AGE_TIMEOUT) is Reason43Subtype.HELD_AGE_TIMEOUT

    normal = parse_output(FakeRTDE.output_mapping(FakeRTDE.run_case("exact_equal")))
    assert normal.reason43_subtype is Reason43Subtype.NONE


def test_r009_tp_resident_packet_fault_preserves_stop_priority_and_reason43() -> None:
    script = render_script(_manifest())
    resident = script[script.index("def step5d_strict_rnn_autotune_v4_r009():") :]
    stop_index = resident.index(
        "if session_command == 3 and session_sequence > consumed_session_sequence:"
    )
    packet_index = resident.index("elif packet_reason != 0:", stop_index)
    integer_index = resident.index("elif integer_reason != 0", packet_index)
    arm_index = resident.index("elif not session_active", integer_index)
    assert stop_index < packet_index < integer_index < arm_index
    packet_branch = resident[packet_index:integer_index]
    assert "stopl(0.250000000)" in packet_branch
    assert "state = 90\n      reason = packet_reason" in packet_branch
    assert "completed = False" in packet_branch


def test_r009_tp_stationary_call_sites_preserve_guard_or_use_timeout_fallback() -> None:
    script = render_script(_manifest())
    sites = re.findall(
        r"(?m)^\s+(?:if|elif) not codex_r009_stationary\(0\.250000000\):\n"
        r"\s+return codex_r009_(?:fault|return_fault)\([^\n]*"
        r"codex_r009_stationary_failure_reason\((\d+)\)",
        script,
    )
    assert sorted(int(value) for value in sites) == [11, 58, 58, 58, 59, 59, 60]
    assert "codex_r009_stationary_reason = 0\n  local dwell_s" in script
    assert "codex_r009_stationary_reason = guard\n      return False" in script
    assert "if guard != 0 or not codex_r009_stationary" not in script
    assert (
        "if guard != 0:\n"
        "    return codex_r009_return_fault(epoch, ordinal, token, kind, consumed, guard,"
        in script
    )


@pytest.mark.parametrize(
    ("case", "subtype"),
    [
        ("sequence_regression", Reason43Subtype.SEQUENCE_REGRESSION),
        (
            "equal_sequence_payload_changed",
            Reason43Subtype.EQUAL_SEQUENCE_PAYLOAD_CHANGED,
        ),
        ("held_age_timeout", Reason43Subtype.HELD_AGE_TIMEOUT),
    ],
)
def test_r009_tp_resident_and_stationary_reason43_mappings_parse_strictly(
    case: str, subtype: Reason43Subtype
) -> None:
    decision = FakeRTDE.run_case(case)
    for stationary in (False, True):
        propagation = R009TPPropagation.from_decision(
            decision,
            stationary=stationary,
        )
        snapshot = parse_output(
            FakeRTDE.output_mapping(
                decision,
                state=propagation.state,
                terminal_reason=propagation.terminal_reason,
            ),
            observed_at_s=2.0,
        )
        assert snapshot.state == 90
        assert snapshot.terminal_reason == PACKET_FRESHNESS_FAILURE_REASON
        assert snapshot.reason43_subtype is subtype


@pytest.mark.parametrize("fallback_reason", (11, 58, 59, 60))
def test_r009_tp_stationary_timeout_keeps_legacy_reason_with_subtype_zero(
    fallback_reason: int,
) -> None:
    decision = FakeRTDE.run_case("exact_equal")
    propagation = R009TPPropagation.from_decision(
        decision,
        stationary=True,
        stationary_fallback_reason=fallback_reason,
    )
    snapshot = parse_output(
        FakeRTDE.output_mapping(
            decision,
            state=propagation.state,
            terminal_reason=propagation.terminal_reason,
        ),
        observed_at_s=2.0,
    )
    assert snapshot.state == 90
    assert snapshot.terminal_reason == fallback_reason
    assert snapshot.reason43_subtype is Reason43Subtype.NONE


def test_r009_builder_writes_only_tmp_triplet_and_differs_from_r006(tmp_path: Path) -> None:
    result = build_r009(tmp_path, behavior_manifest=_manifest())
    assert result["controller_upload"] is False
    assert result["controller_readback"] is False
    for suffix in (".script", ".txt", ".urp"):
        path = tmp_path / f"step5d_strict_rnn_autotune_v4_r009{suffix}"
        assert path.is_file()
    r006_dir = ROOT / "programs/step5/step5d"
    for suffix in (".script", ".txt", ".urp"):
        r009_digest = hashlib.sha256(
            (tmp_path / f"step5d_strict_rnn_autotune_v4_r009{suffix}").read_bytes()
        ).hexdigest()
        r006_digest = hashlib.sha256(
            (r006_dir / f"step5d_strict_rnn_autotune_v4_r006{suffix}").read_bytes()
        ).hexdigest()
        assert r009_digest != r006_digest
    assert set(result["artifacts"]) >= {
        ".script",
        ".txt",
        ".urp",
        ".numeric-sanity.json",
        ".deploy-manifest.json",
    }


def test_r009_load_validate_path_does_not_write_repository_artifacts(tmp_path: Path) -> None:
    sentinel = ROOT / "config/step5d/current.json"
    before = sentinel.read_bytes()
    before_stat = sentinel.stat()
    manifest = _manifest()
    # Building and validating a contract in memory is the only identity
    # operation here; no R009 contract or identity persistence is invoked.
    bundle = build_contract(behavior_manifest=manifest)
    assert validate_contract(bundle).sha256 == bundle.sha256
    assert manifest.campaign_fingerprint == manifest.behavior_manifest_sha256
    assert sentinel.read_bytes() == before
    assert sentinel.stat().st_mtime_ns == before_stat.st_mtime_ns
    assert not list(tmp_path.iterdir())


def test_r009_diagnostic_recorder_preserves_order_and_is_deterministic() -> None:
    recorder = R009DiagnosticRecorder(max_events=8)
    recorder.record_packet_publish(1.0, 7)
    recorder.record_rtde_poll(1.1, (3, 4))
    recorder.record_tp_output(
        1.2,
        state=90,
        terminal_reason=43,
        reason43_subtype=Reason43Subtype.SEQUENCE_REGRESSION,
        observed_packet_sequence=6,
        cached_packet_sequence=7,
        cache_age_s=0.01,
        consumed_packet_sequence=7,
    )
    recorder.record_transport_close(1.3, "rtde")
    recorder.record_socket_close(1.4, "rtde")
    first = recorder.serialize()
    second = recorder.snapshot().serialize()
    assert first == second
    snapshot = recorder.snapshot()
    assert [event.kind for event in snapshot.events] == [
        "packet_publish",
        "rtde_poll",
        "tp_output",
        "transport_close",
        "socket_close",
    ]
    assert snapshot.last_packet_publish is not None
    assert snapshot.last_packet_publish.sequence == 7
    assert snapshot.last_rtde_poll is not None
    assert snapshot.last_rtde_poll.frame_identity == (3, 4)
    assert snapshot.last_tp_output is not None
    assert snapshot.last_tp_output.reason43_subtype is Reason43Subtype.SEQUENCE_REGRESSION
    assert snapshot.close_order == ("transport:rtde", "socket:rtde")

    with pytest.raises(R009DiagnosticError):
        recorder.record_packet_publish(1.5, 7)


def test_r009_diagnostic_recorder_rejects_inconsistent_rows_and_close_order() -> None:
    recorder = R009DiagnosticRecorder()
    with pytest.raises(R009DiagnosticError):
        recorder.record_tp_output(
            1.0,
            state=90,
            terminal_reason=43,
            reason43_subtype=0,
            observed_packet_sequence=7,
            cached_packet_sequence=7,
            cache_age_s=0.0,
            consumed_packet_sequence=7,
        )
    recorder.record_packet_publish(1.0, 1)
    with pytest.raises(R009DiagnosticError):
        recorder.record_rtde_poll(0.9, (1, 1))
    with pytest.raises(R009DiagnosticError):
        recorder.record_socket_close(1.1, "rtde")


def test_r009_behavior_source_set_changes_campaign_and_excludes_generated_contracts() -> None:
    source_set = default_source_set()
    baseline = _manifest()
    for relative in (
        "tools/step5d_autotune_v4_r009/freshness.py",
        "tools/step5d_autotune_v4_r009/tp.py",
    ):
        changed = dict(source_set.files)
        behavior_path = ROOT / relative
        changed[relative] = hashlib.sha256(
            behavior_path.read_bytes() + b"\n# offline byte drift\n"
        ).hexdigest()
        drifted = build_behavior_manifest_from_r006(source_set=changed)
        assert baseline.campaign_fingerprint != drifted.campaign_fingerprint
    assert "config/step5d/autotune_v4_r009.json" not in source_set.files
    assert "config/step5d/autotune_v4_r009.release-identity.json" not in source_set.files
    with pytest.raises(R009IdentityError):
        R009SourceSet.from_mapping(
            {
                "schema": source_set.schema,
                "files": {
                    "config/step5d/autotune_v4_r009.json": "a" * 64,
                    "tools/behavior.py": "b" * 64,
                },
                "sha256": "c" * 64,
            }
        )


def test_reason43_validator_rejects_unknown_or_inconsistent_subtypes() -> None:
    with pytest.raises(R009FreshnessError):
        validate_reason43_diagnostic(
            terminal_reason=43,
            tp_state=90,
            reason43_subtype=0,
            observed_packet_sequence=7,
            cached_packet_sequence=7,
            consumed_packet_sequence=7,
            cache_age_s=0.0,
        )
    with pytest.raises(R009FreshnessError):
        validate_reason43_diagnostic(
            terminal_reason=0,
            tp_state=78,
            reason43_subtype=2,
            observed_packet_sequence=7,
            cached_packet_sequence=7,
            consumed_packet_sequence=7,
            cache_age_s=0.0,
        )
