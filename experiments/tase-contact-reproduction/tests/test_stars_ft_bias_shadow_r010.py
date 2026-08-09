"""R010 bindings for the analysis-only STARS FT-bias sidecar."""

from __future__ import annotations

import hashlib
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from stars_ft_bias_shadow.adapters.r008_run_dir import canonical_json_bytes  # noqa: E402
from stars_ft_bias_shadow.adapters.r010_release import validate_r010_binding  # noqa: E402


def test_stars_binding_is_analysis_only_and_hash_bound(tmp_path: Path) -> None:
    payload = {
        "schema": "stars_ft_bias_shadow/r010-analysis-binding-v1",
        "science_not_promoted": True,
        "campaign_identity_member": False,
        "gp_observation": False,
        "force_correction": False,
        "completion_certificate": False,
        "zero_tare_or_config_write": False,
    }
    payload["binding_sha256"] = hashlib.sha256(canonical_json_bytes(payload)).hexdigest()
    path = tmp_path / "binding.json"
    path.write_bytes(canonical_json_bytes(payload) + b"\n")
    assert validate_r010_binding(path)["science_not_promoted"] is True
