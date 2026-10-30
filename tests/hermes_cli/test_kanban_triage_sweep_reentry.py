"""The stale-triage sweep releases a card once per triage entry, capped at 3 sweeps per card per 24 h.

An ownerless card (no chat) has no other releaser, so a once-per-card-ever guard stranded it the
second time it landed in triage. [daveey]
"""
import json
import time

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_dispatch as kd

T0 = 1_900_000_000
HOUR = 3600


@pytest.fixture
def clock(monkeypatch):
    now = [T0]
    monkeypatch.setattr(time, "time", lambda: float(now[0]))
    return now


@pytest.fixture
def conn(tmp_path, monkeypatch, clock):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    kb.init_db()
    from hermes_cli.kanban_db_connect import connect_closing
    with connect_closing() as c:
        yield c


def _status(conn, tid):
    return conn.execute("SELECT status FROM tasks WHERE id=?", (tid,)).fetchone()["status"]


def _events(conn, tid, kind):
    return [json.loads(r["payload"] or "{}") for r in conn.execute(
        "SELECT payload FROM task_events WHERE task_id=? AND kind=? ORDER BY id", (tid, kind))]


def _give_up(conn, tid, reason):
    """What the dispatcher's failure budget does: the card goes to triage with a gave_up event."""
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET status='triage', block_kind='transient' WHERE id=?", (tid,))
        kb._append_event(conn, tid, "gave_up", {"reason_code": "worker_failures_exhausted", "error": reason})


def test_retriaged_card_is_swept_again(conn, clock):
    tid = kb.create_task(conn, title="ownerless", triage=True)
    clock[0] += HOUR + 1
    assert kd.sweep_stale_triage(conn) == [tid]
    clock[0] += 60
    _give_up(conn, tid, "drain kill 1")
    clock[0] += HOUR - 10
    assert kd.sweep_stale_triage(conn) == []  # this entry is still fresh
    clock[0] += 20
    assert kd.sweep_stale_triage(conn) == [tid]
    assert _status(conn, tid) == "ready"
    assert len(_events(conn, tid, "triage_swept")) == 2
    body = conn.execute("SELECT body FROM tasks WHERE id=?", (tid,)).fetchone()["body"]
    assert body.count("[triage sweep]") == 1


def test_fourth_triage_in_a_day_is_capped_with_one_supervisor_card(conn, clock):
    tid = kb.create_task(conn, title="drain victim", body="do the thing")
    for n in range(1, 5):
        _give_up(conn, tid, f"drain kill {n}")
        clock[0] += HOUR + 1
        swept = kd.sweep_stale_triage(conn, default_assignee="default")
        assert swept == ([tid] if n <= 3 else []), n
        clock[0] += 60
    assert len(_events(conn, tid, "triage_swept")) == 3
    assert _status(conn, tid) == "triage"
    capped = _events(conn, tid, "triage_sweep_capped")
    assert len(capped) == 1
    sup_id = capped[0]["supervisor_task_id"]
    sup = kb.get_task(conn, sup_id)
    assert sup.assignee == "default" and sup.created_by == "triage-sweep"
    assert sup.status == "ready"
    assert tid in sup.body
    for n in (2, 3, 4):
        assert f"drain kill {n}" in sup.body
    assert "drain kill 1" not in sup.body
    assert f"hermes kanban promote {tid}" in sup.body
    # Later ticks neither sweep it nor file another supervisor card.
    for _ in range(3):
        clock[0] += HOUR
        assert kd.sweep_stale_triage(conn) == []
    assert len(_events(conn, tid, "triage_sweep_capped")) == 1
    assert conn.execute("SELECT COUNT(*) FROM tasks WHERE created_by='triage-sweep'").fetchone()[0] == 1


def test_cap_supervisor_uses_orchestrator_profile(conn, clock, monkeypatch):
    from hermes_cli import kanban_triage_sweep as ts
    monkeypatch.setattr(ts, "_supervisor_profile", lambda: "boss")
    tid = kb.create_task(conn, title="loops")
    for n in range(4):
        _give_up(conn, tid, f"r{n}")
        clock[0] += HOUR + 1
        kd.sweep_stale_triage(conn)
    sup_id = _events(conn, tid, "triage_sweep_capped")[0]["supervisor_task_id"]
    assert kb.get_task(conn, sup_id).assignee == "boss"


def test_sweeps_older_than_a_day_do_not_count_toward_the_cap(conn, clock):
    tid = kb.create_task(conn, title="slow repeat")
    for n in range(5):
        _give_up(conn, tid, f"r{n}")
        clock[0] += HOUR + 1
        assert kd.sweep_stale_triage(conn) == [tid], n
        clock[0] += 10 * HOUR
    assert _events(conn, tid, "triage_sweep_capped") == []


def test_open_human_request_is_untouched(conn, clock):
    tid = kb.create_task(conn, title="asks david", triage=True)
    conn.execute("UPDATE tasks SET human_request=? WHERE id=?",
                 (json.dumps({"action": "reply yes"}), tid))
    conn.commit()
    clock[0] += 5 * HOUR
    assert kd.sweep_stale_triage(conn) == []
    _give_up(conn, tid, "again")
    conn.execute("UPDATE tasks SET human_request=? WHERE id=?", (json.dumps({"action": "reply yes"}), tid))
    conn.commit()
    clock[0] += 5 * HOUR
    assert kd.sweep_stale_triage(conn) == []
    assert _status(conn, tid) == "triage"


def test_young_triage_entry_is_untouched(conn, clock):
    tid = kb.create_task(conn, title="fresh", triage=True)
    clock[0] += HOUR + 1
    assert kd.sweep_stale_triage(conn) == [tid]
    clock[0] += 5 * HOUR  # long after the first sweep...
    _give_up(conn, tid, "new failure")
    clock[0] += HOUR - 1  # ...but the new entry is younger than the window
    assert kd.sweep_stale_triage(conn) == []
    assert _status(conn, tid) == "triage"
