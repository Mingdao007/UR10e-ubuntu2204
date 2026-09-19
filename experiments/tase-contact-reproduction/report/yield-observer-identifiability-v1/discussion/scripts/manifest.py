"""Write data/manifest.json with SHA-256 of the receipts used and of every retained file here."""
import hashlib, json, glob, os, sys
root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
exp = os.path.abspath(os.path.join(root, '..', '..', '..'))
prior = json.load(open(os.path.join(exp, 'report/yield-normal-v2/discussion/data/manifest.json')))['source_receipts_sha256']
sha = lambda p: hashlib.sha256(open(p, 'rb').read()).hexdigest()
receipts = {}
for rel, expected in prior.items():
    actual = sha(os.path.join(exp, rel)); receipts[rel] = {'sha256': actual, 'matches_round2_manifest': actual == expected}
files = {}
for p in sorted(glob.glob(os.path.join(root, '**', '*'), recursive=True)):
    if os.path.isfile(p) and not p.endswith('manifest.json'): files[os.path.relpath(p, root)] = sha(p)
json.dump({'schema': 'yield-observer-identifiability-v1-discussion-round3-manifest',
           'role': 'advisory read-only postprocessing; development data; no closed-loop runs; no controller/test/config edits',
           'model': 'claude-fable-5-1 (xhigh)', 'base_commit': sys.argv[1] if len(sys.argv) > 1 else 'fa961c38',
           'source_receipts_sha256': receipts, 'files_sha256': files,
           'cache': '/tmp/ynv2 (per-tick npz from report/yield-normal-v2/discussion/scripts/extract_ticks.py; not retained)'},
          open(os.path.join(root, 'data', 'manifest.json'), 'w'), indent=1)
print(json.dumps({'receipts_ok': all(v['matches_round2_manifest'] for v in receipts.values()), 'n_files': len(files)}))
