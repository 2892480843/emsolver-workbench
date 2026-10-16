"""One job budget, partitioned across disjoint host and container process domains."""
from __future__ import annotations


def memory_budgets(config):
    total=int(config['resources']['memory_limit_gib']*1024**3)
    container=total//2 if config['solver']['backend']=='slepc' else 0
    return {'total_bytes':total,'host_bytes':total-container,'container_bytes':container}
