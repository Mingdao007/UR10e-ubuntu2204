"""Bounded offline mechanism screen and full-cycle checks, not formal BO/holdout."""
import argparse,json,hashlib,time
from pathlib import Path
from contact_yield_runner import run_closed_loop
from contact_yield_protocol import law_seed_parameters,PERIOD_S
from contact_yield_metrics import compare_pair
from run_contact_yield import write,read


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--output',type=Path,required=True);a=p.parse_args()
    root=a.output;root.mkdir(parents=True,exist_ok=False)
    started=time.time();screen=[];selected={};summary=[]
    # Frozen, equal four-point gain screen. Proposal p/a change is an explicitly
    # separate low-force design variant, motivated before this screen is run.
    for method in ('SFC','DSFC','MSFC'):
        base=law_seed_parameters(method)
        if method!='SFC':base.update(p=.5,a=.05)
        candidates=[]
        for factor in (.03,.1,.3,1.):
            params={**base,'g':base['g']*factor}
            r=run_closed_loop(method=method,duration_s=6.,timeline='diagnostic',law_parameters=params,
                              campaign_kind='training',record_fullstate=True)
            name=f'screen-{method}-{factor}.json.gz';write(root/name,r)
            m=r['metrics'];score=None
            if not m['failed'] and m['n_samples']:
                score=m['force_mae_n']/.5+m['path_rmse_m']/.002+abs(1-m['progress_ratio'])
            item={'method':method,'gain_factor':factor,'parameters':params,'file':name,'metrics':m,'score':score}
            screen.append(item)
            if score is not None:candidates.append(item)
            print('screen',method,factor,score,flush=True)
        if not candidates:raise RuntimeError('no executable candidate for '+method)
        best=min(candidates,key=lambda x:x['score']);selected[method]=best['parameters']
    write(root/'screen.json',{'screen':screen,'selected':selected,'formal_budget_used':False,
        'selection':'lowest forceMAE/0.5 + pathRMSE/0.002 + abs(1-progressRatio); same four gain factors',
        'proposal_variant':'p=0.5,a=0.05 fixed before screen; original p=0.1,a=1.2 seeds retained separately',
        'scope':'diagnostic selection, no Bayesian optimization and no independent holdout claim'})
    for method in ('SFC','SFC_RADIAL','DSFC','MSFC'):
        params=selected['SFC' if method=='SFC_RADIAL' else method]
        for material in ('stiff_low_mu','compliant_high_mu'):
            nominal=None
            for scenario in ('nominal','sustained_release_normal','sustained_release_tangent','short_pulse_oblique'):
                r=run_closed_loop(method=method,scenario=scenario,material=material,duration_s=PERIOD_S,
                    timeline='full_cycle',law_parameters=params,record_fullstate=True)
                name=f'full-{method}-{material}-{scenario}.json.gz';write(root/name,r)
                item={'method':method,'material':material,'scenario':scenario,'file':name,'metrics':r['metrics'],
                      'full_cycle':r['full_cycle'],'source_hashes':r['source_hashes']}
                if scenario=='nominal':nominal={k:v for k,v in r.items() if k!='records'}
                else:item['pair']=compare_pair(nominal,r)
                summary.append(item)
                print('full',method,material,scenario,r['metrics']['force_mae_n'],r['metrics']['path_rmse_m'],flush=True)
                del r
    write(root/'summary.json',{'runs':summary,'source_screen':'screen.json','formal_campaign_complete':False,
          'live_executed':False,'elapsed_wall_s':time.time()-started})
    manifest={p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in root.iterdir() if p.is_file()}
    write(root/'manifest.json',{'sha256':manifest,'claim_scope':'offline mechanism study, selected on short nominal screen; not formal held-out evidence'})

if __name__=='__main__':main()
