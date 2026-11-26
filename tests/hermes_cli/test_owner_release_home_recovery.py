"""HOME queue recovery. Mocked authenticated Board adapter, not real DO proof."""
import json

import pytest

from hermes_cli import kanban_db as kb, kanban_operator_call as operator
from hermes_cli import kanban_release_certificate as certificate, kanban_release_intent as intent
from tests.hermes_cli import test_owner_release_home as helpers

fleet = helpers.fleet
home = helpers.home
queued = helpers.queued


def test_local_home_catchup_keeps_pending_carrier(home):
    h = home
    with kb.write_txn(h.f.h):
        h.f.h.execute("UPDATE tasks SET created_on='not-yet-home' WHERE id=?", (h.f.target,))
    operator.apply_incoming(h.f.h)
    assert queued(h)["stage"] == "observed" and not h.hub.calls
    with kb.write_txn(h.f.h):
        h.f.h.execute("UPDATE tasks SET created_on='hub' WHERE id=?", (h.f.target,))
    operator.apply_incoming(h.f.h)
    assert queued(h)["stage"] == "consume_pending"


def test_invalid_duplicate_cannot_suppress_authenticated_delivery(home):
    h = home
    duplicate = {**h.payload, "request_uid": "invalid-duplicate", "digest": "bad"}
    with kb.write_txn(h.f.rep):
        h.f.rep.execute("INSERT INTO task_events (uid,task_id,kind,payload,created_at) VALUES (?,?,?,?,?)",
                        ("invalid-duplicate", h.f.target, operator.REQUEST_KIND, certificate.encode(duplicate), 100))
    operator.apply_incoming(h.f.h)
    assert queued(h)["stage"] == "consume_pending" and len(h.hub.calls) == 1


def winning_receipt(h):
    op = json.loads(queued(h)["consume_wire"])
    b = h.body
    uid = op["client_op_id"]
    payload = dict(version=1, episode=b["episode"], covered_restriction_uids=[b["episode"]], request_uid=uid,
                   owner_release_intent_uid=b["intent_uid"], owner_release_digest=intent.digest(b), actor_kind="agent",
                   actor_gateway=b["actor"]["gateway"], actor_profile=b["actor"]["profile"], actor_session=b["actor"]["session"],
                   authority_kind="chat", authority_ref=None, reason=b["note"])
    event = dict(uid=uid, ord=3, task_id=b["task_id"], run_uid=None, kind="scope_renewed", payload=certificate.encode(payload),
                 created_at=100, seq=10, version=1, updated_at=100000, hlc="100000:0:hub", origin_gateway="hub", deleted_at=None)
    return dict(version=1, intent=b, digest=intent.digest(b), consume=op, renewal=event)


def synced_cache(h):
    with kb.write_txn(h.f.h):
        h.f.h.execute("CREATE TABLE IF NOT EXISTS kanban_sync_state (board_id TEXT,gateway_id TEXT)")
        h.f.h.execute("INSERT INTO kanban_sync_state(board_id,gateway_id) VALUES ('default','hub')")


def test_lost_reply_private_winner_reconciles_without_resend_or_clearance(home):
    from hermes_cli import kanban_release_replica as replica
    h = home
    operator.apply_incoming(h.f.h)
    h.hub.winner = winning_receipt(h)
    h.hub.state = "consumed"
    synced_cache(h)
    h.event["deleted_at"] = 1
    operator.apply_incoming(h.f.h)
    assert len(h.hub.calls) == 1
    assert queued(h)["stage"] == "consumed_pending_apply"
    uid = h.hub.winner["consume"]["client_op_id"]
    assert replica.verified_consumption(h.f.h, uid) == h.hub.winner
    assert not h.f.h.execute("SELECT 1 FROM task_events WHERE kind='stop_released'").fetchone()
    assert not h.f.h.execute("SELECT 1 FROM task_events WHERE kind='promoted'").fetchone()
    operator.apply_incoming(h.f.h)
    assert len(h.hub.calls) == 1


@pytest.mark.parametrize("change", ["uid", "comments", "note"])
def test_different_private_winner_stays_held(home, change):
    h = home
    operator.apply_incoming(h.f.h)
    receipt = winning_receipt(h)
    if change == "uid":
        receipt["consume"]["client_op_id"] = "different-winning-consume"
        receipt["renewal"]["uid"] = "different-winning-consume"
        payload = json.loads(receipt["renewal"]["payload"])
        payload["request_uid"] = "different-winning-consume"
        receipt["renewal"]["payload"] = certificate.encode(payload)
    elif change == "comments":
        receipt["consume"]["seen_comments"] = [dict(uid="new-comment", task_id=h.f.target, author="writer", body="new",
            created_at=100, seq=1, version=1, updated_at=100, hlc="1:0:hub", origin_gateway="hub", deleted_at=None)]
    else:
        receipt["intent"] = {**h.body, "note": "another goal"}
        receipt["digest"] = intent.digest(receipt["intent"])
    h.hub.winner, h.hub.state = receipt, "consumed"
    synced_cache(h)
    operator.apply_incoming(h.f.h)
    assert len(h.hub.calls) == 1 and queued(h)["stage"] == "consume_pending"
