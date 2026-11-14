"""Bounded terminal history, preserving durable retries and FIFO custody."""
import json
import sqlite3
import time

import pytest

from scripts.ci import flaky_intake as intake
from scripts.ci import flaky_kanban as fk
from tests.ci.test_flaky_intake import Board


def test_scheduling_cleanup_scales_with_retained_history(tmp_path):
    """Count VM work, not wall time: correlated key scans must stay indexed."""
    costs = []
    for size in (500, 2000):
        journal = intake.Journal(tmp_path / f"queue-{size}.db")
        now = time.time()
        with journal.db:
            journal.db.executemany(
                "INSERT INTO occurrences(id,repo,identity,day,target_day,metadata,delivered,retained_at) "
                "VALUES (?,'o/r',?,'2026-01-01','2026-01-01','{}',?,?)",
                ((i, str(i), int(i != size), now) for i in range(1, size + 1)))
            journal.db.executemany(
                "INSERT INTO pages(id,repo,day,payload,delivered,retained_at) "
                "VALUES (?,'o/r','2026-01-01','{}',?,?)",
                ((i, int(i != size), now) for i in range(1, size + 1)))
            journal.db.executemany(
                "INSERT INTO runs(repo,key,descriptor,complete,retained_at) VALUES ('o/r',?,'{}',?,?)",
                ((f"{i}/1", int(i != size), now) for i in range(1, size + 1)))
            for kind in ('occurrence', 'page', 'run'):
                journal.db.executemany(
                    "INSERT INTO scheduling VALUES ('o/r',?,?,?)",
                    ((kind, f"{i}/1" if kind == 'run' else str(i), i) for i in range(1, size + 1)))
        steps = [0]
        def count_steps():
            steps[0] += 1
            return 0
        journal.db.set_progress_handler(count_steps, 1000)
        start = time.monotonic()
        journal.prune('o/r')
        elapsed = time.monotonic() - start
        journal.db.set_progress_handler(None, 0)
        costs.append(steps[0])
        print(f"retained={size} vm_batches={steps[0]} elapsed={elapsed:.4f}s")
        assert [tuple(r) for r in journal.db.execute(
            "SELECT kind,item,ticket FROM scheduling ORDER BY kind")] == [
                ('occurrence', str(size), size), ('page', str(size), size), ('run', f'{size}/1', size)]
        journal.close()
    assert costs[1] < costs[0] * 6  # 4x rows: linear work, not 16x scans.



def test_drain_packs_consecutive_rows_and_reads_back(tmp_path):
    journal = intake.Journal(tmp_path / "queue.db")
    journal.record("o/r", [(9, [{"test_id": f"tests/a.py::test_{i}"} for i in range(40)])],
                   {"id": 1, "head_sha": "a" * 40})
    board = Board()
    fk.drain_intake(journal, "o/r", board, {"cards": {}}, None)
    comments = board.notes["card0"]
    print(f"40 occurrences: before=40 comments after={len(comments)}")
    assert len(comments) == 3
    assert all(len(c.encode()) <= 6000 for c in comments)
    assert journal.db.execute("SELECT sum(delivered) FROM occurrences").fetchone()[0] == 40
    fk.drain_intake(journal, "o/r", board, {"cards": {}}, None)
    assert board.notes["card0"] == comments
    journal.close()


def test_oversized_identity_does_not_poison_valid_batch(tmp_path, monkeypatch):
    monkeypatch.setattr(fk, "file_cards", lambda *args, **kwargs: ["queued"])
    journal = intake.Journal(tmp_path / "queue.db")
    # Diagnostics are now bounded. A smaller test-only cap and valid long ID
    # exercise size rejection without bypassing metadata validation.
    monkeypatch.setattr(intake, "PAGE_BYTES", 700)
    journal.record("o/r", [(9, [{"test_id": f"tests/a.py::test_{s}"} for s in ("good", "x" * 480, "last")])],
                   {"id": 1, "head_sha": "a" * 40})
    lines = fk.drain_intake(journal, "o/r", Board(), {"cards": {}}, None)
    assert any("intake_page_oversize" in line for line in lines)
    assert [r[0] for r in journal.db.execute("SELECT delivered FROM occurrences ORDER BY id")] == [1, 0, 1]
    journal.close()


