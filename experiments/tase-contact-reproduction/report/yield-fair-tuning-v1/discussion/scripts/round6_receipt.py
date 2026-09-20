"""Round-6 input/output receipt: current hashes of everything read, comparison with the round-5 hash
list, raw-receipt manifest, output hashes, HEAD at start and at receipt time.  Read-only git queries only.
Writes data/round6-input-hashes.json and data/round6-receipt-manifest.json; prints the receipt JSON.
"""
import json, hashlib, subprocess, glob, os
from datetime import datetime, timezone
from pathlib import Path
import numpy as np

ROOT = Path(__file__).resolve().parents[4]; D = ROOT / 'report/yield-fair-tuning-v1/discussion'
sha = lambda p: hashlib.sha256(Path(p).read_bytes()).hexdigest()
git = lambda *a: subprocess.run(['git', '-C', str(ROOT), *a], capture_output=True, text=True, check=True).stdout.strip()
HEAD_AT_START = '6f041ef6'

report_inputs = sorted(set("""
report/yield-frozen-pulse-v1/discussion/fable-round5.md
report/yield-frozen-pulse-v1/discussion/main-adjudication.md
report/yield-frozen-pulse-v1/discussion/main-verification.json
report/yield-frozen-pulse-v1/discussion/round5-input-receipt.json
report/yield-frozen-pulse-v1/discussion/data/round5-input-hashes.json
report/yield-frozen-pulse-v1/discussion/data/main-envelope-check.json
report/yield-frozen-pulse-v1/discussion/scripts/main_envelope_check.py
report/yield-frozen-pulse-v1/discussion/scripts/extract_ticks_v5.py
report/yield-frozen-pulse-v1/discussion/scripts/analyze_round5.py
report/yield-frozen-pulse-v1/README.md
report/yield-frozen-pulse-v1/protocol.json
report/yield-frozen-pulse-v1/results.json
report/yield-frozen-pulse-v1/parallel_run_manifest.json
report/yield-frozen-pulse-v1/refinement/README.md
report/yield-frozen-pulse-v1/refinement/results.json
report/yield-frozen-pulse-v1/refinement/parallel_run_manifest.json
report/yield-frozen-pulse-v1/refinement/start.json
report/yield-coefficient-equivalence-v1/README.md
report/yield-coefficient-equivalence-v1/check.py
report/yield-coefficient-equivalence-v1/result.json
report/yield-coefficient-equivalence-v1/tuner-bounds.json
report/yield-coefficient-equivalence-v1/tuner_bounds_check.py
report/yield-cycle-boundary-v1/README.md
report/yield-normal-v3/README.md
report/yield-normal-v3/results.md
report/yield-normal-v3/results.json
report/yield-normal-v3/protocol.md
report/yield-normal-v3/parallel_run_manifest.json
report/yield-normal-v3/refinement.json
report/yield-normal-v3/refinement-manifest.json
report/yield-normal-v3/discussion/main-adjudication.md
report/yield-observer-transfer-v1/README.md
report/yield-observer-transfer-v1/results.md
report/yield-observer-transfer-v1/results.json
report/yield-observer-transfer-v1/protocol.json
report/yield-observer-transfer-v1/start.json
report/yield-observer-transfer-v1/manifest.json
report/contact-yield-recovery-20260920/README.md
report/contact-yield-recovery-20260920/results.md
report/contact-yield-recovery-20260920/advancement.json
report/contact-yield-recovery-20260920/full-refinement.md
report/contact-yield-recovery-20260920/common-fix.md
report/yield-discretization-v1/README.md
report/yield-discretization-v1/results.md
report/yield-discretization-v1/results.json
report/yield-gain-memory-v1/README.md
report/yield-gain-memory-v1/results.md
report/yield-gain-memory-v1/discussion/main-adjudication.md
report/yield-transfer-v1/README.md
report/yield-transfer-v1/results.md
report/yield-full-writer-v1/README.md
report/yield-compute-budget-v1/README.md
report/yield-surface-excitation-v1/README.md
report/yield-surface-excitation-v1/results.md
report/yield-provider-admission-v2/README.md
report/yield-platform-preflight-v2/README.md
report/yield-frozen-memory-v1/README.md
report/yield-frozen-memory-v1/results.json
report/yield-frozen-transfer-v1/protocol.json
report/yield-frozen-transfer-v1/README.md
report/yield-frozen-transfer-v1/results.json
report/yield-observer-identifiability-v1/discussion/main-adjudication.md
config/yield_fair_tuning_v1.json
config/contact_yield_candidates/manifest.json
""".split()))
tool_inputs = sorted("""
tools/contact_yield_protocol.py
tools/contact_yield_metrics.py
tools/contact_yield_runner.py
tools/contact_yield_simulator.py
tools/contact_benchmark_tuner.py
tools/contact_benchmark_protocol.py
tools/contact_yield_normal.py
tools/yield_contact_tuner.py
tests/test_yield_contact_tuner.py
""".split())

