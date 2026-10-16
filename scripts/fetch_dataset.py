#!/usr/bin/env python3
"""Download a declared public matrix, preserve provenance, and convert to sparse NPZ."""
from __future__ import annotations

import argparse
import hashlib
import http.client
import json
from pathlib import Path
import tarfile
import time
import urllib.error
import urllib.parse
import urllib.request


CATALOG = {
    'gsm_106857': {'group': 'Dziekonski', 'n': 589446, 'nnz': 21758924, 'hermitian_candidate': True},
    'vfem': {'group': 'CEMW', 'n': 93476, 'hermitian_candidate': False},
}


class HTTPSOnlyRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Reject a downgrade before urllib can send the redirected request."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if urllib.parse.urlsplit(newurl).scheme.lower() != 'https':
            raise ValueError('INSECURE_REDIRECT')
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def download_archive(name: str, archive: Path, max_download_bytes: int):
    """Try the current host then the collection's original official UF mirror."""
    if max_download_bytes <= 0:
        raise ValueError('INVALID_DOWNLOAD_BUDGET')
    if archive.exists():
        raise FileExistsError(archive)
    item = CATALOG[name]
    suffix = f'MM/{item["group"]}/{name}.tar.gz'
    urls = [f'https://sparse-files.engr.tamu.edu/{suffix}',
            f'https://www.cise.ufl.edu/research/sparse/{suffix}']
    temporary = archive.with_suffix('.part')
    attempts = []
    opener = urllib.request.build_opener(HTTPSOnlyRedirectHandler())
    deadline = time.monotonic() + 600
    try:
        for url in urls:
            temporary.unlink(missing_ok=True)
            try:
                request = urllib.request.Request(url, headers={'User-Agent': 'EMSolver-public-dataset-fetch/0.2'})
                with opener.open(request, timeout=30) as response:
                    if urllib.parse.urlsplit(response.url).scheme.lower() != 'https':
                        raise ValueError('INSECURE_REDIRECT')
                    declared = response.headers.get('Content-Length')
                    if declared is not None and int(declared) > max_download_bytes:
                        raise ValueError('DOWNLOAD_BUDGET_EXCEEDED')
                    count = 0
                    with temporary.open('xb') as output:
                        while chunk := response.read(1024 * 1024):
                            count += len(chunk)
                            if count > max_download_bytes:
                                raise ValueError('DOWNLOAD_BUDGET_EXCEEDED')
                            if time.monotonic() > deadline:
                                raise TimeoutError('DOWNLOAD_WALLTIME_EXCEEDED')
                            output.write(chunk)
                    if declared is not None and count != int(declared):
                        raise OSError('INCOMPLETE_DOWNLOAD')
                    attempts.append({'url': url, 'status': 'OK', 'bytes': count})
                    temporary.replace(archive)
                    return {'download_url': response.url, 'download_attempts': attempts,
                            'download_bytes': count, 'transport': 'HTTPS only; TLS verification enabled'}
            except (urllib.error.URLError, http.client.IncompleteRead, OSError, ValueError) as exc:
                attempts.append({'url': url, 'status': 'FAILED', 'error_type': type(exc).__name__, 'message': str(exc)})
                if isinstance(exc, ValueError) and str(exc) != 'INSECURE_REDIRECT':
                    raise
                if time.monotonic() > deadline:
                    raise TimeoutError('DOWNLOAD_WALLTIME_EXCEEDED') from exc
        raise RuntimeError('ALL_OFFICIAL_HTTPS_SOURCES_FAILED: ' + json.dumps(attempts))
    finally:
        temporary.unlink(missing_ok=True)


