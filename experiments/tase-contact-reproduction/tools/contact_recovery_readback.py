"""Fresh GET and existing TP validation for the installed recovery pair."""
import hashlib,json,subprocess,time
from pathlib import Path
from build_contact_recovery import RELIEF_PROGRAM
from build_contact_home import BASENAME
from contact_yield_live_contract import PACKAGE_DIR

OWNER=Path('/home/andy/codex-private-skills-shared-main/skills')


def fetch_recovery_readback(output, package_dir=None, *, basenames=(RELIEF_PROGRAM, BASENAME)):
    package_dir=Path(package_dir) if package_dir is not None else PACKAGE_DIR
    output=Path(output);output.mkdir(parents=True,exist_ok=False)
    observed=time.time()
    for name in basenames:
        binding=json.loads((package_dir/f'{name}.binding.json').read_text())
        artifacts=[{'filename':f'{name}.{ext}','source':str(package_dir/f'{name}.{ext}'),'sha256':hashlib.sha256((package_dir/f'{name}.{ext}').read_bytes()).hexdigest()} for ext in ('script','txt','urp')]
        manifest=output/f'{name}-manifest.json';manifest.write_text(json.dumps({'schema_version':1,'basename':name,'controller_directory':'/programs/andyl/kunwei/step5','artifacts':artifacts},indent=2)+'\n')
        target=output/'readback'/name
        with (output/f'{name}-readback.log').open('w') as log:
            subprocess.run(['/usr/bin/python3',str(OWNER/'ur10e-controller-access/scripts/ur10e_controller_ssh.py'),'readback','--manifest',str(manifest),'--output-dir',str(target)],check=True,stdout=log,stderr=subprocess.STDOUT,timeout=30)
        result=subprocess.run(['/usr/bin/python3',str(OWNER/'ur10e-tp-package-delivery/scripts/validate_ur_tp_package.py'),'--package-dir',str(package_dir),'--compare-dir',str(target),'--basename',name,'--controller-dir','/programs/andyl/kunwei/step5','--expected-stamp',binding['stamp'],'--expected-installation','/programs/default.installation'],check=True,capture_output=True,text=True,timeout=15)
        (output/f'{name}-validation.json').write_text(result.stdout)
        if json.loads(result.stdout).get('pass') is not True:raise ValueError(f'{name} read-back invalid')
    (output/'readback-results.json').write_text(json.dumps({'pass':True,'observed_at_s':observed,'scope':'fresh GET of installed packages','basenames':list(basenames)},indent=2)+'\n')
    return output
