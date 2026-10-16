"""Select the configured metadata backend without changing local defaults."""
from __future__ import annotations

import os

from .store import Store


def open_store(path):
    """Use PostgreSQL when configured, otherwise the existing SQLite store."""
    url = os.environ.get('EMSOLVER_DATABASE_URL', '').strip()
    if not url:
        return Store(path)
    if not url.startswith(('postgresql://', 'postgres://')):
        raise ValueError('Unsupported database URL; use postgresql:// or postgres://')
    from .postgres_store import PostgresStore
    return PostgresStore(url, schema=os.environ.get('EMSOLVER_DATABASE_SCHEMA', 'emsolver'), path=path)
