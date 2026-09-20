"""Round-7 input/output receipt: current hashes of everything read this round, comparison with the round-6
hash list, raw-receipt manifest (caches re-hashed against files on disk), output hashes, HEAD at start and at
receipt time.  Read-only git queries only.  Writes data/round7-input-hashes.json and
data/round7-receipt-manifest.json; prints the receipt JSON.
"""
import json, hashlib, subprocess, os
from datetime import datetime, timezone
from pathlib import Path
import numpy as np

ROOT = Path(__file__).resolve().parents[4]; D = ROOT / 'report/yield-fair-tuning-v1/discussion-round7'
sha = lambda p: hashlib.sha256(Path(p).read_bytes()).hexdigest()
git = lambda *a: subprocess.run(['git', '-C', str(ROOT), *a], capture_output=True, text=True, check=True).stdout.strip()
HEAD_AT_START = '96a3c8bd03933c40be718af5516582fe1b6c6024'

report_inputs = sorted(set("""
report/yield-fair-tuning-v1/discussion/fable-round6.md
report/yield-fair-tuning-v1/discussion/main-adjudication.md
report/yield-fair-tuning-v1/discussion/fable-final.txt
report/yield-fair-tuning-v1/discussion/main-verification.json
report/yield-fair-tuning-v1/discussion/round6-input-receipt.json
report/yield-fair-tuning-v1/discussion/data/round6-analysis.json
report/yield-fair-tuning-v1/discussion/data/round6-input-hashes.json
report/yield-fair-tuning-v1/discussion/data/round6-receipt-manifest.json
report/yield-fair-tuning-v1/discussion/scripts/analyze_round6.py
report/yield-fair-tuning-v1/discussion/scripts/extract_rows_v6.py
report/yield-fair-tuning-v1/discussion/scripts/round6_receipt.py
report/yield-fair-tuning-v1/discussion/scripts/main_verify_round6.py
report/yield-fair-tuning-v1/README.md
report/yield-fair-tuning-v1/writer-task.md
report/yield-fair-tuning-v1/writer-receipt.json
report/yield-warm-memory-v1/README.md
report/yield-warm-memory-v1/check.py
report/yield-warm-memory-v1/result.json
report/yield-validation-reservation-v1/README.md
report/yield-validation-reservation-v1/protocol.json
report/yield-validation-reservation-v1/receipt.json
report/yield-fair-campaign-review-v1/README.md
report/yield-fair-campaign-review-v1/null-snapshots.json
report/yield-fair-campaign-review-v1/pair-bypass.json
report/yield-fair-campaign-review-v1/original-delivery-hashes.json
report/yield-fair-campaign-review-v1/main-original-tests.txt
report/yield-fair-campaign-review-v1/main-draft-tests.txt
report/yield-fair-campaign-review-v1/writer-receipt.json
report/yield-native-route-v1/notes.md
report/yield-native-route-v1/main-integration.md
report/yield-native-route-v1/main-tests.txt
report/yield-native-route-v1/main-binding-tests.txt
report/yield-native-route-v1/writer-receipt.json
report/yield-native-route-v1/writer-file-hashes.json
report/yield-native-route-v1/testlog.txt
report/yield-native-route-v1/main-test-path-error.txt
report/yield-cycle-boundary-v1/README.md
report/yield-cycle-boundary-v1/main-integration.md
report/yield-cycle-boundary-v1/main-tests.txt
report/yield-cycle-boundary-v1/main-additional-methods.txt
report/yield-cycle-boundary-v1/protocol-binding.json
report/yield-cycle-boundary-v1/writer-receipt.json
report/yield-cycle-boundary-v1/focused-tests.txt
report/contact-yield-recovery-20260920/advancement.json
report/yield-frozen-pulse-v1/discussion/scripts/extract_ticks_v5.py
config/yield_normal_observer_v3.json
config/yield_fair_tuning_v1.json
""".split()))
tool_inputs = sorted("""
tools/contact_yield_runner.py
tools/contact_yield_replay.py
tools/contact_yield_controller.py
tools/contact_yield_laws.py
tools/contact_laws.py
tools/contact_yield_simulator.py
tools/contact_yield_metrics.py
tools/contact_yield_protocol.py
tools/contact_yield_normal.py
build/contact-six-laws/contact-laws-733c419baa0884b8859255d4/native_seven_laws.cpp
""".split())
raw_inputs = ['runs/yield-warm-memory-v1/native-history.json.gz']

