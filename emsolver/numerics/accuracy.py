"""Shared value-fidelity policy and a non-authoritative refinement hint.

The exact certifiers own acceptance. This floating estimate only decides
whether an iterative candidate merits more work before independent validation.
"""
from fractions import Fraction

import numpy as np


VALUE_FIDELITY_ULPS_PER_ROW = 4096


def value_fidelity_budget(n, theta):
    return Fraction(VALUE_FIDELITY_ULPS_PER_ROW*n, 2**52)*max(abs(theta), Fraction(1, 2**1022))


def refinement_readiness(scaled, values, vectors, scale, columns, *, check):
    """Estimate the exact certifier's residual enclosure with an 8x margin.

    Work a column at a time in scaled units to avoid overflow and n*k scratch
    arrays. A true hint is never a certificate; rounding/cancellation may still
    cause the subsequent exact proof to refuse. The original matrix is intact.
    """
    ready = np.zeros(len(values), dtype=bool)
    factor = VALUE_FIDELITY_ULPS_PER_ROW*scaled.shape[0]*np.finfo(float).eps
    for column in columns:
        check()
        vector = vectors[:, column]
        lower = max(float(np.max(np.abs(vector.real), initial=0.)),
                    float(np.max(np.abs(vector.imag), initial=0.)))
        with np.errstate(over='ignore', invalid='ignore', divide='ignore', under='ignore'):
            theta = values[column]/scale
            residual = scaled@vector-theta*vector
            upper = float(np.sum(np.abs(residual.real))+np.sum(np.abs(residual.imag)))
            budget = factor*max(abs(theta), np.finfo(float).tiny/scale)
            radius = upper/lower if lower else float('inf')
        ready[column] = bool(np.isfinite(radius) and np.isfinite(budget) and radius <= budget/8.)
        check()
    return ready
