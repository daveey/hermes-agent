"""The sync loop's report census reuses unchanged boards and still reports what a fresh read reports."""
import os
import sqlite3

import pytest

from hermes_cli import kanban_db
from hermes_cli import kanban_db_boards
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_sync as sync
from hermes_cli import kanban_sync_client as client
from hermes_cli import machine_metrics as mm
from hermes_cli import machine_report_census as census
from hermes_cli.kanban_sync_board_watch import BoardWatch
from tests.hermes_cli import kanban_sync_support as support


@pytest.fixture
def host(tmp_path, monkeypatch):
    """A synced default board and two named boards; yields ({slug: path}, run(slug) -> task id)."""
    home = support.kanban_home(tmp_path, monkeypatch)
    support.write_sync_config(home, True)
    paths = {"default": home / "kanban.db"}
    for slug in ("one", "two"):
        kanban_db_boards.create_board(slug)
        paths[slug] = kanban_db.kanban_db_path(board=slug)
    for slug, path in paths.items():
        conn = kbc.connect(path)
        client.ensure_identity(conn, slug, "fixture")
        sync.set_mode(conn, "primary")
        conn.close()

    def run(slug, status="running"):
        conn = kbc.connect(paths[slug])
        try:
            task = kanban_db.create_task(conn, title="t", assignee="default")
            conn.execute("UPDATE tasks SET status = ? WHERE id = ?", (status, task))
            conn.commit()
            return task
        finally:
            conn.close()

    watch = BoardWatch()
    try:
        yield paths, run, watch
    finally:
        watch.reset()
        client.reset_board_watch()


def both(watch):
    """(kept-reader answer, fresh-connection answer), each as (count, pools)."""
    out = []
    for count in (census.census(watch), mm.active_card_runs):
        detail = {"pools": {"regular": 0, "priority": 0}}
        out.append((count(detail=detail), detail))
    return out


def reads(monkeypatch):
    seen = []
    original = mm._running_rows

    def counted(db):
        seen.append(db)
        return original(db)

    monkeypatch.setattr(mm, "_running_rows", counted)
    return seen


def test_census_equals_a_fresh_read_and_follows_other_connections_writes(host):
    paths, run, watch = host
    kept, fresh = both(watch)
    assert kept == fresh and kept[0] == 0
    run("default")
    run("one")
    run("two", status="ready")
    kept, fresh = both(watch)
    assert kept == fresh and kept[0] == 2
    done = run("two")
    assert both(watch)[0][0] == 3
    conn = kbc.connect(paths["two"])
    conn.execute("UPDATE tasks SET status = 'done' WHERE id = ?", (done,))
    conn.commit()
    conn.close()
    kept, fresh = both(watch)
    assert kept == fresh and kept[0] == 2


def test_unchanged_boards_are_not_read_again_and_one_write_rereads_one_board(host, monkeypatch):
    _paths, run, watch = host
    run("one")
    count = census.census(watch)
    assert count() == 1
    seen = reads(monkeypatch)
    assert count() == 1 and count() == 1
    assert seen == []  # no board changed: no statement over tasks
    run("two")
    assert count() == 2
    assert len(seen) == 1
    assert count() == 2 and len(seen) == 1


def test_the_census_opens_no_connection_once_its_readers_are_kept(host, monkeypatch):
    _paths, _run, watch = host
    count = census.census(watch)
    assert count() == 0
    opened = []
    original = sqlite3.connect
    monkeypatch.setattr(sqlite3, "connect", lambda *a, **k: opened.append(a) or original(*a, **k))
    assert count() == 0
    assert opened == []


def test_a_replaced_board_file_is_read_again(host, monkeypatch):
    paths, run, watch = host
    run("one")
    count = census.census(watch)
    assert count() == 1
    conn = kbc.connect(paths["one"])
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    conn.close()
    copy = paths["one"].with_name("copy.db")
    copy.write_bytes(paths["one"].read_bytes())
    edit = sqlite3.connect(copy)
    edit.execute("UPDATE tasks SET status = 'done'")
    edit.commit()
    edit.close()
    os.replace(copy, paths["one"])
    kept, fresh = both(watch)
    assert kept == fresh and kept[0] == 0


def test_a_schema_the_one_statement_read_cannot_decide_uses_the_fresh_census(host, monkeypatch):
    paths, _run, watch = host
    old = paths["one"].parent.parent / "old" / "kanban.db"
    old.parent.mkdir()
    db = sqlite3.connect(old)
    db.executescript("CREATE TABLE tasks (id TEXT, status TEXT); INSERT INTO tasks VALUES ('a','running'),('b','ready');")
    db.commit()
    db.close()
    calls = []
    original = mm.active_card_runs

    def fallback(**kwargs):
        calls.append(kwargs)
        return original(**kwargs)

    count = census.census(watch, fallback)
    detail = {"pools": {"regular": 0, "priority": 0}}
    assert count() == original() == 1
    assert count(detail=detail) == original(detail={"pools": {"regular": 0, "priority": 0}})
    assert len(calls) == 2 and calls[1]["detail"] is detail


