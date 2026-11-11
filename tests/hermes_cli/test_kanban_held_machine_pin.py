"""A pin to a machine on Board ``hold`` is refused at the write, and a card already waiting on one says so. [daveey]

2026-11-11: worker-1 was put on ``hold`` at 02:17 UTC for an AZ migration (machine-audit seq 52). At 03:42 a MacBook
chat ran ``kanban_create(placement="worker-1")``; it returned ok with ``runs_on=[machine:worker-1]``. A held machine's
pickup imports nothing (``eligible`` -> ``held``) and the Board refuses its leases, so the card sat ready 48 min with
the origin reading "waiting for worker-1 ... queued, not stuck". ``placement_runs_on`` and ``check_machine_pins``
checked ``state`` and ``pickup`` but never ``hold``.

Pinned here, one test per write path: the agent tool with ``placement=<machine>``, the agent tool with ``runs_on``,
``hermes kanban create --runs-on``; and the read side (``kanban_show`` queue + note) for a card pinned before the hold.
"""
from __future__ import annotations

import json

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli import kanban_db_sync as sync
from hermes_cli import kanban_queue_position as kqp
from hermes_cli import kanban_worker_node as wn
from tests.hermes_cli.kanban_sync_fake_board import FakeService
from tests.hermes_cli.test_kanban_worker_node import HUB, MACHINES, WORKER, Gateway

WORKER_2 = {**MACHINES[WORKER], "name": "worker-2"}


@pytest.fixture
def svc():
    return FakeService(lease_runs=True, machines=MACHINES)


@pytest.fixture
def hub(tmp_path, monkeypatch, svc):
    monkeypatch.setattr(kbd, "_profile_exists_fn", lambda: None)
    monkeypatch.setattr("hermes_cli.kanban_capability_registry.self_gateway", lambda: HUB)
    sync.consume_dispatcher_wake()
    gw = Gateway(tmp_path, HUB, svc, monkeypatch, pickup=False)
    gw.enter()
    yield gw
    gw.conn.close()
    sync.set_lease_client(None)
    wn.set_registry_fetch(None)


def _registry(*, hold):
    """worker-1 as on 11-11 (active, pickup on, ``hold`` as given) beside an unheld worker-2."""
    held = {**MACHINES[WORKER], "hold": hold}
    wn.set_registry_fetch(lambda: {"me": HUB, "machines": [MACHINES[HUB], held, WORKER_2, MACHINES["macbook"]]})


def _count(conn):
    return conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]


def _create(args):
    from tools import kanban_tools
    return json.loads(kanban_tools._handle_create({"title": "verify five fixes", "assignee": "default", **args}))


def _refused(out):
    assert out.get("code") == "placement_unavailable" and out.get("rejection") == "machine_held", out
    assert "on hold" in out["error"] and "worker-2" in out["error"] and out["unheld"] == ["worker-2"], out
    assert out["created"] is False and 'placement="worker-1"' not in out["error"], out  # its gateway is held too


@pytest.mark.parametrize("args", [{"placement": WORKER}, {"runs_on": ["machine:worker-1"]}],
                         ids=["placement_pin", "explicit_runs_on"])
def test_tool_create_pinned_to_a_held_machine_is_refused_and_writes_nothing(hub, args):
    _registry(hold=True)
    before = _count(hub.conn)
    _refused(_create(args))
    assert _count(hub.conn) == before


@pytest.mark.parametrize("args", [{"placement": WORKER}, {"runs_on": ["machine:worker-1"]}],
                         ids=["placement_pin", "explicit_runs_on"])
def test_tool_create_pinned_to_an_unheld_machine_still_lands_with_its_label(hub, args):
    _registry(hold=False)
    out = _create(args)
    assert "error" not in out and out["runs_on"] == ["machine:worker-1"], out


def test_cli_create_pinned_to_a_held_machine_is_refused(hub):
    from hermes_cli import kanban as kcli
    _registry(hold=True)
    before = _count(hub.conn)
    out = kcli.run_slash('create "verify" --assignee default --runs-on machine:worker-1 --json')
    assert "placement_unavailable" in out and "on hold" in out, out
    assert _count(hub.conn) == before


def test_a_card_already_waiting_on_a_held_machine_reads_on_hold_not_queued(hub):
    """The hold can land after the create: the origin's card read must stop saying "queued, not stuck"."""
    _registry(hold=False)
    tid = kb.create_task(hub.conn, title="pinned before the hold", assignee="default",
                         runs_on=["machine:worker-1"])
    place = kqp.placement(hub.conn, tid)
    assert place == {"lane": "machine", "runs_on": ["machine:worker-1"], "machines": ["worker-1"]}, place
    assert "queued, not stuck" in (kqp.describe(place) or "")
    wn.set_registry_fetch(None)
    _registry(hold=True)
    place = kqp.placement(hub.conn, tid)
    assert place == {"lane": "machine", "runs_on": ["machine:worker-1"], "machines": ["worker-1"],
                     "held": ["worker-1"]}, place
    note = kqp.describe(place) or ""
    assert "ON HOLD" in note and "machine-audit worker-1" in note and "queued, not stuck" not in note, note


def test_a_label_one_unheld_machine_still_carries_keeps_the_plain_wait(hub):
    """``gpu`` is on held worker-1 and unheld worker-2: the card still has a taker, so no hold wording."""
    w1 = {**MACHINES[WORKER], "labels": ["linux", "gpu"], "hold": True}
    w2 = {**WORKER_2, "labels": ["linux", "gpu"]}
    wn.set_registry_fetch(lambda: {"me": HUB, "machines": [MACHINES[HUB], w1, w2, MACHINES["macbook"]]})
    tid = kb.create_task(hub.conn, title="needs a gpu", assignee="default", runs_on=["gpu"])
    place = kqp.placement(hub.conn, tid)
    assert place == {"lane": "machine", "runs_on": ["gpu"], "machines": ["worker-1", "worker-2"],
                     "held": ["worker-1"]}, place
    note = kqp.describe(place) or ""
    assert "ON HOLD" not in note and "queued, not stuck" in note, note
