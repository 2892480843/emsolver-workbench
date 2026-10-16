"""Locally generated, authorized scalar TM_z teaching model acceptance."""
import hashlib
import json

import numpy as np
import pytest
from scipy import sparse

from scripts.generate_cavity_case import create_case


def test_generated_operator_matches_independent_stencil_spectrum_and_actual_coordinates(tmp_path):
    output=tmp_path/'case'
    result=create_case(output,nx=3,ny=4,width_m=.03,height_m=.02)
    matrix=sparse.load_npz(output/'matrix.npz')
    nx,ny=3,4
    hx,hy=.03/(nx+1),.02/(ny+1)
    independent=np.zeros((nx*ny,nx*ny))
    for row in range(nx*ny):
        x,y=row%nx,row//nx
        independent[row,row]=2/hx**2+2/hy**2
        for dx,dy,coefficient in [(1,0,-1/hx**2),(-1,0,-1/hx**2),(0,1,-1/hy**2),(0,-1,-1/hy**2)]:
            if 0<=x+dx<nx and 0<=y+dy<ny:independent[row,(y+dy)*nx+x+dx]=coefficient
    np.testing.assert_allclose(matrix@np.eye(12),independent,rtol=1e-14)
    reference=np.load(output/'reference'/'eigenvalues.npy',allow_pickle=False)
    np.testing.assert_allclose(reference,np.linalg.eigvalsh(independent),rtol=2e-14)
    sidecar=json.loads((output/'engineering.json').read_text())
    assert sidecar['coordinates'][0]==[hx,hy,0.]
    assert sidecar['coordinates'][nx-1]==[nx*hx,hy,0.]
    assert sidecar['coordinates'][-1]==[nx*hx,ny*hy,0.]
    assert sidecar['dof_nodes']==list(range(12)) and sidecar['dof_components']==['z']*12
    assert sidecar['matrix_sha256']==hashlib.sha256((output/'matrix.npz').read_bytes()).hexdigest()
    assert sidecar['license']=='CC0-1.0' and sidecar['authorized'] is True and sidecar['public'] is True
    assert sidecar['frequency_mapping']['kind']=='wavenumber_squared'
    source=json.loads((output/'source-manifest.json').read_text())
    assert source['industrial_validation'] is False
    assert source['physical_scope']=='2D scalar PEC TM_z finite-difference teaching model'
    assert source['reference']['solver_visible'] is False
    assert source['files']['engineering.json']==hashlib.sha256((output/'engineering.json').read_bytes()).hexdigest()
    assert str(tmp_path) not in json.dumps(source) and str(tmp_path) not in json.dumps(result)
    with pytest.raises(ValueError):create_case(output)


@pytest.mark.parametrize('arguments',[{'nx':0},{'ny':1},{'nx':True},{'nx':200001},{'nx':400,'ny':300},{'width_m':float('nan')},{'height_m':0},{'width_m':1e20}])
def test_generator_rejects_unbounded_or_invalid_cases_before_writing(tmp_path,arguments):
    output=tmp_path/'refused'
    with pytest.raises(ValueError):create_case(output,**arguments)
    assert not output.exists()


def test_generated_model_registers_freezes_and_solves_without_reference_directory(tmp_path):
    from emsolver.engineering import EngineeringAssets, frequency_values
    from emsolver.service import Service
    service=Service(tmp_path/'data',start_runner=False)
    try:
        case=service.root/'inbox'/'cavity'
        create_case(case,nx=4,ny=4)
        expected=np.load(case/'reference'/'eigenvalues.npy',allow_pickle=False)[:6]
        # Remove external reference data before the solver is invoked. The
        # service validates the original sparse operator independently.
        (case/'reference'/'eigenvalues.npy').unlink()
        (case/'reference').rmdir()
        project=service.create_project('2D educational PEC TM_z')
        matrix=service.import_matrix(project['id'],case/'matrix.npz',metadata={'public':True,'license':'CC0-1.0','units':'m^-2','purpose':'2D scalar PEC TM_z finite-difference teaching example'})
        manifest=json.loads((case/'engineering.json').read_text())
        assets=EngineeringAssets(service)
        model=assets.create(matrix['id'],manifest)
        assets.select(matrix['id'],model['id'])
        plan=service.create_plan(config=json.loads((case/'config.json').read_text())|{'problem':{'matrix_id':matrix['id']}})
        assert 'reference' not in json.dumps(plan['config']).lower()
        job=service.submit_job(plan['id'])
        service._execute(job['id'])
        completed=service.get_job(job['id'])
        assert completed['status']=='SUCCEEDED',completed
        assert completed['engineering_model']['sidecar_sha256']==model['sidecar_sha256']
        result=service.results(job['id'])
        assert result['validation']['numerical_status']=='PASSED'
        np.testing.assert_allclose(sorted(result['eigenvalues']),expected,rtol=1e-10)
        frequencies=frequency_values(expected.tolist(),manifest['frequency_mapping'])['frequencies_hz']
        np.testing.assert_allclose(sorted(result['frequencies_hz']),frequencies,rtol=1e-10)
        preview=assets.field_preview(job['id'],component='z',limit=16)
        assert len(preview['points'])==16 and preview['coordinate_units']=='m'
        assert preview['points'][0]['coordinate']==manifest['coordinates'][0]
        assert set(preview['points'][0]['components'])=={'z'}
        assert preview['points'][0]['complete_vector'] is False
    finally:service.close()


def test_default_grid_is_64_nodes_and_keeps_reference_out_of_configuration(tmp_path):
    output=tmp_path/'default'
    result=create_case(output)
    assert result['n']==64
    assert len(json.loads((output/'engineering.json').read_text())['coordinates'])==64
    assert np.load(output/'reference'/'eigenvalues.npy',allow_pickle=False).shape==(64,)
    config=json.loads((output/'config.json').read_text())
    assert config['solver']['adaptive_shift']['policy']=='observed_ritz_gap_v2'
    assert 'reference' not in json.dumps(config).lower()
