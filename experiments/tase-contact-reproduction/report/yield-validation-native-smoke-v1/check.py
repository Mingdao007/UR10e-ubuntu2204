"""Native diagnostic schema/interface checks on old development surface, no validation cells."""
import dataclasses,json,math
from pathlib import Path
from types import SimpleNamespace
import numpy as np
from contact_yield_runner import run_closed_loop
from yield_validation_runner import calibrated_prior_basis
from yield_validation_selection import prior_inward_normal
from yield_fair_selection import _require_fullstate,OBSERVER_V3
from yield_contact_tuner import YieldContactTuner
root=Path(__file__).resolve().parents[2]
basis=calibrated_prior_basis(root)
tuner=YieldContactTuner(root/'config/yield_fair_tuning_v1.json',training_cell_id='stiff_low_mu',selection_contract_id='yield-fair-selection-contract-v1')
results=[]
for arm in ('SFC_RADIAL','MSFC_IDENTITY'):
 method='SFC_RADIAL' if arm=='SFC_RADIAL' else 'MSFC'
 params=dict(tuner.propose('SFC' if arm=='SFC_RADIAL' else 'MSFC',[],0).candidate.parameters)
 if arm=='MSFC_IDENTITY':params['minimum_metric_eigenvalue']=1.0
 for preparation in ('cold','warm'):
  approach=np.asarray(basis['approach']);along=np.asarray(basis['along']);angle=math.radians(7)
  prior=np.cos(angle)*approach+np.sin(angle)*along
  a=run_closed_loop(method=method,scenario='nominal',material='stiff_low_mu',duration_s=.02,dt_s=.002,timeline='diagnostic',campaign_kind='diagnostic_seed',preparation=preparation,record_fullstate=True,require_ur10e=True,law_parameters=params,plant_substeps=8,estimator_parameters={**OBSERVER_V3,'initial_inward_normal_base':prior.tolist()},surface_parameters={'kappa_xx':.8,'kappa_yy':.4},build_root=root/'build/contact-six-laws',qp_library=root/'build/contact-qp/libcontact_qp.so')
  assert a['metrics']['failed'] is False,a['metrics']
  _require_fullstate(a,SimpleNamespace(preparation=preparation,duration_s=.02,dt_s=.002,record_fullstate=True,material='stiff_low_mu'))
  assert a['method']==method
  assert np.allclose(a['initial_controller_snapshot']['normal_estimate']['inward_normal_base'],prior,atol=1e-12,rtol=0)
  results.append({'arm':arm,'actual_method':method,'preparation':preparation,'records':len(a['records']),'rows':len(a['rows']),'initial_prior_verified':True,'full_record_schema_verified':True,'identity':a['identity'],'law_parameters':a['identity_payload']['parameters'],'campaign_kind':a['campaign_kind']})
  print(results[-1],flush=True)
Path(__file__).with_name('result.json').write_text(json.dumps({'diagnostic_only':True,'validation_trials':0,'formal_training_units':0,'cases':results},indent=2)+'\n')
