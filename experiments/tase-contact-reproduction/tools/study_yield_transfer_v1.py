"""TR-v1: frozen development transfer matrix, with exact retained-cell reuse.

Primary methods remain SFC/DSFC/MSFC; identity-metric MSFC is an ablation.
No retuning, final holdout, formal budget or physical timing claim.
"""
import argparse
from concurrent.futures import ProcessPoolExecutor
from dataclasses import asdict
from pathlib import Path

from contact_yield_controller import YieldSettings
from contact_yield_runner import run_closed_loop, make_system
from contact_yield_protocol import PERIOD_S
from contact_yield_metrics import compare_pair
from study_yield_gain_memory import digest, tail_diagnostic
from run_contact_yield import read, write

ROOT=Path(__file__).resolve().parents[1]
SCENARIOS=('nominal','sustained_release_normal','sustained_release_tangent','short_pulse_oblique')


def retained(label,material,scenario):
    if material!='stiff_low_mu':return None
    if label in ('SFC','DSFC') and scenario in ('nominal','sustained_release_tangent'):
        return ROOT/'runs/yield-offset-ablation'/f'{label}-{scenario}.json.gz'
    if label.startswith('MSFC-GM') and scenario in ('nominal','sustained_release_normal'):
        return ROOT/'runs/yield-gain-memory-v1'/f'{label}-{scenario}-plant8.json.gz'
    return None


def job(spec):
    out,label,method,params,material,scenario,reuse=spec
    if reuse:
        r=read(reuse)
        expected={'method':method,'material':material,'scenario':scenario,
                  'dt_s':.002,'duration_s':PERIOD_S,'timeline':'full_cycle','preparation':'cold'}
        if any(r[k]!=v for k,v in expected.items()) or not r['full_cycle'] or not r['records']:
            raise ValueError('retained cell grid/protocol differs')
        c,p,_=make_system(method=method,material=material,dt_s=.002,timeline='full_cycle',
                         law_parameters=params,plant_substeps=8)
        try:
            if (r['identity']!=c.identity or r['initial_simulator_snapshot']['identity']!=p.identity
                or r['identity_payload']['settings']!=asdict(YieldSettings())):
                raise ValueError('retained controller/plant identity differs')
        finally:c.close()
        path=reuse
    else:
        r=run_closed_loop(method=method,material=material,scenario=scenario,
            duration_s=PERIOD_S,timeline='full_cycle',law_parameters=params,
            plant_substeps=8,record_fullstate=True)
        path=out/f'{label}-{material}-{scenario}.json.gz'
        pending=path.with_name(path.name.replace('.json.gz','.pending.json.gz'))
        write(pending,r);path.hardlink_to(pending);pending.unlink()
    return label,material,scenario,str(path.resolve()),{k:v for k,v in r.items() if k!='records'}


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--output',type=Path,required=True)
    a=p.parse_args();a.output.mkdir(parents=True,exist_ok=False)
    variants={m:{'method':m,'parameters':read(ROOT/'config/contact_yield_candidates'/f'{m.lower()}.json'),
                 'role':'baseline' if m=='SFC' else 'proposal'} for m in ('SFC','DSFC')}
    gm=read(ROOT/'runs/yield-gain-memory-v1/protocol.json')['variants']
    for label in ('MSFC-GM-v1-g50-on','MSFC-GM-v1-g50-identity_metric'):
        variants[label]={'method':'MSFC','parameters':gm[label],
                         'role':'proposal_candidate' if label.endswith('-on') else 'matched_memory_ablation'}
    specs=[(a.output,label,v['method'],v['parameters'],material,scenario,retained(label,material,scenario))
        for label,v in variants.items() for material in ('stiff_low_mu','compliant_high_mu') for scenario in SCENARIOS]
    write(a.output/'protocol.json',{'version':'TR-v1','dataset_role':'development_only',
        'variants':variants,'settings':asdict(YieldSettings()),'controller_dt_s':.002,'plant_substeps':8,
        'scenarios':SCENARIOS,'materials':['stiff_low_mu','compliant_high_mu'],
        'frozen_before_new_runs':True,'formal_tuning_budget_used':False,'holdout':False,
        'retained_count':sum(s[-1] is not None for s in specs),'new_count':sum(s[-1] is None for s in specs),
        'harness_sha256':digest(Path(__file__)),'source_hashes':{q.name:digest(q) for q in Path(__file__).parent.glob('contact_yield_*.py')},
        'retained_inputs':{str(s[-1].resolve()):digest(s[-1]) for s in specs if s[-1]}})
    summary=[];nominal={}
    with ProcessPoolExecutor(max_workers=4) as pool:
        for label,material,scenario,path,r in pool.map(job,specs):
            item={'variant':label,'material':material,'scenario':scenario,'path':path,'sha256':digest(Path(path)),
                  'metrics':r['metrics'],'full_cycle':r['full_cycle'],'tail':tail_diagnostic(r['rows']) if r['full_cycle'] else None}
            key=(label,material)
            if scenario=='nominal':nominal[key]=r
            else:item['pair']=compare_pair(nominal[key],r)
            summary.append(item)
            print(label,material,scenario,'failed='+str(r['metrics']['failed']),flush=True)
    write(a.output/'summary.json',{'runs':summary,'dataset_role':'development_only',
        'no_retuning':True,'formal_campaign_complete':False,'live_executed':False})
    write(a.output/'manifest.json',{'sha256':{p.name:digest(p) for p in a.output.iterdir() if p.is_file()}})


if __name__=='__main__':main()
