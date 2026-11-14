"""Recovery without duplicate creates or abandoning unreadable owners."""
import json

import pytest

from scripts.ci import flaky_kanban as fk
from scripts.ci.flaky_intake import Journal
from tests.ci.test_flaky_intake import Board, record


class ReadableBoard(Board):
    def card(self, cid, board=None):
        return self.cards.get(cid)


def test_ambiguous_create_then_unrelated_overlap_recovers(tmp_path):
    class Overlap(ReadableBoard):
        calls = 0

        def create(self, title, body, key, **kwargs):
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("lost response")
            if not kwargs.get("distinct_from"):
                raise fk.CreateRefused("overlap", [{"id": "old"}])
            assert kwargs["distinct_from"] == ["old"]
            assert kwargs["distinct_reason"]
            return super().create(title, body, key)

    journal = Journal(tmp_path / "queue.sqlite3")
    try:
        record(journal, count=1)
        board = Overlap()
        board.cards["old"] = dict(id="old", title="Yesterday's intake", body="", status="ready",
                                  idempotency_key="ci-flaky-intake:o/r:2000-01-01")
        board.notes["old"] = []
        assert any(s.startswith("FAILED") for s in fk.drain_intake(journal, "o/r", board, {"cards": {}}, None))
        fk.drain_intake(journal, "o/r", board, {"cards": {}}, None)
        assert journal.db.execute("SELECT sum(delivered) FROM occurrences").fetchone()[0] == 1
        assert board.calls == 3
        assert len(board.cards) == 2
    finally:
        journal.close()


@pytest.mark.parametrize("readable", [True, False])
def test_ambiguous_create_never_repeated_when_key_exists_or_inventory_fails(tmp_path, readable):
    class Lost(ReadableBoard):
        calls = 0
        unavailable = False

        def create(self, *args, **kwargs):
            self.calls += 1
            super().create(*args, **kwargs)
            raise RuntimeError("lost response")

        def list_cards(self):
            if self.unavailable:
                raise RuntimeError("inventory unavailable")
            return super().list_cards()

    journal = Journal(tmp_path / "queue.sqlite3")
    try:
        record(journal, count=1)
        board = Lost()
        fk.drain_intake(journal, "o/r", board, {"cards": {}}, None)
        board.unavailable = not readable
        fk.drain_intake(journal, "o/r", board, {"cards": {}}, None)
        assert board.calls == 1
        assert journal.db.execute("SELECT sum(delivered) FROM occurrences").fetchone()[0] == int(readable)
    finally:
        journal.close()


@pytest.mark.parametrize("cached", [True, False])
@pytest.mark.parametrize("job", [9, 10])
def test_legacy_cursor_body_receipt_dedupes_cutover(tmp_path, cached, job):
    path = tmp_path / "legacy.json"
    test = "tests/a.py::test_0"
    url = "https://github.com/o/r/actions/runs/1/job/9"
    cards = {test: {"card": "card0", "generation": 0}} if cached else {}
    path.write_text(json.dumps({"runs": ["1/1"], "cards": cards}))
    board = ReadableBoard()
    board.create(f"Deflake {test}", f"Flaky test: `{test}`\n\nFailed in {url}", f"deflake:{test}")
    journal = Journal(tmp_path / "queue.sqlite3")
    try:
        journal.record("o/r", [(job, [{"test_id": test}])], {"id": 1, "head_sha": "a" * 40})
        fk.drain_intake(journal, "o/r", board, fk.load_state(path), None)
        assert journal.db.execute("SELECT sum(delivered) FROM occurrences").fetchone()[0] == 1
        assert len(board.notes["card0"]) == int(job != 9)
    finally:
        journal.close()


@pytest.mark.parametrize("listed", [False, True])
def test_cached_missing_local_owner_requires_authoritative_absence(listed):
    class Unreadable(ReadableBoard):
        def card(self, cid, board=None):
            return None

    board = Unreadable()
    test = "tests/a.py::test_0"
    if listed:
        board.cards["gone"] = dict(id="gone", title=f"Deflake {test}", body="", status="ready")
    state = {"cards": {test: {"card": "gone", "generation": 0}}}
    lines = fk.file_cards([{"test_id": test}], run_url="https://github.com/o/r/actions/runs/1/job/9",
                          branch="a" * 40, board=board, state=state, on_unowned=lambda: "queued")
    if listed:
        assert lines[0].startswith("FAILED")
        assert state["cards"][test]["card"] == "gone"
    else:
        assert lines == ["queued"]
        assert test not in state["cards"]


@pytest.mark.parametrize("scope", [{"gateway": "peer"}, {"board": "other"}])
def test_foreign_cache_absence_never_uses_local_inventory(scope):
    test = "tests/a.py::test_0"
    state = {"cards": {test: {"card": "gone", "generation": 0, **scope}}}
    lines = fk.file_cards([{"test_id": test}], run_url="https://github.com/o/r/actions/runs/1/job/9",
                          branch="a" * 40, board=ReadableBoard(), state=state, on_unowned=lambda: "queued")
    assert lines[0].startswith("FAILED")
    assert state["cards"][test]["card"] == "gone"


def test_cached_owner_inventory_error_preserves_cache():
    class Unavailable(ReadableBoard):
        def list_cards(self):
            raise RuntimeError("unavailable")

    test = "tests/a.py::test_0"
    state = {"cards": {test: {"card": "gone", "generation": 0}}}
    with pytest.raises(RuntimeError, match="unavailable"):
        fk.file_cards([{"test_id": test}], run_url="https://github.com/o/r/actions/runs/1/job/9",
                      branch="a" * 40, board=Unavailable(), state=state, on_unowned=lambda: "queued")
    assert state["cards"][test]["card"] == "gone"


def test_gone_cached_owner_is_replaced_by_discovered_owner():
    board = ReadableBoard()
    test = "tests/a.py::test_0"
    replacement = board.create(f"Deflake {test}", "", f"deflake:{test}")
    state = {"cards": {test: {"card": "gone", "generation": 2}}}
    lines = fk.file_cards([{"test_id": test}], run_url="https://github.com/o/r/actions/runs/1/job/9",
                          branch="a" * 40, board=board, state=state, on_unowned=lambda: "queued")
    assert lines == [f"appended {replacement} {test}"]
    assert state["cards"][test] == {"card": replacement, "generation": 2}
    assert len(board.notes[replacement]) == 1
