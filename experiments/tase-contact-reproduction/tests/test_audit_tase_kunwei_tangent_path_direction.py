from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from audit_tase_kunwei_tangent_path_direction import (  # noqa: E402
    AuditError,
    tangent_alignment_cosine,
)


def test_tangent_alignment_uses_base_rotation_and_normal_projection():
    cosine, force_norm, reference_norm = tangent_alignment_cosine(
        force_tcp_n=(1.0, 0.0, 2.0),
        tcp_rotvec_rad=(0.0, 0.0, 0.0),
        normal_base=(0.0, 0.0, 1.0),
        reference_velocity_base_m_s=(-0.003, 0.0, 0.5),
    )
    assert cosine == pytest.approx(-1.0)
    assert force_norm == pytest.approx(1.0)
    assert reference_norm == pytest.approx(0.003)


def test_tangent_alignment_rejects_zero_tangent_reference():
    with pytest.raises(AuditError, match="zero magnitude"):
        tangent_alignment_cosine(
            force_tcp_n=(1.0, 0.0, 0.0),
            tcp_rotvec_rad=(0.0, 0.0, 0.0),
            normal_base=(0.0, 0.0, 1.0),
            reference_velocity_base_m_s=(0.0, 0.0, 1.0),
        )
