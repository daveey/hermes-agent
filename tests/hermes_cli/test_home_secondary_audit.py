"""Owner-chat maintenance skips frozen custody without starving healthy cards."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from hermes_cli import kanban_db as kb, kanban_db_connect as dbc
from hermes_cli import kanban_owner_chat as owner
from tests.hermes_cli import test_kanban_spend_allowance as fixtures
from tests.hermes_cli.test_home_repair_allowance_custody import freeze, KINDS, snapshot

kanban_home = fixtures.kanban_home
primary = fixtures.primary


@pytest.fixture
def sessions(monkeypatch):
    calls = []
    monkeypatch.setattr(owner, "_UNLINKABLE", set())
    monkeypatch.setattr(owner, "_liveness", lambda: (
        SimpleNamespace(close=lambda: None), lambda sid: sid in {owner.session_id(p) for p in calls}))

    def ensure(producer, **kwargs):
        calls.append(producer)
        return owner.session_id(producer)

    monkeypatch.setattr(owner, "ensure", ensure)
    return calls


def two_cards(conn):
    first = kb.create_task(conn, title="first ownerless", created_by="audit-first")
    later = kb.create_task(conn, title="later ownerless", created_by="audit-later")
    with dbc.write_txn(conn):
        conn.execute("UPDATE tasks SET created_at=1 WHERE id=?", (first,))
        conn.execute("UPDATE tasks SET created_at=2 WHERE id=?", (later,))
    return first, later


def custody_snapshot(conn, task):
    return snapshot(conn, task) + [[tuple(r) for r in conn.execute(sql, (task,))] for sql in (
        "SELECT * FROM task_chat_links WHERE task_id=? ORDER BY uid",
        "SELECT * FROM kanban_notify_subs WHERE task_id=? ORDER BY chat_id")]


def test_unfrozen_backfill_links_both_cards(primary, sessions):
    first, later = two_cards(primary)
    result = owner.backfill(primary, apply=True, limit=50)
    assert result["linked"] == 2
    for tid in (first, later):
        assert primary.execute("SELECT 1 FROM task_events WHERE task_id=? AND kind=?", (tid, owner.EVENT)).fetchone()
    assert sessions == ["audit-first", "audit-later"]


@pytest.mark.parametrize("kind", KINDS)
def test_backfill_skips_frozen_before_session_creation(primary, sessions, kind):
    first, later = two_cards(primary)
    freeze(primary, first, kind)
    before = custody_snapshot(primary, first)
    selected = owner.ownerless(primary, lambda sid: False)
    assert [tid for tid, _ in selected] == [later]
    result = owner.backfill(primary, apply=True, limit=1)
    assert result["linked"] == 1
    assert sessions == ["audit-later"]
    assert custody_snapshot(primary, first) == before
    assert first not in owner._UNLINKABLE
    assert primary.execute("SELECT 1 FROM task_events WHERE task_id=? AND kind=?", (later, owner.EVENT)).fetchone()


@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("seam", ["selection", "ensure", "writer"])
def test_custody_arriving_during_backfill_does_not_abort_neighbors(primary, sessions, monkeypatch, kind, seam):
    from contextlib import contextmanager

    first, later = two_cards(primary)
    before = []
    original_selection = owner.ownerless
    original_ensure = owner.ensure
    original_writer = kb.write_txn
    armed = [False]

    def take_custody():
        freeze(primary, first, kind)
        before.append(custody_snapshot(primary, first))
        armed[0] = False

    def selection(*args, **kwargs):
        result = original_selection(*args, **kwargs)
        if seam == "selection":
            take_custody()
        return result

    def ensure(producer, **kwargs):
        sid = original_ensure(producer, **kwargs)
        if producer == "audit-first":
            if seam == "ensure":
                take_custody()
            elif seam == "writer":
                armed[0] = True
        return sid

    @contextmanager
    def writer(conn, *args, **kwargs):
        if armed[0]:
            take_custody()
        with original_writer(conn, *args, **kwargs):
            yield

    monkeypatch.setattr(owner, "ownerless", selection)
    monkeypatch.setattr(owner, "ensure", ensure)
    monkeypatch.setattr(kb, "write_txn", writer)
    result = owner.backfill(primary, apply=True, limit=50)
    assert result["linked"] == 1
    assert custody_snapshot(primary, first) == before[0]
    assert first not in owner._UNLINKABLE
    assert sessions == (["audit-later"] if seam == "selection" else ["audit-first", "audit-later"])
    assert primary.execute("SELECT 1 FROM task_events WHERE task_id=? AND kind=?", (later, owner.EVENT)).fetchone()


@pytest.mark.parametrize("kind", KINDS)
def test_relink_itself_refuses_frozen_custody(primary, sessions, kind):
    first, _ = two_cards(primary)
    freeze(primary, first, kind)
    before = custody_snapshot(primary, first)
    with kb.write_txn(primary):
        assert not owner._relink(primary, first, "audit-first", owner.session_id("audit-first"),
                                 True, lambda sid: False)
    assert custody_snapshot(primary, first) == before
    assert sessions == []


@pytest.mark.parametrize("kind", ["prepared", "pending"])
def test_backfill_retries_after_exact_custody_resolution(primary, sessions, kind):
    from hermes_cli import kanban_home_transfer as transfer
    from tests.hermes_cli.test_home_generation_feed import feed
    from tests.hermes_cli.test_home_repair_allowance_custody import NONCE

    first, _later = two_cards(primary)
    freeze(primary, first, kind)
    before = custody_snapshot(primary, first)
    assert owner.backfill(primary, apply=True)["linked"] == 1
    assert sessions == ["audit-later"]
    assert custody_snapshot(primary, first) == before
    if kind == "prepared":
        with dbc.write_txn(primary):
            transfer.abort_source(primary, dict(task=first, source="worker-a3", destination="hub",
                nonce="repair-test-nonce-0001", expected_version=7, phase="aborted", rows=[]))
    else:
        feed(primary, first, version=9, nonce=NONCE)
    assert not transfer.frozen(primary, first)
    assert owner.backfill(primary, apply=True)["linked"] == 1
    assert sessions == ["audit-later", "audit-first"]
    assert first not in owner._UNLINKABLE