r6 = json.loads((ROOT / 'report/yield-fair-tuning-v1/discussion/data/round6-input-hashes.json').read_text())
r6map = {e['path']: e['sha256'] for e in r6['report_inputs'] + r6['tool_inputs']}
def entry(p):
    cur = sha(ROOT / p); e = {'path': p, 'sha256': cur}
    if p in r6map:
        e['round6_sha256'] = r6map[p]; e['status_vs_round6'] = 'unchanged' if r6map[p] == cur else 'changed_since_round6'
    else:
        e['status_vs_round6'] = 'not_in_round6_list'
    return e
rep = [entry(p) for p in report_inputs if (ROOT / p).exists()]
missing = [p for p in report_inputs + tool_inputs if not (ROOT / p).exists()]
tools = []
for p in tool_inputs:
    if not (ROOT / p).exists(): continue
    e = entry(p)
    if p.startswith('build/'):
        e['tracked_by_git'] = False; e['note'] = 'native source in the build tree (fingerprint 733c419b); not a tracked file'
    else:
        try:
            at_start = hashlib.sha256(subprocess.run(['git', '-C', str(ROOT), 'show', f'{HEAD_AT_START}:./{p}'], capture_output=True, check=True).stdout).hexdigest()
            e['sha256_at_head_96a3c8bd'] = at_start; e['changed_between_96a3c8bd_and_worktree'] = at_start != e['sha256']
        except subprocess.CalledProcessError:
            e['sha256_at_head_96a3c8bd'] = None; e['changed_between_96a3c8bd_and_worktree'] = 'file absent at 96a3c8bd'
    tools.append(e)
raws = [{'path': p, 'sha256': sha(ROOT / p), 'bytes': (ROOT / p).stat().st_size} for p in raw_inputs]
hashes = {'report_inputs': rep, 'tool_inputs': tools, 'raw_artifacts': raws, 'missing_inputs': missing,
          'summary': {'report_inputs': len(rep), 'tools': len(tools), 'changed_since_round6': [e['path'] for e in rep + tools if e['status_vs_round6'] == 'changed_since_round6'],
                      'unchanged_since_round6': sum(e['status_vs_round6'] == 'unchanged' for e in rep + tools), 'not_in_round6_list': sum(e['status_vs_round6'] == 'not_in_round6_list' for e in rep + tools)}}
(D / 'data/round7-input-hashes.json').write_text(json.dumps(hashes, indent=1))

# ---- raw receipts used through the tick caches: sha stored in the npz, current file sha, sha recorded by the owning study
expected = {}
for jf in ['report/yield-observer-transfer-v1/results.json', 'report/yield-normal-v3/results.json', 'report/yield-frozen-pulse-v1/results.json', 'report/yield-frozen-pulse-v1/protocol.json',
           'report/yield-frozen-transfer-v1/results.json', 'report/yield-frozen-transfer-v1/protocol.json', 'report/yield-frozen-memory-v1/results.json', 'report/yield-observer-prior-v1/results.json']:
    if not (ROOT / jf).exists(): continue
    def walk(x):
        if isinstance(x, dict):
            if 'path' in x and 'sha256' in x and isinstance(x['path'], str) and x['path'].endswith('.json.gz'):
                expected.setdefault(os.path.basename(os.path.dirname(x['path'])) + '/' + os.path.basename(x['path']), set()).add(x['sha256'])
            for v in x.values(): walk(v)
        elif isinstance(x, list):
            for v in x: walk(v)
    walk(json.loads((ROOT / jf).read_text()))
