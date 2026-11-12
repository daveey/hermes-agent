"""Withdrawal receiver lifecycle and malformed wire controls."""
import contextlib
import json

import pytest

from hermes_cli.kanban_withdrawal_notice import envelope
from hermes_state import SessionDB
from tui_gateway import kanban_remote_notices as remote


def task():
    return dict(id="t_one", status="archived", board="default", title="Decision",
                withdrawal_notice=dict(kind="request_withdrawn", event_id=42, created_at=100),
                sessions=[dict(gw="origin", profile="work", session="chat", rel="shepherd")])


@pytest.mark.parametrize("change", [dict(event_id=True), dict(event_id=0), dict(event_id="42"),
                                    dict(created_at=True), dict(created_at=0), dict(created_at=float("nan")),
                                    dict(created_at=float("inf")), dict(created_at="100"),
                                    dict(kind="completed"), dict(payload={"action": "secret"})])
def test_malformed_envelope_is_silent(change):
    card = task()
    card["withdrawal_notice"].update(change)
    assert envelope(card) is None
    assert list(remote._sweep_cards([("owner", "build/default/t_one", 1, json.dumps(card))], "origin")) == []


@pytest.mark.parametrize("field,value", [("profile", 123), ("profile", []), ("profile", ""),
                                        ("session", 123), ("session", ["chat"]),
                                        ("session", ""), ("gw", ["origin"])])
def test_withdrawal_recipient_identity_is_not_coerced(field, value):
    card = task()
    link = dict(gw="origin", profile="work", session="chat")
    link[field] = value
    card["withdrawal_recipients"] = [link]
    assert remote._linked_chats(card, "origin") == []


@pytest.mark.parametrize("status", ["todo", "blocked", "done"])
def test_nonarchive_with_old_withdrawal_is_not_a_receipt(status):
    card = task()
    card["status"] = status
    assert envelope(card) is None


@pytest.mark.parametrize("notice", [None, [], "withdrawn"])
def test_nonmapping_envelope_is_silent(notice):
    card = task()
    card["withdrawal_notice"] = notice
    assert envelope(card) is None


def test_released_shepherd_cannot_reenter_as_origin():
    card = task()
    card["sessions"][0]["released"] = True
    card["origin_bindings"] = [dict(origin=dict(gateway="origin", profile="work", session="chat"))]
    assert remote._linked_chats(card, "origin") == []
    card["sessions"][0]["released"] = False
    assert remote._linked_chats(card, "origin") == [("work", "chat")]


@pytest.mark.parametrize("reason", ["archived", "moved_to_gateway:elsewhere"])
def test_deferred_persist_rechecks_chat_exclusion(tmp_path, reason):
    with SessionDB(tmp_path / "state.db") as db:
        db.create_session("chat", "desktop")
        if reason == "archived":
            db._write_rowcount("UPDATE sessions SET archived=1 WHERE id=?", ("chat",))
        else:
            db.end_session("chat", reason)

        @contextlib.contextmanager
        def opener(profile, sid):
            yield db, sid

        ledger = tmp_path / "ledger.db"
        claim = dict(session="chat", ref="owner/build/default/t_one", state="request_withdrawn@42")
        key, metadata, text = remote._sweep_payload("owner", "build/default/t_one", task(), "origin", "work", "chat")
        assert not remote._persist_claimed(opener, "work", "chat", text, metadata, claim,
                                          ("replica", "work", "chat", key), ledger)
        assert db.get_messages("chat") == []


def test_stale_withdrawal_sweep_key_matches_event_identity():
    row = dict(session="chat", notice_key="remote:owner:build/default/t_one:42",
               state="request_withdrawn@42", ref="owner/build/default/t_one")
    assert remote._stale_notice_id(row, task()) == ("work", "remote:owner:build/default/t_one:42:work:chat")


def test_old_claim_ack_release_cannot_settle_new_event(tmp_path):
    ledger = tmp_path / "ledger.db"
    old = dict(session="chat", ref="owner/build/default/t_one", state="request_withdrawn@41")
    new = dict(old, state="request_withdrawn@42")
    with contextlib.closing(remote._ledger(ledger)) as conn:
        for claim in (old, new):
            assert remote._claim_notice(conn, **claim, now=100, fresh=True,
                                        notice_key=claim["state"], task_id="t_one", transcript_has=None) == "offer"
        assert remote.ack(old, ledger=ledger)
        assert remote.release(old, ledger=ledger)
        row = conn.execute("SELECT state,delivered FROM notices").fetchone()
        assert tuple(row) == (new["state"], 0)
