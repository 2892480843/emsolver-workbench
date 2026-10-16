"""Default workflows must fit a single CPU without altering explicit budgets."""
import pytest

from emsolver.contracts import resolve_config
from emsolver.errors import ServiceError
from emsolver.service import Service


def test_default_thread_budget_is_portable_and_explicit_values_are_preserved():
    assert resolve_config({})['resources']['threads'] == 1
    assert resolve_config({'resources': {'threads': 8}})['resources']['threads'] == 8


def test_single_cpu_can_plan_defaults_and_benchmarks_but_rejects_explicit_oversubscription(tmp_path, monkeypatch):
    service=Service(tmp_path,start_runner=False,execution_mode='local')
    original=service.capabilities
    monkeypatch.setattr(service,'capabilities',lambda:{**original(),'cpu_count':1})
    try:
        project=service.create_project('Single CPU')
        matrix=service.generate_sample(project['id'],n=20)
        config={'problem':{'matrix_id':matrix['id']},'target':{'k':2}}
        plan=service.create_plan(config=config)
        assert plan['config']['resources']['threads']==1
        batch=service.create_benchmark(project['id'],[matrix['id']],k=2,repeats=1)
        assert len(batch['jobs'])==1
        with pytest.raises(ServiceError) as caught:
            service.create_plan(config={**config,'resources':{'threads':2}})
        assert caught.value.code=='THREAD_BUDGET_EXCEEDED'
    finally:
        service.close()
