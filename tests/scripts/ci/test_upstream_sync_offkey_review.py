"""Off-key receipts discover candidates; only ship's independent gate may approve them."""
import json
import sqlite3
from types import SimpleNamespace

import pytest

from scripts.ci import upstream_sync as sync
from scripts.ci import upstream_sync_review as review
from tests.scripts.ci import test_upstream_sync_review as helpers
from tests.scripts.ci.test_upstream_sync import gate

world = helpers.world
land_stub = helpers.land_stub
CARD = "t_1234567890abcdef"
SHA = "a" * 40
URL = "https://github.com/example/fork/pull/7"


def board(path, *, metadata=None, task_status="done", run_status="done", outcome="completed", card=CARD):
    with sqlite3.connect(path) as db:
        db.executescript("CREATE TABLE tasks (id TEXT, status TEXT, idempotency_key TEXT); "
                         "CREATE TABLE task_runs (task_id TEXT, status TEXT, outcome TEXT, metadata TEXT, ended_at INT);")
        db.execute("INSERT INTO tasks VALUES (?, ?, ?)", (card, task_status, "unrelated:review"))
        md = metadata if metadata is not None else {"verdict": "approve", "reviewed_sha": SHA, "reviewed_pr": URL}
        db.execute("INSERT INTO task_runs VALUES (?, ?, ?, ?, 1)", (card, run_status, outcome, json.dumps(md)))
    return SimpleNamespace(board_db_paths=lambda: [path])


@pytest.mark.parametrize("mismatch", [None, "other_sha", "request_changes"])
def test_offkey_exact_head_approval_lands_without_duplicate(world, land_stub, tmp_path, mismatch):
    root, checkout = world
    head = helpers.fixup_sync(root, checkout)
    actions = helpers.staged_green(root, checkout, head)
    pr = {"number": 1}  # gate creates its first landing PR after staging CI is green
    slug = "example/fork"
    board(tmp_path / "land-state.db", metadata={
        "verdict": "request_changes" if mismatch == "request_changes" else "approve",
        "reviewed_sha": "0" * 40 if mismatch == "other_sha" else head,
        "reviewed_pr": f"https://github.com/{slug}/pull/{pr['number']}"})
    land_stub.update(approve={} if mismatch else {CARD: head})
    assert gate(checkout, head, actions) == (sync.NOT_YET if mismatch else sync.LANDED)
    state = land_stub.read()
    assert len(state["filed"]) == (1 if mismatch else 0)
    assert state["gates"][0][0] == ("" if mismatch == "other_sha" else CARD)


@pytest.mark.parametrize("change", [
    {"reviewed_sha": "b" * 40},
    {"reviewed_pr": URL + "0"}, {"reviewed_sha": SHA[:10]},
    {"verdict": "APPROVE"}, {"reviewed_pr": {URL: True}},
])
def test_offkey_wrong_receipt_not_discovered(tmp_path, change):
    md = {"verdict": "approve", "reviewed_sha": SHA, "reviewed_pr": URL, **change}
    mod = board(tmp_path / "board.db", metadata=md)
    assert review.off_key_review_cards(mod, "example/fork", 7, SHA) == []


@pytest.mark.parametrize("fields", [
    {"task_status": "running"}, {"run_status": "running"}, {"outcome": "blocked"},
    {"metadata": []}, {"card": "not-a-task"},
])
def test_offkey_requires_done_completed_record(tmp_path, fields):
    mod = board(tmp_path / "board.db", **fields)
    assert review.off_key_review_cards(mod, "example/fork", 7, SHA) == []


def test_offkey_full_sha_and_replica_dedup(tmp_path):
    path = tmp_path / "board.db"
    mod = board(path)
    mod.board_db_paths = lambda: [tmp_path / "missing.db", path, path]
    assert review.off_key_review_cards(mod, "example/fork", 7, SHA) == [CARD]
    assert review.off_key_review_cards(mod, "example/fork", 7, SHA[:10]) == []
    with sqlite3.connect(path) as db:
        db.execute("UPDATE task_runs SET metadata = '{broken'")
    assert review.off_key_review_cards(mod, "example/fork", 7, SHA) == []


@pytest.mark.parametrize("sha, expected", [
    pytest.param(SHA, [CARD], id="full-lowercase-control"),
    pytest.param(SHA[:10], [], id="short"),
    pytest.param(SHA + "a", [], id="long"),
    pytest.param(SHA.upper(), [], id="uppercase"),
    pytest.param("g" * 40, [], id="nonhex"),
    pytest.param(SHA + "\n", [], id="trailing-newline"),
])
def test_offkey_sha_shape_with_matching_receipt(tmp_path, sha, expected):
    # Match both sides so receipt equality cannot mask a missing caller-shape guard.
    mod = board(tmp_path / "board.db", metadata={
        "verdict": "approve", "reviewed_sha": sha, "reviewed_pr": URL,
    })
    assert review.off_key_review_cards(mod, "example/fork", 7, sha) == expected


def test_offkey_discovery_is_not_approval_and_preserves_impl_identity(land_stub, tmp_path):
    board(tmp_path / "land-state.db")
    checker = review.ShipReview(tmp_path, impl_task="t_implementer")
    accepted, _ = checker.check("example/fork", 7, SHA, "sync/head")
    assert not accepted  # authoritative ship gate refuses the otherwise matching receipt
    assert land_stub.read()["gates"][0] == [CARD, 7, SHA, "sync/head", "main", "t_implementer"]
    assert len(land_stub.read()["filed"]) == 1


