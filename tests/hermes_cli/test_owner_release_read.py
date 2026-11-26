"""Complete native read inventory and durable emitted-output acknowledgement.

This does not prove a human saw output or grant HOME application authority.
"""
import copy
import json

import pytest

from hermes_cli import kanban_release_intent as intent
from tests.hermes_cli import test_owner_release_intent as fixtures

turn = fixtures.turn
journal = fixtures.journal
action = fixtures.action
gate = fixtures.gate


def prepared(journal):
    body = intent.prepare(journal, action(), gate=gate())
    intent.advance(journal, body, expected="intent_journaled_unsent", stage="send_pending")
    intent.advance(journal, body, expected="send_pending", stage="authorized_pending_home")
    return body


class Hub:
    def __init__(self, body):
        self.body = body
        self.value = {"version": 1, "intent": body, "digest": intent.digest(body), "comments": []}
    def owner_release_comments(self, board, uid):
        assert board == self.body["board"] and uid == self.body["intent_uid"]
        return copy.deepcopy(self.value)


def test_unconfirmed_ack_redisplays_same_nonce_then_prior_emission_allows_ack(turn, journal):
    from hermes_cli import kanban_release_read as read
    body = prepared(journal)
    hub = Hub(body)
    first = read.prepare(journal, hub, body, seen=None)
    nonce = first["release_read"]["nonce"]
    second = read.prepare(journal, hub, body, seen=nonce)
    assert second["release_stage"] == "read_first_pending"
    assert second["release_read"]["nonce"] == nonce
    read.confirm(journal, hub, second.confirmation)
    acknowledged = read.prepare(journal, hub, body, seen=nonce)
    assert acknowledged["release_stage"] == "acknowledged_pending_home"
    assert "release_read" not in acknowledged
    assert intent.stage(journal, body) == "read_first_pending"  # never consumed/applied
    assert read.prepare(journal, hub, body, seen=nonce) == acknowledged


def test_reopen_preserves_unconfirmed_and_confirmed_inventory(turn, journal):
    from hermes_cli import kanban_db_connect as kbc, kanban_release_read as read
    body = prepared(journal)
    hub = Hub(body)
    first = read.prepare(journal, hub, body, seen=None)
    with kbc.connect_closing() as reopened:
        again = read.prepare(reopened, hub, body, seen=first["release_read"]["nonce"])
        assert again["release_read"] == first["release_read"]
        read.confirm(reopened, hub, again.confirmation)
    assert read.prepare(journal, hub, body, seen=first["release_read"]["nonce"])["release_stage"] == "acknowledged_pending_home"


def test_changed_rows_supersede_nonce_and_stale_confirmation_never_marks_new_rows(turn, journal):
    from hermes_cli import kanban_release_read as read
    body = prepared(journal)
    hub = Hub(body)
    first = read.prepare(journal, hub, body, seen=None)
    row = {"uid": "comment-a", "task_id": body["task_id"], "author": "owner", "body": "new instruction",
           "created_at": 1, "seq": 1, "version": 1, "updated_at": 1, "hlc": "1", "origin_gateway": "home", "deleted_at": None}
    hub.value["comments"] = [row]
    with pytest.raises(ValueError, match="comments changed"):
        read.confirm(journal, hub, first.confirmation)
    second = read.prepare(journal, hub, body, seen=first["release_read"]["nonce"])
    assert second["release_read"]["nonce"] != first["release_read"]["nonce"]
    assert second["release_read"]["comments"] == [row]
    with pytest.raises(ValueError, match="superseded"):
        read.confirm(journal, hub, first.confirmation)


@pytest.mark.parametrize("change", [{"version": True}, {"extra": 1}, {"digest": "wrong"}, {"comments": {}}, {"intent": {}}])
def test_strict_private_comments_envelope_before_journal_write(turn, journal, change):
    from hermes_cli import kanban_release_read as read
    body = prepared(journal)
    hub = Hub(body)
    hub.value.update(change)
    with pytest.raises(ValueError):
        read.prepare(journal, hub, body, seen=None)
    assert not journal.execute("SELECT 1 FROM sqlite_master WHERE name='native_owner_release_reads'").fetchone()


