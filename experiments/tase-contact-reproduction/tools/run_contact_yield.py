#!/usr/bin/env python3
"""Offline mechanism run/replay/refinement. No robot endpoints or command writer."""
import argparse,gzip,json
from pathlib import Path
from contact_yield_protocol import METHODS,SCENARIOS,MATERIALS,PERIOD_S,protocol
from contact_yield_runner import run_closed_loop
from contact_yield_replay import replay_artifact,refinement_error

def read(path):
    op=gzip.open if str(path).endswith('.gz') else open
    with op(path,'rt') as f:return json.load(f)

def write(path,payload):
    path.parent.mkdir(parents=True,exist_ok=True)
    op=gzip.open if str(path).endswith('.gz') else open
    with op(path,'xt') as f:json.dump(payload,f,allow_nan=False,separators=(',',':'))

def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('mode',choices=['run','replay','refine','protocol'])
    p.add_argument('--output',type=Path,required=True)
    p.add_argument('--input',type=Path)
    p.add_argument('--method',choices=METHODS,default='SFC')
    p.add_argument('--scenario',choices=SCENARIOS,default='nominal')
    p.add_argument('--material',choices=MATERIALS,default='stiff_low_mu')
    p.add_argument('--timeline',choices=['diagnostic','full_cycle'],default='diagnostic')
    p.add_argument('--duration-s',type=float)
    p.add_argument('--dt-s',type=float,default=.002)
    p.add_argument('--preparation',choices=['cold','warm'],default='cold')
    p.add_argument('--parameters',type=Path,help='Complete law parameter object, recorded with identity')
    a=p.parse_args()
    if a.output.exists():p.error('output already exists; retained experiments are immutable')
    if a.mode=='protocol':result=protocol()
    elif a.mode=='run':
        result=run_closed_loop(method=a.method,scenario=a.scenario,material=a.material,
            duration_s=a.duration_s or (PERIOD_S if a.timeline=='full_cycle' else .5),dt_s=a.dt_s,
            timeline=a.timeline,preparation=a.preparation,law_parameters=read(a.parameters) if a.parameters else None)
    else:
        if not a.input:p.error('input artifact required')
        original=read(a.input)
        if a.mode=='replay':result=replay_artifact(original)
        else:
            from contact_yield_controller import YieldSettings
            fine=run_closed_loop(method=original['method'],scenario=original['scenario'],material=original['material'],
                duration_s=original['duration_s'],dt_s=original['dt_s']/2,timeline=original['timeline'],
                preparation=original['preparation'],law_parameters=original['identity_payload']['parameters'],
                settings=YieldSettings(**original['identity_payload']['settings']),record_fullstate=False)
            result={'refinement':refinement_error(original['rows'],fine['rows'],coarse_dt_s=original['dt_s'],fine_dt_s=fine['dt_s']),
                    'fine':fine}
    write(a.output,result)
    print(json.dumps(result.get('metrics',result.get('refinement',{'output':str(a.output),'passed':result.get('passed')})),indent=2))
    return 1 if result.get('metrics',{}).get('failed') or result.get('passed') is False else 0

if __name__=='__main__':raise SystemExit(main())
