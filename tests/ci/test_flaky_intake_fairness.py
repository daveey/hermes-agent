"""Deterministic budget exhaustion across real journal reopen boundaries."""
import json

from scripts.ci import flaky_intake as intake
from scripts.ci import flaky_kanban as fk
from tests.ci.test_flaky_intake import Board


def test_enqueued_retries_precede_sustained_new_arrivals(tmp_path):
    path = tmp_path / "queue.sqlite3"
    run = {"id": 1, "head_sha": "a" * 40}
    journal = intake.Journal(path)
    journal.record("o/r", [(9, [{"test_id": "tests/a.py::test_old"}])], run)
    journal.remember_runs("o/r", [run])
    old = journal.pending("o/r")[0]
    journal.rotate("o/r", "occurrence", str(old["id"]))
    journal.rotate("o/r", "run", "1/1")
    journal.close()
    for i in range(2, 7):
        journal = intake.Journal(path)
        new_run = {"id": i, "head_sha": "a" * 40}
        journal.record("o/r", [(9, [{"test_id": f"tests/a.py::test_new_{i}"}])], new_run)
        pending_runs = journal.remember_runs("o/r", [new_run, run])
        assert pending_runs[0]["id"] == 1
        assert journal.pending("o/r")[0]["id"] == old["id"]
        journal.close()


def test_failed_page_precedes_new_pages_after_restart(tmp_path, monkeypatch):
    path = tmp_path / "queue.sqlite3"
    clock, attempted = [0.0], []
    monkeypatch.setattr(intake.time, "monotonic", lambda: clock[0])
    class Unavailable(Board):
        def list_cards(self):
            clock[0] += 20
            raise RuntimeError("unavailable")
    for i in range(1, 5):
        journal = intake.Journal(path)
        journal.record("o/r", [(9, [{"test_id": f"tests/a.py::test_{i}"}])],
                       {"id": i, "head_sha": "a" * 40})
        journal.plan_pages("o/r", journal.pending("o/r"))
        before = dict(journal.db.execute("SELECT item,ticket FROM scheduling WHERE kind='page'"))
        journal.deliver("o/r", Unavailable(), deadline=clock[0] + 10)
        after = dict(journal.db.execute("SELECT item,ticket FROM scheduling WHERE kind='page'"))
        attempted.extend(int(k) for k, v in after.items() if before.get(k) != v)
        journal.close()
    assert attempted == [1, 1, 2, 1]


def test_legacy_missing_tickets_backfill_before_new_arrivals(tmp_path):
    path = tmp_path / "queue.sqlite3"
    journal = intake.Journal(path)
    run = {"id": 9, "head_sha": "a" * 40}
    journal.remember_runs("o/r", [run])
    journal.record("o/r", [(9, [{"test_id": "tests/a.py::test_old"}])], run)
    journal.plan_pages("o/r", journal.pending("o/r"))
    with journal.db:
        journal.db.execute("DELETE FROM scheduling")
    journal.close()
    journal = intake.Journal(path)
    before = [tuple(r) for r in journal.db.execute("SELECT * FROM scheduling ORDER BY ticket")]
    assert {r[1] for r in before} == {"run", "occurrence", "page"}
    assert [r["id"] for r in journal.remember_runs("o/r", [{"id": 1, "head_sha": "a" * 40}])] == [9, 1]
    journal.close()
    journal = intake.Journal(path)
    assert [tuple(r) for r in journal.db.execute("SELECT * FROM scheduling ORDER BY ticket LIMIT 3")] == before
    journal.close()


def test_reused_page_id_gets_new_enqueue_ticket(tmp_path):
    journal = intake.Journal(tmp_path / "queue.sqlite3")
    journal.record("o/r", [(9, [{"test_id": "tests/a.py::test_old"}])],
                   {"id": 1, "head_sha": "a" * 40})
    journal.plan_pages("o/r", journal.pending("o/r"))
    page_id = journal.db.execute("SELECT id FROM pages").fetchone()[0]
    journal.rotate("o/r", "page", str(page_id))
    ticket = journal.db.execute("SELECT ticket FROM scheduling WHERE kind='page'").fetchone()[0]
    journal.unplan_page(page_id)
    journal.rotate("o/r", "occurrence", "1")
    journal.plan_pages("o/r", journal.pending("o/r"))
    assert journal.db.execute("SELECT id FROM pages").fetchone()[0] == page_id
    assert journal.db.execute("SELECT ticket FROM scheduling WHERE kind='page'").fetchone()[0] > ticket
    journal.close()


def test_main_drains_durable_backlog_before_slow_discovery(tmp_path, monkeypatch):
    home = tmp_path / "isolated-home"
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("GH_TOKEN", "fixture-not-a-secret")
    state_path = tmp_path / "cursor.json"
    argv = ["--state", str(state_path), "--repo", "o/r", "--from-github", "--budget", "10"]
    assert fk.main([*argv, "--initialize-intake"]) == 0
    journal = intake.Journal(state_path.with_suffix(".intake.sqlite3"))
    journal.record("o/r", [(9, [{"test_id": "tests/a.py::test_pending"}])],
                   {"id": 1, "head_sha": "a" * 40})
    journal.close()
    board = Board()
    monkeypatch.setattr(fk, "CliBoard", lambda *a: board)
    monkeypatch.setattr(fk, "HubRelay", lambda *a, **kw: None)
    clock, observed = [0.0], []
    monkeypatch.setattr(fk.time, "monotonic", lambda: clock[0])
    def discover(*args, **kwargs):
        observed.append(len(board.notes.get("card0", [])))
        assert state_path.is_file(), "delivery must checkpoint before network"
        clock[0] += 20
        raise OSError("fixture network unavailable")
    monkeypatch.setattr(fk, "_completed_runs", discover)
    assert fk.main(argv) == 1  # network failure remains red, not hidden by delivery
    assert observed == [1]
    journal = intake.Journal(state_path.with_suffix(".intake.sqlite3"))
    assert journal.db.execute("SELECT sum(delivered) FROM occurrences").fetchone()[0] == 1
    journal.close()
    assert fk.main(argv) == 1
    assert observed == [1, 1]
    assert len(board.cards) == 1



