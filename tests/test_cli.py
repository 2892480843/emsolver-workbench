import json

from emsolver.cli import main


def test_cli_config_validation_is_machine_readable(tmp_path, capsys):
    path=tmp_path/'config.yaml'; path.write_text('target:\n  k: 3\n')
    assert main(['config','validate','--config',str(path),'--json']) == 0
    parsed=json.loads(capsys.readouterr().out)
    assert parsed['config']['target']['k']==3


def test_cli_rejects_bad_configuration_with_exit_two(tmp_path, capsys):
    path=tmp_path/'config.yaml'; path.write_text('target:\n  k: 0\n')
    assert main(['config','validate','--config',str(path),'--json']) == 2
    assert json.loads(capsys.readouterr().err)['code']=='INVALID_CONFIG'


def test_cli_matrix_import_applies_declared_metadata(tmp_path,capsys,monkeypatch):
    from scipy import sparse
    from emsolver.service import Service
    source=tmp_path/'input.npz';sparse.save_npz(source,sparse.eye(10,format='csr'))
    metadata=tmp_path/'metadata.json';metadata.write_text(json.dumps({'license':'CC0-1.0','public':True,'source_url':'https://example.org/public-matrix'}))
    monkeypatch.setenv('EMSOLVER_IMPORT_ROOTS',str(tmp_path))
    service=Service(tmp_path/'state',start_runner=False)
    project=service.create_project('CLI metadata');service.close()
    assert main(['--data-dir',str(tmp_path/'state'),'matrix','import','--matrix',str(source),'--project',project['id'],'--metadata',str(metadata),'--json'])==0
    matrix=json.loads(capsys.readouterr().out)
    assert matrix['metadata']['license']=='CC0-1.0' and matrix['metadata']['public'] is True
