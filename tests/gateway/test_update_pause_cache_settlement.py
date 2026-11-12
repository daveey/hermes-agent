"""A parked-head cache must not outlive the durable row it describes (real SQLite)."""
import pytest

from gateway.session_update_pause import checkpoint_head
from hermes_state_runtime import RuntimeStoreError, admit_session_input, get_session_admission
from hermes_state_update_pause import CHECKPOINT_PREFIX
from tests.gateway.test_authority_update_pause import parked_fixture
from tests.gateway.test_session_authority_cancel_settlement import REF


def _external_settle(db, head):
    def write(conn):
        conn.execute("UPDATE session_admissions SET status='terminal',outcome='cancelled' "
                     "WHERE admission_id=?", (head['admission_id'],))
        conn.execute('DELETE FROM state_meta WHERE key=?', (CHECKPOINT_PREFIX + head['admission_id'],))
    db._execute_write(write)


@pytest.mark.asyncio
async def test_externally_settled_parked_head_does_not_starve_follower(tmp_path, monkeypatch):
    from gateway import session_update_pause
    db, owner, head = parked_fixture(tmp_path, monkeypatch)
    with db:
        admit_session_input(db, epoch=owner.epoch, principal_id='human', session_id='s',
                            request_id='later', payload={'text': 'later'})
        assert checkpoint_head(owner, REF)['admission_id'] == head['admission_id']  # parked, cached
        _external_settle(db, head)
        monkeypatch.setattr(session_update_pause, 'update_held', lambda authority: True)
        # Hold: a stale cache must not report a parked head over a terminal row.
        row, _ = await owner._claim_next(REF, owner.sessions['s'])
        assert row and row['request_id'] == 'later' and not row.get('update_resume')
        assert REF.session_id not in owner.update_parked
        monkeypatch.setattr(session_update_pause, 'update_held', lambda authority: False)
        assert checkpoint_head(owner, REF) is None  # release: nothing left to resume/invalidate


@pytest.mark.asyncio
async def test_started_head_with_vanished_proof_fails_closed_before_fifo(tmp_path, monkeypatch):
    db, owner, head = parked_fixture(tmp_path, monkeypatch)
    with db:
        admit_session_input(db, epoch=owner.epoch, principal_id='human', session_id='s',
                            request_id='later', payload={'text': 'later'})
        checkpoint_head(owner, REF)
        db._execute_write(lambda conn: conn.execute(
            'DELETE FROM state_meta WHERE key=?', (CHECKPOINT_PREFIX + head['admission_id'],)))
        with pytest.raises(RuntimeStoreError, match='invalid_checkpoint'):
            checkpoint_head(owner, REF)
        assert get_session_admission(db, admission_id=head['admission_id'])['status'] == 'started'
        with pytest.raises(RuntimeStoreError):
            await owner._claim_next(REF, owner.sessions['s'])  # follower never bypasses the head
        later = next(r for r in __import__('hermes_state_runtime').list_session_admissions(db, session_id='s')
                 if r['request_id'] == 'later')
        assert later['status'] == 'queued'


@pytest.mark.asyncio
async def test_live_parked_head_still_uses_cache_without_rehash(tmp_path, monkeypatch):
    from gateway import session_update_pause
    db, owner, _head = parked_fixture(tmp_path, monkeypatch)
    with db:
        first = checkpoint_head(owner, REF)
        monkeypatch.setattr(session_update_pause, 'get_session_checkpoint',
                            lambda *a, **kw: pytest.fail('rehashed'))
        assert checkpoint_head(owner, REF) is first