def test_slow_later_owner_cannot_starve_admitted_prefix(tmp_path, monkeypatch):
    clock = [0.0]
    monkeypatch.setattr(fk.time, "monotonic", lambda: clock[0])
    original = fk.file_cards
    def resolve(entries, **kwargs):
        if entries[0]["test_id"].endswith("test_slow"):
            clock[0] += 20
            raise RuntimeError("slow unavailable owner")
        return original(entries, **kwargs)
    monkeypatch.setattr(fk, "file_cards", resolve)
    path = tmp_path / "queue.db"
    journal = intake.Journal(path)
    journal.record("o/r", [(9, [{"test_id": f"tests/a.py::test_{s}"} for s in ("good", "slow")])],
                   {"id": 1, "head_sha": "a" * 40})
    journal.close()
    board = Board()
    for _ in range(3):
        journal = intake.Journal(path)
        fk.drain_intake(journal, "o/r", board, {"cards": {}}, None, deadline=clock[0] + 10)
        journal.close()
    journal = intake.Journal(path)
    assert [r[0] for r in journal.db.execute("SELECT delivered FROM occurrences ORDER BY id")] == [1, 0]
    assert len(board.notes["card0"]) == 1
    journal.close()


@pytest.mark.parametrize("age,remaining", [(30 * 86400, 1), (30 * 86400 + 21600, 1),
                                          (30 * 86400 + 21601, 0)])
def test_retention_boundary_and_foreign_repo(tmp_path, monkeypatch, age, remaining):
    clock = [1000000.0]
    monkeypatch.setattr(intake.time, "time", lambda: clock[0])
    journal = intake.Journal(tmp_path / "queue.db")
    for repo in ("o/r", "other/repo"):
        journal.remember_runs(repo, [{"id": 1, "head_sha": "a" * 40}])
        journal.finish_run(repo, "1/1")
    clock[0] += age
    journal.prune("o/r")
    assert journal.db.execute("SELECT count(*) FROM runs WHERE repo='o/r'").fetchone()[0] == remaining
    assert journal.db.execute("SELECT count(*) FROM runs WHERE repo='other/repo'").fetchone()[0] == 1
    journal.close()


def test_legacy_history_gets_full_retention_window(tmp_path, monkeypatch):
    path = tmp_path / "queue.db"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE runs(repo TEXT,key TEXT,descriptor TEXT,complete INTEGER,PRIMARY KEY(repo,key))")
        db.execute("INSERT INTO runs VALUES ('o/r','1/1',?,1)", (json.dumps({"id": 1, "head_sha": "a" * 40}),))
    monkeypatch.setattr(intake.time, "time", lambda: 9999999999.0)
    journal = intake.Journal(path)
    journal.prune("o/r")
    assert journal.db.execute("SELECT count(*) FROM runs").fetchone()[0] == 1
    journal.close()


def test_retention_preserves_pending_and_recent_history(tmp_path, monkeypatch):
    clock = [1000000.0]
    monkeypatch.setattr(intake.time, "time", lambda: clock[0])
    journal = intake.Journal(tmp_path / "queue.db")
    runs = [{"id": i, "head_sha": "a" * 40} for i in range(1, 5)]
    journal.remember_runs("o/r", runs + [{"id": 5, "head_sha": "b" * 40}])
    for run in runs:
        journal.record("o/r", [(9, [{"test_id": "tests/a.py::test_a"}])], run)
    rows = journal.pending("o/r")
    journal.plan_pages("o/r", [rows[0]])
    journal.deliver("o/r", Board())
    journal.acknowledge(rows[1]["id"], "owner")
    journal.plan_pages("o/r", [rows[2]])  # pending page must survive
    journal.finish_run("o/r", "1/1")
    journal.finish_run("o/r", "2/1")
    journal.finish_run("o/r", "3/1")  # discovery complete != delivery complete
    clock[0] += 32 * 86400
    journal.remember_runs("o/r", [runs[1]])  # still listed: preserve replay evidence
    tickets = dict(journal.db.execute("SELECT item,ticket FROM scheduling WHERE kind='occurrence'"))
    journal.prune("o/r", lookback_hours=6)
    assert [r[0] for r in journal.db.execute("SELECT key FROM runs ORDER BY key")] == ["2/1", "3/1", "4/1", "5/1"]
    assert [r[0] for r in journal.db.execute("SELECT id FROM occurrences ORDER BY id")] == [2, 3, 4]
    assert journal.db.execute("SELECT count(*) FROM pages WHERE delivered=0").fetchone()[0] == 1
    assert dict(journal.db.execute("SELECT item,ticket FROM scheduling WHERE kind='occurrence'")) == {
        str(r["id"]): tickets[str(r["id"])] for r in rows[2:]}
    journal.close()
