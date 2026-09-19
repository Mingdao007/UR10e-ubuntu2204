"""Exact telemetry equivalence and isolated mixed-age insertion costs."""
import sys,json,time,random,argparse
from pathlib import Path
import numpy as np
ROOT=Path(__file__).resolve().parents[2];sys.path.insert(0,str(ROOT/'tools'))
from contact_benchmark_protocol import SensorFreshnessTracker,_percentile
parser=argparse.ArgumentParser(description=__doc__)
parser.add_argument("--output",type=Path,default=Path(__file__).resolve().parent)
args=parser.parse_args()
out=args.output.resolve();out.mkdir(parents=True,exist_ok=True)
if (out/"age-stream-results.json").exists():
    raise FileExistsError("Use a new --output directory to preserve existing receipts")
rng=random.Random(9020)
streams={'random':[rng.random()*.08 for _ in range(32000)],
         'descending':[.08-i*.08/32000 for i in range(32000)],
         'coalesced':[(i%20)*.002 for i in range(32000)]}
results={}
for name,ages in streams.items():
    tracker=SensorFreshnessTracker();timings=[]
    for age in ages:
        start=time.perf_counter_ns();tracker.observe(age);summary=tracker.as_dict()
        timings.append(time.perf_counter_ns()-start)
    for label,q in [('p50',.5),('p95',.95),('p99',.99)]:
        assert summary[f'age_{label}_s']==_percentile(ages,q)
    assert tracker._ages==ages
    values=np.array(timings)
    np.save(out/f'age-{name}-ns.npy',values)
    results[name]={'n':len(values),'p99_us':float(np.quantile(values,.99)/1000),'max_us':float(max(values)/1000),'summary':summary}
(out/'age-stream-results.json').write_text(json.dumps(results,indent=2)+'\n')
print(json.dumps(results,indent=2))
