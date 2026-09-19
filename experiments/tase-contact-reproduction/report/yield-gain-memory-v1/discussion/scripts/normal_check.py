import gzip, json, sys, numpy as np
def load(p):
    with gzip.open(p,'rt') as f: return json.load(f)
for p,label in ((sys.argv[1],sys.argv[2]),):
    D=load(p); rows=D['rows']; t=np.array([r['time_s'] for r in rows])
    ntrue=np.array([r['true_inward_normal'] for r in rows]); nest=np.array([r['normal_estimate'] for r in rows])
    approach=np.array(D['identity_payload']['approach_inward_base'])
    ang=lambda a,b: np.degrees(np.arccos(np.clip(np.einsum('ij,ij->i',a,b),-1,1)))
    e_est=ang(nest,ntrue); e_prior=ang(np.tile(approach,(len(ntrue),1)),ntrue)
    # tangential bias force from misaligned target: Fd*(-nest) projected on true tangent
    P=lambda n: np.eye(3)-np.outer(n,n)
    bias=np.array([np.linalg.norm(P(ntrue[i])@(5.0*(-nest[i]))) for i in range(0,len(rows),10)])
    path=np.array([r['path_error_m'] for r in rows])*1e3
    m=t>=12
    print('=== %s (%s)'%(label,D['method']))
    print(' normal-estimate error vs truth: RMS %.2f deg, max %.2f deg (t>=12s: RMS %.2f)'%(np.sqrt(np.mean(e_est**2)),e_est.max(),np.sqrt(np.mean(e_est[m]**2))))
    print(' approach-prior error vs truth  : RMS %.2f deg, max %.2f deg'%(np.sqrt(np.mean(e_prior**2)),e_prior.max()))
    print(' tangential bias of 5N target along true tangent: median %.3f N, max %.3f N  → /Kp(120) = %.2f mm median'%(np.median(bias),bias.max(),1e3*np.median(bias)/120))
    print(' path error mm: RMS %.3f (t>=12s %.3f), peak %.3f'%(np.sqrt(np.mean(path**2)),np.sqrt(np.mean(path[m]**2)),path.max()))
    fe=np.array([r['force_error_n'] for r in rows]); print(' force MAE all %.4f / t>=12s %.4f ; force err peak all %.3f / t>=12s %.3f'%(np.mean(abs(fe)),np.mean(abs(fe[m])),abs(fe).max(),abs(fe[m]).max()))
