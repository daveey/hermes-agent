"""A return between queue miss and permit acquisition must remain usable."""
import logging

from hermes_state import SessionDB
import hermes_state_readpool as pool


def test_own_reader_return_after_checkout_miss_is_not_capacity_denial(tmp_path, monkeypatch, caplog):
    db = SessionDB(tmp_path / 'state.db')
    held = []
    borrowed = None
    try:
        held = [db._get_read_conn() for _ in range(pool._READ_POOL_MAX)]
        assert all(conn is not None for conn in held)
        original = db._get_read_conn

        def return_before_admission():
            # Exact race ordering: checkout saw an empty queue; an existing
            # reader returns before the following permit attempt. No sleeps.
            returned = held.pop()
            assert returned is not None
            db._read_pool.put_nowait(returned)
            return original()

        monkeypatch.setattr(db, '_get_read_conn', return_before_admission)
        pool._pool_exhaustion_warned_at.clear()
        with caplog.at_level(logging.WARNING, logger='hermes_state'):
            borrowed = db._checkout_read_conn()
        assert borrowed is not None, 'an idle reader returned before admission, yet checkout degraded'
        assert borrowed.execute('SELECT 42').fetchone()[0] == 42
        assert not [r for r in caplog.records if 'read pool at capacity' in r.getMessage()]
    finally:
        if borrowed is not None:
            db._close_read_conn(borrowed)
        for conn in held:
            if conn is not None:
                db._close_read_conn(conn)
        db.close()
