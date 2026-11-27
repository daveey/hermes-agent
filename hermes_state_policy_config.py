"""Transaction-owned, exact-byte frozen-policy config interning.

Legacy inline receipts remain the default. Callers must explicitly opt in only
AFTER every reader is upgraded; this module installs no schema or activation.
Blob/reference keys live in state_meta so ordinary recovery copies their custody.
The request and all other receipt fields stay inline and byte-identical as values.
"""
import hashlib
import json
import re

from hermes_state_runtime import RuntimeStoreError, _json

POLICY_PREFIX = 'gateway.local_policy.v1:'
BLOB_PREFIX = 'gateway.policy_config.blob.v1:'
REF_PREFIX = 'gateway.policy_config.ref.v1:'
_DIGEST = re.compile(r'[0-9a-f]{64}\Z')


def _digest(text):
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def _reference(receipt):
    policy = receipt.get('policy')
    if not isinstance(policy, dict):
        return None
    digest = policy.get('config_ref_v1')
    if digest is not None and (not isinstance(digest, str) or not _DIGEST.fullmatch(digest)
                               or 'config_json' in policy):
        raise RuntimeStoreError('storage_unavailable')
    return digest


def _decode(raw, lookup):
    """Decode with blob lookup bound to the receipt's SQLite snapshot."""
    try:
        receipt = json.loads(raw)
        if not isinstance(receipt, dict):
            raise ValueError('invalid receipt')
        digest = _reference(receipt)
        if digest is not None:
            text = lookup(digest)
            if not isinstance(text, str) or _digest(text) != digest:
                raise ValueError('invalid config blob')
            receipt['policy'].pop('config_ref_v1')
            receipt['policy']['config_json'] = text
        return receipt
    except (ValueError, TypeError, UnicodeError) as exc:
        raise RuntimeStoreError('storage_unavailable') from exc


def decode_receipt(conn, raw):
    """Hydrate raw bytes already read inside the caller's owner transaction.

    An autocommit connection cannot promise that its earlier raw SELECT and this
    blob SELECT share a snapshot; use load_receipt for standalone reads instead.
    """
    if not conn.in_transaction:
        raise RuntimeStoreError('runtime_coordination_required')
    def lookup(digest):
        row = conn.execute('SELECT value FROM state_meta WHERE key=?', (BLOB_PREFIX + digest,)).fetchone()
        return row[0] if row is not None else None
    return _decode(raw, lookup)


def load_receipt(conn, session_id):
    # One statement is one snapshot even on pooled autocommit readers. Merely
    # borrowing the same connection for two SELECTs does NOT provide that fence.
    row = conn.execute(
        "SELECT m.value,b.value FROM state_meta m LEFT JOIN state_meta b "
        "ON b.key=? || CASE WHEN json_valid(m.value) THEN json_extract(m.value,'$.policy.config_ref_v1') END "
        "WHERE m.key=?", (BLOB_PREFIX, POLICY_PREFIX + session_id)).fetchone()
    if row is None:
        raise RuntimeStoreError('storage_unavailable')
    return _decode(row[0], lambda digest: row[1])


def _stored_reference(conn, session_id):
    row = conn.execute('SELECT value FROM state_meta WHERE key=?', (POLICY_PREFIX + session_id,)).fetchone()
    if row is None:
        return None
    try:
        return _reference(json.loads(row[0]))
    except (ValueError, TypeError, AttributeError) as exc:
        raise RuntimeStoreError('storage_unavailable') from exc


def _retire_reference(conn, session_id, digest):
    if digest is None:
        return
    conn.execute('DELETE FROM state_meta WHERE key=?', (REF_PREFIX + digest + ':' + session_id,))
    # GLOB's fixed hash prefix uses state_meta's key index; never scan policy
    # payloads (hundreds of MB) or maintain a refcount that can drift on retries.
    if conn.execute('SELECT 1 FROM state_meta WHERE key GLOB ? LIMIT 1',
                    (REF_PREFIX + digest + ':*',)).fetchone() is None:
        conn.execute('DELETE FROM state_meta WHERE key=?', (BLOB_PREFIX + digest,))


