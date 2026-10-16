"""Verify externally issued receipts; this application never issues signatures.

Trust file (outside the data root): {"keys":[{"issuer":"lab", "key_hex":
"64 or more hex characters", "claims":["reference","physical_review"]}]}.
Only configured issuers can turn a declaration into an authenticated review.
HMAC authenticates the issuer's assertion, not the truth of a physical model.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
from pathlib import Path
import re

from .engineering import canonical


def attestation_message(binding, purpose):
    if purpose not in {'reference', 'physical_review'}: raise ValueError('Invalid attestation purpose')
    return canonical({'schema_version': '1.0', 'purpose': 'emsolver.industrial.' + purpose + '.v1', 'binding': binding})


def verify_attestation(receipt, binding, purpose, *, data_root=None):
    if receipt is None: return 'UNVERIFIED', 'NO_TRUSTED_ATTESTATION'
    filename = os.environ.get('EMSOLVER_INDUSTRIAL_ATTESTATION_KEYS_FILE')
    if not filename: return 'UNVERIFIED', 'TRUST_AUTHORITY_NOT_CONFIGURED'
    try:
        path = Path(filename)
        if data_root is None or path.resolve().is_relative_to(Path(data_root).resolve()):
            raise ValueError('Authority keys must be outside backed-up business data')
        if any(parent.is_symlink() for parent in [path, *path.parents]) or path.stat().st_uid != os.getuid():
            raise ValueError('Unowned or redirected authority configuration')
        if path.is_symlink() or not path.is_file() or path.stat().st_size > 65536 or path.stat().st_mode & 0o077:
            raise ValueError('Unsafe authority configuration')
        value = json.loads(path.read_text(encoding='utf-8'))
        if not isinstance(value, dict) or set(value) != {'keys'} or not isinstance(value['keys'], list) or not 1 <= len(value['keys']) <= 64:
            raise ValueError('Invalid authority configuration')
        seen = set()
        for key in value['keys']:
            if (not isinstance(key, dict) or set(key) != {'issuer', 'key_hex', 'claims'} or
                not isinstance(key['issuer'], str) or not re.fullmatch(r'[A-Za-z0-9_.-]{1,128}', key['issuer']) or key['issuer'] in seen or
                not isinstance(key['key_hex'], str) or not re.fullmatch(r'[0-9a-f]{64,128}', key['key_hex']) or len(key['key_hex']) % 2 or
                not isinstance(key['claims'], list) or not key['claims'] or set(key['claims']) - {'reference', 'physical_review'}):
                raise ValueError('Invalid authority configuration')
            seen.add(key['issuer'])
        authority = next((key for key in value['keys'] if key['issuer'] == receipt['issuer'] and purpose in key['claims']), None)
        if authority is None: return 'UNVERIFIED', 'ISSUER_NOT_TRUSTED_FOR_PURPOSE'
        signature = hmac.new(bytes.fromhex(authority['key_hex']), attestation_message(binding, purpose), hashlib.sha256).hexdigest()
        return ('PASS', 'CONFIGURED_ISSUER_ATTESTED') if hmac.compare_digest(signature, receipt['signature']) else ('FAIL', 'INVALID_ATTESTATION')
    except (OSError, ValueError, TypeError, KeyError, RecursionError):
        return 'UNVERIFIED', 'TRUST_AUTHORITY_UNAVAILABLE'
