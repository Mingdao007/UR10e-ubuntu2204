"""Content-addressed strategy profiles and qualified-pointer resolution."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from .common import R014Error, atomic_write_json, load_json, sha256_file, sha256_value
from .catalog import catalog_document
from .metrics import METRIC_FINGERPRINT


PROFILE_SCHEMA = "step5d.autotuner-strategy-profile-v1"
POINTER_SCHEMA = "step5d.autotuner-current-qualified-pointer-v1"
FORMAL_PROFILE_NAME = "finite-time-r08-formal-v1"
LEGACY_PROFILE_NAME = "legacy-r1-v1"


@dataclass(frozen=True)
class StrategyProfile:
    path: Path
    sha256: str
    raw: Mapping[str, Any]

    @property
    def name(self) -> str:
        return str(self.raw["name"])

    @property
    def solver_id(self) -> str:
        return str(self.raw["solver"]["id"])

    @property
    def paper_eligible(self) -> bool:
        return self.raw.get("paper_eligible") is True

    @property
    def qualification_status(self) -> str:
        return str(self.raw.get("qualification", {}).get("status", "missing"))


class ProfileRegistry:
    def __init__(self, experiment_root: Path):
        self.experiment_root = experiment_root.resolve()
        self.root = self.experiment_root / "config/step5d/autotuner_strategies"
        self.pointer_path = self.root / "current-qualified.json"

    @property
    def release_pointer_path(self) -> Path:
        return self.experiment_root / "config/step5d/current.json"

    @property
    def source_capture_path(self) -> Path:
        return self.experiment_root / "config/step5d/r014_source_capture.json"

    @property
    def host_source_pointer_path(self) -> Path:
        return self.root / "r014-host-source.json"

    def _release_binding(self) -> dict[str, str]:
        pointer = load_json(self.release_pointer_path)
        relative = Path(str(pointer.get("manifest_path", "")))
        if relative.is_absolute() or ".." in relative.parts:
            raise R014Error("release manifest path is unsafe")
        manifest = self.experiment_root / relative
        expected = str(pointer.get("manifest_sha256", ""))
        if not manifest.is_file() or sha256_file(manifest) != expected:
            raise R014Error("release manifest pointer/hash mismatch")
        return {"manifest_path": relative.as_posix(), "manifest_sha256": expected}

    def _source_binding(self) -> dict[str, str]:
        capture = load_json(self.source_capture_path)
        capture_identity = str(capture.get("manifest_sha256", ""))
        if len(capture_identity) != 64:
            raise R014Error("R014 source-capture identity is missing")
        pointer = load_json(self.host_source_pointer_path)
        relative = Path(str(pointer.get("manifest_path", "")))
        if relative.is_absolute() or ".." in relative.parts:
            raise R014Error("R014 host-source manifest path is unsafe")
        manifest = self.root / relative
        identity = str(pointer.get("manifest_sha256", ""))
        if not manifest.is_file() or sha256_file(manifest) != identity:
            raise R014Error("R014 host-source manifest pointer/hash mismatch")
        return {
            "source_capture_path": self.source_capture_path.relative_to(self.experiment_root).as_posix(),
            "source_capture_manifest_sha256": capture_identity,
            "host_source_manifest_path": manifest.relative_to(self.experiment_root).as_posix(),
            "host_source_manifest_sha256": identity,
        }

    def formal_document(self) -> dict[str, Any]:
        return {
            "schema": PROFILE_SCHEMA,
            "version": 1,
            "name": FORMAL_PROFILE_NAME,
            "paper_eligible": True,
            "release": self._release_binding(),
            "host_source": self._source_binding(),
            "target": {"force_n": 5.0, "phase_correction": "off"},
            "feedforward": "on",
            "trajectory": {"default": "cycloid", "allowed": ["cycloid", "figure8"]},
            "contact": {"default": "two-stage", "formal": "two-stage"},
            "solver": {
                "id": "finite-time-r08",
                "r": 0.8,
                "epsilon": 0.01,
                "iterations": 512,
                "backend": "cupy",
                "qdot_limit_rad_s": 0.05,
                "preconstruct_outside_tick": True,
            },
            "safety": {
                "geometric_filter": "fixed_path_filter",
                "force_veto": "independent_hard_veto",
                "force_cbf_claim": False,
                "optimizer_configurable": False,
            },
            "optimizer": {
                "catalog": "qafs-bo-130-seed-20260821-v1",
                "catalog_sha256": catalog_document()["catalog_sha256"],
                "rnn_fields_in_bo": False,
            },
            "metric": {
                "primary": "force_bin_mae_0_60_v1",
                "secondary": "force_bin_mae_5_60_v1",
                "target_n": 5.0,
                "bin_width_s": 0.1,
                "metric_fingerprint": METRIC_FINGERPRINT,
            },
            "qualification": {"status": "pending", "receipts": []},
        }

    def legacy_document(self) -> dict[str, Any]:
        document = self.formal_document()
        document.update({"name": LEGACY_PROFILE_NAME, "paper_eligible": False})
        document["solver"] = {
            "id": "legacy-r1",
            "r": 1.0,
            "epsilon": 0.022,
            "iterations": 1,
            "backend": "numpy",
            "qdot_limit_rad_s": 0.15,
            "preconstruct_outside_tick": False,
        }
        document["qualification"] = {
            "status": "historical_only",
            "receipts": [],
            "formal_campaign_allowed": False,
        }
        return document

    def install_defaults(self) -> dict[str, StrategyProfile]:
        installed: dict[str, StrategyProfile] = {}
        for document in (self.formal_document(), self.legacy_document()):
            identity = sha256_value(document)
            path = self.root / "profiles" / identity / "profile.json"
            if path.exists() and load_json(path) != document:
                raise R014Error(f"content-addressed profile collision: {path}")
            if not path.exists():
                atomic_write_json(path, document)
            installed[str(document["name"])] = self.load_profile(path, expected_sha=identity)
        return installed

    def load_profile(self, path: Path, *, expected_sha: str | None = None) -> StrategyProfile:
        path = path.resolve()
        raw = load_json(path)
        if raw.get("schema") != PROFILE_SCHEMA or raw.get("version") != 1:
            raise R014Error("strategy profile schema/version mismatch")
        identity = sha256_value(raw)
        if expected_sha is not None and identity != expected_sha:
            raise R014Error("strategy profile content identity mismatch")
        if path.parent.name != identity:
            raise R014Error("strategy profile path is not content addressed")
        return StrategyProfile(path=path, sha256=identity, raw=raw)

    def by_name(self, name: str) -> StrategyProfile:
        defaults = self.install_defaults()
        if name == "finite-time":
            return defaults[FORMAL_PROFILE_NAME]
        if name == "legacy-r1":
            return defaults[LEGACY_PROFILE_NAME]
        if name != "current":
            raise R014Error(f"unknown strategy selection: {name}")
        return self.current_qualified()

    def current_qualified(self) -> StrategyProfile:
        if not self.pointer_path.is_file():
            raise R014Error(
                "current-qualified pointer is absent; complete R014 small qualification before live use"
            )
        pointer = load_json(self.pointer_path)
        if pointer.get("schema") != POINTER_SCHEMA:
            raise R014Error("current-qualified pointer schema mismatch")
        relative = Path(str(pointer.get("profile_path", "")))
        if relative.is_absolute() or ".." in relative.parts:
            raise R014Error("current-qualified profile path is unsafe")
        profile = self.load_profile(
            self.root / relative,
            expected_sha=str(pointer.get("profile_sha256", "")),
        )
        if profile.qualification_status != "qualified":
            raise R014Error("current-qualified profile is not qualification-complete")
        return profile

    def promote(self, qualified_profile_path: Path) -> StrategyProfile:
        profile = self.load_profile(qualified_profile_path)
        if profile.qualification_status != "qualified" or not profile.paper_eligible:
            raise R014Error("only an exact qualified formal profile may be promoted")
        relative = profile.path.relative_to(self.root)
        atomic_write_json(
            self.pointer_path,
            {
                "schema": POINTER_SCHEMA,
                "version": 1,
                "profile_path": relative.as_posix(),
                "profile_sha256": profile.sha256,
            },
        )
        return profile
