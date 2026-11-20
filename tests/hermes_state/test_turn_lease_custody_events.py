"""Turn-lease custody events name every row producer and change no fencing [daveey t_01a1f9c3ea7cb7fa]."""
import logging
import os
import time

import pytest

import hermes_state_lease_custody as custody
from hermes_state import SessionDB
from hermes_state_errors import SessionTurnLeaseLostError

LOGGER = "hermes_state.lease_custody"
MSG = [{"role": "assistant", "content": "ok"}]


@pytest.fixture
def db(tmp_path, caplog):
    caplog.set_level(logging.INFO, logger=LOGGER)
    store = SessionDB(tmp_path / "state.db")
    yield store
    store.close()


def _events(caplog):
    return [dict(part.split("=", 1) for part in r.getMessage().split()[1:])
            for r in caplog.records if r.name == LOGGER]


def _ops(caplog):
    return [e.get("op") or e["phase"] for e in _events(caplog)]


def _holder(name="turn"):
    return f"pid={os.getpid()}:turn=private-{name}"


def _lease_rows(db):
    with db._read_ctx() as conn:
        return [tuple(r) for r in conn.execute("SELECT conversation_id, holder FROM session_turn_leases")]


def test_turn_lifecycle_is_reported_with_settlement_and_without_capabilities(db, caplog, tmp_path):
    sid, holder = "private-session-name", _holder()
    db.create_session(sid, source="desktop")
    caplog.clear()
    assert db.try_acquire_session_turn_lease(sid, holder)
    assert db.append_messages_batch(sid, MSG, turn_lease_holder=holder) == 1
    db.release_session_turn_lease(sid, holder)
    with pytest.raises(SessionTurnLeaseLostError, match="missing lease row"):
        db.append_messages_batch(sid, [{"role": "assistant", "content": "late"}], turn_lease_holder=holder)

    assert _ops(caplog) == ["claim", "committed", "release", "committed", "missing", "rolled_back"]
    claim, claimed, release, released, missing, refused = _events(caplog)
    assert (claim["txn"], release["txn"], missing["txn"]) == (claimed["txn"], released["txn"], refused["txn"])
    assert len({claim["txn"], release["txn"], missing["txn"]}) == 3
    assert (claim["changed"], claim["why"], release["changed"]) == ("1", "owned=1", "1")
    assert claim["key"] == claim["session"] == custody.tag(sid) and claim["root_is_session"] == "1"
    assert claim["holder"] == claim["row_holder"] == release["row_holder"] == missing["holder"] == custody.tag(holder)
    assert missing["row_holder"] == "-" and missing["why"] == "lease_rows=0,session_row=1,parent=0"
    assert claim["store"] == custody.tag(db.db_path) != "-"
    text = "\n".join(r.getMessage() for r in caplog.records if r.name == LOGGER)
    assert holder not in text and sid not in text and str(tmp_path) not in text
    assert [r.levelno for r in caplog.records if "op=missing" in r.getMessage()] == [logging.WARNING]
    # Fencing is unchanged: the refused write did not land and no row was restored.
    assert [m["content"] for m in db.get_messages(sid)] == ["ok"]
    assert _lease_rows(db) == []


def test_never_admitted_and_superseded_holders_still_refuse(db, caplog):
    sid, owner, other = "s1", _holder("owner"), _holder("other")
    db.create_session(sid, source="desktop")
    with pytest.raises(SessionTurnLeaseLostError, match="missing lease row"):
        db.append_messages_batch(sid, MSG, turn_lease_holder=other)
    assert _lease_rows(db) == []
    assert db.try_acquire_session_turn_lease(sid, owner)
    caplog.clear()
    assert not db.try_acquire_session_turn_lease(sid, other)
    lost = _events(caplog)[0]
    assert (lost["op"], lost["changed"], lost["why"], lost["row_holder"]) == ("claim", "0", "owned=0", custody.tag(owner))
    caplog.clear()
    with pytest.raises(SessionTurnLeaseLostError, match="holder mismatch"):
        db.append_messages_batch(sid, MSG, turn_lease_holder=other)
    mismatch = _events(caplog)[0]
    assert (mismatch["op"], mismatch["holder"], mismatch["row_holder"]) == (
        "mismatch", custody.tag(other), custody.tag(owner))
    assert db.append_messages_batch(sid, MSG, turn_lease_holder=owner) == 1
    assert _lease_rows(db) == [(sid, owner)]


def test_a_release_that_removes_nothing_is_not_reported(db, caplog):
    db.create_session("s1", source="desktop")
    caplog.clear()
    db.release_session_turn_lease("s1", _holder("stale"))
    assert _events(caplog) == []


def test_rolled_back_delete_is_reported_as_rolled_back(db, caplog):
    sid, holder = "s1", _holder()
    db.create_session(sid, source="desktop")
    assert db.try_acquire_session_turn_lease(sid, holder)
    caplog.clear()

    def _delete_then_fail(conn):
        assert custody.delete_row(conn, "release", sid, holder, store=db.db_path) == 1
        raise RuntimeError("abort this transaction")

    with pytest.raises(RuntimeError):
        db._execute_write(_delete_then_fail)
    assert _ops(caplog) == ["release", "rolled_back"]
    assert _lease_rows(db) == [(sid, holder)]
    assert db.append_messages_batch(sid, MSG, turn_lease_holder=holder) == 1


