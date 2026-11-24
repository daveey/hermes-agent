"""Durable fanout replay must survive failures before and after action commit."""
import contextlib
import json

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from tui_gateway import notice_fanout as nf
from tui_gateway import notice_fanout_state as state
from tests.tui_gateway import test_session_outcome_notices as helpers


db = helpers.db


def test_chat_hosted_elsewhere_does_not_hold_the_board_cursor(db, monkeypatch, caplog):
    foreign = "20261123_100853_foreign"
    monkeypatch.setattr(helpers.server, "_readopt_wake_profiles", lambda: [None])

    @contextlib.contextmanager
    def profile_db(session, **kwargs):
        yield db

    monkeypatch.setattr(helpers.server, "_profile_db", profile_db)
    original = helpers.ku.find_session_db
    opened = []

    @contextlib.contextmanager
    def tracked(chat):
        opened.append(chat)
        with original(chat) as result:
            yield result

    monkeypatch.setattr(helpers.ku, "find_session_db", tracked)
    nf.card_change_pass()
    local_tid = helpers._card(done=False)
    foreign_tid = helpers._card(done=False, chat=foreign)
    with caplog.at_level("INFO", logger=nf.__name__):
        rows = nf.card_change_pass()
        with contextlib.closing(kbc.connect()) as conn:
            top = conn.execute("SELECT MAX(id) FROM task_events").fetchone()[0]
        state.forget_memory_for_tests()
        assert state.get_cursor(str(kb.kanban_db_path().resolve())) == top
        assert {r["task_id"]: r["skipped"] for r in rows} == {
            local_tid: None, foreign_tid: "not_hosted_here"}
        assert foreign not in opened and helpers.CHAT in opened
        assert helpers.so.get_outcome(helpers.CHAT, db=db)["on"][0]["id"] == local_tid
        with contextlib.closing(kbc.connect()) as conn:
            kb.complete_task(conn, foreign_tid, summary="done")
        nf.card_change_pass()
    assert sum(foreign in record.message for record in caplog.records) == 1


def test_unknown_hosting_keeps_cursor(db, monkeypatch):
    nf.card_change_pass(session_db=helpers._opener(db))
    ident = str(kb.kanban_db_path().resolve())
    before = state.get_cursor(ident)
    tid = helpers._card(done=False)

    @contextlib.contextmanager
    def missing(chat):
        yield None, None

    with monkeypatch.context() as m:
        m.setattr(helpers.ku, "chat_hosted_here", lambda chat: False)
        result = nf.card_change_pass(session_db=missing, hosted=lambda chat: None)
    assert result.incomplete
    state.forget_memory_for_tests()
    assert state.get_cursor(ident) == before
    rows = nf.card_change_pass(session_db=helpers._opener(db))
    assert [r["task_id"] for r in rows] == [tid]
    assert state.get_cursor(ident) > before


def test_chat_hosted_here_reports_absent_only_when_every_store_opened(db, tmp_path, monkeypatch):
    from hermes_state import SessionDB
    with contextlib.closing(SessionDB(tmp_path / "second.db")) as second:
        second.create_session("second-chat", "desktop")
        monkeypatch.setattr(helpers.server, "_readopt_wake_profiles", lambda: [None, "second"])
        broken = []

        @contextlib.contextmanager
        def profile_db(session, **kwargs):
            if session is None and broken:
                raise OSError("profile unavailable")
            yield second if session else db

        monkeypatch.setattr(helpers.server, "_profile_db", profile_db)
        assert helpers.ku.chat_hosted_here(helpers.CHAT) is True
        assert helpers.ku.chat_hosted_here("second-chat") is True
        assert helpers.ku.chat_hosted_here("absent") is False
        broken.append(True)
        assert helpers.ku.chat_hosted_here("absent") is None
        assert helpers.ku.chat_hosted_here("second-chat") is True
        nf.card_change_pass()
        tid = helpers._card(done=False, chat="absent")
        before = state.get_cursor(str(kb.kanban_db_path().resolve()))
        assert nf.card_change_pass().incomplete
        state.forget_memory_for_tests()
        assert state.get_cursor(str(kb.kanban_db_path().resolve())) == before
        broken.clear()
        assert [(r["task_id"], r["skipped"]) for r in nf.card_change_pass()] == [(tid, "not_hosted_here")]


def test_stale_metadata_cas_loss_keeps_durable_cursor(db, monkeypatch):
    tid = helpers._card(done=False)
    with contextlib.closing(kbc.connect()) as conn:
        kb.block_task(conn, tid, reason="registry unavailable")
    helpers.ku.sweep_once(set(), now=helpers.LATER, session_db=helpers._opener(db))
    [notice] = helpers._notices(db)
    nf.card_change_pass(session_db=helpers._opener(db))
    ident = str(kb.kanban_db_path().resolve())
    before = state.get_cursor(ident)
    with contextlib.closing(kbc.connect()) as conn:
        kb.complete_task(conn, tid, summary="done")
    original = db._write_rowcount
    raced = []

    def concurrent_metadata(sql, params=()):
        if sql.startswith("UPDATE messages SET display_metadata") and not raced:
            raced.append(True)
            original("UPDATE messages SET display_metadata = ? WHERE id = ?",
                     (json.dumps({"concurrent": "preserve"}), notice["id"]))
        return original(sql, params)

    with monkeypatch.context() as m:
        m.setattr(db, "_write_rowcount", concurrent_metadata)
        nf.card_change_pass(session_db=helpers._opener(db))
    assert raced
    state.forget_memory_for_tests()
    assert state.get_cursor(ident) == before
    nf.card_change_pass(session_db=helpers._opener(db))
    [updated] = helpers._notices(db)
    assert updated["display_metadata"] == {
        "concurrent": "preserve", "stale_tasks": [tid], "stale": True}
    assert state.get_cursor(ident) > before


