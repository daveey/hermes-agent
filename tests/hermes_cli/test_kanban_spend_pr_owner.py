"""Open core publication owners keep counting spend without a new spend triage."""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch_pr_state as prs
from hermes_cli import kanban_spend_budget as spend
from tests.hermes_cli import test_kanban_spend_budget as helpers

kanban_home = helpers.kanban_home
_REAL_LOOKUP = prs._gh_pr_state
PR = "https://github.com/softmax-universe/daveey-hermes/pull/3598"


@pytest.fixture
def github(monkeypatch):
    prs.clear_cache()
    monkeypatch.setattr(prs, "_gh_pr_state", _REAL_LOOKUP)
    monkeypatch.setitem(helpers._CLOCK, "t", 0)
    calls = []
    data = {"state": "OPEN", "baseRefName": "main", "comments": []}

    def read(argv, **kwargs):
        calls.append(argv)
        return SimpleNamespace(returncode=0, stdout=json.dumps(data), stderr="")

    monkeypatch.setattr(prs.subprocess, "run", read)
    monkeypatch.setattr(prs, "_gh_argv", lambda: ["gh"])
    yield data, calls
    prs.clear_cache()


def owner(conn, *, key="published_pr", url=PR, runs=15):
    tid = kb.create_task(conn, title="Core work", assignee="default")
    helpers._runs(conn, tid, runs, usage={"cache_read_tokens": 1_000_000})
    conn.execute("UPDATE task_runs SET metadata=? WHERE task_id=?",
                 (json.dumps({key: url}), tid))
    return tid


def test_open_owner_spared_once_per_window(kanban_home, github):
    with kbc.connect_closing() as conn:
        tid = owner(conn)
        for _ in range(3):
            assert spend.hold(conn, tid) is None
        assert kb.get_task(conn, tid).status == "ready"
        assert helpers._events(conn, tid, spend.EVENT) == []
        (event,) = helpers._events(conn, tid, "spend_budget_spared")
        assert event.payload["pr"] == PR
        assert event.payload["runs"] == 15
        assert event.payload["tokens"]["cache_read_tokens"] == 15_000_000
        comments = [c for c in kb.list_comments(conn, tid) if c.author == spend.AUTHOR]
        assert len(comments) == 1 and PR in comments[0].body
        assert spend.spend(conn, tid).runs == 15
        assert len(github[1]) == 1
        assert "baseRefName" in github[1][0][-1]


@pytest.mark.parametrize("state,base,key,url", [
    ("MERGED", "main", "published_pr", PR),
    ("CLOSED", "main", "published_pr", PR),
    (None, "main", "published_pr", PR),
    ("OPEN", "state", "published_pr", PR),
    ("OPEN", None, "published_pr", PR),
    ("OPEN", "main", "reviewed_pr", PR),
    ("OPEN", "main", "published_pr", PR.replace("daveey-hermes", "other")),
    ("OPEN", "main", "published_pr", [PR]),
])
def test_nonowners_and_noncore_open_prs_hold(kanban_home, github, state, base, key, url):
    github[0].update(state=state, baseRefName=base)
    with kbc.connect_closing() as conn:
        tid = owner(conn, key=key, url=url)
        kb.add_comment(conn, tid, "reviewer", f"Review of {PR}: approved")
        assert spend.hold(conn, tid) == "triage"
        assert kb.get_task(conn, tid).status == "triage"
        assert len(helpers._events(conn, tid, spend.EVENT)) == 1
        assert helpers._events(conn, tid, "spend_budget_spared") == []


def test_existing_triage_is_not_released(kanban_home, github):
    with kbc.connect_closing() as conn:
        tid = owner(conn)
        github[0]["state"] = "MERGED"
        assert spend.hold(conn, tid) == "triage"
        github[0]["state"] = "OPEN"
        prs.clear_cache()
        spend.hold(conn, tid)
        assert kb.get_task(conn, tid).status == "triage"
        assert helpers._events(conn, tid, "spend_budget_spared") == []


