"""Build content-addressed catalog and R014 executable source closure."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .catalog import catalog_document
from .common import R014Error, atomic_write_json, sha256_file


HOST_SOURCE_PATHS = (
    "autotuner.sh",
    "tools/run_step5d_autotuner_r014.py",
    "tools/promote_step5d_autotuner_r014_qualification.py",
    "tools/r014_capture_source.py",
    "tools/step5d_autotune_v4_r013/contact_search_strategy.py",
    "tools/step5d_autotune_v4_r013/controller_triplet.py",
    "tools/step5d_autotune_v4_r004/calibrated_runtime.py",
    "tools/step5d_autotune_v4_r014/__init__.py",
    "tools/step5d_autotune_v4_r014/assets.py",
    "tools/step5d_autotune_v4_r014/catalog.py",
    "tools/step5d_autotune_v4_r014/certification.py",
    "tools/step5d_autotune_v4_r014/common.py",
    "tools/step5d_autotune_v4_r014/dispatcher.py",
    "tools/step5d_autotune_v4_r014/discovery.py",
    "tools/step5d_autotune_v4_r014/metrics.py",
    "tools/step5d_autotune_v4_r014/profiles.py",
    "tools/step5d_autotune_v4_r014/qualification.py",
    "tools/step5d_autotune_v4_r014/solver_profile.py",
)


def build_assets(experiment_root: Path) -> dict[str, Any]:
    experiment_root = experiment_root.resolve()
    registry = experiment_root / "config/step5d/autotuner_strategies"
    catalog = catalog_document()
    catalog_sha = str(catalog["catalog_sha256"])
    catalog_path = registry / "catalogs" / catalog_sha / "catalog.json"
    if catalog_path.exists() and catalog_path.read_text(encoding="utf-8"):
        # atomic_write_json below is idempotent; this branch documents that an
        # existing content address is deliberately reused.
        pass
    atomic_write_json(catalog_path, catalog)

    files: dict[str, str] = {}
    for relative in HOST_SOURCE_PATHS:
        path = experiment_root / relative
        if not path.is_file():
            raise R014Error(f"R014 host-source file is missing: {relative}")
        files[relative] = sha256_file(path)
    manifest = {
        "schema": "step5d.autotuner-r014/host-source-closure-v1",
        "version": 1,
        "source_capture": "config/step5d/r014_source_capture.json",
        "files": files,
    }
    # This identity is the hash of the stored bytes, matching the existing
    # Step5d release-pointer convention.
    staging = registry / "source-closures" / "staging" / "manifest.json"
    atomic_write_json(staging, manifest)
    manifest_sha = sha256_file(staging)
    manifest_path = registry / "source-closures" / manifest_sha / "manifest.json"
    if manifest_path.exists() and manifest_path.read_bytes() != staging.read_bytes():
        raise R014Error("R014 host-source content-address collision")
    atomic_write_json(manifest_path, manifest)
    staging.unlink()
    try:
        staging.parent.rmdir()
    except OSError:
        pass
    atomic_write_json(
        registry / "r014-host-source.json",
        {
            "schema": "step5d.autotuner-r014/host-source-pointer-v1",
            "version": 1,
            "manifest_path": f"source-closures/{manifest_sha}/manifest.json",
            "manifest_sha256": manifest_sha,
        },
    )
    return {
        "catalog_path": str(catalog_path),
        "catalog_sha256": catalog_sha,
        "host_source_manifest_path": str(manifest_path),
        "host_source_manifest_sha256": manifest_sha,
    }
