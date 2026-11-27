"""Frozen-policy consumers must see exact hydrated values, not storage refs."""
from contextlib import closing

import pytest

from hermes_state import SessionDB
from hermes_state_local import commit_local_session, local_receipt, retire_local_receipts, release_receipts
from hermes_state_policy_config import BLOB_PREFIX, REF_PREFIX, store_receipt
import hermes_state_runtime as rt
from tests.hermes_state.test_target_advance_fence import _local_session


@pytest.fixture
def interned(tmp_path):
    with closing(SessionDB(tmp_path / 'state.db')) as db:
        epoch = rt.begin_runtime_epoch(db, instance_id='owner')
        sid = _local_session(db, epoch, tmp_path)
        original = local_receipt(db, sid)
        db._execute_write(lambda conn: store_receipt(conn, sid, original, intern=True))
        yield db, epoch, sid, original


def _assert_no_blobs(db):
    with db._read_ctx() as conn:
        assert conn.execute('SELECT 1 FROM state_meta WHERE key GLOB ? OR key GLOB ?',
                            (BLOB_PREFIX + '*', REF_PREFIX + '*')).fetchall() == []


def test_interned_create_retry_returns_exact_frozen_policy(interned):
    db, epoch, sid, original = interned
    assert commit_local_session(db, epoch=epoch, receipt=original) == original
    assert local_receipt(db, sid) == original


def test_interned_compression_adoption_uses_hydrated_expected_receipt(interned):
    from hermes_state_local_lineage import adopt_untracked_compression
    from tests.gateway.test_local_receipt_untracked_compression import _compact_like_0_22_2
    db, epoch, sid, original = interned
    _compact_like_0_22_2(db, sid, 'child')
    adopt_untracked_compression(db, epoch=epoch, session_id=sid, expected=original)
    saved = local_receipt(db, sid)
    assert saved['entry']['session_id'] == 'child'
    assert saved['policy'] == original['policy']


def test_interned_prepared_snapshot(interned):
    from hermes_state_mutation_prepared import local_snapshot
    db, _epoch, sid, original = interned
    snapshot = db._execute_write(lambda conn: local_snapshot(db, conn, sid))
    assert snapshot['receipt'] == original


def test_interned_quarantine(interned):
    from hermes_state_local_quarantine import quarantine_local_policy
    db, epoch, sid, original = interned
    assert quarantine_local_policy(db, epoch=epoch, session_id=sid, expected=original, error_type='ValueError')
    saved = local_receipt(db, sid)
    assert saved['policy'] == original['policy']
    assert saved['quarantine']['error_type'] == 'ValueError'


def test_interned_prepared_update_retires_previous_blob(interned):
    from hermes_state_mutation_prepared import _commit_policy, local_snapshot
    db, _epoch, sid, original = interned
    changed = {**original['policy'], 'config_json': '{"changed":true}'}
    def update(conn):
        snapshot = local_snapshot(db, conn, sid)
        _commit_policy(db, conn, sid, {'snapshot': snapshot, 'policy': changed})
    db._execute_write(update)
    assert local_receipt(db, sid)['policy'] == changed
    db._execute_write(lambda conn: release_receipts(conn, [sid]))
    _assert_no_blobs(db)


def test_interned_branch_has_independent_reference(interned):
    from hermes_state_mutation_branch import branch_in_transaction
    db, _epoch, sid, original = interned
    _, result = db._execute_write(lambda conn: branch_in_transaction(db, conn, sid, {}))
    child = result['branched_session_id']
    db._execute_write(lambda conn: retire_local_receipts(conn, [sid]))
    assert local_receipt(db, child)['policy'] == original['policy']
    db._execute_write(lambda conn: release_receipts(conn, [child]))
    _assert_no_blobs(db)


@pytest.mark.parametrize('remove', [retire_local_receipts, release_receipts])
def test_interned_retirement(interned, remove):
    db, _epoch, sid, _original = interned
    db._execute_write(lambda conn: remove(conn, [sid]))
    _assert_no_blobs(db)
