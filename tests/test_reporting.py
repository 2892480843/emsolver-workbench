from pathlib import Path
import io
import zipfile

from emsolver.reporting import build_bundle, build_report


def test_report_escapes_untrusted_names_and_keeps_partial_state(tmp_path):
    job = {"id": "run_1", "name": "<script>alert(1)</script>", "status": "PARTIAL",
           "result": {"eigenvalues": [0.1], "residuals": [1e-10],
                      "validation": {"target_certification": "unverified"}},
           "config": {}, "metrics": {"elapsed_seconds": 1.2}}
    path = build_report(job, [], tmp_path / "report.html")
    text = path.read_text()
    assert "<script>alert" not in text
    assert "PARTIAL" in text and "unverified" in text
    assert "1e-10" in text


def test_public_bundle_filters_secrets_and_excludes_private_input(tmp_path):
    run = tmp_path / "run"; run.mkdir()
    (run / "metrics.json").write_text('{"elapsed_seconds":1,"token":"secret-token"}')
    matrix = tmp_path / "private.npy"; matrix.write_bytes(b"private matrix")
    job = {"id": "r", "status": "PARTIAL", "token": "secret-token", "path": str(matrix)}
    path = build_bundle(job, [], run, tmp_path / "bundle.zip", matrix)
    with zipfile.ZipFile(path) as z:
        assert not any("private" in n for n in z.namelist())
        all_text = b"".join(z.read(n) for n in z.namelist() if n.endswith(('.json', '.md')))
        assert b"secret-token" not in all_text
        assert b"private.npy" not in all_text
        assert "README.md" in z.namelist()


def test_redacted_bundle_keeps_internal_artifact_hashes_consistent(tmp_path):
    import numpy as np
    from scipy import sparse
    from emsolver.numerics import run_solver, verify_run
    matrix=tmp_path/'input.npz'; sparse.save_npz(matrix,sparse.diags(np.arange(12)-5.8,format='csr'))
    run=tmp_path/'run';result=run_solver(matrix,{'target':{'k':3}},run)
    bundle=build_bundle({'id':'r','status':result['status']},[],run,tmp_path/'bundle.zip',include_results=True)
    with zipfile.ZipFile(bundle) as z:z.extractall(tmp_path/'unpacked')
    check=verify_run(matrix,tmp_path/'unpacked'/'run')
    assert check['artifact_integrity']=='PASSED'


def test_batch_report_retains_failed_rows_and_denominators(tmp_path):
    from emsolver.reporting import build_benchmark_report
    batch={'id':'batch_1','runs':[{'id':'one','status':'SUCCEEDED','metrics':{'walltime_seconds':1}},
                               {'id':'two','status':'FAILED','error':{'code':'RESOURCE_EXHAUSTED'}}],
           'summary':{'total':2,'failed':1,'timing_denominator':1}}
    text=build_benchmark_report(batch,tmp_path/'batch.html').read_text()
    assert 'RESOURCE_EXHAUSTED' in text and 'two' in text
    assert 'timing_denominator' in text


def test_default_bundle_omits_private_modal_results_and_identities(tmp_path):
    import json
    import numpy as np
    run=tmp_path/'run';run.mkdir()
    np.save(run/'eigenvectors.npy',np.eye(2))
    (run/'validation.json').write_text('{"eigenvalues":[123.456]}')
    (run/'config.resolved.yaml').write_text('target:\n  k: 1\n')
    job={'id':'r','status':'PARTIAL','name':'private-project-name','owner':'person@example.com',
         'result':{'eigenvalues':[123.456]},'matrix_metadata':{'source_url':'private-source'},
         'config':{'target':{'k':1}},'runtime':{'hardware_id':'private-host','python':'3.12'}}
    bundle=build_bundle(job,[{'seq':1,'event':'SOLVED','eigenvalues':[123.456]}],run,tmp_path/'public.zip')
    with zipfile.ZipFile(bundle) as archive:
        assert 'run/eigenvectors.npy' not in archive.namelist()
        assert 'run/validation.json' not in archive.namelist()
        summary=json.loads(archive.read('job.json'))
        assert 'result' not in summary and 'owner' not in summary and 'name' not in summary
        text=archive.read('job.json')+archive.read('events.json')
        assert b'123.456' not in text and b'private-host' not in text
        assert json.loads(archive.read('export-policy.json'))['results_included'] is False


