"""Cold-read dataset assembly for completed TacDiffusion formal V4 campaigns."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import math
import os
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Mapping

import numpy as np

from .action import MODEL_MODE_FIXED_K_V1, MODEL_MODE_VARIABLE_K_V1
from .eligibility import read_formal_eligibility_receipt
from .episode_recorder import validate_formal_episode_artifact
from .formal_campaign import FormalCampaignContractV1
from .formal_model import FormalDatasetManifestV1
from .formal_orchestration import (
    FormalAttemptOutcome,
    FormalCampaignLedgerV1,
)
from .mainline_dataset import (
    validate_mainline_dataset,
    write_mainline_dataset,
    write_mainline_manifest,
)


FORMAL_DATASET_BUILD_SCHEMA_V1 = "ur10e_tacdiffusion_formal_dataset_build/v1"
FORMAL_DATASET_SPLIT_COUNTS = {"train": 140, "validation": 30, "test": 30}


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _canonical_sha256(payload: Mapping[str, object]) -> str:
    return hashlib.sha256(
        json.dumps(
            dict(payload), sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode("utf-8")
    ).hexdigest()


def _require_sha256(value: object, name: str) -> str:
    text = str(value)
    if len(text) != 64 or any(character not in "0123456789abcdef" for character in text):
        raise ValueError(f"{name} must be a lowercase SHA-256")
    return text


def split_for_eligible_ordinal(eligible_ordinal: int) -> str:
    index = int(eligible_ordinal)
    if not 0 <= index < 200:
        raise ValueError("formal eligible ordinal must be within [0,200)")
    if index < 140:
        return "train"
    if index < 170:
        return "validation"
    return "test"


def _atomic_npz(path: Path, arrays: Mapping[str, np.ndarray]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with NamedTemporaryFile(
            "w+b", dir=path.parent, prefix=f".{path.name}.", delete=False
        ) as handle:
            temporary = Path(handle.name)
            np.savez_compressed(handle, **arrays)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        directory_fd = os.open(path.parent, os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _atomic_json(path: Path, payload: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile(
        "w", encoding="utf-8", dir=path.parent, prefix=f".{path.name}.", delete=False
    ) as handle:
        temporary = Path(handle.name)
        json.dump(dict(payload), handle, indent=2, sort_keys=True, allow_nan=False)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    try:
        os.replace(temporary, path)
        directory_fd = os.open(path.parent, os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        temporary.unlink(missing_ok=True)


@dataclass(frozen=True)
class FormalDatasetBuildResultV1:
    campaign_id: str
    mode: str
    mainline_dataset_path: Path
    mainline_manifest_path: Path
    training_dataset_path: Path
    training_manifest_path: Path
    row_count: int
    episode_count: int
    split_episode_counts: Mapping[str, int]
    source_artifact_hashes: Mapping[str, str]
    build_manifest_sha256: str

    def as_json(self) -> dict[str, object]:
        return {
            "schema_version": FORMAL_DATASET_BUILD_SCHEMA_V1,
            "campaign_id": self.campaign_id,
            "mode": self.mode,
            "mainline_dataset_path": str(self.mainline_dataset_path),
            "mainline_dataset_sha256": _sha256_file(self.mainline_dataset_path),
            "mainline_manifest_path": str(self.mainline_manifest_path),
            "mainline_manifest_sha256": _sha256_file(self.mainline_manifest_path),
            "training_dataset_path": str(self.training_dataset_path),
            "training_dataset_sha256": _sha256_file(self.training_dataset_path),
            "training_manifest_path": str(self.training_manifest_path),
            "training_manifest_sha256": _sha256_file(self.training_manifest_path),
            "row_count": self.row_count,
            "episode_count": self.episode_count,
            "split_episode_counts": dict(self.split_episode_counts),
            "source_artifact_hashes": dict(self.source_artifact_hashes),
            "build_manifest_sha256": self.build_manifest_sha256,
            "active_enabled": False,
            "shadow_only": True,
        }


def build_formal_campaign_dataset(
    *,
    campaign_root: str | Path,
    contract: FormalCampaignContractV1,
    output_dir: str | Path,
    surface_calibration_sha256: str,
    action_profile_sha256: str,
    filter_profile_sha256: str,
    normalization_sha256: str,
) -> FormalDatasetBuildResultV1:
    """Build 84D/12D and typed 6D/7D datasets from exactly 200 receipts."""

    for name, value in (
        ("surface_calibration_sha256", surface_calibration_sha256),
        ("action_profile_sha256", action_profile_sha256),
        ("filter_profile_sha256", filter_profile_sha256),
        ("normalization_sha256", normalization_sha256),
    ):
        _require_sha256(value, name)
    mode = (
        MODEL_MODE_FIXED_K_V1
        if contract.kind == "fixed_k"
        else MODEL_MODE_VARIABLE_K_V1
    )
    ledger = FormalCampaignLedgerV1(campaign_root, contract)
    records = ledger.records(verify_artifacts=True)
    eligible_records = tuple(
        record
        for record in records
        if record.outcome == FormalAttemptOutcome.ELIGIBLE.value
    )
    if len(eligible_records) != contract.total_episodes:
        raise ValueError("formal dataset requires exactly 200 eligible episodes")
    if tuple(record.eligible_ordinal for record in eligible_records) != tuple(range(200)):
        raise ValueError("formal dataset eligible ordinals are not contiguous")

    observations: list[tuple[float, ...]] = []
    actions_12d: list[tuple[float, ...]] = []
    training_targets: list[tuple[float, ...]] = []
    episode_ids: list[str] = []
    splits: list[str] = []
    timestamps: list[float] = []
    source_hashes: dict[str, str] = {}
    split_episode_counts = {"train": 0, "validation": 0, "test": 0}
    root = Path(campaign_root).resolve()
    for record in eligible_records:
        assert record.artifact_path is not None
        assert record.recorder_manifest_path is not None
        assert record.eligibility_path is not None
        artifact_path = (root / record.artifact_path).resolve()
        recorder_manifest_path = (root / record.recorder_manifest_path).resolve()
        eligibility_path = (root / record.eligibility_path).resolve()
        _, rows = validate_formal_episode_artifact(
            artifact_path, recorder_manifest_path
        )
        receipt = read_formal_eligibility_receipt(eligibility_path)
        if (
            receipt.get("formal_eligible") is not True
            or receipt.get("training_eligible") is not True
            or int(receipt.get("row_count", -1)) != len(rows)
        ):
            raise ValueError("formal dataset encountered a non-eligible receipt")
        episode_id = record.attempt_id
        split = split_for_eligible_ordinal(record.eligible_ordinal)
        split_episode_counts[split] += 1
        source_hashes[episode_id] = str(record.artifact_sha256)
        previous_time = -math.inf
        for row in rows:
            observation = tuple(float(value) for value in row["observation_84d"])
            action = tuple(float(value) for value in row["expert_action_12d"])
            timestamp = float(row["control_time_s"])
            if len(observation) != 84 or len(action) != 12:
                raise ValueError("formal dataset row dimensions changed")
            if not all(math.isfinite(value) for value in observation + action):
                raise ValueError("formal dataset row contains non-finite values")
            if not math.isfinite(timestamp) or timestamp <= previous_time:
                raise ValueError("formal dataset row time is not strictly increasing")
            previous_time = timestamp
            if mode == MODEL_MODE_FIXED_K_V1:
                target = action[:6]
            else:
                if not (
                    math.isclose(action[6], action[7], abs_tol=1e-9)
                    and math.isclose(action[7], action[8], abs_tol=1e-9)
                    and 400.0 <= action[6] <= 800.0
                    and action[9:] == (30.0, 30.0, 30.0)
                ):
                    raise ValueError("variable-K formal action violates 7D label contract")
                target = action[:6] + (action[6],)
            observations.append(observation)
            actions_12d.append(action)
            training_targets.append(target)
            episode_ids.append(episode_id)
            splits.append(split)
            timestamps.append(timestamp)

    if split_episode_counts != FORMAL_DATASET_SPLIT_COUNTS:
        raise ValueError("formal episode split must be exactly 140/30/30")
    output = Path(output_dir)
    output.mkdir(parents=True, exist_ok=False)
    mainline_path = output / "dataset_84d_12d.npz"
    mainline_manifest_path = output / "dataset_84d_12d.manifest.json"
    mainline_manifest = write_mainline_dataset(
        mainline_path,
        observations=observations,
        actions=actions_12d,
        episode_ids=episode_ids,
        splits=splits,
        timestamps_s=timestamps,
        source_raw_artifact_hashes=source_hashes,
        surface_calibration_sha256=surface_calibration_sha256,
        action_profile_sha256=action_profile_sha256,
        filter_profile_sha256=filter_profile_sha256,
        normalization_sha256=normalization_sha256,
    )
    write_mainline_manifest(mainline_manifest_path, mainline_manifest)
    validate_mainline_dataset(mainline_path, mainline_manifest)

    observation_array = np.asarray(observations, dtype=np.float64)
    target_array = np.asarray(training_targets, dtype=np.float64)
    formal_manifest = FormalDatasetManifestV1.from_arrays(
        observation_array,
        target_array,
        dataset_id=f"{contract.campaign_id}_dataset_v1",
        mode=mode,
        source_hashes=source_hashes,
    )
    training_path = output / f"dataset_84d_{target_array.shape[1]}d.npz"
    _atomic_npz(
        training_path,
        {
            "observations": observation_array,
            "targets": target_array,
            "episode_ids": np.asarray(episode_ids),
            "splits": np.asarray(splits),
            "timestamps_s": np.asarray(timestamps, dtype=np.float64),
        },
    )
    training_manifest_path = output / f"dataset_84d_{target_array.shape[1]}d.manifest.json"
    training_manifest_payload = formal_manifest.as_json() | {
        "artifact": training_path.name,
        "artifact_sha256": _sha256_file(training_path),
        "episode_count": 200,
        "split_episode_counts": split_episode_counts,
        "mainline_manifest_sha256": _sha256_file(mainline_manifest_path),
    }
    _atomic_json(training_manifest_path, training_manifest_payload)
    build_payload = {
        "schema_version": FORMAL_DATASET_BUILD_SCHEMA_V1,
        "campaign_id": contract.campaign_id,
        "mode": mode,
        "mainline_dataset_sha256": _sha256_file(mainline_path),
        "mainline_manifest_sha256": _sha256_file(mainline_manifest_path),
        "training_dataset_sha256": _sha256_file(training_path),
        "training_manifest_sha256": _sha256_file(training_manifest_path),
        "row_count": len(observations),
        "episode_count": 200,
        "split_episode_counts": split_episode_counts,
        "source_artifact_hashes": source_hashes,
        "active_enabled": False,
        "shadow_only": True,
    }
    build_sha = _canonical_sha256(build_payload)
    _atomic_json(output / "dataset_build_receipt.json", build_payload | {"build_manifest_sha256": build_sha})
    return FormalDatasetBuildResultV1(
        campaign_id=contract.campaign_id,
        mode=mode,
        mainline_dataset_path=mainline_path,
        mainline_manifest_path=mainline_manifest_path,
        training_dataset_path=training_path,
        training_manifest_path=training_manifest_path,
        row_count=len(observations),
        episode_count=200,
        split_episode_counts=split_episode_counts,
        source_artifact_hashes=source_hashes,
        build_manifest_sha256=build_sha,
    )


__all__ = [
    "FORMAL_DATASET_BUILD_SCHEMA_V1",
    "FORMAL_DATASET_SPLIT_COUNTS",
    "FormalDatasetBuildResultV1",
    "build_formal_campaign_dataset",
    "split_for_eligible_ordinal",
]
