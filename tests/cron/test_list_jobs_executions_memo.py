"""list_jobs reads latest/live executions through a kept reader memoized on the ledger's PRAGMA data_version.

Card t_01a1d6c7f670c9d8 (design t_01a1d563324d7e8e §6, serve residue): after the jobs.json memo, the Desktop
cron poll still opened executions.db twice per profile per list (connect + WAL pragma + schema initialize).
An unchanged ledger must now cost no open, and a start, a finish or an owner's death must still show on the
very next list. [daveey]
"""
import multiprocessing
import shutil
import sqlite3

import pytest

import cron.executions as ce
import cron.jobs as cj


@pytest.fixture()
def ledger(tmp_path, monkeypatch):
    monkeypatch.setattr("cron.jobs.CRON_DIR", tmp_path / "cron")
    monkeypatch.setattr("cron.jobs.JOBS_FILE", tmp_path / "cron" / "jobs.json")
    monkeypatch.setattr("cron.jobs.OUTPUT_DIR", tmp_path / "cron" / "output")
    monkeypatch.setattr(ce, "EXECUTIONS_FILE", tmp_path / "cron" / "executions.db")
    yield tmp_path / "cron" / "executions.db"
    views = getattr(ce, "_VIEWS", {})  # absent on the pre-change base (RED run)
    for key in [k for k in views if k.startswith(str(tmp_path))]:
        ce._drop_view(key)


def _listed(job_id):
    return next(j for j in cj.list_jobs(include_disabled=True) if j["id"] == job_id)


def _counting_connects(monkeypatch):
    opened = []
    real = sqlite3.connect
    monkeypatch.setattr(ce.sqlite3, "connect", lambda *a, **k: opened.append(a[0] if a else k) or real(*a, **k))
    return opened


def test_an_unchanged_ledger_is_not_opened_again(ledger, monkeypatch):
    job = cj.create_job(prompt="first", schedule="every 1h", name="one")
    run = ce.create_execution(job["id"], source="builtin")
    ce.finish_execution(run["id"], success=True)
    assert _listed(job["id"])["latest_execution"]["status"] == "completed"  # warm
    opened = _counting_connects(monkeypatch)
    for _ in range(3):
        assert _listed(job["id"])["latest_execution"]["id"] == run["id"]
    assert opened == []


def test_a_start_and_a_finish_show_on_the_next_list(ledger):
    job = cj.create_job(prompt="first", schedule="every 1h", name="one")
    assert _listed(job["id"])["latest_execution"] is None
    assert _listed(job["id"])["live_execution"] is None

    run = ce.create_execution(job["id"], source="builtin")
    listed = _listed(job["id"])
    assert listed["latest_execution"]["id"] == run["id"]
    assert listed["latest_execution"]["status"] == "claimed"
    assert listed["live_execution"]["id"] == run["id"]

    ce.mark_execution_running(run["id"])
    assert _listed(job["id"])["live_execution"]["status"] == "running"

    ce.finish_execution(run["id"], success=False, error="boom")
    listed = _listed(job["id"])
    assert listed["latest_execution"]["status"] == "failed"
    assert listed["latest_execution"]["error"] == "boom"
    assert listed["live_execution"] is None

    second = ce.create_execution(job["id"], source="builtin")
    assert _listed(job["id"])["latest_execution"]["id"] == second["id"]


def test_a_write_by_a_foreign_connection_shows_on_the_next_list(ledger):
    job = cj.create_job(prompt="first", schedule="every 1h", name="one")
    run = ce.create_execution(job["id"], source="builtin")
    assert _listed(job["id"])["live_execution"]["id"] == run["id"]
    conn = sqlite3.connect(ledger)
    try:
        with conn:
            conn.execute("UPDATE executions SET status='unknown' WHERE id=?", (run["id"],))
    finally:
        conn.close()
    listed = _listed(job["id"])
    assert listed["latest_execution"]["status"] == "unknown"
    assert listed["live_execution"] is None


def test_an_owner_death_shows_on_the_next_list_without_a_ledger_write(ledger, monkeypatch):
    job = cj.create_job(prompt="first", schedule="every 1h", name="one")
    run = ce.create_execution(job["id"], source="builtin")
    monkeypatch.setattr(ce, "_PROCESS_ID", "another-gateway")  # the row's owner is now a foreign process
    alive = {"live": True}
    monkeypatch.setattr(ce, "_owner_is_live", lambda _pid, _started: alive["live"])
    assert _listed(job["id"])["live_execution"]["id"] == run["id"]
    alive["live"] = False
    listed = _listed(job["id"])
    assert listed["live_execution"] is None
    assert listed["latest_execution"]["status"] == "claimed"


def test_a_replaced_ledger_file_is_reopened(ledger, tmp_path):
    job = cj.create_job(prompt="first", schedule="every 1h", name="one")
    first = ce.create_execution(job["id"], source="builtin")
    ce.finish_execution(first["id"], success=True)
    snapshot = tmp_path / "snapshot.db"
    shutil.copy(ledger, snapshot)  # a checkpointed copy holding only the first run
    second = ce.create_execution(job["id"], source="builtin")
    assert _listed(job["id"])["latest_execution"]["id"] == second["id"]
    for suffix in ("-wal", "-shm"):
        ledger.with_name(ledger.name + suffix).unlink(missing_ok=True)
    snapshot.replace(ledger)  # a restore: new inode
    assert _listed(job["id"])["latest_execution"]["id"] == first["id"]