def copy_matrix_market(archive: Path, name: str, destination: Path, *, max_expanded_bytes=2 * 1024**3):
    """Copy only the unique declared regular matrix; never extract archive paths."""
    if destination.exists():
        raise FileExistsError(destination)
    temporary = destination.with_suffix('.mtx.part')
    try:
        with tarfile.open(archive, 'r:gz') as compressed:
            members = []
            total = 0
            for index, member in enumerate(compressed):
                total += member.size
                if index >= 1000 or member.size < 0 or total > max_expanded_bytes:
                    raise ValueError('ARCHIVE_BUDGET_EXCEEDED')
                if member.name in (f'{name}.mtx', f'{name}/{name}.mtx'):
                    members.append(member)
            if len(members) != 1 or not members[0].isfile():
                raise ValueError('INVALID_MATRIX_ARCHIVE')
            with compressed.extractfile(members[0]) as source, temporary.open('xb') as output:
                for chunk in iter(lambda: source.read(1024 * 1024), b''):
                    output.write(chunk)
        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)


def sha256(path):
    digest = hashlib.sha256()
    with path.open('rb') as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def convert_archive(name: str, directory: Path, archive: Path, download_record: dict):
    """Convert an acquired original archive without editing its physical matrix."""
    from scipy import io, sparse
    item = CATALOG[name]
    source_url = f'https://sparse.tamu.edu/{item["group"]}/{name}'
    matrix_market = directory / f'{name}.mtx'
    npz = directory / f'{name}.npz'
    for output in (matrix_market, npz, directory / 'provenance.json', directory / 'import-metadata.json'):
        if output.exists():
            raise FileExistsError(output)
    copy_matrix_market(archive, name, matrix_market)
    matrix = io.mmread(matrix_market)
    if not sparse.issparse(matrix) or matrix.shape != (item['n'], item['n']):
        raise ValueError('UNEXPECTED_MATRIX_STRUCTURE')
    normalized = matrix.tocsr()
    normalized.sum_duplicates()
    normalized.sort_indices()
    if 'nnz' in item and normalized.nnz != item['nnz']:
        raise ValueError('UNEXPECTED_MATRIX_STRUCTURE')
    sparse.save_npz(npz, normalized)
    metadata = {'source_url': source_url, **download_record, 'license': 'CC-BY-4.0',
                'license_source': 'https://sparse.tamu.edu/about', 'public': True,
                'description': 'Format conversion only: original Matrix Market preserved, declared symmetric storage reconstructed, duplicate entries combined, CSR indices sorted. No coefficient edits, symmetrization, subsetting or reference answers.',
                'n': normalized.shape[0], 'nnz': normalized.nnz, 'dtype': str(normalized.dtype),
                'hermitian_candidate': item['hermitian_candidate'],
                'archive_sha256': sha256(archive), 'matrix_market_sha256': sha256(matrix_market),
                'npz_sha256': sha256(npz),
                'citation': 'Davis and Hu (2011), doi:10.1145/2049662.2049663; Kolodziej et al. (2019), doi:10.21105/joss.01244; retain the original Matrix Market header for author citations.'}
    (directory / 'provenance.json').write_text(json.dumps(metadata, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    accepted = {key: metadata[key] for key in ('source_url', 'license', 'public', 'description')}
    (directory / 'import-metadata.json').write_text(json.dumps(accepted, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    return metadata


def fetch(name: str, directory: Path, max_download_bytes: int):
    directory.mkdir(parents=True, exist_ok=False)
    archive = directory / f'{name}.tar.gz'
    record = download_archive(name, archive, max_download_bytes)
    return convert_archive(name, directory, archive, record)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('name', choices=CATALOG)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--max-download-mib', type=int, default=256)
    args = parser.parse_args()
    if not 1 <= args.max_download_mib <= 1024:
        parser.error('download budget must be between 1 and 1024 MiB')
    try:
        print(json.dumps(fetch(args.name, args.output, args.max_download_mib * 1024**2), ensure_ascii=False, indent=2))
    except Exception as exc:
        # Keep failed downloads distinguishable from successfully converted data.
        print(json.dumps({'status': 'FAILED', 'error_type': type(exc).__name__, 'message': str(exc)}, ensure_ascii=False))
        raise SystemExit(2)
