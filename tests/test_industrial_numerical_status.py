"""Industrial reports must not label a failed empty candidate as validated."""
import numpy as np
from scipy import sparse
from emsolver.contracts import resolve_config
from emsolver.numerics import core


def test_empty_nearest_candidate_is_not_numerically_passed():
    report, _ = core._validate(sparse.eye(8, format='csr'), np.empty(0), np.empty((8, 0)), resolve_config({}))
    assert report['numerical_status'] == 'FAILED'
    assert report['n_validated'] == 0


def test_certified_empty_interval_remains_a_valid_result(tmp_path):
    path = tmp_path / 'matrix.npz'
    sparse.save_npz(path, sparse.diags([1., 2., 3., 4.], format="csr"))
    result = core.run_solver(path, {'target': {'mode': 'interval_all', 'interval': {'lower': 1.1, 'upper': 1.9}}}, tmp_path / 'result')
    assert result['status'] == 'SUCCEEDED'
    assert result['validation']['target_certification'] == 'CERTIFIED'
    assert result['n_accepted'] == 0