def test_callers_mutating_rows_do_not_change_the_next_answer(ledger):
    job = cj.create_job(prompt="first", schedule="every 1h", name="one")
    run = ce.create_execution(job["id"], source="builtin")
    listed = _listed(job["id"])
    listed["latest_execution"]["status"] = "mutated"
    listed["live_execution"]["status"] = "mutated"
    again = _listed(job["id"])
    assert again["latest_execution"]["status"] == "claimed"
    assert again["live_execution"]["status"] == "claimed"
    assert again["live_execution"]["id"] == run["id"]


def test_listed_executions_matches_the_exact_reads(ledger):
    jobs = [cj.create_job(prompt=f"p{i}", schedule="every 1h", name=f"j{i}") for i in range(3)]
    ids = [j["id"] for j in jobs]
    a = ce.create_execution(ids[0], source="builtin")
    ce.finish_execution(a["id"], success=True)
    ce.create_execution(ids[0], source="builtin")
    ce.create_execution(ids[1], source="builtin")
    ce.create_execution(ids[2], source="builtin")  # excluded job has both latest and live rows
    assert ce.listed_executions(ids[:2]) == (ce.latest_executions(ids[:2]), ce.live_executions(ids[:2]))
    assert ce.listed_executions(ids) == (ce.latest_executions(ids), ce.live_executions(ids))
    assert ce.listed_executions([]) == ({}, {})


def test_multiple_live_attempts_choose_newest_on_cold_and_warm_reads(ledger):
    older = ce.create_execution("job", source="builtin")
    newer = ce.create_execution("job", source="builtin")
    # Explicit instants avoid clock resolution and UUID tie-breaking deciding the winner.
    with ce._transaction() as conn:
        conn.execute("UPDATE executions SET claimed_at=? WHERE id=?",
                     ("2026-01-01T00:00:00+00:00", older["id"]))
        conn.execute("UPDATE executions SET claimed_at=? WHERE id=?",
                     ("2026-01-02T00:00:00+00:00", newer["id"]))
    expected = ce.live_executions(["job"])
    assert expected["job"]["id"] == newer["id"]
    for _ in range(2):
        latest, live = ce.listed_executions(["job"])
        assert latest["job"]["id"] == newer["id"]
        assert live == expected


def test_forked_child_does_not_query_inherited_reader(ledger):
    run = ce.create_execution("job", source="builtin")
    expected = ce.listed_executions(["job"])
    inherited = ce._VIEWS[str(ledger)]["conn"]
    queries = []
    inherited.set_trace_callback(queries.append)
    ctx = multiprocessing.get_context("fork")
    receive, send = ctx.Pipe(duplex=False)

    def read_in_child():
        result = ce.listed_executions(["job"])
        send.send((result, queries, ce._VIEWS[str(ledger)]["conn"] is inherited))
        send.close()

    child = ctx.Process(target=read_in_child)
    try:
        child.start()
        send.close()
        assert receive.poll(10), "child did not return its ledger read"
        result, child_queries, reused = receive.recv()
        child.join(10)
        assert child.exitcode == 0
        assert result == expected
        assert child_queries == []
        assert not reused
        # The parent's handle and populated view remain usable after the child exits.
        assert inherited.execute("SELECT id FROM executions").fetchone()[0] == run["id"]
        assert ce.listed_executions(["job"]) == expected
    finally:
        if child.is_alive():
            child.terminate()
            child.join(10)
        receive.close()
        send.close()
        inherited.set_trace_callback(None)


def test_changed_shm_identity_reopens_reader_with_unchanged_database(ledger, monkeypatch):
    ce.create_execution("job", source="builtin")
    expected = ce.listed_executions(["job"])
    key = str(ledger)
    old = ce._VIEWS[key]
    ident = ce._file_ident
    db_ident = ident(key)
    assert old["shm"] is not None
    assert ce.listed_executions(["job"]) == expected  # settled memo-hit control
    assert ce._VIEWS[key] is old
    # Model only the stat boundary: never unlink an mmap-backed live SQLite WAL index.
    changed_shm = (old["shm"][0], old["shm"][1] + 1)
    monkeypatch.setattr(ce, "_file_ident", lambda path: changed_shm if path == key + "-shm" else ident(path))
    assert ce._file_ident(key) == db_ident
    assert ce.listed_executions(["job"]) == expected
    assert ce._VIEWS[key]["conn"] is not old["conn"]
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        old["conn"].execute("SELECT 1")
    replacement = ce._VIEWS[key]["conn"]
    assert ce.listed_executions(["job"]) == expected
    assert ce._VIEWS[key]["conn"] is replacement


def test_no_ledger_file_yet_takes_the_exact_path(ledger):
    job = cj.create_job(prompt="first", schedule="every 1h", name="one")
    assert not ledger.exists()
    assert _listed(job["id"])["latest_execution"] is None
    assert ledger.exists()  # the exact path creates it, as before


def test_memo_hits_are_p1_skips(ledger):
    from hermes_cli import loop_accounting as la

    job = cj.create_job(prompt="first", schedule="every 1h", name="one")
    ce.create_execution(job["id"], source="builtin")
    before = la.counters().get("cron.executions", {"runs": 0, "skips": 0, "changes": 0})
    for _ in range(3):
        cj.list_jobs(include_disabled=True)
    ce.create_execution(job["id"], source="builtin")
    cj.list_jobs(include_disabled=True)
    after = la.counters()["cron.executions"]
    assert after["runs"] - before["runs"] == 4
    assert after["skips"] - before["skips"] == 2
    assert after["changes"] - before["changes"] == 2
