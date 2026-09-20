import importlib.util,contextlib,io,json,pathlib
p=pathlib.Path('report/yield-fair-training-v1/discussion-round10/scripts/analyze_round10.py').resolve()
spec=importlib.util.spec_from_file_location('round10_analysis',p);m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
m.CACHE=pathlib.Path('/tmp/yfp10-main');out=io.StringIO()
with contextlib.redirect_stdout(out):m.main()
r=json.loads(out.getvalue());expected=json.loads((p.parent.parent/'data/round10-analysis.json').read_text());assert r==expected
pathlib.Path('/tmp/yield-round10-main-analysis-reproduction.json').write_text(json.dumps({'analysis_exactly_reproduced_from_main_extracted_cache':True,'member_count':len(r['members']),'pair_count':len(r['pairs']),'checks':{k:v for k,v in r.items() if k.startswith('all_') or k=='max_J_abs_diff'},'scope':'Rerun supplied analysis with separately extracted and hash-verified raw inputs; not an independently implemented analysis.'},indent=2)+'\n')
print('EXACT',len(r['members']),len(r['pairs']))
