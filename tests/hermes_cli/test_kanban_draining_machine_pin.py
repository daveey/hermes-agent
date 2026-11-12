"""A card pinned to a machine that is draining for a core update says so at create and in its queue read. [daveey]

t_01a1cca8ed755919, 11-11 (UTC): a MacBook chat ran ``kanban_create(placement="hub")`` at 21:49:38. The hub's
dispatcher had claimed its last card at 21:48:56; at 21:50:01 it logged "kanban dispatch held for a core update
drain" and claimed nothing more until 22:14:20. The create reply said "it runs on hub ... do not re-create it" and
``kanban_show`` said "queued, not stuck", so the chat archived the card at 22:08 as lost. The control card made the
same way at 21:46 was claimed at 21:47:13, before the drain. The body's local marker played no part: the marker test
below passes on core before this fix too, and the hub picks the card up once it is not draining.

Every machine reports ``drain_active`` / ``drain_until_ms`` in its Board registry metrics
(``machine_drain_metrics``). The origin now reads them, so the create note, the ``kanban_show`` queue note and the
chat-wait notice name the drain instead of promising a start.
"""
from __future__ import annotations

import json
import time

import pytest

from hermes_cli import kanban_capability_registry as reg
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli import kanban_db_sync as sync
from hermes_cli import kanban_parent_unclaimed as kpu
from hermes_cli import kanban_queue_position as kqp
from hermes_cli import kanban_worker_node as wn
from tests.hermes_cli.kanban_sync_fake_board import FakeService
from tests.hermes_cli.test_kanban_worker_node import HUB, MACHINES, Gateway, _tool_create

MAC = "macbook"
# An operate instruction: a body that only names "the Desktop backend" no longer pins (PR 1222). [daveey]
BODY = "Restart the Desktop backend, then inspect ~/.hermes/state.db and ~/.hermes/logs/gateway.log on the MacBook."


@pytest.fixture
def mac(tmp_path, monkeypatch):
    monkeypatch.setattr(kbd, "_profile_exists_fn", lambda: None)
    sync.consume_dispatcher_wake()
    gw = Gateway(tmp_path, MAC, FakeService(lease_runs=True, machines=MACHINES), monkeypatch, pickup=False)
    gw.enter()
    monkeypatch.setattr(reg, "self_gateway", lambda: MAC)
    yield gw
    gw.conn.close()
    sync.set_lease_client(None)
    wn.set_registry_fetch(None)


def _registry(**metrics):
    """The hub (picking up every card, as live) with the given metrics sample beside the MacBook and the rest."""
    hub = {**MACHINES[HUB], "pickup": "all", "metrics": metrics}
    others = [entry for name, entry in MACHINES.items() if name != HUB]
    wn.set_registry_fetch(lambda: {"me": MAC, "machines": [hub, *others]})


def _now_ms():
    return int(time.time() * 1000)


def _create():
    return _tool_create({"title": "Async subagent results", "placement": "hub", "assignee": "default",
                         "requires": ["terminal", "file", "git"], "body": BODY})


def test_create_pinned_to_a_draining_hub_names_the_drain(mac):
    until = _now_ms() + 20 * 60_000
    _registry(at=_now_ms(), drain_active=1, drain_until_ms=until)
    out = _create()
    assert "error" not in out and out["runs_on"] == ["machine:hub"], out
    assert "DRAINING for a core update" in out["note"] and "do not re-create it" in out["note"], out["note"]
    place = kqp.placement(mac.conn, out["task_id"])
    assert place == {"lane": "machine", "runs_on": ["machine:hub"], "machines": ["hub"], "draining": ["hub"],
                     "drain_until_ms": until}, place
    note = kqp.describe(place) or ""
    assert "DRAINING" in note and "starts when the drain ends" in note, note
    assert "queued, not stuck" not in note, note


def test_chat_wait_notice_names_the_drain_not_an_unknown_queue_place(mac):
    _registry(at=_now_ms(), drain_active=1)
    tid = kb.create_task(mac.conn, title="pinned to hub", assignee="default", runs_on=["machine:hub"])
    words = kpu._queue_words(kqp.placement(mac.conn, tid) or {})
    assert "DRAINING" in words and "end time unknown" in words and "queue place unknown" not in words, words


@pytest.mark.parametrize("metrics", [{"drain_active": 0}, {"drain_active": 1, "stale": True}],
                         ids=["not_draining", "stale_drain_sample"])
def test_no_drain_or_a_stale_sample_keeps_the_plain_wait(mac, metrics):
    at = _now_ms() - (kqp.DRAIN_SAMPLE_MAX_AGE_S + 60) * 1000 if metrics.pop("stale", False) else _now_ms()
    _registry(at=at, **metrics)
    out = _create()
    assert "DRAINING" not in out["note"], out["note"]
    place = kqp.placement(mac.conn, out["task_id"])
    assert place == {"lane": "machine", "runs_on": ["machine:hub"], "machines": ["hub"]}, place
    assert "queued, not stuck" in (kqp.describe(place) or "")


def test_local_marker_hub_pin_is_offered_and_claimed_when_the_hub_is_not_draining(tmp_path, monkeypatch):
    """The original hypothesis, ruled out: a local-marker body does not keep the hub from leasing the card."""
    service = FakeService(lease_runs=True)
    monkeypatch.setitem(MACHINES, HUB, {**MACHINES[HUB], "pickup": "all"})
    monkeypatch.setattr(kbd, "_profile_exists_fn", lambda: None)
    origin = Gateway(tmp_path, MAC, service, monkeypatch, pickup=False)
    hub = Gateway(tmp_path, HUB, service, monkeypatch, pickup="true")
    try:
        origin.enter()
        monkeypatch.setattr(reg, "self_gateway", lambda: MAC)
        out = _create()
        assert "error" not in out and out["runs_on"] == ["machine:hub"], out
        tid = out["task_id"]
        payload = origin.conn.execute(
            "SELECT payload FROM task_events WHERE task_id=? AND kind='created'", (tid,)).fetchone()[0]
        assert json.loads(payload)["portable_reason"] == "local marker"
        origin.round()
        assert tid in service.board("default")["rows"]["tasks"]
        hub.round()
        hub.tick()
        task = kb.get_task(hub.conn, tid)
        assert task is not None and task.status == "running"
        assert hub.conn.execute("SELECT lease_holder FROM tasks WHERE id=?", (tid,)).fetchone()[0] == HUB
    finally:
        origin.conn.close()
        hub.conn.close()
        sync.set_lease_client(None)
        wn.set_registry_fetch(None)
