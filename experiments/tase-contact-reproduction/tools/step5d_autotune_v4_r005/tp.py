"""r005 resident TP renderer and offline attempt-sequence model.

The renderer starts from the r004 resident transport source, then applies a
typed r005 identity and removes the old fixed 1..16 ordinal/phase-plan gate.
It is a new generated program and never mutates the r004 source or artifact.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from step5d_autotune_v4_r004.contracts import runtime_identity_limbs

from .contracts import PROGRAM, R005ContractError, load_contract


RUNTIME_PROTOCOL = 606005
CONTROLLER_DIR = "/programs/andyl/kunwei/step5"


class TPProtocolError(RuntimeError):
    """The resident offline TP model rejected an attempt sequence."""


@dataclass
class ResidentTPModel:
    last_attempt_sequence: int = 0
    state: str = "WAITING_ARM"
    epoch: int | None = None
    current_token: int = 0
    current_kind: int = 0
    consumed_session_sequence: int = 0

    def arm(
        self,
        attempt_sequence: int,
        *,
        epoch: int = 1,
        candidate_token: int = 1,
        attempt_kind: int = 1,
        session_command_sequence: int | None = None,
    ) -> None:
        if isinstance(attempt_sequence, bool) or not isinstance(attempt_sequence, int) or attempt_sequence <= 0:
            raise TPProtocolError("attempt sequence must be a positive integer")
        if isinstance(epoch, bool) or not isinstance(epoch, int) or epoch <= 0:
            raise TPProtocolError("epoch must be positive")
        if self.epoch is not None and epoch != self.epoch:
            raise TPProtocolError("epoch differs within one resident Play")
        if isinstance(candidate_token, bool) or not isinstance(candidate_token, int) or candidate_token <= 0:
            raise TPProtocolError("candidate token must be positive")
        if isinstance(attempt_kind, bool) or not isinstance(attempt_kind, int) or not 1 <= attempt_kind <= 4:
            raise TPProtocolError("attempt kind is invalid")
        if attempt_sequence <= self.last_attempt_sequence:
            raise TPProtocolError("attempt sequence must increase")
        if self.state != "WAITING_ARM":
            raise TPProtocolError("resident TP is not waiting for ARM")
        command_sequence = attempt_sequence if session_command_sequence is None else session_command_sequence
        if command_sequence <= self.consumed_session_sequence:
            raise TPProtocolError("session command sequence must increase")
        self.consumed_session_sequence = command_sequence
        self.epoch = epoch
        self.last_attempt_sequence = attempt_sequence
        self.current_token = candidate_token
        self.current_kind = attempt_kind
        self.state = "RUNNING"

    def safe_return(self) -> None:
        if self.state != "RUNNING":
            raise TPProtocolError("safe return requires a running attempt")
        self.state = "WAITING_ARM"

    def complete(
        self,
        *,
        session_command_sequence: int,
        epoch: int,
        attempt_sequence: int,
        candidate_token: int,
        attempt_kind: int,
    ) -> None:
        if self.state != "WAITING_ARM":
            raise TPProtocolError("COMPLETE requires a safe returned resident")
        if session_command_sequence <= self.consumed_session_sequence:
            raise TPProtocolError("COMPLETE command sequence must be newer")
        if (
            epoch != self.epoch
            or attempt_sequence != self.last_attempt_sequence
            or candidate_token != self.current_token
            or attempt_kind != self.current_kind
        ):
            raise TPProtocolError("COMPLETE identity differs from current attempt")
        self.consumed_session_sequence = session_command_sequence
        self.state = "COMPLETE"


def _replace_identity(source: str, *, contract_sha256: str, fingerprint: str) -> str:
    # These are lexical, allow-listed identity edits.  They deliberately do
    # not perform a free-form r004 -> r005 replacement, which could mutate a
    # numeric literal or an unrelated token in generated URScript.
    source = re.sub(
        re.escape("step5d_strict_rnn_autotune_v4_r004"),
        PROGRAM,
        source,
    )
    source = re.sub(r"\bcodex_r004(?=_)", "codex_r005", source)
    source = re.sub(
        r"(?m)^# ROLE: isolated V4 r004 resident rolling ARM loop$",
        "# ROLE: isolated V4 r005 resident rolling ARM loop",
        source,
    )
    source = re.sub(
        r"(?m)^# FIXED_HOME_PROFILE: step5d\.autotune-v4/r004-fixed-home-v1$",
        "# FIXED_HOME_PROFILE: step5d.autotune-v4/r005-fixed-home-v1",
        source,
    )
    source = re.sub(r"606004", str(RUNTIME_PROTOCOL), source)
    source = re.sub(
        r"# V4_CONTRACT_SHA256: [0-9a-f]{64}",
        f"# V4_CONTRACT_SHA256: {contract_sha256}",
        source,
    )
    source = re.sub(
        r"# V4_CAMPAIGN_FINGERPRINT: [0-9a-f]{64}",
        f"# V4_CAMPAIGN_FINGERPRINT: {fingerprint}",
        source,
    )
    runtime_hi, runtime_lo = runtime_identity_limbs(
        PROGRAM, contract_sha256, fingerprint
    )
    source, runtime_hi_count = re.subn(
        r"(?m)^(\s*local runtime_hi = )\d+$",
        rf"\g<1>{runtime_hi}",
        source,
        count=1,
    )
    source, runtime_lo_count = re.subn(
        r"(?m)^(\s*local runtime_lo = )\d+$",
        rf"\g<1>{runtime_lo}",
        source,
        count=1,
    )
    if runtime_hi_count != 1 or runtime_lo_count != 1:
        raise R005ContractError("r004 TP runtime identity limbs were not found")
    source = re.sub(
        r"input_ordinal < 1 or input_ordinal > 16",
        "input_ordinal < 1",
        source,
    )
    source = re.sub(r"ordinal < 0 or ordinal > 16", "ordinal < 0", source)
    source = re.sub(r"input_ordinal == 16", "input_ordinal > 0", source)
    monotonic_guard = (
        "if input_epoch <= last_failed_epoch or input_epoch <= 0 or "
        "input_ordinal < 1 or input_kind < 1 or input_kind > 4 or input_token <= 0:"
    )
    monotonic_guard_replacement = (
        "if input_epoch <= last_failed_epoch or input_epoch <= 0 or "
        "input_ordinal < 1 or input_ordinal <= current_ordinal or "
        "(active_epoch > 0 and input_epoch != active_epoch) or "
        "input_kind < 1 or input_kind > 4 or input_token <= 0:"
    )
    source, monotonic_count = re.subn(
        re.escape(monotonic_guard),
        monotonic_guard_replacement,
        source,
        count=1,
    )
    if monotonic_count != 1:
        raise R005ContractError("r004 TP attempt-sequence guard was not found")
    source, plan_count = re.subn(
        r"(?m)^(?P<indent>\s*)elif \(input_ordinal <= 3 and input_kind != 1\).*$",
        r"\g<indent>elif False:  # r005 host owns phase sequencing; TP accepts positive monotonic attempts",
        source,
        count=1,
    )
    if plan_count != 1:
        raise R005ContractError("r004 TP phase-plan guard was not found")
    complete_before = (
        "    elif session_command == 2 and state == 78 and "
        "session_sequence > consumed_session_sequence and "
        "input_epoch == active_epoch and input_ordinal > 0:\n"
        "      consumed_session_sequence = session_sequence\n"
        "      completed = True\n"
        "      state = 80\n"
        "      reason = 0\n"
        "      return_guard = 123"
    )
    complete_after = (
        "    elif session_command == 2 and state == 78 and session_sequence > consumed_session_sequence:\n"
        "      if input_epoch == active_epoch and input_ordinal == current_ordinal and input_token == current_token and input_kind == current_kind:\n"
        "        consumed_session_sequence = session_sequence\n"
        "        completed = True\n"
        "        state = 80\n"
        "        reason = 0\n"
        "        return_guard = 123\n"
        "      else:\n"
        "        reason = 63\n"
        "      end"
    )
    source, complete_count = source.replace(complete_before, complete_after, 1), source.count(complete_before)
    if complete_count != 1:
        raise R005ContractError("r004 TP COMPLETE guard was not found")
    redundant_initial_stationary_gate = (
        "  if not codex_r005_stationary(0.250000000):\n"
        "    return codex_r005_fault(epoch, ordinal, token, kind, consumed, 23, runtime_hi, runtime_lo)\n"
        "  end\n"
    )
    stationary_count = source.count(redundant_initial_stationary_gate)
    if stationary_count != 1:
        raise R005ContractError(
            "r004 TP initial stationary/reason23 gate was not found exactly once"
        )
    # Reuse the V3 READY_HOME_NEXT -> fresh ARM semantic: the host's fresh
    # pre-ARM stationary/Home/Safety boundary and TP fixed-Home ARM-entry
    # check already own this boundary.  Keep the packet, contact-stop, and
    # every return-home stationary check in the resident TP.
    source = source.replace(
        redundant_initial_stationary_gate,
        "  # R005 removes only the duplicate pre-execute stationary dwell; ARM/Home gates remain authoritative.\n",
        1,
    )
    source = (
        "# R005_ATTEMPT_SEQUENCE: positive monotonically increasing, unbounded; "
        "resident waits for next ARM after every safe return\n"
        "# R005_ASYNC_BOUND: two pending plus one physical inflight; host-owned\n"
        "# R005_RATE_CONTRACT: 500 Hz absolute-deadline pacing; writer/RTDE/Kunwei/TP gates >=460 Hz\n"
        + source
    )
    if "ordinal > 16" in source or "input_ordinal > 16" in source:
        raise R005ContractError("r005 TP renderer retained the old 1..16 ordinal gate")
    if "input_ordinal >= 14 and input_kind != 4" in source:
        raise R005ContractError("r005 TP renderer retained the old phase plan")
    if "r004" in source:
        raise R005ContractError("r005 TP renderer retained an r004 identity token")
    return source


def render_script(contract_path=None) -> str:
    contract = load_contract() if contract_path is None else load_contract(contract_path)
    try:
        from step5d_autotune_v4_r004.tp import render_script as render_r004
    except ImportError as exc:
        raise R005ContractError("r004 TP source is unavailable for content derivation") from exc
    source = render_r004()
    rendered = _replace_identity(
        source,
        contract_sha256=contract.sha256,
        fingerprint=contract.campaign_fingerprint,
    )
    if f"# TP_PROGRAM_ID: {PROGRAM}" not in rendered:
        raise R005ContractError("r005 TP program identity was not rendered")
    return rendered


__all__ = [
    "CONTROLLER_DIR",
    "RUNTIME_PROTOCOL",
    "ResidentTPModel",
    "TPProtocolError",
    "render_script",
]