def test_budgeted_owner_scan_eventually_delivers_whole_backlog(tmp_path, monkeypatch):
    path = tmp_path / "queue.sqlite3"
    clock = [0.0]
    monkeypatch.setattr(fk.time, "monotonic", lambda: clock[0])
    original = fk.file_cards
    def resolve(*args, **kwargs):
        clock[0] += 1
        return original(*args, **kwargs)
    monkeypatch.setattr(fk, "file_cards", resolve)
    board, state = Board(), {"cards": {}}
    with_journal = intake.Journal(path)
    with_journal.record("o/r", [(9, [{"test_id": f"tests/a.py::test_{i}"} for i in range(40)])],
                        {"id": 1, "head_sha": "a" * 40})
    with_journal.close()
    for _ in range(12):
        journal = intake.Journal(path)
        fk.drain_intake(journal, "o/r", board, state, None, deadline=clock[0] + 10)
        journal.close()
    journal = intake.Journal(path)
    assert journal.db.execute("SELECT sum(delivered) FROM occurrences").fetchone()[0] == 40
    assert len(board.cards) == 1
    items = [item for payload in board.notes["card0"] for item in
             json.loads(payload.split("```json\n")[1].split("\n```")[0])["occurrences"]]
    assert len(items) == len({item["occurrence_id"] for item in items}) == 40
    journal.close()


def test_pending_run_retry_rotates_after_budget_exhaustion(tmp_path, monkeypatch):
    path = tmp_path / "queue.sqlite3"
    clock, seen = [0.0], []
    monkeypatch.setattr(fk.time, "monotonic", lambda: clock[0])
    runs = [{"id": i, "head_sha": "a" * 40} for i in (1, 2)]
    monkeypatch.setattr(fk, "_completed_runs", lambda *a, **kw: runs)
    def poll(repo, token, run, board, state, relay, journal):
        seen.append(run["id"])
        clock[0] += 20
        return "unavailable", [], "missing"
    monkeypatch.setattr(fk, "_poll_run", poll)
    state = {"cards": {}, "runs": []}
    receipts = []
    for _ in range(4):
        journal = intake.Journal(path)
        receipts += fk.poll_github("o/r", "unused", Board(), state, journal=journal, deadline=clock[0] + 10)
        journal.close()
    assert seen == [1, 2, 1, 2]
    assert sum(line.startswith("FAILED run=") for line in receipts) == 2
    journal = intake.Journal(path)
    assert len(journal.remember_runs("o/r", [])) == 2
    journal.close()


def test_retry_pages_rotate_after_slow_failure(tmp_path, monkeypatch):
    path = tmp_path / "queue.sqlite3"
    clock = [0.0]
    monkeypatch.setattr(intake.time, "monotonic", lambda: clock[0])
    class Slow(Board):
        def comment(self, cid, body):
            if '"test_id": "tests/a.py::test_0"' in body:
                clock[0] += 20
                raise RuntimeError("delivery unavailable")
            super().comment(cid, body)
    journal = intake.Journal(path)
    journal.record("o/r", [(9, [{"test_id": f"tests/a.py::test_{i}"} for i in range(2)])],
                   {"id": 1, "head_sha": "a" * 40})
    for row in journal.pending("o/r"):
        journal.plan_pages("o/r", [row])
    journal.close()
    board = Slow()
    for _ in range(2):
        journal = intake.Journal(path)
        journal.deliver("o/r", board, deadline=clock[0] + 10)
        journal.close()
    journal = intake.Journal(path)
    assert [r[0] for r in journal.db.execute("SELECT delivered FROM occurrences ORDER BY id")] == [0, 1]
    assert len(board.notes["card0"]) == 1
    journal.close()


def test_slow_unavailable_owner_rotates_past_prefix_after_restart(tmp_path, monkeypatch):
    path = tmp_path / "queue.sqlite3"
    clock = [0.0]
    monkeypatch.setattr(fk.time, "monotonic", lambda: clock[0])
    original = fk.file_cards
    def resolve(entries, **kwargs):
        if entries[0]["test_id"].endswith("test_0"):
            clock[0] += 20
            raise RuntimeError("owner unavailable")
        return original(entries, **kwargs)
    monkeypatch.setattr(fk, "file_cards", resolve)
    journal = intake.Journal(path)
    journal.record("o/r", [(9, [{"test_id": f"tests/a.py::test_{i}"} for i in range(2)])],
                   {"id": 1, "head_sha": "a" * 40})
    journal.close()
    board, state = Board(), {"cards": {}}
    for _ in range(3):
        journal = intake.Journal(path)
        fk.drain_intake(journal, "o/r", board, state, None, deadline=clock[0] + 10)
        journal.close()
    journal = intake.Journal(path)
    assert [r[0] for r in journal.db.execute("SELECT delivered FROM occurrences ORDER BY id")] == [0, 1]
    assert len(board.cards) == 1
    journal.close()
