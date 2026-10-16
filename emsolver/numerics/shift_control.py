"""Version dispatch preserves explicitly pinned legacy shift experiments."""
from __future__ import annotations

import math

from .shift_policy import FeedbackShiftPolicy


class _LegacyScalePolicy:
    def __init__(self,base,delta,count):
        self.base,self.delta,self.count,self.index=base,delta,count,0
        self.last_decision={}

    def observe(self,values=(),*,error_code=None,linear_residual=None):
        # v1 was a predetermined scale-only sequence; do not silently turn an
        # explicitly version-pinned experiment into the learned v2 controller.
        pass

    def next_shift(self):
        index=self.index
        if index>=self.count:raise RuntimeError('SHIFT_ATTEMPTS_EXHAUSTED: legacy retry count reached')
        value=self.base if not index else self.base+((-1)**(index+1))*self.delta*10**((index-1)//2)
        if not math.isfinite(value):raise RuntimeError('NUMERICAL_SCALE_LIMIT: legacy work shift is not finite')
        self.index+=1
        self.last_decision={'controller_version':'feedback_bounded_v1','reason':'original_target' if not index else 'legacy_matrix_scale',
                            'attempt':index+1,'observed_values':0,'observed_gap_scaled':None}
        return value


def create_shift_policy(base,delta,count,policy):
    return _LegacyScalePolicy(base,delta,count) if policy=='feedback_bounded_v1' else FeedbackShiftPolicy(base,delta,count)
