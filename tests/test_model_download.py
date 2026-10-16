import importlib.util
from pathlib import Path

import pytest


SPEC=importlib.util.spec_from_file_location('model_download',Path(__file__).resolve().parents[1]/'scripts/download_local_model.py')
module=importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(module)


def test_range_response_validation_uses_final_redirect_response_and_exact_offsets():
    headers='HTTP/1.1 302 Found\r\nLocation: https://cdn.example/signed\r\n\r\nHTTP/2 206\r\nContent-Range: bytes 12-29/100\r\nContent-Length: 18\r\n\r\n'
    assert module.valid_range(headers,12,29,100,8) is True
    for changed in [headers.replace('206','200'),headers.replace('12-29','0-29'),headers.replace('/100','/101'),headers.replace('Content-Range: bytes 12-29/100','')]:
        assert module.valid_range(changed,12,29,100,8) is False
    assert module.valid_range(headers,12,29,100,19) is False
    assert module.valid_range(headers,12,29,100,0) is False


def test_partition_covers_remaining_bytes_exactly_without_overlap():
    parts=module.partition(31,100,4)
    assert parts[0][0]==31 and parts[-1][1]==99
    assert all(right[0]==left[1]+1 for left,right in zip(parts,parts[1:]))
    assert sum(end-start+1 for start,end in parts)==69
    assert module.partition(100,100,4)==[]
    with pytest.raises(ValueError):module.partition(101,100,4)
