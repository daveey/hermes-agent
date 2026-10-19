"""review_wait stamps: sessions waiting on a code review go red, or grey while
only other reviewers were asked in the last 24h. [daveey]"""

from __future__ import annotations

import sqlite3
import time

import pytest

from hermes_cli import review_session_activity as rsa

PR = "https://github.com/Acme/widgets/pull/42"
KEY = ("acme", "widgets", 42)
NOW = 1_800_000_000.0


def _gql(*, state="OPEN", decision="REVIEW_REQUIRED", author="me", requested=(), requested_at=None,
         draft=False, created="2026-10-19T19:00:00Z"):
    return {"data": {"viewer": {"login": "me"}, "repository": {"pullRequest": {
        "state": state, "isDraft": draft, "reviewDecision": decision, "createdAt": created,
        "author": {"login": author},
        "reviewRequests": {"nodes": [{"requestedReviewer": {"__typename": t, "login": n}} for t, n in requested]},
        "timelineItems": {"nodes": [
            {"createdAt": requested_at, "requestedReviewer": {"__typename": t, "login": n}}
            for t, n in requested if requested_at]},
    }}}}


def _iso(ts: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


def test_pr_keys_dedupes_and_lowercases():
    text = f"see {PR} and {PR.upper().replace('HTTPS', 'https')} plus https://github.com/a/b/pull/7"
    assert rsa.pr_keys(text) == [KEY, ("a", "b", 7)]
    assert rsa.pr_keys("https://github.com/a/b/issues/7") == []


@pytest.mark.parametrize("requested,age_s,expected", [
    ((("User", "me"),), 60, "stuck"),             # review asked of me
    ((), None, "stuck"),                          # nobody asked: on me
    ((("User", "alice"),), 3600, "parked"),       # others, < 24h: grey
    ((("Team", "core"),), 3600, "parked"),        # a team counts as others
    ((("User", "alice"),), 25 * 3600, "stuck"),   # others, > 24h: red
])
def test_review_state_my_pr(requested, age_s, expected):
    at = _iso(NOW - age_s) if age_s is not None else None
    info = rsa.parse_pr(_gql(requested=requested, requested_at=at))
    assert rsa.review_state(info, NOW) == expected


def test_review_state_parks_from_pr_creation_without_request_event():
    info = rsa.parse_pr(_gql(requested=(("User", "alice"),), created=_iso(NOW - 600)))
    assert rsa.review_state(info, NOW) == "parked"


@pytest.mark.parametrize("kw", [
    {"state": "MERGED"}, {"state": "CLOSED"}, {"draft": True},
    {"decision": "APPROVED"}, {"decision": None}, {"decision": "CHANGES_REQUESTED"},
    {"author": "bob", "requested": (("User", "alice"),)},  # someone else's PR, not asking me
])
def test_review_state_not_waiting(kw):
    assert rsa.review_state(rsa.parse_pr(_gql(**kw)), NOW) is None


def test_someone_elses_pr_requesting_me_is_stuck():
    info = rsa.parse_pr(_gql(author="bob", requested=(("User", "me"),), requested_at=_iso(NOW - 60)))
    assert rsa.review_state(info, NOW) == "stuck"


@pytest.fixture
def store(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    db = sqlite3.connect(home / "state.db")
    db.execute("create table messages (id integer primary key, session_id text, role text, content text)")
    db.commit()
    rsa._reset_cache_for_tests()
    monkeypatch.setattr(rsa, "_sync_fetch", True)
    yield db
    rsa._reset_cache_for_tests()
    db.close()


def _say(db, sid, role, content):
    db.execute("insert into messages (session_id, role, content) values (?,?,?)", (sid, role, content))
    db.commit()


def test_annotate_stamps_from_latest_assistant_message(store, monkeypatch):
    calls = []

    def fetch(key):
        calls.append(key)
        return rsa.parse_pr(_gql(requested=(("User", "alice"),), requested_at=_iso(time.time() - 60)))

    monkeypatch.setattr(rsa, "_fetch_override", fetch)
    _say(store, "s1", "assistant", f"PR is up: {PR}")
    _say(store, "s2", "assistant", f"old {PR}")
    _say(store, "s2", "assistant", "all merged, nothing pending")
    _say(store, "s3", "user", f"look at {PR}")  # user text alone doesn't count
    rows = [{"id": "s1", "last_active": 1, "message_count": 1},
            {"id": "s2", "last_active": 1, "message_count": 2},
            {"id": "s3", "last_active": 1, "message_count": 1}]
    rsa.annotate_review_wait(rows)
    assert rows[0]["review_wait"] == "parked"
    assert rows[0]["review_wait_pr"] == "https://github.com/acme/widgets/pull/42"
    assert "review_wait" not in rows[1] and "review_wait" not in rows[2]
    # Cached: a second poll doesn't refetch the PR.
    rsa.annotate_review_wait(rows)
    assert calls == [KEY]


def test_annotate_reads_the_newest_message_across_the_lineage(store, monkeypatch):
    monkeypatch.setattr(rsa, "_fetch_override", lambda key: rsa.parse_pr(_gql()))
    _say(store, "root", "assistant", "starting")
    _say(store, "tip", "assistant", f"waiting on review {PR}")
    rows = [{"id": "root", "_lineage_ids": ["root", "tip"], "last_active": 2, "message_count": 2}]
    rsa.annotate_review_wait(rows)
    assert rows[0]["review_wait"] == "stuck"


def test_annotate_clears_stale_stamp_and_survives_fetch_errors(store, monkeypatch):
    def boom(key):
        raise RuntimeError("gh: not logged in")

    monkeypatch.setattr(rsa, "_fetch_override", boom)
    _say(store, "s1", "assistant", PR)
    rows = [{"id": "s1", "last_active": 1, "message_count": 1, "review_wait": "stuck"}]
    rsa.annotate_review_wait(rows)
    assert "review_wait" not in rows[0]


def test_annotate_does_not_block_on_github(store, monkeypatch):
    import threading

    gate = threading.Event()

    def slow(key):
        gate.wait(5)
        return rsa.parse_pr(_gql())

    monkeypatch.setattr(rsa, "_sync_fetch", False)
    monkeypatch.setattr(rsa, "_fetch_override", slow)
    _say(store, "s1", "assistant", PR)
    rows = [{"id": "s1", "last_active": 1, "message_count": 1}]
    t0 = time.monotonic()
    rsa.annotate_review_wait(rows)
    assert time.monotonic() - t0 < 1.0
    assert "review_wait" not in rows[0]  # first poll: lookup still in flight
    gate.set()
    for _ in range(50):
        if KEY in rsa._pr_cache:
            break
        time.sleep(0.05)
    rsa.annotate_review_wait(rows)
    assert rows[0]["review_wait"] == "stuck"
