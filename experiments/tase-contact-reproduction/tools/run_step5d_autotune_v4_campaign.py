#!/usr/bin/env python3
"""Run the bounded, offline-first V4 r003 campaign.

The campaign is deliberately a small deterministic state machine.  The
default executor is network-free; the live executor is opt-in and requires
explicit content-addressed bindings before it can ask the single live writer
to do anything.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib
import json
import math
import statistics
import sys
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Protocol, Sequence


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "tools") not in sys.path:
    sys.path.insert(0, str(ROOT / "tools"))

from step5d_autotune_v4.baseline_ledger import (  # noqa: E402
    BASELINE_SUCCESS_STAGE,
    BaselineQualificationLedger,
    BaselineLedgerError,
    BaselineSuccessReceipt,
)
from step5d_autotune_v4.contracts import (  # noqa: E402
    DEFAULT_CONTRACT,
    TARGET_FORCE_N,
    V4Candidate,
    V4Contract,
    V4ContractError,
    assert_runtime_target,
    changed_physical_coordinates,
    load_contract,
    validate_live_transition,
)
from step5d_autotune_v4.live_attempt import (  # noqa: E402
    AttemptKind,
    V4LiveAttemptSpec,
    ledger_payload_sha256,
)
from step5d_autotune_v4.eligibility import (  # noqa: E402
    AttemptEvidence,
    evaluate_attempt,
    promotion_allowed,
)


CAMPAIGN_SCHEMA = "step5d.autotune-v4/r003-campaign-v1"
LEDGER_SCHEMA = "step5d.autotune-v4/r003-campaign-ledger-row-v1"
CAMPAIGN_ID = "step5d_strict_rnn_autotune_v4_r003_bounded_campaign"
GENESIS_SHA256 = "0" * 64
TOTAL_ATTEMPTS = 16
QUALIFICATION_ATTEMPTS = 3
RETEST_ATTEMPTS = 3
OCTAVE_STEP = 0.25
DEFAULT_LEDGER = Path("step5d_autotune_v4_r003_campaign.jsonl")
DEFAULT_LIVE_OUTPUT_ROOT = Path("step5d_autotune_v4_r003_attempts")
DEFAULT_WRITER_CONTRACT = ROOT / "config/step5d/autotune_v4_live_writer_r003.json"
TRIPLET_BASENAME = "step5d_strict_rnn_autotune_v4_r003"
TRIPLET_DIR = ROOT / "programs/step5/step5d"
DEFAULT_CONTROLLER_READBACK_RECEIPT = (
    TRIPLET_DIR / f"{TRIPLET_BASENAME}.controller-readback-receipt.json"
)


class CampaignError(RuntimeError):
    """The bounded campaign cannot safely continue."""


class RemoteBindingError(CampaignError):
    """Live binding is absent, invalid, or not ready before motion."""


class CampaignStructuralFailure(CampaignError):
    """An executor returned a structurally invalid result."""


def _canonical(value: Mapping[str, Any]) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_mapping(value: Mapping[str, Any]) -> str:
    return _sha256_bytes(_canonical(value))


def _digest(value: Any, role: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise CampaignError(f"{role} must be a lowercase SHA-256")
    return value


def _finite_target(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise CampaignError("target_force_n must be numeric")
    target = float(value)
    if not math.isfinite(target) or not math.isclose(
        target, TARGET_FORCE_N, rel_tol=0.0, abs_tol=1e-12
    ):
        raise CampaignError("target_force_n must remain immutable at 5 N")
    return target


@dataclass(frozen=True)
class CampaignAttempt:
    """One immutable slot in the r003 campaign order."""

    ordinal: int
    attempt_id: str
    phase: str
    label: str
    kind: AttemptKind
    candidate: V4Candidate

    def __post_init__(self) -> None:
        if self.ordinal < 1 or self.ordinal > TOTAL_ATTEMPTS:
            raise CampaignError("campaign ordinal is outside the 16-attempt bound")
        if not self.attempt_id or not self.phase or not self.label:
            raise CampaignError("campaign attempt identity is incomplete")
        if not isinstance(self.kind, AttemptKind):
            raise CampaignError("campaign attempt kind is not typed")
        assert_runtime_target(self.candidate, TARGET_FORCE_N)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ordinal": self.ordinal,
            "attempt_id": self.attempt_id,
            "phase": self.phase,
            "label": self.label,
            "kind": self.kind.value,
            "candidate_uid": self.candidate.candidate_uid,
            "candidate": self.candidate.canonical_physical,
            "target_force_n": TARGET_FORCE_N,
        }


def _candidate_at_octave(
    anchor: V4Candidate, field_name: str, delta_octave: float
) -> V4Candidate:
    if field_name not in {"force_p_gain", "force_damping"}:
        raise CampaignError(f"unsupported bounded campaign coordinate: {field_name}")
    candidate = replace(
        anchor,
        **{field_name: getattr(anchor, field_name) * (2.0**delta_octave)},
    )
    assert_runtime_target(candidate, TARGET_FORCE_N)
    return candidate


def build_campaign_plan(contract: V4Contract | None = None) -> tuple[CampaignAttempt, ...]:
    """Return the exact 16-slot r003 plan, independent of execution mode."""

    active_contract = contract or load_contract()
    raw_anchor = active_contract.raw.get("bo", {}).get("anchor")
    if not isinstance(raw_anchor, Mapping):
        raise CampaignError("r003 anchor is missing from the V4 contract")
    anchor = V4Candidate(**dict(raw_anchor))
    p_minus = _candidate_at_octave(anchor, "force_p_gain", -OCTAVE_STEP)
    p_plus = _candidate_at_octave(anchor, "force_p_gain", OCTAVE_STEP)
    d_minus = _candidate_at_octave(anchor, "force_damping", -OCTAVE_STEP)
    d_plus = _candidate_at_octave(anchor, "force_damping", OCTAVE_STEP)

    slots: list[tuple[str, str, AttemptKind, V4Candidate]] = []
    slots.extend(("QUAL", "anchor", AttemptKind.QUALIFICATION, anchor) for _ in range(3))
    slots.extend(
        (
            ("BATCH_A", label, AttemptKind.PD_TRIAL, candidate)
            for label, candidate in (
                ("anchor", anchor),
                ("P-", p_minus),
                ("anchor", anchor),
                ("P+", p_plus),
                ("anchor", anchor),
            )
        )
    )
    slots.extend(
        (
            ("BATCH_B", label, AttemptKind.PD_TRIAL, candidate)
            for label, candidate in (
                ("anchor", anchor),
                ("D-", d_minus),
                ("anchor", anchor),
                ("D+", d_plus),
                ("anchor", anchor),
            )
        )
    )
    slots.extend(
        ("RETEST", "incumbent", AttemptKind.RETEST, anchor) for _ in range(3)
    )

    plan = tuple(
        CampaignAttempt(
            ordinal=index,
            attempt_id=f"r003-{index:02d}-{phase.lower()}-{label.lower().replace('-', 'minus').replace('+', 'plus')}",
            phase=phase,
            label=label,
            kind=kind,
            candidate=candidate,
        )
        for index, (phase, label, kind, candidate) in enumerate(slots, start=1)
    )
    validate_campaign_plan(plan)
    return plan


def validate_campaign_plan(plan: Sequence[CampaignAttempt]) -> None:
    """Validate order, candidate identity, immutable target, and transitions."""

    if len(plan) != TOTAL_ATTEMPTS:
        raise CampaignError("r003 campaign must contain exactly 16 attempts")
    expected_phases = ("QUAL",) * 3 + ("BATCH_A",) * 5 + ("BATCH_B",) * 5 + ("RETEST",) * 3
    if tuple(item.phase for item in plan) != expected_phases:
        raise CampaignError("r003 campaign phase order differs")
    expected_labels = (
        ("anchor",) * 3
        + ("anchor", "P-", "anchor", "P+", "anchor")
        + ("anchor", "D-", "anchor", "D+", "anchor")
        + ("incumbent",) * 3
    )
    if tuple(item.label for item in plan) != expected_labels:
        raise CampaignError("r003 campaign candidate order differs")
    for index, attempt in enumerate(plan, start=1):
        if attempt.ordinal != index:
            raise CampaignError("r003 campaign ordinals are not contiguous")
        _finite_target(attempt.candidate.target_force_n)
        if index == 1:
            continue
        previous = plan[index - 2]
        changed = changed_physical_coordinates(previous.candidate, attempt.candidate)
        if not changed:
            continue
        try:
            validate_live_transition(previous.candidate, attempt.candidate)
        except V4ContractError as exc:
            raise CampaignError(
                f"campaign transition {previous.attempt_id}->{attempt.attempt_id} is invalid: {exc}"
            ) from exc


def _candidate_from_row(row: Mapping[str, Any]) -> V4Candidate:
    value = row.get("candidate")
    if not isinstance(value, Mapping):
        raise CampaignError("campaign row candidate is missing")
    return V4Candidate(**dict(value))


def select_tested_incumbent(
    contract: V4Contract,
    rows: Sequence[Mapping[str, Any]],
) -> V4Candidate:
    """Choose the lowest-MAE eligible P/D candidate; never invent a probe."""

    anchor_value = contract.raw.get("bo", {}).get("anchor")
    if not isinstance(anchor_value, Mapping):
        raise CampaignError("V4 anchor is missing")
    anchor = V4Candidate(**dict(anchor_value))
    ranked: list[tuple[float, int, V4Candidate]] = []
    for row in rows:
        if row.get("phase") not in {"BATCH_A", "BATCH_B"}:
            continue
        if row.get("gp_eligible") is not True:
            continue
        objective = row.get("objective")
        if (
            isinstance(objective, bool)
            or not isinstance(objective, (int, float))
            or not math.isfinite(float(objective))
        ):
            continue
        candidate = _candidate_from_row(row)
        ranked.append((float(objective), int(row["ordinal"]), candidate))
    if not ranked:
        return anchor
    ranked.sort(key=lambda item: (item[0], item[1]))
    return ranked[0][2]


def _promotion_summary(
    contract: V4Contract,
    rows: Sequence[Mapping[str, Any]],
) -> tuple[str, bool]:
    incumbent = select_tested_incumbent(contract, rows)
    anchor_value = contract.raw.get("bo", {}).get("anchor")
    if not isinstance(anchor_value, Mapping):
        raise CampaignError("V4 anchor is missing")
    anchor = V4Candidate(**dict(anchor_value))
    anchor_objectives = [
        float(row["objective"])
        for row in rows
        if row.get("phase") in {"BATCH_A", "BATCH_B"}
        and row.get("candidate_uid") == anchor.candidate_uid
        and row.get("gp_eligible") is True
        and isinstance(row.get("objective"), (int, float))
        and not isinstance(row.get("objective"), bool)
        and math.isfinite(float(row["objective"]))
    ]
    retest_rows = [row for row in rows if row.get("phase") == "RETEST"]
    retest_mae = [
        float(row["mae_n"])
        for row in retest_rows
        if isinstance(row.get("mae_n"), (int, float))
        and not isinstance(row.get("mae_n"), bool)
        and math.isfinite(float(row["mae_n"]))
    ]
    retest_objectives = [
        float(row["objective"])
        for row in retest_rows
        if isinstance(row.get("objective"), (int, float))
        and not isinstance(row.get("objective"), bool)
        and math.isfinite(float(row["objective"]))
    ]
    allowed = (
        incumbent.candidate_uid != anchor.candidate_uid
        and bool(anchor_objectives)
        and promotion_allowed(
            anchor_objective=statistics.median(anchor_objectives),
            retest_mae_n=retest_mae,
            retest_objectives=retest_objectives,
        )
    )
    return incumbent.candidate_uid, allowed


@dataclass(frozen=True)
class AttemptOutcome:
    """Transport-neutral result returned by either executor."""

    completed: bool = True
    terminal_stage: int = BASELINE_SUCCESS_STAGE
    qualification_passed: bool | None = None
    safety_failure: bool = False
    structural_failure: bool = False
    reason: str = ""
    target_force_n: float = TARGET_FORCE_N
    completion_sha256: str | None = None
    metrics: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.completed, bool):
            raise CampaignError("outcome.completed must be bool")
        if (
            not isinstance(self.terminal_stage, int)
            or isinstance(self.terminal_stage, bool)
            or self.terminal_stage < 0
        ):
            raise CampaignError("outcome terminal_stage is invalid")
        if self.qualification_passed is not None and not isinstance(
            self.qualification_passed, bool
        ):
            raise CampaignError("outcome.qualification_passed must be bool or null")
        if not isinstance(self.safety_failure, bool) or not isinstance(
            self.structural_failure, bool
        ):
            raise CampaignError("outcome failure flags must be bool")
        _finite_target(self.target_force_n)
        if not isinstance(self.reason, str):
            raise CampaignError("outcome reason must be text")
        if self.qualification_passed and (
            not self.completed or self.terminal_stage != BASELINE_SUCCESS_STAGE
        ):
            raise CampaignError("a passed qualification must terminate at stage 22")
        if self.completion_sha256 is not None:
            _digest(self.completion_sha256, "completion_sha256")
        if not isinstance(self.metrics, Mapping):
            raise CampaignError("outcome metrics must be a mapping")
        object.__setattr__(self, "metrics", dict(self.metrics))

    def completion_digest(self, attempt: CampaignAttempt) -> str:
        if self.completion_sha256 is not None:
            return self.completion_sha256
        return _sha256_mapping(
            {
                "campaign": CAMPAIGN_ID,
                "attempt_id": attempt.attempt_id,
                "candidate_uid": attempt.candidate.candidate_uid,
                "completed": self.completed,
                "terminal_stage": self.terminal_stage,
                "qualification_passed": self.qualification_passed,
                "safety_failure": self.safety_failure,
                "structural_failure": self.structural_failure,
                "reason": self.reason,
                "target_force_n": TARGET_FORCE_N,
                "metrics": dict(self.metrics),
            }
        )

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "AttemptOutcome":
        terminal = value.get("terminal")
        terminal_stage = value.get("terminal_stage", BASELINE_SUCCESS_STAGE)
        if isinstance(terminal, Mapping) and terminal.get("stage") is not None:
            try:
                terminal_stage = int(round(float(terminal["stage"])))
            except (TypeError, ValueError) as exc:
                raise CampaignStructuralFailure("live terminal stage is invalid") from exc
        reason = str(value.get("reason", value.get("stop_signal", "")) or "")
        status = str(value.get("status", ""))
        completed = bool(value.get("completed", value.get("success", False)))
        if status == "tp_terminal_observed":
            completed = True
        return cls(
            completed=completed,
            terminal_stage=terminal_stage,
            qualification_passed=value.get("qualification_passed"),
            safety_failure=bool(value.get("safety_failure", False)),
            structural_failure=bool(value.get("structural_failure", False)),
            reason=reason,
            target_force_n=float(value.get("target_force_n", TARGET_FORCE_N)),
            completion_sha256=value.get("completion_sha256"),
            metrics=value.get("metrics", {}),
        )


@dataclass(frozen=True)
class CampaignSnapshot:
    next_ordinal: int
    qualification_streak: int
    frozen: bool
    last_candidate_uid: str | None


class AttemptExecutor(Protocol):
    def preflight(self) -> None: ...

    def execute(
        self, attempt: CampaignAttempt, state: CampaignSnapshot
    ) -> AttemptOutcome | Mapping[str, Any]: ...


OutcomeProvider = Callable[[CampaignAttempt, CampaignSnapshot], AttemptOutcome | Mapping[str, Any]]


class DryRunExecutor:
    """A deterministic executor with no sockets, subprocesses, or remote imports."""

    def __init__(self, provider: OutcomeProvider | None = None) -> None:
        self.provider = provider

    def preflight(self) -> None:
        return None

    def execute(
        self, attempt: CampaignAttempt, state: CampaignSnapshot
    ) -> AttemptOutcome | Mapping[str, Any]:
        if self.provider is not None:
            return self.provider(attempt, state)
        return AttemptOutcome(
            completed=True,
            terminal_stage=(
                BASELINE_SUCCESS_STAGE
                if attempt.phase == "QUAL"
                else 80
            ),
            qualification_passed=(True if attempt.phase == "QUAL" else None),
            reason="dry_run_success",
        )


def _parse_triplet_bindings(values: Iterable[str]) -> dict[str, str]:
    result: dict[str, str] = {}
    for item in values:
        if "=" not in item:
            raise RemoteBindingError("--triplet-sha256 values must be NAME=SHA256")
        name, digest = item.split("=", 1)
        if name not in {"urp", "script", "txt"}:
            raise RemoteBindingError(f"unsupported triplet binding: {name}")
        result[name] = _digest(digest, f"triplet_sha256.{name}")
    if set(result) != {"urp", "script", "txt"}:
        raise RemoteBindingError("live mode requires urp,script,txt triplet SHA-256 bindings")
    return result


def _read_baseline_seal(path: Path) -> tuple[Mapping[str, Any], str]:
    if path.is_symlink() or not path.is_file():
        raise RemoteBindingError(f"baseline ledger seal must be a regular file: {path}")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RemoteBindingError(f"baseline ledger seal is unreadable: {exc}") from exc
    if not isinstance(value, Mapping):
        raise RemoteBindingError("baseline ledger seal must be an object")
    expected = value.get("ledger_sha256")
    if not isinstance(expected, str):
        raise RemoteBindingError("baseline ledger seal hash is missing")
    try:
        _digest(expected, "baseline ledger seal hash")
        if ledger_payload_sha256(value) != expected:
            raise RemoteBindingError("baseline ledger seal hash mismatch")
    except (BaselineLedgerError, TypeError, ValueError) as exc:
        raise RemoteBindingError("baseline ledger seal hash validation failed") from exc
    return value, expected


class RemoteBinding:
    """Lazy live-writer binding; validation occurs before any writer call."""

    def __init__(
        self,
        contract: V4Contract,
        *,
        writer_contract_path: Path = DEFAULT_WRITER_CONTRACT,
        triplet_sha256: Mapping[str, str] | None = None,
        baseline_ledger_sha256: str | None = None,
        baseline_ledger_seal: Mapping[str, Any] | None = None,
        controller_readback_receipt_path: Path = DEFAULT_CONTROLLER_READBACK_RECEIPT,
        output_root: Path = DEFAULT_LIVE_OUTPUT_ROOT,
        module_name: str = "step5d_autotune_v4_live_writer",
        runner: Callable[[Any, V4LiveAttemptSpec, Path], Mapping[str, Any]] | None = None,
    ) -> None:
        self.contract = contract
        self.writer_contract_path = writer_contract_path
        self.triplet_sha256 = dict(triplet_sha256 or {})
        self.baseline_ledger_sha256 = baseline_ledger_sha256
        self.baseline_ledger_seal = baseline_ledger_seal
        self.controller_readback_receipt_path = controller_readback_receipt_path
        self.output_root = output_root
        self.module_name = module_name
        self.runner = runner
        self._module: Any | None = None

    @property
    def module_bound(self) -> bool:
        return self._module is not None

    def preflight(self) -> None:
        if self.writer_contract_path.is_symlink() or not self.writer_contract_path.is_file():
            raise RemoteBindingError(
                f"live writer contract must be a regular file: {self.writer_contract_path}"
            )
        if set(self.triplet_sha256) != {"urp", "script", "txt"}:
            raise RemoteBindingError("live mode requires all three explicit triplet SHA-256 bindings")
        for name, digest in self.triplet_sha256.items():
            _digest(digest, f"triplet_sha256.{name}")
        try:
            local_triplet = {
                suffix: _sha256_bytes(
                    (TRIPLET_DIR / f"{TRIPLET_BASENAME}.{suffix}").read_bytes()
                )
                for suffix in ("urp", "script", "txt")
            }
        except OSError as exc:
            raise RemoteBindingError(
                f"local frozen triplet is unavailable: {exc}"
            ) from exc
        if self.triplet_sha256 != local_triplet:
            raise RemoteBindingError(
                "live triplet SHA-256 binding differs from the local frozen candidate"
            )
        receipt_path = self.controller_readback_receipt_path
        if receipt_path.is_symlink() or not receipt_path.is_file():
            raise RemoteBindingError(
                f"controller read-back receipt is unavailable: {receipt_path}"
            )
        try:
            receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise RemoteBindingError(
                f"controller read-back receipt is unreadable: {exc}"
            ) from exc
        artifacts = receipt.get("artifacts")
        gates = receipt.get("gates")
        if (
            receipt.get("basename") != TRIPLET_BASENAME
            or receipt.get("controller_target")
            != f"/programs/andyl/kunwei/step5/{TRIPLET_BASENAME}.urp"
            or receipt.get("state") != "controller read-back verified"
            or artifacts
            != {
                "urp_sha256": local_triplet["urp"],
                "script_sha256": local_triplet["script"],
                "txt_sha256": local_triplet["txt"],
            }
            or not isinstance(gates, Mapping)
            or gates.get("local_triplet_sha_match") is not True
            or gates.get("controller_triplet_sha_match") is not True
            or gates.get("fresh_get_triplet_sha_match") is not True
            or gates.get("cached_contents_exact") is not True
        ):
            raise RemoteBindingError(
                "controller read-back receipt does not bind the exact frozen triplet"
            )
        if self.baseline_ledger_seal is not None:
            expected = self.baseline_ledger_seal.get("ledger_sha256")
            if not isinstance(expected, str) or ledger_payload_sha256(self.baseline_ledger_seal) != expected:
                raise RemoteBindingError("baseline ledger seal binding is invalid")
            _digest(expected, "baseline ledger seal hash")
            if self.baseline_ledger_sha256 is not None and self.baseline_ledger_sha256 != expected:
                raise RemoteBindingError("baseline ledger hash differs from sealed input")
            self.baseline_ledger_sha256 = expected
        if self.baseline_ledger_sha256 is None:
            raise RemoteBindingError("live mode requires a baseline ledger hash before motion")
        _digest(self.baseline_ledger_sha256, "input_baseline_ledger_sha256")

    def _bind_module(self) -> Any:
        if self._module is None:
            try:
                self._module = importlib.import_module(self.module_name)
            except Exception as exc:
                raise RemoteBindingError(
                    f"live writer binding failed closed before motion: {exc}"
                ) from exc
        return self._module

    def execute(
        self, attempt: CampaignAttempt, state: CampaignSnapshot
    ) -> AttemptOutcome:
        del state
        self.preflight()
        module = self._bind_module()
        if self.runner is not None:
            result = self.runner(module, self._build_spec(attempt), self.output_root / attempt.attempt_id)
        else:
            writer_contract = module.load_contract(self.writer_contract_path)
            spec = self._build_spec(attempt)
            output_dir = self.output_root / attempt.attempt_id
            result = module.run_writer(
                writer_contract,
                spec,
                output_dir=output_dir,
                skip_dashboard=False,
                skip_lease=False,
            )
        if isinstance(result, AttemptOutcome):
            return result
        if not isinstance(result, Mapping):
            raise CampaignStructuralFailure("live writer returned a non-mapping result")
        summary = result.get("summary", result)
        if not isinstance(summary, Mapping):
            raise CampaignStructuralFailure("live writer summary is not a mapping")
        output_seal = summary.get("output_baseline_ledger")
        if not isinstance(output_seal, Mapping):
            raise CampaignStructuralFailure(
                "live writer omitted the output baseline ledger seal"
            )
        output_digest = output_seal.get("ledger_sha256")
        if (
            not isinstance(output_digest, str)
            or ledger_payload_sha256(output_seal) != output_digest
        ):
            raise CampaignStructuralFailure(
                "live writer output baseline ledger seal is invalid"
            )
        self.baseline_ledger_seal = dict(output_seal)
        self.baseline_ledger_sha256 = output_digest
        return self._outcome_from_summary(attempt, summary)

    def _build_spec(self, attempt: CampaignAttempt) -> V4LiveAttemptSpec:
        if self.baseline_ledger_sha256 is None:
            raise RemoteBindingError("baseline ledger hash disappeared before motion")
        kwargs: dict[str, Any] = {
            "attempt_id": attempt.attempt_id,
            "kind": attempt.kind,
            "candidate": attempt.candidate,
            "contract_sha256": self.contract.sha256,
            "campaign_fingerprint": self.contract.campaign_fingerprint,
            "eoat_sha256": self.contract.eoat_sha256,
            "model_hashes": self.contract.model_hashes,
            "triplet_sha256": self.triplet_sha256,
            "input_baseline_ledger_sha256": self.baseline_ledger_sha256,
            "path_requested": attempt.phase != "QUAL",
        }
        if self.baseline_ledger_seal is not None:
            kwargs["baseline_ledger_seal"] = self.baseline_ledger_seal
        if "v4_contract_path" in getattr(V4LiveAttemptSpec, "__dataclass_fields__", {}):
            kwargs["v4_contract_path"] = self.contract.path
        try:
            return V4LiveAttemptSpec(**kwargs)
        except TypeError as exc:
            raise RemoteBindingError(
                "live-attempt interface cannot bind the V4 contract before motion"
            ) from exc

    @staticmethod
    def _outcome_from_summary(
        attempt: CampaignAttempt, summary: Mapping[str, Any]
    ) -> AttemptOutcome:
        terminal = summary.get("terminal")
        stage = 0
        if isinstance(terminal, Mapping) and terminal.get("stage") is not None:
            stage = int(round(float(terminal["stage"])))
        status = str(summary.get("status", ""))
        reason = str(summary.get("stop_signal", "") or "")
        terminal_reason = 0
        if isinstance(terminal, Mapping) and terminal.get("reason") is not None:
            terminal_reason = int(round(float(terminal["reason"])))
        lower_reason = reason.lower()
        safety = terminal_reason in {5, 6, 7} or any(
            token in lower_reason
            for token in ("safety", "hard_abs", "hard_force", "hard_torque")
        )
        expected_terminal_reason = (
            31 if attempt.kind is AttemptKind.QUALIFICATION else 32
        )
        terminal_kind_mismatch = (
            status == "tp_terminal_observed"
            and stage == 80
            and terminal_reason in {31, 32}
            and terminal_reason != expected_terminal_reason
        )
        structural = any(
            token in lower_reason
            for token in ("structural", "hash", "nonfinite", "stale", "kinematic")
        ) or status == "writer_failed" or stage == 90 or terminal_kind_mismatch
        completed = (
            status == "tp_terminal_observed"
            and terminal is not None
            and stage == 80
            and terminal_reason == expected_terminal_reason
        )
        qualification_passed = (
            completed and attempt.phase == "QUAL" and terminal_reason == 31
        )
        logical_terminal_stage = (
            BASELINE_SUCCESS_STAGE if qualification_passed else stage
        )
        return AttemptOutcome(
            completed=completed,
            terminal_stage=logical_terminal_stage,
            qualification_passed=qualification_passed if attempt.phase == "QUAL" else None,
            safety_failure=safety,
            structural_failure=structural,
            reason=(
                reason
                or (
                    "terminal_reason_kind_mismatch"
                    if terminal_kind_mismatch
                    else "live_terminal_observed"
                    if completed
                    else "live_writer_failed"
                )
            ),
            target_force_n=TARGET_FORCE_N,
            completion_sha256=(
                summary.get("completion_sha256")
                if isinstance(summary.get("completion_sha256"), str)
                else None
            ),
            metrics={
                **dict(summary.get("metrics", {})),
                "timing_acceptance": summary.get("timing_acceptance"),
                "replay_eligible_shape": summary.get("replay_eligible_shape"),
                "physical_terminal_stage": stage,
                "physical_terminal_reason": terminal_reason,
            },
        )


class LiveExecutor:
    def __init__(self, binding: RemoteBinding) -> None:
        self.binding = binding

    def preflight(self) -> None:
        self.binding.preflight()

    def execute(
        self, attempt: CampaignAttempt, state: CampaignSnapshot
    ) -> AttemptOutcome:
        return self.binding.execute(attempt, state)


class AppendOnlySHA256Ledger:
    """An append-only JSONL ledger whose every row commits to its predecessor."""

    HASH_FIELDS = frozenset({"sha256", "row_sha256"})

    def __init__(
        self,
        path: Path,
        *,
        campaign_id: str = CAMPAIGN_ID,
        campaign_fingerprint: str,
    ) -> None:
        self.path = path
        self.campaign_id = campaign_id
        self.campaign_fingerprint = _digest(campaign_fingerprint, "campaign_fingerprint")
        self.rows = self._read_and_verify()
        self.previous_sha256 = (
            self.rows[-1]["sha256"] if self.rows else GENESIS_SHA256
        )

    def _read_and_verify(self) -> list[dict[str, Any]]:
        if self.path.is_symlink():
            raise CampaignError(f"campaign ledger must not be a symlink: {self.path}")
        if not self.path.exists():
            return []
        if not self.path.is_file():
            raise CampaignError(f"campaign ledger is not a regular file: {self.path}")
        rows: list[dict[str, Any]] = []
        previous = GENESIS_SHA256
        try:
            lines = self.path.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeError) as exc:
            raise CampaignError(f"campaign ledger is unreadable: {exc}") from exc
        for line_number, line in enumerate(lines, start=1):
            if not line:
                raise CampaignError(f"campaign ledger has a blank line at {line_number}")
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                raise CampaignError(f"campaign ledger JSON is invalid at {line_number}") from exc
            if not isinstance(row, dict):
                raise CampaignError(f"campaign ledger row {line_number} is not an object")
            if row.get("schema") != LEDGER_SCHEMA:
                raise CampaignError(f"campaign ledger schema differs at {line_number}")
            if row.get("campaign_id") != self.campaign_id:
                raise CampaignError(f"campaign ledger campaign identity differs at {line_number}")
            if row.get("campaign_fingerprint") != self.campaign_fingerprint:
                raise CampaignError(f"campaign ledger fingerprint differs at {line_number}")
            if row.get("sequence") != line_number:
                raise CampaignError(f"campaign ledger sequence is not contiguous at {line_number}")
            if row.get("previous_sha256") != previous:
                raise CampaignError(f"campaign ledger chain breaks at {line_number}")
            expected = _sha256_mapping(
                {key: value for key, value in row.items() if key not in self.HASH_FIELDS}
            )
            if row.get("sha256") != expected or row.get("row_sha256", expected) != expected:
                raise CampaignError(f"campaign ledger hash mismatch at {line_number}")
            rows.append(row)
            previous = expected
        return rows

    def append(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        if any(key in payload for key in {"schema", "campaign_id", "campaign_fingerprint", "sequence", "previous_sha256", *self.HASH_FIELDS}):
            raise CampaignError("campaign ledger payload contains reserved fields")
        row = {
            "schema": LEDGER_SCHEMA,
            "campaign_id": self.campaign_id,
            "campaign_fingerprint": self.campaign_fingerprint,
            "sequence": len(self.rows) + 1,
            "previous_sha256": self.previous_sha256,
            **dict(payload),
        }
        digest = _sha256_mapping(row)
        row["sha256"] = digest
        row["row_sha256"] = digest
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(row, sort_keys=True, separators=(",", ":"), allow_nan=False))
                handle.write("\n")
                handle.flush()
        except (OSError, TypeError, ValueError) as exc:
            raise CampaignError(f"campaign ledger append failed: {exc}") from exc
        self.rows.append(row)
        self.previous_sha256 = digest
        return row


def verify_sha256_chain(path: Path, *, campaign_fingerprint: str) -> tuple[dict[str, Any], ...]:
    """Verify an existing chain without opening it for append."""

    return tuple(
        AppendOnlySHA256Ledger(
            path, campaign_fingerprint=campaign_fingerprint
        ).rows
    )


def _normalize_outcome(
    value: AttemptOutcome | Mapping[str, Any],
) -> AttemptOutcome:
    if isinstance(value, AttemptOutcome):
        return value
    if isinstance(value, Mapping):
        return AttemptOutcome.from_mapping(value)
    raise CampaignStructuralFailure("executor returned an invalid outcome type")


def _state_from_rows(
    rows: Sequence[Mapping[str, Any]],
    plan: Sequence[CampaignAttempt],
    contract: V4Contract,
) -> tuple[int, bool, str, bool]:
    if len(rows) > len(plan):
        raise CampaignError("campaign ledger contains more than 16 attempt rows")
    previous_candidate: V4Candidate | None = None
    frozen = False
    freeze_reason = ""
    all_completed = True
    streak = 0
    for index, row in enumerate(rows):
        base_attempt = plan[index]
        attempt = (
            replace(
                base_attempt,
                candidate=select_tested_incumbent(
                    contract, rows[:index]
                ),
            )
            if base_attempt.phase == "RETEST"
            else base_attempt
        )
        if row.get("record_type") != "attempt":
            raise CampaignError("campaign ledger contains a non-attempt row")
        if row.get("ordinal") != attempt.ordinal or row.get("attempt_id") != attempt.attempt_id:
            raise CampaignError("campaign ledger attempt order differs from r003 plan")
        if row.get("phase") != attempt.phase or row.get("label") != attempt.label:
            raise CampaignError("campaign ledger attempt label differs from r003 plan")
        if row.get("candidate_uid") != attempt.candidate.candidate_uid:
            raise CampaignError("campaign ledger candidate differs from r003 plan")
        _finite_target(row.get("target_force_n"))
        if previous_candidate is not None:
            changed = changed_physical_coordinates(previous_candidate, attempt.candidate)
            if changed:
                try:
                    validate_live_transition(previous_candidate, attempt.candidate)
                except V4ContractError as exc:
                    raise CampaignError("campaign ledger contains an invalid transition") from exc
        previous_candidate = attempt.candidate
        streak_value = row.get("qualification_streak_after")
        if not isinstance(streak_value, int) or isinstance(streak_value, bool) or streak_value < 0:
            raise CampaignError("campaign ledger qualification streak is invalid")
        streak = streak_value
        if row.get("frozen"):
            frozen = True
            freeze_reason = str(row.get("freeze_reason", ""))
        outcome = row.get("outcome")
        if not isinstance(outcome, Mapping) or not bool(outcome.get("completed", False)):
            all_completed = False
    return streak, frozen, freeze_reason, all_completed


def _failure_reason(outcome: AttemptOutcome, attempt: CampaignAttempt) -> str:
    if outcome.reason:
        return outcome.reason
    if outcome.safety_failure:
        return f"{attempt.attempt_id}:safety_failure"
    if outcome.structural_failure:
        return f"{attempt.attempt_id}:structural_failure"
    return f"{attempt.attempt_id}:qualification_failure"


def run_campaign(
    ledger_path: Path,
    *,
    contract: V4Contract | None = None,
    executor: AttemptExecutor | None = None,
    live: bool = False,
    outcome_provider: OutcomeProvider | None = None,
    writer_contract_path: Path = DEFAULT_WRITER_CONTRACT,
    triplet_sha256: Mapping[str, str] | None = None,
    baseline_ledger_sha256: str | None = None,
    baseline_ledger_seal: Mapping[str, Any] | None = None,
    controller_readback_receipt_path: Path = DEFAULT_CONTROLLER_READBACK_RECEIPT,
    live_output_root: Path = DEFAULT_LIVE_OUTPUT_ROOT,
) -> CampaignResult:
    """Execute or resume the exact plan while keeping the ledger append-only."""

    active_contract = contract or load_contract(DEFAULT_CONTRACT)
    plan = build_campaign_plan(active_contract)
    ledger = AppendOnlySHA256Ledger(
        ledger_path,
        campaign_fingerprint=active_contract.campaign_fingerprint,
    )
    streak, frozen, freeze_reason, all_completed = _state_from_rows(
        ledger.rows, plan, active_contract
    )
    if frozen:
        incumbent_uid, promote = _promotion_summary(active_contract, ledger.rows)
        return CampaignResult(
            ledger_path=ledger_path,
            attempts_run=len(ledger.rows),
            completed=False,
            frozen=True,
            stopped=True,
            stop_reason=freeze_reason,
            qualification_streak=streak,
            rows=tuple(ledger.rows),
            selected_incumbent_uid=incumbent_uid,
            promotion_allowed=promote,
        )
    if len(ledger.rows) == len(plan):
        incumbent_uid, promote = _promotion_summary(active_contract, ledger.rows)
        return CampaignResult(
            ledger_path=ledger_path,
            attempts_run=len(ledger.rows),
            completed=all_completed,
            frozen=False,
            stopped=False,
            stop_reason="",
            qualification_streak=streak,
            rows=tuple(ledger.rows),
            selected_incumbent_uid=incumbent_uid,
            promotion_allowed=promote,
        )
    if executor is None:
        if live:
            executor = LiveExecutor(
                RemoteBinding(
                    active_contract,
                    writer_contract_path=writer_contract_path,
                    triplet_sha256=triplet_sha256,
                    baseline_ledger_sha256=baseline_ledger_sha256,
                    baseline_ledger_seal=baseline_ledger_seal,
                    controller_readback_receipt_path=controller_readback_receipt_path,
                    output_root=live_output_root,
                )
            )
        else:
            executor = DryRunExecutor(provider=outcome_provider)
    elif live and outcome_provider is not None:
        raise CampaignError("live executor and dry-run outcome provider cannot be combined")
    executor.preflight()

    previous_candidate = _candidate_from_row(ledger.rows[-1]) if ledger.rows else None
    stopped = False
    stop_reason = ""
    for base_attempt in plan[len(ledger.rows) :]:
        attempt = (
            replace(
                base_attempt,
                candidate=select_tested_incumbent(
                    active_contract, ledger.rows
                ),
            )
            if base_attempt.phase == "RETEST"
            else base_attempt
        )
        if attempt.phase != "QUAL" and streak < QUALIFICATION_ATTEMPTS:
            stopped = True
            stop_reason = "three_consecutive_qualifications_required_before_trials"
            break
        if previous_candidate is not None:
            changed = changed_physical_coordinates(previous_candidate, attempt.candidate)
            if changed:
                try:
                    validate_live_transition(previous_candidate, attempt.candidate)
                except V4ContractError as exc:
                    raise CampaignError(f"campaign transition is invalid: {exc}") from exc
        snapshot = CampaignSnapshot(
            next_ordinal=attempt.ordinal,
            qualification_streak=streak,
            frozen=False,
            last_candidate_uid=(
                previous_candidate.candidate_uid if previous_candidate is not None else None
            ),
        )
        try:
            outcome = _normalize_outcome(executor.execute(attempt, snapshot))
        except RemoteBindingError:
            raise
        except CampaignStructuralFailure as exc:
            outcome = AttemptOutcome(
                completed=False,
                terminal_stage=0,
                structural_failure=True,
                reason=str(exc) or "structural_failure",
            )
        except CampaignError:
            raise
        except Exception as exc:
            outcome = AttemptOutcome(
                completed=False,
                terminal_stage=0,
                structural_failure=True,
                reason=f"executor_exception:{type(exc).__name__}",
            )
        _finite_target(outcome.target_force_n)
        streak_before = streak
        qualification_passed = outcome.qualification_passed
        if qualification_passed is None and attempt.phase == "QUAL":
            qualification_passed = (
                outcome.completed and outcome.terminal_stage == BASELINE_SUCCESS_STAGE
            )
        safety_or_structural = outcome.safety_failure or outcome.structural_failure
        try:
            if safety_or_structural:
                reason = _failure_reason(outcome, attempt)
                BaselineQualificationLedger(active_contract).record_failure(
                    attempt_id=attempt.attempt_id,
                    terminal_stage=outcome.terminal_stage,
                    reason=reason,
                    safety_or_structural=True,
                )
                frozen = True
                stopped = True
                stop_reason = reason
            elif qualification_passed is True:
                qualification_ledger = BaselineQualificationLedger(active_contract)
                # Restore the scalar streak through deterministic receipts.  The
                # campaign ledger is the durable owner for resumed runs; the
                # receipt ids remain the exact preceding qualification slots.
                for prior in plan[: attempt.ordinal - 1]:
                    prior_row = ledger.rows[prior.ordinal - 1]
                    prior_qualification = prior_row.get("qualification_passed")
                    if prior_qualification is True:
                        qualification_ledger.record_success(
                            BaselineSuccessReceipt(
                                attempt_id=prior.attempt_id,
                                terminal_stage=BASELINE_SUCCESS_STAGE,
                                campaign_fingerprint=active_contract.campaign_fingerprint,
                                eoat_sha256=active_contract.eoat_sha256,
                                target_force_n=TARGET_FORCE_N,
                                sensor_authority="kunwei_only",
                                completion_sha256=str(prior_row["completion_sha256"]),
                            )
                        )
                    elif prior_qualification is False:
                        prior_stage = prior_row.get("terminal_stage", 21)
                        if not isinstance(prior_stage, int) or prior_stage > BASELINE_SUCCESS_STAGE:
                            prior_stage = 21
                        qualification_ledger.record_failure(
                            attempt_id=prior.attempt_id,
                            terminal_stage=prior_stage,
                            reason=str(prior_row.get("reason", "qualification_failure"))
                            or "qualification_failure",
                            safety_or_structural=False,
                        )
                qualification_ledger.record_success(
                    BaselineSuccessReceipt(
                        attempt_id=attempt.attempt_id,
                        terminal_stage=BASELINE_SUCCESS_STAGE,
                        campaign_fingerprint=active_contract.campaign_fingerprint,
                        eoat_sha256=active_contract.eoat_sha256,
                        target_force_n=TARGET_FORCE_N,
                        sensor_authority="kunwei_only",
                        completion_sha256=outcome.completion_digest(attempt),
                    )
                )
                streak = qualification_ledger.snapshot().consecutive_successes
            elif qualification_passed is False:
                # A qualification miss always clears the streak, even if a
                # remote reports a later terminal stage for the same attempt.
                streak = 0
            if safety_or_structural:
                streak = 0 if outcome.terminal_stage <= BASELINE_SUCCESS_STAGE else streak
        except BaselineLedgerError as exc:
            raise CampaignStructuralFailure(f"qualification ledger update failed: {exc}") from exc
        gp_eligible = False
        eligibility_disposition = "not_applicable"
        eligibility_reasons: tuple[str, ...] = ()
        objective: float | None = None
        mae_n: float | None = None
        if attempt.phase != "QUAL":
            metrics = dict(outcome.metrics)

            def optional_metric(name: str) -> float | None:
                value = metrics.get(name)
                if isinstance(value, bool) or not isinstance(value, (int, float)):
                    return None
                result = float(value)
                return result if math.isfinite(result) else None

            objective = optional_metric("objective")
            mae_n = optional_metric("mae_n")
            complete_bins_value = metrics.get("complete_bins", 0)
            complete_bins = (
                int(complete_bins_value)
                if isinstance(complete_bins_value, int)
                and not isinstance(complete_bins_value, bool)
                else 0
            )
            timing_acceptance = metrics.get("timing_acceptance")
            timing_passed = (
                isinstance(timing_acceptance, Mapping)
                and timing_acceptance.get("passed") is True
            )
            replay_eligible_shape = metrics.get("replay_eligible_shape") is True
            lower_reason = outcome.reason.lower()
            evidence = AttemptEvidence(
                campaign_fingerprint=active_contract.campaign_fingerprint,
                eoat_sha256=active_contract.eoat_sha256,
                target_force_n=outcome.target_force_n,
                kunwei_only_receipt=True,
                complete_bins=complete_bins,
                terminal_closure=outcome.completed,
                completion_closure=outcome.completion_sha256 is not None,
                replay_closure=(
                    complete_bins == 550
                    and timing_passed
                    and replay_eligible_shape
                ),
                safety_failure=outcome.safety_failure,
                structural_failure=outcome.structural_failure,
                stale_or_nonfinite=(
                    "stale" in lower_reason or "nonfinite" in lower_reason
                ),
                incomplete=(
                    not outcome.completed
                    or complete_bins != 550
                    or not timing_passed
                    or not replay_eligible_shape
                ),
                objective=objective,
                mae_n=mae_n,
                p99_normal_n=optional_metric("p99_normal_n"),
                max_force_norm_n=optional_metric("max_force_norm_n"),
                max_torque_norm_nm=optional_metric("max_torque_norm_nm"),
            )
            prior_eligible_count = sum(
                row.get("gp_eligible") is True for row in ledger.rows
            )
            decision = evaluate_attempt(
                active_contract,
                evidence,
                prior_eligible_count=prior_eligible_count,
            )
            gp_eligible = decision.eligible
            eligibility_disposition = decision.disposition.value
            eligibility_reasons = decision.reasons
        completion_sha256 = outcome.completion_digest(attempt)
        if not outcome.completed:
            all_completed = False
        row = ledger.append(
            {
                "record_type": "attempt",
                "ordinal": attempt.ordinal,
                "attempt_id": attempt.attempt_id,
                "phase": attempt.phase,
                "label": attempt.label,
                "kind": attempt.kind.value,
                "candidate_uid": attempt.candidate.candidate_uid,
                "candidate": attempt.candidate.canonical_physical,
                "target_force_n": TARGET_FORCE_N,
                "qualification_passed": qualification_passed,
                "qualification_streak_before": streak_before,
                "qualification_streak_after": streak,
                "completed": outcome.completed,
                "terminal_stage": outcome.terminal_stage,
                "completion_sha256": completion_sha256,
                "safety_failure": outcome.safety_failure,
                "structural_failure": outcome.structural_failure,
                "safety_or_structural_failure": safety_or_structural,
                "reason": outcome.reason,
                "metrics": dict(outcome.metrics),
                "objective": objective,
                "mae_n": mae_n,
                "gp_eligible": gp_eligible,
                "eligibility_disposition": eligibility_disposition,
                "eligibility_reasons": list(eligibility_reasons),
                "frozen": frozen,
                "freeze_reason": stop_reason if frozen else "",
                "stop": stopped,
                "transition_changed_coordinates": list(
                    ()
                    if previous_candidate is None
                    else changed_physical_coordinates(previous_candidate, attempt.candidate)
                ),
                "outcome": {
                    "completed": outcome.completed,
                    "terminal_stage": outcome.terminal_stage,
                    "reason": outcome.reason,
                    "safety_failure": outcome.safety_failure,
                    "structural_failure": outcome.structural_failure,
                },
            }
        )
        previous_candidate = attempt.candidate
        if stopped:
            break
    incumbent_uid, promote = _promotion_summary(active_contract, ledger.rows)
    return CampaignResult(
        ledger_path=ledger_path,
        attempts_run=len(ledger.rows),
        completed=(
            len(ledger.rows) == TOTAL_ATTEMPTS
            and all_completed
            and not frozen
        ),
        frozen=frozen,
        stopped=stopped,
        stop_reason=stop_reason,
        qualification_streak=streak,
        rows=tuple(ledger.rows),
        selected_incumbent_uid=incumbent_uid,
        promotion_allowed=promote,
    )


@dataclass(frozen=True)
class CampaignResult:
    ledger_path: Path
    attempts_run: int
    completed: bool
    frozen: bool
    stopped: bool
    stop_reason: str
    qualification_streak: int
    rows: tuple[Mapping[str, Any], ...]
    selected_incumbent_uid: str
    promotion_allowed: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": CAMPAIGN_SCHEMA,
            "campaign_id": CAMPAIGN_ID,
            "ledger_path": str(self.ledger_path),
            "attempts_run": self.attempts_run,
            "completed": self.completed,
            "frozen": self.frozen,
            "stopped": self.stopped,
            "stop_reason": self.stop_reason,
            "qualification_streak": self.qualification_streak,
            "last_sha256": self.rows[-1].get("sha256") if self.rows else GENESIS_SHA256,
            "selected_incumbent_uid": self.selected_incumbent_uid,
            "promotion_allowed": self.promotion_allowed,
        }


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--contract", type=Path, default=DEFAULT_CONTRACT)
    parser.add_argument("--ledger", type=Path, default=DEFAULT_LEDGER)
    parser.add_argument("--live", action="store_true", help="opt into the remote writer")
    parser.add_argument("--dry-run", action="store_true", help="explicitly select the network-free executor")
    parser.add_argument("--writer-contract", type=Path, default=DEFAULT_WRITER_CONTRACT)
    parser.add_argument("--triplet-sha256", action="append", default=[])
    parser.add_argument("--baseline-ledger-seal", type=Path)
    parser.add_argument("--baseline-ledger-sha256")
    parser.add_argument(
        "--controller-readback-receipt",
        type=Path,
        default=DEFAULT_CONTROLLER_READBACK_RECEIPT,
    )
    parser.add_argument("--live-output-root", type=Path, default=DEFAULT_LIVE_OUTPUT_ROOT)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    if args.live and args.dry_run:
        print("--live and --dry-run are mutually exclusive", file=sys.stderr)
        return 2
    try:
        active_contract = load_contract(args.contract)
        triplet = _parse_triplet_bindings(args.triplet_sha256) if args.live else None
        seal = None
        seal_digest = args.baseline_ledger_sha256
        if args.baseline_ledger_seal is not None:
            seal, seal_digest_from_file = _read_baseline_seal(args.baseline_ledger_seal)
            if seal_digest is not None and seal_digest != seal_digest_from_file:
                raise RemoteBindingError("explicit baseline ledger hash differs from seal file")
            seal_digest = seal_digest_from_file
        result = run_campaign(
            args.ledger,
            contract=active_contract,
            live=args.live,
            writer_contract_path=args.writer_contract,
            triplet_sha256=triplet,
            baseline_ledger_sha256=seal_digest,
            baseline_ledger_seal=seal,
            controller_readback_receipt_path=args.controller_readback_receipt,
            live_output_root=args.live_output_root,
        )
    except (CampaignError, V4ContractError, OSError, ValueError) as exc:
        print(json.dumps({"schema": CAMPAIGN_SCHEMA, "status": "blocked", "reason": str(exc)}, sort_keys=True))
        return 2
    print(json.dumps(result.to_dict(), sort_keys=True))
    return 0 if result.completed else 2


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "AppendOnlySHA256Ledger",
    "AttemptExecutor",
    "AttemptKind",
    "AttemptOutcome",
    "CAMPAIGN_ID",
    "CAMPAIGN_SCHEMA",
    "CampaignAttempt",
    "CampaignError",
    "CampaignResult",
    "CampaignSnapshot",
    "CampaignStructuralFailure",
    "DryRunExecutor",
    "GENESIS_SHA256",
    "LiveExecutor",
    "OCTAVE_STEP",
    "RemoteBinding",
    "RemoteBindingError",
    "TOTAL_ATTEMPTS",
    "build_campaign_plan",
    "run_campaign",
    "validate_campaign_plan",
    "verify_sha256_chain",
]
