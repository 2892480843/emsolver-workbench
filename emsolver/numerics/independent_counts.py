"""Exact, scalable counts for Hermitian matrices with separated row disks.

This is a conservative proof, not floating LDL pivot classification. All
original IEEE coefficients are interpreted as exact dyadic rationals. Enlarged
Gershgorin disks have rational radii sum(|real|+|imag|). A separated union of k
disks contains exactly k eigenvalues, including multiplicity. Overlap refuses
certification; no general sparse spectral-counting promise is made.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from fractions import Fraction
import heapq
import hashlib
import math
import time

import numpy as np
from scipy import sparse

from .accuracy import value_fidelity_budget
from .certification import CertificateUnavailable, _Q, _ZERO


@dataclass(frozen=True)
class GappedCountBudget:
    max_n: int = 1_000_000
    max_nnz: int = 30_000_000
    max_modes: int = 16
    max_vector_entries: int = 2_000_000
    max_operations: int = 5_000_000
    max_bits: int = 16384
    max_count_calls: int = 32
    max_seconds: float = 5.

    def validate(self):
        for key in ('max_n', 'max_nnz', 'max_modes', 'max_vector_entries', 'max_operations', 'max_bits', 'max_count_calls'):
            if type(getattr(self, key)) is not int or getattr(self, key) < 0: raise CertificateUnavailable('invalid_certification_budget')
        if (type(self.max_seconds) not in (int, float) or not math.isfinite(self.max_seconds) or not 0 <= self.max_seconds <= 30 or
            self.max_n > 2_000_000 or self.max_nnz > 30_000_000 or self.max_modes > 32 or self.max_vector_entries > 8_000_000 or
            self.max_bits > 65536 or self.max_operations > 100_000_000 or self.max_count_calls > 256):
            raise CertificateUnavailable('invalid_certification_budget')


_DTYPES = {np.dtype('float32'), np.dtype('float64'), np.dtype('complex64'), np.dtype('complex128')}
_WEIGHT_POLICY = 'row_component_inverse_sqrt_power2_v1'
_WEIGHT_POLICIES = {'uniform', _WEIGHT_POLICY}


def _point(value):
    if isinstance(value, Fraction): return value
    if type(value) not in (float, int) or not math.isfinite(value): raise CertificateUnavailable('invalid_count_point')
    return Fraction(value)


def _encoded(value):
    return {'numerator': str(value.numerator), 'denominator': str(value.denominator)}


class GappedCountOracle:
    """Certified inertia only where every exact row disk avoids the point.

    An isolated row of radius zero at the point contributes an exact zero.
    Nontrivial disks touching the point remain unknown, even if a floating
    factorization would classify them as positive or negative.
    """
    def __init__(self, matrix, *, budget=None, check=None, early_target=None, weight_policy='uniform'):
        self.budget = budget or GappedCountBudget()
        if not isinstance(self.budget, GappedCountBudget): raise CertificateUnavailable('invalid_certification_budget')
        self.budget.validate()
        self.check = check; self.started = time.monotonic()
        self.operations = self.calls = self.rows_processed = self.peak_bits = 0
        self.cache = {}; self.scalar_cache = {}; self.centers = []; self.radii = []
        if weight_policy not in _WEIGHT_POLICIES: raise CertificateUnavailable('unsupported_weight_policy')
        self.weight_policy = weight_policy; self.weight_info = {}
        self._guard()
        if not isinstance(matrix, (sparse.csr_matrix, sparse.csr_array)) or matrix.ndim != 2 or matrix.shape[0] != matrix.shape[1] or not matrix.shape[0]:
            raise CertificateUnavailable('canonical_sparse_csr_required')
        self.n = matrix.shape[0]
        if self.n > self.budget.max_n: raise CertificateUnavailable('dimension_budget')
        if weight_policy != 'uniform' and self.n > 1_000_000: raise CertificateUnavailable('weighted_dimension_budget')
        if matrix.nnz > self.budget.max_nnz: raise CertificateUnavailable('input_nonzero_budget')
        # Each original coefficient and each row must be scanned to derive
        # weights. Refuse a provably impossible pass before native scans,
        # sparse Hermitian comparison or allocating the exponent array.
        if weight_policy != 'uniform' and self.n + matrix.nnz > self.budget.max_operations:
            raise CertificateUnavailable('operation_budget')
        if (matrix.dtype not in _DTYPES or len(matrix.indptr) != self.n + 1 or matrix.indptr[0] != 0 or
            matrix.indptr[-1] != len(matrix.data) or len(matrix.indices) != len(matrix.data) or
            matrix.indices.dtype.kind not in 'iu' or matrix.indptr.dtype.kind not in 'iu' or
            np.any(matrix.indptr[1:] < matrix.indptr[:-1]) or np.any(matrix.indices < 0) or np.any(matrix.indices >= self.n) or
            not matrix.has_canonical_format or not np.isfinite(matrix.data).all()):
            raise CertificateUnavailable('invalid_canonical_ieee_input')
        # SciPy caches these flags and callers can overwrite them. Inspect the
        # actual row ordering/uniqueness before any CSR conversion or counting.
        if len(matrix.indices) > 1:
            bad = matrix.indices[1:] <= matrix.indices[:-1]
            boundaries = matrix.indptr[1:-1]
            boundaries = boundaries[(boundaries > 0) & (boundaries < len(matrix.indices))]
            bad[boundaries - 1] = False
            if np.any(bad): raise CertificateUnavailable('invalid_canonical_ieee_input')
        self.matrix = matrix
        self._guard()
        # Exact entry equality is required. A tolerance or symmetrization would
        # certify a different matrix. The operation stays sparse.
        if (matrix != matrix.conjugate().transpose()).nnz:
            raise CertificateUnavailable('exact_hermitian_required')
        self._guard()
        exponents = self._weights(matrix) if weight_policy != 'uniform' else None
        ratios = {}
        covered = 0
        for row in range(self.n):
            center = Fraction(0); radius = Fraction(0)
            for index in range(matrix.indptr[row], matrix.indptr[row + 1]):
                value = matrix.data[index]
                real, imag = self._scalar(float(np.real(value))), self._scalar(float(np.imag(value)))
                if matrix.indices[index] == row:
                    if imag: raise CertificateUnavailable('exact_hermitian_required')
                    center = real
                else:
                    component = abs(real) + abs(imag)
                    if exponents is not None:
                        difference = int(exponents[matrix.indices[index]]) - int(exponents[row])
                        if difference not in ratios:
                            ratios[difference] = Fraction(1 << difference) if difference >= 0 else Fraction(1, 1 << -difference)
                            self._guard(ratios[difference], operation=True)
                        component *= ratios[difference]
                    radius += component
                self._guard(radius, operation=True)
            self.centers.append(center); self.radii.append(radius); self.rows_processed += 1
            self._guard(center - radius, operation=True); self._guard(center + radius)
            if early_target:
                lower, upper = center - radius, center + radius
                if early_target['mode'] == 'nearest_k':
                    origin = early_target['sigma']
                    if lower <= origin <= upper:
                        covered += 1
                        if covered > early_target['k']: raise CertificateUnavailable('too_many_disks_cover_target')
                elif not (upper < early_target['lower'] or lower >= early_target['upper'] or
                          lower >= early_target['lower'] and upper < early_target['upper']):
                    raise CertificateUnavailable('interval_boundary_disk_overlap')

    def _weights(self, matrix):
        """Positive dyadic similarity, derived from original row coefficients.

        Only integer exponents are stored: no floating weights or scaled
        sparse matrix are formed. Chunked maxima keep scratch space bounded
        even for a long row. Charge every scanned coefficient before work.
        """
        self._guard()
        exponents = np.empty(self.n, dtype='<i2')
        for row in range(self.n):
            self._guard(operation=True)
            maximum = 0.
            stop = int(matrix.indptr[row + 1])
            for start in range(int(matrix.indptr[row]), stop, 65536):
                end = min(stop, start + 65536)
                self.operations += end - start
                if self.operations > self.budget.max_operations: raise CertificateUnavailable('operation_budget')
                self._guard()
                part = matrix.data[start:end]
                maximum = max(maximum, float(np.max(np.abs(part.real), initial=0.)),
                    float(np.max(np.abs(part.imag), initial=0.)))
                self._guard()
            exponents[row] = -((math.frexp(maximum)[1] - 1) // 2) if maximum else 0
        self._guard()
        self.weight_info = {'weight_policy': self.weight_policy,
            'weight_exponent_min': int(exponents.min()), 'weight_exponent_max': int(exponents.max()),
            'weight_exponents_sha256': hashlib.sha256(memoryview(exponents)).hexdigest()}
        self._guard()
        return exponents

    def _scalar(self, value):
        if value in self.scalar_cache: return self.scalar_cache[value]
        result = Fraction(value)
        self._guard(result, operation=True)
        if len(self.scalar_cache) < 16384: self.scalar_cache[value] = result
        return result

    def _q(self, value):
        return _Q(self._scalar(float(np.real(value))), self._scalar(float(np.imag(value))))

    def _guard(self, value=None, *, operation=False):
        if operation:
            self.operations += 1
            if self.operations > self.budget.max_operations: raise CertificateUnavailable('operation_budget')
        if value is not None:
            parts = (value.real, value.imag) if isinstance(value, _Q) else (value,)
            bits = max(number.bit_length() for part in parts for number in (part.numerator, part.denominator))
            self.peak_bits = max(self.peak_bits, bits)
            if bits > self.budget.max_bits: raise CertificateUnavailable('bit_budget')
        if not operation or self.operations % 64 == 0:
            if time.monotonic() - self.started >= self.budget.max_seconds: raise CertificateUnavailable('time_budget')
            if self.check is not None: self.check()

    def statistics(self):
        return {'arithmetic': 'exact_binary64_gaussian_rationals', 'budgets': asdict(self.budget),
            'rows_processed': self.rows_processed, 'operations': self.operations, 'count_evaluations': self.calls,
            'peak_fraction_bits': self.peak_bits, 'seconds': time.monotonic() - self.started,
            'radius_bound': ('sum_abs_real_plus_abs_imag; exact dyadic enlargement' if self.weight_policy == 'uniform' else
                'sum_abs_real_plus_abs_imag_times_positive_dyadic_weight_ratio; exact dyadic enlargement'), **self.weight_info}

    def inertia(self, point):
        point = _point(point); self._guard(point)
        if point in self.cache: return self.cache[point]
        self.calls += 1
        if self.calls > self.budget.max_count_calls: raise CertificateUnavailable('count_evaluation_budget')
        negative = zero = positive = 0
        for center, radius in zip(self.centers, self.radii):
            self._guard(operation=True)
            if center + radius < point: negative += 1
            elif center - radius > point: positive += 1
            elif not radius and center == point: zero += 1
            else: raise CertificateUnavailable('disk_crosses_count_point')
        answer = (negative, zero, positive); self.cache[point] = answer
        return answer

    def count_interval(self, lower, upper):
        lower, upper = _point(lower), _point(upper)
        if lower >= upper: raise CertificateUnavailable('invalid_interval')
        return self.inertia(upper)[0] - self.inertia(lower)[0]

    def count_closed(self, lower, upper):
        lower, upper = _point(lower), _point(upper)
        if lower > upper: raise CertificateUnavailable('invalid_interval')
        right, left = self.inertia(upper), self.inertia(lower)
        return right[0] + right[1] - left[0]

    def target_window(self, target):
        self._guard()
        if target['mode'] == 'nearest_k':
            origin, k = _point(target['sigma']), target['k']
            if type(k) is not int or not 1 <= k < self.n: raise CertificateUnavailable('invalid_nearest_target')
            def extent(index):
                self._guard(operation=True)
                center, radius = self.centers[index], self.radii[index]
                return abs(center - origin) + radius
            selected = set(heapq.nsmallest(k, range(self.n), key=lambda index: (extent(index), index)))
            inside = max(abs(self.centers[i] - origin) + self.radii[i] for i in selected)
            outside = None
            for index, (center, radius) in enumerate(zip(self.centers, self.radii)):
                self._guard(operation=True)
                if index in selected: continue
                distance = max(Fraction(0), abs(center - origin) - radius)
                outside = distance if outside is None else min(outside, distance)
            if outside is None or inside >= outside: raise CertificateUnavailable('no_separated_nearest_disk_union')
            radius = (inside + outside) / 2
            lower, upper = origin - radius, origin + radius
            count = self.count_closed(lower, upper)
            if count != k: raise CertificateUnavailable('independent_disk_count_mismatch')
            return lower, upper, count, origin - outside, origin + outside
        lower, upper = _point(target['interval']['lower']), _point(target['interval']['upper'])
        if lower >= upper: raise CertificateUnavailable('invalid_interval')
        selected = 0; left = right = None
        for center, radius in zip(self.centers, self.radii):
            self._guard(operation=True)
            low, high = center - radius, center + radius
            if high < lower: left = high if left is None else max(left, high)
            elif low >= upper: right = low if right is None else min(right, low)
            elif low >= lower and high < upper: selected += 1
            else: raise CertificateUnavailable('interval_boundary_disk_overlap')
        return lower, upper, selected, left, right


def _columns(oracle, values, vectors):
    """Exact sparse matvecs and residual enclosures in original units."""
    oracle._guard()
    matrix = oracle.matrix.tocsc()
    oracle._guard()
    answer = []
    for value, vector in zip(values, vectors.T):
        theta = oracle._scalar(float(value))
        column = {}
        for index in np.flatnonzero(vector):
            column[int(index)] = oracle._q(vector[index]); oracle._guard(column[int(index)], operation=True)
        norm_lower = max((max(abs(item.real), abs(item.imag)) for item in column.values()), default=Fraction(0))
        if not norm_lower: raise CertificateUnavailable('zero_candidate_vector')
        residual = {}
        for col, scalar in column.items():
            for index in range(matrix.indptr[col], matrix.indptr[col + 1]):
                row = int(matrix.indices[index])
                result = residual.get(row, _ZERO) + oracle._q(matrix.data[index]) * scalar
                oracle._guard(result, operation=True)
                if result: residual[row] = result
                else: residual.pop(row, None)
        for row, scalar in column.items():
            result = residual.get(row, _ZERO) - _Q(theta) * scalar
            oracle._guard(result, operation=True)
            if result: residual[row] = result
            else: residual.pop(row, None)
        norm_upper = sum((value.l1() for value in residual.values()), Fraction(0))
        oracle._guard(norm_upper)
        radius = norm_upper / norm_lower
        value_budget = value_fidelity_budget(oracle.n, theta)
        if radius > value_budget: raise CertificateUnavailable('candidate_value_enclosure_too_wide')
        answer.append({'theta': theta, 'radius': radius, 'residual': norm_upper, 'norm_lower': norm_lower, 'vector': column})
    return answer


def _subspace(oracle, columns, lower, upper, outside_left, outside_right):
    leakage = angle = Fraction(0)
    for column in columns:
        theta, radius = column['theta'], column['radius']
        if theta - radius < lower or theta + radius >= upper:
            raise CertificateUnavailable('candidate_endpoint_membership_unproved')
        gaps = []
        if outside_left is not None: gaps.append(theta - outside_left)
        if outside_right is not None: gaps.append(outside_right - theta)
        if not gaps: continue  # The target is the entire matrix eigenspace.
        gap = min(gaps)
        if gap <= 0: raise CertificateUnavailable('unresolved_subspace_separation')
        leakage += column['residual'] / gap
        angle = max(angle, column['residual'] / (gap * column['norm_lower']))
        oracle._guard(leakage); oracle._guard(angle)
    if angle > Fraction(1, 10000): raise CertificateUnavailable('mode_subspace_not_verified')
    gram_lower = None
    for i, left in enumerate(columns):
        diagonal = off = Fraction(0)
        for j, right in enumerate(columns):
            inner = _ZERO
            common = left['vector'].keys() & right['vector'].keys()
            for row in common:
                inner += left['vector'][row].conjugate() * right['vector'][row]
                oracle._guard(inner, operation=True)
            if i == j:
                if inner.imag: raise CertificateUnavailable('invalid_candidate_gram')
                diagonal = inner.real
            else: off += inner.l1()
        bound = diagonal - off
        gram_lower = bound if gram_lower is None else min(gram_lower, bound)
        oracle._guard(bound)
    if gram_lower is None or gram_lower <= 0 or leakage**2 >= gram_lower:
        raise CertificateUnavailable('candidate_subspace_rank_unproved')
    # For Q = X (X*X)^(-1/2), ||(I-P)Q|| <= leakage/sqrt(delta).
    # This also rejects ill-conditioned bases whose individually tiny exterior
    # components become a large principal angle after changing basis.
    square = leakage**2 / gram_lower
    if square > Fraction(1, 10000)**2: raise CertificateUnavailable('mode_subspace_not_verified')
    if square:
        scaled = (square.numerator * 2**160 + square.denominator - 1) // square.denominator
        integer = math.isqrt(scaled)
        if integer**2 < scaled: integer += 1
        angle = max(angle, Fraction(integer, 2**80))
    return angle, gram_lower


def _verify_groups(oracle, columns, lower, upper, outside_left, outside_right):
    """Prove each candidate group's eigenvalue multiplicity independently.

    Whole-target rank alone cannot associate two almost-parallel vectors with
    two different eigenvalues. Separators must have independently countable
    root-free row-disk gaps. A repeated/overlapping candidate enclosure needs
    an additional count inside that tight enclosure, not merely in a larger
    window that could hide a different root.
    """
    groups = []
    for column in sorted(columns, key=lambda item: item['theta'] - item['radius']):
        low = column['theta'] - column['radius']
        if groups and low <= max(item['theta'] + item['radius'] for item in groups[-1]): groups[-1].append(column)
        else: groups.append([column])
    separators = [lower]
    for left, right in zip(groups, groups[1:]):
        high = max(item['theta'] + item['radius'] for item in left)
        low = min(item['theta'] - item['radius'] for item in right)
        middle = (low + high) / 2
        if oracle.inertia(middle)[1]: raise CertificateUnavailable('candidate_separator_not_root_free')
        separators.append(middle)
    separators.append(upper)
    angle = Fraction(0); gram_lower = None
    for index, group in enumerate(groups):
        left, right = separators[index:index + 2]
        if oracle.count_interval(left, right) != len(group): raise CertificateUnavailable('candidate_group_count_mismatch')
        if len(group) > 1:
            low = min(item['theta'] - item['radius'] for item in group)
            high = max(item['theta'] + item['radius'] for item in group)
            if oracle.count_closed(low, high) != len(group): raise CertificateUnavailable('candidate_cluster_count_mismatch')
        group_angle, group_gram = _subspace(oracle, group, left, right,
            outside_left if index == 0 else left, outside_right if index == len(groups) - 1 else right)
        angle = max(angle, group_angle)
        gram_lower = group_gram if gram_lower is None else min(gram_lower, group_gram)
    return angle, gram_lower, len(groups)


def certify_gapped(matrix, values, vectors, config, *, budget=None, check=None, weight_policy='uniform'):
    """Certify only separated targets with an independent exact count and rank.

    Numerical residual acceptance remains the caller's separate responsibility.
    This function neither calls a candidate eigensolver nor reads references.
    """
    limits = budget or GappedCountBudget()
    source = 'exact_dyadic_weighted_gershgorin_v1' if weight_policy == _WEIGHT_POLICY else 'exact_dyadic_gapped_gershgorin_v1'
    result = {'target_certification': 'UNVERIFIED', 'certification_source': source,
        'boundary_cluster_cut': False, 'expected_count': None, 'missing_count': None, 'endpoint_ambiguous': False}
    oracle = None
    try:
        if not isinstance(limits, GappedCountBudget): raise CertificateUnavailable('invalid_certification_budget')
        limits.validate()
        if weight_policy not in _WEIGHT_POLICIES: raise CertificateUnavailable('unsupported_weight_policy')
        if not isinstance(values, np.ndarray) or not isinstance(vectors, np.ndarray) or values.ndim != 1 or vectors.ndim != 2:
            raise CertificateUnavailable('invalid_candidate_shape')
        if len(values) > limits.max_modes or vectors.size > limits.max_vector_entries: raise CertificateUnavailable('candidate_resource_budget')
        target = config['target']
        if not isinstance(target, dict) or target.get('mode') not in {'nearest_k', 'interval_all'}: raise CertificateUnavailable('invalid_target')
        if target['mode'] == 'nearest_k':
            if type(target.get('k')) is not int or not 1 <= target['k'] <= limits.max_modes: raise CertificateUnavailable('mode_budget')
            early = {'mode': 'nearest_k', 'sigma': _point(target['sigma']), 'k': target['k']}
            result['expected_count'] = target['k']
        else:
            early = {'mode': 'interval_all', 'lower': _point(target['interval']['lower']), 'upper': _point(target['interval']['upper'])}
            if early['lower'] >= early['upper']: raise CertificateUnavailable('invalid_interval')
        oracle = GappedCountOracle(matrix, budget=limits, check=check, early_target=early, weight_policy=weight_policy)
        if (values.dtype not in _DTYPES or vectors.dtype not in _DTYPES or values.ndim != 1 or vectors.ndim != 2 or
            vectors.shape != (oracle.n, len(values)) or not np.isfinite(values).all() or not np.isfinite(vectors).all() or
            np.iscomplexobj(values) and np.any(values.imag != 0)):
            raise CertificateUnavailable('invalid_candidate_shape')
        values = values.real
        lower, upper, expected, outside_left, outside_right = oracle.target_window(target)
        result.update(expected_count=expected, missing_count=max(0, expected - len(values)), certification_window_count=expected,
            certification_window={'lower': _encoded(lower), 'upper': _encoded(upper), 'endpoint_semantics': 'half_open; nearest endpoints independently root-free'})
        if expected > limits.max_modes or target['mode'] == 'interval_all' and expected > target.get('max_modes', limits.max_modes):
            raise CertificateUnavailable('target_exceeds_mode_budget')
        if len(values) != expected: raise CertificateUnavailable('target_count_mismatch')
        if target['mode'] == 'nearest_k':
            origin = early['sigma']
            order = sorted(range(len(values)), key=lambda index: (abs(_point(float(values[index])) - origin), _point(float(values[index])), index))
            if order != list(range(len(values))): raise CertificateUnavailable('candidate_target_order')
        if not expected:
            oracle._guard()
            result.update(target_certification='CERTIFIED', missing_count=0, mode_subspace_angle_bound_max=0.)
            return result
        columns = _columns(oracle, values, vectors)
        angle, gram_lower, groups = _verify_groups(oracle, columns, lower, upper, outside_left, outside_right)
        global_angle, global_gram = _subspace(oracle, columns, lower, upper, outside_left, outside_right)
        angle, gram_lower = max(angle, global_angle), min(gram_lower, global_gram)
        oracle._guard()
        # Outward rounding preserves an upper bound even if an extremely small
        # positive exact angle underflows during conversion for display.
        angle_upper = math.nextafter(float(angle), math.inf) if angle else 0.
        result.update(target_certification='CERTIFIED', missing_count=0, mode_subspace_angle_bound_max=angle_upper,
            mode_subspace_angle_threshold=1e-4, candidate_gram_lower_bound=_encoded(gram_lower))
        result['certified_candidate_groups'] = groups
    except CertificateUnavailable as exc:
        result['certification_limit'] = str(exc)
        if 'endpoint' in str(exc) or 'boundary' in str(exc): result['endpoint_ambiguous'] = True
    except (ValueError, TypeError, KeyError, OverflowError) as exc:
        result['certification_limit'] = 'invalid_certification_input'
    finally:
        result['certification_arithmetic'] = oracle.statistics() if oracle else {'budgets': asdict(limits) if isinstance(limits, GappedCountBudget) else None}
    return result


def certify_weighted(matrix, values, vectors, config, *, budget=None, check=None):
    """Use exact positive diagonal similarity disks and the same candidate proof.

    Weights depend solely on original row coefficients. Hermiticity, residuals,
    multiplicity, and subspace checks all refer to the original operator.
    """
    return certify_gapped(matrix, values, vectors, config, budget=budget, check=check, weight_policy=_WEIGHT_POLICY)
