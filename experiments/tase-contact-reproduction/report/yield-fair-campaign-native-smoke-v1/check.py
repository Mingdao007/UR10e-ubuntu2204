"""Full-period native engineering pair; diagnostic, no ledger or tuning budget."""
import dataclasses,gzip,hashlib,json,time
from pathlib import Path
from contact_yield_runner import run_closed_loop
from yield_fair_selection import load_contract,bind_candidate,inspect_member,evaluate_pair,OBSERVER_V3
from yield_contact_tuner import YieldContactTuner
root=Path(__file__).resolve().parents[2]
output=root/'runs/yield-fair-campaign-native-smoke-v1';output.mkdir(parents=True,exist_ok=True)
report=Path(__file__).resolve().parent
contract=load_contract()
tuner=YieldContactTuner(root/'config/yield_fair_tuning_v1.json',training_cell_id='stiff_low_mu',selection_contract_id='yield-fair-selection-contract-v1')
candidate=tuner.propose('SFC',[],0).candidate
contract=dataclasses.replace(bind_candidate(contract,method='SFC',parameters=candidate.parameters),campaign_kind='diagnostic_seed')
artifacts=[];receipt={'formal_budget_used':0,'engineering_only':True,'files':{},'members':{}}
for condition,scenario in [('nominal',contract.nominal_scenario),('disturbed',contract.disturbed_scenario)]:
 start=time.monotonic()
 artifact=run_closed_loop(method='SFC',scenario=scenario,material=contract.material,duration_s=contract.duration_s,dt_s=contract.dt_s,timeline=contract.timeline,campaign_kind='diagnostic_seed',preparation=contract.preparation,record_fullstate=True,require_ur10e=True,law_parameters=candidate.parameters,plant_substeps=contract.plant_substeps,estimator_parameters=dict(OBSERVER_V3),surface_parameters={'kappa_xx':contract.kappa_xx,'kappa_yy':contract.kappa_yy},build_root=root/'build/contact-six-laws',qp_library=root/'build/contact-qp/libcontact_qp.so')
 path=output/(condition+'.json.gz')
 with gzip.open(path,'xt') as stream:json.dump(artifact,stream,allow_nan=False,separators=(',',':'))
 receipt['files'][condition]={'path':str(path),'sha256':hashlib.sha256(path.read_bytes()).hexdigest(),'elapsed_s':time.monotonic()-start}
 try:receipt['members'][condition]=dataclasses.asdict(inspect_member(artifact,contract,condition=condition))
 except Exception as error:receipt['members'][condition]={'error':f'{type(error).__name__}: {error}'}
 (report/'result.json').write_text(json.dumps(receipt,indent=2,allow_nan=False)+'\n')
 print(condition,receipt['members'][condition],flush=True)
 artifacts.append(artifact)
try:receipt['pair']=dataclasses.asdict(evaluate_pair(*artifacts,contract))
except Exception as error:receipt['pair']={'error':f'{type(error).__name__}: {error}'}
(report/'result.json').write_text(json.dumps(receipt,indent=2,allow_nan=False)+'\n')
print('pair',receipt['pair'],flush=True)
