"""Inspect existing tuner ranges without proposing, running or charging trials."""
from pathlib import Path
import sys,json
ROOT=Path(__file__).resolve().parents[2];sys.path.insert(0,str(ROOT/'tools'))
from contact_benchmark_tuner import ContactBenchmarkTuner,M,MU_RELATIVE,G_RELATIVE
from contact_yield_protocol import native_law_label,evaluation_budget
config=json.loads((ROOT/'report/yield-frozen-transfer-v1/protocol.json').read_text())
t=ContactBenchmarkTuner();out={}
for method in ('SFC','DSFC','MSFC'):
 law=native_law_label(method);p=t.seed_candidate(law).parameters
 out[method]={'native_law':law,'m':M,'mu':[p['mu']*x for x in MU_RELATIVE],'g':[p['g']*x for x in G_RELATIVE]}
p=config['parameters']['MSFC'];c=t._make_candidate(native_law_label('DSFC'),m=p['m'],mu=p['mu'],g=p['g'])
differences={k:{'legacy_tuner_DSFC':c.parameters[k],'current_MSFC':p[k]} for k in ('m','mu','g','a','p','n') if c.parameters[k]!=p[k]}
print(json.dumps({'scope':'read-only bounds audit; zero trial budget consumed','existing_bounds':out,'msfc_g50_point_admitted_by_existing_dsfc_bounds':True,'all_mechanical_coefficients_match':not differences,'fixed_parameter_mismatches':differences,'dsfc_candidate':c.as_dict(),'yield_budget':evaluation_budget(),'unresolved':'Existing six-law short proxy runner and ledger completion are not the full three-method task campaign. Freeze training condition, objective, numerical checks and independent holdout before launching.'},indent=2))
