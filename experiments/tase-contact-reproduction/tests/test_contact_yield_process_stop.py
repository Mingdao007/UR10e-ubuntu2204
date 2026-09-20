"""Real OS signal delivery against a fixture process; no robot endpoints."""
import json
import subprocess
import sys
import time
from pathlib import Path
import contact_yield_live as entry


def test_stop_targets_bound_process_and_waits_for_its_receipt(tmp_path):
    code = '''
import json, os, signal, sys, time
from pathlib import Path
root = Path(sys.argv[2])
def stop(signum, frame):
    (root/'dispatch_receipt.json').write_text(json.dumps({
      'attempt_id':'signal-test', 'stop':{'stopped':True, 'test_fixture_only':True}}))
    sys.exit(0)
signal.signal(signal.SIGINT, stop)
(root/'ready').write_text('ready')
while True: time.sleep(.05)
'''
    process = subprocess.Popen([sys.executable, '-c', code, str(Path(entry.__file__).resolve()), str(tmp_path)])
    try:
        deadline=time.monotonic()+3
        while not (tmp_path/'ready').exists() and time.monotonic()<deadline:
            time.sleep(.01)
        assert (tmp_path/'ready').exists()
        (tmp_path/'owner.json').write_text(json.dumps({
            'pid':process.pid, 'process_start':entry._process_start(process.pid),
            'entry':str(Path(entry.__file__).resolve()),'run_dir':str(tmp_path.resolve()),
            'attempt_id':'signal-test','active':True}))
        receipt=entry.request_process_stop(tmp_path)
        assert receipt['stopped'] is True
        assert receipt['test_fixture_only'] is True
        assert process.wait(timeout=3)==0
    finally:
        if process.poll() is None:
            process.terminate()
            process.wait(timeout=3)
