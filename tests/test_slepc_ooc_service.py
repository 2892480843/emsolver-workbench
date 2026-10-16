"""Service must defer explicit SLEPc profile validation to the backend contract."""
import pytest
from emsolver.service import Service,ServiceError


def test_service_accepts_backend_validated_slepc_factor_profile_without_bypassing_scipy_checks(tmp_path,monkeypatch):
 import emsolver.numerics as numerical
 service=Service(tmp_path/'data',start_runner=False)
 try:
  project=service.create_project('OOC profile routing');matrix=service.generate_sample(project['id'],n=20)
  monkeypatch.setattr(service,'capabilities',lambda:{'slepc':{'supported':True},'scipy':{'supported':True,'methods':['arpack']},'limits':{'max_modes':512,'max_subspace':1024},'cpu_count':2})
  calls=[]
  def validate(config,**kwargs):
   calls.append(config['solver']['linear']['preconditioner'])
   assert config['solver']['backend']=='slepc'
   return {'actual_backend':'slepc','actual_method':'krylovschur'}
  monkeypatch.setattr(numerical,'algorithm_plan',validate)
  config={'problem':{'matrix_id':matrix['id']},'target':{'k':2},'solver':{'backend':'slepc','method':'krylovschur','linear':{'preconditioner':'mumps_ooc_symmetric_v1'}},'resources':{'threads':1}}
  plan=service.create_plan(config=config)
  assert calls==['mumps_ooc_symmetric_v1']
  assert plan['config']['solver']['linear']['preconditioner']=='mumps_ooc_symmetric_v1'
  config['solver'].update(backend='scipy',method='arpack')
  with pytest.raises(ServiceError) as error:service.create_plan(config=config)
  assert error.value.code=='UNSUPPORTED_PRECONDITIONER'
  assert calls==['mumps_ooc_symmetric_v1']
 finally:service.close()


def test_low_ooc_workspace_budget_returns_resource_error_instead_of_internal_failure(tmp_path,monkeypatch):
 import emsolver.numerics as numerical
 from emsolver.optional_backends import driver_config
 service=Service(tmp_path/'data',start_runner=False)
 try:
  project=service.create_project('OOC budget');matrix=service.generate_sample(project['id'],n=20)
  monkeypatch.setattr(service,'capabilities',lambda:{'slepc':{'supported':True},'cpu_count':2})
  monkeypatch.setattr(numerical,'algorithm_plan',lambda config,**kwargs:driver_config(config))
  config={'problem':{'matrix_id':matrix['id']},'target':{'k':2},'solver':{'backend':'slepc','method':'krylovschur','linear':{'preconditioner':'mumps_ooc_symmetric_v1'}},'resources':{'threads':1,'memory_limit_gib':.1}}
  with pytest.raises(ServiceError) as error:service.create_plan(config=config)
  assert error.value.code=='RESOURCE_ESTIMATE_EXCEEDED'
 finally:service.close()


def test_unexpected_plan_runtime_error_is_not_reclassified_as_budget_exhaustion(tmp_path,monkeypatch):
 import emsolver.numerics as numerical
 service=Service(tmp_path/'data',start_runner=False)
 try:
  project=service.create_project('Unexpected failure');matrix=service.generate_sample(project['id'],n=20)
  monkeypatch.setattr(service,'capabilities',lambda:{'scipy':{'supported':True,'methods':['arpack']},'cpu_count':2})
  def fail(*args,**kwargs):raise RuntimeError('unexpected planner bug')
  monkeypatch.setattr(numerical,'algorithm_plan',fail)
  with pytest.raises(RuntimeError,match='unexpected planner bug'):
   service.create_plan(config={'problem':{'matrix_id':matrix['id']},'target':{'k':2},'resources':{'threads':1}})
 finally:service.close()
