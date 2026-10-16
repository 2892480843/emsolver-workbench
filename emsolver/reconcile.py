"""Recover a completed worker only from a hash-checked published numerical run."""
from __future__ import annotations
import json
from pathlib import Path
import re
from .integrity import file_hash


def completed_result(gateway,job,allowed_files):
    try:
        import numpy as np
        if not re.fullmatch(r'[A-Za-z0-9_-]{1,200}',job['id']):return None
        folder=gateway.root/'runs'/job['id']/'artifacts'
        if any(path.is_symlink() for path in (gateway.root/'runs',folder.parent,folder)):return None
        if not folder.resolve().is_relative_to(gateway.root):return None
        def read(name,maximum=1024**2):
            path=folder/name
            if path.is_symlink() or not path.is_file() or path.stat().st_size>maximum:raise ValueError('Invalid published metadata')
            value=json.loads(path.read_text(encoding='utf-8'))
            if not isinstance(value,dict):raise ValueError('Invalid published metadata')
            json.dumps(value,allow_nan=False)
            return value
        manifest=read('manifest.json')
        if manifest.get('status') not in {'SUCCEEDED','PARTIAL'} or manifest.get('input_sha256')!=job['matrix_hash']:return None
        hashes=manifest.get('artifacts')
        if (not isinstance(hashes,dict) or not 1<=len(hashes)<=64 or
            not {'validation.json','metrics.json','eigenvalues.npy','eigenvectors.npy','residuals.npy','config.resolved.yaml'}<=hashes.keys()):return None
        for name,expected in hashes.items():
            if name not in allowed_files or not re.fullmatch(r'[0-9a-f]{64}',expected):return None
            path=folder/name
            if path.is_symlink() or not path.is_file() or file_hash(path)!=expected:return None
        import yaml
        from .contracts import resolve_config
        if resolve_config(yaml.safe_load((folder/'config.resolved.yaml').read_text()))!=resolve_config(job['config']):return None
        matrix=gateway.store.get('matrices',job['matrix_id'])
        if not matrix or file_hash(matrix['_path'])!=job['matrix_hash']:return None
        from .validation_process import verify_bounded
        validation=verify_bounded(matrix['_path'],folder,job['config'])
        if validation is None:return None
        metrics=read('metrics.json')
        if validation.get('numerical_status')!='PASSED':return None
        if manifest['status']=='SUCCEEDED' and validation.get('target_certification')!='CERTIFIED':return None
        values=np.load(folder/'eigenvalues.npy',mmap_mode='r',allow_pickle=False)
        if values.ndim!=1 or len(values)>512 or not np.isfinite(values).all() or len(values)!=manifest.get('n_accepted'):return None
        if manifest['status']=='PARTIAL' and not len(values):return None
        result={'status':manifest['status'],'error_code':manifest.get('error_code'),'eigenvalues':values.tolist(),
                'residuals':validation.get('residuals',[]),'validation':validation,'metrics':metrics,'n_accepted':len(values),
                'attempts':manifest.get('attempts',[])}
        for key,name in (('strategy','strategy.json'),('memory_estimate','memory-estimate.json'),('diagnostics','diagnostics.json')):
            if name in hashes:result[key]=read(name)
        return result
    except Exception:return None
