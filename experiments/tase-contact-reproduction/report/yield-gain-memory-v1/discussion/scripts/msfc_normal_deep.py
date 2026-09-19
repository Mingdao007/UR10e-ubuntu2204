import gzip, json, numpy as np
def load(p):
    with gzip.open(p,'rt') as f: return json.load(f)
R='runs/yield-common-fix-qualification/'
A=load(R+'MSFC-sustained_release_normal-plant4.json.gz'); B=load(R+'MSFC-sustained_release_normal-plant8.json.gz')
def series(D):
    recs=[r for r in D['records'] if r['reference']['phase']=='path']
    t=np.array([r['reference']['path_time_s'] for r in recs])
    law=np.array([r['controller_snapshot']['law22']['values'] for r in recs])
    res=[r['result'] for r in recs]
    ns=np.array([x['normal_speed_m_s'] for x in res]); nsat=np.array([x['normal_saturated'] for x in res],dtype=bool)
    lawn=np.array([np.dot(x['law_normal_velocity_m_s'],x['inward_normal_base']) for x in res])
    ext=np.array([r['observation_after'].get('software_injection_base_n',[0,0,0]) for r in recs])
    tscale=np.array([x['task_scale'] for x in res]); qpi=np.array([x['qp_intervention'] for x in res],dtype=bool)
    qd=np.array([r['observation']['actual_qdot'] for r in recs]); 
    F=np.array([rw['true_normal_load_n'] for rw in D['rows']]); extF=np.array([np.linalg.norm(rw['external_force_base_n']) for rw in D['rows']])
    est=np.array([x['force_error_n'] for x in res])
    return dict(t=t,law=law,ns=ns,nsat=nsat,lawn=lawn,tscale=tscale,qpi=qpi,qd=qd,F=F,extF=extF,est=est)
a=series(A); b=series(B)
t=a['t']
print('slots 0-3 (const):',a['law'][0,:4])
print('external force norm: max=%.3f; windows where >0: %.2f..%.2f s'%(a['extF'].max(),t[a['extF']>0][0],t[a['extF']>0][-1]))
print('joint qdot at clip(0.05): plant4 ticks=%d plant8 ticks=%d'%((abs(a['qd'])>=0.05-1e-9).any(1).sum(),(abs(b['qd'])>=0.05-1e-9).any(1).sum()))
print('normal-cap saturation ticks plant4=%d plant8=%d ; task_scale<1 ticks plant4=%d plant8=%d'%(a['nsat'].sum(),b['nsat'].sum(),(a['tscale']<1-1e-9).sum(),(b['tscale']<1-1e-9).sum()))
print()
print('per-window (plant8): extF  minEig(slots19-21)  max|h|(guess slots 7-9)  maxS|slots10-15|  |w|max  ns_sat  lawn_max(mm/s)  min F')
edges=np.arange(0,64,2.)
for i in range(len(edges)-1):
    m=(t>=edges[i])&(t<edges[i+1])
    if not m.any(): continue
    L=b['law'][m]
    print(' %4.0f-%-4.0f ext=%.2f minEig=%.4f |h|=%.3f  maxS=%.4f  |w|=%.4f sat=%4d lawn=%.2f  minF=%.2f | plant4: minEig=%.4f sat=%4d minF=%.2f'%(
        edges[i],edges[i+1],a['extF'][m].max(),L[:,19:22].min(),np.linalg.norm(L[:,7:10],axis=1).max(),abs(L[:,10:16]).max(),np.linalg.norm(L[:,4:7],axis=1).max(),b['nsat'][m].sum(),abs(b['lawn'][m]).max()*1e3,b['F'][m].min(),
        a['law'][m][:,19:22].min(),a['nsat'][m].sum(),a['F'][m].min()))
# lag scan in windows
def lagscan(x,y,maxlag=400):
    x=x-x.mean();y=y-y.mean();best=(None,-2)
    for l in range(-maxlag,maxlag+1):
        if l>=0: xs,ys=x[l:],y[:len(y)-l]
        else: xs,ys=x[:len(x)+l],y[-l:]
        c=np.dot(xs,ys)/np.sqrt(np.dot(xs,xs)*np.dot(ys,ys)+1e-30)
        if c>best[1]: best=(l,c)
    c0=np.dot(x,y)/np.sqrt(np.dot(x,x)*np.dot(y,y)+1e-30)
    return best[0],best[1],c0
print()
print('lag scan (plant8 shifted vs plant4), residual rms after optimal shift:')
for lo,hi in ((14,20),(22,26),(26,32),(32,40),(40,52),(54,63.8)):
    m=(t>=lo)&(t<hi); x=a['F'][m]; y=b['F'][m]
    l,c,c0=lagscan(x,y)
    if l>=0: xs,ys=x[l:],y[:len(y)-l]
    else: xs,ys=x[:len(x)+l],y[-l:]
    rms0=np.sqrt(np.mean((x-y)**2)); rms1=np.sqrt(np.mean((xs-ys)**2))
    # dominant freq & amplitude
    for nm,z in (('p4',x),('p8',y)):
        zz=z-z.mean(); sp=np.abs(np.fft.rfft(zz*np.hanning(len(zz))))*2/len(zz)*2; fr=np.fft.rfftfreq(len(zz),0.002); k=np.argmax(sp[1:])+1
        print('   %2.0f-%4.1f s %s dom %.2f Hz amp~%.3f N   std=%.3f'%(lo,hi,nm,fr[k],sp[k],zz.std()))
    print('   %2.0f-%4.1f s best lag=%+d ticks (%.0f ms) corr0=%.3f corr=%.3f  rms dF before=%.3f after=%.3f'%(lo,hi,l,l*2,c0,c,rms0,rms1))
