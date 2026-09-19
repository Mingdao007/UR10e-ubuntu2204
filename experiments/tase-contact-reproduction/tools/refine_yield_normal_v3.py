"""NO-v3 fixed-parameter 1 ms check of combined versus motion-only prior correction."""
import argparse,hashlib
from pathlib import Path
from concurrent.futures import ProcessPoolExecutor
from datetime import datetime,timezone
from run_contact_yield import read,write
from contact_yield_runner import run_closed_loop
from contact_yield_replay import refinement_error

def job(spec):
    output,cell,params,coarse=spec
    r=run_closed_loop(method='DSFC',material='stiff_low_mu',scenario=cell['scenario'],duration_s=coarse['duration_s'],dt_s=.001,
        timeline='full_cycle',plant_substeps=8,law_parameters=params,estimator_parameters=cell['observer'],surface_parameters=cell['surface'])
    path=output/(cell['id']+'.json.gz');write(path,r)
    diagnostic=None
    if not r['metrics']['failed'] and not coarse['metrics']['failed']:
        diagnostic=refinement_error(coarse['rows'],r['rows'],coarse_dt_s=.002,fine_dt_s=.001)
    return {'id':cell['id'],'path':str(path.resolve()),'sha256':hashlib.sha256(path.read_bytes()).hexdigest(),
        'full_cycle':r['full_cycle'],'metrics':r['metrics'],'refinement':diagnostic}
def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--input',type=Path,required=True);p.add_argument('--output',type=Path,required=True);a=p.parse_args()
    a.output.mkdir(parents=True,exist_ok=False);protocol=read(a.input/'protocol.json');summary=read(a.input/'summary.json')
    names=('combined-mild-across','motion-only-mild-across');cells=[c for c in protocol['cells'] if c['id'] in names]
    baseline={r['id']:r for r in summary['runs']};specs=[]
    for c in cells:
        row=baseline[c['id']];path=Path(row['path']);assert hashlib.sha256(path.read_bytes()).hexdigest()==row['sha256']
        # Pass only summary rows to workers; no full-state duplicate in parent pool.
        r=read(path);coarse={k:r[k] for k in ('duration_s','rows','metrics')};del r
        specs.append((a.output,c,protocol['law_parameters'],coarse))
    manifest={'started_at':datetime.now(timezone.utc).isoformat(),'workers':2,'dt_s':.001,'plant_substeps':8,
        'cells':cells,'dataset_role':'development','parameters_changed':False,'exit_code':None,
        'interpretation':'Controller and plant substep both halve. This is numerical sensitivity, not independent physics or physical acceptance.'}
    write(a.output/'protocol.json',manifest)
    try:
        with ProcessPoolExecutor(max_workers=2) as pool:results=list(pool.map(job,specs))
        write(a.output/'summary.json',{'runs':results,'dataset_role':'development'});manifest['exit_code']=0
    except BaseException:manifest['exit_code']=1;raise
    finally:manifest['finished_at']=datetime.now(timezone.utc).isoformat();write(a.output/'manifest.json',manifest)
if __name__=='__main__':main()
