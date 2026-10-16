"""Bounded work-shift selection from matrix scale and observed numerical feedback.

Candidate Ritz gaps guide a retry; they are never evidence of target coverage.
The original target remains immutable and every output is validated against it.
"""
from __future__ import annotations
import math
import numpy as np


class FeedbackShiftPolicy:
    version='observed_ritz_gap_v2'

    def __init__(self,target,delta,max_attempts):
        if (not math.isfinite(target) or not math.isfinite(delta) or delta<=0 or
                type(max_attempts) is not int or not 1<=max_attempts<=16):
            raise ValueError('INVALID_SHIFT_POLICY: finite scale and bounded attempts required')
        self.target=float(target);self.delta=float(delta);self.max_attempts=max_attempts
        self.radius_limit=min(self.delta*10**max(0,(max_attempts-2)//2),np.finfo(float).max/4)
        self._observed=np.empty(0);self._used=[];self.failures=0;self.last_error=None;self.last_decision={}

    def observe(self,values=(),*,error_code=None,linear_residual=None):
        candidate=np.asarray(values).ravel()[:1024]
        if np.iscomplexobj(candidate):candidate=candidate.real[np.abs(candidate.imag)<=np.finfo(float).eps*64]
        candidate=np.asarray(candidate,dtype=float)
        finite=candidate[np.isfinite(candidate)]
        if finite.size:self._observed=np.unique(finite)
        if error_code:self.failures+=1;self.last_error=str(error_code)
        if linear_residual is not None and math.isfinite(linear_residual) and linear_residual>1e-6:
            self.failures+=1

    def next_shift(self):
        index=len(self._used)
        if index>=self.max_attempts:raise RuntimeError('SHIFT_ATTEMPTS_EXHAUSTED: frozen retry count reached')
        reason='original_target';gap=None
        if not index:
            chosen=self.target
        else:
            radius=min(self.radius_limit,self.delta*10**((index-1)//2)*2**min(self.failures,4))
            reason='linear_failure_feedback' if self.failures else 'matrix_scale_fallback'
            roots=self._observed
            if roots.size>=2:
                # Only a small local set influences the work shift; neither the
                # input name nor a reference spectrum is an accepted argument.
                local=roots[np.argsort(np.abs(roots-self.target),kind='stable')[:8]]
                ordered=np.sort(local)
                with np.errstate(over='ignore',invalid='ignore'):gaps=np.diff(ordered)
                noise=np.finfo(float).eps*64*max(1.,abs(self.target))
                usable=gaps[np.isfinite(gaps)&(gaps>noise)]
                if usable.size:
                    gap=float(np.min(usable))
                    radius=min(self.radius_limit,max(radius,gap/4))
                    reason='observed_ritz_gap'
            sign=1. if index%2 else -1.
            candidates=[self.target+sign*radius,self.target-sign*radius]
            candidates=[value for value in candidates if math.isfinite(value) and value not in self._used]
            if candidates:
                chosen=max(candidates,key=lambda value:float(np.min(np.abs(roots-value))) if roots.size and reason=='observed_ritz_gap' else 0.)
            else:
                chosen=None;low=high=self.target
                for _ in range(self.max_attempts):
                    low=math.nextafter(low,-math.inf);high=math.nextafter(high,math.inf)
                    for candidate in ((high,low) if sign>0 else (low,high)):
                        if math.isfinite(candidate) and candidate not in self._used and abs(candidate-self.target)<=self.radius_limit:
                            chosen=candidate;break
                    if chosen is not None:break
                if chosen is None:
                    raise RuntimeError('NUMERICAL_SCALE_LIMIT: no distinct bounded work shift is representable')
        self._used.append(float(chosen))
        self.last_decision={'controller_version':self.version,'reason':reason,'attempt':index+1,
                            'observed_values':len(self._observed),'observed_gap_scaled':gap,
                            'failures_observed':self.failures,'radius_limit_scaled':self.radius_limit}
        return float(chosen)
