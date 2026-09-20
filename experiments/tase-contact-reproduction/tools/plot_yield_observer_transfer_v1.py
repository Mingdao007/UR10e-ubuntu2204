"""Plot OT-v1 fixed-parameter observer interactions, no final method ranking."""
import argparse,json,math
from pathlib import Path
import numpy as np
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt

def main():
 p=argparse.ArgumentParser(description=__doc__);p.add_argument('--report',type=Path,required=True);a=p.parse_args()
 rows=json.loads((a.report/'results.json').read_text())['rows'];index={(r['method'],r['observer'],r['scenario']):r for r in rows}
 methods=('SFC','DSFC','MSFC');scenarios=('nominal','sustained_release_normal','sustained_release_tangent')
 fields=[('normal_estimation_rmse_rad','Normal RMS (deg)',180/math.pi),('force_mae_n','Force MAE (N)',1),('path_rmse_m','Path RMS (mm)',1000),('recovery_s','Recovery (s)',1)]
 fig,axes=plt.subplots(3,4,figsize=(13,8),constrained_layout=True)
 for i,scenario in enumerate(scenarios):
  for j,(field,label,scale) in enumerate(fields):
   ax=axes[i,j]
   for k,observer in enumerate(('legacy','NO-v3')):
    vals=[]
    for method in methods:
     row=index[method,observer,scenario];v=(row.get('pair',{}).get(field) if field=='recovery_s' else row['metrics'][field]);vals.append(np.nan if v is None else v*scale)
    ax.bar(np.arange(3)+(k-.5)*.36,vals,.36,label=observer,color=('#8b9197','#2878a0')[k])
   ax.set_xticks(np.arange(3),['SFC','DSFC','MSFC g50'],fontsize=8);ax.set_title(label,fontsize=10);ax.grid(axis='y',alpha=.2)
   if j==0:ax.set_ylabel(('Nominal','Normal hold','Tangent hold')[i])
   if i==0 and field=='recovery_s':
    ax.set_axis_off();ax.text(.5,.5,'Not applicable\nto nominal',ha='center',va='center',transform=ax.transAxes)
 axes[0,0].legend(fontsize=8)
 fig.suptitle('OT-v1: same observer across fixed control candidates\nDevelopment only; no equal-budget optimization or physical qualification.\nSFC / NO-v3 normal hold: load below 1 N for 0.434 s; no geometric separation in the trace.',fontsize=12)
 for ext in ('png','pdf','svg'):fig.savefig(a.report/f'comparison.{ext}',dpi=160)
 svg=a.report/'comparison.svg';svg.write_text('\n'.join(x.rstrip() for x in svg.read_text().splitlines())+'\n')
if __name__=='__main__':main()