def test_public_bundle_redacts_embedded_identity_paths_and_streams_binary(tmp_path,monkeypatch):
    import json,numpy as np
    run=tmp_path/'run';run.mkdir()
    np.save(run/'eigenvalues.npy',np.asarray([1.]))
    (run/'diagnostics.json').write_text(json.dumps({'actor':'alice-private','description':'loaded /Users/alice/private/device.npz, then inspected','url':'https://example.org/public','source':'C:\\Users\\alice\\secret.npz'}))
    job={'id':'r','status':'PARTIAL','config':{'problem':{'nullspace':{'source':'from /home/alice/model.npz'}}},'matrix_metadata':{'public':True,'license':'CC0-1.0','description':'alice-private'}}
    original=Path.read_bytes
    def bounded(self):
        if self.suffix in {'.npy','.npz'}:raise AssertionError('Binary export must stream')
        return original(self)
    monkeypatch.setattr(Path,'read_bytes',bounded)
    bundle=build_bundle(job,[],run,tmp_path/'public.zip',include_results=True)
    with zipfile.ZipFile(bundle) as archive:
        text='\n'.join(archive.read(name).decode() for name in archive.namelist() if name.endswith('.json'))
        assert '/Users/alice' not in text and '/home/alice' not in text and 'alice-private' not in text
        assert 'https://example.org/public' in text
        assert 'eigenvalues.npy' in archive.read('checksums.json').decode()


def test_public_scanner_blocks_private_runtime_and_allows_source_redaction_patterns(tmp_path):
    from emsolver.public_export import scan_public_archive
    bad=tmp_path/'bad.zip'
    with zipfile.ZipFile(bad,'w') as archive:
        archive.writestr('job.json','{"actor":"private-user","note":"/Users/alice/input.npz"}')
    assert not scan_public_archive(bad)['passed']
    good=tmp_path/'good.zip'
    with zipfile.ZipFile(good,'w') as archive:
        archive.writestr('job.json','{"status":"PARTIAL"}')
        archive.writestr('emsolver/redaction.py','PREFIX="/Users/alice/"')
    assert scan_public_archive(good)['passed']


def test_benchmark_report_has_per_case_accuracy_iterations_and_disposition(tmp_path):
    from emsolver.reporting import build_benchmark_report
    job={'id':'failed-one','status':'PARTIAL','config':{},'error':{'code':'TARGET_UNVERIFIED'},
         'result':{'validation':{'residuals':[1e-9,2e-9],'target_certification':'UNVERIFIED'},
                   'metrics':{'outer_iterations':7,'linear_iterations':13}}}
    batch={'id':'b','runs':[job],'summary':{'total':1}}
    text=build_benchmark_report(batch,tmp_path/'report.html').read_text()
    assert '2e-09' in text and '外层迭代' in text and '线性内层迭代' in text
    assert 'NOT_AVAILABLE' in text and '独立参考' in text and '处置建议' in text
    assert 'failed-one' in text and 'TARGET_UNVERIFIED' in text


def test_public_bundle_url_query_credentials_are_redacted_and_raw_scanner_rejects_them(tmp_path):
    from emsolver.public_export import scan_public_archive
    import json
    secret='REVIEW_ONLY_SECRET_123'
    run=tmp_path/'run';run.mkdir()
    job={'id':'r','status':'PARTIAL','matrix_metadata':{'public':True,'license':'CC0-1.0','source_url':'https://example.org/data?dataset=42&access_token='+secret}}
    bundle=build_bundle(job,[],run,tmp_path/'public.zip',include_results=True)
    with zipfile.ZipFile(bundle) as archive:
        data=archive.read('job.json').decode()
        assert secret not in data and 'dataset=42' in data
    assert scan_public_archive(bundle)['passed']
    bad=tmp_path/'raw.zip'
    with zipfile.ZipFile(bad,'w') as archive:archive.writestr('job.json',json.dumps(job))
    assert not scan_public_archive(bad)['passed']
