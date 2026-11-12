"""Tests for scripts/ci/batch_review_comments.py (no network)."""

from __future__ import annotations

import importlib.util
import re
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

_PATH = Path(__file__).resolve().parents[2] / "scripts" / "ci" / "batch_review_comments.py"
_spec = importlib.util.spec_from_file_location("batch_review_comments", _PATH)
assert _spec and _spec.loader
b = importlib.util.module_from_spec(_spec)
sys.modules["batch_review_comments"] = b
_spec.loader.exec_module(b)

REPO = "o/r"
NOW = datetime(2026, 11, 11, 12, 17, tzinfo=UTC)


def _pr(n, sha, head_repo: str | None = REPO, state="open"):
    return {"number": n, "state": state, "head": {"sha": sha, "repo": {"full_name": head_repo} if head_repo else None}}


def _run(rid, sha, created, name="CI", event="pull_request"):
    return {"id": rid, "name": name, "event": event, "head_sha": sha, "created_at": created,
            "html_url": f"https://x/runs/{rid}", "head_commit": {"message": f"msg {sha}\nbody"}}


class Fake:
    """Patches the live_comment API surface used by the batch script."""

    def __init__(self, monkeypatch, prs, runs_by_sha, fresh=None, write_ok=None, fail_collect=(), comments=None):
        self.prs, self.runs_by_sha = prs, runs_by_sha
        self.fresh = fresh or {}
        self.write_ok = write_ok or {}
        self.fail_collect = set(fail_collect)
        self.comments = comments or {}  # PR number -> that PR's issue comments
        self.posted: dict[str, str] = {}
        self.comment_ids: dict[str, int | None] = {}
        self.collected: list[str] = []
        lc = b.lc

        def paginated(url, token, list_key=None):
            if "/pulls?" in url:
                if isinstance(self.prs, Exception):
                    raise self.prs
                return self.prs
            if "/actions/runs?" in url:
                sha = url.split("head_sha=")[1].split("&")[0]
                assert "event=pull_request" in url
                return self.runs_by_sha.get(sha, [])
            if url.endswith("/comments") and "/issues/" in url:
                return self.comments.get(int(url.split("/issues/")[1].split("/")[0]), [])
            raise AssertionError(url)

        def request(url, token):
            n = int(url.rsplit("/", 1)[1])
            return self.fresh[n]

        def collect(token, repo, run_id, watch=None, *, strict=False):
            assert strict
            self.collected.append(run_id)
            if run_id in self.fail_collect:
                raise RuntimeError("boom")
            return [], True

        def upsert(token, repo, pr, body, comment_id=None):
            if not self.write_ok.get(pr, True):
                return None
            self.posted[pr] = body
            self.comment_ids[pr] = comment_id
            return 1

        monkeypatch.setattr(lc, "_api_get_paginated", paginated)
        monkeypatch.setattr(lc, "_api_request", request)
        monkeypatch.setattr(lc, "collect_run_jobs", collect)
        monkeypatch.setattr(lc, "fetch_all_review_statuses", lambda *a, **k: [])
        monkeypatch.setattr(lc, "upsert_comment", upsert)


def _fresh(prs):
    return {p["number"]: {"state": p["state"], "head": {"sha": p["head"]["sha"]}} for p in prs}


def test_two_prs_one_invocation_exact_head_newest_run(monkeypatch):
    prs = [_pr(1, "aaa"), _pr(2, "bbb")]
    runs = {
        "aaa": [_run(10, "aaa", "2026-11-11T01:00:00Z"), _run(11, "aaa", "2026-11-11T02:00:00Z"),
                _run(12, "zzz", "2026-11-11T03:00:00Z"),  # other head
                _run(13, "aaa", "2026-11-11T04:00:00Z", name="Docker"),
                _run(14, "aaa", "2026-11-11T05:00:00Z", event="push")],
        "bbb": [_run(20, "bbb", "2026-11-11T01:00:00Z")],
    }
    f = Fake(monkeypatch, prs, runs, fresh=_fresh(prs))
    assert b.run_batch("t", REPO, NOW) == 0
    assert f.collected == ["11", "20"]
    assert set(f.posted) == {"1", "2"}
    body = f.posted["1"]
    assert "snapshot of" in body and b.REFRESH_NOTE in body
    assert "running on" not in body
    assert "<!-- hermes-ci-review-bot -->" in body


