import copy

import pytest

from emsolver.contracts import content_hash, resolve_config
from emsolver.effects import AdviceEffects
from emsolver.errors import ServiceError
from emsolver.store import Store


def setup_ledger(tmp_path):
    store = Store(tmp_path / 'ledger.sqlite3')
    base = resolve_config({'target': {'k': 4}, 'solver': {'subspace_max': 30}})
    candidate = copy.deepcopy(base)
    candidate['solver']['subspace_max'] = 50
    source = {'id': 'old', 'project_id': 'p', 'matrix_hash': 'abc', 'config': base, 'status': 'SUCCEEDED',
              'runtime': {'hardware_id': 'machine', 'python': '3.12'}, 'metrics': {'walltime_seconds': 4},
              'result': {'validation': {'numerical_status': 'PASSED', 'target_certification': 'CERTIFIED'}}}
    derived = {'id': 'cfg', 'config': candidate, 'hash': content_hash(candidate),
               'derivation': {'source_job_id': 'old', 'source_config_hash': content_hash(base)}}
    new = {**copy.deepcopy(source), 'id': 'new', 'config': copy.deepcopy(candidate), 'metrics': {'walltime_seconds': 2}}
    jobs = {'old': source, 'new': new}
    return store, jobs, derived, AdviceEffects(store, jobs.__getitem__, lambda _: derived)


def test_derived_link_and_observation_are_append_only_and_no_automatic_adoption(tmp_path):
    store, jobs, derived, ledger = setup_ledger(tmp_path)
    try:
        effect = ledger.create('old', 'cfg')
        before = store.get('advice_effects', effect['id'])
        link = ledger.link(effect['id'], 'new')
        comparison = ledger.evaluate(effect['id'])
        assert comparison['speedup'] == 2
        assert comparison['comparable'] is True
        assert comparison['before']['status'] == 'SUCCEEDED'
        assert effect['automatic_adoption'] is False
        assert store.get('advice_effects', effect['id']) == before
        assert link['new_job_id'] == 'new'
        assert {row['record_type'] for row in ledger.list('old')} == {'derived', 'linked', 'observed'}
        jobs['old']['config']['target']['k'] = 99
        assert store.get('advice_effects', effect['id'])['before_config']['target']['k'] == 4
    finally:
        store.close()


@pytest.mark.parametrize('protected', ['target', 'validation', 'resources', 'problem'])
def test_adoption_refuses_changed_protected_contract(tmp_path, protected):
    store, jobs, derived, ledger = setup_ledger(tmp_path)
    try:
        key = next(iter(derived['config'][protected]))
        derived['config'][protected][key] = 'tampered'
        with pytest.raises(ServiceError):
            ledger.create('old', 'cfg')
        assert ledger.list() == []
    finally:
        store.close()


@pytest.mark.parametrize('change', ['hardware', 'failure', 'missing_metric', 'matrix'])
def test_effects_never_report_speedup_without_matching_certified_conditions(tmp_path, change):
    store, jobs, derived, ledger = setup_ledger(tmp_path)
    try:
        effect = ledger.create('old', 'cfg')
        if change == 'hardware': jobs['new']['runtime']['hardware_id'] = 'other'
        if change == 'failure': jobs['new']['status'] = 'FAILED'
        if change == 'missing_metric': jobs['new']['metrics'] = {}
        if change == 'matrix': jobs['new']['matrix_hash'] = 'other'
        if change == 'matrix':
            with pytest.raises(ServiceError): ledger.link(effect['id'], 'new')
            return
        ledger.link(effect['id'], 'new')
        comparison = ledger.evaluate(effect['id'])
        assert comparison['speedup'] is None
        assert comparison['comparable'] is False
        assert comparison['reason']
    finally:
        store.close()


def test_cannot_rebind_an_effect_or_link_to_modified_config(tmp_path):
    store, jobs, derived, ledger = setup_ledger(tmp_path)
    try:
        effect = ledger.create('old', 'cfg')
        jobs['new']['config']['solver']['subspace_max'] = 40
        with pytest.raises(ServiceError): ledger.link(effect['id'], 'new')
        jobs['new']['config'] = copy.deepcopy(derived['config'])
        assert ledger.link(effect['id'], 'new') == ledger.link(effect['id'], 'new')
        jobs['another'] = {**copy.deepcopy(jobs['new']), 'id': 'another'}
        with pytest.raises(ServiceError): ledger.link(effect['id'], 'another')
    finally:
        store.close()
