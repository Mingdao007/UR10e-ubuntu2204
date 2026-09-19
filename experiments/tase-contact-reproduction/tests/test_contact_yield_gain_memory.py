"""GM-v1 ablation must remove mechanical memory coupling, not erase memory."""
import json
from pathlib import Path
import numpy as np

from contact_yield_laws import YieldLaw
from contact_yield_protocol import law_seed_parameters


def test_identity_metric_matches_parameter_matched_dsfc_without_memory_reset():
    root = Path(__file__).resolve().parents[1]
    memory = json.loads((root/'config/contact_yield_candidates/msfc.json').read_text())
    memory['minimum_metric_eigenvalue'] = 1.
    radial = law_seed_parameters('DSFC')
    for key in radial:
        if key in memory:
            radial[key] = memory[key]
    with YieldLaw('MSFC', memory) as m, YieldLaw('DSFC', radial) as d:
        for k in range(500):
            force = (1.5, -.7, .3) if k < 300 else (0., 0., 0.)
            np.testing.assert_allclose(m.step(force, .002), d.step(force, .002),
                                       rtol=0, atol=2e-10)
        token = m.snapshot().values
        # Full memory state remains nonzero after release, while A is identity.
        assert any(abs(value) > 1e-8 for value in token[7:19])
