"""Durable intake failure and rollover controls; external Board is an in-memory fake."""
import json

import pytest

from scripts.ci import flaky_intake as intake


def test_missing_previously_initialized_queue_fails_closed(tmp_path):
    path = tmp_path / "queue.sqlite3"
    journal = intake.Journal(path)
    record(journal, count=1)
    journal.close()
    path.unlink()
    with pytest.raises(OSError, match="intake_queue_missing"):
        intake.Journal(path)
    assert not path.exists()


def test_lock_is_bounded_and_corruption_fails_closed(tmp_path):
    import sqlite3

    path = tmp_path / "queue.sqlite3"
    journal = intake.Journal(path)
    with pytest.raises(BlockingIOError):
        intake.Journal(path)
    journal.close()
    path.write_bytes(b"corrupted journal")
    for _ in range(2):
        with pytest.raises(sqlite3.DatabaseError):
            intake.Journal(path)
    assert path.read_bytes() == b"corrupted journal"


@pytest.mark.parametrize("field,value", [
    ("retried", "secret failure output"), ("source_sha", "secret failure output"),
    ("slice", "secret failure output"), ("slices", True), ("run_attempt", False),
    ("test_id", "not a test"),
])
def test_metadata_validation_is_atomic(tmp_path, field, value):
    journal = intake.Journal(tmp_path / "queue.sqlite3")
    good = {"test_id": "tests/a.py::test_a", "retried": "tests"}
    bad = {**good, field: value}
    with pytest.raises(ValueError, match="^invalid intake metadata$"):
        journal.record("o/r", [(1, [good, bad])], {"id": 1, "head_sha": "a" * 40})
    assert journal.db.execute("SELECT count(*) FROM occurrences").fetchone()[0] == 0
    journal.close()


def test_ambiguous_delivery_then_archive_reconciles_before_carry(tmp_path):
    journal = intake.Journal(tmp_path / "queue.sqlite3")
    record(journal, count=1)
    board = Board()
    board.fail = True
    journal.deliver("o/r", board)
    payload = journal.db.execute("SELECT payload FROM pages").fetchone()[0]
    board.notes["card0"].append(payload)
    board.cards["card0"]["status"] = "archived"
    journal.deliver("o/r", board)
    assert journal.db.execute("SELECT sum(delivered) FROM occurrences").fetchone()[0] == 1
    assert journal.pending("o/r") == []
    assert len(board.cards) == 1
    journal.close()

@pytest.mark.parametrize("readback", [None, {}, {"comments": "truncated"}])
def test_unavailable_comment_readback_is_not_empty(readback):
    from scripts.ci import flaky_kanban as fk

    class Unreadable(fk.CliBoard):
        def _show(self, card_id, board=None):
            return readback

    with pytest.raises(RuntimeError, match="comments unavailable"):
        Unreadable().comments("owner")


def test_missing_replica_never_admits_to_intake(tmp_path):
    from scripts.ci import flaky_kanban as fk

    admitted = []
    with pytest.raises(RuntimeError, match="owner inventory unavailable"):
        fk.file_cards([{"test_id": "tests/a.py::test_a"}], run_url="https://github.com/o/r/actions/runs/1/job/9",
                      branch="a" * 40, board=Board(), state={"cards": {}},
                      relay=fk.HubRelay(tmp_path / "missing.db"),
                      on_unowned=lambda: admitted.append(True) or "queued")
    assert not admitted


def test_matching_foreign_owner_unreadable_never_admits(tmp_path):
    import sqlite3
    from scripts.ci import flaky_kanban as fk

    path = tmp_path / "hub.db"
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE replica (gw TEXT, key TEXT, coll TEXT, data TEXT)")
        conn.execute("INSERT INTO replica VALUES (?,?,?,?)", (
            "peer", "default/default/owner", "tasks", json.dumps({
                "id": "owner", "title": "Deflake tests/a.py::test_a", "status": "running"})))
    class Unreadable(fk.HubRelay):
        def self_gateway(self):
            return "here"
        def card(self, gw, board, card_id):
            return None

    admitted = []
    with pytest.raises(RuntimeError, match="owner readback unavailable"):
        fk.file_cards([{"test_id": "tests/a.py::test_a"}], run_url="https://github.com/o/r/actions/runs/1/job/9",
                      branch="a" * 40, board=Board(), state={"cards": {}}, relay=Unreadable(path),
                      on_unowned=lambda: admitted.append(True) or "queued")
    assert not admitted


