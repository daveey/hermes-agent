"""ci-flaky-cards exit policy: only data that stays unreadable across polls turns the cron red.

Fixtures are metadata-only projections of real softmax-universe/daveey-hermes Actions runs from
2026-11-12 that kept the hub cron (98673e649121) exiting 1 on every poll:

* 43179810118 'CI' on wt/reload-tooldefs-barrier, cancelled 08:10:26Z by the concurrency group when
  43180734907 (itself cancelled later) and 43181620115 (in progress) were pushed to the same branch.
* 43165298341 attempt 2, a "Re-run failed jobs" of a run whose 8 test slices passed in attempt 1: attempt 2
  lists the slices with new job ids and run_attempt 2 but attempt 1's started_at/completed_at, and the only
  metadata artifacts are flaky-metadata-v1-43165298341-1-{1..8}.
"""
from __future__ import annotations

import time
from pathlib import Path

import pytest

from scripts.ci import flaky_artifact as fa
from scripts.ci import flaky_kanban as fk
from tests.ci.flaky_artifact_fakes import FakeGitHub, artifact, job, make_zip, meta
from tests.ci.test_flaky_kanban import FakeBoard

A = "tests/x.py::test_a"
WF, BRANCH = 381502190, "wt/reload-tooldefs-barrier"
OLD = "2026-11-12T08:10:26Z"   # completed long before "now" in these tests


@pytest.fixture
def gh(monkeypatch):
    fake = FakeGitHub()
    monkeypatch.setattr(fk, "_gh", fake.gh)
    monkeypatch.setattr(fa, "download_zip", fake.download)
    return fake


def _state():
    return fk.load_state(Path("/nonexistent/state.json"))


def _cancelled(gh, run_id=43179810118, branch=BRANCH, workflow=WF):
    run = gh.run(run_id)
    run.update(conclusion="cancelled", head_branch=branch, workflow_id=workflow, updated_at=OLD)
    gh.jobs[(run_id, 1)] = [job(131040000001, 1, 2), job(131040000002, 2, 2, conclusion="cancelled")]
    return run


def _main(tmp_path, monkeypatch, capsys):
    from tests.ci.test_flaky_intake import Board

    monkeypatch.setenv("GH_TOKEN", "tok")
    board = Board()
    monkeypatch.setattr(fk, "CliBoard", lambda *a, **k: board)
    monkeypatch.setattr(fk, "HubRelay", lambda *a, **k: None)
    argv = ["--from-github", "--repo", "o/r", "--state", str(tmp_path / "s.json")]
    assert fk.main(["--initialize-intake", "--state", str(tmp_path / "s.json")]) == 0
    capsys.readouterr()

    def run() -> tuple[int, str]:
        rc = fk.main(argv)
        return rc, capsys.readouterr().out
    return run


# --- 1. a cancelled run superseded by a newer run of its workflow and branch is not applicable ---------------


@pytest.mark.parametrize("newer", ["completed", "in_progress"])
def test_superseded_cancelled_run_is_not_applicable(gh, tmp_path, monkeypatch, capsys, newer):
    _cancelled(gh)
    successor = {"id": 43180734907, "head_branch": BRANCH, "workflow_id": WF, "run_attempt": 1}
    if newer == "completed":     # also in the completed listing: a cancelled, superseded run with no test jobs yet
        gh.runs.append({**successor, "head_sha": "a" * 40, "conclusion": "cancelled", "updated_at": OLD})
        gh.jobs[(43180734907, 1)] = []
        gh.other_runs.append({"id": 43181620115, "head_branch": BRANCH, "workflow_id": WF, "status": "in_progress"})
    else:                        # the newer run is still running, so only the per-workflow listing shows it
        gh.other_runs.append({**successor, "status": "in_progress"})
    poll = _main(tmp_path, monkeypatch, capsys)
    rc, out = poll()
    assert rc == 0 and "FAILED" not in out and "WAITING" not in out
    assert "not_applicable=" in out and fk.load_state(tmp_path / "s.json")["runs"].count("43179810118/1") == 1
    assert not gh.downloads and not any("/artifacts" in u for u in gh.urls)
    assert any(f"/actions/workflows/{WF}/runs?branch=wt%2Freload-tooldefs-barrier" in u for u in gh.urls)
    assert poll() == (0, "")      # processed: never read again


# --- 2. a cancelled test run nothing superseded stays unavailable -----------------------------------------------