def test_below_budget_does_not_query_github(kanban_home, github):
    with kbc.connect_closing() as conn:
        tid = owner(conn, runs=14)
        assert spend.hold(conn, tid) is None
        assert github[1] == []


@pytest.mark.parametrize("remote", [False, True])
def test_dispatch_paths_spare_owner(kanban_home, github, monkeypatch, remote):
    from hermes_cli import kanban_db_dispatch as dispatch
    from tests.hermes_cli import test_kanban_home_budget_dispatch as home

    with kbc.connect_closing() as conn:
        tid = owner(conn)
        if remote:
            home.remote_only(monkeypatch, conn, tid)
        spawns = helpers.Spawns()
        result = dispatch.dispatch_once(conn, spawn_fn=spawns)
        assert not result.no_progress_held
        assert (tid in spawns.ids) == (not remote)
        assert len(helpers._events(conn, tid, "spend_budget_spared")) == 1


@pytest.mark.parametrize("body_template", [
    "Published PR {pr} via canonical ship; required CI pending.",
    "Canonical ship published PR {pr} at exact head; CI pending.",
    "Published PR #3598 ({pr}) at head. Next: hermes-core-wt ship topic.",
    "Published via canonical ship: PR {pr}, exact head.",
    "Recovered prior worktree on this worker and published via canonical core-wt: PR {pr} exact head.",
    "Run42 canonical publication advanced work: PR #3598 {pr} OPEN at exact head.",
])
@pytest.mark.parametrize("provenance", ["owner", "foreign", "missing", "ambiguous"])
def test_canonical_publication_comment(kanban_home, github, provenance, body_template):
    with kbc.connect_closing() as conn:
        tid = owner(conn, key="reviewed_pr")
        run_id = conn.execute("SELECT id FROM task_runs WHERE task_id=? LIMIT 1", (tid,)).fetchone()[0]
        if provenance == "foreign":
            other = owner(conn)
            run_id = conn.execute("SELECT id FROM task_runs WHERE task_id=? LIMIT 1", (other,)).fetchone()[0]
        body = body_template.format(pr=PR)
        cid = kb.add_comment(conn, tid, "default", body)
        conn.execute("DELETE FROM task_events WHERE task_id=? AND kind='commented'", (tid,))
        if provenance != "missing":
            for _ in range(2 if provenance == "ambiguous" else 1):
                kb._append_event(conn, tid, "commented", {
                    "author": "default", "len": len(body), "comment_id": cid,
                    "writer_run_id": run_id,
                })
        assert spend.hold(conn, tid) == (None if provenance == "owner" else "triage")


def test_closed_after_open_enforces_without_resetting_count(kanban_home, github):
    with kbc.connect_closing() as conn:
        tid = owner(conn)
        assert spend.hold(conn, tid) is None
        prs.clear_cache()
        github[0]["state"] = "CLOSED"
        assert spend.hold(conn, tid) == "triage"
        assert helpers._events(conn, tid, spend.EVENT)[0].payload["runs"] == 15