@pytest.mark.parametrize("status", ["done", "archived"])
@pytest.mark.parametrize("same_job", [True, False])
@pytest.mark.parametrize("history", ["complete", "missing", "tail", "invalid_count", "clipped_comment", "clipped_body"])
def test_foreign_closed_owner_preserves_exact_job_ownership(tmp_path, status, same_job, history):
    import sqlite3
    import time
    from scripts.ci import flaky_kanban as fk

    url = "https://github.com/o/r/actions/runs/1/job/9"
    path = tmp_path / "hub.db"
    owner = {"id": "owner", "title": "Deflake tests/a.py::test_a", "status": status,
             "body": "", "comments": 1,
             "last_comments": [{"body": url if same_job else url.replace("/9", "/8")}]}
    owner.update({
        "complete": {}, "missing": {}, "tail": {"comments": 4}, "invalid_count": {"comments": True},
        "clipped_comment": {"last_comments": [{"body": owner["last_comments"][0]["body"] + "\n" + "x" * 500}]},
        "clipped_body": {"body": "x" * 4000},
    }[history])
    if history == "missing":
        owner.pop("comments")
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE replica (gw TEXT, key TEXT, coll TEXT, data TEXT)")
        conn.execute("INSERT INTO replica VALUES (?,?,?,?)", (
            "peer", "default/default/owner", "tasks", json.dumps(owner)))
        conn.execute("INSERT INTO replica VALUES (?,?,?,?)", (
            "peer", "info", "gateway", json.dumps({"gateway": "peer", "task_scopes": [
                {"profile": "default", "board": "default", "state": "ok", "observed_at": time.time()}]})))
    class Relay(fk.HubRelay):
        def self_gateway(self):
            return "here"
        def comment(self, *args):
            pytest.fail("closed exact-job owner must not receive a new comment")

    state = {"cards": {}}
    notes: dict = {}
    lines = fk.file_cards([{"test_id": "tests/a.py::test_a"}], run_url=url,
                          branch="a" * 40, board=Board(), state=state, relay=Relay(path),
                          on_unowned=lambda: "queued", owner_notes=notes)
    if same_job:
        assert len(lines) == 1 and lines[0].startswith("skipped")
        assert state["cards"]["tests/a.py::test_a"]["gateway"] == "peer"
        assert notes == {}
    else:
        # Unverifiable closed history must not strand the occurrence: it is
        # admitted to the intake WITH the candidate named for triage.
        assert lines == ["queued"]
        assert state == {"cards": {}}
        expected = [] if history == "complete" else [
            {"candidate": "@task:default@peer/owner", "status": status, "reason": "closed_history_incomplete"}]
        assert notes == {"tests/a.py::test_a": expected}


def test_complete_foreign_history_empty_is_complete():
    from scripts.ci import flaky_kanban as fk
    assert fk._complete_foreign_history({"comments": 0, "last_comments": [], "body": "", "title": ""})


@pytest.mark.parametrize("field,value", [
    ("comments", -1), ("comments", "0"), ("comments", False),
    ("last_comments", {}), ("last_comments", [None]), ("last_comments", [{"body": None}]),
    ("last_comments", [{"body": "x" * 500}]), ("body", None), ("body", "x" * 4000),
    ("title", None), ("title", "x" * 300),
])
def test_incomplete_foreign_history_cannot_prove_absence(field, value):
    from scripts.ci import flaky_kanban as fk
    owner = {"comments": 0, "last_comments": [], "body": "", "title": ""}
    if field == "last_comments" and isinstance(value, list):
        owner["comments"] = len(value)
    owner[field] = value
    assert not fk._complete_foreign_history(owner)


def test_unreadable_cached_owner_never_admits_to_intake():
    from scripts.ci import flaky_kanban as fk

    class MissingOwner(Board):
        def card(self, cid):
            return None

        def list_cards(self):
            return [{"id": "missing", "status": "running", "title": "Deflake tests/a.py::test_a"}]

    state = {"cards": {"tests/a.py::test_a": {"card": "missing", "generation": 0}}}
    admitted = []
    lines = fk.file_cards([{"test_id": "tests/a.py::test_a"}], run_url="https://github.com/o/r/actions/runs/1/job/9",
                          branch="a" * 40, board=MissingOwner(), state=state,
                          on_unowned=lambda: admitted.append(True) or "queued")
    assert not admitted
    assert len(lines) == 1 and lines[0].startswith("FAILED")
    assert state["cards"]["tests/a.py::test_a"]["card"] == "missing"