@pytest.mark.parametrize("other", [
    None,                                                                   # alone on its branch
    {"id": 43181620115, "head_branch": "wt/other", "workflow_id": WF},      # newer run, other branch
    {"id": 43181620115, "head_branch": BRANCH, "workflow_id": 4242},        # newer run, other workflow
    {"id": 43100000000, "head_branch": BRANCH, "workflow_id": WF},          # older run only
])
def test_cancelled_test_run_without_a_newer_run_stays_unavailable(gh, other):
    _cancelled(gh)
    if other:
        gh.other_runs.append(other)
    board, state = FakeBoard(), _state()
    log = fk.poll_github("o/r", "tok", board, state)
    assert log == ["WAITING run=43179810118/1 code=jobs_unavailable polls=1",
                   "POLL complete=0 empty=0 unavailable=1 not_applicable=0"]
    assert fk.poll_github("o/r", "tok", board, state)[0] == "FAILED run=43179810118/1 code=jobs_unavailable polls=2"
    assert state["runs"] == []


@pytest.mark.parametrize("breaks", ["lookup_fails", "no_workflow_id", "no_branch"])
def test_unprovable_supersession_keeps_the_cancelled_run_unavailable(gh, breaks):
    run = _cancelled(gh)
    gh.other_runs.append({"id": 43181620115, "head_branch": BRANCH, "workflow_id": WF})
    if breaks == "lookup_fails":
        gh.fail["/actions/workflows/"] = OSError("boom")
    else:
        run.pop("workflow_id" if breaks == "no_workflow_id" else "head_branch")
    state = _state()
    log = fk.poll_github("o/r", "tok", FakeBoard(), state)
    assert log[0] == "WAITING run=43179810118/1 code=jobs_unavailable polls=1" and state["runs"] == []


# --- 3. a just-finished run whose artifact is not listed yet is retried, not FAILED -----------------------------


