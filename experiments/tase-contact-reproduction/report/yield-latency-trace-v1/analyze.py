"""Join recorded GC intervals to measured tick outliers without censoring."""
from pathlib import Path
import argparse,json
import numpy as np
parser=argparse.ArgumentParser(description=__doc__)
parser.add_argument('--input',type=Path,default=Path(__file__).resolve().parent)
parser.add_argument('--output',type=Path,required=True)
a=parser.parse_args();result={}
for method in ('SFC','DSFC','MSFC'):
    wall=np.load(a.input/f'{method}-tick-ns.npy');cpu=np.load(a.input/f'{method}-cpu-ns.npy')
    events=json.loads((a.input/f'{method}-gc.json').read_text());starts={};collections=[]
    for event in events:
        if event['phase']=='start':starts[event['generation']]=event
        else:
            start=starts.pop(event['generation'])
            collections.append({'generation':event['generation'],'start_tick':start['tick'],
                'end_tick':event['tick'],'duration_ms':(event['wall_ns']-start['wall_ns'])/1e6,
                'cpu_ms':(event['cpu_ns']-start['cpu_ns'])/1e6,'collected':event['collected']})
    assert not starts, 'incomplete collection trace'
    result[method]={'overruns':[{'tick':int(i),'wall_ms':wall[i]/1e6,'cpu_ms':cpu[i]/1e6,
        'gc':[e for e in collections if e['start_tick']==i or e['end_tick']==i]}
        for i in np.flatnonzero(wall>2000000)],
        'generation_2':[e for e in collections if e['generation']==2]}
with a.output.open('x') as f:json.dump(result,f,indent=2);f.write('\n')
