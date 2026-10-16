import pytest
from emsolver.contracts import resolve_config
from emsolver.numerics import algorithm_plan,estimate_resources


def config():
 return resolve_config({'target':{'k':2},'solver':{'backend':'slepc','method':'krylovschur','subspace_max':16,'linear':{'preconditioner':'mumps_ooc_symmetric_v1'}},'resources':{'threads':1,'memory_limit_gib':8,'disk_limit_gib':8}})


def test_ooc_preflight_refuses_actual_complex_input_even_when_declared_auto():
 with pytest.raises(ValueError,match='UNSUPPORTED_SCALAR'):
  algorithm_plan(config(),{'n':100,'nnz':500,'dtype':'complex128'})
 with pytest.raises(ValueError,match='UNSUPPORTED_SCALAR'):
  estimate_resources({'n':100,'nnz':500,'dtype':'complex128'},config())


def test_ooc_resource_plan_distinguishes_host_memory_from_disk_factors():
 stats={'n':1000,'nnz':5000,'dtype':'float64'};estimate=estimate_resources(stats,config())
 assert estimate['factorization_fill_in']['bounded'] is False
 assert estimate['out_of_core']['workspace_limit_bytes']==768*2**20
 assert estimate['out_of_core']['workspace_is_total_rss_bound'] is False
 assert estimate['out_of_core']['disk_fill_bounded'] is False
 assert estimate['out_of_core']['storage_directory']=='task_private_output'
 assert estimate['job_memory_partitions']['container_bytes']==4*2**30
 assert estimate['requires_runtime_process_tree_guard'] is True
