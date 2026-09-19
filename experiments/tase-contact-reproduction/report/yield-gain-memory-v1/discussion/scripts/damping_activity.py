import gzip, json, sys, numpy as np
def load(p):
    with gzip.open(p,'rt') as f: return json.load(f)
p=sys.argv[1]; label=sys.argv[2]
D=load(p); prm=D['identity_payload']['parameters']; method=D['method']
recs=[r for r in D['records'] if r['reference']['phase']=='path']
t=np.array([r['reference']['path_time_s'] for r in recs])
u=np.array([r['result']['law_force_base_n'] for r in recs]); w=np.array([r['result']['law_state'] for r in recs])[:,:3]
v=np.array([r['result']['law_velocity_base_m_s'] for r in recs])
inward=np.array([r['result']['inward_normal_base'] for r in recs])
un=np.einsum('ij,ij->i',u,inward); ut=np.linalg.norm(u-un[:,None]*inward,axis=1)
m,g,mu,n=prm['m'],prm['g'],prm['mu'],prm['n']
r=np.linalg.norm(w,axis=1)
if method=='SFC':
    damp=mu*np.abs(w)**(n-1)*w
elif method=='DSFC':
    a,pp=prm['a'],prm['p']; damp=(a*np.where(r>0,r**(pp-1),0)+mu*r**(n-1))[:,None]*w
else:
    a,pp=prm['a'],prm['p']
    law=np.array([r_['controller_snapshot']['law22']['values'] for r_ in recs]); S=law[:,10:19].reshape(-1,3,3)
    lam=prm['minimum_metric_eigenvalue']
    A=np.zeros_like(S)
    for i in range(len(S)):
        e,V=np.linalg.eigh(S[i]); A[i]=(V*(lam+(1-lam)*np.exp(e)))@V.T
    Aw=np.einsum('ijk,ik->ij',A,w); q=np.einsum('ij,ij->i',w,Aw)
    damp=(a*np.where(r>0,r**(pp-1),0))[:,None]*w+(mu*q**((n-1)/2))[:,None]*Aw
dn=np.linalg.norm(damp,axis=1); un_abs=np.linalg.norm(u,axis=1)
print('=== %s  (%s g=%.4f mu=%.1f m=%.1f)'%(label,method,g,mu,m))
print(' v-space: integrator gain g/m=%.4f 1/kg ; cubic coeff mu/g^3=%.3e ; speed where cubic=1N: %.1f mm/s'%(g/m,mu/g**3,1e3*(1/mu)**(1/3)*g))
print(' |w| max=%.4f  |v|=g|w| max=%.2f mm/s ; |u| median=%.3f max=%.3f N'%(r.max(),1e3*g*r.max(),np.median(un_abs),un_abs.max()))
ratio=dn/np.maximum(un_abs,1e-9)
for lo,hi in ((0,20),(20,36),(36,52),(52,63.8)):
    mm=(t>=lo)&(t<hi)
    print('  %2.0f-%4.1f s: |damp| median=%.4f p99=%.4f max=%.4f N ; |u| median=%.3f p99=%.3f ; damp/u median=%.3f p99=%.3f ; |u_n| median=%.3f |u_t| median=%.3f'%(lo,hi,np.median(dn[mm]),np.quantile(dn[mm],.99),dn[mm].max(),np.median(un_abs[mm]),np.quantile(un_abs[mm],.99),np.median(ratio[mm]),np.quantile(ratio[mm],.99),np.median(abs(un[mm])),np.median(ut[mm])))
