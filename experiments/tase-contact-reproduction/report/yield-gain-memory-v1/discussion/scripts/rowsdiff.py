import gzip, json, sys, numpy as np
def load(p):
    with gzip.open(p,'rt') as f: return json.load(f)
def arr(rows,k): return np.asarray([r[k] for r in rows],dtype=float)
def lagscan(x,y,maxlag=400):
    x=x-x.mean();y=y-y.mean();best=(0,-2)
    for l in range(-maxlag,maxlag+1):
        xs,ys=(x[l:],y[:len(y)-l]) if l>=0 else (x[:len(x)+l],y[-l:])
        c=np.dot(xs,ys)/np.sqrt(np.dot(xs,xs)*np.dot(ys,ys)+1e-30)
        if c>best[1]: best=(l,c)
    return best
def main(pa,pb,label,windows=((0,6),(6,14),(14,20),(20,26),(26,32),(32,40),(40,52),(52,63.8))):
    A=load(pa);B=load(pb); ra,rb=A['rows'],B['rows']
    t=arr(ra,'time_s'); Fa=arr(ra,'true_normal_load_n');Fb=arr(rb,'true_normal_load_n'); Pa=arr(ra,'position_m');Pb=arr(rb,'position_m')
    dF=Fb-Fa; dP=np.linalg.norm(Pb-Pa,axis=1)
    ma,mb=A['metrics'],B['metrics']
    print('=== %s'%label)
    print(' A: %s | B: %s'%(A.get('advisory_note',{'perturbation_rad_joint0':'receipt'}),B.get('advisory_note',{'perturbation_rad_joint0':'receipt'})))
    print(' invariants A|B: MAE %.4f|%.4f  peak %.3f|%.3f  contact_loss_s %.3f|%.3f  sat %d|%d  qp %d|%d  pathRMS mm %.3f|%.3f'%(ma['force_mae_n'],mb['force_mae_n'],ma['force_peak_n'],mb['force_peak_n'],ma['contact_loss_duration_s'],mb['contact_loss_duration_s'],ma['saturation_ticks'],mb['saturation_ticks'],ma['qp_intervention_ticks'],mb['qp_intervention_ticks'],1e3*ma['path_rmse_m'],1e3*mb['path_rmse_m']))
    print(' overall max|dF|=%.4f N at t=%.3f ; max|dP|=%.4f mm'%(abs(dF).max(),t[abs(dF).argmax()],1e3*dP.max()))
    print(' window    max|dF|  rms|dF|  lag(ms)  corr0  corrLag  rmsAfterLag  dom(Hz) ampA ampB')
    for lo,hi in windows:
        m=(t>=lo)&(t<hi); x=Fa[m]; y=Fb[m]
        l,c=lagscan(x,y); xs,ys=(x[l:],y[:len(y)-l]) if l>=0 else (x[:len(x)+l],y[-l:])
        c0=np.corrcoef(x,y)[0,1] if x.std()>0 and y.std()>0 else float('nan')
        def amp(z):
            zz=z-z.mean(); sp=np.abs(np.fft.rfft(zz*np.hanning(len(zz))))*4/len(zz); fr=np.fft.rfftfreq(len(zz),0.002); k=np.argmax(sp[1:])+1; return fr[k],sp[k]
        fa,aa=amp(x); fb,ab=amp(y)
        print(' %4.0f-%-5.1f %8.4f %8.4f %7d %7.3f %7.3f %10.4f   %5.2f %6.3f %6.3f'%(lo,hi,abs(dF[m]).max(),np.sqrt(np.mean(dF[m]**2)),2*l,c0,c,np.sqrt(np.mean((xs-ys)**2)),fa,aa,ab))
if __name__=='__main__': main(sys.argv[1],sys.argv[2],sys.argv[3])
