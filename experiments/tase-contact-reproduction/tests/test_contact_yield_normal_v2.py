"""Observable and unobservable normal directions, independent planar fixture."""
import json
from pathlib import Path
import numpy as np
import pytest
from contact_yield_normal import NormalEstimator
from contact_yield_runner import run_closed_loop
from contact_yield_replay import replay_artifact
from test_contact_yield import library

PARAMS=json.loads((Path(__file__).resolve().parents[1]/'config/yield_normal_observer_v2.json').read_text())


def fixture(two_directions=True, speed=.008):
    prior=np.array([.15,.12,-1.]);prior/=np.linalg.norm(prior)
    estimator=NormalEstimator(prior,**PARAMS)
    for i in range(10000):
        velocity=np.array([speed,0.,0.]) if not two_directions or (i//250)%2==0 else np.array([0.,speed,0.])
        # Friction is deliberately present. Its direction is never a normal measurement.
        force=np.array([0.,0.,5.])-.4*5.*velocity/np.linalg.norm(velocity)
        estimator.update(dt_s=.002,measured_linear_velocity_base_m_s=velocity,
            measured_force_base_n=force,in_contact=True)
    return estimator


def test_two_tangent_directions_identify_plane_despite_friction():
    estimator=fixture()
    assert np.linalg.norm(estimator.normal[:2])<1e-4
    # Identifiability is directional: one tangent alone cannot recover y tilt.
    one=fixture(False)
    assert abs(one.normal[1])>.1
    assert abs(one.normal[0])<1e-6


def test_normalization_is_speed_invariant_above_gate():
    np.testing.assert_allclose(fixture(speed=.004).normal,fixture(speed=.012).normal,atol=1e-12)


def test_loss_of_contact_preserves_state_and_rate_is_bounded():
    estimator=fixture(False);before=estimator.snapshot()
    estimator.update(dt_s=.002,measured_linear_velocity_base_m_s=[0,.01,0],
        measured_force_base_n=[0,0,5],in_contact=False)
    assert before==estimator.snapshot()
    n=estimator.normal.copy()
    estimator.update(dt_s=.002,measured_linear_velocity_base_m_s=[0,.01,0],
        measured_force_base_n=[0,0,5],in_contact=True)
    assert np.arccos(np.clip(n@estimator.normal,-1,1))<=.002*PARAMS['motion_rate_cap_rad_s']+1e-10


def test_v2_complete_controller_snapshot_replays(library):
    artifact=run_closed_loop(method='DSFC',duration_s=.08,qp_library=library,
        estimator_parameters=PARAMS)
    assert not artifact['metrics']['failed']
    assert artifact['identity_payload']['estimator_parameters']['force_correction_gain']==0.
    assert replay_artifact(artifact)['passed']
