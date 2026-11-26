"""HOME real operator entrypoint. Board adapter is not a real DO composition."""
from __future__ import annotations

import copy
import json
from types import SimpleNamespace

import pytest

from hermes_cli import kanban_db as kb, kanban_operator_call as operator
from hermes_cli import kanban_release_certificate as certificate, kanban_release_intent as intent
from tests.hermes_cli import test_kanban_operator_call as helpers

fleet = helpers.fleet


@pytest.fixture
def home(fleet, monkeypatch):
    from hermes_cli import kanban_release_home as subject
    f = fleet
    body = dict(version=1, intent_uid="intent-home", board="default", task_id=f.target, home="hub",
                actor=dict(gateway="worker-5", profile="default", session="native-chat"), episode="scope-now",
                capability_episodes=["old-capability"], runs=2, note="Resume the original goal")
    p = dict(version=1, carrier="owner_release", action="owner_release", request_uid="delivery-home", board="default",
             target_task_id=f.target, home="hub", intent=body, digest=intent.digest(body), seen_comments=[], nonce="a" * 64)
    event = dict(uid=p["request_uid"], task_id=f.target, run_uid=None, kind=operator.REQUEST_KIND,
                 payload=certificate.encode(p), created_at=100, hlc="1795700000000-0000-worker-5", origin_gateway="worker-5",
                 deleted_at=None)
    with kb.write_txn(f.rep):
        f.rep.execute("INSERT INTO task_events (uid,task_id,kind,payload,created_at) VALUES (?,?,?,?,?)",
                      tuple(event[k] for k in ("uid", "task_id", "kind", "payload", "created_at")))
    class Hub:
        ident = SimpleNamespace(gateway="hub")
        def __init__(self):
            self.state, self.calls, self.down, self.winner = "authorized", [], False, None
        def outside(self):
            assert not f.h.in_transaction
            if self.down:
                raise OSError("network unavailable")
        def row_evidence(self, *a, **kw):
            self.outside()
            return dict(board="default", table="task_events", uid=event["uid"], state="live", row=copy.deepcopy(event))
        def owner_release_evidence(self, *a):
            self.outside()
            return dict(version=1, intent=body, digest=intent.digest(body), state=self.state)
        def owner_release_consumption(self, *a):
            self.outside()
            return self.winner
        def ops(self, board, ops):
            self.outside()
            self.calls.append(copy.deepcopy(ops[0]))
            raise OSError("lost reply")
        def close(self):
            pass
    hub = Hub()
    monkeypatch.setattr(subject, "_hub", lambda: hub)
    return SimpleNamespace(f=f, body=body, payload=p, event=event, hub=hub, subject=subject)


def queued(h):
    return h.f.h.execute("SELECT * FROM owner_release_home_queue WHERE delivery_uid='delivery-home'").fetchone()


def test_real_operator_entrypoint_freezes_before_network_and_reopens(home):
    h = home
    before = tuple(h.f.h.execute("SELECT status,current_run_id FROM tasks WHERE id=?", (h.f.target,)).fetchone())
    operator.apply_incoming(h.f.h)
    row = queued(h)
    assert row["stage"] == "consume_pending"
    frozen = row["consume_wire"]
    assert h.hub.calls == [json.loads(frozen)]
    assert not h.f.h.execute("SELECT 1 FROM operator_calls WHERE request_uid='delivery-home'").fetchone()
    from hermes_cli import kanban_db_connect as connections
    path = h.f.h.execute("PRAGMA database_list").fetchone()[2]
    h.f.h.close()
    from pathlib import Path
    h.f.h = connections.connect(Path(path))
    h.f.gw[id(h.f.h)] = "hub"
    operator.apply_incoming(h.f.h)
    assert h.hub.calls == [json.loads(frozen), json.loads(frozen)]
    assert queued(h)["consume_wire"] == frozen
    assert tuple(h.f.h.execute("SELECT status,current_run_id FROM tasks WHERE id=?", (h.f.target,)).fetchone()) == before


def test_old_generic_refusal_and_watermark_do_not_suppress_home_queue(home):
    h = home
    with kb.write_txn(h.f.h):
        operator.ensure_schema(h.f.h)
        operator.log(h.f.h, request_uid="delivery-home", side="execute", outcome="refused")
        eid = h.f.rep.execute("SELECT MAX(id) FROM task_events").fetchone()[0]
        uid = h.f.rep.execute("SELECT uid FROM task_events WHERE id=?", (eid,)).fetchone()[0]
        h.f.h.execute("INSERT INTO operator_call_scan VALUES (?,?,?)", ("default", eid, uid))
    operator.apply_incoming(h.f.h)
    assert queued(h)["stage"] == "consume_pending" and len(h.hub.calls) == 1


@pytest.mark.parametrize("key,value", [("origin_gateway", "other-gateway"), ("deleted_at", 1), ("run_uid", "a-run"),
                                       ("kind", "commented"), ("hlc", "invalid"), ("payload", "{}")])
def test_canonical_wrong_or_forged_carrier_never_consumes(home, key, value):
    h = home
    h.event[key] = value
    operator.apply_incoming(h.f.h)
    assert not h.hub.calls
    assert queued(h)["stage"] == "observed"


def test_unknown_keeps_identity_and_other_calls_progress(home):
    h = home
    h.hub.down = True
    operator.apply_incoming(h.f.h)
    assert queued(h)["stage"] == "observed" and not h.hub.calls
    h.hub.down = False
    operator.apply_incoming(h.f.h)
    assert queued(h)["stage"] == "consume_pending" and len(h.hub.calls) == 1


@pytest.mark.parametrize("state", ["expired", "refused", "consumed"])
def test_non_authorized_state_does_not_post(home, state):
    h = home
    h.hub.state = state
    operator.apply_incoming(h.f.h)
    assert not h.hub.calls


def test_corrupt_frozen_wire_never_regenerates(home):
    h = home
    operator.apply_incoming(h.f.h)
    with pytest.raises(Exception, match="immutable"):
        with kb.write_txn(h.f.h):
            h.f.h.execute("UPDATE owner_release_home_queue SET consume_wire='{}'")
    with pytest.raises(Exception, match="immutable"):
        with kb.write_txn(h.f.h):
            h.f.h.execute("DELETE FROM owner_release_home_queue")


def test_resume_checks_fresh_home_identity(home):
    h = home
    operator.apply_incoming(h.f.h)
    calls = len(h.hub.calls)
    with kb.write_txn(h.f.h):
        h.f.h.execute("UPDATE tasks SET created_on='another-home' WHERE id=?", (h.f.target,))
    operator.apply_incoming(h.f.h)
    assert len(h.hub.calls) == calls


def test_mixed_discriminator_never_uses_worker_auth(home):
    h = home
    p = {**h.payload, "carrier": "other"}
    with kb.write_txn(h.f.rep):
        h.f.rep.execute("UPDATE task_events SET payload=? WHERE uid=?", (certificate.encode(p), h.event["uid"]))
    operator.apply_incoming(h.f.h)
    assert not h.hub.calls
    assert not h.f.h.execute("SELECT 1 FROM operator_calls WHERE request_uid='delivery-home'").fetchone()
