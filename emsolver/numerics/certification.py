"""Independent, bounded exact inertia of the original IEEE Hermitian matrix.

No candidate eigensolver, floating-point pivot test, or dense matrix is used.
Every accepted count follows exact Hermitian congruence eliminations over
Gaussian rationals. The deliberately small budgets make this a certificate
for bounded inputs, not a scalable replacement for a sparse inertia backend.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from fractions import Fraction
import math
import time

import numpy as np
from scipy import sparse

from .accuracy import value_fidelity_budget


class CertificateUnavailable(ValueError):
    pass


@dataclass(frozen=True)
class CertificationBudget:
    max_n: int = 64
    max_nnz: int = 4096
    max_entries: int = 4096
    max_bits: int = 16384
    max_updates: int = 2000000
    max_inertias: int = 256
    max_seconds: float = 5.


@dataclass(frozen=True, slots=True)
class _Q:
    real: Fraction = Fraction(0)
    imag: Fraction = Fraction(0)

    def __bool__(self):return bool(self.real or self.imag)
    def __add__(self,other):return _Q(self.real+other.real,self.imag+other.imag)
    def __sub__(self,other):return _Q(self.real-other.real,self.imag-other.imag)
    def conjugate(self):return _Q(self.real,-self.imag)
    def __mul__(self,other):
        if not self.imag and not other.imag:return _Q(self.real*other.real)
        return _Q(self.real*other.real-self.imag*other.imag,self.real*other.imag+self.imag*other.real)
    def __truediv__(self,other):
        if not other.imag:return _Q(self.real/other.real,self.imag/other.real)
        denominator=other.real**2+other.imag**2
        return _Q((self.real*other.real+self.imag*other.imag)/denominator,
                  (self.imag*other.real-self.real*other.imag)/denominator)
    def l1(self):return abs(self.real)+abs(self.imag)


_ZERO=_Q()


def _fraction(value):
    if isinstance(value,Fraction):return value
    if not math.isfinite(float(value)):raise CertificateUnavailable('nonfinite_input')
    return Fraction.from_float(float(value))


def _q(value):
    return _Q(_fraction(np.real(value)),_fraction(np.imag(value)))


class InertiaOracle:
    """Count (negative, zero, positive) eigenvalues of A-point*I exactly.

    A nonzero scalar diagonal pivot contributes its sign. When all remaining
    diagonals vanish, any nonzero off diagonal gives an invertible 2x2 block
    with determinant -|z|² and exactly one sign of each kind. Its Schur
    complement is an exact nonsingular congruence. A zero remainder contributes
    its full nullity. No numerical near-zero threshold enters this argument.
    """
    def __init__(self,matrix,*,budget=None,check=None):
        self.budget=budget or CertificationBudget()
        self.started=time.monotonic();self.check=check
        self.updates=0;self.calls=0;self.peak_entries=0;self.peak_bits=0;self.two_pivots=0
        self.cache={}
        self._guard()
        if not sparse.issparse(matrix) or matrix.ndim!=2 or matrix.shape[0]!=matrix.shape[1] or not matrix.shape[0]:
            raise CertificateUnavailable('invalid_sparse_shape')
        self.n=matrix.shape[0]
        if self.n>self.budget.max_n:raise CertificateUnavailable('dimension_budget')
        if matrix.nnz>self.budget.max_nnz:raise CertificateUnavailable('input_nonzero_budget')
        a=matrix.tocsr(copy=False)
        if not a.has_canonical_format:raise CertificateUnavailable('canonical_input_required')
        self.rows=[{} for _ in range(self.n)]
        for row in range(self.n):
            for index in range(a.indptr[row],a.indptr[row+1]):
                value=_q(a.data[index]);self._guard(value=value)
                col=int(a.indices[index])
                if not 0<=col<self.n:raise CertificateUnavailable('invalid_sparse_index')
                if value:self.rows[row][col]=value
        self._fill(self.rows)
        for row,entries in enumerate(self.rows):
            for col,value in entries.items():
                if value!=self.rows[col].get(row,_ZERO).conjugate():
                    raise CertificateUnavailable('exact_hermitian_required')

    def _guard(self,*,value=None,update=False):
        if update:
            self.updates+=1
            if self.updates>self.budget.max_updates:raise CertificateUnavailable('operation_budget')
        if time.monotonic()-self.started>=self.budget.max_seconds:raise CertificateUnavailable('time_budget')
        if value is not None:
            bits=max(item.bit_length() for part in (value.real,value.imag) for item in (part.numerator,part.denominator))
            self.peak_bits=max(self.peak_bits,bits)
            if bits>self.budget.max_bits:raise CertificateUnavailable('bit_budget')
        if self.check is not None and (not update or self.updates%64==0):self.check()

    def _fill(self,rows):
        entries=sum(len(row) for row in rows)
        self.peak_entries=max(self.peak_entries,entries)
        if entries>self.budget.max_entries:raise CertificateUnavailable('fill_budget')

    def statistics(self):
        return {'arithmetic':'exact_binary64_gaussian_rationals','budgets':asdict(self.budget),
                'inertia_evaluations':self.calls,'schur_updates':self.updates,
                'peak_entries':self.peak_entries,'peak_fraction_bits':self.peak_bits,
                'two_by_two_pivots':self.two_pivots,'seconds':time.monotonic()-self.started}

    def inertia(self,point):
        point=_fraction(point);self._guard(value=_Q(point))
        if point in self.cache:return self.cache[point]
        self.calls+=1
        if self.calls>self.budget.max_inertias:raise CertificateUnavailable('inertia_evaluation_budget')
        rows=[dict(row) for row in self.rows]
        for index,row in enumerate(rows):
            value=row.get(index,_ZERO)-_Q(point)
            self._guard(value=value)
            if value:row[index]=value
            else:row.pop(index,None)
        self._fill(rows)
        remaining=set(range(self.n));negative=positive=zero=0
        while remaining:
            self._guard()
            pivot=next((i for i in sorted(remaining) if rows[i].get(i,_ZERO)),None)
            if pivot is not None:
                diagonal=rows[pivot][pivot]
                if diagonal.imag:raise CertificateUnavailable('nonreal_exact_pivot')
                negative+=int(diagonal.real<0);positive+=int(diagonal.real>0)
                neighbors=sorted(i for i in remaining if i!=pivot and rows[i].get(pivot,_ZERO))
                for offset,i in enumerate(neighbors):
                    factor=rows[i][pivot]/diagonal
                    self._guard(value=factor)
                    for j in neighbors[offset:]:
                        value=rows[i].get(j,_ZERO)-factor*rows[j][pivot].conjugate()
                        self._store(rows,i,j,value)
                self._remove(rows,remaining,[pivot])
            else:
                edge=next(((i,j) for i in sorted(remaining) for j in sorted(rows[i]) if j in remaining and j!=i),None)
                if edge is None:
                    zero+=len(remaining);break
                p,q=edge;z=rows[p][q]
                self.two_pivots+=1;negative+=1;positive+=1
                neighbors=sorted(i for i in remaining if i not in edge and (rows[i].get(p,_ZERO) or rows[i].get(q,_ZERO)))
                for offset,i in enumerate(neighbors):
                    first=rows[i].get(q,_ZERO)/z
                    second=rows[i].get(p,_ZERO)/z.conjugate()
                    self._guard(value=first);self._guard(value=second)
                    for j in neighbors[offset:]:
                        correction=first*rows[j].get(p,_ZERO).conjugate()+second*rows[j].get(q,_ZERO).conjugate()
                        self._store(rows,i,j,rows[i].get(j,_ZERO)-correction)
                self._remove(rows,remaining,[p,q])
        result=(negative,zero,positive)
        self.cache[point]=result
        return result

    def _store(self,rows,i,j,value):
        self._guard(value=value,update=True)
        if value:
            rows[i][j]=value
            if i!=j:rows[j][i]=value.conjugate()
        else:
            rows[i].pop(j,None)
            if i!=j:rows[j].pop(i,None)
        self._fill(rows)

    @staticmethod
    def _remove(rows,remaining,pivots):
        for pivot in pivots:
            remaining.remove(pivot)
            for row in rows:row.pop(pivot,None)
            rows[pivot].clear()

    def count_interval(self,lower,upper):
        """Exact membership in the configured half-open interval [lower,upper)."""
        lower,upper=_fraction(lower),_fraction(upper)
        if lower>=upper:raise CertificateUnavailable('invalid_interval')
        return self.inertia(upper)[0]-self.inertia(lower)[0]

    def count_closed(self,lower,upper):
        left=self.inertia(lower);right=self.inertia(upper)
        return right[0]+right[1]-left[0]


def _residual_enclosures(oracle,values,vectors):
    """Rational L1 residual / component-norm lower bound encloses a true root."""
    columns=[]
    for value,vector in zip(values,vectors.T):
        theta=_fraction(value);x=[_q(item) for item in vector]
        lower=max((max(abs(item.real),abs(item.imag)) for item in x),default=Fraction(0))
        if not lower:raise CertificateUnavailable('zero_candidate_vector')
        residual=Fraction(0)
        for row,entries in enumerate(oracle.rows):
            answer=_ZERO
            for col,entry in entries.items():answer=answer+entry*x[col]
            answer=answer-_Q(theta)*x[row]
            oracle._guard(value=answer,update=True)
            residual+=answer.l1()
        radius=residual/lower
        # Do not let a huge unrelated matrix entry authorize a wrong tiny
        # eigenvalue. This is an additional relative enclosure requirement;
        # it never relaxes the existing original-problem residual thresholds.
        value_budget=value_fidelity_budget(oracle.n,theta)
        if radius>value_budget:raise CertificateUnavailable('candidate_value_enclosure_too_wide')
        columns.append({'theta':theta,'low':theta-radius,'high':theta+radius,
                        'residual':residual,'norm_lower':lower,'vector':x})
    return columns


def _verify_group(oracle,group):
    low=min(item['low'] for item in group);high=max(item['high'] for item in group)
    if oracle.count_closed(low,high)!=len(group):raise CertificateUnavailable('candidate_cluster_count_mismatch')
    # A root-free collar provides an exact gap to the complement eigenspace.
    radius=max(item['residual']/item['norm_lower'] for item in group)
    magnitude=max(abs(low),abs(high),Fraction(1,2**1022))
    padding=max(radius*65536,magnitude/Fraction(2**32))
    outer_low,outer_high=low-padding,high+padding
    if oracle.count_closed(outer_low,outer_high)!=len(group):raise CertificateUnavailable('unresolved_cluster_separation')
    leakage=Fraction(0);angle=Fraction(0)
    for item in group:
        gap=min(item['theta']-outer_low,outer_high-item['theta'])
        if gap<=0:raise CertificateUnavailable('unresolved_cluster_separation')
        leakage+=item['residual']/gap
        angle=max(angle,item['residual']/(gap*item['norm_lower']))
    if angle>Fraction(1,10000):raise CertificateUnavailable('mode_subspace_not_verified')
    # Exact strict diagonal dominance bounds the smallest Gram eigenvalue.
    # ||(I-P)V||_2 <= sum_j ||r_j||_1/gap_j. A bound below sqrt(lambda_min(V*V))
    # makes projection injective; matching inertia dimension proves coverage.
    gram_lower=None
    for i,left in enumerate(group):
        diagonal=Fraction(0);off=Fraction(0)
        for j,right in enumerate(group):
            inner=_ZERO
            for x,y in zip(left['vector'],right['vector']):inner=inner+x.conjugate()*y
            oracle._guard(value=inner,update=True)
            if i==j:
                if inner.imag:raise CertificateUnavailable('invalid_candidate_gram')
                diagonal=inner.real
            else:off+=inner.l1()
        bound=diagonal-off
        gram_lower=bound if gram_lower is None else min(gram_lower,bound)
    if gram_lower is None or gram_lower<=0 or leakage**2>=gram_lower:
        raise CertificateUnavailable('candidate_subspace_rank_unproved')
    return low,high,angle


def certify_sparse(matrix,values,vectors,config,*,budget=None,check=None):
    """Prove target membership/coverage or explain the bounded refusal.

    The exact matrix/shift counts are independent of all candidate values.
    Candidates only propose disjoint root enclosures, each checked by inertia
    and an original-matrix residual/Gram subspace proof.
    """
    result={'target_certification':'UNVERIFIED','certification_source':'bounded_exact_rational_inertia_v1',
            'boundary_cluster_cut':False,'expected_count':None,'missing_count':None,'endpoint_ambiguous':False}
    oracle=None
    try:
        oracle=InertiaOracle(matrix,budget=budget,check=check)
        values=np.asarray(values);vectors=np.asarray(vectors)
        if values.dtype.kind not in 'fci' or vectors.dtype.kind not in 'fci' or values.ndim!=1 or vectors.shape!=(oracle.n,len(values)) or not np.isfinite(values).all() or not np.isfinite(vectors).all():
            raise CertificateUnavailable('invalid_candidate_shape')
        if np.iscomplexobj(values) and np.any(values.imag!=0):raise CertificateUnavailable('nonreal_candidate_values')
        values=values.real
        target=config['target'];count=len(values)
        if target['mode']=='interval_all':
            interval=target['interval'];lower,upper=_fraction(interval['lower']),_fraction(interval['upper'])
            expected=oracle.count_interval(lower,upper)
            result.update(expected_count=expected,missing_count=max(0,expected-count))
            if expected>target['max_modes']:raise CertificateUnavailable('target_exceeds_mode_budget')
            if count!=expected:raise CertificateUnavailable('target_count_mismatch')
        else:
            result['expected_count']=target['k'];result['missing_count']=max(0,target['k']-count)
            if count!=target['k']:raise CertificateUnavailable('fixed_candidate_count_required')
            origin=_fraction(target['sigma'])
            order=sorted(range(count),key=lambda i:(abs(_fraction(values[i])-origin),_fraction(values[i]),i))
            if order!=list(range(count)):raise CertificateUnavailable('candidate_target_order')
        columns=_residual_enclosures(oracle,values,vectors)
        groups=[]
        for item in sorted(columns,key=lambda column:column['low']):
            if groups and item['low']<=max(previous['high'] for previous in groups[-1]):groups[-1].append(item)
            else:groups.append([item])
        windows=[_verify_group(oracle,group) for group in groups]
        if target['mode']=='interval_all':
            if any(low<lower or high>=upper for low,high,_ in windows):
                result['endpoint_ambiguous']=True
                raise CertificateUnavailable('candidate_endpoint_membership_unproved')
        else:
            radius=max((max(abs(low-origin),abs(high-origin)) for low,high,_ in windows),default=Fraction(0))
            neighborhood=oracle.count_closed(origin-radius,origin+radius)
            result['certification_window_count']=neighborhood
            if neighborhood!=count:raise CertificateUnavailable('nearer_or_boundary_modes_missing')
        result.update(target_certification='CERTIFIED',missing_count=0,
                      mode_subspace_angle_bound_max=float(max((angle for _,_,angle in windows),default=Fraction(0))),
                      mode_subspace_angle_threshold=1e-4)
    except CertificateUnavailable as error:
        result['certification_limit']=str(error)
    finally:
        result['certification_arithmetic']=oracle.statistics() if oracle is not None else {'budgets':asdict(budget or CertificationBudget())}
    return result
