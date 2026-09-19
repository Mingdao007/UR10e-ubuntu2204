import gzip, json, numpy as np
def load(p):
    with gzip.open(p,'rt') as f: return json.load(f)
R='runs/yield-common-fix-qualification/'
files={'p4':R+'MSFC-sustained_release_normal-plant4.json.gz','p8':R+'MSFC-sustained_release_normal-plant8.json.gz','p16':'/tmp/ygm/ref-normal-p16.json.gz','twin8':'/tmp/ygm/twin-normal-p8-eps1e-9.json.gz'}
D={k:load(v) for k,v in files.items()}
rows={k:D[k]['rows'] for k in D}
t=np.array([r['time_s'] for r in rows['p8']])
F={k:np.array([r['true_normal_load_n'] for r in rows[k]]) for k in rows}
print('twin exact: max|dF| = %.3e N ; max|dP| = %.3e m'%(abs(F['twin8']-F['p8']).max(),max(np.linalg.norm(np.array(a['position_m'])-np.array(b['position_m'])) for a,b in zip(rows['p8'],rows['twin8']))))
def bandpass(x,lo=1.2,hi=4.0,dt=0.002):
    X=np.fft.rfft(x-x.mean()); fr=np.fft.rfftfreq(len(x),dt); X[(fr<lo)|(fr>hi)]=0; return np.fft.irfft(X,len(x))
def analytic(x):
    X=np.fft.fft(x); n=len(x); h=np.zeros(n); h[0]=1; h[1:(n+1)//2]=2
    if n%2==0: h[n//2]=1
    return np.fft.ifft(X*h)
seg=(t>=36)&(t<=52)   # post-release, no external force, no contact loss
print('\nlimit-cycle frequency (36-52 s, zero-crossing count of band-passed force) and amplitude:')
freq={}
for k in ('p4','p8','p16'):
    x=bandpass(F[k][seg]); zc=np.where(np.diff(np.sign(x))>0)[0]; T=(t[seg][zc[-1]]-t[seg][zc[0]])/(len(zc)-1)
    freq[k]=1/T; print('  %-4s f=%.4f Hz  period=%.2f ms  cycles=%d  amp(std*sqrt2)=%.3f N'%(k,1/T,T*1e3,len(zc)-1,x.std()*np.sqrt(2)))
print('  Δf(p4-p8)=%.4f Hz  Δf(p8-p16)=%.4f Hz  ratio=%.2f  (first-order ⇒ ~2)'%(freq['p4']-freq['p8'],freq['p8']-freq['p16'],(freq['p4']-freq['p8'])/(freq['p8']-freq['p16']+1e-12)))
print('\nunwrapped phase difference (rad) of band-passed force vs time, referenced to p16:')
ph={k:np.unwrap(np.angle(analytic(bandpass(F[k][seg])))) for k in ('p4','p8','p16')}
ts=t[seg]
print('   t(s)   φ(p4)-φ(p16)   φ(p8)-φ(p16)')
for tt in np.arange(36,52.1,2):
    i=np.argmin(abs(ts-tt)); print('  %5.1f   %+8.3f      %+8.3f'%(tt,ph['p4'][i]-ph['p16'][i],ph['p8'][i]-ph['p16'][i]))
s4=np.polyfit(ts,ph['p4']-ph['p16'],1)[0]; s8=np.polyfit(ts,ph['p8']-ph['p16'],1)[0]
print('  slope rad/s: p4-p16 %+.4f  p8-p16 %+.4f  → Δf %.4f / %.4f Hz'%(s4,s8,s4/(2*np.pi),s8/(2*np.pi)))
# invariants
print('\ninvariants vs substeps:')
for k in ('p4','p8','p16'):
    m=D[k]['metrics']; f=F[k]; lost=f<1.0; ev=int((np.diff(lost.astype(int))>0).sum())
    print('  %-4s MAE=%.4f peak=%.3f contact_loss=%.3f s (events=%d, first=%.3f s) sat=%d qp=%d  amp36-52=%.3f N f=%.4f Hz'%(k,m['force_mae_n'],m['force_peak_n'],m['contact_loss_duration_s'],ev,t[lost][0] if lost.any() else -1,m['saturation_ticks'],m['qp_intervention_ticks'],bandpass(f[seg]).std()*np.sqrt(2),freq[k]))
# joint clip timing (records exist for p4/p8)
for k in ('p4','p8'):
    recs=[r for r in D[k]['records'] if r['reference']['phase']=='path']
    tt=np.array([r['reference']['path_time_s'] for r in recs]); qd=np.array([r['observation']['actual_qdot'] for r in recs])
    clip=(abs(qd)>=0.05-1e-9)
    print('\n %s joint-velocity clip (0.05 rad/s) ticks per joint:'%k,clip.sum(0).tolist())
    for lo,hi in ((0,20),(20,26),(26,32),(32,40),(40,52),(52,64)):
        mm=(tt>=lo)&(tt<hi); print('   %2d-%2d s clip ticks=%d'%(lo,hi,clip[mm].any(1).sum()))
