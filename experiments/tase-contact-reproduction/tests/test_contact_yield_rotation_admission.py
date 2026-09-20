"""Preserve rotation admission, including existing off-diagonal tolerances."""
import numpy as np
import pytest
from contact_yield_math import require_rotation, so3_exp


def test_rotation_admission_matches_existing_contract_at_boundaries():
    rng = np.random.default_rng(273)
    matrices = [np.eye(3), np.diag([-1., 1., 1.]), np.zeros((3, 3))]
    for factor in (0.999, 1., 1.001, 2.):
        matrices.append(np.diag([np.sqrt(1. + factor * (1e-8 + 1e-5)), 1., 1.]))
        matrix = np.eye(3); matrix[0, 1] = factor * 1e-8
        matrices.append(matrix)
    for scale in (0., 1e-10, 1e-8, 1e-5):
        for _ in range(100):
            matrices.append(so3_exp(rng.normal(size=3)) + rng.normal(size=(3, 3)) * scale)
    for matrix in matrices:
        expected = np.allclose(matrix.T @ matrix, np.eye(3), atol=1e-8) and np.linalg.det(matrix) >= .999
        if expected:
            np.testing.assert_array_equal(require_rotation(matrix), matrix)
        else:
            with pytest.raises(ValueError):
                require_rotation(matrix)


@pytest.mark.parametrize('matrix', [np.full((3, 3), np.nan), np.full((3, 3), np.inf), np.eye(2)])
def test_nonfinite_and_wrong_shape_still_rejected(matrix):
    with pytest.raises(ValueError):
        require_rotation(matrix)
