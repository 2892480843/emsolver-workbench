import numpy as np
import pytest
from scipy import sparse

from emsolver.errors import ServiceError
from emsolver.service import Service


def test_diagonal_plan_uses_validated_structural_statistics_for_resource_budget(tmp_path):
    service=Service(tmp_path/'data',start_runner=False)
    try:
        project=service.create_project('Diagonal budget')
        source=tmp_path/'matrix.npz'
        sparse.save_npz(source,sparse.diags(np.linspace(-2.,3.,100000),format='csr'))
        matrix=service.upload_matrix(project['id'],'matrix.npz',source.read_bytes())
        assert {'n','nnz','dtype','storage_bytes','is_diagonal'}<=matrix['diagnostics'].keys()
        plan=service.create_plan(config={'problem':{'matrix_id':matrix['id']},'target':{'k':1,'sigma':.15},
                   'solver':{'method':'block_shift_invert','subspace_max':1024},
                   'resources':{'threads':1,'memory_limit_gib':1}})
        assert plan['strategy']['actual_method']=='structural_diagonal'
        assert plan['estimate']['components']['subspace_bytes']==0
        assert plan['estimate']['minimum_fits_budget'] is True
    finally:service.close()


def test_auto_alias_plans_validated_arpack_family_without_enabling_other_methods(tmp_path):
    service=Service(tmp_path/'data',start_runner=False)
    try:
        project=service.create_project('Auto family')
        matrix=service.generate_sample(project['id'],kind='near_singular',n=40)
        config={'problem':{'matrix_id':matrix['id']},'target':{'k':2},'solver':{'method':'auto'},'resources':{'threads':1}}
        plan=service.create_plan(config=config)
        assert plan['strategy']['requested_method']=='auto'
        assert plan['strategy']['actual_method']=='structural_diagonal'
        config['solver']['method']='lobpcg'
        with pytest.raises(ServiceError) as error:service.create_plan(config=config)
        assert error.value.code=='BACKEND_UNAVAILABLE'
    finally:service.close()


def test_plan_rejects_estimate_above_the_same_safe_memory_budget_as_backend(tmp_path):
    from emsolver.contracts import resolve_config
    from emsolver.numerics import estimate_resources
    service=Service(tmp_path/'data',start_runner=False)
    try:
        project=service.create_project('Safe memory margin')
        matrix=service.generate_sample(project['id'],kind='near_singular',n=40)
        config={'problem':{'matrix_id':matrix['id']},'target':{'k':2},
                'resources':{'threads':1,'memory_limit_gib':.04}}
        estimate=estimate_resources(matrix['diagnostics'],resolve_config(config))
        assert estimate['minimum_memory_bytes']<estimate['budget_bytes']
        assert estimate['minimum_fits_budget'] is False
        with pytest.raises(ServiceError) as error:service.create_plan(config=config)
        assert error.value.code=='RESOURCE_ESTIMATE_EXCEEDED'
    finally:service.close()
