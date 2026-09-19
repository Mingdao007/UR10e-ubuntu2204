"""Report frozen NO-v3 cells and existing matched recovery metrics."""
import argparse,gc,hashlib,math
from pathlib import Path
from run_contact_yield import read,write
from contact_yield_metrics import compare_pair

def fmt(v,scale=1.):return 'NA' if v is None else f'{v*scale:.3f}'
def checked(row):
    p=Path(row['path']);assert hashlib.sha256(p.read_bytes()).hexdigest()==row['sha256']
    return read(p)
def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--input',type=Path,required=True);p.add_argument('--output',type=Path,required=True);a=p.parse_args()
    a.output.mkdir(parents=True,exist_ok=True)
    protocol=read(a.input/'protocol.json');new=read(a.input/'summary.json')['runs'];assert len(new)==9
    retained=protocol['retained'];index={x['id']:x for x in new}
    baseline=next(x for x in retained if x.get('prior')=='approach')
    for kind,nominal_row in [('combined',index['combined-mild-approach']),('frozen',baseline)]:
        nominal=checked(nominal_row)
        for direction in ('normal','tangent'):
            row=index[f'{kind}-{direction}-hold'];disturbed=checked(row)
            row['pair']=compare_pair(nominal,disturbed)
            del disturbed;gc.collect()
        del nominal;gc.collect()
    table=['| Cell | Full | Failed | Normal RMS deg | Attitude RMS deg | Force MAE N | Peak N | Path RMS mm | Progress | Contact loss s | Recovery s |',
        '|---|---|---|---:|---:|---:|---:|---:|---:|---:|---|']
    rows=new+[{'id':'retained-'+x.get('prior','strong-frozen'),**x} for x in retained]
    for row in rows:
        assert hashlib.sha256(Path(row['path']).read_bytes()).hexdigest()==row['sha256']
        m=row['metrics'];pair=row.get('pair',{})
        recovery='censored' if pair.get('right_censored') else fmt(pair.get('recovery_s'))
        table.append('| '+' | '.join([row['id'],str(row['full_cycle']),str(m['failed']),fmt(m['normal_estimation_rmse_rad'],180/math.pi),
            fmt(m['orientation_rmse_rad'],180/math.pi),fmt(m['force_mae_n']),fmt(m['force_peak_n']),fmt(m['path_rmse_m'],1000),
            fmt(m['progress_ratio']),fmt(m['contact_loss_duration_s']),recovery])+' |')
    write(a.output/'results.json',{'dataset_role':'development','rows':rows,'live_executed':False})
    with (a.output/'results.md').open('x') as f:
        f.write('# NO-v3 combined observer: frozen development comparison\n\nNine new DSFC cells, one material, fixed gains. Four prior/curvature comparators are retained with verified hashes. All results are development; no holdout, physical qualification, or single-metric winner.\n\n'+'\n'.join(table)+'\n\nRecovery uses the existing matched nominal definition, with right censoring retained. Full cycle denotes scheduled time coverage, not successful path completion. Initial-state and PATH-start normal errors, gate fractions, tail errors and all failure messages are in results.json. The coplanarity term assumes the measured force is in the normal/actual-slide plane; transverse external force can violate that condition. The ideal Coulomb simulator cannot establish noise, stick-slip or physical intervention robustness.\n')
if __name__=='__main__':main()
