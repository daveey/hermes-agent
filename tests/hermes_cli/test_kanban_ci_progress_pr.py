"""A CI wait on a pull request must notice when no check can start. [daveey]

Replays 2026-11-13: t_01a1d5dae3fe181b pushed state PR #2495 head 7072089bad and parked (awaiting kind=ci) on
``https://github.com/softmax-universe/daveey-hermes/pull/2495 checks @7072089bad...``. The PR conflicted with
``state``, so GitHub started no ``pull_request`` workflow for that head (0 runs), and ``kanban_ci_progress`` left the
PR ref ``untracked``: the owner would have slept until its 1 h cap on a producer that could not fire.

GitHub is stubbed at ``kanban_ci_progress._gh_api`` (the one broker-prefixed read every check goes through), serving
the PR, the head's workflow runs and their jobs as the REST API returns them.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from hermes_cli import kanban_ci_progress as kcp
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_shared_blockers as ksb
from hermes_cli import kanban_wake_check as kwc

REPO = "softmax-universe/daveey-hermes"
PR = 2495
HEAD = "7072089bad288a86dc638ba386e6d12fc5d32e7d"
OTHER = "6c627f9f0e0000000000000000000000000000ab"
BASE = "aaaf75adb60000000000000000000000000000cd"
REF = f"https://github.com/{REPO}/pull/{PR} checks @{HEAD}"  # verbatim from the incident's park
RUN_ID = "43400000001"
LABELS = ["self-hosted", "ci-fast"]
# GitHub can report a conflict in either field while the other is still settling (PR #2149's first read gave
# mergeable=None mergeable_state=unknown while gh pr list showed it CONFLICTING/DIRTY), so each half must count.
CONFLICTS = pytest.mark.parametrize("mergeable,state", [(False, "dirty"), (False, "unknown"), (None, "dirty")],
                                    ids=["both", "mergeable_false", "state_dirty"])


def _iso(age: int) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - age))


@pytest.fixture
def kanban_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_WAKE_DRYRUN", "0")
    monkeypatch.setenv("ASK_GATE_LEDGER", str(home / "state" / "ask-ledger.sqlite"))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    yield home
    kwc._shutdown_for_tests()
    for state in (kcp._LAST_CHECK, kcp._LAST_SWEEP, kcp._SWEEPS, kcp._POOL_SCANS, kcp._WARNED_AT):
        state.clear()


class Api:
    """GitHub as ``gh api <path>`` answers it: the PR, the head's runs, their jobs. Runner lists are 403 (a token
    without admin scope), and the repo's recent-run lists are empty."""

    def __init__(self) -> None:
        self.pr: dict = {"number": PR, "state": "open", "merged": False, "mergeable": True,
                         "mergeable_state": "clean", "head": {"sha": HEAD}, "base": {"ref": "state"},
                         "updated_at": _iso(60)}
        self.runs: list[dict] = []
        self.jobs: dict[str, list[dict]] = {}
        self.fail = False
        self.calls: list[str] = []

    def dirty(self, mergeable: bool | None = False, state: str = "dirty") -> None:
        self.pr.update(mergeable=mergeable, mergeable_state=state)

    def run(self, status: str, *, age: int, runner: str | None = None) -> None:
        self.runs = [{"id": int(RUN_ID), "status": status, "event": "pull_request", "head_sha": HEAD,
                      "created_at": _iso(age)}]
        self.jobs[RUN_ID] = [{"id": 1, "run_id": int(RUN_ID), "name": "ci", "created_at": _iso(age),
                              "status": "queued" if status == "queued" else "in_progress",
                              "started_at": None if status == "queued" else _iso(age),
                              "labels": LABELS, "runner_name": runner}]

    def __call__(self, path: str) -> dict:
        self.calls.append(path)
        if self.fail:
            raise RuntimeError("gh api exit 1: HTTP 502: Bad Gateway (https://api.github.com/)")
        if path == f"repos/{REPO}/pulls/{PR}":
            return json.loads(json.dumps(self.pr))
        if path.startswith(f"repos/{REPO}/actions/runs?head_sha="):
            sha = path.split("head_sha=", 1)[1].split("&", 1)[0]
            runs = [r for r in self.runs if r["head_sha"] == sha]
            return {"total_count": len(runs), "workflow_runs": runs}
        if path.startswith(f"repos/{REPO}/actions/runs/") and "/jobs" in path:
            run_id = path.split("/runs/", 1)[1].split("/", 1)[0]
            return {"total_count": len(self.jobs.get(run_id, [])), "jobs": self.jobs.get(run_id, [])}
        if "/actions/runners" in path:
            raise RuntimeError("gh api exit 1: HTTP 403: Resource not accessible by integration")
        if "/actions/runs?status=" in path:
            return {"total_count": 0, "workflow_runs": []}
        raise AssertionError(f"unexpected gh api {path}")


