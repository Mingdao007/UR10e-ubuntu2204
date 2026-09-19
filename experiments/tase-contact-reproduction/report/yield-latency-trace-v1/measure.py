"""Development compute lower bound; no device IO, pacing or formal timing claim."""
import sys, time, json, platform, os, subprocess, hashlib, cProfile, pstats, argparse, gc, resource
from pathlib import Path
import numpy as np
import pytest
ROOT=Path(__file__).resolve().parents[2]
sys.path[:0]=[str(ROOT/'tests'),str(ROOT/'tools')]
import test_contact_yield_provider as fixture
from build_contact_qp import build
from step5d_autotune_v4_r004.qualification import CanonicalQualificationControl
parser=argparse.ArgumentParser(description=__doc__)
parser.add_argument("--output",type=Path,default=Path(__file__).resolve().parent)
parser.add_argument("--skip-profile",action="store_true")
parser.add_argument("--mature-gc-policy",action="store_true",help="Reproduce existing bounded writer GC policy; no production change")
args=parser.parse_args()
out=args.output.resolve();out.mkdir(parents=True,exist_ok=True)
if (out/'results.json').exists():
    raise FileExistsError('Use a new --output directory; retained timing receipts are immutable')
lib=build(out/'build')
original_step=CanonicalQualificationControl.step
original_runtime=fixture.YieldContactRuntime
result={'scope':'offline unpaced stationary observation qualification compute, excludes transport, logging, scheduler and evidence collector; not realtime qualification','period_ns':2000000,'platform':platform.platform(),'python':sys.version,'cpu_affinity':sorted(os.sched_getaffinity(0)),'base_commit':subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip(),'methods':{},'mature_gc_policy':args.mature_gc_policy}
for method in ('SFC','DSFC','MSFC'):
    times=[]; cpu_times=[]; gc_events=[]; active_tick=None; gc_states=set()
    def trace_gc(phase,info):
        gc_events.append({'phase':phase,'generation':info['generation'],
                          'wall_ns':time.perf_counter_ns(),'cpu_ns':time.thread_time_ns(),
                          'tick':active_tick,'collected':info.get('collected',0),
                          'uncollectable':info.get('uncollectable',0)})
    def measured(self,**kwargs):
        global active_tick
        active_tick=len(times)
        gc_states.add(gc.isenabled())
        start=time.perf_counter_ns();cpu_start=time.thread_time_ns()
        try:return original_step(self,**kwargs)
        finally:
            cpu_times.append(time.thread_time_ns()-cpu_start)
            times.append(time.perf_counter_ns()-start)
            active_tick=None
    def runtime_factory(**kwargs):
        kwargs['method']=method
        return original_runtime(**kwargs)
    gc_was_enabled=gc.isenabled()
    if args.mature_gc_policy:
        gc.collect()
        gc.disable()
    try:
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(CanonicalQualificationControl,'step',measured)
            mp.setattr(fixture,'YieldContactRuntime',runtime_factory)
            started=time.time()
            gc.callbacks.append(trace_gc)
            try:fixture.test_mature_qualification_entry_and_formal_clock_use_same_native_provider(lib,mp)
            finally:gc.callbacks.remove(trace_gc)
    finally:
        if gc_was_enabled and not gc.isenabled():gc.enable()
    values=np.array(times,dtype=np.int64)
    np.save(out/f'{method}-tick-ns.npy',values)
    np.save(out/f'{method}-cpu-ns.npy',np.array(cpu_times,dtype=np.int64))
    (out/f'{method}-gc.json').write_text(json.dumps(gc_events,indent=2)+'\n')
    def summary(v):return {'n':len(v),'p50_ms':float(np.quantile(v,.5)/1e6),'p95_ms':float(np.quantile(v,.95)/1e6),'p99_ms':float(np.quantile(v,.99)/1e6),'max_ms':float(max(v)/1e6),'over_2ms':int(np.sum(v>2000000))}
    result['methods'][method]={'all':summary(values),'entry':summary(values[:500]),'formal':summary(values[500:]),'wall_s':time.time()-started,'cpu':summary(np.array(cpu_times)), 'gc_states_in_step':sorted(gc_states),'maxrss_kib':resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,'gc_event_count':len(gc_events), 'gc_enabled':gc.isenabled(), 'gc_threshold':gc.get_threshold()}
    (out/'results.json').write_text(json.dumps(result,indent=2)+'\n')
    print(method,result['methods'][method],flush=True)
if args.skip_profile:
    raise SystemExit(0)
# Profiling is a separate instrumented run and never used as latency evidence.
with pytest.MonkeyPatch.context() as mp:
    profiler=cProfile.Profile()
    profiler.enable()
    fixture.test_mature_qualification_entry_and_formal_clock_use_same_native_provider(lib,mp)
    profiler.disable()
profiler.dump_stats(str(out/'msfc-profile.pstats'))
with (out/'msfc-profile.txt').open('w') as f:pstats.Stats(profiler,stream=f).strip_dirs().sort_stats('cumulative').print_stats(65)
