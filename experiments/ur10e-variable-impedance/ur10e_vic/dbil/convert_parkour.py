"""Deterministic converter for the pinned upstream Parkour text dataset."""

from __future__ import annotations

import csv
import json
import os
from pathlib import Path
import random
import subprocess
import tempfile
import numpy as np

from .config import DBILConfig
from .dataset import sha256_file, write_dataset_manifest


UPSTREAM_COMMIT = "8c05a4d4aca8012927a9fdf5bfcd8313247f6a30"
REQUIRED_COLUMNS = (
    "f_x",
    "f_y",
    "f_z",
    "m_x",
    "m_y",
    "m_z",
    "x",
    "y",
    "z",
    "x0",
    "y0",
    "z0",
    "u_x",
    "u_y",
    "u_z",
    "theta",
    "u0_x",
    "u0_y",
    "u0_z",
    "theta0",
)


def axis_angle_to_quaternion_wxyz(axis: np.ndarray, angle: np.ndarray) -> np.ndarray:
    if axis.ndim != 2 or axis.shape[1] != 3 or angle.shape != (axis.shape[0],):
        raise ValueError("axis/angle arrays must have shapes [N,3] and [N]")
    norm = np.linalg.norm(axis, axis=1, keepdims=True)
    normalized_axis = axis / np.maximum(norm, 1e-12)
    half = 0.5 * angle
    quaternion = np.concatenate(
        (np.cos(half)[:, None], normalized_axis * np.sin(half)[:, None]), axis=1
    )
    zero_axis = norm[:, 0] <= 1e-12
    quaternion[zero_axis] = np.asarray((1.0, 0.0, 0.0, 0.0))
    quaternion[quaternion[:, 0] < 0.0] *= -1.0
    return quaternion.astype(np.float32)


def _read_text(path: Path) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    columns: dict[str, list[float]] = {name: [] for name in REQUIRED_COLUMNS}
    with path.open("r", encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream, delimiter="\t")
        if reader.fieldnames is None or not set(REQUIRED_COLUMNS) <= set(reader.fieldnames):
            raise ValueError(f"{path} does not contain the required Parkour columns")
        for row_index, row in enumerate(reader, start=2):
            if not row or all(value in (None, "") for value in row.values()):
                continue
            try:
                values = {name: float(row[name]) for name in REQUIRED_COLUMNS}
            except (TypeError, ValueError) as error:
                raise ValueError(f"non-numeric row {row_index} in {path}") from error
            if not all(np.isfinite(value) for value in values.values()):
                raise ValueError(f"non-finite row {row_index} in {path}")
            for name, value in values.items():
                columns[name].append(value)
    if len(columns["x"]) < DBILConfig().history_window:
        raise ValueError(f"{path} has fewer than 16 usable rows")
    position = np.column_stack((columns["x"], columns["y"], columns["z"]))
    target_position = np.column_stack(
        (columns["x0"], columns["y0"], columns["z0"])
    )
    quaternion = axis_angle_to_quaternion_wxyz(
        np.column_stack((columns["u_x"], columns["u_y"], columns["u_z"])),
        np.asarray(columns["theta"]),
    )
    target_quaternion = axis_angle_to_quaternion_wxyz(
        np.column_stack((columns["u0_x"], columns["u0_y"], columns["u0_z"])),
        np.asarray(columns["theta0"]),
    )
    pose = np.concatenate((position, quaternion), axis=1).astype(np.float32)
    target = np.concatenate((target_position, target_quaternion), axis=1).astype(
        np.float32
    )
    wrench = np.column_stack(
        tuple(columns[name] for name in ("f_x", "f_y", "f_z", "m_x", "m_y", "m_z"))
    ).astype(np.float32)
    return pose, wrench, target


def _split_by_file(paths: list[Path], seed: int) -> dict[Path, int]:
    if len(paths) < 3:
        raise ValueError("at least three non-application files are required for file-level splits")
    shuffled = list(paths)
    random.Random(seed).shuffle(shuffled)
    validation_count = max(1, round(len(shuffled) * 0.15))
    test_count = max(1, round(len(shuffled) * 0.15))
    train_count = len(shuffled) - validation_count - test_count
    if train_count <= 0:
        raise ValueError("not enough files to retain a training split")
    result: dict[Path, int] = {}
    for path in shuffled[:train_count]:
        result[path] = 0
    for path in shuffled[train_count : train_count + validation_count]:
        result[path] = 1
    for path in shuffled[train_count + validation_count :]:
        result[path] = 2
    return result


