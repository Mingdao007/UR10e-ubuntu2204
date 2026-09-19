"""Report mild/strong curvature development evidence without single-metric ranking."""
from pathlib import Path
import argparse,hashlib,math
from run_contact_yield import read,write
ROOT=Path(__file__).resolve().parents[1]
def fmt(value,scale=1.):return 'NA' if value is None else f'{value*scale:.3f}'
def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--input',type=Path,required=True);p.add_argument('--output',type=Path,required=True);a=p.parse_args()
    new=read(a.input/'summary.json')['runs'];assert len(new)==4
    retained=[]
    for row in read(ROOT/'runs/yield-transfer-v1/summary.json')['runs']:
        if row['variant']=='DSFC' and row['scenario']=='nominal':retained.append({**row,'variant':'legacy'})
    for row in read(ROOT/'runs/yield-observer-prior-v1/summary.json')['runs']:
        if row['prior']=='approach':retained.append({**row,'variant':'frozen_prior'})
    assert len(retained)==4
    table=['| Surface | Material | Observer | Full cycle | Failed | Normal RMS deg | Attitude RMS deg | Force MAE N | Peak N | Path RMS mm | Progress | Contact loss s |',
        '|---|---|---|---|---|---:|---:|---:|---:|---:|---:|---:|']
    allrows=[]
    for surface,rows in [('mild retained',retained),('strong SE-v1',new)]:
        for row in rows:
            assert hashlib.sha256(Path(row['path']).read_bytes()).hexdigest()==row['sha256']
            m=row['metrics'];normal=m.get('normal_estimation_rmse_rad');attitude=m.get('orientation_rmse_rad')
            table.append(f"| {surface} | {row['material']} | {row['variant']} | {row['full_cycle']} | {m['failed']} | {fmt(normal,180/math.pi)} | {fmt(attitude,180/math.pi)} | {fmt(m['force_mae_n'])} | {fmt(m['force_peak_n'])} | {fmt(m['path_rmse_m'],1000)} | {fmt(m['progress_ratio'])} | {fmt(m['contact_loss_duration_s'])} |")
            allrows.append({'surface':surface,**row})
    write(a.output/'results.json',{'dataset_role':'development','rows':allrows,'live_executed':False})
    text='''# SE-v1: surface variation with unchanged control task

Four new full-duration attempts, fixed DSFC candidate and two existing observer modes, plus four retained mild-surface comparators. Curvature changes from (0.8, 0.4) to (6, 8) inverse metres; path, initial approach, initial contact height/load construction and controller parameters remain fixed. The observer sees measured velocity/force only. No tuning or new observer is introduced.

The fixed reference visits planned true-normal deviations of 10.108 degrees RMS and 13.521 degrees maximum from the approach. These are evaluator diagnostics, never controller inputs. Actual visited normal variation is separately recorded in results.json, since poor physical progress could otherwise mimic good estimation.

'''+ '\n'.join(table)+'''

Full cycle means complete scheduled time coverage, not successful path execution. All data are development. A reduced force peak with worse attitude/path/progress is not a winner. Raw failure messages and every metric are retained in results.json. Physics, force-limit interpretation, robot speed clipping and sensor/servo fidelity remain unqualified. This nominal-only comparison cannot establish transverse-intervention robustness.
'''
    with (a.output/'results.md').open('x') as f:f.write(text)
if __name__=='__main__':main()