@pytest.mark.parametrize("field,value", [
    ("id", True), ("run_attempt", 0), ("workflow_id", "secret"),
    ("head_sha", "secret"), ("head_branch", "x" * 256),
    ("head_branch", "bad\nbranch"), ("conclusion", "secret"),
    ("updated_at", "2026-99-99T00:00:00Z"), ("run_started_at", {"secret": 1}),
])
def test_run_descriptor_validation_is_atomic(tmp_path, field, value):
    journal = intake.Journal(tmp_path / "queue.sqlite3")
    good = {"id": 1, "head_sha": "a" * 40, "head_branch": "feature/topic"}
    with pytest.raises(ValueError, match="^invalid intake run$"):
        journal.remember_runs("o/r", [good, {**good, "id": 2, field: value}])
    assert journal.remember_runs("o/r", []) == []
    journal.close()


def test_delivery_budget_preserves_every_unsent_page(tmp_path, monkeypatch):
    journal = intake.Journal(tmp_path / "queue.sqlite3")
    record(journal)
    board = Board()
    monkeypatch.setattr(intake.time, "monotonic", lambda: 10)
    lines = journal.deliver("o/r", board, deadline=10)
    assert any(line.startswith("FAILED intake_budget") for line in lines)
    assert not board.cards
    assert journal.db.execute("SELECT sum(delivered) FROM occurrences").fetchone()[0] == 0
    journal.deliver("o/r", board, deadline=11)
    assert journal.db.execute("SELECT sum(delivered) FROM occurrences").fetchone()[0] == 30
    journal.close()


def test_planned_but_unattempted_page_rechecks_new_exact_owner(tmp_path):
    from scripts.ci import flaky_kanban as fk

    journal = intake.Journal(tmp_path / "queue.sqlite3")
    record(journal, count=1)
    board = Board()
    owner = board.create("Deflake tests/a.py::test_0", "", "deflake:tests/a.py::test_0")
    lines = fk.drain_intake(journal, "o/r", board, {"cards": {}}, None)
    assert len(board.cards) == 1
    assert len(board.notes[owner]) == 1 and board.notes[owner][0].startswith("Flaked again:")
    assert journal.db.execute("SELECT sum(delivered) FROM occurrences").fetchone()[0] == 1
    assert not any(line.startswith("FAILED") for line in lines)
    journal.close()


def test_ambiguous_create_keeps_attempted_page_immutable(tmp_path):
    from scripts.ci import flaky_kanban as fk

    class LostCreate(Board):
        def create(self, title, body, key):
            cid = super().create(title, body, key)
            if key.startswith("ci-flaky-intake:"):
                raise RuntimeError("response lost")
            return cid

    journal = intake.Journal(tmp_path / "queue.sqlite3")
    record(journal, count=1)
    board = LostCreate()
    assert journal.deliver("o/r", board)[0].startswith("FAILED")
    original = journal.db.execute("SELECT payload FROM pages").fetchone()[0]
    board.create("Deflake tests/a.py::test_0", "", "deflake:tests/a.py::test_0")
    fk.drain_intake(journal, "o/r", board, {"cards": {}}, None)
    assert board.notes["card0"] == [original]
    assert board.notes["card1"] == []
    assert journal.db.execute("SELECT sum(delivered) FROM occurrences").fetchone()[0] == 1
    journal.close()


@pytest.mark.parametrize("exact", [True, False])
def test_intake_create_refusal_resolves_racing_owner_or_distinct_work(tmp_path, exact):
    from scripts.ci import flaky_kanban as fk

    class Racing(Board):
        def create(self, title, body, key, **kwargs):
            if not self.cards:
                other = "tests/a.py::test_0" if exact else "tests/other.py::test_other"
                super().create(f"Deflake {other}", "", f"deflake:{other}")
            if kwargs.get("distinct_from") != ["card0"]:
                raise fk.CreateRefused("overlap", [{"id": "card0"}])
            assert not exact, "must not declare an exact owner distinct"
            return super().create(title, body, key)

    journal = intake.Journal(tmp_path / "queue.sqlite3")
    record(journal, count=1)
    board, state = Racing(), {"cards": {}}
    fk.drain_intake(journal, "o/r", board, state, None)
    fk.drain_intake(journal, "o/r", board, state, None)
    assert journal.db.execute("SELECT sum(delivered) FROM occurrences").fetchone()[0] == 1
    assert len(board.cards) == (1 if exact else 2)
    if exact:
        assert len(board.notes["card0"]) == 1
        assert board.notes["card0"][0].startswith("Flaked again:")
    else:
        assert board.notes["card0"] == []
        assert len(board.notes["card1"]) == 1
    journal.close()