def test_forks_and_closed_excluded(monkeypatch):
    prs = [_pr(1, "a"), _pr(2, "b", head_repo="someone/fork"), _pr(3, "c", head_repo=None),
           _pr(4, "d", state="closed")]
    f = Fake(monkeypatch, prs, {"a": [_run(1, "a", "t")]}, fresh=_fresh(prs))
    assert b.run_batch("t", REPO, NOW) == 0
    assert list(f.posted) == ["1"]


def test_empty_set_is_success(monkeypatch):
    f = Fake(monkeypatch, [], {})
    assert b.run_batch("t", REPO, NOW) == 0
    assert f.posted == {} and f.collected == []


def test_pr_without_ci_run_skipped_not_error(monkeypatch):
    prs = [_pr(1, "a")]
    f = Fake(monkeypatch, prs, {"a": [_run(1, "other", "t")]}, fresh=_fresh(prs))
    assert b.run_batch("t", REPO, NOW) == 0
    assert f.posted == {}


def test_head_race_skips_and_closed_race_skips(monkeypatch):
    prs = [_pr(1, "a"), _pr(2, "b"), _pr(3, "c")]
    runs = {s: [_run(i, s, "t")] for i, s in enumerate("abc", 1)}
    fresh = {1: {"state": "open", "head": {"sha": "NEW"}},
             2: {"state": "closed", "head": {"sha": "b"}},
             3: {"state": "open", "head": {"sha": "c"}}}
    f = Fake(monkeypatch, prs, runs, fresh=fresh)
    assert b.run_batch("t", REPO, NOW) == 0
    assert list(f.posted) == ["3"]


def test_list_failure_nonzero(monkeypatch):
    Fake(monkeypatch, RuntimeError("api down"), {})
    assert b.run_batch("t", REPO, NOW) == 1


def test_per_pr_isolation_on_collect_and_write_errors(monkeypatch):
    prs = [_pr(1, "a"), _pr(2, "b"), _pr(3, "c")]
    runs = {"a": [_run(1, "a", "t")], "b": [_run(2, "b", "t")], "c": [_run(3, "c", "t")]}
    f = Fake(monkeypatch, prs, runs, fresh=_fresh(prs), write_ok={"2": False}, fail_collect=["1"])
    assert b.run_batch("t", REPO, NOW) == 1
    assert list(f.posted) == ["3"]  # PR 3 still posted


def test_main_requires_env(monkeypatch):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.setattr(sys, "argv", ["x"])
    assert b.main() == 1


@pytest.mark.parametrize("conclusion", ["cancelled", "timed_out", "action_required", "neutral", "skipped"])
def test_incomplete_run_never_overwrites_comment(monkeypatch, conclusion):
    prs = [_pr(1, "a")]
    run = _run(1, "a", "t") | {"status": "completed", "conclusion": conclusion}
    f = Fake(monkeypatch, prs, {"a": [run]}, fresh=_fresh(prs))
    assert b.run_batch("t", REPO, NOW) == 0
    assert f.posted == {}


def test_final_snapshot_preserves_ship_footer_contract(monkeypatch):
    sha = "a" * 40
    prs = [_pr(1, sha)]
    run = _run(1, sha, "t") | {"status": "completed", "conclusion": "success"}
    f = Fake(monkeypatch, prs, {sha: [run]}, fresh=_fresh(prs))
    assert b.run_batch("t", REPO, NOW) == 0
    # Consumer contract from hermes-core-wt-land's BOT_HEAD parser.
    match = re.search(r"<sub>(running|ran) on \[[0-9a-f]+\]\([^)]*/commits/([0-9a-f]{40})\)", f.posted["1"])
    assert match and match.groups() == ("ran", sha)


