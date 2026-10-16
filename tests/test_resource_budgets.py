from emsolver.contracts import resolve_config
from emsolver.resource_budgets import memory_budgets


def test_disjoint_memory_domains_share_one_job_limit():
    cfg=resolve_config({'resources':{'memory_limit_gib':1.000000001}})
    cpu=memory_budgets(cfg)
    assert cpu['host_bytes']==cpu['total_bytes'] and cpu['container_bytes']==0
    cfg['solver']['backend']='slepc'
    limits=memory_budgets(cfg)
    assert limits['host_bytes']+limits['container_bytes']==limits['total_bytes']
    assert abs(limits['host_bytes']-limits['container_bytes'])<=1