@pytest.mark.parametrize("reject_first", [True, False])
@pytest.mark.parametrize("rejection", ["ReviewRejected", "ReviewChangesRequested"])
def test_offkey_rejection_blocks_other_approval(tmp_path, monkeypatch, reject_first, rejection):
    mod = board(tmp_path / "board.db")
    rejected = "t_abcdef"
    with sqlite3.connect(tmp_path / "board.db") as db:
        db.execute("INSERT INTO tasks VALUES (?, 'done', 'another-key')", (rejected,))
        db.execute("INSERT INTO task_runs VALUES (?, 'done', 'completed', ?, ?)", (
            rejected, json.dumps({"verdict": "request_changes", "reviewed_sha": SHA,
                                 "reviewed_pr": URL}), 2 if reject_first else 0))
    class Refused(Exception):
        pass
    class Rejected(Refused):
        pass
    class ChangesRequested(Refused):
        pass
    mod.ReviewRefused = Refused
    mod.ReviewRejected = Rejected
    mod.ReviewChangesRequested = ChangesRequested
    mod.ReviewCIStopped = type("CIStopped", (Exception,), {})
    mod.Gh = lambda repo: None
    mod.keyed_review_card = lambda *args: ""
    calls = []
    def review_gate(gh, slug, pr, sha, branch, base, card, **kwargs):
        calls.append(card)
        if card == rejected:
            raise getattr(mod, rejection)("exact-head rejection")
        return {"id": card, "source": "kanban"}
    mod.review_gate = review_gate
    mod.review_reply_guard = lambda *args: (None, "")
    mod.request_review = lambda *args, **kwargs: pytest.fail("must not file past rejection")
    checker = review.ShipReview(tmp_path)
    monkeypatch.setattr(checker, "_module", lambda: mod)
    accepted, reason = checker.check("example/fork", 7, SHA, "sync/head")
    assert not accepted
    assert "exact-head rejection" in reason
    assert rejected in calls


@pytest.mark.parametrize("reply", ["", "follow-up t_abcdef1234567890"])
def test_every_offkey_approval_requires_reply_guard(land_stub, tmp_path, monkeypatch, reply):
    path = tmp_path / "land-state.db"
    board(path)
    second = "t_fedcba0987654321"
    with sqlite3.connect(path) as db:
        db.execute("INSERT INTO tasks VALUES (?, 'done', 'second-key')", (second,))
        db.execute("INSERT INTO task_runs VALUES (?, 'done', 'completed', ?, 0)", (
            second, json.dumps({"verdict": "approve", "reviewed_sha": SHA, "reviewed_pr": URL})))
    land_stub.update(approve={CARD: SHA, second: SHA})
    checker = review.ShipReview(tmp_path, impl_task="t_implementer", reply=reply)
    mod = checker._module()
    original_gate, original_guard = mod.review_gate, mod.review_reply_guard
    guarded = []

    def review_gate(*args, **kwargs):
        receipt = original_gate(*args, **kwargs)
        if receipt["id"] == second:
            receipt["non_blocking"] = ["Nit (non-blocking): behavioral guard missing on later receipts"]
        return receipt

    def reply_guard(gh, slug, pr, receipt, answer):
        guarded.append(receipt["id"])
        return original_guard(gh, slug, pr, receipt, answer)

    monkeypatch.setattr(mod, "review_gate", review_gate)
    monkeypatch.setattr(mod, "review_reply_guard", reply_guard)
    monkeypatch.setattr(checker, "_module", lambda: mod)
    accepted, reason = checker.check("example/fork", 7, SHA, "sync/head")
    assert accepted is bool(reply)
    assert guarded == [CARD, second]
    if not reply:
        assert "unanswered non-blocking" in reason
        assert "--review-reply" in reason
    state = land_stub.read()
    assert [row[0] for row in state["gates"]] == [CARD, second, ""]
    assert all(row[-1] == "t_implementer" for row in state["gates"])
    assert state["filed"] == []


def test_success_detail_names_first_offkey_receipt_before_github(land_stub, tmp_path, monkeypatch):
    board(tmp_path / "land-state.db")
    land_stub.update(approve={CARD: SHA})
    checker = review.ShipReview(tmp_path)
    mod = checker._module()
    original_gate = mod.review_gate
    receipts = []

    def review_gate(*args, **kwargs):
        if args[6]:
            receipt = original_gate(*args, **kwargs)
        else:
            receipt = {"source": "github", "id": 12345, "reviewed_sha": SHA, "verdict": "approve"}
        receipts.append(receipt)
        return receipt

    monkeypatch.setattr(mod, "review_gate", review_gate)
    monkeypatch.setattr(checker, "_module", lambda: mod)
    accepted, detail = checker.check("example/fork", 7, SHA, "sync/head")

    assert [(r["source"], r["id"]) for r in receipts] == [("kanban", CARD), ("github", 12345)]
    assert accepted is True
    assert detail == f"independent exact-head review kanban {CARD} approves {SHA}"
    assert land_stub.read()["filed"] == []


def test_keyed_refusal_cannot_be_bypassed_by_offkey(land_stub, tmp_path):
    board(tmp_path / "land-state.db")
    land_stub.update(cards={f"example/fork#7:{SHA}": "t_keyed"}, approve={CARD: SHA})
    accepted, _ = review.ShipReview(tmp_path).check("example/fork", 7, SHA, "sync/head")
    assert not accepted
    assert [row[0] for row in land_stub.read()["gates"]] == ["t_keyed"]