@pytest.mark.parametrize("status", ["done", "archived"])
@pytest.mark.parametrize("same_job", [True, False])
def test_closed_overlap_owner_only_blocks_its_own_job(tmp_path, status, same_job):
    from scripts.ci import flaky_kanban as fk

    class Racing(Board):
        def create(self, title, body, key, **kwargs):
            if not self.cards:
                cid = super().create("Deflake tests/a.py::test_0", "", "deflake:tests/a.py::test_0")
                self.cards[cid]["status"] = status
                job = 9 if same_job else 8
                self.notes[cid].append(f"https://github.com/o/r/actions/runs/1/job/{job}")
            if kwargs.get("distinct_from") != ["card0"]:
                raise fk.CreateRefused("overlap", [{"id": "card0"}])
            assert not same_job, "same job remains owned even when closed"
            return super().create(title, body, key)

    journal = intake.Journal(tmp_path / "queue.sqlite3")
    record(journal, count=1)
    board, state = Racing(), {"cards": {}}
    fk.drain_intake(journal, "o/r", board, state, None)
    fk.drain_intake(journal, "o/r", board, state, None)
    assert journal.db.execute("SELECT sum(delivered) FROM occurrences").fetchone()[0] == 1
    assert len(board.cards) == (1 if same_job else 2)
    assert len(board.notes["card0"]) == 1
    assert board.cards["card0"]["status"] == status
    journal.close()


def test_definitive_refusal_with_missing_readback_remains_retriable(tmp_path):
    from scripts.ci import flaky_kanban as fk

    class Refusing(Board):
        def create(self, *args, **kwargs):
            raise fk.CreateRefused("overlap", [{"id": "unknown"}])
        def card(self, cid):
            return None

    journal = intake.Journal(tmp_path / "queue.sqlite3")
    record(journal, count=1)
    assert any(line.startswith("FAILED") for line in fk.drain_intake(
        journal, "o/r", Refusing(), {"cards": {}}, None))
    assert len(journal.pending("o/r")) == 1
    board = Board()
    fk.drain_intake(journal, "o/r", board, {"cards": {}}, None)
    assert journal.db.execute("SELECT sum(delivered) FROM occurrences").fetchone()[0] == 1
    journal.close()


def test_prior_ambiguous_create_followed_by_refusal_never_repartitions(tmp_path):
    from scripts.ci import flaky_kanban as fk

    class Refusing(Board):
        def create(self, *args, **kwargs):
            raise fk.CreateRefused("overlap", [{"id": "unknown"}])

        def card(self, cid):
            return None

    journal = intake.Journal(tmp_path / "queue.sqlite3")
    record(journal, count=1)
    with journal.db:
        journal.db.execute("UPDATE pages SET attempted=1")
    original = tuple(journal.db.execute("SELECT * FROM pages").fetchone())
    fk.drain_intake(journal, "o/r", Refusing(), {"cards": {}}, None)
    assert tuple(journal.db.execute("SELECT * FROM pages").fetchone()) == original
    assert journal.pending("o/r") == []
    assert journal.db.execute("SELECT sum(delivered) FROM occurrences").fetchone()[0] == 0
    journal.close()


def test_old_page_schema_migrates_conservatively(tmp_path):
    import sqlite3

    path = tmp_path / "queue.sqlite3"
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE pages (id INTEGER PRIMARY KEY, repo TEXT NOT NULL, day TEXT NOT NULL, "
                     "payload TEXT NOT NULL, card TEXT, delivered INTEGER NOT NULL DEFAULT 0)")
        conn.execute("INSERT INTO pages(repo,day,payload) VALUES ('o/r','2026-11-13','old payload')")
    journal = intake.Journal(path)
    journal.recheck_unattempted("o/r")
    assert journal.db.execute("SELECT payload,attempted FROM pages").fetchone()[:] == ("old payload", 1)
    record(journal, count=1)
    assert journal.db.execute("SELECT attempted FROM pages WHERE id=2").fetchone()[0] == 0
    journal.close()


