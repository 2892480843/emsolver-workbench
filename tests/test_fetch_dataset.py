"""Focused offline checks for the public-matrix retrieval boundary."""
import io
import http.client
import json
import tarfile
import urllib.error
import urllib.request

import pytest

from scripts import fetch_dataset as dataset


class Response(io.BytesIO):
    def __init__(self, value, url, declared=None):
        super().__init__(value)
        self.url = url
        self.headers = {} if declared is None else {'Content-Length': str(declared)}


def test_unreachable_primary_uses_only_declared_https_mirror(tmp_path, monkeypatch):
    calls = []

    class Opener:
        def open(self, request, timeout):
            calls.append(request.full_url)
            if len(calls) == 1:
                raise urllib.error.URLError('timed out')
            return Response(b'original', request.full_url, 8)

    monkeypatch.setattr(urllib.request, 'build_opener', lambda *_: Opener())
    destination = tmp_path / 'matrix.tar.gz'
    result = dataset.download_archive('gsm_106857', destination, 100)
    assert destination.read_bytes() == b'original'
    assert calls == [
        'https://sparse-files.engr.tamu.edu/MM/Dziekonski/gsm_106857.tar.gz',
        'https://www.cise.ufl.edu/research/sparse/MM/Dziekonski/gsm_106857.tar.gz',
    ]
    assert result['download_url'] == calls[-1]
    assert result['download_attempts'][0]['status'] == 'FAILED'
    assert not destination.with_suffix('.part').exists()


@pytest.mark.parametrize('declared', [None, 101])
def test_download_budget_blocks_oversized_response_and_removes_partial(tmp_path, monkeypatch, declared):
    class Opener:
        def open(self, request, timeout):
            return Response(b'x' * 101, request.full_url, declared)

    monkeypatch.setattr(urllib.request, 'build_opener', lambda *_: Opener())
    destination = tmp_path / 'matrix.tar.gz'
    with pytest.raises(ValueError, match='DOWNLOAD_BUDGET_EXCEEDED'):
        dataset.download_archive('gsm_106857', destination, 100)
    assert not destination.exists()
    assert not destination.with_suffix('.part').exists()


def test_https_redirect_handler_rejects_downgrade_before_following():
    handler = dataset.HTTPSOnlyRedirectHandler()
    request = urllib.request.Request('https://sparse-files.engr.tamu.edu/a')
    with pytest.raises(ValueError, match='INSECURE_REDIRECT'):
        handler.redirect_request(request, None, 301, '', {}, 'http://example.org/a')


def test_expected_matrix_is_copied_without_extracting_archive_paths(tmp_path):
    archive = tmp_path / 'test.tar.gz'
    content = b'%%MatrixMarket matrix coordinate real symmetric\n2 2 1\n1 1 3\n'
    with tarfile.open(archive, 'w:gz') as output:
        for name, value in [('../../outside', b'evil'), ('sample/sample.mtx', content)]:
            member = tarfile.TarInfo(name)
            member.size = len(value)
            output.addfile(member, io.BytesIO(value))
    destination = tmp_path / 'sample.mtx'
    dataset.copy_matrix_market(archive, 'sample', destination)
    assert destination.read_bytes() == content
    assert set(path.name for path in tmp_path.iterdir()) == {'test.tar.gz', 'sample.mtx'}


def test_duplicate_or_oversized_matrix_member_is_rejected(tmp_path):
    archive = tmp_path / 'test.tar.gz'
    with tarfile.open(archive, 'w:gz') as output:
        for _ in range(2):
            member = tarfile.TarInfo('sample/sample.mtx')
            member.size = 1
            output.addfile(member, io.BytesIO(b'x'))
    with pytest.raises(ValueError, match='INVALID_MATRIX_ARCHIVE'):
        dataset.copy_matrix_market(archive, 'sample', tmp_path / 'sample.mtx')
    with pytest.raises(ValueError, match='ARCHIVE_BUDGET_EXCEEDED'):
        dataset.copy_matrix_market(archive, 'sample', tmp_path / 'sample.mtx', max_expanded_bytes=1)