r5 = json.loads((ROOT / 'report/yield-frozen-pulse-v1/discussion/data/round5-input-hashes.json').read_text())
r5map = {e['path']: e['sha256'] for e in r5['report_inputs'] + r5['tool_inputs']}
def entry(p):
    cur = sha(ROOT / p); e = {'path': p, 'sha256': cur}
    if p in r5map:
        e['round5_sha256'] = r5map[p]; e['status_vs_round5'] = 'unchanged' if r5map[p] == cur else 'changed_since_round5'
    else:
        e['status_vs_round5'] = 'not_in_round5_list'
    return e
rep = [entry(p) for p in report_inputs if (ROOT / p).exists()]
missing = [p for p in report_inputs if not (ROOT / p).exists()]
tools = []
for p in tool_inputs:
    e = entry(p)
    try:
        at_start = hashlib.sha256(subprocess.run(['git', '-C', str(ROOT), 'show', f'{HEAD_AT_START}:./{p}'], capture_output=True, check=True).stdout).hexdigest()
        e['sha256_at_head_6f041ef6'] = at_start; e['changed_between_6f041ef6_and_worktree'] = at_start != e['sha256']
    except subprocess.CalledProcessError:
        e['sha256_at_head_6f041ef6'] = None; e['changed_between_6f041ef6_and_worktree'] = 'file absent at 6f041ef6'
    tools.append(e)
hashes = {'report_inputs': rep, 'tool_inputs': tools, 'missing_inputs': missing,
          'summary': {'report_inputs': len(rep), 'tools': len(tools), 'changed_since_round5': [e['path'] for e in rep + tools if e['status_vs_round5'] == 'changed_since_round5'],
                      'unchanged_since_round5': sum(e['status_vs_round5'] == 'unchanged' for e in rep + tools), 'not_in_round5_list': sum(e['status_vs_round5'] == 'not_in_round5_list' for e in rep + tools)}}
(D / 'data/round6-input-hashes.json').write_text(json.dumps(hashes, indent=1))

# ---- raw receipts: npz-stored sha, current file sha, sha recorded by the owning study
expected = {}
for jf in ['report/yield-observer-transfer-v1/results.json', 'report/yield-normal-v3/results.json', 'report/yield-frozen-pulse-v1/results.json', 'report/yield-frozen-pulse-v1/protocol.json',
           'report/yield-frozen-pulse-v1/refinement/results.json', 'report/yield-frozen-transfer-v1/results.json', 'report/yield-frozen-transfer-v1/protocol.json', 'report/yield-frozen-memory-v1/results.json']:
    def walk(x):
        if isinstance(x, dict):
            if 'path' in x and 'sha256' in x and isinstance(x['path'], str) and x['path'].endswith('.json.gz'):
                expected.setdefault(os.path.basename(os.path.dirname(x['path'])) + '/' + os.path.basename(x['path']), set()).add(x['sha256'])
            for v in x.values(): walk(v)
        elif isinstance(x, list):
            for v in x: walk(v)
    walk(json.loads((ROOT / jf).read_text()))
