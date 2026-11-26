"""Fresh evidence of real preclaim storage aborts, not replayed card decisions."""
from contextlib import closing
import json
import sqlite3

from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_dispatch_owner as owner
from hermes_cli import kanban_governors as governors
from hermes_cli import kanban_why_fast as fast
from hermes_cli.kanban_db_dispatch import DispatchResult
import pytest


@pytest.mark.parametrize("entry", ["owner", "dispatcher"])
def test_real_storage_abort_publishes_current_candidate_without_writer_reentry(tmp_path, monkeypatch, entry):
    from hermes_cli import kanban_model_retirement, kanban_ratio_routing
    from hermes_cli import kanban_dispatch_storage as storage
    from hermes_cli import kanban_db_dispatch as dispatch
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(home))
    monkeypatch.delenv("HERMES_KANBAN_DB", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)
    monkeypatch.setattr(kanban_model_retirement, "sweep", lambda conn: None)
    monkeypatch.setattr(kanban_ratio_routing, "begin_tick", lambda conn: None)
    monkeypatch.setattr(storage, "WRITE_WAIT_SECONDS", .02)
    monkeypatch.setattr(dispatch, "review_dispatch_enabled", lambda: True)
    # Retained batch018 why04: the last genuine completion had expired.
    last_completion, at = 1795678384.5697815, 1795678493.0533526
    monkeypatch.setattr(governors.time, "time", lambda: at)
    path = home / "kanban.db"
    sidecar = path.with_name(path.name + fast.SUFFIX)
    old = {"version": 1, "board": "default", "tick_at": last_completion,
           "freshness_bound_s": 102.78792333602905,
           "cards": {"gone": {"governor": "max_spawn", "status": "ready"}},
           "caps": {"max_spawn": 0}, "governors": [], "tick_completions": [last_completion]}
    with closing(kbc.connect(path)) as conn:
        conn.executemany("INSERT INTO tasks(id, status, claim_lock, created_at, title) VALUES (?, ?, ?, 1, 'fixture')", [
            ("candidate", "ready", None), ("reviewer", "review", None),
            ("claimed", "ready", "lease"), ("gated", "triage", None)])
        conn.commit()
        governors._write(sidecar, old)
        assert fast.read_answer(path, "candidate", now=at) is None
        writes = []
        conn.set_trace_callback(writes.append)
        monkeypatch.setattr(kbc, "_maybe_checkpoint_wal", lambda *a: (_ for _ in ()).throw(AssertionError("checkpoint")))
        with closing(sqlite3.connect(path, isolation_level=None)) as blocker:
            blocker.execute("BEGIN IMMEDIATE")
            try:
                calls = []
                def tick():
                    calls.append(True)
                    kbc._execute_boundary_with_retry(conn, "BEGIN IMMEDIATE")
                    return DispatchResult()
                if entry == "dispatcher":
                    monkeypatch.setattr(kanban_model_retirement, "sweep", lambda conn: tick())
                    result = dispatch.dispatch_once(conn, board="default", spawn_fn=lambda *a: pytest.fail("spawn"))
                else:
                    result = owner.run_owned_tick(conn, tick, None, "default", False)
                assert result.storage_skip_reason == storage.REASON
                assert calls == [True]
                doc = json.loads(sidecar.read_text())
                assert doc["tick_at"] == at
                assert doc["phase"] == "preclaim_aborted"
                assert set(doc["cards"]) == {"candidate", "reviewer"}
                assert doc["cards"]["candidate"]["governor"] == "storage_admission"
                assert doc["cards"]["candidate"]["overlaps"] is None
                assert doc["caps"] == {}
                assert doc["coverage"]["excluded_statuses"] == ["triage"]
                answer = fast.read_answer(path, "candidate", now=at)
                assert answer is not None
                assert answer["source"] == "snapshot"
                assert answer["phase"] == "preclaim_aborted"
                assert fast.read_answer(path, "gated", now=at) is None
                assert fast.read_answer(path, "candidate", now=at + 601) is None
                assert not any(sql.upper().startswith(("INSERT", "UPDATE", "DELETE", "ROLLBACK")) for sql in writes)
                assert not conn.in_transaction
            finally:
                blocker.rollback()


def test_census_never_enters_lifecycle_recovery(tmp_path, monkeypatch):
    from hermes_cli import kanban_generation as generation
    from hermes_cli import kanban_governor_storage as observation
    path = tmp_path / "kanban.db"
    with closing(kbc.connect(path)) as conn:
        conn.execute("INSERT INTO tasks(id, status, created_at, title) VALUES ('candidate', 'ready', 1, 'fixture')")
        conn.commit()
        def forbidden(*args):
            pytest.fail("census must not enter lifecycle recovery")
        monkeypatch.setattr(generation, "recover", forbidden)
        rows, counts = observation._census(path, False, 5)
        assert [tuple(row) for row in rows] == [("candidate", "ready")]
        assert counts == {"ready": 1}