@pytest.mark.parametrize("cancelled_id", [1, 2])
@pytest.mark.parametrize("conclusion", ["cancelled", "timed_out", "action_required", None])
def test_collection_time_incomplete_run_cannot_publish(monkeypatch, cancelled_id, conclusion):
    sha = "a" * 40
    pr = _pr(1, sha)
    initial = _run(1, sha, "t") | {"status": "in_progress", "conclusion": None}
    primary = initial | {"status": "completed", "conclusion": "success"}
    sibling = _run(2, sha, "t", name=b.DEFAULT_WATCH_WORKFLOWS[0]) | {
        "status": "completed", "conclusion": "success"}
    (primary if cancelled_id == 1 else sibling)["conclusion"] = conclusion
    posted = []

    def request(url, token):
        if "/pulls/" in url:
            return pr
        return primary

    def pages(url, token, list_key=None):
        if "/pulls?" in url:
            return [pr]
        if "event=pull_request" in url:
            return [initial]
        if url.endswith("/jobs"):
            return [{"name": "test", "status": "completed", "conclusion": "cancelled"}]
        return [sibling]

    monkeypatch.setattr(b.lc, "_api_request", request)
    monkeypatch.setattr(b.lc, "_api_get_paginated", pages)
    monkeypatch.setattr(b.lc, "fetch_all_review_statuses", lambda *a, **k: [])
    monkeypatch.setattr(b.lc, "upsert_comment", lambda *a: posted.append(a) or 1)
    assert b.run_batch("t", REPO, NOW) == 1
    assert posted == []


def test_artifact_listing_error_is_not_clean_snapshot(monkeypatch):
    def fail(*args):
        raise OSError("artifact API unavailable")
    monkeypatch.setattr(b.lc, "_list_artifacts", fail)
    with pytest.raises(OSError):
        b.lc.fetch_all_review_statuses("t", REPO, "1", strict=True)


def test_artifact_download_error_is_not_clean_snapshot(monkeypatch):
    monkeypatch.setattr(b.lc, "_list_artifacts", lambda *args: [{"name": "review-status-test"}])
    monkeypatch.setattr(b.lc, "_download_artifact", lambda *args: None)
    with pytest.raises(RuntimeError):
        b.lc.fetch_all_review_statuses("t", REPO, "1", strict=True)


def test_workflow_shape():
    yaml = pytest.importorskip("hermes_yaml")
    doc = yaml.safe_load((Path(__file__).resolve().parents[2] / ".github/workflows/ci-review-comment.yml").read_text())
    on = doc.get("on", doc.get(True))
    assert set(on) == {"schedule", "workflow_dispatch", "workflow_run"}
    # The hourly sweep stays: it refreshes every PR, including heads no CI completion reached.
    assert on["schedule"] == [{"cron": "17 * * * *"}]
    assert doc["concurrency"]["cancel-in-progress"] is False
    job = doc["jobs"]["comment"]
    assert job["timeout-minutes"] == 15 and job["runs-on"] == "blacksmith-2vcpu-ubuntu-2404"
    assert any("batch_review_comments.py" in (s.get("run") or "") for s in job["steps"])


def _workflow():
    yaml = pytest.importorskip("hermes_yaml")
    return yaml.safe_load((Path(__file__).resolve().parents[2] / ".github/workflows/ci-review-comment.yml").read_text())


