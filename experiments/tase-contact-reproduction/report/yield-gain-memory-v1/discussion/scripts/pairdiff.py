import gzip, json, sys, numpy as np
def load(p):
    with gzip.open(p,'rt') as f: return json.load(f)
def arr(rows,k): return np.asarray([r[k] for r in rows],dtype=float)
def analyze(pa,pb,label):
    A=load(pa);B=load(pb)
    ra,rb=A['rows'],B['rows']
    t=arr(ra,'time_s')
    Fa=arr(ra,'true_normal_load_n');Fb=arr(rb,'true_normal_load_n')
    Pa=arr(ra,'position_m');Pb=arr(rb,'position_m')
    dF=Fb-Fa; dP=np.linalg.norm(Pb-Pa,axis=1)
    sa=arr(ra,'saturated');sb=arr(rb,'saturated')
    print('=== %s'%label)
    print('n=%d  max|dF|=%.4f at t=%.3f  max|dP|=%.3e at t=%.3f'%(len(t),abs(dF).max(),t[abs(dF).argmax()],dP.max(),t[dP.argmax()]))
    # windowed stats (every 2 s)
    edges=np.arange(0,t[-1]+2,2.0)
    print(' window_s   max|dF|   rms dF   max|dP|mm  sat4  sat8  minF4  minF8  maxF4  maxF8')
    for i in range(len(edges)-1):
        m=(t>=edges[i])&(t<edges[i+1])
        if not m.any(): continue
        print(' %5.0f-%-4.0f %8.4f %8.4f %9.4f %5d %5d %6.2f %6.2f %6.2f %6.2f'%(edges[i],edges[i+1],abs(dF[m]).max(),np.sqrt(np.mean(dF[m]**2)),dP[m].max()*1e3,sa[m].sum(),sb[m].sum(),Fa[m].min(),Fb[m].min(),Fa[m].max(),Fb[m].max()))
    # contact loss events
    for nm,F in (('plant4',Fa),('plant8',Fb)):
        lost=F<1.0
        if lost.any():
            idx=np.where(np.diff(lost.astype(int))!=0)[0]
            print('  %s contact-loss(<1N) ticks=%d first t=%.3f last t=%.3f transitions=%d'%(nm,lost.sum(),t[lost][0],t[lost][-1],len(idx)))
        zero=F<=0.0
        if zero.any(): print('  %s zero-load ticks=%d first t=%.3f last t=%.3f'%(nm,zero.sum(),t[zero][0],t[zero][-1]))
    # dominant oscillation: spectrum of force error in a quiet window (20-60 s)
    m=(t>=20)&(t<60)
    for nm,F in (('plant4',Fa),('plant8',Fb)):
        x=F[m]-F[m].mean(); sp=np.abs(np.fft.rfft(x*np.hanning(len(x))));fr=np.fft.rfftfreq(len(x),0.002)
        top=np.argsort(sp[1:])[-5:][::-1]+1
        print('  %s force spectrum 20-60s top freqs Hz: '%nm+', '.join('%.3f(%.2g)'%(fr[k],sp[k]) for k in top))
    # cross-correlation lag between plant4 and plant8 force in late window -> phase drift?
    x=Fa[m]-Fa[m].mean(); y=Fb[m]-Fb[m].mean()
    lags=np.arange(-50,51); cc=[np.dot(x[max(0,l):len(x)+min(0,l)],y[max(0,-l):len(y)+min(0,-l)]) for l in lags]
    print('  best lag (ticks, plant8 vs plant4) in 20-60s: %d  (corr0=%.4f, best=%.4f)'%(lags[int(np.argmax(cc))],cc[50]/np.sqrt(np.dot(x,x)*np.dot(y,y)),max(cc)/np.sqrt(np.dot(x,x)*np.dot(y,y))))
    return dict(t=t,dF=dF,dP=dP,Fa=Fa,Fb=Fb)
if __name__=='__main__':
    analyze(sys.argv[1],sys.argv[2],sys.argv[3])
