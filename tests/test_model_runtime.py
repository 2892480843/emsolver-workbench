import hashlib
import pytest
from emsolver import model_runtime as model
from emsolver.ai_explainer import LocalModelConfig


def test_pinned_model_verification_refuses_corruption_missing_and_linked_payloads(tmp_path,monkeypatch):
    data=b'controlled model payload';digest=hashlib.sha256(data).hexdigest()
    monkeypatch.setattr(model,'FILES',{'model.safetensors':digest})
    monkeypatch.setattr(model,'WEIGHTS_BYTES',len(data))
    with pytest.raises(ValueError):model.verify_model(tmp_path)
    (tmp_path/'model.safetensors').write_bytes(data)
    evidence=model.verify_model(tmp_path)
    assert evidence['remote_code'] is False and evidence['license']=='Apache-2.0'
    assert evidence['files']['model.safetensors']==digest
    (tmp_path/'model.safetensors').write_bytes(b'corrupted')
    with pytest.raises(ValueError,match='HASH_MISMATCH'):model.verify_model(tmp_path)
    (tmp_path/'model.safetensors').unlink();(tmp_path/'outside').write_bytes(data)
    (tmp_path/'model.safetensors').symlink_to(tmp_path/'outside')
    with pytest.raises(ValueError):model.verify_model(tmp_path)


def test_reviewed_local_config_never_uses_a_private_model_path():
    config=model.local_config()
    assert LocalModelConfig(**config).license_approved
    assert config['model']=='default_model' and '127.0.0.1' in config['endpoint']
    assert model.REVISION in config['model_version']
    for port in [0,True,65536]:
        with pytest.raises(ValueError):model.local_config(port)
