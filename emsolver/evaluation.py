"""Post-run reference evaluator. Never imported by the solver or worker.

This module reads explicitly supplied reference data only after a candidate run;
its output is evidence for the evaluator, not feedback to parameter selection.
"""
from __future__ import annotations

from pathlib import Path
import numpy as np
from scipy.linalg import subspace_angles
from scipy.optimize import linear_sum_assignment


def _read(folder):
    folder=Path(folder)
    values=np.load(folder/'eigenvalues.npy',allow_pickle=False)
    vectors=np.load(folder/'eigenvectors.npy',allow_pickle=False,mmap_mode='r')
    if values.ndim!=1 or vectors.ndim!=2 or vectors.shape[1]!=values.size or values.size>512 or vectors.size>64_000_000:
        raise ValueError('INVALID_REFERENCE_SHAPE')
    if not np.isfinite(values).all() or not np.isfinite(vectors).all():raise ValueError('NONFINITE_RESULT')
    return values,vectors


def evaluate_results(candidate_dir,reference_dir,lambda_atol=1e-8,lambda_rtol=1e-6,angle_tol=1e-4):
    if min(lambda_atol,lambda_rtol,angle_tol)<0:raise ValueError('INVALID_EVALUATION_TOLERANCE')
    values,vectors=_read(candidate_dir);expected,basis=_read(reference_dir)
    if vectors.shape[0]!=basis.shape[0]:raise ValueError('DIMENSION_MISMATCH')
    if len(values)!=len(expected):
        return {'passed':False,'reason':'MODE_COUNT_MISMATCH','candidate_count':len(values),'reference_count':len(expected)}
    if len(values)==0:return {'passed':True,'matched_count':0,'max_subspace_sine':0.,'reference_scope':'provided target set only'}
    costs=abs(expected[:,None]-values[None,:])
    rows,columns=linear_sum_assignment(costs)
    matched=values[columns];actual=vectors[:,columns]
    thresholds=lambda_atol+lambda_rtol*np.maximum(abs(expected),abs(matched))
    matches=costs[rows,columns]<=thresholds
    order=np.argsort(expected,kind='stable');groups=[];current=[]
    for index in order:
        if current:
            previous=current[-1]
            gap=abs(expected[index]-expected[previous])
            if gap>lambda_atol+lambda_rtol*max(abs(expected[index]),abs(expected[previous])):
                groups.append(current);current=[]
        current.append(int(index))
    groups.append(current)
    angles=[];ranks=[]
    for group in groups:
        expected_block=np.asarray(basis[:,group]);candidate_block=np.asarray(actual[:,group])
        rank=int(np.linalg.matrix_rank(candidate_block));reference_rank=int(np.linalg.matrix_rank(expected_block))
        ranks.append(rank==reference_rank==len(group))
        angles.append(float(np.max(np.sin(subspace_angles(expected_block,candidate_block)),initial=0.0)) if ranks[-1] else 1.0)
    norms=np.linalg.norm(actual,axis=0)
    norm_valid=bool(np.all(norms>0))
    gram=(actual/norms).conj().T@(actual/norms) if norm_valid else np.zeros((len(values),len(values)))
    orth=float(np.linalg.norm(gram-np.eye(len(values)),ord=2)) if norm_valid else None
    passed=bool(np.all(matches) and all(ranks) and max(angles)<=angle_tol and norm_valid and orth is not None and orth<=1e-6)
    return {'passed':passed,'matched_count':int(np.sum(matches)),'candidate_count':len(values),'reference_count':len(expected),
            'max_eigenvalue_absolute_error':float(np.max(costs[rows,columns])),
            'max_subspace_sine':max(angles),'clusters':groups,'cluster_ranks_valid':all(ranks),
            'orthogonality_error':orth,'matching':columns.tolist(),'reference_scope':'provided target set only',
            'limitations':['Reference accuracy and target coverage must be established independently; no feedback to solver']}
