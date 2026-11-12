"""Per-tick gateway passes read once per pass, not once per card. [daveey]

Hub gateway profile 2026-11-12 (t_01a1ccca7a1979a7, 4,948 cards, 139% of a core): the divergence repair read the
replica once per settled card every sync round (13% of samples), the wait-notice sweeps computed a queue place for
every long-ready card every dispatcher tick before finding its notice already posted (5%), and ``gateway_names``
opened hub.db once per link in the links pass (4%).
"""
from __future__ import annotations

import os
import sqlite3
import time
from pathlib import Path

import pytest

from hermes_cli import kanban_chat_wait as cw
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli import kanban_db_notify as kn
from hermes_cli import kanban_db_sync as sync
from hermes_cli import kanban_origin
from hermes_cli import kanban_parent_unclaimed as pu
from hermes_cli import kanban_queue_position as kqp
from hermes_cli import kanban_sync_client as ksc
from tests.hermes_cli import kanban_sync_support as support
from tests.hermes_cli.kanban_sync_fake_board import FakeService

ME, BOARD = "macbook", "default"


# --- divergence repair --------------------------------------------------------------------------------------------


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    return support.kanban_home(tmp_path, monkeypatch)


@pytest.fixture
def live(kanban_home):
    support.write_sync_config(kanban_home, True)
    conn = kbc.connect(kanban_home / "kanban.db")
    ksc.ensure_identity(conn, BOARD, ME)
    try:
        yield conn
    finally:
        conn.close()


@pytest.mark.parametrize("cards", [3, 40])
def test_divergence_repair_reads_the_replica_per_pass_not_per_card(live, kanban_home, monkeypatch, cards):
    svc = FakeService()
    sync.set_mode(live, "primary")
    ids = [kb.create_task(live, title=f"card {i}", assignee="worker") for i in range(cards)]
    for _ in range(3):
        ksc.sync_board(BOARD, kanban_home / "kanban.db", svc.hub(ME), ME)
    real, seen = ksc.repair_divergence, []

    def counted(live_conn, shadow, gateway):
        statements = []
        shadow.set_trace_callback(statements.append)
        try:
            return real(live_conn, shadow, gateway)
        finally:
            shadow.set_trace_callback(None)
            seen.append([s for s in statements if s.lstrip().upper().startswith("SELECT")])

    monkeypatch.setattr(ksc, "repair_divergence", counted)
    with kbc.write_txn(live):
        sync.drop_triggers(live)  # an uncaptured write: only the repair scan finds it
        live.execute("UPDATE tasks SET title = 'gap edit' WHERE id = ?", (ids[-1],))
    sync.install_triggers(live)
    ksc.sync_board(BOARD, kanban_home / "kanban.db", svc.hub(ME), ME)
    ksc.sync_board(BOARD, kanban_home / "kanban.db", svc.hub(ME), ME)
    assert seen, "the repair scan did not run after a local write"
    assert len(seen[0]) <= 2, f"{len(seen[0])} replica reads for {cards} settled cards: {seen[0][:6]}"
    assert svc.board(BOARD)["rows"]["tasks"][ids[-1]]["title"] == "gap edit", "the uncaptured edit was not healed"


# --- wait notices ---------------------------------------------------------------------------------------------------

_ENV = ("HERMES_KANBAN_DB", "HERMES_KANBAN_BOARD", "HERMES_KANBAN_TASK", "HERMES_KANBAN_RUN_ID",
        "HERMES_KANBAN_CLAIM_LOCK", "HERMES_KANBAN_HOME", "HERMES_DELEGATED_CHILD_CONTEXT",
        "HERMES_KANBAN_WORKER_TASK")


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    h = tmp_path / ".hermes"
    h.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(h))
    monkeypatch.setenv("HERMES_PROFILE", "default")
    for key in _ENV:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(kbd, "configured_max_in_progress", lambda: 7)
    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    return h


def _waiting_card(conn, *, age_s: int = 40 * 60) -> str:
    now = int(time.time())
    tid = kb.create_task(conn, title="waiting", assignee="default")
    kn.add_notify_sub(conn, task_id=tid, platform="tui", chat_id="origin-chat", delivery_metadata={"reason": "create"})
    conn.execute("UPDATE tasks SET created_at=? WHERE id=?", (now - age_s, tid))
    conn.execute("UPDATE task_events SET created_at=? WHERE task_id=?", (now - age_s, tid))
    conn.commit()
    return tid