def test_failed_action_does_not_ack_and_replays_after_restart(db, monkeypatch):
    tid = helpers._card(done=False)
    opener = helpers._opener(db)
    nf.card_change_pass(session_db=opener)
    ident = str(kb.kanban_db_path().resolve())
    before = state.get_cursor(ident)
    with contextlib.closing(kbc.connect()) as conn:
        kb.complete_task(conn, tid, summary="done")
    original = nf._fan_out_changes

    def crash(*args):
        raise RuntimeError("crash before canonical action")

    monkeypatch.setattr(nf, "_fan_out_changes", crash)
    nf.card_change_pass(session_db=opener)
    state.forget_memory_for_tests()
    assert state.get_cursor(ident) == before
    monkeypatch.setattr(nf, "_fan_out_changes", original)
    changed = nf.card_change_pass(session_db=opener)
    assert [row["task_id"] for row in changed] == [tid]
    assert state.get_cursor(ident) > before
    assert nf.card_change_pass(session_db=opener) == []


def test_source_rewind_never_reseeds_durable_cursor(db):
    helpers._card(done=False)
    nf.card_change_pass(session_db=helpers._opener(db))
    ident = str(kb.kanban_db_path().resolve())
    before = state.get_cursor(ident)
    assert before is not None and before > 0
    with contextlib.closing(kbc.connect()) as conn:
        conn.execute("DELETE FROM task_events")
        conn.commit()
        with pytest.raises(RuntimeError, match="regressed"):
            nf._changed_tasks(conn, ident)
    nf.card_change_pass(session_db=helpers._opener(db))
    state.forget_memory_for_tests()
    assert state.get_cursor(ident) == before


def test_bounded_batch_keeps_older_events(db, monkeypatch):
    opener = helpers._opener(db)
    tids = [helpers._card(done=False) for _ in range(3)]
    nf.card_change_pass(session_db=opener)
    with contextlib.closing(kbc.connect()) as conn:
        for tid in tids:
            kb.complete_task(conn, tid, summary="done")
    monkeypatch.setattr(nf, "MAX_EVENTS_PER_PASS", 2)
    first = nf.card_change_pass(session_db=opener)
    second = nf.card_change_pass(session_db=opener)
    assert [row["task_id"] for row in first + second] == tids


@pytest.mark.parametrize("failure", ["missing_db", "stale_write", "outcome"])
def test_incomplete_action_keeps_cursor(db, monkeypatch, failure):
    tid = helpers._card(done=False)
    opener = helpers._opener(db)
    nf.card_change_pass(session_db=opener)
    ident = str(kb.kanban_db_path().resolve())
    before = state.get_cursor(ident)
    with contextlib.closing(kbc.connect()) as conn:
        kb.complete_task(conn, tid, summary="done")
    with monkeypatch.context() as m:
        if failure == "missing_db":
            @contextlib.contextmanager
            def missing(chat):
                yield None, None
            opener = missing
        elif failure == "stale_write":
            def broken(*args):
                raise RuntimeError("write unavailable")
            m.setattr(nf, "mark_stale_blocked_notices", broken)
        else:
            m.setattr(helpers.so, "refresh_outcomes_quietly", lambda *a, **k: 0)
        nf.card_change_pass(session_db=opener)
    state.forget_memory_for_tests()
    assert state.get_cursor(ident) == before
    assert [r["task_id"] for r in nf.card_change_pass(session_db=helpers._opener(db))] == [tid]


def test_failed_durable_ack_does_not_advance_memory(db, monkeypatch):
    assert state.set_cursor("board", 4) is not False
    original = state._open

    @contextlib.contextmanager
    def unavailable():
        yield None

    monkeypatch.setattr(state, "_open", unavailable)
    assert state.set_cursor("board", 9) is False
    assert state.get_cursor("board") == 4
    monkeypatch.setattr(state, "_open", original)
    state.forget_memory_for_tests()
    assert state.get_cursor("board") == 4


@pytest.mark.parametrize("operation", ["seed", "ack"])
def test_remote_snapshot_never_acknowledges_only_in_memory(db, monkeypatch, operation):
    old = {"chat": frozenset({("owner/board/card", "blocked")})}
    new = {"chat": frozenset({("owner/board/card", "done")})}
    state.seed_remote(old)

    @contextlib.contextmanager
    def unavailable():
        yield None

    with monkeypatch.context() as m:
        m.setattr(state, "_open", unavailable)
        write = state.seed_remote if operation == "seed" else state.ack_remote
        assert write(new) is False
        assert state.load_remote() == old
    state.forget_memory_for_tests()
    assert state.load_remote() == old


def test_action_commit_before_ack_is_replayed_idempotently(db, monkeypatch):
    tid = helpers._card(done=False)
    opener = helpers._opener(db)
    nf.card_change_pass(session_db=opener)
    ident = str(kb.kanban_db_path().resolve())
    before = state.get_cursor(ident)
    with contextlib.closing(kbc.connect()) as conn:
        kb.complete_task(conn, tid, summary="done")
    with monkeypatch.context() as m:
        m.setattr(state, "set_cursor", lambda *a: False)
        assert [r["task_id"] for r in nf.card_change_pass(session_db=opener)] == [tid]
    state.forget_memory_for_tests()
    assert state.get_cursor(ident) == before
    before_messages = db.get_messages(helpers.CHAT)
    assert [r["task_id"] for r in nf.card_change_pass(session_db=opener)] == [tid]
    assert db.get_messages(helpers.CHAT) == before_messages
    assert nf.card_change_pass(session_db=opener) == []
