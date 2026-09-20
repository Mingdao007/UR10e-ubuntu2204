"""Round-10 input/output receipt: hashes of every file read or written, cache provenance, binding checks.

Read-only except for stdout.  No run, no ledger access.
"""
import csv, hashlib, json, os, subprocess, sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
DIR = ROOT / 'report/yield-fair-training-v1/discussion-round10'
CACHE = Path('/tmp/yfp10')
HEAD_AT_START = '4e5b36bf5763dab2b6f28471d33e46eb509beeda'


def sha(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(1 << 20), b''):
            h.update(chunk)
    return h.hexdigest()


def git(*args):
    return subprocess.run(['git', *args], cwd=ROOT, capture_output=True, text=True, check=True).stdout.strip()


def entry(rel):
    p = ROOT / rel
    return {'path': rel, 'sha256': sha(p), 'bytes': p.stat().st_size}


INPUTS = [
    'report/yield-training-report-final-v1/report.md', 'report/yield-training-report-final-v1/report.json',
    'report/yield-training-report-final-v1/members.csv', 'report/yield-training-report-final-v1/slots.csv',
    'report/yield-training-report-final-v1/initial_triples.csv', 'report/yield-training-report-final-v1/best_so_far.csv',
    'report/yield-training-report-final-v1/artifact_digest_manifest.csv', 'report/yield-training-report-final-v1/main-interpretation.md',
    'report/yield-fair-training-v1/launcher-result.json', 'report/yield-fair-training-v1/README.md',
    'report/yield-fair-training-v1/sfc-initial-stop.md', 'report/yield-fair-training-v1/launch-binding.json',
    'report/yield-fair-training-v1/dsfc-first-repeat-verification.json', 'report/yield-fair-training-v1/round9-attempt1-status.json',
    'report/yield-fair-training-v1/round8-input-snapshot.json',
    'report/yield-round8-main-review-v1/main-adjudication.md', 'report/yield-round8-main-review-v1/README.md',
    'report/yield-round8-main-review-v1/cache-verification.json',
    'report/yield-fair-training-v1/discussion-round8/fable-round8.md', 'report/yield-fair-training-v1/discussion-round8/round8-input-receipt.json',
    'report/yield-fair-training-v1/discussion-round8/data/round8-analysis.json', 'report/yield-fair-training-v1/discussion-round8/data/round8-mechanics.json',
    'report/yield-fair-training-v1/discussion-round8/scripts/extract_rows_v8.py', 'report/yield-fair-training-v1/discussion-round8/scripts/analyze_round8.py',
    'report/yield-validation-reservation-v1/protocol.json', 'report/yield-validation-reservation-v1/README.md',
    'report/yield-validation-native-smoke-v1/README.md',
    'config/yield_fair_campaign_v1.json', 'config/yield_fair_tuning_v1.json', 'config/yield_normal_observer_v3.json',
    'config/contact_yield_protocol.json',
    'tools/yield_fair_selection.py', 'tools/contact_yield_metrics.py', 'tools/contact_yield_protocol.py',
    'tools/contact_yield_laws.py', 'tools/yield_contact_tuner.py', 'tools/yield_validation_selection.py',
    'tools/yield_validation_runner.py', 'tools/build_contact_laws.py',
    'report/yield-mechanism-development-v1/README.md', 'report/yield-mechanism-development-v1/frozen-campaign.json',
    'report/yield-mechanism-development-v1/main-review.md',
]