yfp5 = {'nom-SFC': 'runs/yield-frozen-transfer-v1/SFC-nominal.json.gz', 'nom-DSFC': 'runs/yield-observer-prior-v1/DSFC-stiff_low_mu-approach.json.gz', 'nom-MSFC': 'runs/yield-frozen-transfer-v1/MSFC-nominal.json.gz', 'nom-MSFCid': 'runs/yield-frozen-memory-v1/MSFC-nominal.json.gz',
        'tan-SFC': 'runs/yield-frozen-transfer-v1/SFC-sustained_release_tangent.json.gz', 'tan-DSFC': 'runs/yield-normal-v3/frozen-tangent-hold.json.gz', 'tan-MSFC': 'runs/yield-frozen-transfer-v1/MSFC-sustained_release_tangent.json.gz', 'tan-MSFCid': 'runs/yield-frozen-memory-v1/MSFC-sustained_release_tangent.json.gz',
        'pulse-SFC': 'runs/yield-frozen-pulse-v1/SFC/SFC-short_pulse_oblique.json.gz', 'pulse-DSFC': 'runs/yield-frozen-pulse-v1/DSFC/DSFC-short_pulse_oblique.json.gz', 'pulse-MSFC': 'runs/yield-frozen-pulse-v1/MSFC/MSFC-short_pulse_oblique.json.gz', 'pulse-MSFCid': 'runs/yield-frozen-pulse-v1/MSFC-identity/MSFC-short_pulse_oblique.json.gz'}
yfp6 = {'no3-nom-SFC': 'runs/yield-observer-transfer-v1/SFC-nominal.json.gz', 'no3-nrm-SFC': 'runs/yield-observer-transfer-v1/SFC-sustained_release_normal.json.gz', 'no3-tan-SFC': 'runs/yield-observer-transfer-v1/SFC-sustained_release_tangent.json.gz',
        'no3-nom-DSFC': 'runs/yield-normal-v3/combined-mild-approach.json.gz', 'no3-nrm-DSFC': 'runs/yield-normal-v3/combined-normal-hold.json.gz', 'no3-tan-DSFC': 'runs/yield-normal-v3/combined-tangent-hold.json.gz',
        'no3-nom-MSFC': 'runs/yield-observer-transfer-v1/MSFC-nominal.json.gz', 'no3-nrm-MSFC': 'runs/yield-observer-transfer-v1/MSFC-sustained_release_normal.json.gz', 'no3-tan-MSFC': 'runs/yield-observer-transfer-v1/MSFC-sustained_release_tangent.json.gz',
        'no3-strong-DSFC': 'runs/yield-normal-v3/combined-strong-approach.json.gz'}
yfp7 = {'no3-nom-MSFC': 'runs/yield-observer-transfer-v1/MSFC-nominal.json.gz', 'no3-tan-MSFC': 'runs/yield-observer-transfer-v1/MSFC-sustained_release_tangent.json.gz', 'no3-nrm-MSFC': 'runs/yield-observer-transfer-v1/MSFC-sustained_release_normal.json.gz'}
manifest = []
for cache, table in (('/tmp/yfp5', yfp5), ('/tmp/yfp6', yfp6), ('/tmp/yfp7', yfp7)):
    for key, rel in table.items():
        f = Path(f'{cache}/{key}.npz')
        if not f.exists():
            manifest.append({'cache_key': key, 'cache': cache, 'path': rel, 'sha256_now': sha(ROOT / rel), 'sha256_in_cache': None, 'cache_matches_file': 'cache file absent'}); continue
        npz_sha = str(np.load(f)['sha256']); cur = sha(ROOT / rel)
        k = os.path.basename(os.path.dirname(rel)) + '/' + os.path.basename(rel)
        exp = sorted(expected.get(k, []))
        manifest.append({'cache_key': key, 'cache': cache, 'path': rel, 'sha256_now': cur, 'sha256_in_cache': npz_sha, 'sha256_recorded_by_study': exp,
                         'cache_matches_file': npz_sha == cur, 'study_record_matches_file': (cur in exp) if exp else 'no study record scanned'})