def test_ci_completion_on_a_ship_head_triggers_a_targeted_refresh():
    """A PR whose CI completes on a new (rebased) head gets a bot run for exactly that head, with no dispatch.

    Regression PR1104 (2026-11-12): required CI green and exact-head review approved at 10:42-10:43Z, but ship
    refused (rc 3, "ci-review-bot has not finished") until a hand dispatch, because the bot ran only hourly.
    """
    doc = _workflow()
    on = doc.get("on", doc.get(True))
    assert on["workflow_run"] == {"workflows": ["CI", "Docker Build, Test, and Publish"], "types": ["completed"],
                                  "branches": ["wt/**"]}
    job = doc["jobs"]["comment"]
    cond = " ".join(job["if"].split())
    # schedule/dispatch keep the full sweep; workflow_run only for same-repo PR runs that finished green or red.
    assert cond.startswith("github.event_name != 'workflow_run' || (")
    assert "github.event.workflow_run.event == 'pull_request'" in cond
    assert "github.event.workflow_run.head_repository.full_name == github.repository" in cond
    assert "github.event.workflow_run.conclusion == 'success'" in cond
    assert "github.event.workflow_run.conclusion == 'failure'" in cond
    step = next(s for s in job["steps"] if "batch_review_comments.py" in (s.get("run") or ""))
    assert step["env"]["TARGET_HEAD_SHA"] == "${{ github.event.workflow_run.head_sha }}"
    # Untrusted event data never reaches a run: line directly (script injection).
    assert "${{" not in step["run"]
    # Each head refreshes in its own group: a shared group keeps one pending run and cancels the rest.
    group = doc["concurrency"]["group"]
    assert "github.event.workflow_run.head_sha" in group and group.startswith("ci-review-comment")
    # The default-branch checkout is kept for every trigger: PR code never runs with the write token.
    checkout = job["steps"][0]
    assert checkout["with"]["ref"] == "${{ github.event.repository.default_branch }}"


def test_targeted_run_refreshes_only_the_pr_at_that_head(monkeypatch):
    old, new, other = "1" * 40, "2" * 40, "3" * 40
    prs = [_pr(1104, new), _pr(7, other)]
    runs = {new: [_run(1, new, "t") | {"status": "completed", "conclusion": "success"}],
            other: [_run(2, other, "t") | {"status": "completed", "conclusion": "success"}],
            old: [_run(3, old, "t") | {"status": "completed", "conclusion": "success"}]}
    f = Fake(monkeypatch, prs, runs, fresh=_fresh(prs))
    assert b.run_batch("t", REPO, NOW, head_sha=new) == 0
    assert list(f.posted) == ["1104"] and f.collected == ["1"]
    match = re.search(r"<sub>(running|ran) on \[[0-9a-f]+\]\([^)]*/commits/([0-9a-f]{40})\)", f.posted["1104"])
    assert match and match.groups() == ("ran", new)


def test_targeted_run_without_an_open_pr_at_that_head_writes_nothing(monkeypatch):
    prs = [_pr(1, "a" * 40)]
    f = Fake(monkeypatch, prs, {}, fresh=_fresh(prs))
    assert b.run_batch("t", REPO, NOW, head_sha="b" * 40) == 0
    assert f.posted == {} and f.collected == []


def test_main_targets_the_completed_runs_head_from_the_workflow_env(monkeypatch):
    head = "c" * 40
    seen = {}
    monkeypatch.setenv("GITHUB_TOKEN", "t")
    monkeypatch.setenv("GITHUB_REPOSITORY", REPO)
    monkeypatch.setenv("TARGET_HEAD_SHA", head.upper())
    monkeypatch.delenv("TRIGGER_WORKFLOW", raising=False)
    monkeypatch.setattr(sys, "argv", ["x"])
    monkeypatch.setattr(b, "run_batch", lambda token, repo, **kw: seen.update(kw) or 0)
    assert b.main() == 0
    assert seen == {"head_sha": head, "trigger_workflow": ""}
    # schedule/dispatch: the workflow expression renders empty, so every PR is swept
    monkeypatch.setenv("TARGET_HEAD_SHA", "")
    assert b.main() == 0
    assert seen == {"head_sha": "", "trigger_workflow": ""}