def _count_places(monkeypatch) -> list:
    real, places = kqp.position, []

    def counted(conn, task, *a, **k):
        places.append(task.id)
        return real(conn, task, *a, **k)

    monkeypatch.setattr(kqp, "position", counted)
    return places


def test_a_posted_wait_notice_costs_no_queue_place_on_later_ticks(home, monkeypatch):
    places = _count_places(monkeypatch)
    with kbc.connect_closing() as conn:
        cards = [_waiting_card(conn) for _ in range(5)]
        assert cw.sweep(conn) == 5
        assert sorted(places) == sorted(cards)  # the notice itself still names the queue place
        places.clear()
        for _ in range(3):
            assert cw.sweep(conn) == 0
        assert places == [], f"queue place recomputed for already-noticed cards: {places}"
        notices = conn.execute("SELECT COUNT(*) FROM task_events WHERE kind=?", (cw.NOTICE_KIND,)).fetchone()[0]
    assert notices == 5


def test_a_parent_unclaimed_notice_costs_no_queue_place_once_posted(home, monkeypatch):
    places = _count_places(monkeypatch)
    with kbc.connect_closing() as conn:
        parent = _waiting_card(conn)
        child = kb.create_task(conn, title="child", assignee="default", parents=[parent])
        kn.add_notify_sub(conn, task_id=child, platform="tui", chat_id="origin-chat")
        kb._append_event(conn, child, "dependency_wait", {"reason": "parent"})
        conn.execute("UPDATE task_events SET created_at=? WHERE task_id=?", (int(time.time()) - 3600, child))
        conn.commit()
        assert pu.sweep(conn) == 1
        assert places == [parent]
        places.clear()
        assert pu.sweep(conn) == 0
    assert places == []


# --- gateway names ------------------------------------------------------------------------------------------------


@pytest.fixture
def hub_db(tmp_path, monkeypatch):
    state = tmp_path / "hub-state"
    state.mkdir()
    monkeypatch.setenv("HERMES_HUB_STATE", str(state))
    monkeypatch.setenv("HERMES_MAIL_CONFIG", str(tmp_path / "absent.json"))
    monkeypatch.setattr(kanban_origin, "_names_memo", None, raising=False)
    db = sqlite3.connect(state / "hub.db")
    db.execute("CREATE TABLE meta (k TEXT PRIMARY KEY, v TEXT)")
    db.execute("CREATE TABLE replica (gw TEXT)")
    db.execute("INSERT INTO meta VALUES ('gateway', 'hub')")
    db.execute("INSERT INTO replica VALUES ('macbook')")
    db.commit()
    old = time.time() - 60
    os.utime(state / "hub.db", (old, old))  # a settled file, as on a running host
    yield db
    db.close()


def _opens(monkeypatch) -> list:
    real, opened = kanban_origin._ro, []

    def counted(path):
        opened.append(str(path))
        return real(path)

    monkeypatch.setattr(kanban_origin, "_ro", counted)
    return opened


def test_gateway_names_opens_hub_db_once_for_a_pass_of_lookups(hub_db, monkeypatch):
    opened = _opens(monkeypatch)
    answers = [kanban_origin.gateway_names() for _ in range(200)]
    assert all(a == ("hub", {"hub", "macbook"}) for a in answers)
    assert len(opened) == 1, f"{len(opened)} hub.db opens for 200 lookups"
    answers[0][1].add("mutated by a caller")
    assert kanban_origin.gateway_names()[1] == {"hub", "macbook"}


def test_gateway_names_sees_a_hub_db_write_at_once(hub_db, monkeypatch):
    opened = _opens(monkeypatch)
    assert kanban_origin.gateway_names() == ("hub", {"hub", "macbook"})
    hub_db.execute("INSERT INTO replica VALUES ('mini')")
    hub_db.commit()
    assert kanban_origin.gateway_names() == ("hub", {"hub", "macbook", "mini"})
    assert len(opened) == 2


def test_gateway_names_rereads_after_the_age_bound_and_never_keeps_a_just_written_file(hub_db, monkeypatch):
    opened = _opens(monkeypatch)
    clock = [1000.0]
    monkeypatch.setattr(kanban_origin.time, "monotonic", lambda: clock[0])
    kanban_origin.gateway_names()
    clock[0] += kanban_origin._NAMES_MAX_AGE_S + 0.1
    kanban_origin.gateway_names()
    assert len(opened) == 2
    hub_db.execute("INSERT INTO replica VALUES ('mini')")
    hub_db.commit()  # written inside the timestamp granularity: the answer is used once, not remembered
    kanban_origin.gateway_names()
    kanban_origin.gateway_names()
    assert len(opened) == 4
