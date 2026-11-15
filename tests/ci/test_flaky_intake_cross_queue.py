"""One durable FIFO across owner resolution and immutable page retries."""
import pytest

from scripts.ci import flaky_intake as intake
from scripts.ci import flaky_kanban as fk
from tests.ci.test_flaky_intake import Board


@pytest.mark.parametrize("first", ["page", "occurrence"])
def test_slow_queues_take_turns_across_restart(tmp_path, monkeypatch, first):
    path = tmp_path / "queue.sqlite3"
    clock, calls = [0.0], []
    monkeypatch.setattr(fk.time, "monotonic", lambda: clock[0])
    journal = intake.Journal(path)
    journal.record("o/r", [(9, [{"test_id": "tests/a.py::test_page"}])],
                   {"id": 1, "head_sha": "a" * 40})
    journal.plan_pages("o/r", journal.pending("o/r"))
    with journal.db:
        journal.db.execute("UPDATE pages SET attempted=1")
    original = tuple(journal.db.execute("SELECT id,payload,attempted FROM pages").fetchone())
    journal.record("o/r", [(9, [{"test_id": "tests/a.py::test_pending"}])],
                   {"id": 2, "head_sha": "a" * 40})
    with journal.db:
        journal.enqueue("o/r", "run", "2")  # a run ticket is not pending-owner work
    if first == "occurrence":
        journal.rotate("o/r", "page", "1")
    journal.close()

    class Slow(Board):
        def list_cards(self):
            calls.append("page")
            clock[0] += 20
            raise RuntimeError("page inventory unavailable")

    def resolve(*args, **kwargs):
        calls.append("occurrence")
        clock[0] += 20
        raise RuntimeError("owner unavailable")

    monkeypatch.setattr(fk, "file_cards", resolve)
    board = Slow()
    other = "occurrence" if first == "page" else "page"
    for index in range(4):
        journal = intake.Journal(path)
        log = fk.drain_intake(journal, "o/r", board, {"cards": {}}, None, deadline=clock[0] + 10)
        # No second operation may start after the first consumes the budget.
        assert len(calls) == index + 1
        assert any("intake_budget" in line for line in log)
        assert tuple(journal.db.execute("SELECT id,payload,attempted FROM pages").fetchone()) == original
        assert journal.db.execute("SELECT sum(delivered) FROM occurrences").fetchone()[0] == 0
        journal.close()
    assert calls == [first, other, first, other]
    assert not board.cards


def test_retry_page_delivers_despite_slow_pending_owner(tmp_path, monkeypatch):
    path = tmp_path / "queue.sqlite3"
    clock = [0.0]
    monkeypatch.setattr(fk.time, "monotonic", lambda: clock[0])
    journal = intake.Journal(path)
    journal.record("o/r", [(9, [{"test_id": "tests/a.py::test_page"}])],
                   {"id": 1, "head_sha": "a" * 40})
    journal.plan_pages("o/r", journal.pending("o/r"))
    with journal.db:
        journal.db.execute("UPDATE pages SET attempted=1")
    payload = journal.db.execute("SELECT payload FROM pages").fetchone()[0]
    journal.record("o/r", [(9, [{"test_id": "tests/a.py::test_pending"}])],
                   {"id": 2, "head_sha": "a" * 40})
    journal.close()

    def resolve(*args, **kwargs):
        clock[0] += 20
        raise RuntimeError("owner unavailable")

    monkeypatch.setattr(fk, "file_cards", resolve)
    board = Board()
    for _ in range(3):
        journal = intake.Journal(path)
        fk.drain_intake(journal, "o/r", board, {"cards": {}}, None, deadline=clock[0] + 10)
        journal.close()
    journal = intake.Journal(path)
    assert [r[0] for r in journal.db.execute("SELECT delivered FROM occurrences ORDER BY id")] == [1, 0]
    assert len(board.cards) == 1
    assert board.notes["card0"] == [payload]
    journal.close()


