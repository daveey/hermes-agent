"""GET /board keeps its SQL rows while the database is unchanged.

Every GET reprojects clock-driven diagnostics, ages, config and profiles. Retained
rows avoid full materialization: commits patch changed rows incrementally, including
bookkeeping-only writes observed through ``PRAGMA data_version``.
"""

from __future__ import annotations

import importlib.util
import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc


def _load_plugin():
    repo_root = Path(__file__).resolve().parents[2]
    plugin_file = repo_root / "plugins" / "kanban" / "dashboard" / "plugin_api.py"
    spec = importlib.util.spec_from_file_location("hermes_kanban_plugin_board_rows_test", plugin_file)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def _clear_rows():
    try:
        from plugins.kanban.dashboard import board_rows
    except ImportError:  # base tree (RED run) has no row cache
        return
    board_rows.clear()


@pytest.fixture
def env(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    assert str(kb.kanban_db_path()).startswith(str(tmp_path))
    _clear_rows()
    mod = _load_plugin()
    sql_reads = []
    real_list = kb.list_tasks
    monkeypatch.setattr(kb, "list_tasks", lambda *a, **k: sql_reads.append(1) or real_list(*a, **k))
    yield mod, sql_reads, home
    _clear_rows()


def _read(mod, **kw):
    args = dict(tenant=None, include_archived=False, board=None, workflow_template_id=None,
                current_step_key=None, done_limit=mod._BOARD_DONE_LIMIT)
    args.update(kw)
    return mod._get_board_uncached(args["tenant"], args["include_archived"], args["board"],
                                   args["workflow_template_id"], args["current_step_key"], args["done_limit"])


def _cards(body):
    return {t["id"]: t for col in body["columns"] for t in col["tasks"]}


def _create(title, **kw):
    conn = kbc.connect()
    try:
        return kb.create_task(conn, title=title, assignee=kw.pop("assignee", "worker"), **kw)
    finally:
        conn.close()


def _raw(sql, params=()):
    """A commit from a separate plain connection (no kanban helpers, no events)."""
    conn = sqlite3.connect(str(kb.kanban_db_path()))
    try:
        conn.execute(sql, params)
        conn.commit()
    finally:
        conn.close()


def _strip_now(body):
    return {k: v for k, v in body.items() if k != "now"}


def _freeze_wall(monkeypatch):
    """Pin time.time() so reads compared for equality share one clock second.

    Task ages are whole seconds of time.time(); on a CPU-throttled runner two reads a few
    milliseconds apart can straddle a second boundary and differ only in created_age_seconds.
    """
    t = float(int(time.time()))
    monkeypatch.setattr(time, "time", lambda: t)


def test_unchanged_board_materializes_rows_once(env, monkeypatch):
    mod, sql_reads, _home = env
    tid = _create("first")
    _freeze_wall(monkeypatch)
    bodies = [_read(mod) for _ in range(5)]
    assert len(sql_reads) == 1
    assert tid in _cards(bodies[0])
    assert all(_strip_now(b) == _strip_now(bodies[0]) for b in bodies[1:])


def test_incremental_samples_commit_clock_once_per_read(env):
    from plugins.kanban.dashboard import board_rows

    mod, _sql_reads, _home = env
    tid = _create("first")
    _read(mod)
    [obs] = board_rows._observers.values()
    statements = []
    obs.conn.set_trace_callback(statements.append)
    try:
        for title in ("first", "changed"):
            if title == "changed":
                _raw("UPDATE tasks SET title=? WHERE id=?", (title, tid))
            statements.clear()
            assert _cards(_read(mod))[tid]["title"] == title
            clocks = [sql.upper() for sql in statements if sql.upper().startswith("PRAGMA ")]
            assert clocks == ["PRAGMA SCHEMA_VERSION", "PRAGMA DATA_VERSION"]
    finally:
        obs.conn.set_trace_callback(None)


def test_incremental_clock_is_sampled_after_refresh_custody(env, monkeypatch):
    from contextlib import contextmanager
    from plugins.kanban.dashboard import board_rows

    mod, _sql_reads, _home = env
    tid = _create("first")
    _read(mod)
    [obs] = board_rows._observers.values()

    @contextmanager
    def commit_while_acquiring():
        _raw("UPDATE tasks SET title=? WHERE id=?", ("during wait", tid))
        yield

    monkeypatch.setattr(obs, "refresh", commit_while_acquiring())
    assert _cards(_read(mod))[tid]["title"] == "during wait"


def test_incremental_failed_clock_drops_observer_and_retries(env):
    from plugins.kanban.dashboard import board_rows

    mod, _sql_reads, _home = env
    tid = _create("first")
    _read(mod)
    [obs] = board_rows._observers.values()
    obs.conn.close()
    _raw("UPDATE tasks SET title=? WHERE id=?", ("after failure", tid))
    assert _cards(_read(mod))[tid]["title"] == "after failure"
    assert obs not in board_rows._observers.values()
    assert _cards(_read(mod))[tid]["title"] == "after failure"


def test_local_commit_is_read_on_the_next_read_without_full_materialization(env):
    mod, sql_reads, _home = env
    _create("first")
    _read(mod)
    _read(mod)
    second = _create("second")
    body = _read(mod)
    assert second in _cards(body)
    assert len(sql_reads) == 1
    _read(mod)
    assert len(sql_reads) == 1


def test_external_process_commit_is_read_on_the_next_read_without_full_materialization(env):
    mod, sql_reads, _home = env
    tid = _create("before")
    _read(mod)
    code = ("import sqlite3,sys; c=sqlite3.connect(sys.argv[1]); "
            "c.execute(\"UPDATE tasks SET title='after' WHERE id=?\", (sys.argv[2],)); c.commit(); c.close()")
    subprocess.run([sys.executable, "-c", code, str(kb.kanban_db_path()), tid], check=True)
    body = _read(mod)
    assert _cards(body)[tid]["title"] == "after"
    assert len(sql_reads) == 1


def test_heartbeat_only_write_is_read_on_the_next_read_without_full_materialization(env):
    """Bookkeeping columns (excluded from the activity revision) are in the payload."""
    mod, sql_reads, _home = env
    tid = _create("hb")
    assert _cards(_read(mod))[tid]["last_heartbeat_at"] is None
    _raw("UPDATE tasks SET last_heartbeat_at = 1234567 WHERE id = ?", (tid,))
    assert _cards(_read(mod))[tid]["last_heartbeat_at"] == 1234567
    assert len(sql_reads) == 1


def test_wake_check_only_write_is_read_on_the_next_read_without_full_materialization(env, monkeypatch):
    from plugins.kanban.dashboard import board_read

    mod, sql_reads, _home = env
    tid = _create("parked")
    _read(mod)
    _read(mod)
    assert len(sql_reads) == 1
    _raw("INSERT INTO task_wake_checks (task_id, cmd, every, next_at, fails, generation) "
         "VALUES (?, 'true', 60, 1, 0, 'g')", (tid,))
    seen = []
    real = board_read._wake_states
    monkeypatch.setattr(board_read, "_wake_states", lambda *a: seen.append(real(*a)) or seen[-1])
    _read(mod)
    assert len(seen) == 1 and tid in seen[0]
    assert len(sql_reads) == 1


def test_commit_during_the_row_read_is_read_again(env, monkeypatch):
    """The version is taken before the rows: a commit that lands mid-read moves it."""
    mod, _sql_reads, _home = env
    tid = _create("before")
    real = kb.list_tasks
    once = {"done": False}

    def racing(*a, **k):
        out = real(*a, **k)  # rows read; now another writer commits before the read ends
        if not once["done"]:
            once["done"] = True
            _raw("UPDATE tasks SET title = 'during' WHERE id = ?", (tid,))
        return out

    monkeypatch.setattr(kb, "list_tasks", racing)
    assert _cards(_read(mod))[tid]["title"] == "before"
    assert _cards(_read(mod))[tid]["title"] == "during"


def test_failed_read_is_retried_and_not_cached(env, monkeypatch):
    mod, _sql_reads, _home = env
    tid = _create("first")
    real = kb.list_tasks
    calls = {"n": 0}

    def flaky(*a, **k):
        calls["n"] += 1
        if calls["n"] == 1:
            raise sqlite3.OperationalError("database is locked")
        return real(*a, **k)

    monkeypatch.setattr(kb, "list_tasks", flaky)
    with pytest.raises(sqlite3.OperationalError):
        _read(mod)
    assert tid in _cards(_read(mod))
    _read(mod)
    assert calls["n"] == 2


def test_unobservable_board_reads_every_time_then_recovers(env, monkeypatch):
    from plugins.kanban.dashboard import board_rows

    mod, sql_reads, _home = env
    _create("first")
    real_open = board_rows._open_observer

    def broken(*a, **k):
        raise sqlite3.OperationalError("unable to open database file")

    monkeypatch.setattr(board_rows, "_open_observer", broken)
    _read(mod)
    _read(mod)
    assert len(sql_reads) == 2
    monkeypatch.setattr(board_rows, "_open_observer", real_open)
    _read(mod)
    _read(mod)
    assert len(sql_reads) == 3


def test_observer_read_error_drops_rows_and_reopens(env, monkeypatch):
    """A fresh observer's counter restarts, so rows read under the old one must not match."""
    from plugins.kanban.dashboard import board_rows

    mod, sql_reads, _home = env
    _create("first")
    _read(mod)
    [obs] = board_rows._observers.values()
    obs.conn.close()  # the next PRAGMA raises ProgrammingError
    _read(mod)  # unobservable this call: built uncached
    _read(mod)  # new generation: built and cached
    _read(mod)
    assert len(sql_reads) == 3
    [fresh] = board_rows._observers.values()
    assert fresh.gen != obs.gen


def test_replaced_board_file_is_read_again(env):
    mod, sql_reads, _home = env
    tid = _create("first")
    _read(mod)
    path = kb.kanban_db_path()
    copy = path.with_name("copy.db")
    src = sqlite3.connect(str(path))
    dst = sqlite3.connect(str(copy))
    src.backup(dst)
    dst.execute("UPDATE tasks SET title = 'replaced' WHERE id = ?", (tid,))
    dst.commit()
    src.close()
    dst.close()
    for suffix in ("-wal", "-shm"):
        Path(f"{path}{suffix}").unlink(missing_ok=True)
    os.replace(copy, path)
    assert _cards(_read(mod))[tid]["title"] == "replaced"
    assert len(sql_reads) == 2


def test_time_only_diagnostics_and_ages_refresh_without_sql(env, monkeypatch):
    mod, sql_reads, _home = env
    tid = _create("stale")
    t0 = int(time.time())
    _raw("UPDATE tasks SET status = 'running', claim_lock = ?, claim_expires = ?, started_at = ?, "
         "worker_pid = NULL, last_heartbeat_at = NULL WHERE id = ?",
         (kb._host_prefix() + "dead", t0, t0, tid))
    clock = {"t": float(t0 + 60)}
    monkeypatch.setattr(time, "time", lambda: clock["t"])
    first = _cards(_read(mod))[tid]
    assert not any(d["kind"] == "stale_running" for d in first.get("diagnostics") or [])
    clock["t"] = float(t0 + 3 * 3600)
    later = _cards(_read(mod))[tid]
    assert any(d["kind"] == "stale_running" for d in later.get("diagnostics") or [])
    assert later["age"]["started_age_seconds"] == 3 * 3600
    assert first["age"]["started_age_seconds"] == 60
    assert len(sql_reads) == 1


def test_config_and_profiles_on_disk_refresh_without_sql(env, monkeypatch):
    import hermes_cli.config as cfg

    mod, sql_reads, home = env
    tid = _create("stale")
    t0 = int(time.time()) - 2 * 3600
    _raw("UPDATE tasks SET status = 'running', claim_lock = ?, claim_expires = ?, started_at = ?, "
         "worker_pid = NULL WHERE id = ?", (kb._host_prefix() + "dead", t0, t0, tid))
    first = _read(mod)
    assert any(d["kind"] == "stale_running" for d in _cards(first)[tid].get("diagnostics") or [])
    assert "newbie" not in first["assignees"]
    real_load = cfg.load_config

    def load_config(*a, **k):
        out = dict(real_load(*a, **k))
        out["kanban"] = {**(out.get("kanban") or {}), "diagnostics": {"stale_running_grace_seconds": 10 * 3600}}
        return out

    monkeypatch.setattr(cfg, "load_config", load_config)
    (home / "profiles" / "newbie").mkdir(parents=True)
    (home / "profiles" / "newbie" / "config.yaml").write_text("{}\n")
    later = _read(mod)
    assert "newbie" in later["assignees"]
    assert not any(d["kind"] == "stale_running" for d in _cards(later)[tid].get("diagnostics") or [])
    assert len(sql_reads) == 1


def test_projection_copies_do_not_leak_into_cached_rows(env):
    mod, sql_reads, _home = env
    parent = _create("parent", skills=["a"])
    _create("child", parents=[parent])
    body = _read(mod)
    card = _cards(body)[parent]
    card["skills"].append("mutated")
    card["link_counts"]["children"] = 99
    card["title"] = "mutated"
    body["assignees"].append("ghost")
    body["columns"][0]["tasks"].clear()
    again = _read(mod)
    fresh = _cards(again)[parent]
    assert fresh["skills"] == ["a"] and fresh["link_counts"]["children"] == 1 and fresh["title"] == "parent"
    assert "ghost" not in again["assignees"]
    assert len(sql_reads) == 1


def test_row_cache_and_observers_are_bounded(env):
    from plugins.kanban.dashboard import board_rows

    mod, _sql_reads, _home = env
    _create("first")
    boards = [f"b{i}" for i in range(board_rows.MAX_OBSERVERS + 2)]
    for slug in boards:
        kb.create_board(slug)
    _read(mod)
    first_obs = next(iter(board_rows._observers.values()))
    for slug in boards:
        _read(mod, board=slug)
    for limit in range(1, board_rows.MAX_ENTRIES + 3):
        _read(mod, done_limit=limit)
    stats = board_rows.stats()
    assert stats["observers"] <= board_rows.MAX_OBSERVERS
    assert stats["entries"] <= board_rows.MAX_ENTRIES
    with pytest.raises(sqlite3.ProgrammingError):  # the evicted observer was closed
        first_obs.conn.execute("SELECT 1")
    board_rows.clear()
    assert board_rows.stats() == {"observers": 0, "entries": 0}


def test_idle_observer_is_closed(env, monkeypatch):
    from plugins.kanban.dashboard import board_rows

    mod, _sql_reads, _home = env
    kb.create_board("other")
    _read(mod, board="other")
    [obs] = board_rows._observers.values()
    clock = {"t": time.monotonic() + board_rows.OBSERVER_IDLE_S + 1}
    monkeypatch.setattr(board_rows.time, "monotonic", lambda: clock["t"])
    _read(mod)  # a different board: the idle one is swept
    assert obs.path not in board_rows._observers
    with pytest.raises(sqlite3.ProgrammingError):
        obs.conn.execute("SELECT 1")


def test_observer_never_writes_or_holds_a_snapshot(env):
    from plugins.kanban.dashboard import board_rows

    mod, _sql_reads, _home = env
    _create("first")
    _read(mod)
    [obs] = board_rows._observers.values()
    assert not obs.conn.in_transaction
    with pytest.raises(sqlite3.OperationalError):
        obs.conn.execute("CREATE TABLE nope (x)")
    # A writer can still checkpoint the whole WAL: the observer holds no read snapshot.
    conn = sqlite3.connect(str(kb.kanban_db_path()))
    try:
        busy, _log, _ckpt = conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
    finally:
        conn.close()
    assert busy == 0


def test_each_get_reprojects_unchanged_board_without_sql(env, monkeypatch):
    """Through GET /board: age changes appear without waiting for a snapshot TTL."""
    mod, sql_reads, _home = env
    app = FastAPI()
    app.include_router(mod.router, prefix="/api/plugins/kanban")
    client = TestClient(app)
    tid = _create("first")
    mono = {"t": 1000.0}
    monkeypatch.setattr(mod.time, "monotonic", lambda: mono["t"])
    wall = {"t": float(int(time.time()))}
    monkeypatch.setattr(time, "time", lambda: wall["t"])
    one = _cards(client.get("/api/plugins/kanban/board").json())[tid]["age"]["created_age_seconds"]
    wall["t"] += 600
    two = _cards(client.get("/api/plugins/kanban/board").json())[tid]["age"]["created_age_seconds"]
    assert two == one + 600
    assert len(sql_reads) == 1


def test_board_payload_matches_a_cold_read(env, monkeypatch):
    """A projection over retained rows equals a read with no retained rows."""
    mod, _sql_reads, _home = env
    parent = _create("parent", skills=["a"])
    _create("child", parents=[parent])
    conn = kbc.connect()
    try:
        kb.add_comment(conn, parent, "me", "hello")
    finally:
        conn.close()
    _freeze_wall(monkeypatch)
    _read(mod)
    warm = _read(mod)
    _clear_rows()
    cold = _read(mod)
    assert _strip_now(warm) == _strip_now(cold)


# --- WAL generation safety (kanban_generation) ------------------------------------------------
# A retained observer must never pin a deleted -wal/-shm generation: core recover() refuses every
# board connect while any handle in the process still maps one (kanban-759104eb56).

def _lose_sidecars(db, title, tid):
    """An external process checkpointed and unlinked the board's sidecars (a sibling's last close, after a
    stray close dropped this process's POSIX locks), then another process committed on a fresh generation."""
    from contextlib import closing

    with closing(sqlite3.connect(str(db))) as conn:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
    for suffix in ("-wal", "-shm"):
        os.unlink(str(db) + suffix)
    subprocess.run(
        [sys.executable, "-c", "import sqlite3,sys; c=sqlite3.connect(sys.argv[1]); "
         "c.execute('UPDATE tasks SET title=? WHERE id=?', (sys.argv[2], sys.argv[3])); c.commit(); c.close()",
         str(db), title, tid],
        check=True, timeout=30,
    )


@pytest.mark.platforms("linux")
def test_sidecar_loss_then_core_connect_recovers(env):
    from contextlib import closing

    from hermes_cli import kanban_lockguard
    from plugins.kanban.dashboard import board_rows

    mod, _sql_reads, _home = env
    tid = _create("first")
    _read(mod)
    db = kb.kanban_db_path()
    assert board_rows.stats()["observers"] == 1
    _lose_sidecars(db, "renamed", tid)
    assert kanban_lockguard.stranded_sidecars(db)
    with closing(kanban_lockguard.connect(db)) as conn:
        assert conn.execute("SELECT title FROM tasks WHERE id = ?", (tid,)).fetchone()[0] == "renamed"
    assert not kanban_lockguard.stranded_sidecars(db)
    assert board_rows.stats()["observers"] == 0  # evicted by recover(), not merely bypassed
    assert _cards(_read(mod))[tid]["title"] == "renamed"
    assert board_rows.stats()["observers"] == 1


@pytest.mark.platforms("linux")
def test_continuously_polled_board_reopens_after_sidecar_loss(env):
    from hermes_cli import kanban_lockguard
    from plugins.kanban.dashboard import board_rows

    mod, sql_reads, _home = env
    tid = _create("first")
    _read(mod)
    _read(mod)
    db = kb.kanban_db_path()
    _lose_sidecars(db, "renamed", tid)
    # No idle gap and no explicit core connect first: the next poll itself must notice.
    before = len(sql_reads)
    assert _cards(_read(mod))[tid]["title"] == "renamed"
    assert len(sql_reads) == before + 1
    assert not kanban_lockguard.stranded_sidecars(db)
    [obs] = board_rows._observers.values()
    assert obs.shm == kanban_lockguard.shm_identity(db)


def test_observer_is_a_registered_guarded_handle(env):
    from hermes_cli import kanban_generation, kanban_lockguard
    from plugins.kanban.dashboard import board_rows

    mod, _sql_reads, _home = env
    _create("first")
    _read(mod)
    [obs] = board_rows._observers.values()
    assert isinstance(obs.conn, kanban_lockguard._GuardMixin)
    assert obs.conn in set(kanban_generation._connections)
    assert board_rows.evict in kanban_generation._reader_caches


@pytest.mark.platforms("linux")
def test_recovery_refuses_while_observer_is_borrowed_then_succeeds(env):
    import threading
    from contextlib import closing

    from hermes_cli import kanban_lockguard
    from hermes_cli.kanban_generation import DeletedWalGenerationError
    from plugins.kanban.dashboard import board_rows

    mod, _sql_reads, _home = env
    tid = _create("first")
    _read(mod)
    db = kb.kanban_db_path()
    [obs] = board_rows._observers.values()
    _lose_sidecars(db, "renamed", tid)
    result = {}
    with obs.lock:  # a poll is mid-PRAGMA on another thread
        worker = threading.Thread(target=lambda: result.setdefault("r", _try_connect(kanban_lockguard, db)))
        worker.start()
        worker.join(10)
        assert not worker.is_alive(), "recover() must not wait on a borrowed observer (deadlock risk)"
    assert isinstance(result["r"], DeletedWalGenerationError)
    obs.conn.execute("SELECT 1")  # never closed under its borrower
    with closing(kanban_lockguard.connect(db)) as conn:  # the retry succeeds once it is returned
        assert conn.execute("SELECT title FROM tasks WHERE id = ?", (tid,)).fetchone()[0] == "renamed"
    with pytest.raises(sqlite3.ProgrammingError):
        obs.conn.execute("SELECT 1")


def _try_connect(lockguard, db):
    try:
        lockguard.connect(db).close()
        return "ok"
    except Exception as exc:  # returned to the asserting thread
        return exc


def test_drop_waits_for_the_borrower_before_closing(env):
    import threading

    from plugins.kanban.dashboard import board_rows

    mod, _sql_reads, _home = env
    _create("first")
    _read(mod)
    [obs] = board_rows._observers.values()
    obs.lock.acquire()
    try:
        dropper = threading.Thread(target=board_rows.clear)
        dropper.start()
        dropper.join(0.3)
        assert dropper.is_alive()  # waits for the borrower ...
        assert board_rows.stats()["observers"] == 0  # ... after unpublishing it
        obs.conn.execute("PRAGMA data_version").fetchone()  # still usable by the borrower
    finally:
        obs.lock.release()
    dropper.join(10)
    assert not dropper.is_alive()
    with pytest.raises(sqlite3.ProgrammingError):
        obs.conn.execute("SELECT 1")


_LOCK_ORDER_PROBE = r'''
import os, sys, threading, time
from pathlib import Path
from hermes_cli import kanban_db as kb, kanban_lockguard as lg, kanban_generation as gen
from hermes_cli.kanban_sync_connections import _Pool
from plugins.kanban.dashboard import board_rows

kb.init_db()
path = str(kb.kanban_db_path())
assert board_rows.version(path) is not None
y_in_recover, x_started = threading.Event(), threading.Event()

def fake_stranded(p):
    if threading.current_thread().name == "Y" and not y_in_recover.is_set():
        y_in_recover.set()        # Y holds the board lifecycle lock inside recover()
        x_started.wait(2)
        time.sleep(0.2)           # let X enter board_rows.evict first
    return [p + "-shm"]           # a deleted WAL generation is stranded
lg.stranded_sidecars = fake_stranded
gen._poisoned.add(os.path.realpath(path))

def X():
    y_in_recover.wait(2); x_started.set()
    try:
        _Pool(2).take(Path(path), lambda: lg.connect(path))
    except Exception:
        pass

def Y():
    try:
        lg.connect(path, timeout=2.0).close()
    except Exception:
        pass

ts = [threading.Thread(target=f, name=f.__name__, daemon=True) for f in (Y, X)]
for t in ts: t.start()
for t in ts: t.join(8)
alive = [t.name for t in ts if t.is_alive()]
print("ALIVE", alive, "LOCKED", board_rows._lock.locked())
os._exit(3 if alive else 0)
'''


def test_evict_never_closes_under_module_lock_against_lifecycle_lock(env, tmp_path):
    """recover() without the lifecycle lock (sync pool) racing a locked connect() must not
    deadlock: evict unpublishes under ``_lock`` and closes after releasing it."""
    _mod, _reads, home = env
    repo_root = Path(__file__).resolve().parents[2]
    proc = subprocess.run(
        [sys.executable, "-c", _LOCK_ORDER_PROBE],
        cwd=repo_root, capture_output=True, text=True, timeout=60, check=False,
        env={**os.environ, "HERMES_HOME": str(home), "HOME": str(tmp_path),
             "PYTHONPATH": os.pathsep.join([str(repo_root), os.environ.get("PYTHONPATH", "")])},
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