@pytest.fixture
def api(monkeypatch: pytest.MonkeyPatch) -> Api:
    fake = Api()
    monkeypatch.setattr(kcp, "_gh_api", fake)
    return fake


def _running(conn) -> str:
    tid = kb.create_task(conn, title="t", assignee="worker")
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (tid,))
    assert kb.claim_task(conn, tid, claimer="worker") is not None
    return tid


def _park(monkeypatch, conn, tid, ref: str = REF, *, reason: str = f"Waiting for CI on PR #{PR} head {HEAD[:10]}.",
          wake_check: str = f"false gh pr checks {PR} -R {REPO}") -> dict:
    from tools import kanban_tools as kt
    monkeypatch.setenv("HERMES_KANBAN_TASK", tid)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(kb.get_task(conn, tid).current_run_id))
    try:
        return json.loads(kt._handle_schedule({
            "reason": reason, "wake_check": wake_check,
            "awaiting": {"kind": "ci", "ref": ref, "why": "state CI on the fix"}}))
    finally:
        monkeypatch.delenv("HERMES_KANBAN_TASK")


def _parked(conn, monkeypatch, *, ago: int = 0, **park) -> str:
    """A card parked on the PR while it read clean (accepted), its park ``ago`` seconds back."""
    tid = _running(conn)
    out = _park(monkeypatch, conn, tid, **park)
    assert out.get("status") == "scheduled", out
    if ago:
        with kb.write_txn(conn):
            conn.execute("UPDATE task_events SET created_at = created_at - ? WHERE task_id = ? "
                         "AND kind IN ('scheduled', 'continued')", (ago, tid))
    return tid


def _notes(conn, tid) -> list[str]:
    return [c.body for c in kb.list_comments(conn, tid) if c.author == kcp.AUTHOR]


def _runner_blockers(conn) -> list[str]:
    return [b for b in ksb.blocker_states(conn) if b.startswith(kcp.BLOCKER_PREFIX)]


# --- the ref ----------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("ref,hint,want", [
    (REF, "", (REPO, PR, HEAD)),
    (f"{REPO}/pull/{PR}", "", (REPO, PR, None)),
    (f"https://github.com/{REPO}/pull/{PR}@7072089", "", (REPO, PR, "7072089")),
    (f"https://github.com/{REPO}/pull/{PR}", f"head is {HEAD}", (REPO, PR, HEAD)),
    (f"https://github.com/{REPO}/pull/{PR}", f"head {HEAD.upper()} (was {HEAD})", (REPO, PR, HEAD)),
    # several distinct shas: no guess (it could be an old head or the base), so the current head is read
    (f"https://github.com/{REPO}/pull/{PR}", f"head {HEAD} onto base {BASE}", (REPO, PR, None)),
    (f"https://github.com/{REPO}/pull/{PR} {OTHER} {HEAD}", "", (REPO, PR, None)),
    # the ref's own sha wins over others in the hint; an @sha wins over everything
    (f"https://github.com/{REPO}/pull/{PR} checks {HEAD}", f"rebased from {OTHER}", (REPO, PR, HEAD)),
    (f"https://github.com/{REPO}/pull/{PR}@{HEAD} (was {OTHER})", f"base {BASE}", (REPO, PR, HEAD)),
    (f"https://github.com/{REPO}/actions/runs/{RUN_ID}", "", None),  # a run keeps the run path
    ("t_01a1d5dae3fe181b", "", None),
])
def test_pr_target(ref, hint, want) -> None:
    assert kcp.pr_target(ref, hint) == want


def test_pr_target_ref_with_two_shas_ignores_the_hint() -> None:
    """Post-merge check of PR #2616: a ref that itself names two shas reads the PR's current head. It never falls
    through to the hint's one sha, which may be an old head the ref's own text already disagrees with."""
    ref = f"https://github.com/{REPO}/pull/{PR} {OTHER} {BASE}"
    assert kcp.pr_target(ref, f"head is {HEAD}") == (REPO, PR, None)


# --- a. park time -------------------------------------------------------------------------------------------------

