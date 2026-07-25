"""Latest-model mailbox with monotonic identity and stale/nonfinite gates."""

from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Optional

from .action import ActionProfile, TacDiffusionAction, guard_action
from .dynamic_filter import DynamicFilterState, RateInvariantForceFilter


@dataclass(frozen=True)
class ModelPacket:
    sequence: int
    timestamp_s: float
    action: TacDiffusionAction
    profile: ActionProfile
    inference_latency_s: float
    mode: str = "shadow"

    def __post_init__(self) -> None:
        if self.sequence < 0 or not math.isfinite(self.timestamp_s) or self.timestamp_s < 0.0 or not math.isfinite(self.inference_latency_s) or self.inference_latency_s < 0.0:
            raise ValueError("model packet metadata is invalid")
        if self.mode not in {"shadow", "active"}:
            raise ValueError("model packet mode must be shadow or active")
        if not all(math.isfinite(value) for value in self.action.vector12):
            raise ValueError("model packet action is non-finite")


@dataclass(frozen=True)
class MailboxRead:
    packet: Optional[ModelPacket]
    reason: str


@dataclass(frozen=True)
class EpisodeModelStep:
    filtered_state: DynamicFilterState
    episode_failed: bool
    reason: str


class LatestModelMailbox:
    def __init__(self) -> None:
        self._packet: ModelPacket | None = None

    def publish(self, packet: ModelPacket) -> None:
        if packet.action.frame_id != packet.profile.frame_id:
            raise ValueError("model packet action/profile frame mismatch")
        if self._packet is not None and packet.sequence <= self._packet.sequence:
            raise ValueError("model sequence must increase monotonically")
        self._packet = packet

    def read(self, *, now_s: float, max_age_s: float) -> MailboxRead:
        if not math.isfinite(now_s) or now_s < 0.0 or not math.isfinite(max_age_s) or max_age_s <= 0.0:
            raise ValueError("mailbox read time/age is invalid")
        packet = self._packet
        if packet is None:
            return MailboxRead(None, "empty")
        age = now_s - packet.timestamp_s
        if age < 0.0:
            return MailboxRead(None, "future_packet")
        if age > max_age_s:
            return MailboxRead(None, "stale")
        if not all(math.isfinite(value) for value in packet.action.vector12):
            return MailboxRead(None, "nonfinite")
        return MailboxRead(packet, "fresh")

    def episode_step(self, filter_: RateInvariantForceFilter, *, now_s: float, dt_s: float, max_age_s: float) -> EpisodeModelStep:
        """Resolve one episode tick; stale/nonfinite only fail this episode."""
        read = self.read(now_s=now_s, max_age_s=max_age_s)
        if read.packet is None:
            return EpisodeModelStep(filter_.smooth_to_zero(dt_s=dt_s), True, read.reason)
        try:
            state = filter_.step(read.packet.action.raw_f_df, dt_s=dt_s)
        except ValueError:
            return EpisodeModelStep(filter_.smooth_to_zero(dt_s=dt_s), True, "nonfinite")
        return EpisodeModelStep(state, False, "fresh")

    @property
    def latest_sequence(self) -> int | None:
        return None if self._packet is None else self._packet.sequence

    @property
    def latest_mode(self) -> str | None:
        return None if self._packet is None else self._packet.mode
