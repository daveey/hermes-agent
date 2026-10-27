"""Two independent SQLite writers cannot admit the same operation twice."""
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest

from hermes_state import SessionDB
from hermes_state_prompt_admission import admit_prompt, prompt_digest, read_admission
from tests.hermes_state.test_prompt_admission import no_update_recovery


def test_competing_handles_have_one_winner(tmp_path):
    path = tmp_path / "state.db"
    db = SessionDB(db_path=path)
    db.create_session(session_id="exact", source="desktop")
    # Warm the lazy transcript machinery before entering independent threads.
    db.append_message("exact", "assistant", "ready")
    db.close()
    barrier = Barrier(2)
    digest = prompt_digest("exact", "hello")
    def accept():
        handle = SessionDB(db_path=path)
        try:
            barrier.wait(timeout=10)
            return admit_prompt(handle, "exact", "op", "hello", digest)
        finally:
            handle.close()
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: accept(), range(2)))
    assert sorted(result[2] for result in results) == [False, True]
    assert results[0][0] == results[1][0]
    db = SessionDB(db_path=path)
    try:
        assert [row["role"] for row in db.get_messages("exact")] == ["assistant", "user"]
        receipt = results[0][0]
        # A destructive external rewrite invalidates readback, but the operation
        # tombstone survives: even then a replay cannot insert another user turn.
        db._execute_write(lambda conn: conn.execute("DELETE FROM messages WHERE id = ?", (receipt["user_row_id"],)))
        with pytest.raises(ValueError, match="no longer verifiable"):
            read_admission(db, "exact", "op", digest)
        with pytest.raises(ValueError, match="no longer verifiable"):
            admit_prompt(db, "exact", "op", "hello", digest)
    finally:
        db.close()
