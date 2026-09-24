import json
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import analyze_tase_baseline_contact as analysis  # noqa: E402


def _packet(*, normal, force_norm, torque_norm, qdot, setpoint, filtered):
    doubles = [0.0] * 24
    doubles[0] = normal
    doubles[1] = force_norm
    doubles[6] = torque_norm
    doubles[13:19] = qdot
    doubles[19] = 1.0
    doubles[20] = setpoint
    doubles[21] = filtered
    return {
        "sequence": 1,
        "command_mode": 1,
        "reason": "baseline",
        "double_values": doubles,
    }


def test_baseline_timeline_labels_force_fraction_as_diagnostic_not_official_gate(tmp_path):
    run_dir = tmp_path / "attempt"
    run_dir.mkdir()
    (run_dir / "attempt-result.json").write_text(json.dumps({
        "evidence": {"failure": "release dwell timeout"},
        "timing": {"tp_stage_observations": {"21": {"samples": 3}}},
        "state": {"last_result": {"phase": "baseline", "measured_normal_n": 5.0}},
    }), encoding="utf-8")
    rows = [
        [1.0, _packet(normal=1.0, force_norm=1.0, torque_norm=0.0,
                      qdot=[0.0] * 6, setpoint=1.0, filtered=1.0)],
        [1.1, _packet(normal=5.0, force_norm=5.0, torque_norm=0.0,
                      qdot=[.001] * 6, setpoint=5.0, filtered=5.0)],
        [1.3, _packet(normal=4.8, force_norm=4.8, torque_norm=0.0,
                      qdot=[.001] * 6, setpoint=5.0, filtered=4.9)],
    ]
    (run_dir / "published_packets.jsonl").write_text(
        "".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8"
    )

    result = analysis._summarize_run("fixture", run_dir)

    assert result["target_windows"]["0.5s"]["force_only_gate_pass_fraction"] == 1.0
    assert "not the official continuous release gate" in result["metric_scope"]["force_only_gate_pass_fraction"]
