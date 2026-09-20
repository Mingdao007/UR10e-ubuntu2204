import numpy as np
import pytest
from contact_yield_task_frame import figure8_home_pose, require_figure8_home


def test_old_cycloid_home_cannot_anchor_figure8():
    with pytest.raises(ValueError, match="Home XY differs"):
        require_figure8_home((.487834547, .129337053, .033, 2.033, 2.395, 0.))


def test_planar_frame_does_not_rotate_with_tool_and_preserves_tool_calibration():
    original = (.487834547, .129337053, .033, 2.033, 2.395, 0.)
    home = figure8_home_pose(original)
    assert home[2:] == original[2:]
    basis = require_figure8_home(home)
    np.testing.assert_allclose(basis.T @ basis, np.eye(3), atol=1e-12)
    assert np.linalg.det(basis) == pytest.approx(1.)
    np.testing.assert_allclose(basis[:, 0], [.0049336434, .9999878295, 0.], atol=1e-10)
    np.testing.assert_allclose(require_figure8_home((*home[:3], 0., 0., 0.)), basis)
    # Full-period path remains centred on the original fitted origin.
    t = np.linspace(0., 2*np.pi, 10001)
    local = np.array([.04*np.sin(t), .01*np.sin(2*t), np.zeros_like(t)])
    path = np.asarray(home[:3])[:, None] + basis @ local
    assert path[0].max() < .4888784335298146  # existing Step6 X boundary
    np.testing.assert_allclose(path[:, 0], home[:3], atol=1e-12)
    np.testing.assert_allclose(path[:, -1], home[:3], atol=1e-12)