@CONFLICTS
def test_park_on_a_conflicting_pr_is_refused_naming_the_pr_and_rebase(kanban_home, api, monkeypatch, mergeable,
                                                                      state) -> None:
    api.dirty(mergeable, state)
    with kbc.connect_closing() as conn:
        tid = _running(conn)
        out = _park(monkeypatch, conn, tid)
        err = out.get("error") or ""
        assert kcp.CI_RUN_UNSTARTABLE in err, out
        assert f"#{PR}" in err and "rebase" in err and "state" in err, out
        assert kb.get_task(conn, tid).status == "running"


def test_park_on_a_fresh_head_with_no_runs_yet_is_allowed(kanban_home, api, monkeypatch) -> None:
    api.pr["mergeable"], api.pr["mergeable_state"] = None, "unknown"
    with kbc.connect_closing() as conn:
        tid = _running(conn)
        assert _park(monkeypatch, conn, tid).get("status") == "scheduled"


# --- b. the PR turns dirty while parked ---------------------------------------------------------------------------

@CONFLICTS
def test_sweep_wakes_a_card_whose_pr_turns_dirty_once_and_asserts_no_runner_blocker(kanban_home, api, monkeypatch,
                                                                                     mergeable, state) -> None:
    api.run("in_progress", age=60, runner="ci-fast-1")
    with kbc.connect_closing() as conn:
        tid = _parked(conn, monkeypatch)
        api.dirty(mergeable, state)
        assert kcp.sweep(conn) == [tid]
        assert kb.get_task(conn, tid).status == "ready"
        notes = _notes(conn, tid)
        assert len(notes) == 1, notes
        assert f"PR #{PR} conflicts with state" in notes[0] and "rebase" in notes[0], notes
        assert _runner_blockers(conn) == []
        kinds = [e.kind for e in kb.list_events(conn, tid)]
        assert kcp.CI_RUN_UNSTARTABLE in kinds and ksb.PROBE_RELEASED_EVENT not in kinds, kinds
        kcp._LAST_CHECK.clear()
        assert kcp.sweep(conn) == []
        assert len(_notes(conn, tid)) == 1


# --- c/d. runs present ----------------------------------------------------------------------------------------------

def test_null_mergeable_with_a_running_check_does_not_wake(kanban_home, api, monkeypatch, caplog) -> None:
    api.run("in_progress", age=20 * 60, runner="ci-fast-1")
    with kbc.connect_closing() as conn:
        tid = _parked(conn, monkeypatch, ago=30 * 60)
        api.pr["mergeable"], api.pr["mergeable_state"] = None, "unknown"
        with caplog.at_level("WARNING"):
            assert kcp.sweep(conn) == []
        assert kb.get_task(conn, tid).status == "scheduled"
        assert not [r for r in caplog.records if r.levelname == "WARNING"], caplog.text
    p = kcp.pr_progress(REPO, PR, HEAD, time.time() - 30 * 60)
    assert p.state == "unknown", p


def test_clean_pr_with_a_running_check_does_not_wake(kanban_home, api, monkeypatch) -> None:
    api.run("in_progress", age=20 * 60, runner="ci-fast-1")
    with kbc.connect_closing() as conn:
        tid = _parked(conn, monkeypatch, ago=30 * 60)
        assert kcp.sweep(conn) == []
        assert kb.get_task(conn, tid).status == "scheduled"
    assert kcp.pr_progress(REPO, PR, HEAD, time.time() - 30 * 60).state == "ok"


def test_clean_pr_with_a_run_queued_past_the_limit_takes_the_runners_offline_path(kanban_home, api,
                                                                                    monkeypatch) -> None:
    api.run("queued", age=2 * 60)
    with kbc.connect_closing() as conn:
        tid = _parked(conn, monkeypatch)
        api.run("queued", age=20 * 60)
        assert kcp.sweep(conn) == [tid]
        assert _runner_blockers(conn) == ["runners-offline.ci-fast"]
        assert tid in ksb.blocker_states(conn)["runners-offline.ci-fast"].behind
        notes = _notes(conn, tid)
        assert any(f"CI run {RUN_ID} queued 20 min: no runner picked it up" in n for n in notes), notes


# --- e. no run was ever created -----------------------------------------------------------------------------------

@pytest.mark.parametrize("mergeable", [True, None], ids=["clean", "null"])
def test_no_run_for_the_head_15_min_after_the_park_wakes(kanban_home, api, monkeypatch, mergeable) -> None:
    api.pr["updated_at"] = _iso(20 * 60)
    with kbc.connect_closing() as conn:
        tid = _parked(conn, monkeypatch, ago=16 * 60)
        api.pr["mergeable"] = mergeable
        api.pr["mergeable_state"] = "clean" if mergeable else "unknown"
        assert kcp.sweep(conn) == [tid]
        notes = _notes(conn, tid)
        assert len(notes) == 1 and f"no CI run was created for {HEAD[:10]}" in notes[0], notes
        assert f"mergeable={mergeable}" in notes[0], notes
        assert _runner_blockers(conn) == []


