"""A missing lease is absence of custody, not permission to resurrect a writer."""
import os
from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest

from hermes_state import SessionDB
from hermes_state_errors import SessionTurnLeaseLostError


@pytest.mark.parametrize("boundary", ["release", "takeover_release", "never_acquired"])
def test_missing_lease_refuses_stale_writer(tmp_path, boundary):
    db = SessionDB(tmp_path / "state.db")
    db.create_session("shared", source="test")
    holder = f"pid={os.getpid()}:turn=old"
    successor = f"pid={os.getpid()}:turn=new"
    if boundary != "never_acquired":
        assert db.try_acquire_session_turn_lease("shared", holder)
        db.release_session_turn_lease("shared", holder)
    if boundary == "takeover_release":
        assert db.try_acquire_session_turn_lease("shared", successor)
        db.append_messages_batch("shared", [{"role": "assistant", "content": "new"}],
                                 turn_lease_holder=successor)
        db.release_session_turn_lease("shared", successor)
    before = db.get_messages("shared")
    with pytest.raises(SessionTurnLeaseLostError, match="missing"):
        db.append_messages_batch("shared", [{"role": "assistant", "content": "stale"}],
                                 turn_lease_holder=holder)
    assert db.get_messages("shared") == before
    assert db.session_turn_lease_owner("shared") is None
    db.close()


def test_late_flush_after_successor_release_on_independent_connection(tmp_path):
    path = tmp_path / "state.db"
    writer = SessionDB(path)
    owner = SessionDB(path)
    owner.create_session("shared", source="test")
    holder = f"pid={os.getpid()}:turn=old"
    successor = f"pid={os.getpid()}:turn=new"
    assert owner.try_acquire_session_turn_lease("shared", holder)
    released = Event()

    def late_flush():
        assert released.wait(10)
        with pytest.raises(SessionTurnLeaseLostError, match="missing lease row"):
            writer.append_messages_batch("shared", [{"role": "assistant", "content": "stale"}],
                                         turn_lease_holder=holder)

    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(late_flush)
        try:
            owner.release_session_turn_lease("shared", holder)
            assert owner.try_acquire_session_turn_lease("shared", successor)
            with pytest.raises(SessionTurnLeaseLostError, match="holder mismatch"):
                writer.append_messages_batch("shared", [{"role": "assistant", "content": "stale"}],
                                             turn_lease_holder=holder)
            owner.release_session_turn_lease("shared", successor)
        finally:
            released.set()
        future.result(timeout=15)
    assert owner.get_messages("shared") == []
    assert owner.session_turn_lease_owner("shared") is None
    writer.close()
    owner.close()