def test_owner_resolution_budget_leaves_rows_retryable(tmp_path, monkeypatch):
    from scripts.ci import flaky_kanban as fk

    journal = intake.Journal(tmp_path / "queue.sqlite3")
    journal.record("o/r", [(9, [{"test_id": "tests/a.py::test_a"}])], {"id": 1, "head_sha": "a" * 40})
    monkeypatch.setattr(fk.time, "monotonic", lambda: 10)
    board = Board()
    lines = fk.drain_intake(journal, "o/r", board, {"cards": {}}, None, deadline=10)
    assert any(line.startswith("FAILED intake_budget") for line in lines)
    assert len(journal.pending("o/r")) == 1
    assert not board.cards
    journal.close()


def test_unavailable_owner_does_not_abort_other_occurrences(tmp_path, monkeypatch):
    from scripts.ci import flaky_kanban as fk

    journal = intake.Journal(tmp_path / "queue.sqlite3")
    record(journal, count=2)
    original = fk.file_cards
    def resolve(entries, **kwargs):
        if entries[0]["test_id"] == "tests/a.py::test_0":
            raise RuntimeError("owner scope freshness unavailable")
        return original(entries, **kwargs)
    monkeypatch.setattr(fk, "file_cards", resolve)
    board, state = Board(), {"cards": {}}
    for _ in range(2):
        lines = fk.drain_intake(journal, "o/r", board, state, None)
        assert any(line.startswith("FAILED") for line in lines)
        assert journal.db.execute("SELECT delivered FROM occurrences ORDER BY id").fetchone()[0] == 0
        assert journal.db.execute("SELECT sum(delivered) FROM occurrences").fetchone()[0] == 1
    assert len(board.cards) == 1 and len(board.notes["card0"]) == 1
    journal.close()


class Board:
    def __init__(self):
        self.cards = {}
        self.notes = {}
        self.fail = False
        self.ambiguous = False

    def list_cards(self):
        return list(self.cards.values())

    def create(self, title, body, key):
        existing = [c for c in self.cards.values() if c["idempotency_key"] == key]
        if existing:
            return existing[0]["id"]
        cid = f"card{len(self.cards)}"
        self.cards[cid] = dict(id=cid, title=title, body=body, idempotency_key=key, status="ready")
        self.notes[cid] = []
        return cid

    def card(self, cid):
        return self.cards[cid]

    def comments(self, cid):
        return self.notes[cid]

    def comment(self, cid, body):
        if self.fail:
            raise RuntimeError("unavailable")
        self.notes[cid].append(body)
        if self.ambiguous:
            raise RuntimeError("response lost")

    def reopen(self, cid):
        self.cards[cid]["status"] = "ready"


def record(journal, run=1, count=30):
    journal.record("o/r", [(9, [{"test_id": f"tests/a.py::test_{i}", "retried": "tests"}
                                for i in range(count)])], {"id": run, "head_sha": "a" * 40})
    journal.plan_pages("o/r", journal.pending("o/r"))


def test_failure_restart_immutable_pages_and_ambiguous_comment(tmp_path):
    path = tmp_path / "queue.sqlite3"
    j = intake.Journal(path)
    record(j)
    board = Board()
    board.fail = True
    assert all(line.startswith("FAILED") for line in j.deliver("o/r", board))
    assert j.db.execute("SELECT sum(delivered) FROM occurrences").fetchone()[0] == 0
    payloads = [r[0] for r in j.db.execute("SELECT payload FROM pages ORDER BY id")]
    j.close()
    j = intake.Journal(path)
    board.fail = False
    board.ambiguous = True
    assert all(not line.startswith("FAILED") for line in j.deliver("o/r", board))
    assert j.db.execute("SELECT sum(delivered) FROM occurrences").fetchone()[0] == 30
    assert len(board.cards) == 1
    assert board.notes["card0"] == payloads
    assert j.deliver("o/r", board) == []
    items = []
    for payload in payloads:
        assert len(payload.encode()) <= intake.PAGE_BYTES
        items += json.loads(payload.split("```json\n")[1].split("\n```")[0])["occurrences"]
    assert len({i["occurrence_id"] for i in items}) == 30
    assert len(payloads) > 1
    j.close()


