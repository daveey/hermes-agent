"""A spend stop needs a decision, never a timer, and exactly one producer."""
from __future__ import annotations

import time

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_notify as notify
from hermes_cli import kanban_spend_budget as budget
from hermes_cli import kanban_spend_decision as decision
from hermes_cli import kanban_triage_sweep as sweep
from tests.hermes_cli import test_kanban_spend_budget as helpers

kanban_home = helpers.kanban_home


def stopped(conn):
    tid = kb.create_task(conn, title="Bounded work", assignee="default")
    with kb.write_txn(conn):
        kb._append_event(conn, tid, "blocked", {"reason": "old wait"})
    helpers._runs(conn, tid, budget.DEFAULT_RUNS, usage={"cache_read_tokens": 1_200_000})
    return tid


def test_stale_sweep_cannot_release_spend_stop(kanban_home):
    with kbc.connect_closing() as conn:
        tid = stopped(conn)
        assert budget.hold(conn, tid) == "triage"
        future = int(time.time()) + 7200
        assert tid not in sweep.sweep_stale_triage(conn, now=future)
        assert kb.get_task(conn, tid).status == "triage"
        assert not helpers._events(conn, tid, "triage_swept")


@pytest.mark.parametrize("origin", [False, True])
def test_owned_stop_has_one_chat_notice_and_no_supervisor(kanban_home, origin):
    from gateway import kanban_watchers_notifier as watcher
    from tui_gateway.session_notifications import _format_kanban_event_text

    with kbc.connect_closing() as conn:
        tid = stopped(conn)
        if origin:
            with kb.write_txn(conn):
                conn.execute("UPDATE tasks SET session_id='human-chat' WHERE id=?", (tid,))
        else:
            notify.add_notify_sub(conn, task_id=tid, platform="tui", chat_id="human-chat")
        assert budget.hold(conn, tid) == "triage"
        budget.hold(conn, tid)
        (event,) = helpers._events(conn, tid, decision.NOTICE)
        assert event.payload["producer"]["kind"] == "chats"
        assert decision.NOTICE in watcher.TERMINAL_KINDS
        assert decision.NOTICE in watcher._WAKE_KINDS
        text = _format_kanban_event_text({"task_id": tid}, kb.get_task(conn, tid), event, "default")
        assert "15 runs" in text and "18.0M" in text
        assert "promote" in text and "archive" in text
        assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 1
        _, events = notify.unseen_events_for_sub(conn, task_id=tid, platform="tui", chat_id="human-chat",
                                              kinds=(decision.NOTICE,))
        assert len(events) == 1


@pytest.mark.parametrize("observer", [None, "touch", "shepherd"])
def test_unowned_stop_has_one_supervisor(kanban_home, observer):
    with kbc.connect_closing() as conn:
        tid = stopped(conn)
        if observer:
            notify.add_notify_sub(conn, task_id=tid, platform="tui", chat_id="observer",
                                  delivery_metadata={"reason": observer})
        assert budget.hold(conn, tid) == "triage"
        budget.hold(conn, tid)
        (event,) = helpers._events(conn, tid, decision.NOTICE)
        producer = event.payload["producer"]
        assert producer["kind"] == "supervisor"
        supervisor = kb.get_task(conn, producer["task_id"])
        assert supervisor.assignee == "default"
        assert tid in supervisor.body and "15 runs" in supervisor.body
        assert "promote" in supervisor.body and "archive" in supervisor.body
        assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 2
        assert conn.execute("SELECT COUNT(*) FROM kanban_notify_subs WHERE task_id=?", (tid,)).fetchone()[0] == int(bool(observer))


@pytest.mark.parametrize("status", ["ready", "review"])
def test_unrecorded_release_cannot_spawn_even_with_limits_disabled(kanban_home, status):
    with kbc.connect_closing() as conn:
        tid = stopped(conn)
        budget.hold(conn, tid)
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status=? WHERE id=?", (status, tid))
        assert budget.hold(conn, tid, lim=budget.Limits(0, 0)) == "triage"
        assert kb.get_task(conn, tid).status == "triage"
        assert len(helpers._events(conn, tid, budget.EVENT)) == 1


