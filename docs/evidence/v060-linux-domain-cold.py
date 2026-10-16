from pathlib import Path
from contextlib import closing
import hashlib,json,sys
import numpy as np
from scipy import sparse
import emsolver
from emsolver.service import Service
from emsolver.industrial import IndustrialCases
from emsolver.industrial_reference import IndustrialReferenceAssets

if __name__ == '__main__':
    root=Path(sys.argv[1]); evidence_path=Path(sys.argv[2]);root.mkdir()
    import sysconfig
    installed = Path(emsolver.__file__).resolve()
    assert installed.is_relative_to(Path(sysconfig.get_path('purelib')).resolve()), installed
    assert 'site-packages' in installed.parts and emsolver.__version__ == '0.6.0', installed
    with closing(Service(root/'data',start_runner=False)) as gateway:
        project=gateway.create_project('Installed industrial workflow fixture')
        source=gateway.root/'inbox'/'matrix.npz';sparse.save_npz(source,sparse.diags(np.arange(1.,9.),format='csr'))
        matrix=gateway.import_matrix(project['id'],source)
        plan=gateway.create_plan(config={'problem':{'matrix_id':matrix['id']},'target':{'k':2,'sigma':0.},'resources':{'threads':1,'memory_limit_gib':1,'walltime_seconds':30}})
        job=gateway.submit_job(plan['id']);gateway._execute(job['id']);job=gateway.get_job(job['id'])
        assert job['status']=='SUCCEEDED'
        reference=gateway.root/'inbox'/'reference.npz';np.savez(reference,eigenvalues=np.array([1.,2.]),eigenvectors=np.eye(8)[:,:2])
        asset=IndustrialReferenceAssets(gateway).create(project['id'],reference)
        ledger=IndustrialCases(gateway);manifest=ledger.template(project['id'],job['id'])['manifest']
        manifest.update(title='Installed teaching reference workflow',declared_use='teaching')
        manifest['reference']={'matrix_sha256':matrix['hash'],'target_sha256':ledger.template(project['id'],job['id'])['binding']['target_sha256'],'method':'analytic','producer':'Independent diagonal fixture generator','source':{'title':'Test fixture only','url':'https://example.org/test-fixture'},'artifact_sha256':asset['sha256'],'artifact_id':asset['id'],'attestation':None}
        case=ledger.create(project['id'],manifest);check=ledger.check(case['id'])
        assert check['industrial_accepted'] is False and check['status']=='UNVERIFIED'
        assert next(g for g in check['gates'] if g['name']=='reference_comparison')['status']=='PASS'
        backup=gateway.backup();restored_path=root/'restored';gateway.restore_backup(backup,restored_path)
    with closing(Service(restored_path,start_runner=False)) as restored:
        assert IndustrialCases(restored).get(case['id'])['latest_check']['industrial_accepted'] is False
        assert IndustrialReferenceAssets(restored).path(asset['id']).is_file()
        restored_check=IndustrialCases(restored).check(case['id'])
        assert restored_check['status']=='UNVERIFIED'
    evidence={'passed':True,'version':emsolver.__version__,'installed_package':emsolver.__file__,'scope':'Installed CPU wheel; diagonal teaching fixture, not industrial physics','candidate_status':job['status'],'reference_comparison':'PASS','industrial_status':check['status'],'restored_status':restored_check['status'],'backup_restore':True,'external_http_requests':0}
    evidence_path.write_text(json.dumps(evidence,ensure_ascii=False,indent=2)+'\n');print(json.dumps(evidence,ensure_ascii=False))