@pytest.mark.parametrize("bad", ["abc123", "g" * 40, "1" * 39, "1" * 41, "$(id)"])
def test_main_refuses_a_target_that_is_not_a_full_sha(monkeypatch, bad):
    monkeypatch.setenv("GITHUB_TOKEN", "t")
    monkeypatch.setenv("GITHUB_REPOSITORY", REPO)
    monkeypatch.setenv("TARGET_HEAD_SHA", bad)
    monkeypatch.setattr(sys, "argv", ["x"])
    monkeypatch.setattr(b, "run_batch", lambda *a, **k: pytest.fail("ran with an invalid target"))
    assert b.main() == 1


# --- PR #1382 review follow-ups (card t_01a1cfff03a1f1ea) --------------------------------------------------------
DOCKER = "Docker Build, Test, and Publish"


def _done(rid, sha, **kw):
    return _run(rid, sha, "t", **kw) | {"status": "completed", "conclusion": "success"}


def _bot_comment(cid, head, started, run_id="900"):
    # The state line's format is spelt out here (not built with b.state_line) so this pins the on-comment contract.
    state = f"<!-- hermes-ci-review-bot:state head={head} started={started:%Y-%m-%dT%H:%M:%S.%fZ} run={run_id} -->"
    body = f"<!-- hermes-ci-review-bot -->\n{state}\n# old snapshot\n"
    return {"id": cid, "body": body}


def test_footer_names_both_refresh_triggers(monkeypatch):
    """Point 1: the comment is also refreshed when CI (or Docker) completes on the head, not only hourly."""
    sha = "a" * 40
    prs = [_pr(1, sha)]
    f = Fake(monkeypatch, prs, {sha: [_done(1, sha)]}, fresh=_fresh(prs))
    assert b.run_batch("t", REPO, NOW) == 0
    footer = re.search(r"<sub>ran on [^\n]*</sub>", f.posted["1"]).group(0)
    assert "refreshed hourly" not in footer
    assert "when CI or Docker completes on this head" in footer and "hourly" in footer


@pytest.mark.parametrize(("recorded_offset_s", "writes"), [(300, False), (0, False), (-3600, True)])
def test_an_older_run_never_overwrites_a_newer_snapshot_of_the_same_head(monkeypatch, recorded_offset_s, writes):
    """Point 2: the hourly sweep and a targeted run race on one comment; the run that started later wins.

    Before: a sweep that started at :17 but reached the PR after a targeted run (started later, after CI
    completed) overwrote the final snapshot with its older one until the next sweep.
    """
    from datetime import timedelta
    sha = "a" * 40
    prs = [_pr(1, sha)]
    existing = _bot_comment(55, sha, NOW + timedelta(seconds=recorded_offset_s))
    f = Fake(monkeypatch, prs, {sha: [_done(1, sha)]}, fresh=_fresh(prs), comments={1: [existing]})
    assert b.run_batch("t", REPO, NOW) == 0
    assert ("1" in f.posted) is writes
    if writes:
        assert f.comment_ids["1"] == 55  # updates the comment it compared against


def test_a_snapshot_of_another_head_or_without_state_is_replaced_and_the_write_records_its_state(monkeypatch):
    from datetime import timedelta
    old, new = "1" * 40, "2" * 40
    prs = [_pr(1, new), _pr(2, new)]
    later = NOW + timedelta(hours=1)
    comments = {1: [{"id": 7, "body": "unrelated"}, _bot_comment(8, old, later)],
                2: [{"id": 9, "body": "<!-- hermes-ci-review-bot -->\n# from before the state line\n"}]}
    monkeypatch.setenv("GITHUB_RUN_ID", "4242")
    f = Fake(monkeypatch, prs, {new: [_done(1, new)]}, fresh=_fresh(prs), comments=comments)
    assert b.run_batch("t", REPO, NOW) == 0
    assert f.comment_ids == {"1": 8, "2": 9}
    for body in f.posted.values():
        assert body.startswith("<!-- hermes-ci-review-bot -->\n" + b.state_line(new, NOW, "4242") + "\n")
        # The ship parser still reads the head from the footer.
        assert re.search(r"<sub>(running|ran) on [^\n]*?/commits/([0-9a-f]{40})", body).group(2) == new


