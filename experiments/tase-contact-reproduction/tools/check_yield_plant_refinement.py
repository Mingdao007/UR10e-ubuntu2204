"""Plant integration refinement at FIXED 2ms controller/command sample period."""
import argparse,hashlib,json
from pathlib import Path
import numpy as np
import contact_yield_runner as runner
from contact_yield_simulator import YieldSimulator
from run_contact_yield import write,read


def refined_plant(substeps):
    class RefinedPlant(YieldSimulator):
        def __init__(self,**kwargs):
            super().__init__(**kwargs)
            self.identity_payload={**self.identity_payload,'integration_substeps':substeps}
            self.identity=hashlib.sha256(json.dumps(self.identity_payload,sort_keys=True).encode()).hexdigest()
        def step(self,*,dt_s,**kwargs):
            original_clock=kwargs.get('path_time_s',0.)
            for k in range(substeps):
                result=super().step(dt_s=dt_s/substeps,**{**kwargs,'path_time_s':original_clock+k*dt_s/substeps})
            return result
    return RefinedPlant


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--output',type=Path,required=True)
    p.add_argument('--screen',type=Path,required=True);a=p.parse_args();a.output.mkdir(parents=True,exist_ok=False)
    selected=read(a.screen)['selected'];summary=[]
    for method in ('SFC','DSFC','MSFC'):
        previous=None
        for substeps in (1,2,4,8):
            runner.YieldSimulator=refined_plant(substeps)
            r=runner.run_closed_loop(method=method,scenario='sustained_release_oblique',duration_s=.6,
                dt_s=.002,law_parameters=selected[method],record_fullstate=True)
            write(a.output/f'{method}-{substeps}.json.gz',r)
            current={'method':method,'plant_substeps':substeps,'controller_dt_s':.002,'metrics':r['metrics']}
            if previous is not None:
                ca=previous['rows'];fi=r['rows']
                current['force_difference_max_n']=max(abs(x['force_error_n']-y['force_error_n']) for x,y in zip(ca,fi))
                current['position_difference_max_m']=max(float(np.linalg.norm(np.asarray(x['position_m'])-y['position_m'])) for x,y in zip(ca,fi))
            summary.append(current);previous=r
            print(method,substeps,current.get('force_difference_max_n'),flush=True)
    write(a.output/'summary.json',{'checks':summary,'harness_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
          'claim_scope':'integration sensitivity at fixed controller period; not hardware evidence'})

if __name__=='__main__':main()