def test_pending_progresses_despite_slow_retry_and_stale_tickets(tmp_path, monkeypatch):
    path = tmp_path / "queue.sqlite3"
    clock = [0.0]
    monkeypatch.setattr(fk.time, "monotonic", lambda: clock[0])
    journal = intake.Journal(path)
    run = {"id": 1, "head_sha": "a" * 40}
    journal.remember_runs("o/r", [run])
    journal.record("o/r", [(9, [{"test_id": "tests/a.py::test_page"}])], run)
    journal.plan_pages("o/r", journal.pending("o/r"))
    with journal.db:
        journal.db.execute("UPDATE pages SET attempted=1")
        journal.enqueue("o/r", "page", "999")  # retained historical ticket, no page
    journal.record("o/r", [(9, [{"test_id": "tests/a.py::test_pending"}])],
                   {"id": 2, "head_sha": "a" * 40})
    journal.record("other/repo", [(9, [{"test_id": "tests/a.py::test_foreign"}])], run)
    journal.close()

    class Slow(Board):
        def comment(self, cid, body):
            if '"test_id": "tests/a.py::test_page"' in body:
                clock[0] += 20
                raise RuntimeError("page unavailable")
            super().comment(cid, body)

    board = Slow()
    for _ in range(3):
        journal = intake.Journal(path)
        fk.drain_intake(journal, "o/r", board, {"cards": {}}, None, deadline=clock[0] + 10)
        journal.close()
    journal = intake.Journal(path)
    assert [r[0] for r in journal.db.execute("SELECT delivered FROM occurrences ORDER BY id")] == [0, 1, 0]
    assert len(board.cards) == 1
    assert len(board.notes["card0"]) == 1
    # An unbounded ordinary drain retains the same filtering and exact readback.
    fk.drain_intake(journal, "o/r", board, {"cards": {}}, None)
    assert len(board.notes["card0"]) == 1
    with journal.db:
        journal.enqueue("o/r", "run", "2")  # shares a pending occurrence's numeric ID
    selected = []
    def select_pages(*args, **kwargs):
        if kwargs["page_ids"]:
            selected.append(("page", kwargs["page_ids"]))
        return []
    monkeypatch.setattr(journal, "deliver", select_pages)

    def unavailable(entries, **kwargs):
        selected.append(("occurrence", entries[0]["test_id"]))
        raise RuntimeError("fixture unavailable")

    monkeypatch.setattr(fk, "file_cards", unavailable)
    fk.drain_intake(journal, "o/r", board, {"cards": {}}, None)
    assert selected == [("page", {1})], "exclude delivered, foreign, stale and non-drain tickets"
    journal.close()


@pytest.mark.parametrize("boundary", ["page", "occurrence"])
def test_batch_flush_deadline_prevents_next_attempt(tmp_path, monkeypatch, boundary):
    journal = intake.Journal(tmp_path / "queue.sqlite3")
    journal.record("o/r", [(9, [{"test_id": "tests/a.py::test_first"}])],
                   {"id": 1, "head_sha": "a" * 40})
    journal.plan_pages("o/r", journal.pending("o/r"))
    if boundary == "page":
        with journal.db:
            journal.db.execute("UPDATE pages SET attempted=1")
    journal.record("o/r", [(9, [{"test_id": "tests/a.py::test_second"}])],
                   {"id": 2, "head_sha": "a" * 40})
    if boundary == "page":
        journal.rotate("o/r", "page", "1")
    clock, owners = [0.0], []
    monkeypatch.setattr(fk.time, "monotonic", lambda: clock[0])

    def resolve(entries, **kwargs):
        owners.append(entries[0]["test_id"])
        return ["queued"]

    class Slow(Board):
        def list_cards(self):
            clock[0] += 20
            raise RuntimeError("delivery inventory unavailable")

    monkeypatch.setattr(fk, "file_cards", resolve)
    expected = "first" if boundary == "occurrence" else "second"
    deferred_kind, deferred_id = ("occurrence", "2") if boundary == "occurrence" else ("page", "1")
    ticket = journal.db.execute("SELECT ticket FROM scheduling WHERE kind=? AND item=?",
                                (deferred_kind, deferred_id)).fetchone()[0]
    log = fk.drain_intake(journal, "o/r", Slow(), {"cards": {}}, None, deadline=10)
    assert owners == [f"tests/a.py::test_{expected}"]
    assert any("intake_budget" in line for line in log)
    assert journal.db.execute("SELECT ticket FROM scheduling WHERE kind=? AND item=?",
                              (deferred_kind, deferred_id)).fetchone()[0] == ticket
    journal.close()


def test_expired_drain_does_not_rotate_or_call_board(tmp_path, monkeypatch):
    journal = intake.Journal(tmp_path / "queue.sqlite3")
    journal.record("o/r", [(9, [{"test_id": "tests/a.py::test_pending"}])],
                   {"id": 1, "head_sha": "a" * 40})
    before = [tuple(r) for r in journal.db.execute("SELECT * FROM scheduling")]
    monkeypatch.setattr(fk.time, "monotonic", lambda: 10)
    monkeypatch.setattr(fk, "file_cards", lambda *a, **kw: pytest.fail("expired owner lookup"))
    assert fk.drain_intake(journal, "o/r", Board(), {"cards": {}}, None, deadline=10) == [
        "FAILED intake_budget; durable retry pending"]
    assert [tuple(r) for r in journal.db.execute("SELECT * FROM scheduling")] == before
    journal.close()