def test_no_run_soon_after_the_park_or_the_push_does_not_wake(kanban_home, api, monkeypatch) -> None:
    with kbc.connect_closing() as conn:
        tid = _parked(conn, monkeypatch, ago=5 * 60)
        assert kcp.sweep(conn) == []
        assert kb.get_task(conn, tid).status == "scheduled"
        # Parked long ago, but the head was pushed (PR updated) 5 min back: the clock starts at the later one.
        api.pr["updated_at"] = _iso(5 * 60)
        assert kcp.pr_progress(REPO, PR, HEAD, time.time() - 60 * 60).state != kcp.NOT_STARTING


# --- f. the head moved, or the PR is done ---------------------------------------------------------------------------

@pytest.mark.parametrize("change", ["head-moved", "merged", "closed"])
def test_moved_head_or_finished_pr_does_not_wake(kanban_home, api, monkeypatch, change) -> None:
    with kbc.connect_closing() as conn:
        tid = _parked(conn, monkeypatch, ago=30 * 60)
        api.dirty()
        if change == "head-moved":
            api.pr["head"]["sha"] = OTHER
        elif change == "merged":
            api.pr.update(state="closed", merged=True)
        else:
            api.pr.update(state="closed")
        assert kcp.sweep(conn) == []
        assert kb.get_task(conn, tid).status == "scheduled"
        assert _notes(conn, tid) == []
    assert kcp.pr_progress(REPO, PR, HEAD, time.time() - 30 * 60).state == "ok"


# --- g. GitHub errors fail open -------------------------------------------------------------------------------------

def test_github_error_allows_the_park_and_wakes_nothing(kanban_home, api, monkeypatch, caplog) -> None:
    api.fail = True
    with kbc.connect_closing() as conn:
        tid = _running(conn)
        with caplog.at_level("WARNING"):
            assert _park(monkeypatch, conn, tid).get("status") == "scheduled"
        warned = [r for r in caplog.records if r.levelname == "WARNING" and "ci run progress" in r.getMessage()]
        assert len(warned) == 1, caplog.text
        caplog.clear()
        with kb.write_txn(conn):
            conn.execute("UPDATE task_events SET created_at = created_at - 3600 WHERE task_id = ?", (tid,))
        with caplog.at_level("WARNING"):
            assert kcp.sweep(conn) == []
        assert kb.get_task(conn, tid).status == "scheduled"
        warned = [r for r in caplog.records if r.levelname == "WARNING" and "ci run progress" in r.getMessage()]
        assert len(warned) == 1, caplog.text


# --- the ref names no sha: the hint's shas ------------------------------------------------------------------------

NO_SHA_REF = f"https://github.com/{REPO}/pull/{PR}"


def test_a_hint_with_two_shas_still_wakes_on_a_conflict(kanban_home, api, monkeypatch) -> None:
    """Neither sha is taken as the head (the first one, an old head, would read as "head moved" on every sweep):
    the current head is read, and its conflict wakes the card."""
    api.run("in_progress", age=60, runner="ci-fast-1")
    with kbc.connect_closing() as conn:
        tid = _parked(conn, monkeypatch, ref=NO_SHA_REF, reason=f"Rebased onto {BASE} as {HEAD}; waiting for CI.",
                      wake_check=f"false gh pr checks {PR} -R {REPO} # was {OTHER}")
        api.dirty()
        assert kcp.sweep(conn) == [tid]
        notes = _notes(conn, tid)
        assert len(notes) == 1 and f"PR #{PR} conflicts with state" in notes[0], notes
        assert _runner_blockers(conn) == []


def test_a_hint_with_one_old_head_sha_reads_the_moved_head_as_ok(kanban_home, api, monkeypatch) -> None:
    """One sha in the hint is still the head the wait names: once the PR moves off it, the owner's probe answers,
    even when the new head conflicts."""
    with kbc.connect_closing() as conn:
        tid = _parked(conn, monkeypatch, ago=30 * 60, ref=NO_SHA_REF,
                      reason=f"Waiting for CI on PR #{PR} head {HEAD}.")
        api.dirty()
        api.pr["head"]["sha"] = OTHER
        assert kcp.sweep(conn) == []
        assert kb.get_task(conn, tid).status == "scheduled"
        assert _notes(conn, tid) == []