def test_conversion_retains_original_and_reconstructs_only_declared_storage(tmp_path, monkeypatch):
    from scipy import sparse

    monkeypatch.setitem(dataset.CATALOG, 'sample', {'group': 'Test', 'n': 2, 'nnz': 4, 'hermitian_candidate': True})
    archive = tmp_path / 'sample.tar.gz'
    content = b'%%MatrixMarket matrix coordinate real symmetric\n% Original author credit\n2 2 3\n1 1 3\n2 1 2\n2 2 -1\n'
    with tarfile.open(archive, 'w:gz') as output:
        member = tarfile.TarInfo('sample/sample.mtx')
        member.size = len(content)
        output.addfile(member, io.BytesIO(content))
    before = archive.read_bytes()
    result = dataset.convert_archive('sample', tmp_path, archive, {'download_url': 'https://example.org/sample.tar.gz'})
    assert archive.read_bytes() == before
    assert (tmp_path / 'sample.mtx').read_bytes() == content
    assert sparse.load_npz(tmp_path / 'sample.npz').toarray().tolist() == [[3, 2], [2, -1]]
    assert result['archive_sha256'] == dataset.sha256(archive)
    assert json.loads((tmp_path / 'import-metadata.json').read_text())['license'] == 'CC-BY-4.0'
    with pytest.raises(FileExistsError):
        dataset.convert_archive('sample', tmp_path, archive, {})


def test_nonregular_matrix_member_is_rejected(tmp_path):
    archive = tmp_path / 'test.tar.gz'
    with tarfile.open(archive, 'w:gz') as output:
        member = tarfile.TarInfo('sample/sample.mtx')
        member.type = tarfile.SYMTYPE
        member.linkname = '/etc/passwd'
        output.addfile(member)
    with pytest.raises(ValueError, match='INVALID_MATRIX_ARCHIVE'):
        dataset.copy_matrix_market(archive, 'sample', tmp_path / 'sample.mtx')


def test_incomplete_declared_body_is_not_published(tmp_path, monkeypatch):
    class Opener:
        def open(self, request, timeout):
            return Response(b'partial', request.full_url, 20)

    monkeypatch.setattr(urllib.request, 'build_opener', lambda *_: Opener())
    destination = tmp_path / 'matrix.tar.gz'
    with pytest.raises(RuntimeError, match='INCOMPLETE_DOWNLOAD'):
        dataset.download_archive('gsm_106857', destination, 100)
    assert not destination.exists()
    assert not destination.with_suffix('.part').exists()


def test_connection_interrupted_during_body_falls_back_to_official_mirror(tmp_path, monkeypatch):
    class InterruptedResponse(Response):
        def read(self, size):
            raise http.client.IncompleteRead(b'partial', 10)

    class Opener:
        calls = 0

        def open(self, request, timeout):
            self.calls += 1
            if self.calls == 1:
                return InterruptedResponse(b'', request.full_url)
            return Response(b'complete', request.full_url)

    monkeypatch.setattr(urllib.request, 'build_opener', lambda *_: Opener())
    destination = tmp_path / 'matrix.tar.gz'
    result = dataset.download_archive('gsm_106857', destination, 100)
    assert destination.read_bytes() == b'complete'
    assert result['download_attempts'][0]['error_type'] == 'IncompleteRead'


@pytest.mark.parametrize('expected_n,expected_nnz', [(3, 3), (2, 4)])
def test_conversion_rejects_changed_shape_or_nonzero_count(tmp_path, monkeypatch, expected_n, expected_nnz):
    monkeypatch.setitem(dataset.CATALOG, 'sample', {
        'group': 'Test', 'n': expected_n, 'nnz': expected_nnz, 'hermitian_candidate': True,
    })
    archive = tmp_path / 'sample.tar.gz'
    content = b'%%MatrixMarket matrix coordinate real symmetric\n2 2 2\n1 1 3\n2 1 2\n'
    with tarfile.open(archive, 'w:gz') as output:
        member = tarfile.TarInfo('sample/sample.mtx')
        member.size = len(content)
        output.addfile(member, io.BytesIO(content))
    with pytest.raises(ValueError, match='UNEXPECTED_MATRIX_STRUCTURE'):
        dataset.convert_archive('sample', tmp_path, archive, {})
    assert not (tmp_path / 'sample.npz').exists()
