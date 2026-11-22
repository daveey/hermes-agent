"""Boot-pinned plugin review records, never approvals inferred from installed bytes."""
import hashlib
import json
import re
from pathlib import Path, PurePosixPath

REGISTRY = 'hermes_cli/data/plugin-review-records.json'
MAX_REGISTRY = 1024 * 1024
MAX_FILES = 4096
MAX_FILE_BYTES = 8 * 1024 * 1024
MAX_TOTAL_BYTES = 32 * 1024 * 1024


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate review key')
        result[key] = value
    return result


def read_records(core_root, expected_blob):
    """Only bytes matching the exact boot tree can supply review authority."""
    with (core_root / REGISTRY).open('rb') as stream:
        data = stream.read(MAX_REGISTRY + 1)
    if len(data) > MAX_REGISTRY:
        raise ValueError('review registry limit')
    blob = hashlib.sha1(b'blob ' + str(len(data)).encode() + b'\0' + data).hexdigest()
    if not expected_blob or blob != expected_blob:
        raise ValueError('review registry is not boot-pinned')
    value = json.loads(data, object_pairs_hook=_object)
    if (not isinstance(value, dict) or set(value) != {'version', 'plugins'}
            or type(value['version']) is not int or value['version'] != 1
            or not isinstance(value['plugins'], dict)):
        raise ValueError('review registry schema')
    count = 0
    for key, files in value['plugins'].items():
        if not isinstance(key, str) or not key or not isinstance(files, dict) or not files:
            raise ValueError('review record schema')
        count += len(files)
        if count > MAX_FILES:
            raise ValueError('review file count limit')
        for name, digest in files.items():
            path = PurePosixPath(name)
            if (not name or path.is_absolute() or '..' in path.parts
                    or path.as_posix() != name or '\\' in name or ':' in name
                    or '\x00' in name or not isinstance(digest, str)
                    or re.fullmatch('[0-9a-f]{64}', digest) is None):
                raise ValueError('review file schema')
    return value['plugins']


def validate_files(root, digests):
    """Fresh bounded read of every approved file, including not-yet-imported code."""
    total = 0
    for name, expected in digests.items():
        path = root / name
        if not path.resolve().is_relative_to(root):
            raise ValueError('review file escapes root')
        with path.open('rb') as stream:
            data = stream.read(min(MAX_FILE_BYTES, MAX_TOTAL_BYTES - total) + 1)
        total += len(data)
        if len(data) > MAX_FILE_BYTES or total > MAX_TOTAL_BYTES:
            raise ValueError('review source size limit')
        if hashlib.sha256(data).hexdigest() != expected:
            raise ValueError('review source digest mismatch')


def register_directory(key, directory):
    """Called before module construction; no late ledger installation."""
    from hermes_cli import code_attestation
    ledger = code_attestation._installed
    if ledger is None:
        return False
    try:
        root = Path(directory).resolve()
        if root.is_relative_to(ledger.core_root):
            return True  # Physical core custody, not the manifest's source label.
        records = read_records(ledger.core_root, ledger._blobs.get(REGISTRY))
        digests = records.get(key)
        if not digests:
            raise ValueError('plugin has no reviewed record')
        validate_files(root, digests)
        component = 'plugin:' + key
        with ledger._lock:
            expected = (root, 'plugin', digests)
            existing = ledger._components.get(component)
            if existing != expected:
                ledger.register_component_root(component, root, 'plugin', digests=digests)
            if ledger._components.get(component) != expected:
                return False
            ledger.__dict__.setdefault('_reviewed_plugins', {})[component] = (root, dict(digests))
        return True
    except (OSError, ValueError, TypeError, RecursionError):
        ledger.mark_unknown('plugin-review-unverified:' + key)
        return False


def verify_registered(ledger):
    for component, (root, digests) in getattr(ledger, '_reviewed_plugins', {}).items():
        try:
            validate_files(root, digests)
        except (OSError, ValueError):
            ledger.mark_unknown('plugin-review-drift:' + component)


def unreviewed_entrypoint(key):
    from hermes_cli import code_attestation
    if code_attestation._installed is not None:
        code_attestation._installed.mark_unknown('plugin-entrypoint-unreviewed:' + key)
