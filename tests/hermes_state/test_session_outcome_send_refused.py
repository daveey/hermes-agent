"""A refused-send subscription is a notice destination, never the chat's open work. [daveey]

session-mentions (``relay_files.subscribe_refused_chat``) subscribes a human chat to a Kanban worker's OWN card
(``tui`` row, ``delivery_metadata.reason == "send_refused"``) when that worker is refused a cross_session send into
it, so the chat still gets the done notice. Core counted that row like a shepherd: the chat then derived
``waiting`` on a card it never created or asked to watch, its children inherited the row, and the Board synced a
``shepherd`` chat link for it.

Live: hub 2026-11-11, reconcile card t_01a1c0d75a11b3cd's worker was refused a send into
askeve-builder/20261101_014435_714d67 (~06:05Z, run 28388); the Board wrote the shepherd link at 06:06:18Z. The
drop operator archived that chat at 06:10Z and re-derived its outcome: ``waiting`` on t_01a1c0d75a11b3cd, the very
card whose drop requires that origin to be not waiting, so the drop fail-stopped in ``drop_intent``.

Every test runs the REAL board (``kanban_db.init_db``), the real ``add_notify_sub`` and the real ``derive_outcome``.
"""
from __future__ import annotations

from pathlib import Path

import pytest

import hermes_state_outcome as so
from hermes_cli import kanban_chat_links as kcl
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_notify as kbn
from hermes_cli import kanban_session_activity as ksa
from hermes_cli import kanban_watch
from hermes_state import SessionDB

CHAT = "20261101_014435_714d67"  # the live archived origin's id shape (an ordinary human chat)
SUMMARY = "Closed PR 446 as dropped and archived its three stage cards; nothing else is pending."


@pytest.fixture
def home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(ksa, "_hub_db_path", lambda: None)
    monkeypatch.setattr(ksa, "_remote_cards", lambda ids, statuses, stale_ok: [])  # one local board, no replicas
    kb.init_db()
    ksa._reset_cache_for_tests()
    so._reset_hooks_for_tests()
    yield home
    ksa._reset_cache_for_tests()
    so._reset_hooks_for_tests()


@pytest.fixture
def db(home):
    handle = SessionDB(db_path=home / "state.db")
    handle.create_session(CHAT, source="desktop")
    handle.append_message(CHAT, role="user", content="reconcile the dropped PR 446 workflow")
    handle.append_message(CHAT, role="assistant", content=SUMMARY)
    yield handle
    handle.close()


def card(reason: str | None, *, chat: str = CHAT, status: str = "ready") -> str:
    """An open card created by a worker session (not the chat), with one ``tui`` sub for ``chat``."""
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="reconcile PR #446 closed-unmerged", assignee="default")
        if reason is not None:
            kbn.add_notify_sub(conn, task_id=tid, platform="tui", chat_id=chat, notifier_profile="default",
                               delivery_metadata={"reason": reason})
        if status != "ready":
            conn.execute("UPDATE tasks SET status = ? WHERE id = ?", (status, tid))
        conn.commit()
    ksa._reset_cache_for_tests()
    return tid


def derived(db) -> dict:
    ksa._reset_cache_for_tests()
    return so.derive_outcome(CHAT, db=db)


@pytest.mark.parametrize("archived", [True, False])
@pytest.mark.parametrize("status", ["ready", "running", "blocked", "review"])
def test_send_refused_sub_is_not_the_chats_wait(db, archived, status):
    tid = card("send_refused", status=status)
    if archived:
        db.set_session_archived(CHAT, True, notify=False)
    out = derived(db)
    assert out["kind"] == "done", out
    assert tid not in {w.get("id") for w in out.get("on") or ()}
    assert ksa.session_tasks([CHAT]) == []
    assert ksa.active_task_counts().get(CHAT, 0) == 0


@pytest.mark.parametrize("reason", ["shepherd", "create"])
def test_owning_and_shepherd_subs_still_wait(db, reason):
    """Control: the change releases only the refused-send provenance, not a real shepherd/creator obligation."""
    tid = card(reason)
    db.set_session_archived(CHAT, True, notify=False)
    out = derived(db)
    assert out["kind"] == "waiting"
    assert [w["id"] for w in out["on"]] == [tid]