def test_sweep_and_stale_reclaim_name_the_rows_they_removed(db, caplog):
    for sid in ("old", "lapsed", "fresh"):
        db.create_session(sid, source="desktop")
    old, lapsed, new = _holder("old"), _holder("lapsed"), _holder("new")
    assert db.try_acquire_session_turn_lease("old", old)
    assert db.try_acquire_session_turn_lease("lapsed", lapsed)
    now = time.time()
    db._execute_write(lambda conn: conn.executemany(
        "UPDATE session_turn_leases SET expires_at = ? WHERE conversation_id = ?",
        [(now - 2 * 86400, "old"), (now - 5, "lapsed")]))
    caplog.clear()
    assert db.try_acquire_session_turn_lease("fresh", new)
    swept = [e for e in _events(caplog) if e.get("op") == "sweep"]
    assert [(e["key"], e["row_holder"], e["changed"]) for e in swept] == [(custody.tag("old"), custody.tag(old), "1")]
    caplog.clear()
    assert db.try_acquire_session_turn_lease("lapsed", new + "2")
    reclaim, claim, settled = _events(caplog)
    assert (reclaim["op"], reclaim["row_holder"], reclaim["changed"], reclaim["why"]) == (
        "reclaim", custody.tag(lapsed), "1", "stale_on_acquire")
    assert (claim["op"], claim["why"], settled["phase"], settled["events"]) == ("claim", "owned=1", "committed", "2")
    with pytest.raises(SessionTurnLeaseLostError, match="holder mismatch"):
        db.append_messages_batch("lapsed", MSG, turn_lease_holder=lapsed)


def test_leaseless_writer_reclaim_and_worker_deletes_are_reported(db, caplog):
    from hermes_state_runtime import _worker_turn
    sid, holder = "s1", _holder()
    db.create_session(sid, source="desktop")
    assert db.try_acquire_session_turn_lease(sid, holder)
    db._execute_write(lambda conn: conn.execute("UPDATE session_turn_leases SET expires_at = ?", (time.time() - 5,)))
    caplog.clear()
    db._execute_write(lambda conn: db._check_transcript_write_guards(conn, sid, None, reject_active_turn_lease=True))
    assert _ops(caplog) == ["reclaim", "committed"]
    assert next(e["why"] for e in _events(caplog)) == "stale_on_leaseless_write"
    assert db.try_acquire_session_turn_lease(sid, holder)
    caplog.clear()
    db._execute_write(lambda conn: _worker_turn(db, conn, sid, {"holder": holder}, "turn.release"))
    assert _ops(caplog) == ["worker_release", "committed"]
    assert _lease_rows(db) == []


def test_distinct_stores_carry_distinct_tags(db, caplog, tmp_path):
    other = SessionDB(tmp_path / "other" / "state.db")
    try:
        for store in (db, other):
            store.create_session("s1", source="desktop")
            assert store.try_acquire_session_turn_lease("s1", _holder())
        stores = {e["store"] for e in _events(caplog) if e.get("op") == "claim"}
        assert stores == {custody.tag(db.db_path), custody.tag(other.db_path)} and len(stores) == 2
    finally:
        other.close()


def test_admission_readback_reports_only_an_absent_row(db, caplog):
    from agent.turn_facade_lease import LEASE_TTL_SECONDS, _committed_lease_expiry
    sid, holder = "s1", _holder()
    db.create_session(sid, source="desktop")
    assert db.try_acquire_session_turn_lease(sid, holder)
    caplog.clear()
    assert _committed_lease_expiry(db, sid, holder, 100.0) > time.time()
    assert _events(caplog) == []
    db.release_session_turn_lease(sid, holder)
    caplog.clear()
    assert _committed_lease_expiry(db, sid, holder, 100.0) == 100.0 + LEASE_TTL_SECONDS
    absent = _events(caplog)[0]
    assert (absent["phase"], absent["op"], absent["holder"]) == ("admission", "readback_absent", custody.tag(holder))
    assert _lease_rows(db) == []  # the readback never restores custody


def test_a_failing_logger_changes_no_admission_and_no_refusal(db, monkeypatch):
    def _fail(*args, **kwargs):
        raise RuntimeError("logger unavailable")
    for name in ("log", "info", "warning"):
        monkeypatch.setattr(custody._LOG, name, _fail)
    sid, holder = "s1", _holder()
    db.create_session(sid, source="desktop")
    assert db.try_acquire_session_turn_lease(sid, holder)
    assert db.append_messages_batch(sid, MSG, turn_lease_holder=holder) == 1
    db.release_session_turn_lease(sid, holder)
    with pytest.raises(SessionTurnLeaseLostError, match="missing lease row"):
        db.append_messages_batch(sid, MSG, turn_lease_holder=holder)
    custody.admission_readback(db, sid, holder, None)
    assert _lease_rows(db) == []
