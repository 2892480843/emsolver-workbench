import numpy as np
import pytest
from emsolver.numerics.shift_policy import FeedbackShiftPolicy


def test_feedback_uses_observed_ritz_gap_without_changing_target():
    plain=FeedbackShiftPolicy(0.,1e-6,4)
    assert plain.next_shift()==0.
    first=plain.next_shift()
    learned=FeedbackShiftPolicy(0.,1e-6,4)
    assert learned.next_shift()==0.
    learned.observe(np.asarray([-.004,.002,.006]),error_code='ARPACK_NO_CONVERGENCE')
    shift=learned.next_shift()
    assert shift!=first and learned.last_decision['reason']=='observed_ritz_gap'
    assert learned.target==0.
    assert 0<abs(shift)<=learned.radius_limit
    assert learned.last_decision['observed_values']==3


def test_policy_is_bounded_finite_distinct_and_retains_failed_attempts():
    policy=FeedbackShiftPolicy(.5,1e-8,8)
    shifts=[]
    for _ in range(8):
        shifts.append(policy.next_shift())
        policy.observe(np.asarray([.5,.5,.5001]),error_code='FACTORIZATION_FAILED')
    assert len(set(shifts))==8 and np.isfinite(shifts).all()
    assert max(abs(value-.5) for value in shifts)<=policy.radius_limit*(1+1e-8)
    with pytest.raises(RuntimeError,match='SHIFT_ATTEMPTS_EXHAUSTED'):policy.next_shift()
    assert policy.failures==8


def test_empty_nonfinite_partial_feedback_uses_scale_fallback_and_input_is_immutable():
    values=np.asarray([np.nan,np.inf,1.])
    before=values.copy()
    policy=FeedbackShiftPolicy(0.,1e-7,3)
    policy.observe(values)
    assert policy.next_shift()==0.
    assert policy.next_shift()!=0.
    np.testing.assert_equal(values,before)
    assert policy.last_decision['reason']=='matrix_scale_fallback'


@pytest.mark.parametrize('target,delta,count',[(np.inf,1.,2),(0.,0.,2),(0.,1.,0),(0.,1.,True)])
def test_invalid_policy_rejected(target,delta,count):
    with pytest.raises(ValueError):FeedbackShiftPolicy(target,delta,count)
