"""Standalone GM-v1 development figure from the retained summary."""
import argparse
import json
from pathlib import Path
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--summary',type=Path,required=True)
    p.add_argument('--output',type=Path,required=True)
    a=p.parse_args();x=json.loads(a.summary.read_text())
    labels=list(dict.fromkeys(c['variant'] for c in x['receipts']))
    ticklabels=['g100\nmemory on','g100\nidentity metric','g50\nmemory on','g50\nidentity metric']
    cols=['#75507b','#888a85','#3465a4','#9bbad8']
    normal={c['variant']:c for c in x['receipts'] if c['substeps']==8 and c['scenario']=='sustained_release_normal'}
    nominal={c['variant']:c for c in x['receipts'] if c['substeps']==8 and c['scenario']=='nominal'}
    error={c['variant']:c['difference']['force_error_max_n'] for c in x['refinement'] if c['scenario']=='sustained_release_normal'}
    recovery={c['variant']:c['pair']['recovery_s'] for c in x['pairs'] if c['substeps']==8}
    fig,axs=plt.subplots(2,2,figsize=(10,7),constrained_layout=True)
    data=([error[k] for k in labels], [normal[k]['metrics']['force_peak_n'] for k in labels],
          [recovery[k] for k in labels], [1000*nominal[k]['metrics']['path_rmse_m'] for k in labels])
    titles=('Unaligned force refinement difference (N)','Normal intervention contact peak (N)',
            'Post-release recovery (s)','Nominal path RMS (mm)')
    for ax,values,title in zip(axs.flat,data,titles):
        ax.bar(ticklabels,values,color=cols)
        ax.set_title(title,fontsize=11);ax.grid(axis='y',alpha=.2)
        for i,v in enumerate(values):ax.text(i,v,f'{v:.3f}',ha='center',va='bottom',fontsize=9)
        ax.margins(y=.18)
    fig.suptitle('GM-v1: gain x mechanical memory coupling\nDevelopment simulation, not a formal method comparison',fontsize=13)
    a.output.mkdir(parents=True,exist_ok=True)
    fig.savefig(a.output/'gain-memory.png',dpi=160)
    fig.savefig(a.output/'gain-memory.pdf')


if __name__=='__main__':main()
