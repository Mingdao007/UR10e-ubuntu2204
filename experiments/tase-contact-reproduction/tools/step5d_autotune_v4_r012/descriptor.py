"""Typed, readable identity used by the R012 live boundary."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from .behavior import R012_PROGRAM, R012_RUNTIME_PROTOCOL
from .common import R012ValueError, json_tree


R012_REVISION = 12
R012_MOTION_PROTOCOL = 606006


class R012DescriptorError(R012ValueError):
    """A readable R012 live descriptor is malformed or mismatched."""


@dataclass(frozen=True)
class R012LiveDescriptor:
    program: str
    revision: int
    motion_protocol: int
    extension_protocol: int
    campaign_id: str
    run_id: str
    attempt_id: str
    route_id: str
    session_id: str
    session_epoch: int

    def __post_init__(self) -> None:
        if (self.program, self.revision, self.motion_protocol, self.extension_protocol) != (
            R012_PROGRAM, R012_REVISION, R012_MOTION_PROTOCOL, R012_RUNTIME_PROTOCOL
        ):
            raise R012DescriptorError("R012 readable program or protocol differs")
        if not all(isinstance(value, str) and value for value in (
            self.campaign_id, self.run_id, self.attempt_id, self.route_id, self.session_id
        )):
            raise R012DescriptorError("R012 readable route/session identity is invalid")
        if isinstance(self.session_epoch, bool) or not isinstance(self.session_epoch, int) or self.session_epoch <= 0:
            raise R012DescriptorError("R012 session epoch is invalid")

    def as_dict(self) -> dict[str, Any]:
        return {
            "program": self.program,
            "revision": self.revision,
            "motion_protocol": self.motion_protocol,
            "extension_protocol": self.extension_protocol,
            "campaign_id": self.campaign_id,
            "run_id": self.run_id,
            "attempt_id": self.attempt_id,
            "route_id": self.route_id,
            "session_id": self.session_id,
            "session_epoch": self.session_epoch,
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "R012LiveDescriptor":
        required = {
            "program", "revision", "motion_protocol", "extension_protocol",
            "campaign_id", "run_id", "attempt_id", "route_id", "session_id", "session_epoch",
        }
        if not isinstance(value, Mapping) or set(value) != required:
            raise R012DescriptorError("R012 readable descriptor fields differ")
        return cls(**json_tree(value))


def build_descriptor(
    *, campaign_id: str, run_id: str, attempt_id: str,
    route_id: str | None = None, session_id: str | None = None, session_epoch: int = 1,
) -> R012LiveDescriptor:
    return R012LiveDescriptor(
        program=R012_PROGRAM,
        revision=R012_REVISION,
        motion_protocol=R012_MOTION_PROTOCOL,
        extension_protocol=R012_RUNTIME_PROTOCOL,
        campaign_id=campaign_id,
        run_id=run_id,
        attempt_id=attempt_id,
        route_id=route_id or f"r012-route-{run_id}",
        session_id=session_id or f"r012-session-{run_id}",
        session_epoch=session_epoch,
    )


__all__ = [
    "R012LiveDescriptor", "R012DescriptorError", "R012_MOTION_PROTOCOL", "R012_REVISION",
    "build_descriptor",
]
