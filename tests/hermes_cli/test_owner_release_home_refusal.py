"""HOME tick: a typed release Refusal during final freeze holds only that row.

Real HOME queue (``operator.apply_incoming`` -> ``home.tick``) and the real
``kanban_release_final_home.process`` run. The single seam is ``kanban_release_final.freeze``:
the shared operator fixture's ``tasks`` schema cannot satisfy the real assembly/``_budget_plan``
(no ``lease_holder`` column), so freeze is replaced to raise the real ``Refusal`` (or an
unexpected ``RuntimeError``). Not real DO/assembly proof; mocked Board adapter.
"""
from __future__ import annotations

import copy

import pytest

from hermes_cli import kanban_db as kb, kanban_operator_call as operator
from hermes_cli import kanban_release as release, kanban_release_final as final
from hermes_cli import kanban_release_certificate as certificate, kanban_release_intent as intent
from tests.hermes_cli import test_owner_release_home as helpers
from tests.hermes_cli.test_owner_release_home_recovery import synced_cache, winning_receipt

fleet = helpers.fleet
home = helpers.home
queued = helpers.queued

FINAL_TABLES = ("owner_release_applied_receipts", "owner_release_applications")


def _final_effects(h):
    c = h.f.h
    names = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    out = {t: c.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in FINAL_TABLES if t in names}
    out["events"] = [tuple(r) for r in c.execute("SELECT * FROM task_events ORDER BY id")]
    out["task"] = tuple(c.execute("SELECT * FROM tasks WHERE id=?", (h.f.target,)).fetchone())
    out["budget"] = c.execute("SELECT COUNT(*) FROM task_events WHERE kind LIKE '%budget%'").fetchone()[0]
    return out


def _sibling(h):
    """A second, independent observed delivery on the same real queue."""
    b = {**h.body, "intent_uid": "intent-sibling", "note": "Sibling goal"}
    p = {**h.payload, "request_uid": "delivery-sibling", "intent": b, "digest": intent.digest(b)}
    ev = {**h.event, "uid": "delivery-sibling", "payload": certificate.encode(p)}
    with kb.write_txn(h.f.rep):
        h.f.rep.execute("INSERT INTO task_events (uid,task_id,kind,payload,created_at) VALUES (?,?,?,?,?)",
                        tuple(ev[k] for k in ("uid", "task_id", "kind", "payload", "created_at")))
    hub = h.hub
    events = {h.event["uid"]: h.event, "delivery-sibling": ev}
    bodies = {h.body["intent_uid"]: h.body, "intent-sibling": b}
    hub.row_evidence = lambda board, table, uid, **kw: (hub.outside() or dict(
        board="default", table="task_events", uid=uid, state="live", row=copy.deepcopy(events[uid])))
    hub.owner_release_evidence = lambda board, uid, *a, **kw: (hub.outside() or dict(
        version=1, intent=bodies[uid], digest=intent.digest(bodies[uid]), state=hub.state))


def _pending_apply(h):
    operator.apply_incoming(h.f.h)
    h.hub.winner, h.hub.state = winning_receipt(h), "consumed"
    synced_cache(h)
    operator.apply_incoming(h.f.h)
    assert queued(h)["stage"] == "consumed_pending_apply"


def test_typed_refusal_in_final_freeze_holds_row_and_sibling_progresses(home, monkeypatch):
    h = home
    _pending_apply(h)
    _sibling(h)
    h.hub.state = "authorized"
    before = _final_effects(h)
    seen = []

    def refuse(conn, proof, **kw):
        seen.append(kw["delivery_uid"])
        raise release.Refusal("budget_limit_in_force", "typed refusal during final freeze")

    monkeypatch.setattr(final, "freeze", refuse)
    operator.apply_incoming(h.f.h)  # must not raise
    assert seen == ["delivery-home"], "real final_home.process never reached final.freeze"
    assert queued(h)["stage"] == "consumed_pending_apply"
    assert _final_effects(h) == before
    sibling = h.f.h.execute("SELECT stage FROM owner_release_home_queue WHERE delivery_uid='delivery-sibling'").fetchone()
    assert sibling["stage"] == "consume_pending"
    assert len(h.hub.calls) == 2  # original consume + sibling consume; no final POST


def test_unexpected_runtime_error_in_final_freeze_escapes_and_closes_hub(home, monkeypatch):
    h = home
    _pending_apply(h)
    closed = []
    h.hub.close = lambda: closed.append(True)
    before = _final_effects(h)

    def boom(conn, proof, **kw):
        raise RuntimeError("unexpected final failure")

    monkeypatch.setattr(final, "freeze", boom)
    with pytest.raises(RuntimeError, match="unexpected final failure"):
        h.subject.tick(h.f.h, h.f.rep, "hub", "default")
    assert closed == [True]
    assert queued(h)["stage"] == "consumed_pending_apply"
    assert _final_effects(h) == before