def test_closed_intakes_preserve_history_and_deliver_to_new_generation(tmp_path, monkeypatch):
    monkeypatch.setattr(intake, "utc_day", lambda: "2026-11-13")
    j = intake.Journal(tmp_path / "queue.sqlite3")
    board = Board()
    record(j, count=1)
    j.deliver("o/r", board)
    board.cards["card0"]["status"] = "done"
    record(j, run=2, count=1)
    j.deliver("o/r", board)
    assert board.cards["card0"]["status"] == "done"
    assert len(board.cards) == 2
    board.cards["card1"]["status"] = "archived"
    record(j, run=3, count=1)
    assert not j.deliver("o/r", board)[0].startswith("FAILED")
    assert len(board.cards) == 3
    assert j.db.execute("SELECT sum(delivered) FROM occurrences").fetchone()[0] == 3
    assert j.db.execute("SELECT day,target_day FROM occurrences WHERE id=3").fetchone()[:] == (
        "2026-11-13", "2026-11-13")
    record(j, run=4, count=1)
    j.deliver("o/r", board)
    assert len(board.cards) == 3
    j.close()


def test_pending_run_descriptors_survive_window_and_completion(tmp_path):
    path = tmp_path / "queue.sqlite3"
    j = intake.Journal(path)
    run = {"id": 1, "run_attempt": 2, "head_sha": "a" * 40, "untrusted_output": "not persisted"}
    assert len(j.remember_runs("o/r", [run])) == 1
    j.close()
    j = intake.Journal(path)
    assert j.remember_runs("o/r", []) == [{k: v for k, v in run.items() if k != "untrusted_output"}]
    j.finish_run("o/r", "1/2")
    assert j.remember_runs("o/r", [run]) == []
    j.close()


def test_pending_runs_retried_despite_listing_outage_and_json_done(tmp_path, monkeypatch):
    from scripts.ci import flaky_kanban as fk

    journal = intake.Journal(tmp_path / "queue.sqlite3")
    run = {"id": 1, "run_attempt": 2, "head_sha": "a" * 40}
    journal.remember_runs("o/r", [run])
    state = {"runs": ["1/2"], "cards": {}}
    def unavailable(*args, **kwargs):
        raise OSError("unavailable")
    monkeypatch.setattr(fk, "_completed_runs", unavailable)
    seen = []
    def assess(repo, token, descriptor, board, state, relay, journal):
        seen.append(descriptor)
        return "empty", [], None
    monkeypatch.setattr(fk, "_poll_run", assess)
    lines = fk.poll_github("o/r", "unused", Board(), state, journal=journal)
    assert "FAILED runs_unavailable" in lines
    assert seen == [run]
    assert journal.remember_runs("o/r", []) == []
    journal.close()


def test_unverifiable_closed_foreign_candidate_is_admitted_with_named_candidate(tmp_path):
    """A clipped closed foreign history neither strands the row nor hides the candidate."""
    import sqlite3
    import time
    from scripts.ci import flaky_kanban as fk

    path = tmp_path / "hub.db"
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE replica (gw TEXT, key TEXT, coll TEXT, data TEXT)")
        conn.execute("INSERT INTO replica VALUES (?,?,?,?)", ("peer", "default/default/old", "tasks", json.dumps({
            "id": "old", "title": "Deflake tests/a.py::test_0", "status": "done", "body": "",
            "comments": 9, "last_comments": [{"body": "another job"}]})))
        conn.execute("INSERT INTO replica VALUES (?,?,?,?)", ("peer", "info", "gateway", json.dumps({
            "gateway": "peer", "task_scopes": [
                {"profile": "default", "board": "default", "state": "ok", "observed_at": time.time()}]})))

    class Relay(fk.HubRelay):
        def self_gateway(self):
            return "here"

        def comment(self, *args):
            pytest.fail("a closed candidate never receives the comment")

    journal = intake.Journal(tmp_path / "queue.sqlite3")
    journal.record("o/r", [(9, [{"test_id": "tests/a.py::test_0", "retried": "tests"}])],
                   {"id": 1, "head_sha": "a" * 40})
    board = Board()
    lines = fk.drain_intake(journal, "o/r", board, {"cards": {}}, Relay(path))
    assert not any(line.startswith("FAILED") for line in lines), lines
    assert journal.pending("o/r") == []
    assert journal.db.execute("SELECT sum(delivered) FROM occurrences").fetchone()[0] == 1
    [card] = board.cards.values()
    assert card["idempotency_key"].startswith("ci-flaky-intake:o/r:")
    assert "not inventoried" in card["body"]
    [payload] = board.notes[card["id"]]
    item = json.loads(payload.split("```json\n", 1)[1].split("\n```", 1)[0])["occurrences"][0]
    assert item["status"] == "needs_triage"
    assert item["owner_check"] == [
        {"candidate": "@task:default@peer/old", "status": "done", "reason": "closed_history_incomplete"}]
