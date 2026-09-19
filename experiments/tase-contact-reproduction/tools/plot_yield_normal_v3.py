"""Development-only NO-v3 task-metric plot; no uncertainty or winner claim."""
import argparse,json,math
from pathlib import Path
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt

def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--report',type=Path,required=True);a=p.parse_args()
    rows=json.loads((a.report/'results.json').read_text())['rows']
    labels=[r['id'].replace('combined-','CP+motion ').replace('retained-','frozen ').replace('motion-only-','motion ').replace('mild-','').replace('-',' ') for r in rows]
    fig,axes=plt.subplots(2,2,figsize=(13,10),constrained_layout=True)
    fields=[('normal_estimation_rmse_rad','Normal estimation RMS (deg)',180/math.pi),('path_rmse_m','Path RMS (mm)',1000),
        ('force_peak_n','Contact peak (N)',1),('progress_ratio','Measured progress ratio',1)]
    for ax,(field,title,scale) in zip(axes.flat,fields):
        values=[r['metrics'][field] for r in rows]
        ax.barh(range(len(rows)),[float('nan') if v is None else v*scale for v in values],color=['#2878a0' if not r['id'].startswith('retained-') else '#888888' for r in rows])
        ax.set_yticks(range(len(rows)),labels,fontsize=8);ax.invert_yaxis();ax.set_title(title);ax.grid(axis='x',alpha=.2)
    fig.suptitle('NO-v3: fixed DSFC, stiff material, development only\nOne trial per cell; retained frozen comparators in gray. No holdout or physical qualification.')
    for extension in ('png','pdf','svg'):fig.savefig(a.report/f'comparison.{extension}',dpi=160)
    svg=a.report/'comparison.svg';svg.write_text('\n'.join(x.rstrip() for x in svg.read_text().splitlines())+'\n')
if __name__=='__main__':main()
