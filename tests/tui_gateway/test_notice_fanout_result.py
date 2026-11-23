"""Scheduling must distinguish durable completion from an empty failed pass."""
import contextlib

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from tui_gateway import notice_fanout as nf
from tui_gateway import notice_fanout_state as state
from tests.tui_gateway import test_session_outcome_notices as helpers


db = helpers.db


def test_failed_cursor_commit_reports_retry_with_completed_actions(db, monkeypatch):
    tid = helpers._card(done=False)
    opener = helpers._opener(db)
    nf.card_change_pass(session_db=opener)
    ident = str(kb.kanban_db_path().resolve())
    before = state.get_cursor(ident)
    with contextlib.closing(kbc.connect()) as conn:
        kb.complete_task(conn, tid, summary="done")
    with monkeypatch.context() as m:
        m.setattr(state, "set_cursor", lambda *a: False)
        result = nf.card_change_pass(session_db=opener)
    assert [r["task_id"] for r in result] == [tid]
    assert result.incomplete
    assert result.next_deadline is not None
    assert not result.quiet
    state.forget_memory_for_tests()
    assert state.get_cursor(ident) == before


@pytest.mark.parametrize("failure", ["action", "source", "remote"])
def test_empty_failure_is_not_quiet(db, monkeypatch, failure):
    tid = helpers._card(done=False)
    opener = helpers._opener(db)
    nf.card_change_pass(session_db=opener)
    with contextlib.closing(kbc.connect()) as conn:
        kb.complete_task(conn, tid, summary="done")

    def broken(*args, **kwargs):
        raise RuntimeError("injected unavailable")

    target = {"action": "_fan_out_changes", "source": "_changed_tasks", "remote": "_remote_change_pass"}[failure]
    monkeypatch.setattr(nf, target, broken)
    result = nf.card_change_pass(session_db=opener)
    assert result.incomplete
    assert result.next_deadline is not None


def test_bounded_batch_has_continuation_without_another_event(db, monkeypatch):
    tids = [helpers._card(done=False) for _ in range(3)]
    opener = helpers._opener(db)
    nf.card_change_pass(session_db=opener)
    with contextlib.closing(kbc.connect()) as conn:
        for tid in tids:
            kb.complete_task(conn, tid, summary="done")
    monkeypatch.setattr(nf, "MAX_EVENTS_PER_PASS", 2)
    first = nf.card_change_pass(session_db=opener)
    assert first.incomplete
    assert first.next_deadline is not None
    second = nf.card_change_pass(session_db=opener)
    assert [r["task_id"] for r in first + second] == tids


@pytest.mark.parametrize("failure", ["missing_db", "outcome", "ack", "seed"])
def test_remote_swallowed_failure_reports_retry(db, monkeypatch, failure):
    from hermes_cli import kanban_session_activity as activity
    if failure != "seed":
        assert state.seed_remote({})
    monkeypatch.setattr(nf, "_remote_inputs_key", lambda: None)
    monkeypatch.setattr(activity, "_remote_cards", lambda *a, **k: [
        {"key": "remote/default/card", "id": "card", "board": "default", "status": "blocked", "session": helpers.CHAT}])
    opener = helpers._opener(db)
    if failure == "missing_db":
        @contextlib.contextmanager
        def missing(chat):
            yield None, None
        opener = missing
    elif failure == "outcome":
        monkeypatch.setattr(helpers.so, "refresh_outcomes_quietly", lambda *a, **k: 0)
    else:
        monkeypatch.setattr(state, "ack_remote" if failure == "ack" else "seed_remote", lambda *a: False)
    result = nf.card_change_pass(session_db=opener, slugs=[])
    assert result.incomplete
    assert result.next_deadline is not None
    assert state.load_remote() == (None if failure == "seed" else {})


def test_remote_action_never_certifies_stale_display_projection(db, monkeypatch):
    from hermes_cli import kanban_session_activity as activity
    calls = []
    monkeypatch.setattr(nf, "_remote_inputs_key", lambda: None)

    def cards(*args, **kwargs):
        calls.append(kwargs["stale_ok"])
        return []

    monkeypatch.setattr(activity, "_remote_cards", cards)
    result = nf.card_change_pass(session_db=helpers._opener(db), slugs=[])
    assert calls == [False]
    assert result.quiet
    assert result.next_deadline is None


def test_remote_input_failure_retains_retry_after_successful_fallback(db, monkeypatch):
    from hermes_cli import kanban_session_activity as activity

    def broken():
        raise OSError("identity unavailable")

    monkeypatch.setattr(nf, "_remote_inputs_key", broken)
    monkeypatch.setattr(activity, "_remote_cards", lambda *a, **k: [])
    result = nf.card_change_pass(session_db=helpers._opener(db), slugs=[])
    assert result.incomplete
    assert result.next_deadline is not None


def test_board_enumeration_failure_is_not_quiet(db, monkeypatch):
    def broken(*args, **kwargs):
        raise OSError("roster unavailable")
    monkeypatch.setattr(kb, "list_boards", broken)
    monkeypatch.setattr(kb, "read_board_metadata", broken)
    result = nf.card_change_pass(session_db=helpers._opener(db))
    assert result.incomplete
    assert result.next_deadline is not None


@pytest.mark.parametrize("kind", ["local", "remote"])
def test_unreadable_durable_state_never_seeds_over_existing_cursor(db, monkeypatch, kind):
    helpers._card(done=False)
    opener = helpers._opener(db)
    nf.card_change_pass(session_db=opener)
    state.forget_memory_for_tests()
    original = state._open
    calls = []

    @contextlib.contextmanager
    def transient_read_failure():
        calls.append(True)
        if len(calls) == 1:
            yield None
        else:
            with original() as conn:
                yield conn

    with monkeypatch.context() as m:
        m.setattr(state, "_open", transient_read_failure)
        if kind == "local":
            ident = str(kb.kanban_db_path().resolve())
            with contextlib.closing(kbc.connect()) as conn:
                with pytest.raises(RuntimeError, match="unavailable"):
                    nf._changed_tasks(conn, ident)
        else:
            with pytest.raises(RuntimeError, match="unavailable"):
                nf._remote_change_pass(opener)
    assert len(calls) == 1


def test_bookkeeping_only_cursor_ack_failure_is_not_idle(db, monkeypatch):
    tid = helpers._card(done=False)
    opener = helpers._opener(db)
    nf.card_change_pass(session_db=opener)
    ident = str(kb.kanban_db_path().resolve())
    before = state.get_cursor(ident)
    with contextlib.closing(kbc.connect()) as conn:
        conn.execute("INSERT INTO task_events (task_id, kind, created_at) VALUES (?, 'heartbeat', 1)", (tid,))
        conn.commit()
    monkeypatch.setattr(state, "set_cursor", lambda *a: False)
    result = nf.card_change_pass(session_db=opener)
    assert result == []
    assert result.incomplete
    assert result.next_deadline is not None
    assert state.get_cursor(ident) == before