def main():
    head = git('rev-parse', 'HEAD')
    lb = json.loads((ROOT / 'report/yield-fair-training-v1/launch-binding.json').read_text())['campaign_protocol']
    binding = []
    for name, h in lb['source_hashes'].items():
        binding.append({'path': f'tools/{name}', 'expected': h, 'match': sha(ROOT / 'tools' / name) == h})
    for name, h in lb['execution_identities']['scientific_files'].items():
        binding.append({'path': name, 'expected': h, 'match': sha(ROOT / name) == h})
    for name, key in (('config/yield_fair_campaign_v1.json', 'campaign_config_sha256'), ('config/yield_normal_observer_v3.json', 'observer_config_sha256'), ('config/yield_fair_tuning_v1.json', 'tuner_config_sha256')):
        binding.append({'path': name, 'expected': lb[key], 'match': sha(ROOT / name) == lb[key]})
    manifest = {r['attempt_id']: r for r in csv.DictReader((ROOT / 'report/yield-training-report-final-v1/artifact_digest_manifest.csv').open())}
    cache = []
    for meta_path in sorted(CACHE.glob('*.meta.json')):
        aid = meta_path.name[:-len('.meta.json')]
        meta = json.loads(meta_path.read_text())
        npz = CACHE / f'{aid}.npz'
        cache.append({'id': aid, 'artifact_path': meta['_extract']['artifact_path'], 'artifact_sha256_verified_at_extract': meta['_extract']['artifact_sha256'],
                      'artifact_sha256_final_manifest': manifest[aid]['artifact_sha256'], 'match': meta['_extract']['artifact_sha256'] == manifest[aid]['artifact_sha256'],
                      'artifact_bytes': meta['_extract']['bytes'], 'npz_sha256': sha(npz), 'meta_sha256': sha(meta_path), 'extract_seconds': meta['_extract']['seconds']})
    outputs = [entry(str(p.relative_to(ROOT))) for p in sorted(DIR.rglob('*')) if p.is_file() and p.name != 'round10-input-receipt.json']
    r8 = json.loads((ROOT / 'report/yield-fair-training-v1/round8-input-snapshot.json').read_text())
    r8_match = all(a['evidence']['artifact_sha256'] == manifest[a['id']]['artifact_sha256'] for a in r8['attempts'])
    analysis = json.loads((DIR / 'data/round10-analysis.json').read_text())
    rec = {
        'schema': 'fable-advisory-round-receipt-v1', 'round': 10, 'study': 'yield-fair-training-v1',
        'directory': 'report/yield-fair-training-v1/discussion-round10/',
        'role': 'advisory, non-blocking; no live authority; not formal acceptance; first of at most two iterations',
        'model': 'claude-fable-5-1', 'requested_effort': 'xhigh', 'effort_evidence': 'launch parameter only, not independent server attestation',
        'head_at_start': HEAD_AT_START, 'head_at_receipt': head,
        'head_log_since_start': git('log', '--oneline', f'{HEAD_AT_START}..HEAD').splitlines(),
        'inputs_changed_by_commits_since_start': git('diff', '--name-only', f'{HEAD_AT_START}..HEAD', '--', *[str(ROOT / p) for p in INPUTS]).splitlines(),
        'worktree_status_at_receipt': git('status', '--short').splitlines(),
        'written_at_utc': datetime.now(timezone.utc).isoformat(),
        'write_scope': 'report/yield-fair-training-v1/discussion-round10/ only; scratch /tmp/yfp10 (not retained as evidence)',
        'round9_note': 'round9-attempt1-status.json records exit 143 without advisory delivery; not treated as completed; no cause inferred',
        'not_done': ['no runtime/test/config/controller/other-report edits', 'no Git writes', 'no subagents', 'no network', 'no devices or hardware',
                     'no simulation or native execution', 'no tuning or validation', 'no SQLite ledger or progress.json read', 'no runs/yield-mechanism-development-v1 content read',
                     'no lane or writer receipt read', 'no active writer worktree read', 'no artifact outside the final manifest read',
                     'no raw artifact modified'],
        'raw_scope': {'manifest': 'report/yield-training-report-final-v1/artifact_digest_manifest.csv', 'members_listed': len(manifest), 'members_read': len(cache),
                      'members_read_ids': [c['id'] for c in cache], 'all_hashes_match_final_manifest': all(c['match'] for c in cache),
                      'one_at_a_time': True, 'hash_before_parse': True},
        'round8_snapshot_artifacts_identical_in_final_manifest': r8_match,
        'binding_checks': {'count': len(binding), 'all_match': all(b['match'] for b in binding), 'checks': binding},
        'reproduction_checks': {k: analysis[k] for k in ('all_summaries_equal_artifact_metrics', 'all_summaries_equal_members_csv', 'all_J_equal_registered', 'max_J_abs_diff',
                                                         'all_recovery_equal_registered', 'all_pre_onset_rows_bitwise_equal', 'all_whole_episode_peaks_equal_nominal')},
        'inputs_read': [entry(p) for p in INPUTS],
        'inputs_read_via_git_show_at_start_head': ['tools/yield_contact_tuner.py', 'tools/contact_yield_metrics.py', 'tools/contact_yield_protocol.py', 'tools/contact_yield_laws.py',
                                                    'config/yield_fair_campaign_v1.json', 'tools/build_contact_laws.py (grep only)',
                                                    'report/yield-mechanism-development-v1/{README.md,frozen-campaign.json,main-review.md} at receipt HEAD'],
        'cache': cache,
        'outputs': outputs,
        'python': sys.executable, 'cpu_threads': os.environ.get('OMP_NUM_THREADS', 'unset'),
    }
    json.dump(rec, sys.stdout, indent=1)


if __name__ == '__main__':
    main()