yfp6 = {'no3-nom-SFC': 'runs/yield-observer-transfer-v1/SFC-nominal.json.gz', 'no3-nrm-SFC': 'runs/yield-observer-transfer-v1/SFC-sustained_release_normal.json.gz', 'no3-tan-SFC': 'runs/yield-observer-transfer-v1/SFC-sustained_release_tangent.json.gz',
        'no3-nom-DSFC': 'runs/yield-normal-v3/combined-mild-approach.json.gz', 'no3-nrm-DSFC': 'runs/yield-normal-v3/combined-normal-hold.json.gz', 'no3-tan-DSFC': 'runs/yield-normal-v3/combined-tangent-hold.json.gz',
        'no3-nom-MSFC': 'runs/yield-observer-transfer-v1/MSFC-nominal.json.gz', 'no3-nrm-MSFC': 'runs/yield-observer-transfer-v1/MSFC-sustained_release_normal.json.gz', 'no3-tan-MSFC': 'runs/yield-observer-transfer-v1/MSFC-sustained_release_tangent.json.gz',
        'leg-nom-SFC': 'runs/yield-offset-ablation/SFC-nominal.json.gz', 'leg-nom-DSFC': 'runs/yield-offset-ablation/DSFC-nominal.json.gz', 'leg-nom-MSFC': 'runs/yield-gain-memory-v1/MSFC-GM-v1-g50-on-nominal-plant8.json.gz',
        'no3-along-DSFC': 'runs/yield-normal-v3/combined-mild-along.json.gz', 'no3-across-DSFC': 'runs/yield-normal-v3/combined-mild-across.json.gz', 'no3-strong-DSFC': 'runs/yield-normal-v3/combined-strong-approach.json.gz'}
yfp5 = {'nom-SFC': 'runs/yield-frozen-transfer-v1/SFC-nominal.json.gz', 'nom-DSFC': 'runs/yield-observer-prior-v1/DSFC-stiff_low_mu-approach.json.gz', 'nom-MSFC': 'runs/yield-frozen-transfer-v1/MSFC-nominal.json.gz', 'nom-MSFCid': 'runs/yield-frozen-memory-v1/MSFC-nominal.json.gz',
        'tan-SFC': 'runs/yield-frozen-transfer-v1/SFC-sustained_release_tangent.json.gz', 'tan-DSFC': 'runs/yield-normal-v3/frozen-tangent-hold.json.gz', 'tan-MSFC': 'runs/yield-frozen-transfer-v1/MSFC-sustained_release_tangent.json.gz', 'tan-MSFCid': 'runs/yield-frozen-memory-v1/MSFC-sustained_release_tangent.json.gz',
        'pulse-SFC': 'runs/yield-frozen-pulse-v1/SFC/SFC-short_pulse_oblique.json.gz', 'pulse-DSFC': 'runs/yield-frozen-pulse-v1/DSFC/DSFC-short_pulse_oblique.json.gz', 'pulse-MSFC': 'runs/yield-frozen-pulse-v1/MSFC/MSFC-short_pulse_oblique.json.gz', 'pulse-MSFCid': 'runs/yield-frozen-pulse-v1/MSFC-identity/MSFC-short_pulse_oblique.json.gz',
        'fine-pulse-MSFC': 'runs/yield-frozen-pulse-v1-check/MSFC/short_pulse_oblique.json.gz', 'fine-pulse-MSFCid': 'runs/yield-frozen-pulse-v1-check/MSFC-identity/short_pulse_oblique.json.gz', 'fine-nom-MSFC': 'runs/yield-frozen-pulse-v1-check/MSFC/nominal.json.gz', 'fine-nom-MSFCid': 'runs/yield-frozen-pulse-v1-check/MSFC-identity/nominal.json.gz'}
manifest = []
for cache, table in (('/tmp/yfp6', yfp6), ('/tmp/yfp5', yfp5)):
    for key, rel in table.items():
        npz_sha = str(np.load(f'{cache}/{key}.npz')['sha256']); cur = sha(ROOT / rel)
        k = os.path.basename(os.path.dirname(rel)) + '/' + os.path.basename(rel)
        exp = sorted(expected.get(k, []))
        manifest.append({'cache_key': key, 'cache': cache, 'path': rel, 'sha256_now': cur, 'sha256_in_cache': npz_sha, 'sha256_recorded_by_study': exp,
                         'cache_matches_file': npz_sha == cur, 'study_record_matches_file': (cur in exp) if exp else 'no study record scanned'})
