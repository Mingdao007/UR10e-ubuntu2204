"""Development figure: all eight cells, no uncertainty or ranking inferred."""
from pathlib import Path
import argparse,json,math
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt

def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--report',type=Path,required=True);a=p.parse_args()
    data=json.loads((a.report/'results.json').read_text())['rows']
    fig,axes=plt.subplots(2,2,figsize=(10,7))
    specifications=[('normal_estimation_rmse_rad',180/math.pi,'Normal estimate RMS (deg)'),
        ('path_rmse_m',1000,'Path RMS (mm)'),('progress_ratio',1,'Measured progress / reference'),('force_mae_n',1,'Contact force MAE (N)')]
    for ax,(metric,scale,label) in zip(axes.flat,specifications):
        for material,marker,style in [('stiff_low_mu','o','-'),('compliant_high_mu','s','--')]:
            for observer,color in [('legacy','#2166ac'),('frozen_prior','#d95f02')]:
                rows=[next(r for r in data if r['material']==material and r['variant']==observer and r['surface']==surface)
                    for surface in ('mild retained','strong SE-v1')]
                values=[r['metrics'][metric] for r in rows]
                assert all(v is not None for v in values), 'do not silently omit failed cells'
                ax.plot([0,1],[scale*v for v in values],color=color,marker=marker,linestyle=style,
                    label=f"{observer.replace('_',' ')} / {'stiff' if material=='stiff_low_mu' else 'compliant'}")
        ax.set_xticks([0,1],['Mild surface','Stronger curvature']);ax.set_ylabel(label)
        ax.set_xlim(-.15,1.15);ax.set_ylim(bottom=0);ax.grid(axis='y',alpha=.25)
    handles,labels=axes[0,0].get_legend_handles_labels()
    fig.legend(handles,labels,loc='upper center',bbox_to_anchor=(.5,.95),ncol=2,frameon=False)
    fig.suptitle('SE-v1: shared-observer sensitivity with fixed DSFC',y=.995)
    fig.text(.5,.015,'Simulation development: one run per cell; no error bars or holdout. Lines connect paired conditions only.',ha='center',fontsize=9)
    fig.tight_layout(rect=(0,.045,1,.86))
    for suffix in ('png','pdf','svg'):fig.savefig(a.report/f'comparison.{suffix}',dpi=180)
    svg=a.report/'comparison.svg'
    svg.write_text('\n'.join(line.rstrip() for line in svg.read_text().splitlines())+'\n')
    plt.close(fig)
if __name__=='__main__':main()