def test_an_unreadable_board_uses_the_fresh_census(host, monkeypatch):
    _paths, _run, watch = host
    monkeypatch.setattr(watch, "read", lambda *a, **k: None)
    calls = []
    assert census.census(watch, lambda detail=None: calls.append(detail) or 7)() == 7
    assert calls == [None]


def test_a_vanished_named_board_is_unknown_and_an_absent_default_board_is_empty(host, monkeypatch):
    paths, run, watch = host
    run("one")
    count = census.census(watch)
    paths["default"].unlink()
    for suffix in ("-wal", "-shm"):
        paths["default"].with_name("kanban.db" + suffix).unlink(missing_ok=True)
    assert count() == mm.active_card_runs() == 1
    from hermes_cli import kanban_db_boards as boards
    gone = paths["two"].with_name("gone.db")
    monkeypatch.setattr(boards, "named_board_db_files", lambda: [("gone", gone)])
    assert count() is None and mm.active_card_runs() is None


def test_a_synced_board_without_its_own_gateway_id_is_unknown(host):
    paths, _run, watch = host
    conn = kbc.connect(paths["two"])
    conn.execute("UPDATE kanban_sync_state SET gateway_id = ''")
    conn.commit()
    conn.close()
    kept, fresh = both(watch)
    assert kept[0] is None and fresh[0] is None


def test_two_questions_about_one_board_keep_separate_answers(host):
    paths, run, watch = host
    path = paths["default"]
    sql = "SELECT count(*) FROM tasks"
    computed = []

    def rows(conn):
        computed.append(1)
        return conn.execute("SELECT count(*) FROM tasks WHERE status = 'running'").fetchone()[0]

    for _ in range(3):
        assert watch.query(path, sql, unchanged_ok=True)[1][0] == 0
        assert watch.read(path, "t", rows, unchanged_ok=True)[1] == 0
    assert computed == [1]
    run("default")
    assert watch.read(path, "t", rows, unchanged_ok=True)[1] == 1
    assert watch.query(path, sql, unchanged_ok=True)[1][0] == 1
    assert watch.read(path, "t", rows, unchanged_ok=True)[1] == 1
    assert computed == [1, 1]


def test_a_compute_error_stores_nothing_and_keeps_the_reader(host):
    paths, _run, watch = host
    path = paths["default"]

    def broken(conn):
        raise ValueError("not sqlite")

    with pytest.raises(ValueError):
        watch.read(path, "t", broken, unchanged_ok=True)
    assert watch.read(path, "t", lambda conn: 5, unchanged_ok=True)[1] == 5


def test_only_the_reporters_own_collect_uses_the_kept_census(host, monkeypatch):
    """Admission and every other collect() caller keep the fresh per-call read."""
    _paths, _run, _watch = host
    used = []
    monkeypatch.setattr(mm, "active_card_runs", lambda **kw: used.append("fresh") or 0)
    sent = []
    reporter = mm.Reporter(sent.append, clock=lambda: 1000.0)
    reporter.census_fn = lambda **kw: used.append("kept") or 4
    assert reporter.report_due()
    assert used == ["kept"] and sent[0]["active_card_runs"] == 4
    assert getattr(mm._REPORT, "census", None) is None
    assert mm.collect()["active_card_runs"] == 0
    assert used == ["kept", "fresh"]


def test_a_failed_report_collect_does_not_leave_the_kept_census_installed(host, monkeypatch):
    _paths, _run, _watch = host

    def boom():
        assert mm._REPORT.census is not None
        raise OSError("disk")

    reporter = mm.Reporter(lambda sample: None, collect_fn=boom, clock=lambda: 1000.0)
    reporter.census_fn = lambda **kw: 4
    try:
        reporter.report_due()
    except OSError:
        pass
    assert getattr(mm._REPORT, "census", None) is None


def test_the_sync_loop_gives_its_reporter_the_kept_census(monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import Mock
    from hermes_cli import kanban_sync_links_pass, kanban_sync_schema_alert, machine_cpu_sampler, machine_metrics_sender

    made = []

    def reporter(*args):
        made.append(Mock())
        return made[-1]

    monkeypatch.setattr(client, "load_identity", lambda: SimpleNamespace(gateway="test", url="unused"))
    monkeypatch.setattr(client, "Hub", lambda ident: Mock())
    monkeypatch.setattr(client.sync, "set_lease_client", lambda value: None)
    monkeypatch.setattr(kanban_sync_schema_alert, "note_start", lambda: None)
    monkeypatch.setattr(kanban_sync_links_pass, "LinksPass", lambda gateway: Mock())
    monkeypatch.setattr(mm, "Reporter", reporter)
    monkeypatch.setattr(machine_cpu_sampler, "AdvisorySampler", Mock)
    monkeypatch.setattr(machine_metrics_sender, "MetricsSender", lambda *a: Mock())
    monkeypatch.setattr(machine_metrics_sender, "board_post", lambda ident: None)
    client._run_forever(lambda: False, 1, Mock())
    assert len(made) == 1
    assert made[0].census_fn.__qualname__ == census.census(client._WATCH).__qualname__
    assert made[0].census_fn.__closure__ is not None
    assert client._WATCH in [cell.cell_contents for cell in made[0].census_fn.__closure__]
