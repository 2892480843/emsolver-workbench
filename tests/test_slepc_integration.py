import os
from pathlib import Path

import pytest


@pytest.mark.parametrize('kind', ['indefinite', 'complex'])
@pytest.mark.parametrize('ranks', [1, 2])
def test_real_slepc_backend_uses_isolated_candidate_and_authoritative_original_validation(tmp_path, kind, ranks):
    if not os.environ.get('EMSOLVER_SLEPC_IMAGE'):
        pytest.skip('Explicit tested optional SLEPc image is required')
    from emsolver.numerics import core
    path = tmp_path / 'input.npz'
    core.generate_sample(path, kind, n=20, seed=20261016)
    config = {'solver': {'backend': 'slepc', 'method': 'krylovschur', 'subspace_max': 15},
              'target': {'k': 2, 'sigma': .1}, 'resources': {'threads': 1, 'mpi_ranks': ranks,
              'memory_limit_gib': 2, 'walltime_seconds': 30, 'disk_limit_gib': .1}}
    result = core.run_solver(path, config, tmp_path / 'output')
    assert result['validation']['numerical_status'] == 'PASSED'
    assert result['validation']['n_validated'] == 2
    assert result['metrics']['backend'] == 'slepc'
    assert result['metrics']['mpi_ranks'] == ranks
    assert result['metrics']['container_memory_limit_bytes'] == 1024**3
    import json
    plan = json.loads((tmp_path / 'output' / 'strategy.json').read_text())
    assert plan['actual_backend'] == 'slepc' and plan['actual_method'] == 'krylovschur'
    assert plan['reference_isolation']['network'] == 'none'
    assert not plan['reference_isolation']['reference_mounted']


def test_unconfigured_slepc_does_not_claim_supported_profile(monkeypatch):
    monkeypatch.delenv('EMSOLVER_SLEPC_IMAGE', raising=False)
    from emsolver.optional_backends import probe_slepc
    assert probe_slepc()['supported'] is False


def test_slepc_extra_unsorted_candidates_are_validated_and_selected_in_original_target_order(tmp_path,monkeypatch):
    import numpy as np
    from scipy import sparse
    from types import SimpleNamespace
    from emsolver.numerics.slepc import SlepcBackend
    from emsolver.contracts import resolve_config
    import emsolver.optional_backends as optional
    a=sparse.diags([-2.,-1.,1.,2.,3.],format='csr')
    order=[4,3,1,2]
    monkeypatch.setattr(optional,'solve_slepc',lambda *args,**kwargs:{
        'values':np.array([3.,2.,-1.,1.]),'vectors':np.eye(5)[:,order],
        'metrics':{},'strategy':{},'attempts':[]})
    backend=object.__new__(SlepcBackend)
    backend.phase='PREPARED';backend.working=a;backend.strategy={}
    backend.config=resolve_config({'target':{'k':2,'sigma':0.}})
    backend.runtime=SimpleNamespace(output_dir=tmp_path,emit=lambda *args,**kwargs:None,check=lambda:None)
    backend.solve()
    values,vectors,_,metrics=backend._candidates
    assert values.tolist()==[-1.,1.]
    assert np.array_equal(np.abs(vectors),np.eye(5)[:,[1,2]])
    assert metrics['backend_raw_candidates']==4