@pytest.mark.parametrize("failure", [sqlite3.OperationalError("locked"), TimeoutError("deadline")])
def test_failed_census_publishes_unknown_without_old_cards(tmp_path, monkeypatch, failure):
    from hermes_cli import kanban_governor_storage as observation
    with closing(kbc.connect(tmp_path / "kanban.db")) as conn:
        def failed(*args):
            raise failure
        monkeypatch.setattr(observation, "_census", failed)
        doc = observation.record(conn, board="default")
        assert doc is not None
        assert doc["cards"] == {}
        assert doc["idle_total"] is None
        assert doc["charged_total"] is None
        assert doc["coverage"]["status_counts"] is None
        assert all(row["active"] is None and row["cards"] is None for row in doc["governors"]
                   if row["governor"] != "storage_admission")


def test_dry_run_does_not_publish(tmp_path):
    from hermes_cli import kanban_governor_storage as observation
    path = tmp_path / "kanban.db"
    with closing(kbc.connect(path)) as conn:
        assert observation.record(conn, board="default", dry_run=True) is None
        assert not path.with_name(path.name + fast.SUFFIX).exists()


def test_identity_replacement_during_census_cannot_publish(tmp_path, monkeypatch):
    from hermes_cli import kanban_governor_storage as observation
    path = tmp_path / "kanban.db"
    with closing(kbc.connect(path)) as conn:
        def replace(*args):
            path.rename(tmp_path / "original.db")
            path.touch()
            return [], {}
        monkeypatch.setattr(observation, "_census", replace)
        assert observation.record(conn, board="default") is None
        assert not path.with_name(path.name + fast.SUFFIX).exists()


def test_read_census_is_bounded_and_excludes_disabled_review(tmp_path):
    from hermes_cli import kanban_governor_storage as observation
    path = tmp_path / "kanban.db"
    with closing(kbc.connect(path)) as conn:
        conn.executemany("INSERT INTO tasks(id, status, created_at, title) VALUES (?, ?, 1, 'fixture')", [
            ("a", "ready"), ("b", "ready"), ("c", "review")])
        conn.commit()
        rows, counts = observation._census(path, False, 1)
        assert [tuple(row) for row in rows] == [("a", "ready"), ("b", "ready")]
        assert counts == {"ready": 2, "review": 1}


def test_expired_query_budget_discards_census(tmp_path, monkeypatch):
    from hermes_cli import kanban_governor_storage as observation
    path = tmp_path / "kanban.db"
    with closing(kbc.connect(path)):
        times = iter([0., 1.])
        monkeypatch.setattr(observation.time, "monotonic", lambda: next(times, 1.))
        with pytest.raises((TimeoutError, sqlite3.OperationalError)):
            observation._census(path, False, 1)


def test_observation_does_not_rollback_callers_transaction(tmp_path):
    from hermes_cli import kanban_governor_storage as observation
    with closing(kbc.connect(tmp_path / "kanban.db")) as conn:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("INSERT INTO tasks(id, status, created_at, title) VALUES ('uncommitted', 'ready', 1, 'fixture')")
        try:
            doc = observation.record(conn, board="default")
            assert doc is not None and doc["cards"] == {}
            assert conn.in_transaction
            assert conn.execute("SELECT count(*) FROM tasks WHERE id='uncommitted'").fetchone()[0] == 1
        finally:
            conn.rollback()


def test_publication_error_does_not_escape_or_change_database(tmp_path, monkeypatch):
    from hermes_cli import kanban_governor_storage as observation
    with closing(kbc.connect(tmp_path / "kanban.db")) as conn:
        def failed(*args):
            raise OSError("read-only evidence directory")
        monkeypatch.setattr(governors, "_write", failed)
        before = conn.total_changes
        assert observation.record(conn, board="default") is None
        assert conn.total_changes == before


def test_census_rows_and_counts_share_one_read_snapshot(tmp_path, monkeypatch):
    from hermes_cli import kanban_governor_storage as observation, sqlite_safe_read
    path = tmp_path / "kanban.db"
    with closing(kbc.connect(path)) as conn:
        conn.execute("INSERT INTO tasks(id, status, created_at, title) VALUES ('candidate', 'ready', 1, 'fixture')")
        conn.commit()
        real_open = sqlite_safe_read.connect_tracked
        def opened(*args, **kwargs):
            reader = real_open(*args, **kwargs)
            class Interleaved:
                def __getattr__(self, name):
                    return getattr(reader, name)
                def execute(self, sql, *parameters):
                    if sql.startswith("SELECT status, count"):
                        conn.execute("UPDATE tasks SET status='triage' WHERE id='candidate'")
                        conn.commit()
                    return reader.execute(sql, *parameters)
            return Interleaved()
        monkeypatch.setattr(sqlite_safe_read, "connect_tracked", opened)
        rows, counts = observation._census(path, False, 10)
        assert [tuple(row) for row in rows] == [("candidate", "ready")]
        assert counts == {"ready": 1}


def test_census_board_identity_comes_from_validated_database(tmp_path, monkeypatch):
    from hermes_cli import kanban_governor_storage as observation
    with closing(kbc.connect(tmp_path / "kanban.db")) as conn:
        monkeypatch.setattr(governors, "_board", lambda conn: "actual-board")
        doc = observation.record(conn, board="caller-hint")
        assert doc is not None and doc["board"] == "actual-board"
