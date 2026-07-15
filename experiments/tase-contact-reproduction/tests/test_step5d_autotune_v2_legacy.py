from __future__ import annotations

import hashlib
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any, Mapping

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import step5d_autotune_v2.legacy as legacy_module
from step5d_autotune_v2.legacy import LegacyImportError, LegacyImporter
from step5d_autotune_v2.repository import Repository


class SimulatedImportCrash(BaseException):
    pass


def test_legacy_import_is_one_way_checksum_bound_and_leaves_pending_tail(tmp_path: Path) -> None:
    source = tmp_path / "legacy"
    trial_uid = "1" * 64
    trial_dir = source / "campaign/store/trials" / trial_uid
    trial_dir.mkdir(parents=True)
    (trial_dir / "trial_spec.json").write_text(
        json.dumps(
            {
                "trial_uid": trial_uid,
                "source_fingerprint": "2" * 64,
                "config_fingerprint": "3" * 64,
                "candidate": {
                    "force_p_gain": 0.001681792830507429,
                    "force_i_gain": 0.0001,
                    "force_damping": 8.324449805019047,
                },
            }
        ),
        encoding="utf-8",
    )
    bundle = {
        "evaluation": {
            "complete_bins": 550,
            "safe_closure": True,
            "eligible": False,
            "metrics": {
                "governor": {
                    "nontrainable_profile_diagnostic": {"force_mae_n": 0.55},
                    "tracking": {"correlation": 0.98, "lag_s": 0.01, "nrmse": 0.04},
                    "orientation": {"p95_error_rad": 0.02, "max_error_rad": 0.03},
                    "burden_by_layer": {
                        "normal_filter_rate": 0.1,
                        "tp_speedj_acceleration": 0.08,
                        "host_qdot_slew": 0.0,
                    },
                }
            },
        }
    }
    (trial_dir / "immutable_trial_bundle.json").write_text(
        json.dumps(bundle), encoding="utf-8"
    )
    multipliers = ("10", "50", "100", "500", "1000")
    candidates = []
    for index, multiplier in enumerate(multipliers, start=1):
        row = {
            "group_id": f"G{index}",
            "p": "0.001681792830507429",
            "i": str(0.00001 * int(multiplier)),
            "d": "8.324449805019047",
            "log2_p": "0.75",
            "log2_d": "0.25",
            "i_multiplier": multiplier,
        }
        if index == 1:
            row.update({"trial_uid": trial_uid, "trial_dir": "campaign/store/trials/" + trial_uid})
        candidates.append(row)
    mapping = tmp_path / "mapping.json"
    mapping.write_text(
        json.dumps(
            {
                "schema": "step5d.autotune.legacy-map/v1",
                "tp_fingerprint": "4" * 64,
                "batches": [
                    {"batch_id": "legacy", "source": "fixture", "candidates": candidates}
                ],
            }
        ),
        encoding="utf-8",
    )
    repository = Repository((tmp_path / "campaign.sqlite3").resolve())
    repository.initialize()
    output = tmp_path / "import.json"
    importer = LegacyImporter(repository, source_root=source, mapping_path=mapping)
    result = importer.run(output_path=output)
    assert result["pending_groups"] == ["G2", "G3", "G4", "G5"]
    assert result["diagnostic_incumbent"]["group_id"] == "G1"
    assert hashlib.sha256(output.read_bytes()).hexdigest() == repository.get_metadata(
        "legacy_import_result_sha256"
    )
    assert importer.run(output_path=output) == result

    output.write_text("{}\n", encoding="utf-8")
    with pytest.raises(LegacyImportError, match="checksum mismatch"):
        importer.run(output_path=output)


def test_legacy_import_resumes_partial_trial_and_existing_batch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "legacy"
    trial_uid = "1" * 64
    trial_dir = source / "trials" / trial_uid
    trial_dir.mkdir(parents=True)
    (trial_dir / "trial_spec.json").write_text(
        json.dumps(
            {
                "trial_uid": trial_uid,
                "source_fingerprint": "2" * 64,
                "config_fingerprint": "3" * 64,
                "candidate": {
                    "force_p_gain": "0.001681792830507429",
                    "force_i_gain": "0.0001",
                    "force_damping": "8.324449805019047",
                },
            }
        ),
        encoding="utf-8",
    )
    (trial_dir / "immutable_trial_bundle.json").write_text(
        json.dumps(
            {
                "evaluation": {
                    "complete_bins": 550,
                    "safe_closure": True,
                    "eligible": False,
                    "metrics": {
                        "governor": {
                            "nontrainable_profile_diagnostic": {"force_mae_n": 0.55}
                        }
                    },
                }
            }
        ),
        encoding="utf-8",
    )
    candidates = []
    for index, multiplier in enumerate(("10", "50", "100", "500", "1000"), start=1):
        row = {
            "group_id": f"G{index}",
            "log2_p": "0.75",
            "log2_d": "0.25",
            "i_multiplier": multiplier,
        }
        if index == 1:
            row.update({"trial_uid": trial_uid, "trial_dir": f"trials/{trial_uid}"})
        candidates.append(row)
    mapping = tmp_path / "mapping.json"
    mapping.write_text(
        json.dumps(
            {
                "schema": "step5d.autotune.legacy-map/v1",
                "tp_fingerprint": "4" * 64,
                "batches": [
                    {"batch_id": "legacy", "source": "fixture", "candidates": candidates}
                ],
            }
        ),
        encoding="utf-8",
    )
    repository = Repository((tmp_path / "campaign.sqlite3").resolve())
    repository.initialize()
    importer = LegacyImporter(repository, source_root=source, mapping_path=mapping)
    output = tmp_path / "import.json"
    original = legacy_module._ImportedRuntimePort.wait_home_verified

    def crash_after_home(self: object, *, trial_id: str) -> Mapping[str, Any]:
        original(self, trial_id=trial_id)
        raise SimulatedImportCrash("after imported Home observation")

    monkeypatch.setattr(
        legacy_module._ImportedRuntimePort, "wait_home_verified", crash_after_home
    )
    with pytest.raises(SimulatedImportCrash):
        importer.run(output_path=output)
    monkeypatch.setattr(
        legacy_module._ImportedRuntimePort, "wait_home_verified", original
    )

    result = importer.run(output_path=output)
    assert result["pending_groups"] == ["G2", "G3", "G4", "G5"]
    with sqlite3.connect(repository.path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM batches").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM trials").fetchone()[0] == 1
    assert repository.trial_detail(trial_uid)["state"] == "complete"
