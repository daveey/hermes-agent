"""Remote-only card changes refresh authoritative stored chat outcomes."""
import pytest

import hermes_state_outcome as so
from hermes_cli import kanban_session_activity as ksa
from tests.tui_gateway import test_session_outcome_notices as helpers
from tui_gateway import notice_fanout as nf

db = helpers.db
CHAT = helpers.CHAT


@pytest.fixture
def remote(monkeypatch):
    rows = []
    def read(ids, statuses, stale_ok):
        # Display lists stay stale-friendly; empty outcomes also confirm strict counts.
        assert isinstance(stale_ok, bool)
        assert statuses == ksa.LISTED_STATUSES
        return list(rows)
    monkeypatch.setattr(ksa, "_remote_cards", read)
    return rows


def card(chat=CHAT, status="running"):
    return {"key": "worker/default/t_remote", "id": "t_remote", "board": "default",
            "gateway": "worker", "session": chat, "status": status, "title": "remote work"}


def run(db):
    return nf.card_change_pass(session_db=helpers._opener(db), slugs=[])


def test_remote_appears_changes_and_finishes(db, remote):
    assert so.refresh_outcome(CHAT, "manual", db=db)["kind"] == "done"
    assert run(db) == []
    remote.append(card())
    assert len(run(db)) == 1
    assert so.get_outcome(CHAT, db=db)["kind"] == "waiting"
    assert run(db) == []
    remote[0]["status"] = "scheduled"
    assert len(run(db)) == 1
    assert so.get_outcome(CHAT, db=db)["on"][0]["state"] == "scheduled"
    remote.clear()
    assert len(run(db)) == 1
    assert so.get_outcome(CHAT, db=db)["kind"] == "done"


def test_first_pass_seeds_only(db, remote, monkeypatch, caplog):
    remote.append(card())
    monkeypatch.setattr(so, "refresh_outcomes_quietly", lambda *a, **k: pytest.fail("unexpected refresh"))
    assert run(db) == []
    assert run(db) == []
    assert "remote card-change outcome pass failed" not in caplog.text


def test_failed_read_preserves_snapshot(db, remote, monkeypatch):
    remote.append(card())
    run(db)
    with monkeypatch.context() as patch:
        def fail(*a, **k):
            raise OSError("replica unavailable")
        patch.setattr(ksa, "_remote_cards", fail)
        patch.setattr(so, "refresh_outcomes_quietly", lambda *a, **k: pytest.fail("failed read refreshed"))
        assert run(db) == []
    remote.clear()
    assert len(run(db)) == 1


@pytest.mark.parametrize("how", ["archived", "moved"])
def test_skipped_chat_not_refreshed(db, remote, monkeypatch, how):
    run(db)
    if how == "archived":
        db.set_session_archived(CHAT, True)
    else:
        db.end_session(CHAT, "moved_to_gateway:elsewhere")
    monkeypatch.setattr(so, "refresh_outcomes_quietly", lambda *a, **k: pytest.fail("unexpected refresh"))
    remote.append(card())
    assert [r["skipped"] for r in run(db)] == [how]


def test_missing_chat_and_unlisted_cards(db, remote):
    run(db)
    remote.extend([card("absent"), card(status="done"), card(status="unknown"),
                   {**card(status="done"), "unfinished": True}])
    assert run(db) == []


def test_unknown_unfinished_and_compression_tip(db, remote, monkeypatch):
    run(db)
    remote.append({**card(status="unknown"), "unfinished": True})
    seen = []
    monkeypatch.setattr(so, "refresh_outcomes_quietly", lambda ids, trigger, **kw: (seen.append((ids, trigger, kw)) or 1))
    monkeypatch.setattr(db, "get_compression_tip", lambda sid: "compressed-tip")
    assert len(run(db)) == 1
    assert seen == [(["compressed-tip"], "card_change", {"db": db})]


def test_done_pending_release_stays_observed_until_released(db, remote, monkeypatch):
    """[daveey] A done card whose link the owner has not released yet is still the chat's obligation: its move from
    running is a change, and the release (the card leaving the projection) is another, so the outcome refreshes then."""
    remote.append(card())
    run(db)
    seen = []
    monkeypatch.setattr(so, "refresh_outcomes_quietly", lambda ids, trigger, **kw: (seen.append(trigger) or 1))
    remote[0] = {**card(status="done_pending_release"), "unfinished": True}
    assert len(run(db)) == 1
    assert run(db) == []  # unchanged while pending
    remote.clear()
    assert len(run(db)) == 1 and seen == ["card_change", "card_change"]


@pytest.mark.parametrize("failure", ["read", "write", "missing"])
def test_failed_chat_retries_unchanged_projection(db, remote, monkeypatch, failure):
    so.refresh_outcome(CHAT, "manual", db=db)
    run(db)
    remote.append(card())
    with monkeypatch.context() as patch:
        if failure == "read":
            def fail(*a):
                raise OSError("temporary session read")
            patch.setattr(nf, "chat_skip_reason", fail)
        elif failure == "write":
            patch.setattr(so, "refresh_outcomes_quietly", lambda *a, **k: 0)
        else:
            patch.setattr(db, "get_session", lambda *a: None)
        assert run(db) == []
    assert so.get_outcome(CHAT, db=db)["kind"] == "done"
    assert len(run(db)) == 1
    assert so.get_outcome(CHAT, db=db)["kind"] == "waiting"
    assert run(db) == []
    remote.clear()
    with monkeypatch.context() as patch:
        patch.setattr(so, "refresh_outcomes_quietly", lambda *a, **k: 0)
        assert run(db) == []
    assert len(run(db)) == 1
    assert so.get_outcome(CHAT, db=db)["kind"] == "done"
    assert run(db) == []


def test_failed_first_chat_does_not_starve_later_chat(db, remote, monkeypatch):
    run(db)
    remote.extend([card("000-failing"), card()])
    original = nf.chat_skip_reason
    def skip(db, chat):
        if chat == "000-failing":
            raise OSError("temporary session read")
        return original(db, chat)
    from contextlib import contextmanager
    @contextmanager
    def opener(chat):
        yield db, chat
    monkeypatch.setattr(nf, "chat_skip_reason", skip)
    rows = nf.card_change_pass(session_db=opener, slugs=[])
    assert [r["chat_id"] for r in rows] == [CHAT]
    assert so.get_outcome(CHAT, db=db)["kind"] == "waiting"


def test_origin_alias_and_duplicate_rows_refresh_once(db, remote):
    run(db)
    row = {**card("absent"), "origin_session": CHAT}
    remote.extend([row, dict(row)])
    assert len(run(db)) == 1
    assert so.get_outcome(CHAT, db=db)["kind"] == "waiting"
