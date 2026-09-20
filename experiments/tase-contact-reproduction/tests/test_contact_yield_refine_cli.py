"""CLI refinement must vary discretization, not silently replace the experiment."""
import json,subprocess,sys
from pathlib import Path
from contact_yield_runner import run_closed_loop
from run_contact_yield import write,read
from test_contact_yield import library
ROOT=Path(__file__).resolve().parents[1]

def test_refine_cli_preserves_nondefault_observer_surface_and_qp(tmp_path,library):
    params=json.loads((ROOT/'config/yield_normal_observer_v3.json').read_text())
    params['initial_inward_normal_base']=[0.,0.17364817766693033,-0.984807753012208]
    source=run_closed_loop(method='DSFC',duration_s=.08,qp_library=library,
        estimator_parameters=params,surface_parameters={'kappa_xx':6.,'kappa_yy':8.},plant_substeps=8)
    assert not source['metrics']['failed']
    initial=tmp_path/'original.json.gz';output=tmp_path/'refined.json.gz';write(initial,source)
    call=subprocess.run([sys.executable,str(ROOT/'tools/run_contact_yield.py'),'refine','--input',str(initial),'--output',str(output)],capture_output=True,text=True)
    assert call.returncode==0,call.stdout+call.stderr
    fine=read(output)['fine']
    assert not fine['metrics']['failed']
    assert fine['dt_s']==.001
    assert fine['identity_payload']['estimator_parameters']==source['identity_payload']['estimator_parameters']
    assert fine['surface_parameters']==source['surface_parameters']
    assert fine['plant_identity_payload']['surface']==source['plant_identity_payload']['surface']
    assert fine['identity_payload']['qp_library_sha256']==source['identity_payload']['qp_library_sha256']
    assert fine['initial_controller_snapshot']['normal_estimate']==source['initial_controller_snapshot']['normal_estimate']
