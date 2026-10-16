from emsolver.cluster_protocol import execution_fingerprint, ARTIFACT_NAMES, BUNDLE_NAMES


def test_execution_contract_is_stable_and_only_names_portable_artifacts():
    value = execution_fingerprint()
    assert value == execution_fingerprint()
    assert value['protocol'] == 'emsolver.remote.v1'
    assert len(value['source_sha256']) == 64
    assert 'eigenvalues.npy' in ARTIFACT_NAMES
    assert 'worker.result.json' in BUNDLE_NAMES
    assert 'worker.spec.json' not in BUNDLE_NAMES
    assert all('/' not in name and '\\' not in name for name in BUNDLE_NAMES)
