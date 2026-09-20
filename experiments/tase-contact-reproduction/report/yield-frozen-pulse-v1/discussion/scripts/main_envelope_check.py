"""Correct only the physical envelope window in the retained advisory analysis.

Uses the exact advisory script and cached receipt-derived arrays. Keeps its
original output intact. The window is 0.5 s (inclusive sample endpoint), rather
than 250 samples at every dt. No simulation, controller, or recovery change.
"""
from pathlib import Path
import contextlib
import io
import json
import numpy as np
script=Path(__file__).with_name('analyze_round5.py')
source=script.read_text()
old='env = envelope(np.abs(dl[post]))'
new='env = envelope(np.abs(dl[post]), w=int(round(0.5 / float(P["dt"]))))'
assert source.count(old)==1
output=io.StringIO()
with contextlib.redirect_stdout(output):
    exec(compile(source.replace(old,new),str(script),'exec'),{'__name__':'__main__'})
print(json.dumps({'physical_window_s':0.5,'source':str(script),'scope':'supplementary descriptor correction only','analysis':json.loads(output.getvalue())},indent=2))
