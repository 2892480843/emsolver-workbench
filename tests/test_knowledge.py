import copy

import pytest

from emsolver.advice import apply_solver_advice
from emsolver.contracts import resolve_config
from emsolver.knowledge import diagnose, get_knowledge, list_knowledge


@pytest.mark.parametrize('rule_id,code', [
    ('non_hermitian', 'NOT_HERMITIAN'), ('near_singular', 'SINGULAR_SHIFT'),
    ('inner_not_converged', 'LINEAR_RESIDUAL_FAILED'),
    ('subspace_insufficient', 'SUBSPACE_INSUFFICIENT'),
    ('repeated_modes', 'CLUSTER_DEGENERACY'), ('memory_exhausted', 'RESOURCE_EXHAUSTED'),
    ('timeout', 'WALLTIME_EXCEEDED'), ('backend_unavailable', 'BACKEND_UNAVAILABLE'),
    ('target_unverified', 'TARGET_UNVERIFIED'), ('worker_lost', 'WORKER_LOST'),
])
def test_rules_have_positive_negative_evidence_and_safe_changes(rule_id, code):
    config = resolve_config({'target': {'k': 4}, 'solver': {'subspace_max': 30}})
    job = {'config': config, 'error': {'code': code}, 'status': 'FAILED'}
    report = diagnose(job, matrix={'n': 1000})
    rule = next(row for row in report['rules'] if row['id'] == rule_id)
    assert rule['triggered'] is True
    assert any(e['value'] == code for e in rule['evidence'])
    for item in report['suggestions']:
        candidate = apply_solver_advice(config, item['changes'])
        assert candidate['target'] == config['target']
        assert candidate['validation'] == config['validation']
        assert candidate['resources'] == config['resources']
    negative = diagnose({'config': config, 'error': None, 'status': 'SUCCEEDED'})
    assert next(row for row in negative['rules'] if row['id'] == rule_id)['triggered'] is False
    assert report['rule_version'] and report['knowledge']


@pytest.mark.parametrize('rule_id,config,metadata', [
    ('lobpcg_prerequisites', {'solver': {'method': 'lobpcg'}}, {}),
    ('generalized_prerequisites', {'problem': {'type': 'generalized_hermitian', 'matrix_b_id': 'b'}}, {}),
    ('pml_structure', {}, {'pml': True}),
])
def test_special_advice_shows_prerequisites_and_does_not_rewrite_problem(rule_id, config, metadata):
    resolved = resolve_config(config)
    original = copy.deepcopy(resolved)
    report = diagnose(config=resolved, matrix={'metadata': metadata})
    item = next(row for row in report['suggestions'] if row['rule_id'] == rule_id)
    assert item['prerequisites']
    assert item['changes'] == {}
    assert resolved == original
    negative = diagnose(config={})
    assert next(row for row in negative['rules'] if row['id'] == rule_id)['triggered'] is False


def test_registry_versions_sources_search_and_copy_isolation():
    entries = list_knowledge()
    assert len(entries) >= 6
    for entry in entries:
        assert entry['version'] and entry['registry_version'] and entry['prerequisites']
        assert all(source['url'].startswith('https://') and source['version'] for source in entry['sources'])
    assert any(entry['id'] == 'lobpcg' for entry in list_knowledge('LOBPCG'))
    entry = get_knowledge('lobpcg')
    entry['prerequisites'].clear()
    assert get_knowledge('lobpcg')['prerequisites']
    with pytest.raises(KeyError):
        get_knowledge('missing')


def test_missing_numerical_evidence_is_not_invented():
    report = diagnose(config={})
    assert report['suggestions'][0]['changes'] == {}
    assert report['ai_available'] is False
    assert not any(e['field'].startswith('reference') for rule in report['rules'] for e in rule['evidence'])