def test_recorded_state_round_trips_and_orders_runs(monkeypatch):
    from datetime import timedelta
    sha = "a" * 40
    prs = [_pr(1, sha)]
    f = Fake(monkeypatch, prs, {sha: [_done(1, sha)]}, fresh=_fresh(prs))
    assert b.run_batch("t", REPO, NOW) == 0
    written = f.posted.pop("1")
    f.comments = {1: [{"id": 3, "body": written}]}
    assert b.run_batch("t", REPO, NOW - timedelta(microseconds=1)) == 0  # started earlier: skipped
    assert f.posted == {}
    assert b.run_batch("t", REPO, NOW + timedelta(microseconds=1)) == 0  # started later: writes
    assert f.comment_ids == {"1": 3}
    assert b.stale_write_reason(written, "b" * 40, NOW - timedelta(days=1)) == ""  # another head: write
    assert b.stale_write_reason(written, sha, NOW).startswith("a run that started ")


def test_every_watched_sibling_workflow_also_triggers_a_targeted_refresh():
    """Point 3: a Docker run finishing after CI refreshed the comment only at the next hourly sweep."""
    doc = _workflow()
    on = doc.get("on", doc.get(True))
    step = next(s for s in doc["jobs"]["comment"]["steps"] if "batch_review_comments.py" in (s.get("run") or ""))
    watched = b.lc.parse_watch_workflows(step["env"]["WATCH_WORKFLOWS"])
    assert watched == [DOCKER]
    assert on["workflow_run"]["workflows"] == ["CI", *watched]
    assert on["workflow_run"]["branches"] == ["wt/**"]
    assert step["env"]["TRIGGER_WORKFLOW"] == "${{ github.event.workflow_run.name }}"
    # Trigger names must be the workflows' exact `name:`; a misspelt one never fires.
    yaml = pytest.importorskip("hermes_yaml")
    names = {yaml.safe_load(p.read_text()).get("name")
             for p in (Path(__file__).resolve().parents[2] / ".github/workflows").glob("*.y*ml")}
    assert set(on["workflow_run"]["workflows"]) <= names


@pytest.mark.parametrize(("trigger", "ci_status", "writes"), [
    (DOCKER, "in_progress", False),   # Docker first: CI's completion refreshes
    (DOCKER, "queued", False),
    (DOCKER, "completed", True),      # Docker after CI: this run posts Docker's result
    ("CI", "completed", True),
    ("CI", "in_progress", True),      # a rerun of CI on this head (label-rerun) is newest: show it running
    ("", "in_progress", True),        # the hourly sweep still shows a running CI
])
def test_sibling_completion_refreshes_only_once_ci_is_done(monkeypatch, trigger, ci_status, writes):
    sha = "a" * 40
    prs = [_pr(1, sha)]
    run = _run(1, sha, "t") | {"status": ci_status, "conclusion": "success" if ci_status == "completed" else None}
    f = Fake(monkeypatch, prs, {sha: [run]}, fresh=_fresh(prs))
    assert b.run_batch("t", REPO, NOW, head_sha=sha if trigger else "", trigger_workflow=trigger) == 0
    assert ("1" in f.posted) is writes
    assert (f.collected == ["1"]) is writes  # a skipped sibling trigger spends no collection calls


def test_main_passes_the_triggering_workflow(monkeypatch):
    seen = {}
    monkeypatch.setenv("GITHUB_TOKEN", "t")
    monkeypatch.setenv("GITHUB_REPOSITORY", REPO)
    monkeypatch.setenv("TARGET_HEAD_SHA", "c" * 40)
    monkeypatch.setenv("TRIGGER_WORKFLOW", f" {DOCKER}\n")
    monkeypatch.setattr(sys, "argv", ["x"])
    monkeypatch.setattr(b, "run_batch", lambda token, repo, **kw: seen.update(kw) or 0)
    assert b.main() == 0
    assert seen == {"head_sha": "c" * 40, "trigger_workflow": DOCKER}