def test_unstamped_legacy_sub_still_waits(db):
    """Legacy rows without provenance keep counting (cannot tell a shepherd from a touch)."""
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="legacy", assignee="default")
        kbn.add_notify_sub(conn, task_id=tid, platform="tui", chat_id=CHAT, notifier_profile="default")
        conn.commit()
    assert [w["id"] for w in derived(db)["on"]] == [tid]


def test_send_refused_beside_a_shepherd_of_another_card_waits_only_on_that_card(db):
    refused = card("send_refused")
    watched = card("shepherd")
    out = derived(db)
    assert out["kind"] == "waiting"
    assert [w["id"] for w in out["on"]] == [watched]
    assert refused not in [w["id"] for w in out["on"]]


def test_refused_chat_still_gets_the_done_notice(db):
    """The row is kept and still delivers: only its ownership of the card's progress changes."""
    tid = card("send_refused")
    with kbc.connect_closing() as conn:
        kb.complete_task(conn, tid, summary="dropped")
        rows = [(s["platform"], s["chat_id"], (s["delivery_metadata"] or {}).get("reason"))
                for s in kbn.list_notify_subs(conn, tid)]
        unseen = ksa._unseen_on(conn)
    assert rows == [("tui", CHAT, "send_refused")]
    assert unseen == {CHAT: 1}


@pytest.mark.parametrize("first", ["shepherd", "create", None])
def test_a_refused_send_never_releases_an_existing_counted_sub(db, first):
    """A worker refused into a chat that already shepherds/created/legacy-watches its card must not turn that
    counted row into a notice-only one (add_notify_sub's same-key merge)."""
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="watched", assignee="default")
        kbn.add_notify_sub(conn, task_id=tid, platform="tui", chat_id=CHAT, notifier_profile="default",
                           delivery_metadata={"reason": first} if first else None)
        kbn.add_notify_sub(conn, task_id=tid, platform="tui", chat_id=CHAT, notifier_profile="default",
                           delivery_metadata={"reason": "send_refused"})
        (sub,) = kbn.list_notify_subs(conn, tid)
        conn.commit()
    assert (sub["delivery_metadata"] or {}).get("reason") == first
    assert [w["id"] for w in derived(db)["on"]] == [tid]


def test_a_refused_send_replaces_a_touch_and_a_shepherd_upgrades_a_refused_send(db):
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="watched", assignee="default")
        for reason in ("touch", "send_refused"):
            kbn.add_notify_sub(conn, task_id=tid, platform="tui", chat_id=CHAT, notifier_profile="default",
                               delivery_metadata={"reason": reason})
        assert kbn.list_notify_subs(conn, tid)[0]["delivery_metadata"]["reason"] == "send_refused"
        conn.commit()
    assert derived(db)["kind"] == "done"
    with kbc.connect_closing() as conn:
        kbn.add_notify_sub(conn, task_id=tid, platform="tui", chat_id=CHAT, notifier_profile="default",
                           delivery_metadata={"reason": "shepherd"})
        conn.commit()
    assert [w["id"] for w in derived(db)["on"]] == [tid]


def test_send_refused_sub_is_not_inherited(home):
    parent = card("send_refused")
    with kbc.connect_closing() as conn:
        child = kb.create_task(conn, title="follow-up", assignee="default", parents=[parent])
        assert [(s["platform"], s["chat_id"]) for s in kbn.list_notify_subs(conn, child)] == []


def test_send_refused_chat_is_not_held_by_the_card(home, db):
    tid = card("send_refused")
    shepherded = card("shepherd")
    with kbc.connect_closing() as conn:
        assert kanban_watch.held_chats(conn, tid) == []
        assert kanban_watch.held_chats(conn, shepherded) == [CHAT]


def test_send_refused_sub_writes_no_shepherd_chat_link(home, db):
    """The synced Board link (what other gateways' session_tasks read) follows the same rule: the live
    06:06:18Z ``shepherd`` link to the archived origin must not be written for a refused send."""
    refused = card("send_refused")
    shepherded = card("shepherd")
    with kbc.connect_closing() as conn:
        links = kcl._local_links(conn, "hub")
    assert {(v["task_id"], v["chat_session"], v["kind"]) for v in links.values()} == {(shepherded, CHAT, "shepherd")}
    assert refused not in {v["task_id"] for v in links.values()}