(D / 'data/round6-receipt-manifest.json').write_text(json.dumps({'receipts': manifest, 'count': len(manifest), 'all_cache_match': all(m['cache_matches_file'] for m in manifest),
    'all_study_records_match': all(m['study_record_matches_file'] is True for m in manifest if m['study_record_matches_file'] != 'no study record scanned'),
    'without_scanned_study_record': [m['path'] for m in manifest if m['study_record_matches_file'] == 'no study record scanned']}, indent=1))

outputs = [str(p.relative_to(ROOT)) for p in sorted(D.rglob('*')) if p.is_file() and p.name != 'round6-input-receipt.json']
receipt = {
    'schema': 'fable-advisory-round-receipt-v1', 'round': 6, 'study': 'yield-fair-tuning-v1',
    'role': 'advisory, non-blocking; no live authority; not formal acceptance; first of at most two iterations',
    'model': 'claude-fable-5-1', 'requested_effort': 'xhigh', 'effort_evidence': 'launch parameter only, not independent server attestation',
    'head_at_session_start': HEAD_AT_START, 'head_at_receipt': git('rev-parse', 'HEAD'), 'head_log_since_start': git('log', '--oneline', f'{HEAD_AT_START}..HEAD').splitlines(),
    'worktree_status_at_receipt': git('status', '--short').splitlines(),
    'dirty_at_session_start': ['tests/test_contact_yield_evidence.py', 'tests/test_contact_yield_full_writer.py', 'tests/test_contact_yield_provider.py', 'tests/test_contact_yield_writer_loop.py', 'tests/yield_full_writer_offline.py',
                               'tools/contact_yield_protocol.py', 'tools/measure_yield_full_writer.py', 'tools/step5d_autotune_v4_r004_live_writer.py', 'tools/yield_contact_evidence.py', 'tools/yield_contact_provider.py', 'tools/yield_contact_runtime.py', 'report/yield-cycle-boundary-v1/ (untracked)'],
    'dirty_files_handling': 'contact_yield_protocol.py was read at 6f041ef6 via read-only git show, not from the dirty worktree; the other dirty files were not read; main committed them during the session',
    'written_at_utc': datetime.now(timezone.utc).isoformat(),
    'write_scope': 'report/yield-fair-tuning-v1/discussion/ only',
    'not_done': ['no runtime/test/config/other-report edits', 'no git write operations (read-only rev-parse/log/status/show/diff only)', 'no hardware, transport or network access', 'no closed-loop runs, campaigns or holdout launches', 'no subagents', 'no external literature claims', 'no formal tuning or holdout unit consumed'],
    'inputs': {'report_and_config': len(rep), 'tools_and_tests': len(tools), 'changed_since_round5': hashes['summary']['changed_since_round5'], 'file': 'report/yield-fair-tuning-v1/discussion/data/round6-input-hashes.json'},
    'raw_receipts': {'count': len(manifest), 'all_match': all(m['cache_matches_file'] for m in manifest), 'manifest': 'report/yield-fair-tuning-v1/discussion/data/round6-receipt-manifest.json'},
    'tick_caches': {'/tmp/yfp6': 'rows-only extracts, scripts/extract_rows_v6.py, not retained', '/tmp/yfp5': 'round-5 cache, scripts in report/yield-frozen-pulse-v1/discussion, not retained'},
    'numbers_recomputed_vs_reported': 'nominal_recomputed in data/round6-analysis.json: max |stored - recomputed| 8.7e-19 over nine nominals; compare_pair recovery reproduced exactly on all sixteen pairs',
    'outputs': [{'path': p, 'sha256': sha(ROOT / p), 'bytes': (ROOT / p).stat().st_size} for p in outputs],
}
print(json.dumps(receipt, indent=1))