def test_failure_recovery_cannot_release_spend_stop_after_old_gave_up(kanban_home):
    """A card that once exhausted its failure budget keeps ``block_kind='transient'`` and that ``gave_up`` as its
    latest failure stop. When the spend budget later stops it, ``recompute_ready``'s agent-triage recovery must not
    read the old ``gave_up`` as the reason it is in triage. Otherwise it promotes the card, the dispatcher's hold
    sends it back to triage, and every tick repeats that (live: 137 ``promoted`` events in 34 min on one card)."""
    from hermes_cli import kanban_db_dispatch as dispatch
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="Bounded work", assignee="default")
        assert kb.claim_task(conn, tid)
        assert dispatch._record_task_failure(conn, tid, "Worker crashed", outcome="crashed",
                                             failure_limit=1, release_claim=True, end_run=True)
        assert (kb.get_task(conn, tid).status, kb.get_task(conn, tid).block_kind) == ("triage", "transient")
        assert kb.recompute_ready(conn, failure_limit=100) == 1  # the failure-budget recovery itself is unchanged
        assert kb.get_task(conn, tid).status == "ready"
        helpers._runs(conn, tid, budget.DEFAULT_RUNS, usage={"cache_read_tokens": 1_200_000})
        assert budget.hold(conn, tid) == "triage"
        promoted = len(helpers._events(conn, tid, "promoted"))
        for _tick in range(3):
            assert kb.recompute_ready(conn, failure_limit=100) == 0
            assert kb.get_task(conn, tid).status == "triage"
            assert budget.hold(conn, tid) == "triage"
        assert len(helpers._events(conn, tid, "promoted")) == promoted
        assert decision.pending(conn, tid)
        # A recorded decision still releases it, and a later failure stop recovers as before.
        assert kb.promote_task(conn, tid, actor="operator", reason="rescope")[0]
        assert kb.get_task(conn, tid).status in ("ready", "todo")
        assert kb.claim_task(conn, tid)
        assert dispatch._record_task_failure(conn, tid, "Worker crashed", outcome="crashed",
                                             failure_limit=1, release_claim=True, end_run=True)
        assert kb.get_task(conn, tid).status == "triage"
        assert kb.recompute_ready(conn, failure_limit=100) == 1


def test_decomposer_cannot_release_spend_stop(kanban_home):
    from hermes_cli.kanban_decompose import list_triage_ids
    with kbc.connect_closing() as conn:
        tid = stopped(conn)
        budget.hold(conn, tid)
        assert tid not in list_triage_ids()
        assert kb.get_task(conn, tid).status == "triage"


def test_sweep_rechecks_spend_stop_inside_write_lock(kanban_home, monkeypatch):
    from contextlib import contextmanager
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="race", assignee="default", triage=True)
        real = kb.write_txn
        @contextmanager
        def racing(c, **kw):
            monkeypatch.setattr(kb, "write_txn", real)
            with real(c):
                kb._append_event(c, tid, budget.EVENT, {})
            with real(c, **kw):
                yield
        monkeypatch.setattr(kb, "write_txn", racing)
        assert tid not in sweep.sweep_stale_triage(conn, now=int(time.time()) + 7200)
        assert kb.get_task(conn, tid).status == "triage"


def test_manual_decision_clears_hold_and_later_triage_can_sweep(kanban_home):
    from hermes_cli.kanban_spend_decision import pending
    with kbc.connect_closing() as conn:
        tid = stopped(conn)
        budget.hold(conn, tid)
        assert pending(conn, tid)
        assert kb.promote_task(conn, tid, actor="operator", reason="rescope")[0]
        assert not pending(conn, tid)
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status='triage' WHERE id=?", (tid,))
            kb._append_event(conn, tid, "status", {"status": "triage"})
        assert tid in sweep.sweep_stale_triage(conn, now=int(time.time()) + 7200)


def test_gateway_notice_contains_spend_and_decision(kanban_home):
    from types import SimpleNamespace
    from gateway.kanban_watchers_notifier import _EVENT_FORMATTERS
    from hermes_cli.kanban_spend_decision import notice
    payload = {"reason": "Spend budget: 15 runs, 18.0M cache-read"}
    text, body, _ = _EVENT_FORMATTERS[decision.NOTICE](SimpleNamespace(payload=payload), SimpleNamespace(head="card"))
    assert notice(payload) == body
    assert "15 runs" in text and "promote" in text and "archive" in text