def test_fresh_missing_artifact_retries_without_failed_and_is_ingested_next_poll(gh, tmp_path, monkeypatch, capsys):
    gh.simple(tests=(A,))
    gh.runs[0]["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 120))
    listed = gh.artifacts[5]
    gh.artifacts[5] = []
    poll = _main(tmp_path, monkeypatch, capsys)
    rc, out = poll()
    assert rc == 0 and "RETRY run=5/1 code=missing age=" in out and "FAILED" not in out
    assert fk.load_state(tmp_path / "s.json")["runs"] == []
    rc, out = poll()                      # still not listed, still inside the grace window: not red either
    assert rc == 0 and "RETRY run=5/1 code=missing" in out and "FAILED" not in out
    gh.artifacts[5] = listed
    rc, out = poll()
    assert rc == 0 and "intake delivered" in out
    import json
    import sqlite3
    with sqlite3.connect(tmp_path / "s.intake.sqlite3") as conn:
        rows = conn.execute("SELECT metadata,delivered FROM occurrences").fetchall()
    assert len(rows) == 1 and json.loads(rows[0][0])["test_id"] == A and rows[0][1] == 1
    state = fk.load_state(tmp_path / "s.json")
    assert state["runs"] == ["5/1"] and state["unavailable"] == {}


def test_missing_on_a_run_past_the_grace_window_is_not_a_retry(gh):
    gh.simple()
    gh.runs[0]["updated_at"] = OLD
    gh.artifacts[5] = []
    assert fk.poll_github("o/r", "tok", FakeBoard(), _state())[0] == "WAITING run=5/1 code=missing polls=1"


# --- 4. only unavailability that persists across polls (and past the grace window) exits 1 ----------------------


def test_persistent_unavailable_attempt_exits_one(gh, tmp_path, monkeypatch, capsys):
    gh.simple()
    gh.runs[0]["updated_at"] = OLD
    gh.zips[100] = fa.ArtifactError("download_unavailable")
    poll = _main(tmp_path, monkeypatch, capsys)
    rc, out = poll()
    assert rc == 0 and "WAITING run=5/1 code=download_unavailable polls=1" in out
    rc, out = poll()
    assert rc == 1 and "FAILED run=5/1 code=download_unavailable polls=2" in out
    rc, out = poll()
    assert rc == 1 and "FAILED run=5/1 code=download_unavailable polls=3" in out
    assert fk.load_state(tmp_path / "s.json")["runs"] == []


def test_unavailable_inside_the_grace_window_waits_even_when_repeated(gh):
    gh.simple()
    gh.runs[0]["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 60))
    gh.zips[100] = fa.ArtifactError("download_unavailable")
    state = _state()
    fk.poll_github("o/r", "tok", FakeBoard(), state)
    assert fk.poll_github("o/r", "tok", FakeBoard(), state)[0] == "WAITING run=5/1 code=download_unavailable polls=2"


def test_unavailable_streak_needs_consecutive_polls_and_state_stays_bounded(gh):
    gh.simple(run_id=5)
    gh.simple(run_id=6, art_id=200)
    gh.zips[100] = gh.zips[200] = fa.ArtifactError("download_unavailable")
    state = _state()
    fk.poll_github("o/r", "tok", FakeBoard(), state)
    assert set(state["unavailable"]) == {"5/1", "6/1"}
    gh.runs = [r for r in gh.runs if r["id"] != 5]          # 5 left the lookback window
    gh.zips[200] = make_zip(meta(6))                         # 6 became readable
    log = fk.poll_github("o/r", "tok", FakeBoard(), state)
    assert state["unavailable"] == {} and state["runs"] == ["6/1"] and "FAILED" not in "\n".join(log)
    gh.simple(run_id=5)
    gh.zips[100] = fa.ArtifactError("download_unavailable")
    state["poll_seq"] += 5                                    # polls that did not see it break the streak
    state["unavailable"]["5/1"] = {"seq": 1, "polls": 1}
    assert fk.poll_github("o/r", "tok", FakeBoard(), state)[0] == "WAITING run=5/1 code=download_unavailable polls=1"


def test_card_filing_failure_is_still_failed_at_once(gh):
    gh.simple(tests=(A,))

    class Refusing(FakeBoard):
        def create(self, *a, **k):
            raise fk.CreateRefused("overlap", [])

    log = fk.poll_github("o/r", "tok", Refusing(), _state())
    assert log[0].startswith(f"FAILED {A}: create refused")


# --- carried-over jobs of a "Re-run failed jobs" attempt use the attempt that ran them --------------------------


def _rerun(gh, rerun_slice2=False):
    """Run 5 attempt 2 after a failed non-test job: both test slices passed in attempt 1."""
    run = gh.run(5, attempt=2)
    run["run_started_at"] = "2026-11-12T06:26:39Z"
    first = [{**job(131006004001, 1, 2), "run_attempt": 1,
              "started_at": "2026-11-12T06:03:05Z", "completed_at": "2026-11-12T06:10:50Z"},
             {**job(131006004071, 2, 2), "run_attempt": 1,
              "started_at": "2026-11-12T06:03:06Z", "completed_at": "2026-11-12T06:10:31Z"}]
    gh.jobs[(5, 1)] = first
    copies = [{**j, "id": j["id"] + 8000000, "run_attempt": 2} for j in first]
    if rerun_slice2:
        copies[1].update(started_at="2026-11-12T06:27:00Z", completed_at="2026-11-12T06:35:00Z")
    gh.jobs[(5, 2)] = copies
    gh.artifacts[5] = [artifact(100, 5, 1, 1), artifact(101, 5, 1, 2)]
    gh.zips[100] = make_zip(meta(5, attempt=1, slice_no=1, slices=2, tests=(A,)))
    gh.zips[101] = make_zip(meta(5, attempt=1, slice_no=2, slices=2))
    return run


def test_carried_over_slices_are_read_from_the_attempt_that_ran_them(gh):
    _rerun(gh)
    board, state = FakeBoard(), _state()
    log = fk.poll_github("o/r", "tok", board, state)
    assert log[0].startswith("opened") and state["runs"] == ["5/2"] and sorted(gh.downloads) == [100, 101]
    card = board.cards[state["cards"][A]["card"]]
    # The job that ran (attempt 1's id), so a flake already filed from attempt 1 is not filed again.
    assert "https://github.com/o/r/actions/runs/5/job/131006004001" in card["body"]


def test_a_slice_that_really_reran_still_needs_its_own_attempt_artifact(gh):
    _rerun(gh, rerun_slice2=True)
    state = _state()
    log = fk.poll_github("o/r", "tok", FakeBoard(), state)
    assert log[0] == "WAITING run=5/2 code=missing polls=1" and state["runs"] == []
    gh.artifacts[5].append(artifact(110, 5, 2, 2))
    gh.zips[110] = make_zip(meta(5, attempt=2, slice_no=2, slices=2))
    log = fk.poll_github("o/r", "tok", FakeBoard(), state)
    assert state["runs"] == ["5/2"] and "FAILED" not in "\n".join(log) and 110 in gh.downloads and 101 not in gh.downloads


def test_carry_over_needs_matching_timestamps(gh):
    run = _rerun(gh)
    gh.jobs[(5, 1)][0]["completed_at"] = "2026-11-12T06:10:51Z"   # not the same job
    state = _state()
    assert fk.poll_github("o/r", "tok", FakeBoard(), state)[0] == "WAITING run=5/2 code=missing polls=1"
    run.pop("run_started_at")
    gh.jobs[(5, 1)][0]["completed_at"] = "2026-11-12T06:10:50Z"
    assert fk.poll_github("o/r", "tok", FakeBoard(), state)[0].startswith("FAILED run=5/2 code=missing")