(D / 'data/round7-receipt-manifest.json').write_text(json.dumps({'receipts': manifest, 'count': len(manifest), 'all_cache_match': all(m['cache_matches_file'] is True for m in manifest),
    'all_study_records_match': all(m['study_record_matches_file'] is True for m in manifest if m.get('study_record_matches_file') not in ('no study record scanned', None)),
    'without_scanned_study_record': [m['path'] for m in manifest if m.get('study_record_matches_file') == 'no study record scanned'],
    'caches': {'/tmp/yfp5': 'round-5 extract_ticks_v5.py (rows + records incl. law22)', '/tmp/yfp6': 'round-6 extract_rows_v6.py (rows only)', '/tmp/yfp7': 'this round, extract_ticks_v5.py on three NO-v3 MSFC receipts (rows + records incl. law22)'}}, indent=1))

outputs = [str(p.relative_to(ROOT)) for p in sorted(D.rglob('*')) if p.is_file() and p.name != 'round7-input-receipt.json']
receipt = {
    'schema': 'fable-advisory-round-receipt-v1', 'round': 7, 'study': 'yield-fair-tuning-v1', 'directory': 'report/yield-fair-tuning-v1/discussion-round7/',
    'role': 'advisory, non-blocking; no live authority; not formal acceptance; first of at most two iterations',
    'model': 'claude-fable-5-1', 'requested_effort': 'xhigh', 'effort_evidence': 'launch parameter only, not independent server attestation',
    'head_at_session_start': HEAD_AT_START, 'head_at_receipt': git('rev-parse', 'HEAD'), 'head_log_since_start': git('log', '--oneline', f'{HEAD_AT_START}..HEAD').splitlines(),
    'worktree_status_at_receipt': git('status', '--short').splitlines(), 'worktree_clean_at_session_start': True,
    'live_grok_worktree_not_read': '/home/andy/.codex-worktrees/yield-fair-campaign-20260920 (active repair) was not opened; tools/yield_fair_campaign.py, tools/yield_fair_selection.py and config/yield_fair_campaign_v1.json are absent at 96a3c8bd; tools/yield_contact_ledger.py is tracked at 96a3c8bd (pre-draft, commit aeb9b61d) and was not read',
    'files_appearing_in_worktree_during_session_not_read_or_touched': [l for l in git('status', '--short').splitlines() if 'discussion-round7' not in l],
    'written_at_utc': datetime.now(timezone.utc).isoformat(),
    'write_scope': 'report/yield-fair-tuning-v1/discussion-round7/ only',
    'not_done': ['no runtime/test/config/other-report edits', 'no git write operations (read-only rev-parse/log/status/show only)', 'no hardware, transport or network access', 'no closed-loop runs, campaigns, holdout or validation launches', 'no native law stepped', 'no subagents', 'no external literature claims', 'no formal tuning, validation or holdout unit consumed'],
    'inputs': {'report_and_config': len(rep), 'tools_and_native_source': len(tools), 'raw_artifacts': len(raws), 'changed_since_round6': hashes['summary']['changed_since_round6'], 'file': 'report/yield-fair-tuning-v1/discussion-round7/data/round7-input-hashes.json'},
    'raw_receipts': {'count': len(manifest), 'all_match': all(m['cache_matches_file'] is True for m in manifest), 'manifest': 'report/yield-fair-tuning-v1/discussion-round7/data/round7-receipt-manifest.json'},
    'tick_caches': {'/tmp/yfp5': 'round-5 cache, not retained', '/tmp/yfp6': 'round-6 cache, not retained', '/tmp/yfp7': 'this round, three NO-v3 MSFC receipts through extract_ticks_v5.py, not retained'},
    'numbers_recomputed_vs_reported': 'data/round7-analysis.json: J3 recomputed on ten development pairs equals data/round6-analysis.json to 0.0; warm-memory artifact and check.py hashes match result.json; warm-x/cold command difference at 2 s reproduced (0.0024473 m/s) and shown equal to g*w(0); steady-state metric formula matches the tangent-hold plateau to 6.6e-7',
    'outputs': [{'path': p, 'sha256': sha(ROOT / p), 'bytes': (ROOT / p).stat().st_size} for p in outputs],
}
print(json.dumps(receipt, indent=1))