@pytest.mark.parametrize("prior_sweep", [False, True])
def test_producer_failure_keeps_hold_and_sweep_retries_once(kanban_home, monkeypatch, prior_sweep):
    with kbc.connect_closing() as conn:
        tid = stopped(conn)
        if prior_sweep:
            with kb.write_txn(conn):
                conn.execute("UPDATE tasks SET status='triage' WHERE id=?", (tid,))
            assert tid in sweep.sweep_stale_triage(conn, now=int(time.time()) + 7200)
        real = decision.producer
        def fail(c, task_id, note):
            real(c, task_id, note)  # even a partial publication must roll back
            raise RuntimeError("injected publication failure")
        monkeypatch.setattr(decision, "producer", fail)
        assert budget.hold(conn, tid) == "triage"
        assert kb.get_task(conn, tid).status == "triage"
        assert not helpers._events(conn, tid, decision.NOTICE)
        assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 1
        monkeypatch.setattr(decision, "producer", real)
        for _ in range(2):
            assert tid not in sweep.sweep_stale_triage(conn, now=int(time.time()) + 7200)
        assert len(helpers._events(conn, tid, decision.NOTICE)) == 1
        assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 2


def test_repair_does_not_create_owner_after_decision(kanban_home):
    with kbc.connect_closing() as conn:
        tid = stopped(conn)
        decision.ensure_producer(conn, tid)
        assert not helpers._events(conn, tid, decision.NOTICE)


@pytest.mark.parametrize("released", [False, True])
def test_link_only_owner_gets_notice_unless_released(kanban_home, released):
    with kbc.connect_closing() as conn:
        tid = stopped(conn)
        with kb.write_txn(conn):
            conn.execute("CREATE TABLE task_chat_links (task_id TEXT, chat_gateway TEXT, chat_profile TEXT, "
                         "chat_session TEXT, released INTEGER, created_at INTEGER)")
            conn.execute("INSERT INTO task_chat_links VALUES (?, 'remote', 'named', 'human-chat', ?, 0)",
                         (tid, int(released)))
        budget.hold(conn, tid)
        (event,) = helpers._events(conn, tid, decision.NOTICE)
        assert event.payload["producer"]["kind"] == ("supervisor" if released else "chats")
        if not released:
            _, events = notify.unseen_events_for_sub(conn, task_id=tid, platform="hub",
                                                     chat_id="remote/named/human-chat", kinds=(decision.NOTICE,))
            assert len(events) == 1


def test_pending_rechecks_manual_decision_inside_lock(kanban_home, monkeypatch):
    from contextlib import contextmanager
    with kbc.connect_closing() as conn:
        tid = stopped(conn)
        budget.hold(conn, tid)
        real = kb.write_txn
        @contextmanager
        def racing(c, **kw):
            monkeypatch.setattr(kb, "write_txn", real)
            assert kb.promote_task(c, tid, actor="operator", reason="rescope")[0]
            with real(c, **kw):
                yield
        monkeypatch.setattr(kb, "write_txn", racing)
        assert budget.hold(conn, tid) is None
        assert kb.get_task(conn, tid).status == "ready"


def test_headless_origin_does_not_own_stop(kanban_home):
    with kbc.connect_closing() as conn:
        tid = stopped(conn)
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET session_id=? WHERE id=?", ("local-" + "a" * 64, tid))
        budget.hold(conn, tid)
        (event,) = helpers._events(conn, tid, decision.NOTICE)
        assert event.payload["producer"]["kind"] == "supervisor"


def test_archived_stop_needs_no_producer(kanban_home):
    with kbc.connect_closing() as conn:
        tid = stopped(conn)
        with kb.write_txn(conn):
            kb._append_event(conn, tid, budget.EVENT, {})
            kb._append_event(conn, tid, "archived", {})
        assert not decision.pending(conn, tid)
        decision.ensure_producer(conn, tid)
        assert not helpers._events(conn, tid, decision.NOTICE)