def convert_parkour_dataset(
    source_root: Path,
    dataset_output: Path,
    manifest_output: Path,
    stats_output: Path,
    *,
    source_git_root: Path | None = None,
    seed: int = 42,
    overlap_stride: int = 5,
) -> dict[str, object]:
    if seed != 42 or overlap_stride != 5:
        raise ValueError("first reproduction pins seed=42 and overlap stride=5")
    source_root = source_root.resolve()
    git_root = source_git_root.resolve() if source_git_root is not None else None
    if git_root is not None:
        resolved_commit = subprocess.run(
            ("git", "-C", str(git_root), "rev-parse", UPSTREAM_COMMIT),
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        if resolved_commit != UPSTREAM_COMMIT:
            raise ValueError("source Git repository does not contain the pinned commit")
    paths = sorted(
        path
        for path in source_root.rglob("*.txt")
        if path.name != "TEST_NLS_FREE.txt"
    )
    regular = [path for path in paths if "ApplicationData" not in path.parts]
    application = [path for path in paths if "ApplicationData" in path.parts]
    split_by_path = _split_by_file(regular, seed)
    split_by_path.update({path: 3 for path in application})

    pose_windows: list[np.ndarray] = []
    wrench_windows: list[np.ndarray] = []
    target_windows: list[np.ndarray] = []
    splits: list[int] = []
    source_ids: list[int] = []
    window_starts: list[int] = []
    source_manifest: list[dict[str, object]] = []
    for source_id, path in enumerate(paths):
        pose, wrench, target = _read_text(path)
        relative_path = path.relative_to(source_root)
        git_blob_oid: str | None = None
        if git_root is not None:
            git_path = f"Data/Parkour/{relative_path.as_posix()}"
            git_blob_oid = subprocess.run(
                ("git", "-C", str(git_root), "rev-parse", f"{UPSTREAM_COMMIT}:{git_path}"),
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            actual_blob_oid = subprocess.run(
                ("git", "hash-object", str(path)),
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            if actual_blob_oid != git_blob_oid:
                raise ValueError(f"source file is not bound to pinned Git blob: {relative_path}")
        split_id = split_by_path[path]
        stride = 16 if split_id == 3 else overlap_stride
        starts = range(0, pose.shape[0] - 16 + 1, stride)
        window_count = 0
        for start in starts:
            pose_windows.append(pose[start : start + 16])
            wrench_windows.append(wrench[start : start + 16])
            target_windows.append(target[start : start + 16])
            splits.append(split_id)
            source_ids.append(source_id)
            window_starts.append(start)
            window_count += 1
        source_manifest.append(
            {
                "id": source_id,
                "path": str(relative_path),
                "git_blob_oid": git_blob_oid,
                "sha256": sha256_file(path),
                "rows": int(pose.shape[0]),
                "windows": window_count,
                "split_id": split_id,
            }
        )
    if not pose_windows:
        raise ValueError("conversion produced no windows")
    dataset_output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        dir=dataset_output.parent, suffix=".npz", delete=False
    ) as temporary:
        temporary_path = Path(temporary.name)
        np.savez_compressed(
            temporary,
            pose_history=np.stack(pose_windows).astype(np.float32),
            wrench_history=np.stack(wrench_windows).astype(np.float32),
            target_s_zft=np.stack(target_windows).astype(np.float32),
            split=np.asarray(splits, dtype=np.int8),
            source_file_id=np.asarray(source_ids, dtype=np.int16),
            window_start=np.asarray(window_starts, dtype=np.int32),
        )
    os.replace(temporary_path, dataset_output)
    manifest = write_dataset_manifest(
        dataset_output, manifest_output, stats_output_path=stats_output
    )
    manifest.update(
        {
            "upstream_commit": UPSTREAM_COMMIT if git_root is not None else None,
            "upstream_binding_verified": git_root is not None,
            "source_root": "Data/Parkour" if git_root is not None else source_root.name,
            "path_semantics": "source paths are pinned-commit-relative; artifacts are bundle-relative",
            "source_files": source_manifest,
            "split_policy": "file-level deterministic 70/15/15; ApplicationData held separately",
            "overlap_stride": overlap_stride,
            "seed": seed,
        }
    )
    manifest_output.write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return manifest
