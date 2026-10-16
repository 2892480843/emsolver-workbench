import json

from emsolver.provenance import capture_provenance, reproducible_commands


def test_source_hash_is_content_based_and_changes_when_source_changes(tmp_path):
    package = tmp_path / 'emsolver'
    package.mkdir()
    source = package / 'sample.py'
    source.write_text('value = 1\n', encoding='utf-8')
    first = capture_provenance(tmp_path)
    assert first['source']['tree_sha256'] == capture_provenance(tmp_path)['source']['tree_sha256']
    source.write_text('value = 2\n', encoding='utf-8')
    assert first['source']['tree_sha256'] != capture_provenance(tmp_path)['source']['tree_sha256']
    assert str(tmp_path) not in json.dumps(first)
    assert first['source']['git_commit'] is None
    assert first['dependencies']['numpy']
    assert first['container']['image_digest'] is None
    assert first['container']['status'] == 'unknown'


def test_provenance_never_reads_symlinked_private_source_and_unknown_is_honest(tmp_path, monkeypatch):
    private = tmp_path / 'private.txt'
    private.write_text('secret', encoding='utf-8')
    package = tmp_path / 'emsolver'
    package.mkdir()
    (package / 'leak.py').symlink_to(private)
    monkeypatch.setenv('EMSOLVER_IMAGE_DIGEST', 'unverified-image')
    result = capture_provenance(tmp_path)
    assert result['source']['file_count'] == 0
    assert result['source']['tree_sha256'] is None
    assert result['container']['status'] == 'unknown'
    assert 'secret' not in json.dumps(result)


def test_reproduction_commands_use_only_portable_placeholders():
    commands = reproducible_commands()
    assert commands['solve'] == 'emsolver solve --matrix INPUT.npz --config run/config.resolved.yaml --output rerun'
    assert commands['validate'] == 'emsolver validate --matrix INPUT.npz --run rerun'
    assert '/Users/' not in json.dumps(commands)
