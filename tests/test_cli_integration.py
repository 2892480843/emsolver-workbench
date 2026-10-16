import json
import numpy as np
from scipy import sparse

from emsolver.cli import main


def test_cli_solve_supervision_does_not_invalidate_published_artifacts(tmp_path,capsys):
    matrix=tmp_path/'input.npz';sparse.save_npz(matrix,sparse.diags(np.arange(12)-5.8,format='csr'))
    output=tmp_path/'run'
    assert main(['solve','--matrix',str(matrix),'--k','3','--output',str(output),'--json'])==0
    payload=json.loads(capsys.readouterr().out)
    assert payload['n_accepted']==3
    assert payload['metrics']['process_tree_peak_bytes']>0
    assert main(['validate','--matrix',str(matrix),'--run',str(output),'--json'])==0
    assert json.loads(capsys.readouterr().out)['artifact_integrity']=='PASSED'


def test_unavailable_backend_keeps_stable_exit_code(tmp_path,capsys):
    path=tmp_path/'config.yaml';path.write_text('solver:\n  backend: slepc\n')
    result=main(['solve','--matrix',str(tmp_path/'no-input.npz'),'--config',str(path),'--output',str(tmp_path/'run'),'--json'])
    assert result==6
    assert json.loads(capsys.readouterr().out)['error_code']=='BACKEND_UNAVAILABLE'


def test_readonly_cli_does_not_start_pending_jobs_or_recover_running_rows(tmp_path,capsys):
    from emsolver.service import Service
    root=tmp_path/'service'
    service=Service(root,start_runner=False)
    service.store.put('jobs','observed',{'id':'observed','status':'RUNNING','stage':'solving','metrics':{}})
    service.close()
    assert main(['--data-dir',str(root),'jobs','status','--job','observed','--json'])==0
    assert json.loads(capsys.readouterr().out)['status']=='RUNNING'
