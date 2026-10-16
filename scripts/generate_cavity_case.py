#!/usr/bin/env python3
"""Generate original CC0 arrays for an explicitly simplified 2D PEC TM_z model.

No external files or network services are read. The closed-form discrete
reference spectrum is separate from solver configuration and model metadata.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import shutil

import numpy as np
from scipy import sparse


GENERATOR_VERSION = 'rectangular-pec-tmz-fd-v1'
MAX_UNKNOWNS = 100000  # Engineering sidecars support at most 100k real nodes.
SPEED_OF_LIGHT = 299792458.


def sha256(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as source:
        for block in iter(lambda: source.read(1024**2), b''): digest.update(block)
    return digest.hexdigest()


def write_json(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False)+'\n', encoding='utf-8')


def create_case(output, *, nx=8, ny=8, width_m=.02, height_m=.01):
    if (type(nx) is not int or type(ny) is not int or nx<2 or ny<2 or nx*ny>MAX_UNKNOWNS or
        type(width_m) not in (int,float) or type(height_m) not in (int,float) or
        not math.isfinite(width_m) or not math.isfinite(height_m) or not 1e-6<=width_m<=1e6 or not 1e-6<=height_m<=1e6):
        raise ValueError('Require integer nx,ny>=2, nx*ny<=100000 (engineering mapping limit, also below 200000), and finite dimensions 1e-6..1e6 metres')
    output = Path(output)
    if output.exists() or output.is_symlink(): raise ValueError('Output must be a new directory; existing data is never overwritten')
    output.mkdir(parents=True, mode=0o700)
    try:
        n=nx*ny
        hx,hy=width_m/(nx+1),height_m/(ny+1)
        tx=sparse.diags([-np.ones(nx-1),2*np.ones(nx),-np.ones(nx-1)],[-1,0,1],format='csr')/hx**2
        ty=sparse.diags([-np.ones(ny-1),2*np.ones(ny),-np.ones(ny-1)],[-1,0,1],format='csr')/hy**2
        # x is the fast coordinate: dof = iy*nx + ix, zero-based interior nodes.
        matrix=(sparse.kron(sparse.eye(ny,format='csr'),tx,format='csr')+
                sparse.kron(ty,sparse.eye(nx,format='csr'),format='csr')).tocsr()
        matrix.sort_indices()
        sparse.save_npz(output/'matrix.npz',matrix)
        matrix_hash=sha256(output/'matrix.npz')
        coordinates=[[(ix+1)*hx,(iy+1)*hy,0.] for iy in range(ny) for ix in range(nx)]
        cells=[]
        if 2*(nx-1)*(ny-1)<=100000:
            for iy in range(ny-1):
                for ix in range(nx-1):
                    a=iy*nx+ix;b=a+1;c=a+nx;d=c+1
                    cells.extend([{'kind':'triangle','nodes':[a,b,d]},{'kind':'triangle','nodes':[a,d,c]}])
        manifest={'schema_version':'1.0','model_version':GENERATOR_VERSION,'model_kind':'user_supplied',
            'matrix_sha256':matrix_hash,'authorized':True,'public':True,
            'source':{'title':'Locally generated educational 2D PEC TM_z finite-difference model; scripts/generate_cavity_case.py',
                      'url':'urn:emsolver:rectangular-pec-tmz-fd:v1'},
            'license':'CC0-1.0','coordinate_units':'m','field_units':'normalized E_z (arbitrary amplitude)',
            'coordinates':coordinates,'dof_nodes':list(range(n)),'dof_components':['z']*n,'cells':cells,
            'frequency_mapping':{'kind':'wavenumber_squared','eigenvalue_units':'m^-2','reviewed':True,
                'wave_speed_m_per_s':SPEED_OF_LIGHT,
                'applicability':'Homogeneous vacuum; 2D scalar TM_z reduction with PEC Dirichlet E_z=0, z-invariant or beta_z=0 transverse cutoff. lambda=k_c^2 and f=c*sqrt(lambda)/(2*pi) is the discrete 2D-model/cutoff frequency. Not a general 3D cavity or industrial device validation.'},
            'nullspace':None}
        write_json(output/'engineering.json',manifest)
        k=min(6,n-2)
        config={'schema_version':'1.0','problem':{'type':'standard_hermitian','matrix_id':'','storage':'full','scalar':'float64'},
            'target':{'mode':'nearest_k','k':k,'sigma':0.},
            'solver':{'backend':'scipy','method':'arpack','transform':'shift_invert','subspace_max':min(n,max(20,2*k+1)),
                      'adaptive_shift':{'policy':'observed_ritz_gap_v2'}},
            'resources':{'threads':1,'memory_limit_gib':1.,'disk_limit_gib':.5,'walltime_seconds':60}}
        write_json(output/'config.json',config)
        reference=output/'reference'
        reference.mkdir(mode=0o700)
        # This is an analytic DISCRETE spectrum, not a solver invocation. It
        # includes every tensor-product mode, retaining repeated eigenvalues.
        x_values=4/hx**2*np.sin(np.arange(1,nx+1)*np.pi/(2*(nx+1)))**2
        y_values=4/hy**2*np.sin(np.arange(1,ny+1)*np.pi/(2*(ny+1)))**2
        eigenvalues=np.sort((y_values[:,None]+x_values[None,:]).reshape(-1))
        np.save(reference/'eigenvalues.npy',eigenvalues,allow_pickle=False)
        names=['matrix.npz','engineering.json','config.json','reference/eigenvalues.npy']
        source={'schema_version':'1.0','generator_version':GENERATOR_VERSION,'generated_at':datetime.now(timezone.utc).isoformat(),
            'generator_script':'scripts/generate_cavity_case.py','generator_script_sha256':sha256(__file__),
            'data_license':'CC0-1.0','data_origin':'Original arrays and model metadata generated locally from the stated equation; no downloaded geometry or dataset',
            'physical_scope':'2D scalar PEC TM_z finite-difference teaching model','industrial_validation':False,
            'geometry':{'nx':nx,'ny':ny,'interior_nodes':n,'width_m':width_m,'height_m':height_m,'hx_m':hx,'hy_m':hy,
                        'boundary':'PEC E_z=0; boundary nodes eliminated from unknowns',
                        'ordering':'iy*nx+ix, x varying fastest',
                        'topology':'Interior grid triangles for visualization only; operator uses a five-point finite-difference stencil' if cells else 'Topology omitted to preserve the engineering cell budget; actual interior coordinates retained'},
            'equation':{'operator':'negative 2D Dirichlet Laplacian','matrix_units':'m^-2','unknown':'normalized E_z','lambda':'k_c^2','frequency':'c*sqrt(lambda)/(2*pi)',
                        'assumptions':['vacuum','perfectly conducting side walls','2D scalar TM_z reduction','z invariant / transverse cutoff beta_z=0','no conductor or dielectric loss','not a 3D device mesh']},
            'reference':{'file':'reference/eigenvalues.npy','kind':'analytic discrete tensor-product spectrum','solver_visible':False,
                         'formula':'4/hx^2*sin^2(p*pi/(2*(nx+1))) + 4/hy^2*sin^2(q*pi/(2*(ny+1))); 1<=p<=nx, 1<=q<=ny',
                         'scope':'Exact eigenvalues of the specified discrete operator up to floating-point roundoff; not the continuous or measured device spectrum'},
            'theory_sources':[
                {'title':'INL MOOSE Waveguide Eigenvalue Benchmark','url':'https://mooseframework.inl.gov/releases/moose/2022-06-13/modules/electromagnetics/benchmarks/WaveguideEigenvalue.html'},
                {'title':'MIT 18.330 numerical analysis notes, Laurent Demanet, draft April 25 2014','url':'https://math.mit.edu/icg/resources/teaching/18.330/18.330-Apr-2014.pdf'}],
            'files':{name:sha256(output/name) for name in names}}
        write_json(output/'source-manifest.json',source)
        (output/'DATA-LICENSE.txt').write_text('The locally generated arrays and model metadata in this case are dedicated to the public domain under CC0 1.0 Universal: https://creativecommons.org/publicdomain/zero/1.0/\nThe generator source retains the repository software license. No third-party data is redistributed.\n',encoding='utf-8')
        for name in [*names,'source-manifest.json','DATA-LICENSE.txt']:(output/name).chmod(0o400)
        return {'status':'generated','generator_version':GENERATOR_VERSION,'n':n,'nnz':int(matrix.nnz),
                'matrix_sha256':matrix_hash,'files':names+['source-manifest.json','DATA-LICENSE.txt'],
                'data_license':'CC0-1.0','industrial_validation':False,'reference_is_separate':True}
    except BaseException:
        shutil.rmtree(output)
        raise


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,required=True,help='New output directory (never overwritten)')
    parser.add_argument('--nx',type=int,default=8)
    parser.add_argument('--ny',type=int,default=8)
    parser.add_argument('--width-m',type=float,default=.02)
    parser.add_argument('--height-m',type=float,default=.01)
    args=parser.parse_args()
    try:result=create_case(args.output,nx=args.nx,ny=args.ny,width_m=args.width_m,height_m=args.height_m)
    except (ValueError,OSError) as error:parser.error(str(error))
    print(json.dumps(result,ensure_ascii=False,allow_nan=False))


if __name__=='__main__':main()
