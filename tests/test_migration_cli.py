from scripts import migrate_sqlite_to_postgres as cli


def test_report_failure_does_not_claim_a_committed_migration_rolled_back(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv('EMSOLVER_DATABASE_URL', 'postgresql://not-used')
    monkeypatch.setattr(cli, 'migrate_sqlite_to_postgres', lambda *a, **kw: {'dry_run': False})
    assert cli.main([str(tmp_path/'source.sqlite3'), '--report', str(tmp_path/'missing'/'report.json')]) == 1
    message=capsys.readouterr().err
    assert 'Migration completed' in message
    assert 'rolled back' not in message


def test_migration_error_keeps_failure_boundary_clear(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv('EMSOLVER_DATABASE_URL', 'postgresql://not-used')
    def fail(*a, **kw):
        raise OSError('private connection details')
    monkeypatch.setattr(cli, 'migrate_sqlite_to_postgres', fail)
    assert cli.main([str(tmp_path/'source.sqlite3')]) == 1
    message=capsys.readouterr().err
    assert 'rolled back' in message
    assert 'private connection details' not in message
