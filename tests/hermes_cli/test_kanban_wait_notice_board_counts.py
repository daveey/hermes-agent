"""Wait notices name their board, read host counts read-only, and measure ready time without unused grants. [daveey]

Incidents (hermes-hub, 2026-11-10): runners/t_01a1c7d517a4477a was told "0/7 worker slots busy" while default had
7 running cards (the notice path called the host count without its board, so the count excluded ``default`` and
double-counted ``runners``); default/t_01a1c6c56a99c3e1 had a claimed+released ``unused_grant`` pair that reset its
ready age to the release time (``ready@1794369068``) though it had waited since ``continuation_woken`` 1794351914.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from hermes_cli import kanban_chat_wait as cw
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli import kanban_db_notify as kn
from hermes_cli import kanban_diagnostics as kd
from hermes_cli import kanban_parent_unclaimed as pu
from hermes_cli import kanban_queue_position as kqp

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
    kb.create_board("runners")
    return h


def _running(board: str | None, n: int) -> None:
    with kbc.connect_closing(board=board) as conn:
        for i in range(n):
            tid = kb.create_task(conn, title=f"busy {i}", assignee="default")
            conn.execute("UPDATE tasks SET status='running' WHERE id=?", (tid,))
        conn.commit()


def _waiting_card(conn, *, age_s: int = 40 * 60) -> str:
    """A subscribed ready card whose ready episode began ``age_s`` ago."""
    now = int(time.time())
    tid = kb.create_task(conn, title="waiting", assignee="default")
    kn.add_notify_sub(conn, task_id=tid, platform="tui", chat_id="origin-chat", delivery_metadata={"reason": "create"})
    conn.execute("UPDATE tasks SET created_at=? WHERE id=?", (now - age_s, tid))
    conn.execute("UPDATE task_events SET created_at=? WHERE task_id=?", (now - age_s, tid))
    conn.commit()
    return tid


def _notice(conn, tid: str, kind: str) -> dict:
    (payload,) = [json.loads(p) for (p,) in conn.execute(
        "SELECT payload FROM task_events WHERE task_id=? AND kind=?", (tid, kind))]
    return payload


# --- board identity and host counts ------------------------------------------------------------------------------


def test_runners_board_position_counts_default_runs_without_a_board_argument(home):
    _running(None, 7)
    with kbc.connect_closing(board="runners") as conn:
        tid = _waiting_card(conn)
        place = kqp.position(conn, kb.get_task(conn, tid))  # no board passed, as every notice path does
    assert (place["running"], place["limit"]) == (7, 7)
    assert place["board"] == "runners"


def test_default_board_position_does_not_count_itself_twice(home):
    _running(None, 3)
    _running("runners", 2)
    with kbc.connect_closing() as conn:
        tid = _waiting_card(conn)
        place = kqp.position(conn, kb.get_task(conn, tid))
    assert place["running"] == 5
    assert place["board"] == "default"


def test_host_count_opens_no_writable_connection(home, monkeypatch):
    _running(None, 7)
    with kbc.connect_closing(board="runners") as conn:
        tid = _waiting_card(conn)

        def _no_writer(*a, **k):
            raise AssertionError("a notice must not open a migrating/writable board connection")

        monkeypatch.setattr(kbd._kbc, "connect", _no_writer)
        place = kqp.position(conn, kb.get_task(conn, tid))
    assert place["running"] == 7


def test_unreadable_other_board_makes_the_running_count_unknown_not_a_partial_sum(home):
    _running(None, 7)
    (kb.kanban_db_path(board="default")).write_bytes(b"not a sqlite file" * 64)
    with kbc.connect_closing(board="runners") as conn:
        tid = _waiting_card(conn)
        place = kqp.position(conn, kb.get_task(conn, tid))
    assert place["running"] is None
    assert "busy" not in pu._queue_words(place)
    assert "unknown" in pu._queue_words(place)


# --- every notice path carries board, observation time and an admission reason or unknown --------------------------


def test_direct_chat_wait_notice_names_board_count_time_and_unknown_admission(home):
    _running(None, 7)
    with kbc.connect_closing(board="runners") as conn:
        tid = _waiting_card(conn)
        before = int(time.time())
        assert cw.sweep(conn) == 1
        payload = _notice(conn, tid, cw.NOTICE_KIND)
    assert payload["board"] == "runners"
    assert "7/7 worker slots busy" in payload["queue"]
    assert before <= payload["observed_at"] <= int(time.time())
    assert payload["admission"] == {"reason": None, "status": "unknown"}
    text = cw.notice_text(payload)
    assert "runners" in text and "admission reason unknown" in text and "7/7" in text


def test_parent_unclaimed_notice_names_board_count_time_and_unknown_admission(home):
    _running(None, 7)
    with kbc.connect_closing(board="runners") as conn:
        parent = _waiting_card(conn)
        child = kb.create_task(conn, title="child", assignee="default", parents=[parent])
        kn.add_notify_sub(conn, task_id=child, platform="tui", chat_id="origin-chat",
                          delivery_metadata={"reason": "create"})
        now = int(time.time())
        conn.execute("INSERT INTO task_events (task_id, kind, payload, created_at) VALUES (?, 'dependency_wait', "
                     "'{}', ?)", (child, now - 40 * 60))
        conn.commit()
        assert pu.sweep(conn) == 1
        payload = _notice(conn, child, pu.NOTICE_KIND)
    assert payload["board"] == "runners"
    assert "7/7 worker slots busy" in payload["queue"]
    assert payload["admission"]["status"] == "unknown"
    assert isinstance(payload["observed_at"], int)
    text = pu.notice_text(payload)
    assert "runners" in text and "admission reason unknown" in text


def test_show_path_without_board_reads_the_connections_board(home):
    _running(None, 7)
    with kbc.connect_closing(board="runners") as conn:
        tid = _waiting_card(conn)
        place = kqp.read(conn, kb.get_task(conn, tid), None)
    assert place["running"] == 7 and place["board"] == "runners"
    assert "7/7 running" in kqp.describe(place)


def test_unknown_running_count_is_worded_unknown_in_card_reads(home):
    place = {"lane": "local", "ahead": 3, "running": None, "limit": 7, "approximate": True, "board": "runners"}
    assert "running count unknown" in kqp.describe(place)


# --- ready clock: unused grants never reset it -----------------------------------------------------------------


def _grant_pair(conn, tid: str, at: int, *, lock: bool = False) -> int:
    """A Board reservation claimed and returned unused at ``at`` (no worker spawned)."""
    cur = conn.execute("INSERT INTO task_runs (task_id, status, started_at, ended_at, outcome, metadata) "
                       "VALUES (?, 'released', ?, ?, 'released', ?)", (tid, at, at, json.dumps({"unused_grant": True})))
    run_id = cur.lastrowid
    claimed = {"run_id": run_id, **({"lock": "hub:1"} if lock else {})}
    conn.execute("INSERT INTO task_events (task_id, run_id, kind, payload, created_at) VALUES (?, ?, 'claimed', ?, ?)",
                 (tid, run_id, json.dumps(claimed), at))
    conn.execute("INSERT INTO task_events (task_id, run_id, kind, payload, created_at) VALUES (?, ?, 'released', ?, ?)",
                 (tid, run_id, json.dumps({"unused_grant": True}), at))
    conn.commit()
    return run_id


def _woken_card(conn, *, woken_ago: int = 40 * 60) -> tuple[str, int]:
    now = int(time.time())
    tid = _waiting_card(conn, age_s=3 * 3600)
    woken = now - woken_ago
    conn.execute("INSERT INTO task_events (task_id, kind, payload, created_at) VALUES (?, 'continuation_woken', "
                 "'{}', ?)", (tid, woken))
    conn.commit()
    return tid, woken


def test_repeated_unused_grants_preserve_the_ready_age(home):
    now = int(time.time())
    with kbc.connect_closing() as conn:
        tid, woken = _woken_card(conn)
        _grant_pair(conn, tid, now - 600)
        _grant_pair(conn, tid, now - 60)
        assert pu.ready_since(conn, tid) == woken
        assert cw.sweep(conn) == 1  # the SQL prefilter must not hide a card whose grants are recent
        payload = _notice(conn, tid, cw.NOTICE_KIND)
    assert payload["wait_event_id"] == f"ready@{woken}"
    assert payload["ready_minutes"] == 40


def test_pure_diagnostic_floor_ignores_unused_grant_runs_and_their_events(home):
    events = [
        {"id": 1, "kind": "continuation_woken", "created_at": 1000, "payload": {}, "run_id": None},
        {"id": 2, "kind": "claimed", "created_at": 5000, "payload": {"run_id": 7}, "run_id": 7},
        {"id": 3, "kind": "released", "created_at": 5000, "payload": {"unused_grant": True}, "run_id": 7},
    ]
    runs = [{"id": 7, "status": "released", "outcome": "released", "started_at": 5000, "ended_at": 5000,
             "metadata": json.dumps({"unused_grant": True})}]
    assert kd._last_ready_entry_ts(events, runs) == 1000


def test_a_grant_carrying_a_worker_lock_is_real_execution_and_still_resets(home):
    now = int(time.time())
    with kbc.connect_closing() as conn:
        tid, _woken = _woken_card(conn)
        _grant_pair(conn, tid, now - 600, lock=True)
        assert pu.ready_since(conn, tid) == now - 600


def test_actual_lost_worker_after_unused_grants_resets_the_clock(home):
    now = int(time.time())
    with kbc.connect_closing() as conn:
        tid, _woken = _woken_card(conn, woken_ago=3 * 3600)
        _grant_pair(conn, tid, now - 2 * 3600)
        meta = {"exit_code": 76, "owner_restart": True, "retry_status": "ready"}
        cur = conn.execute("INSERT INTO task_runs (task_id, status, started_at, ended_at, outcome, metadata) "
                           "VALUES (?, 'reclaimed', ?, ?, 'reclaimed', ?)",
                           (tid, now - 5400, now - 2400, json.dumps(meta)))
        conn.execute("INSERT INTO task_events (task_id, run_id, kind, payload, created_at) VALUES "
                     "(?, ?, 'claimed', ?, ?)", (tid, cur.lastrowid, json.dumps({"lock": "hub:1"}), now - 5400))
        conn.execute("INSERT INTO task_events (task_id, run_id, kind, payload, created_at) VALUES "
                     "(?, ?, 'owner_restart_lost', ?, ?)", (tid, cur.lastrowid, json.dumps(meta), now - 2400))
        conn.commit()
        assert pu.ready_since(conn, tid) == now - 2400
        assert cw.sweep(conn) == 1
        assert _notice(conn, tid, cw.NOTICE_KIND)["ready_minutes"] == 40


# --- advice: no fixed priority command, no uncoordinated takeover --------------------------------------------------


def test_notice_and_queue_advice_carry_no_fixed_priority_or_takeover_command(home):
    from hermes_cli import kanban_chat_wait, kanban_queue_position
    texts = [pu._options("t_x"), kanban_chat_wait.NO_PROMISE, kanban_queue_position._NOT_STUCK]
    for text in texts:
        assert "--priority 10" not in text
        assert "do the work here" not in text.lower() and "do work here" not in text.lower()


# --- guards on the new conditions ----------------------------------------------------------------------------------


@pytest.mark.parametrize("field,value", [("worker_pid", 4242), ("worker_started_at", 5000), ("last_heartbeat_at", 5000),
                                         ("claim_lock", "hub:1"), ("summary", "did work"), ("error", "boom")])
def test_a_grant_run_with_any_execution_trace_is_not_unused(field, value):
    from hermes_cli.kanban_unused_grant import unused_grant_run
    run = {"id": 7, "status": "released", "outcome": "released", "ended_at": 5000,
           "metadata": json.dumps({"unused_grant": True}), field: value}
    events = [{"kind": "claimed", "payload": {"run_id": 7}}, {"kind": "released", "payload": {"unused_grant": True}}]
    assert unused_grant_run(run, events) is False
    del run[field]
    assert unused_grant_run(run, events) is True


def test_a_grant_whose_run_has_no_release_receipt_is_not_unused():
    from hermes_cli.kanban_unused_grant import unused_grant_run
    run = {"id": 7, "status": "released", "outcome": "released", "ended_at": 5000,
           "metadata": json.dumps({"unused_grant": True})}
    assert unused_grant_run(run, [{"kind": "claimed", "payload": {"run_id": 7}}]) is False
    assert unused_grant_run(run, [{"kind": "spawned", "payload": {"pid": 1}}]) is False


def test_in_memory_connection_has_unknown_board_and_count(home):
    import sqlite3
    from hermes_cli import kanban_host_counts as khc
    mem = sqlite3.connect(":memory:")
    assert khc.board_of(mem) is None
    got = khc.running_on_host(mem)
    assert got["running"] is None and got["board"] is None


def test_board_without_a_database_file_is_skipped_not_unknown(home):
    from hermes_cli import kanban_host_counts as khc
    _running(None, 2)
    kb.kanban_db_path(board="runners").unlink()
    with kbc.connect_closing() as conn:
        assert khc.running_on_host(conn)["running"] == 2


def test_boards_pinned_to_one_file_count_once(home, monkeypatch):
    from hermes_cli import kanban_host_counts as khc
    _running(None, 2)
    monkeypatch.setenv("HERMES_KANBAN_DB", str(kb.kanban_db_path(board="default")))
    with kbc.connect_closing() as conn:
        assert khc.running_on_host(conn)["running"] == 2


@pytest.mark.parametrize("field,value", [("outcome", "reclaimed"), ("status", "reclaimed"), ("ended_at", None)])
def test_only_a_released_ended_grant_is_unused(field, value):
    from hermes_cli.kanban_unused_grant import unused_grant_run
    run = {"id": 7, "status": "released", "outcome": "released", "ended_at": 5000,
           "metadata": json.dumps({"unused_grant": True}), field: value}
    events = [{"kind": "claimed", "payload": {"run_id": 7}}, {"kind": "released", "payload": {"unused_grant": True}}]
    assert unused_grant_run(run, events) is False
