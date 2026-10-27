"""Cancellation retains audit identity and atomically retires authority."""
import sqlite3
import pytest

from hermes_state import SessionDB
from tests.hermes_state.test_prompt_admission import no_update_recovery
from hermes_state_prompt_admission import admit_prompt, read_admission, prompt_digest, mark_dispatch_failed


def test_cancellation_is_atomic_and_replay_proof(tmp_path):
    db = SessionDB(db_path=tmp_path / 'state.db')
    try:
        db.create_session('exact', 'desktop')
        digest = prompt_digest('exact', 'approve')
        receipt, _, _ = admit_prompt(db, 'exact', 'op', 'approve', digest)
        db._write_sql("CREATE TRIGGER reject_cancel BEFORE UPDATE ON prompt_admissions BEGIN SELECT RAISE(ABORT, 'blocked'); END", ())
        with pytest.raises(sqlite3.IntegrityError, match='blocked'):
            mark_dispatch_failed(db, 'exact', 'op', digest)
        assert read_admission(db, 'exact', 'op', digest) == receipt
        assert len(db.get_messages('exact')) == 1
        db._write_sql('DROP TRIGGER reject_cancel', ())
        cancelled = mark_dispatch_failed(db, 'exact', 'op', digest)
        assert cancelled['dispatch'] == 'cancelled'
        assert not db.get_messages('exact')
        assert admit_prompt(db, 'exact', 'op', 'approve', digest) == (cancelled, None, False)
        assert mark_dispatch_failed(db, 'exact', 'op', digest) == cancelled
        with pytest.raises(ValueError, match='conflict'):
            mark_dispatch_failed(db, 'exact', 'op', 'wrong')
        db._write_sql('UPDATE messages SET active=1 WHERE message_uid=?', (cancelled['message_uid'],))
        with pytest.raises(ValueError, match='not verifiable'):
            read_admission(db, 'exact', 'op', digest)
    finally:
        db.close()
