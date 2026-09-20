"""Reuse the existing Figure-eight planar geometry independently of tool attitude."""
import numpy as np
from contact_yield_math import require_rotation
from step6_figure8_autotune_v1.live_composition import (
    FIGURE8_ALONG_BASE, FIGURE8_LATERAL_BASE, FIGURE8_CALIBRATION_HOME_POSE,
)


FIGURE8_CONTACT_HOME_XYZ_M = (*FIGURE8_CALIBRATION_HOME_POSE[:2], 0.033)

def figure8_task_basis():
    along = np.asarray(FIGURE8_ALONG_BASE, dtype=float)
    along /= np.linalg.norm(along)
    lateral = np.asarray(FIGURE8_LATERAL_BASE, dtype=float)
    lateral -= np.dot(lateral, along) * along
    lateral /= np.linalg.norm(lateral)
    return require_rotation(np.column_stack((along, lateral, np.cross(along, lateral))),
                            "existing Figure-eight planar basis")


def figure8_home_pose(tool_home_pose):
    pose = np.asarray(tool_home_pose, dtype=float).copy()
    if pose.shape != (6,) or not np.isfinite(pose).all():
        raise ValueError("finite six-dimensional Home required")
    # Reuse only the reviewed planar origin. Current tool attitude and clearance
    # remain explicit inputs; old tool calibration is not silently reinstated.
    pose[:2] = FIGURE8_CALIBRATION_HOME_POSE[:2]
    return tuple(float(v) for v in pose)


def require_figure8_home(tool_home_pose):
    pose = np.asarray(tool_home_pose, dtype=float)
    expected = np.asarray(figure8_home_pose(pose))
    if not np.allclose(pose[:2], expected[:2], rtol=0., atol=1e-9):
        raise ValueError("native Figure-eight Home XY differs from the existing Figure-eight origin")
    return figure8_task_basis()
