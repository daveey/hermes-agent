"""A label carried only by pinned_only machines is unclaimable from another gateway (silent starvation). [daveey]

t_01a1f867bac2344e (runs_on=["macos"]) sat ready 24 h on the hub: the only macos machines are pinned_only with no
pickup, and no pool worker carries ``macos``. Create/move refuse it, the dispatcher flags a ready one in a single
sweep (chat notice + ONE alarm card), and the stranded reroute grant covers it.
"""
from __future__ import annotations

import json

import pytest

from hermes_cli import kanban_claimants as kc
from hermes_cli import kanban_creator_repin as kcr
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli import kanban_db_sync as sync
from hermes_cli import kanban_move as km
from hermes_cli import kanban_runs_on_capabilities as roc
from hermes_cli import kanban_unclaimable_ready as ur
from hermes_cli import kanban_worker_node as wn
from tests.hermes_cli.kanban_sync_fake_board import FakeService
from tests.hermes_cli.test_kanban_worker_node import HUB, MACHINES, Gateway

OTHER_WORKER_TASK = "t_01a1c0c6a7d4f3af"


@pytest.fixture
def svc(monkeypatch):
    # The alarm queues on a capable pool worker (kanban_pickup_stall.alarm_placement): give worker-1 its labels.
    alarm = ["capability:file", "capability:hub-write", "capability:kanban", "capability:terminal", "pool:worker",
             "profile:default"]
    monkeypatch.setitem(MACHINES, "worker-1", {**MACHINES["worker-1"], "labels": ["linux", *alarm]})
    return FakeService(lease_runs=True, machines=MACHINES)


@pytest.fixture
def hub(tmp_path, monkeypatch, svc):
    monkeypatch.setattr(kbd, "_profile_exists_fn", lambda: None)
    sync.consume_dispatcher_wake()
    g = Gateway(tmp_path, HUB, svc, monkeypatch, pickup=False)
    yield g
    g.conn.close()
    sync.set_lease_client(None)
    wn.set_registry_fetch(None)


def _ready_macos_card(hub, labels=("macos",)):
    hub.enter()
    tid = kb.create_task(hub.conn, title="Build the Mac app", assignee="default")
    with kb.write_txn(hub.conn):
        wn.set_runs_on(hub.conn, tid, list(labels))  # a row written before the check, or rehomed onto the hub
    return tid


def _events(conn, tid, kind):
    return [json.loads(r[0] or "{}") for r in conn.execute(
        "SELECT payload FROM task_events WHERE task_id = ? AND kind = ?", (tid, kind))]


def test_label_only_pinned_only_machines_carry_is_impossible():
    assert MACHINES["macbook"]["pinned_only"] and not MACHINES["macbook"].get("pickup")
    assert roc.impossible(["macos"], MACHINES, HUB)
    assert kc.refusals(["macos"], MACHINES, HUB) == {"macbook": "pinned_only"}
    assert kc.claimants(["macos"], MACHINES, HUB) == []


def test_the_machines_own_cards_and_carried_labels_stay_claimable():
    assert not roc.impossible(["macos"], MACHINES, HUB, home="macbook")  # its own gateway's card
    assert not roc.impossible(["linux"], MACHINES, HUB)
    assert roc.impossible(["hermes-hub"], MACHINES, HUB)  # unchanged: nothing carries it
    pool = {**MACHINES, "worker-2": {"labels": ["macos"], "pinned_only": False, "pickup": "all", "hold": False}}
    assert not roc.impossible(["macos"], pool, HUB)  # a pool worker carrying it makes it claimable


def test_a_pin_needs_pickup_to_count():
    pickup = {**MACHINES, "macbook": {**MACHINES["macbook"], "pickup": "explicit"}}
    assert not roc.impossible(["macos", "machine:macbook"], pickup, HUB)
    assert roc.impossible(["macos", "machine:macbook"], MACHINES, HUB)


def test_create_is_refused_with_the_cross_gateway_remedy(hub):
    hub.enter()
    before = hub.conn.execute("SELECT count(*) FROM tasks").fetchone()[0]
    with pytest.raises(ValueError, match="no enrolled machine can claim") as err:
        kb.create_task(hub.conn, title="mac", assignee="default", requires=[], runs_on=["macos"])
    assert "macbook: pinned_only" in str(err.value) and "cross-gateway create" in str(err.value)
    assert hub.conn.execute("SELECT count(*) FROM tasks").fetchone()[0] == before


def test_move_to_such_labels_is_refused(hub):
    tid = _ready_macos_card(hub, ["linux"])
    res = km.move_queued(hub.conn, tid, runs_on=["macos"], reason="needs xcode")
    assert not res["ok"] and res["reason_code"] == "no_machine" and "cross-gateway create" in res["error"]
    assert json.loads(hub.conn.execute("SELECT runs_on FROM tasks WHERE id = ?", (tid,)).fetchone()[0]) == ["linux"]


def test_sweep_flags_a_ready_card_once_and_keeps_one_alarm(hub):
    tid = _ready_macos_card(hub)
    assert ur.sweep(hub.conn) == [tid]
    notices = _events(hub.conn, tid, "chat_wait_unclaimed")
    assert len(notices) == 1 and "macbook: pinned_only" in notices[0]["unclaimable"]
    from hermes_cli import kanban_chat_wait as cw
    text = cw.notice_text(notices[0])
    assert "NO MACHINE CAN CLAIM" in text and "waiting for macbook" not in text
    alarms = [r[0] for r in hub.conn.execute("SELECT id FROM tasks WHERE idempotency_key LIKE 'pickup-stall:hub:"
                                             "unclaimable-ready:%'")]
    assert len(alarms) == 1 and tid in kb.get_task(hub.conn, alarms[0]).body
    ur.sweep(hub.conn)
    assert len(_events(hub.conn, tid, "chat_wait_unclaimed")) == 1  # once per card and labels
    assert len(list(hub.conn.execute("SELECT id FROM tasks WHERE idempotency_key LIKE 'pickup-stall:hub:"
                                     "unclaimable-ready:%'"))) == 1
    second = _ready_macos_card(hub, ["macos", "xcode"])
    ur.sweep(hub.conn)
    assert len(list(hub.conn.execute("SELECT id FROM tasks WHERE idempotency_key LIKE 'pickup-stall:hub:"
                                     "unclaimable-ready:%'"))) == 1  # the new card goes on the open alarm
    assert any(second in c.body for c in kb.list_comments(hub.conn, alarms[0])) \
        or second in kb.get_task(hub.conn, alarms[0]).body


def test_sweep_ignores_claimable_and_unready_cards(hub):
    ok = _ready_macos_card(hub, ["linux"])
    parked = _ready_macos_card(hub)
    with kb.write_txn(hub.conn):
        hub.conn.execute("UPDATE tasks SET status = 'blocked' WHERE id = ?", (parked,))
    assert ur.sweep(hub.conn) == []
    assert not _events(hub.conn, ok, "chat_wait_unclaimed")


def test_unknown_registry_flags_nothing(hub):
    _ready_macos_card(hub)
    wn.set_registry_fetch(lambda: (_ for _ in ()).throw(OSError("board down")))
    assert ur.sweep(hub.conn) == []


def test_a_worker_may_reroute_the_stranded_card(hub):
    tid = _ready_macos_card(hub)
    assert kcr.stranded_refusal(hub.conn, tid) is None
