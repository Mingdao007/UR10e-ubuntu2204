"""Boundary-safe preparation and closeout helpers for V5 extension epochs."""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import time
from typing import Any, Mapping, Sequence

from .v5_campaign import CampaignIdentityV2, CampaignRoleV2, ENTRY_MODE_HOME_ONLY_V1
from .v5_extension import (
    ACTIVE_SET_MAX,
    EXTENSION_EXACT_TARGET,
    V5ExtensionEpochReceiptV1,
    V5ExtensionError,
    build_active_set,
    canonical_sha256,
    derive_extension_fingerprint,
    verify_active_set,
    verify_epoch_receipt,
)


EXTENSION_RUNNER_SCHEMA = "step6.autotune/figure8-v5-extension-runner-v1"
EXTENSION_RUNNER_VERSION = 1


def _atomic_json(path: Path, value: Mapping[str, Any]) -> None:
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(destination.name + f".tmp-{os.getpid()}-{time.time_ns()}")
    try:
        with temporary.open("x", encoding="utf-8") as stream:
            json.dump(dict(value), stream, sort_keys=True, indent=2, ensure_ascii=False, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, destination)
        directory_fd = os.open(destination.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if temporary.exists():
            temporary.unlink()


@dataclass(frozen=True)
class V5ExtensionEpochInputsV1:
    root: Path
    state_root: Path
    release_identity_sha256: str
    parent_campaign_fingerprint_sha256: str
    parent_ledger_head_sha256: str
    parent_epoch: int
    observations: Sequence[Mapping[str, Any]]
    epoch: int


class V5ExtensionEpochV1:
    """Own one fresh extension namespace and its deterministic parent seed."""

    def __init__(self, inputs: V5ExtensionEpochInputsV1) -> None:
        if not isinstance(inputs, V5ExtensionEpochInputsV1):
            raise TypeError("extension inputs must be typed")
        if inputs.epoch != inputs.parent_epoch + 1 or inputs.epoch < 1:
            raise V5ExtensionError("extension epoch is not the immediate successor")
        self.inputs = inputs
        self.root = Path(inputs.root).resolve()
        self.state_root = Path(inputs.state_root).resolve()
        self.fingerprint = derive_extension_fingerprint(
            release_identity_sha256=inputs.release_identity_sha256,
            parent_campaign_fingerprint_sha256=inputs.parent_campaign_fingerprint_sha256,
            parent_ledger_head_sha256=inputs.parent_ledger_head_sha256,
            epoch=inputs.epoch,
        )
        self.active_set = build_active_set(
            inputs.observations,
            parent_fingerprint_sha256=inputs.parent_campaign_fingerprint_sha256,
            parent_ledger_head_sha256=inputs.parent_ledger_head_sha256,
            max_size=ACTIVE_SET_MAX,
        )
        self._prepare_root()

    def _identity_body(self) -> dict[str, Any]:
        return {
            "schema": EXTENSION_RUNNER_SCHEMA,
            "version": EXTENSION_RUNNER_VERSION,
            "entry_mode": ENTRY_MODE_HOME_ONLY_V1,
            "epoch": self.inputs.epoch,
            "parent_epoch": self.inputs.parent_epoch,
            "release_identity_sha256": self.inputs.release_identity_sha256,
            "parent_campaign_fingerprint_sha256": self.inputs.parent_campaign_fingerprint_sha256,
            "parent_ledger_head_sha256": self.inputs.parent_ledger_head_sha256,
            "campaign_fingerprint_sha256": self.fingerprint,
            "active_set_sha256": self.active_set["active_set_sha256"],
            "exact_novel_target": EXTENSION_EXACT_TARGET,
            "top3_total_n": 5,
            "correction_enabled": False,
        }

    def _prepare_root(self) -> None:
        marker = self.state_root / "extension_identity.json"
        active = self.state_root / "active_set.json"
        expected = {**self._identity_body(), "identity_sha256": canonical_sha256(self._identity_body())}
        if marker.is_file() or active.is_file():
            if not marker.is_file() or not active.is_file():
                raise V5ExtensionError("extension root is partially materialized")
            if json.loads(marker.read_text(encoding="utf-8")) != expected:
                raise V5ExtensionError("extension identity changed on resume")
            verify_active_set(json.loads(active.read_text(encoding="utf-8")))
            return
        if self.state_root.exists() and any(self.state_root.iterdir()):
            raise V5ExtensionError("extension state root is non-fresh")
        self.state_root.mkdir(parents=True, exist_ok=True)
        _atomic_json(active, self.active_set)
        _atomic_json(marker, expected)

    def campaign_identity(self) -> CampaignIdentityV2:
        return CampaignIdentityV2(
            role=CampaignRoleV2.PRIMARY,
            campaign_fingerprint=self.fingerprint,
            release_identity_sha256=self.inputs.release_identity_sha256,
            state_root=self.state_root / "primary" / "campaign",
            observation_namespace=f"PRIMARY:{self.fingerprint}",
            entry_mode=ENTRY_MODE_HOME_ONLY_V1,
        )

    def closeout(
        self,
        *,
        physical_ledger_head_sha256: str,
        exact_novel_count: int,
        top3_total_n: int,
        status: str = "CLOSED",
    ) -> dict[str, Any]:
        receipt = V5ExtensionEpochReceiptV1(
            epoch=self.inputs.epoch,
            parent_epoch=self.inputs.parent_epoch,
            parent_campaign_fingerprint_sha256=self.inputs.parent_campaign_fingerprint_sha256,
            parent_ledger_head_sha256=self.inputs.parent_ledger_head_sha256,
            campaign_fingerprint_sha256=self.fingerprint,
            physical_ledger_head_sha256=physical_ledger_head_sha256,
            active_set_sha256=self.active_set["active_set_sha256"],
            exact_novel_count=exact_novel_count,
            top3_total_n=top3_total_n,
            status=status,
        ).as_dict()
        _atomic_json(self.state_root / "epoch_closeout.json", receipt)
        return receipt

    def verify(self) -> dict[str, Any]:
        identity = json.loads((self.state_root / "extension_identity.json").read_text(encoding="utf-8"))
        active = verify_active_set(json.loads((self.state_root / "active_set.json").read_text(encoding="utf-8")))
        result: dict[str, Any] = {"identity": identity, "active_set": active}
        closeout = self.state_root / "epoch_closeout.json"
        result["closeout"] = None if not closeout.is_file() else verify_epoch_receipt(json.loads(closeout.read_text(encoding="utf-8"))).as_dict()
        return result


__all__ = ["EXTENSION_RUNNER_SCHEMA", "V5ExtensionEpochInputsV1", "V5ExtensionEpochV1"]