def test_revocation_blocks_confirmation_without_changing_row(turn, journal):
    from hermes_cli import kanban_release_read as read
    body = prepared(journal)
    hub = Hub(body)
    first = read.prepare(journal, hub, body, seen=None)
    before = tuple(journal.execute("SELECT * FROM native_owner_release_reads").fetchone())
    turn.release()
    with pytest.raises(ValueError, match="native"):
        read.confirm(journal, hub, first.confirmation)
    assert tuple(journal.execute("SELECT * FROM native_owner_release_reads").fetchone()) == before


def test_same_invocation_cannot_ack_its_own_confirmation(turn, journal, monkeypatch):
    from hermes_cli import kanban_release_read as read
    body = prepared(journal)
    hub = Hub(body)
    monkeypatch.setattr(read, "invocation_uid", lambda: "same-invocation")
    first = read.prepare(journal, hub, body, seen=None)
    read.confirm(journal, hub, first.confirmation)
    again = read.prepare(journal, hub, body, seen=first["release_read"]["nonce"])
    assert again["release_stage"] == "read_first_pending"


def comment(body="", uid="c", task="t_target"):
    return {"uid": uid, "task_id": task, "author": "a", "body": body, "created_at": 0,
            "seq": 1, "version": 1, "updated_at": 0, "hlc": "", "origin_gateway": "h", "deleted_at": None}


def test_independent_row_limit_and_exact_byte_bound(turn, journal):
    from hermes_cli import kanban_release_read as read
    body = prepared(journal)
    hub = Hub(body)
    hub.value["comments"] = [comment(uid=f"c{i:03}", task=body["task_id"]) for i in range(513)]
    assert len(json.dumps(hub.value["comments"], separators=(",", ":")).encode()) < 131072
    with pytest.raises(ValueError, match="comments evidence"):
        read.inventory(hub, body)
    hub.value["comments"].pop()
    read.inventory(hub, body)
    hub.value["comments"] = [comment(task=body["task_id"])]
    overhead = len(json.dumps(hub.value["comments"], separators=(",", ":")).encode())
    hub.value["comments"][0]["body"] = "x" * (131072 - overhead)
    read.inventory(hub, body)
    hub.value["comments"][0]["body"] += "x"
    with pytest.raises(ValueError, match="byte bound"):
        read.inventory(hub, body)


def test_lossless_unicode_and_surrogates_are_bounded_like_json_stringify(turn, journal):
    from hermes_cli import kanban_release_read as read
    body = prepared(journal)
    hub = Hub(body)
    hub.value["comments"] = [comment("é😀\ud800\x00\x1b", task=body["task_id"])]
    assert json.loads(read.inventory(hub, body)) == hub.value["comments"]
    result = read.prepare(journal, hub, body, seen=None)
    assert result["release_read"]["comments"] == hub.value["comments"]


@pytest.mark.parametrize("seen", ["", "A" * 64, "f" * 63, 1, {}, False])
def test_malformed_seen_refuses_without_read_journal(turn, journal, seen):
    from hermes_cli import kanban_release_read as read
    body = prepared(journal)
    with pytest.raises(ValueError, match="nonce"):
        read.prepare(journal, Hub(body), body, seen=seen)
    assert not journal.execute("SELECT 1 FROM sqlite_master WHERE name='native_owner_release_reads'").fetchone()


@pytest.mark.parametrize("change", [{"deleted_at": 1}, {"task_id": "another"}, {"seq": True}, {"version": False}])
def test_malformed_comment_rows_refuse(turn, journal, change):
    from hermes_cli import kanban_release_read as read
    body = prepared(journal)
    hub = Hub(body)
    hub.value["comments"] = [{**comment(task=body["task_id"]), **change}]
    with pytest.raises(ValueError, match="comments evidence"):
        read.inventory(hub, body)
