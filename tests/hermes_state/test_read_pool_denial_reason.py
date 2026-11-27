"""Each descriptor-admission gate must identify itself, not assert eight busy readers."""
import logging

import pytest

import hermes_state_readpool as pool


@pytest.mark.parametrize('gate', ['fd_headroom', 'process_ceiling', 'path_ceiling'])
def test_admission_denial_identifies_gate(tmp_path, monkeypatch, caplog, gate):
    from hermes_state import SessionDB

    db = SessionDB(tmp_path / 'state.db')
    try:
        budget = db._read_budget
        monkeypatch.setattr(pool, '_fd_headroom_ok', lambda: gate != 'fd_headroom')
        if gate != 'fd_headroom':
            monkeypatch.setattr(budget, '_acquire_process_permit', lambda: False)
        if gate == 'path_ceiling':
            # Hold real process permits only briefly; the rejected path must release its permit.
            monkeypatch.setattr(budget, '_acquire_process_permit', lambda: pool._process_read_permits.acquire(False))
            monkeypatch.setattr(budget, '_acquire_path_permit', lambda requester: False)
        pool._pool_exhaustion_warned_at.clear()
        with caplog.at_level(logging.WARNING, logger='hermes_state'):
            assert not budget.acquire(db)
        assert len(caplog.records) == 1
        text = caplog.records[0].getMessage()
        assert f'reason={gate}' in text
        assert 'not proof of long writes' in text
    finally:
        db.close()
