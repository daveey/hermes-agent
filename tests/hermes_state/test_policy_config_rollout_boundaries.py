"""Real owner admission and failure boundaries for policy conversion."""
from contextlib import closing
import json

import pytest

from hermes_state import SessionDB
from hermes_state_local import POLICY_PREFIX, local_receipt
from hermes_state_policy_config import store_receipt
import hermes_state_runtime as rt
from tests.hermes_state.test_policy_config_rollout import create


@pytest.mark.parametrize('status', ['queued', 'started', 'unknown'])
def test_conversion_skips_live_or_unknown_and_preserves_runtime_revision(tmp_path, status):
    from hermes_state_policy_rollout import convert_policy_config_batch
    with closing(SessionDB(tmp_path / 'state.db')) as db:
        epoch = rt.begin_runtime_epoch(db, instance_id='owner')
        sid, expected = create(db, epoch, tmp_path, False)
        rt.admit_session_input(db, epoch=epoch, principal_id='human', session_id=sid,
                               request_id='turn', payload={'text': 'keep'})
        if status != 'queued':
            rt.claim_session_input(db, epoch=epoch, session_id=sid)
            if status == 'unknown':
                db._execute_write(lambda conn: conn.execute('UPDATE session_admissions SET status=? WHERE target_session_id=?', (status, sid)))
        before = db.get_session(sid)
        result = convert_policy_config_batch(db, intern=True, apply=True, readers_compatible=True)
        assert result['converted'] == 0
        assert result['skipped'] == {sid: 'unknown_execution' if status == 'unknown' else 'session_busy'}
        assert db.get_session(sid) == before
        assert local_receipt(db, sid) == expected


def test_dry_run_and_unacknowledged_conversion_never_write(tmp_path):
    from hermes_state_policy_rollout import convert_policy_config_batch
    with closing(SessionDB(tmp_path / 'state.db')) as db:
        epoch = rt.begin_runtime_epoch(db, instance_id='owner')
        sid, expected = create(db, epoch, tmp_path, False)
        with db._read_ctx() as conn:
            before = conn.execute('SELECT key,value FROM state_meta ORDER BY key').fetchall()
        assert convert_policy_config_batch(db, intern=True)['applied'] is False
        with pytest.raises(rt.RuntimeStoreError, match='runtime_coordination_required'):
            convert_policy_config_batch(db, intern=True, apply=True)
        with db._read_ctx() as conn:
            assert conn.execute('SELECT key,value FROM state_meta ORDER BY key').fetchall() == before
        assert local_receipt(db, sid) == expected


def test_converter_rolls_back_earlier_owner_on_later_bad_receipt(tmp_path):
    from hermes_state_policy_rollout import convert_policy_config_batch
    with closing(SessionDB(tmp_path / 'state.db')) as db:
        epoch = rt.begin_runtime_epoch(db, instance_id='owner')
        for i in range(2):
            create(db, epoch, tmp_path, False, str(i))
        def corrupt(conn):
            keys = conn.execute('SELECT key FROM state_meta WHERE key GLOB ? ORDER BY key', (POLICY_PREFIX + '*',)).fetchall()
            conn.execute('UPDATE state_meta SET value=? WHERE key=?', ('bad JSON', keys[-1][0]))
            return conn.execute('SELECT key,value FROM state_meta ORDER BY key').fetchall()
        before = db._execute_write(corrupt)
        with pytest.raises(rt.RuntimeStoreError, match='storage_unavailable'):
            convert_policy_config_batch(db, intern=True, apply=True, readers_compatible=True)
        with db._read_ctx() as conn:
            assert conn.execute('SELECT key,value FROM state_meta ORDER BY key').fetchall() == before


def test_rollback_accounts_for_hydrated_output_byte_bound(tmp_path):
    from hermes_state_policy_rollout import convert_policy_config_batch
    with closing(SessionDB(tmp_path / 'state.db')) as db:
        epoch = rt.begin_runtime_epoch(db, instance_id='owner')
        sid, expected = create(db, epoch, tmp_path, False)
        expected['policy']['config_json'] = 'x' * 65536
        db._execute_write(lambda conn: store_receipt(conn, sid, expected, intern=True))
        result = convert_policy_config_batch(db, intern=False, apply=True, max_bytes=16384)
        assert result['converted'] == 0 and result['skipped'] == {sid: 'oversize'}
        with db._read_ctx() as conn:
            raw = conn.execute('SELECT value FROM state_meta WHERE key=?', (POLICY_PREFIX + sid,)).fetchone()[0]
            assert 'config_ref_v1' in json.loads(raw)['policy']
        assert local_receipt(db, sid) == expected


def test_aggregate_byte_fence_does_not_parse_next_unadmitted_receipt(tmp_path):
    from hermes_state_policy_rollout import convert_policy_config_batch
    with closing(SessionDB(tmp_path / 'state.db')) as db:
        epoch = rt.begin_runtime_epoch(db, instance_id='owner')
        for i in range(2):
            create(db, epoch, tmp_path, False, str(i))
        def prepare(conn):
            rows = conn.execute('SELECT key,length(CAST(value AS BLOB)) FROM state_meta WHERE key GLOB ? ORDER BY key',
                                (POLICY_PREFIX + '*',)).fetchall()
            conn.execute('UPDATE state_meta SET value=? WHERE key=?', ('bad JSON', rows[1][0]))
            return rows[0][0][len(POLICY_PREFIX):], rows[0][1]
        sid, size = db._execute_write(prepare)
        result = convert_policy_config_batch(db, intern=True, apply=True, readers_compatible=True, max_bytes=size)
        assert result['converted'] == 1 and result['more'] and result['after'] == sid
        assert result['bytes'] == size
