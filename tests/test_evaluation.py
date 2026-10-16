import numpy as np

from emsolver.evaluation import evaluate_results


def save_result(folder,values,vectors):
    folder.mkdir();np.save(folder/'eigenvalues.npy',values);np.save(folder/'eigenvectors.npy',vectors)


def test_repeated_eigenspace_rotation_and_order_do_not_fail(tmp_path):
    reference=tmp_path/'reference';candidate=tmp_path/'candidate'
    q=np.eye(5)[:,:3]; angle=.61
    rot=np.array([[np.cos(angle),-np.sin(angle)],[np.sin(angle),np.cos(angle)]])
    changed=q.copy();changed[:,1:3]=q[:,1:3]@rot
    save_result(reference,np.array([-1.,2.,2.]),q)
    save_result(candidate,np.array([2.,-1.,2.]),changed[:,[1,0,2]])
    result=evaluate_results(candidate,reference)
    assert result['passed'] and result['matched_count']==3
    assert result['max_subspace_sine']<1e-8


def test_missing_or_duplicate_direction_cannot_pass(tmp_path):
    reference=tmp_path/'reference';candidate=tmp_path/'candidate'
    q=np.eye(5)[:,:3]
    save_result(reference,np.array([-1.,2.,2.]),q)
    save_result(candidate,np.array([-1.,2.,2.]),q[:,[0,1,1]])
    assert not evaluate_results(candidate,reference)['passed']