def test_failed_refresh_does_not_reuse_old_open_base(kanban_home, github, monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(prs.time, "monotonic", lambda: clock[0])
    with kbc.connect_closing() as conn:
        tid = owner(conn)
        assert spend.hold(conn, tid) is None
        clock[0] += prs.OPEN_TTL_SECONDS + 1
        monkeypatch.setattr(prs.subprocess, "run", lambda *a, **k: SimpleNamespace(returncode=1, stderr=""))
        assert spend.hold(conn, tid) == "triage"


def test_new_spend_window_gets_new_notice(kanban_home, github):
    import time

    with kbc.connect_closing() as conn:
        tid = owner(conn)
        assert spend.hold(conn, tid) is None
        now = int(time.time())
        conn.execute("UPDATE task_runs SET started_at=?, ended_at=? WHERE task_id=?",
                     (now - 500, now - 100, tid))
        kb._append_event(conn, tid, "commented", {"writer_kind": "human"})
        conn.execute("UPDATE task_events SET created_at=? WHERE task_id=? AND kind='commented'",
                     (now - 1000, tid))
        assert spend.hold(conn, tid) is None
        assert len(helpers._events(conn, tid, "spend_budget_spared")) == 2
        assert len([c for c in kb.list_comments(conn, tid) if "Spend budget spared:" in c.body]) == 2


@pytest.mark.parametrize("control", ["foreign", "exempt", "dry_run"])
def test_owner_controls_do_not_query_or_record(kanban_home, github, monkeypatch, control):
    from hermes_cli import kanban_db_dispatch as dispatch
    from tests.hermes_cli import test_kanban_home_budget_dispatch as home

    with kbc.connect_closing() as conn:
        tid = owner(conn)
        home.remote_only(monkeypatch, conn, tid)
        if control == "foreign":
            conn.execute("UPDATE tasks SET created_on='elsewhere' WHERE id=?", (tid,))
        if control == "exempt":
            spend.set_override(conn, tid, runs=0, cache_read_tokens=0, by="operator", reason="test")
        dispatch.dispatch_once(conn, spawn_fn=helpers.Spawns(), dry_run=control == "dry_run")
        assert helpers._events(conn, tid, "spend_budget_spared") == []
        assert github[1] == []


@pytest.mark.parametrize("body", [
    f"Published review of PR {PR}; canonical ship remains the author's responsibility.",
    f"Canonical ship instructions:\n```\nPublished PR {PR}\n```\nI only reviewed it.",
    f"Review of {PR}; published via canonical ship by the implementer.",
    f"Published PR {PR} after review.",
])
def test_own_review_mentions_are_not_publication(kanban_home, github, body):
    with kbc.connect_closing() as conn:
        tid = owner(conn, key="reviewed_pr")
        run_id = conn.execute("SELECT id FROM task_runs WHERE task_id=? LIMIT 1", (tid,)).fetchone()[0]
        cid = kb.add_comment(conn, tid, "default", body)
        conn.execute("DELETE FROM task_events WHERE task_id=? AND kind='commented'", (tid,))
        kb._append_event(conn, tid, "commented", {
            "author": "default", "len": len(body), "comment_id": cid, "writer_run_id": run_id})
        assert spend.hold(conn, tid) == "triage"
        assert github[1] == []


def test_closed_pr_reopen_is_observed_after_shared_ttl(kanban_home, github, monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(prs.time, "monotonic", lambda: clock[0])
    github[0]["state"] = "CLOSED"
    assert prs.pr_details(PR) == ("CLOSED", "main")
    github[0]["state"] = "OPEN"
    clock[0] += prs.OPEN_TTL_SECONDS + 1
    with kbc.connect_closing() as conn:
        tid = owner(conn)
        assert spend.hold(conn, tid) is None
        assert len(github[1]) == 2


def test_no_progress_still_holds_open_owner(kanban_home, github, monkeypatch):
    from hermes_cli import kanban_db_dispatch as dispatch
    from hermes_cli import kanban_no_progress as progress
    from tests.hermes_cli import test_kanban_no_progress as idle
    from tests.hermes_cli import test_kanban_home_budget_dispatch as home

    with kbc.connect_closing() as conn:
        tid, _ = idle._shepherd(conn, progress.DEFAULT_LIMIT)
        conn.execute("UPDATE task_runs SET metadata=? WHERE task_id=?", (json.dumps({"published_pr": PR}), tid))
        home.remote_only(monkeypatch, conn, tid)
        result = dispatch.dispatch_once(conn, spawn_fn=helpers.Spawns())
        assert (tid, "parked") in result.no_progress_held
        assert helpers._events(conn, tid, "spend_budget_spared") == []


@pytest.mark.parametrize("status", ["triage", "done", "running"])
def test_nonlane_owner_writes_no_spared_notice(kanban_home, github, status):
    with kbc.connect_closing() as conn:
        tid = owner(conn)
        conn.execute("UPDATE tasks SET status=? WHERE id=?", (status, tid))
        spend.hold(conn, tid)
        assert kb.get_task(conn, tid).status == status
        assert helpers._events(conn, tid, "spend_budget_spared") == []
