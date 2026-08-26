"""Step6-only composition seam for the Figure-eight active trial censor.

The seam reuses the reviewed R012 register handshake and is deliberately
single-writer: the existing PATH sink is called first, then the same sample is
observed by this Figure-eight typed runtime.  It never owns a second RTDE
reader/writer and never appends to the exact physical ledger.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import math
from typing import Any, Callable, Mapping

try:
    from step5d_autotune_v4_r012.live_host import PathEarlyEndController
except ModuleNotFoundError:  # pragma: no cover - repository-root imports
    from tools.step5d_autotune_v4_r012.live_host import PathEarlyEndController

from .core import (
    CENSOR_ACTIVE_RUN_KINDS,
    CENSOR_GUARD_BINS,
    CENSOR_KAPPA,
    FIGURE8_DURATION_S,
    FORMAL_BIN_COUNT,
    FigureEightCensoredReceiptV1,
    FigureEightError,
    make_figure8_censored_receipt,
)


class FigureEightCensorRuntimeError(RuntimeError):
    """The active censor could not prove the complete typed closure."""


def _sample_mapping(sample: Any) -> dict[str, Any]:
    if isinstance(sample, Mapping):
        return dict(sample)
    as_dict = getattr(sample, "as_dict", None)
    if callable(as_dict):
        value = as_dict()
        if isinstance(value, Mapping):
            return dict(value)
    value: dict[str, Any] = {}
    for name in ("state", "path_time_s", "time_s", "filtered_normal_n", "normal_load_n", "raw_normal_n"):
        if hasattr(sample, name):
            value[name] = getattr(sample, name)
    return value


def _sample_value(sample: Any, *names: str) -> float | None:
    value = _sample_mapping(sample)
    for name in names:
        if name in value:
            try:
                parsed = float(value[name])
            except (TypeError, ValueError):
                continue
            if math.isfinite(parsed):
                return parsed
    return None


def _is_short_duration_exception(error: BaseException) -> bool:
    """Recognize only the mature R013 exact-duration gate."""

    text = str(error)
    return "R013 exact trial duration is short" in text


@dataclass
class FigureEightPhysicalCensorRuntimeV1:
    candidate: Mapping[str, Any]
    campaign_fingerprint_sha256: str
    campaign_id: str
    run_id: str
    attempt_id: str
    trial_id: str
    run_kind: str
    controller: PathEarlyEndController
    confirmed_incumbent_mean_n: float | None
    attempt_sequence: int
    raw_samples: list[dict[str, Any]] = field(default_factory=list)
    closed_absolute_errors: list[float] = field(default_factory=list)
    _bin_index: int | None = None
    _bin_force_sum: float = 0.0
    _bin_sample_count: int = 0
    requested: bool = False

    def arm(self) -> None:
        self.raw_samples.clear()
        self.closed_absolute_errors.clear()
        self._bin_index = None
        self._bin_force_sum = 0.0
        self._bin_sample_count = 0
        self.requested = False
        self.controller.arm(self.attempt_sequence)

    @property
    def active(self) -> bool:
        return (
            self.run_kind in CENSOR_ACTIVE_RUN_KINDS
            and self.confirmed_incumbent_mean_n is not None
        )

    def _close_bin(self) -> None:
        if self._bin_index is None or self._bin_sample_count <= 0:
            return
        mean_force = self._bin_force_sum / float(self._bin_sample_count)
        self.closed_absolute_errors.append(abs(mean_force - 5.0))
        self._bin_force_sum = 0.0
        self._bin_sample_count = 0
        if (
            self.active
            and not self.requested
            and len(self.closed_absolute_errors) >= CENSOR_GUARD_BINS
            and math.fsum(self.closed_absolute_errors) / len(self.closed_absolute_errors)
            > CENSOR_KAPPA * float(self.confirmed_incumbent_mean_n)
        ):
            if not self.controller.request_early_end(self.attempt_sequence):
                raise FigureEightCensorRuntimeError(
                    "Figure-eight active censor request could not be sequence matched"
                )
            self.requested = True

    def observe_path_sample(self, sample: Any) -> None:
        row = _sample_mapping(sample)
        path_time = _sample_value(sample, "path_time_s", "time_s")
        force = _sample_value(sample, "filtered_normal_n", "normal_load_n", "raw_normal_n")
        state = row.get("state")
        if path_time is None or force is None:
            return
        row.setdefault("path_time_s", path_time)
        row.setdefault("time_s", path_time)
        row.setdefault("raw_measured_normal_n", force)
        self.raw_samples.append(row)
        if self.requested or state not in (None, 25, "PATH"):
            return
        if not 5.0 <= path_time < FIGURE8_DURATION_S:
            return
        index = int(math.floor((path_time - 5.0 + 1e-12) / 0.1))
        if not 0 <= index < FORMAL_BIN_COUNT:
            return
        if self._bin_index is None:
            self._bin_index = index
        elif index != self._bin_index:
            if index < self._bin_index:
                raise FigureEightCensorRuntimeError("Figure-eight PATH bin index regressed")
            self._close_bin()
            if self.requested:
                return
            self._bin_index = index
        self._bin_force_sum += force
        self._bin_sample_count += 1

    def seal(self, closure: Mapping[str, Any]) -> FigureEightCensoredReceiptV1:
        if not self.requested:
            raise FigureEightCensorRuntimeError("Figure-eight short trial was not requested by the active censor")
        self._close_bin()
        if len(self.closed_absolute_errors) < CENSOR_GUARD_BINS:
            raise FigureEightCensorRuntimeError("Figure-eight censor guard was not closed")
        if not self.controller.observe_live_completion():
            raise FigureEightCensorRuntimeError("Figure-eight PATH completion ack/terminal reason was not matched")
        ack, terminal_code, terminal_subtype = self.controller.read_completion_registers()
        if (ack, terminal_code, terminal_subtype) != (self.attempt_sequence, 0, 0):
            raise FigureEightCensorRuntimeError("Figure-eight terminal reason/subtype was not normal")
        required = ("return_guard_closed", "home_closed", "safe_return_closed", "home_calibrated")
        if not all(closure.get(name) is True for name in required):
            raise FigureEightCensorRuntimeError("Figure-eight censor closure lacks fresh return/Home evidence")
        if int(closure.get("return_guard_value", -1)) != 123:
            raise FigureEightCensorRuntimeError("Figure-eight return guard value is not 123")
        self.controller.handshake.begin_return_home()
        if not self.controller.handshake.finalize_censor(
            return_guard=True,
            home=True,
            safe_return=True,
        ):
            raise FigureEightCensorRuntimeError("Figure-eight return/Home closure could not be finalized")
        return make_figure8_censored_receipt(
            candidate=self.candidate,
            campaign_fingerprint_sha256=self.campaign_fingerprint_sha256,
            campaign_id=self.campaign_id,
            run_id=self.run_id,
            attempt_id=self.attempt_id,
            trial_id=self.trial_id,
            run_kind=self.run_kind,
            closed_absolute_errors=tuple(self.closed_absolute_errors),
            raw_samples=tuple(self.raw_samples),
            watermark_s=5.0 + len(self.closed_absolute_errors) * 0.1,
            confirmed_incumbent_mean_n=float(self.confirmed_incumbent_mean_n),
            request_sequence=self.attempt_sequence,
            ack_sequence=self.attempt_sequence,
            attempt_sequence=self.attempt_sequence,
            terminal_reason_code=0,
            terminal_reason_subtype=0,
            return_guard_value=123,
            home_calibrated=True,
            home_profile_id=str(closure.get("home_profile_id", "")),
            home_calibration_receipt_sha256=str(
                closure.get("home_calibration_receipt_sha256", "")
            ),
            home_observation=dict(closure.get("home_observation", {})),
        )


@dataclass
class FigureEightPhysicalCensorSeamV1:
    """Install/remove one Step6 wrapper around the existing PATH sink."""

    sink_owner: Any
    register_writer: Any
    closure_provider: Callable[[], Mapping[str, Any]]
    original_sink: Callable[[Any], None] | None = None
    runtime: FigureEightPhysicalCensorRuntimeV1 | None = None
    installed: bool = False

    def install(self) -> None:
        if self.installed:
            return
        original = getattr(self.sink_owner, "_path_sample_sink", None)
        if not callable(original):
            raise FigureEightCensorRuntimeError("Step6 could not bind the existing single PATH sample sink")
        self.original_sink = original

        def sink(sample: Any) -> None:
            assert self.original_sink is not None
            self.original_sink(sample)
            if self.runtime is not None:
                self.runtime.observe_path_sample(sample)

        self.sink_owner._path_sample_sink = sink
        self.installed = True

    def arm(
        self,
        *,
        candidate: Mapping[str, Any],
        campaign_fingerprint_sha256: str,
        campaign_id: str,
        run_id: str,
        attempt_id: str,
        trial_id: str,
        run_kind: str,
        confirmed_incumbent_mean_n: float | None,
        attempt_sequence: int,
    ) -> FigureEightPhysicalCensorRuntimeV1:
        self.install()
        self.runtime = FigureEightPhysicalCensorRuntimeV1(
            candidate=candidate,
            campaign_fingerprint_sha256=campaign_fingerprint_sha256,
            campaign_id=campaign_id,
            run_id=run_id,
            attempt_id=attempt_id,
            trial_id=trial_id,
            run_kind=run_kind,
            controller=PathEarlyEndController(writer=self.register_writer),
            confirmed_incumbent_mean_n=confirmed_incumbent_mean_n,
            attempt_sequence=attempt_sequence,
        )
        self.runtime.arm()
        return self.runtime

    def seal_short_exception(self, error: BaseException) -> FigureEightCensoredReceiptV1 | None:
        if not _is_short_duration_exception(error):
            return None
        if self.runtime is None or not self.runtime.requested:
            raise FigureEightCensorRuntimeError(
                "mature short-duration exception is not an active Figure-eight censor closure"
            )
        return self.runtime.seal(dict(self.closure_provider()))

    def close(self) -> None:
        if self.installed and self.original_sink is not None:
            self.sink_owner._path_sample_sink = self.original_sink
        self.runtime = None
        self.original_sink = None
        self.installed = False


def next_cold_attempt_sequence(
    physical_ledger: Any,
    censor_receipt_path: Any,
    *,
    run_id: str,
) -> int:
    """Derive the next segment-local TP ordinal from cold-readable evidence.

    Every new resident segment restarts its TP attempt ordinal.  Censored
    receipts are campaign-global, so only rows from the current ``run_id`` may
    participate in this sequence calculation.
    """

    if not isinstance(run_id, str) or not run_id:
        raise FigureEightCensorRuntimeError(
            "Figure-eight censor attempt sequence lacks a resident run id"
        )

    maximum = max(
        (int(getattr(row, "attempt_sequence", 0)) for row in getattr(physical_ledger, "records", ())),
        default=0,
    )
    path = getattr(censor_receipt_path, "is_file", lambda: False)()
    if path:
        import json
        for line in censor_receipt_path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                value = json.loads(line)
            except ValueError:
                continue
            receipt = value.get("censored_receipt") if isinstance(value, Mapping) else None
            if isinstance(receipt, Mapping) and receipt.get("run_id") == run_id:
                maximum = max(maximum, int(receipt.get("attempt_sequence", 0)))
    return maximum + 1


__all__ = [
    "FigureEightCensorRuntimeError",
    "FigureEightPhysicalCensorRuntimeV1",
    "FigureEightPhysicalCensorSeamV1",
    "next_cold_attempt_sequence",
]