def store_receipt(conn, session_id, receipt, *, intern=False):
    """Store with its exact-byte blob/ref in the existing owner transaction.

    Does not commit, open connections or change activation. Inline is also the
    rollback representation: callers first hydrate, then store with intern=False.
    A local savepoint makes a caught statement failure leave no partial inventory.
    """
    # Validate/serialize before any write and copy so caller snapshots stay intact.
    saved = json.loads(_json(receipt))
    if _reference(saved) is not None:
        raise RuntimeStoreError('storage_unavailable')
    digest, text = None, None
    if intern:
        text = saved['policy'].get('config_json')
        if not isinstance(text, str):
            raise RuntimeStoreError('storage_unavailable')
        digest = _digest(text)
        saved['policy'].pop('config_json')
        saved['policy']['config_ref_v1'] = digest
    old = _stored_reference(conn, session_id)
    # Do not use an outermost savepoint: its RELEASE would commit on an
    # autocommit caller and make rollback of a larger owner operation impossible.
    if not conn.in_transaction:
        raise RuntimeStoreError('runtime_coordination_required')
    conn.execute('SAVEPOINT policy_config_store')
    try:
        if digest is not None:
            key = BLOB_PREFIX + digest
            existing = conn.execute('SELECT value FROM state_meta WHERE key=?', (key,)).fetchone()
            if existing is not None and existing[0] != text:
                raise RuntimeStoreError('storage_unavailable')
            conn.execute('INSERT OR IGNORE INTO state_meta(key,value) VALUES(?,?)', (key, text))
            conn.execute('INSERT OR IGNORE INTO state_meta(key,value) VALUES(?,?)',
                         (REF_PREFIX + digest + ':' + session_id, digest))
        conn.execute('INSERT INTO state_meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',
                     (POLICY_PREFIX + session_id, _json(saved)))
        if old != digest:
            _retire_reference(conn, session_id, old)
        conn.execute('RELEASE policy_config_store')
    except BaseException:
        conn.execute('ROLLBACK TO policy_config_store')
        conn.execute('RELEASE policy_config_store')
        raise


def reconcile_recovered_references(conn):
    """Rebuild derived refs from recovered owners before any GC can trust them.

    Recovery may salvage receipt/blob/ref rows independently. Ref rows are not
    evidence of completeness. Validate every surviving receipt before changing
    any reference; unreadable custody fails the separate-output recovery closed.
    """
    if not conn.in_transaction:
        raise RuntimeStoreError('runtime_coordination_required')
    present = conn.execute('SELECT 1 FROM state_meta WHERE key GLOB ? OR key GLOB ? LIMIT 1',
                           (BLOB_PREFIX + '*', REF_PREFIX + '*')).fetchone()
    if present is None:
        present = conn.execute(
            "SELECT 1 FROM state_meta WHERE key GLOB ? AND CASE WHEN json_valid(value) "
            "THEN json_type(value,'$.policy.config_ref_v1') IS NOT NULL ELSE 0 END LIMIT 1",
            (POLICY_PREFIX + '*',)).fetchone()
    if present is None:
        return 0
    refs = []
    for key, raw in conn.execute('SELECT key,value FROM state_meta WHERE key GLOB ?', (POLICY_PREFIX + '*',)):
        decoded = decode_receipt(conn, raw)
        digest = _digest(decoded['policy']['config_json']) if _reference(json.loads(raw)) is not None else None
        if digest is not None:
            refs.append((REF_PREFIX + digest + ':' + key[len(POLICY_PREFIX):], digest))
    # The caller's recovery transaction owns all writes; no live-store migration.
    conn.execute('DELETE FROM state_meta WHERE key GLOB ?', (REF_PREFIX + '*',))
    conn.executemany('INSERT INTO state_meta(key,value) VALUES(?,?)', refs)
    # Only after validating the complete surviving owner inventory can recovery
    # retire payloads whose policy rows were not salvaged. Never keep them forever.
    for (key,) in conn.execute('SELECT key FROM state_meta WHERE key GLOB ?', (BLOB_PREFIX + '*',)).fetchall():
        digest = key[len(BLOB_PREFIX):]
        if conn.execute('SELECT 1 FROM state_meta WHERE key GLOB ? LIMIT 1',
                        (REF_PREFIX + digest + ':*',)).fetchone() is None:
            conn.execute('DELETE FROM state_meta WHERE key=?', (key,))
    return len(refs)


def replace_receipt(conn, session_id, receipt):
    """Replace hydrated values while retaining this owner's storage format."""
    store_receipt(conn, session_id, receipt, intern=_stored_reference(conn, session_id) is not None)


def delete_receipt(conn, session_id):
    """Delete a receipt and its last reference on its owner's delete transaction."""
    digest = _stored_reference(conn, session_id)
    if not conn.in_transaction:
        raise RuntimeStoreError('runtime_coordination_required')
    conn.execute('SAVEPOINT policy_config_delete')
    try:
        conn.execute('DELETE FROM state_meta WHERE key=?', (POLICY_PREFIX + session_id,))
        _retire_reference(conn, session_id, digest)
        conn.execute('RELEASE policy_config_delete')
    except BaseException:
        conn.execute('ROLLBACK TO policy_config_delete')
        conn.execute('RELEASE policy_config_delete')
        raise
