"""Tests for scripts/ci/batch_review_comments.py (no network)."""

from __future__ import annotations

import importlib.util
import json
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
    monkeypatch.setattr(b, "run_batch", lambda token, repo, deadline=None, **kw: seen.update(kw) or 0)
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
    monkeypatch.setattr(b, "run_batch", lambda token, repo, deadline=None, **kw: seen.update(kw) or 0)
    assert b.main() == 0
    assert seen == {"head_sha": "c" * 40, "trigger_workflow": DOCKER}


# --- Targeted refresh vs a failed review-status download (card t_01a1d1292db9d3cb) -----------------------------
# PR1661, 2026-11-12: the CI-completion refresh (run 43279407344, 18:24:09Z) failed one artifact download on an
# artifact uploaded 26 s earlier, went red, and nothing retried: the "running on 8543d56" comment stood until a hand
# dispatch at 19:07Z although CI was green at 18:23:53Z and the PR approved.
_REAL_FETCH = b.lc.fetch_all_review_statuses
# hermes-core-wt-land's snapshot phases (ship also reads "refresh failed on <sha>" as naming that head for its
# warnings check, never as the final "ran on")
BOT_HEAD = re.compile(r"<sub>(running|ran) on [^\n]*?/commits/([0-9a-f]{40})")


class Downloads:
    """Real fetch_all_review_statuses over a fake artifact list; the first ``fail`` downloads fail."""

    def __init__(self, monkeypatch, tmp_path, fail):
        self.fail, self.calls, self.sleeps, self.dispatches = fail, 0, [], []
        status = tmp_path / "review-status.json"
        status.write_text('review_status=[{"source": "ci-timings", "results": []}]')
        self.status = status
        monkeypatch.setattr(b.lc, "fetch_all_review_statuses", _REAL_FETCH)
        monkeypatch.setattr(b.lc, "_list_artifacts", lambda *a: [{"name": "review-status-ci-timings"}])
        monkeypatch.setattr(b.lc, "_download_artifact", self.download)
        monkeypatch.setattr(b, "_sleep", self.sleeps.append)
        monkeypatch.setattr(b, "_api_post", lambda url, token, payload: self.dispatches.append((url, payload)))
        monkeypatch.setattr(b.lc.tempfile, "gettempdir", lambda: str(tmp_path))
        monkeypatch.setenv("GITHUB_RUN_ID", "43279407344")
        monkeypatch.setenv("DEFAULT_BRANCH", "main")

    def download(self, *args):
        self.calls += 1
        if self.fail is None or self.calls <= self.fail:
            b.lc.last_download_error = "blob download URLError: reset"
            return None
        return self.status


def _running_comment(sha, started):
    body = (f"<!-- hermes-ci-review-bot -->\n{b.state_line(sha, started, '43279214607')}\n# table\n\n"
            f"<sub>running on [{sha[:7]}](https://github.com/{REPO}/pull/1661/commits/{sha}) · snapshot of CI taken "
            f"2026-11-11 12:00 UTC; {b.REFRESH_NOTE}</sub>\n\n⏳ waiting for more jobs to start…\n")
    return {"id": 77, "body": body}


def test_a_targeted_refresh_retries_a_failed_download_and_posts_the_completed_snapshot(monkeypatch, tmp_path):
    """(a) The download fails twice, then succeeds: the comment is updated to the completed state."""
    from datetime import timedelta
    sha = "8543d562074eaef0674ab7e78fdf85fe020e8ae3"
    prs = [_pr(1661, sha)]
    f = Fake(monkeypatch, prs, {sha: [_done(43276201639, sha)]}, fresh=_fresh(prs),
             comments={1661: [_running_comment(sha, NOW - timedelta(minutes=1))]})
    d = Downloads(monkeypatch, tmp_path, fail=2)
    assert b.run_batch("t", REPO, NOW, head_sha=sha, trigger_workflow="CI") == 0
    assert d.calls == 3 and d.sleeps == list(b.TARGETED_DOWNLOAD_RETRY_DELAYS[:2])
    assert BOT_HEAD.search(f.posted["1661"]).groups() == ("ran", sha)
    assert f.comment_ids["1661"] == 77 and d.dispatches == []


def test_a_targeted_refresh_that_keeps_failing_marks_the_comment_and_queues_exactly_one_re_refresh(
        monkeypatch, tmp_path):
    """(b) The download always fails: the run is red, the comment carries the failed marker (ship's parser no longer
    reads "running on"), and exactly one delayed re-refresh of that head is dispatched."""
    from datetime import timedelta
    sha = "8543d562074eaef0674ab7e78fdf85fe020e8ae3"
    prs = [_pr(1661, sha)]
    f = Fake(monkeypatch, prs, {sha: [_done(43276201639, sha)]}, fresh=_fresh(prs),
             comments={1661: [_running_comment(sha, NOW - timedelta(minutes=1))]})
    d = Downloads(monkeypatch, tmp_path, fail=None)
    assert b.run_batch("t", REPO, NOW, head_sha=sha, trigger_workflow="CI") == 1
    # Bounded: one attempt per delay, ~90 s in all.
    assert d.sleeps == list(b.TARGETED_DOWNLOAD_RETRY_DELAYS) and 60 <= sum(d.sleeps) <= 120
    assert d.calls == len(b.TARGETED_DOWNLOAD_RETRY_DELAYS) + 1
    body = f.posted["1661"]
    assert f.comment_ids["1661"] == 77
    assert BOT_HEAD.search(body) is None  # neither "running on" nor "ran on": the probe keeps waiting, readers see why
    assert re.search(rf"<sub>refresh failed on \[8543d56\]\([^)]*/commits/{sha}\) at \d{{4}}-\d\d-\d\d "
                     r"\d\d:\d\d:\d\d UTC \(review-status artifact download failed", body)
    assert "blob download URLError: reset" in body and "6 attempt(s)" in body
    assert "re-refresh queued" in body and "waiting for more jobs" in body  # the old tables stay, footer swapped
    m = b._STATE_RE.search(body)
    assert m and m.group(1) == sha and m.group(3) == "43279407344" and m.group(4)
    assert body.count(b.STATE_PREFIX) == 1
    assert len(d.dispatches) == 1
    url, payload = d.dispatches[0]
    assert url.endswith(f"/repos/{REPO}/actions/workflows/ci-review-comment.yml/dispatches")
    assert payload == {"ref": "main", "inputs": {"head_sha": sha, "delay_seconds": str(b.RE_REFRESH_DELAY_S),
                                                 "retry_of": "43279407344"}}


def test_the_re_refresh_itself_never_queues_another(monkeypatch, tmp_path):
    """(b) not a loop: the dispatched re-refresh (no workflow_run trigger) that fails again only marks the comment,
    pointing at the hourly sweep; a later, successful refresh replaces the marker."""
    from datetime import timedelta
    sha = "a" * 40
    prs = [_pr(1, sha)]
    f = Fake(monkeypatch, prs, {sha: [_done(5, sha)]}, fresh=_fresh(prs),
             comments={1: [_running_comment(sha, NOW - timedelta(minutes=6))]})
    d = Downloads(monkeypatch, tmp_path, fail=None)
    assert b.run_batch("t", REPO, NOW, head_sha=sha, trigger_workflow="") == 1
    assert d.dispatches == []
    assert "next refresh: hourly sweep" in f.posted["1"] and BOT_HEAD.search(f.posted["1"]) is None
    # The next refresh (started later) succeeds over the marker.
    f.comments = {1: [{"id": 77, "body": f.posted.pop("1")}]}
    d.fail, d.calls = 0, 0
    assert b.run_batch("t", REPO, NOW + timedelta(minutes=30)) == 0
    assert BOT_HEAD.search(f.posted["1"]).groups() == ("ran", sha)
    assert "refresh failed" not in f.posted["1"]


def test_a_failed_refresh_keeps_a_completed_snapshot_of_that_head(monkeypatch, tmp_path):
    from datetime import timedelta
    sha = "a" * 40
    prs = [_pr(1, sha)]
    done = _running_comment(sha, NOW - timedelta(minutes=6))
    done["body"] = done["body"].replace("<sub>running on", "<sub>ran on")
    f = Fake(monkeypatch, prs, {sha: [_done(5, sha)]}, fresh=_fresh(prs), comments={1: [done]})
    d = Downloads(monkeypatch, tmp_path, fail=None)
    assert b.run_batch("t", REPO, NOW, head_sha=sha, trigger_workflow=DOCKER) == 1
    assert f.posted == {} and d.dispatches == []  # "ran on" stays and is the head's result: nothing to re-refresh


def test_a_failed_refresh_with_no_comment_for_that_head_writes_a_failed_note(monkeypatch, tmp_path):
    from datetime import timedelta
    old, sha = "1" * 40, "2" * 40
    prs = [_pr(1, sha)]
    f = Fake(monkeypatch, prs, {sha: [_done(5, sha)]}, fresh=_fresh(prs),
             comments={1: [_running_comment(old, NOW + timedelta(hours=1))]})
    Downloads(monkeypatch, tmp_path, fail=None)
    assert b.run_batch("t", REPO, NOW, head_sha=sha, trigger_workflow="CI") == 1
    body = f.posted["1"]
    assert body.startswith(b.BOT_MARKER + "\n" + b.STATE_PREFIX) and old not in body
    assert "refresh failed on [2222222]" in body and BOT_HEAD.search(body) is None


def test_the_sweep_shares_the_retry_but_writes_no_marker_and_queues_nothing(monkeypatch, tmp_path):
    sha = "a" * 40
    prs = [_pr(1, sha), _pr(2, "b" * 40)]
    f = Fake(monkeypatch, prs, {sha: [_done(5, sha)], "b" * 40: [_done(6, "b" * 40)]}, fresh=_fresh(prs))
    d = Downloads(monkeypatch, tmp_path, fail=None)
    assert b.run_batch("t", REPO, NOW) == 1
    # PR 1 spends the whole short budget; PR 2 gets its own (one budget per snapshot).
    assert d.sleeps == list(b.BATCH_DOWNLOAD_RETRY_DELAYS) * 2
    assert f.posted == {} and d.dispatches == []


def test_a_failed_comment_write_is_not_marked_as_a_failed_refresh(monkeypatch, tmp_path):
    sha = "a" * 40
    prs = [_pr(1, sha)]
    f = Fake(monkeypatch, prs, {sha: [_done(5, sha)]}, fresh=_fresh(prs), write_ok={"1": False})
    d = Downloads(monkeypatch, tmp_path, fail=0)
    assert b.run_batch("t", REPO, NOW, head_sha=sha, trigger_workflow="CI") == 1
    assert d.dispatches == [] and f.posted == {}


def test_main_runs_a_dispatched_re_refresh_after_its_capped_delay(monkeypatch):
    head = "d" * 40
    seen, sleeps = {}, []
    monkeypatch.setenv("GITHUB_TOKEN", "t")
    monkeypatch.setenv("GITHUB_REPOSITORY", REPO)
    monkeypatch.setenv("TARGET_HEAD_SHA", "")
    monkeypatch.setenv("DISPATCH_HEAD_SHA", head)
    monkeypatch.setenv("REFRESH_DELAY_SECONDS", "9999")
    monkeypatch.delenv("TRIGGER_WORKFLOW", raising=False)
    monkeypatch.setattr(sys, "argv", ["x"])
    monkeypatch.setattr(b, "_sleep", sleeps.append)
    monkeypatch.setattr(b, "run_batch", lambda token, repo, deadline=None, **kw: seen.update(kw) or 0)
    assert b.main() == 0
    assert seen == {"head_sha": head, "trigger_workflow": ""} and sleeps == [b.MAX_REFRESH_DELAY_S]
    monkeypatch.setenv("REFRESH_DELAY_SECONDS", "5; id")
    assert b.main() == 1
    # A bare dispatch (no head) is the sweep and never waits.
    monkeypatch.setenv("DISPATCH_HEAD_SHA", "")
    monkeypatch.setenv("REFRESH_DELAY_SECONDS", "300")
    sleeps.clear()
    assert b.main() == 0 and seen == {"head_sha": "", "trigger_workflow": ""} and sleeps == []


def test_workflow_can_queue_one_targeted_re_refresh():
    doc = _workflow()
    on = doc.get("on", doc.get(True))
    assert set(on["workflow_dispatch"]["inputs"]) == {"head_sha", "delay_seconds", "retry_of"}
    delay_doc = on["workflow_dispatch"]["inputs"]["delay_seconds"]["description"]
    assert f"capped at {b.MAX_REFRESH_DELAY_S})" in delay_doc
    assert doc["permissions"]["actions"] == "write"
    step = next(s for s in doc["jobs"]["comment"]["steps"] if "batch_review_comments.py" in (s.get("run") or ""))
    assert step["env"]["DISPATCH_HEAD_SHA"] == "${{ inputs.head_sha }}"
    assert step["env"]["REFRESH_DELAY_SECONDS"] == "${{ inputs.delay_seconds }}"
    assert step["env"]["DEFAULT_BRANCH"] == "${{ github.event.repository.default_branch }}"
    assert "${{" not in step["run"]
    # The re-refresh waits in its own per-head group: never cancelled by, nor holding, the sweep.
    assert "format('-retry-{0}', inputs.head_sha)" in doc["concurrency"]["group"]
    assert b.WORKFLOW_FILE == "ci-review-comment.yml"
    assert doc["jobs"]["comment"]["timeout-minutes"] * 60 > b.MAX_REFRESH_DELAY_S + sum(b.TARGETED_DOWNLOAD_RETRY_DELAYS)


# --- PR1699 review follow-ups (card t_01a1d1827d121b26) -----------------------------------------------------------
# Review 5880960246 nits 2 and 3: the retry budget counted only sleeps (each attempt can also spend the archive API
# and blob timeouts, ~90 s), so a delayed re-refresh or a sweep in an artifact outage could run past the job timeout;
# and a failed refresh queued a re-refresh even when it kept a completed snapshot, and once per failed CI/Docker run.


class Clock:
    """A fake monotonic clock: sleeps and timed-out downloads advance it."""

    def __init__(self, monkeypatch, d: Downloads, attempt_s: float):
        self.t = 0.0
        monkeypatch.setattr(b, "_clock", lambda: self.t)
        monkeypatch.setattr(b, "_sleep", self.sleep)
        self.sleeps = d.sleeps
        inner = d.download

        def download(*args):
            self.t += attempt_s  # a failed attempt that ran into the archive API + blob timeouts
            return inner(*args)

        monkeypatch.setattr(b.lc, "_download_artifact", download)

    def sleep(self, s):
        self.sleeps.append(s)
        self.t += s


def _main_env(monkeypatch, head="", delay="", trigger=""):
    monkeypatch.setenv("GITHUB_TOKEN", "t")
    monkeypatch.setenv("GITHUB_REPOSITORY", REPO)
    monkeypatch.setenv("TARGET_HEAD_SHA", head if trigger else "")
    monkeypatch.setenv("DISPATCH_HEAD_SHA", "" if trigger else head)
    monkeypatch.setenv("REFRESH_DELAY_SECONDS", delay)
    monkeypatch.setenv("TRIGGER_WORKFLOW", trigger)
    monkeypatch.setattr(sys, "argv", ["x"])


def test_attempt_cost_matches_the_download_timeouts(monkeypatch, tmp_path):
    """One _download_artifact attempt passes exactly the two counted socket timeouts (archive API, then blob)."""
    import urllib.error
    import urllib.request
    from email.message import Message
    seen = []
    hdrs = Message()
    hdrs["Location"] = "https://blob/x"

    class Opener:
        def open(self, req, timeout=None):
            seen.append(timeout)
            raise urllib.error.HTTPError(req.full_url, 302, "Found", hdrs, None)

    def urlopen(req, timeout=None):
        seen.append(timeout)
        raise OSError("blob timed out")

    monkeypatch.setattr(b.lc.urllib.request, "build_opener", lambda *handlers: Opener())
    monkeypatch.setattr(b.lc.urllib.request, "urlopen", urlopen)
    art = {"name": "review-status-x", "archive_download_url": "https://api/x/zip"}
    assert b.lc._download_artifact("t", REPO, art, tmp_path) is None
    assert seen == [b.lc.ARCHIVE_API_TIMEOUT_S, b.lc.BLOB_TIMEOUT_S]
    assert sum(seen) == b.lc.DOWNLOAD_ATTEMPT_MAX_S


def test_a_delayed_re_refresh_stops_retrying_at_the_wall_clock_budget(monkeypatch, tmp_path):
    """The dispatched re-refresh waits RE_REFRESH_DELAY_S, then every attempt times out (~90 s): retries stop while
    the run can still finish inside its budget, and the comment is still marked failed."""
    from datetime import timedelta
    sha = "a" * 40
    prs = [_pr(1, sha)]
    f = Fake(monkeypatch, prs, {sha: [_done(5, sha)]}, fresh=_fresh(prs),
             comments={1: [_running_comment(sha, datetime.now(UTC) - timedelta(minutes=6))]})
    d = Downloads(monkeypatch, tmp_path, fail=None)
    c = Clock(monkeypatch, d, b.lc.DOWNLOAD_ATTEMPT_MAX_S)
    _main_env(monkeypatch, head=sha, delay=str(b.RE_REFRESH_DELAY_S))
    assert b.main() == 1
    assert c.sleeps[0] == b.RE_REFRESH_DELAY_S
    assert c.t <= b.RUN_BUDGET_S  # no attempt was started that could not finish inside the budget
    assert 1 <= len(c.sleeps) - 1 < len(b.TARGETED_DOWNLOAD_RETRY_DELAYS)  # some retries, not the whole list
    assert "refresh failed on" in f.posted["1"] and "retry budget" in f.posted["1"]
    assert d.dispatches == []


def test_the_run_budget_leaves_headroom_under_the_job_timeout():
    doc = _workflow()
    timeout_s = doc["jobs"]["comment"]["timeout-minutes"] * 60
    assert b.JOB_TIMEOUT_S == timeout_s
    # The budget plus one in-flight write and the job's checkout/setup stay inside the timeout.
    assert b.RUN_BUDGET_S + b.RUN_HEADROOM_S <= timeout_s and b.RUN_HEADROOM_S >= 120
    # The longest delayed re-refresh still leaves room after its wait for a full attempt, the first retry's wait and a
    # second full attempt, with the PR's own reads: a real retry, and slack for the PR listing (not just equality).
    after_wait = (2 * b.lc.DOWNLOAD_ATTEMPT_MAX_S + b.TARGETED_DOWNLOAD_RETRY_DELAYS[0] + b.PR_MIN_BUDGET_S)
    assert b.MAX_REFRESH_DELAY_S + after_wait < b.RUN_BUDGET_S


def test_a_dispatch_at_the_capped_delay_still_refreshes_the_pr(monkeypatch, tmp_path):
    """Review 5887164857 nit 1: a manual dispatch with delay_seconds >= the cap waited the cap, then the PR listing
    took a moment and the budget gate skipped the PR before any attempt, leaving "running on"."""
    from datetime import timedelta
    sha = "a" * 40
    prs = [_pr(1, sha)]
    f = Fake(monkeypatch, prs, {sha: [_done(5, sha)]}, fresh=_fresh(prs),
             comments={1: [_running_comment(sha, datetime.now(UTC) - timedelta(minutes=20))]})
    d = Downloads(monkeypatch, tmp_path, fail=0)
    c = Clock(monkeypatch, d, 1.0)
    listing = b.lc._api_get_paginated

    def slow_listing(url, token, list_key=None):
        c.t += 2.0  # each API read takes a moment
        return listing(url, token, list_key)

    monkeypatch.setattr(b.lc, "_api_get_paginated", slow_listing)
    _main_env(monkeypatch, head=sha, delay="9999")
    assert b.main() == 0
    assert c.sleeps[0] == b.MAX_REFRESH_DELAY_S
    m = BOT_HEAD.search(f.posted["1"])
    assert m and m.groups() == ("ran", sha)


def test_a_targeted_run_skipped_by_the_budget_gate_marks_the_comment_failed(monkeypatch, tmp_path):
    """Review 5887164857 nit 1: whatever spends the budget before a targeted PR starts, its "running on" comment must
    not stand as if live: the PR is marked "refresh failed on <head>" naming the run budget."""
    from datetime import timedelta
    sha = "a" * 40
    prs = [_pr(1, sha)]
    f = Fake(monkeypatch, prs, {sha: [_done(5, sha)]}, fresh=_fresh(prs),
             comments={1: [_running_comment(sha, NOW - timedelta(minutes=20))]})
    d = Downloads(monkeypatch, tmp_path, fail=0)
    c = Clock(monkeypatch, d, 1.0)
    c.t = b.RUN_BUDGET_S - 10  # the delay and listing left less than one attempt
    assert b.run_batch("t", REPO, NOW, head_sha=sha, trigger_workflow="", deadline=float(b.RUN_BUDGET_S)) == 1
    assert d.calls == 0 and f.collected == []
    body = f.posted["1"]
    assert BOT_HEAD.search(body) is None and "refresh failed on [aaaaaaa]" in body and "run budget" in body
    assert d.dispatches == []  # the re-refresh itself (a dispatch) never queues another


def test_a_ci_triggered_run_skipped_by_the_budget_gate_queues_one_re_refresh(monkeypatch, tmp_path, capsys):
    """Review 5888188312 nit 1: the CI-triggered branch (re_refresh=True) of the budget-skip marker queues exactly one
    re-refresh and records ``requeued=`` in the comment; nit 2: its log line names that, not "the next sweep"."""
    from datetime import timedelta
    sha = "a" * 40
    prs = [_pr(1, sha)]
    f = Fake(monkeypatch, prs, {sha: [_done(5, sha)]}, fresh=_fresh(prs),
             comments={1: [_running_comment(sha, NOW - timedelta(minutes=20))]})
    d = Downloads(monkeypatch, tmp_path, fail=0)
    c = Clock(monkeypatch, d, 1.0)
    c.t = b.RUN_BUDGET_S - 10
    assert b.run_batch("t", REPO, NOW, head_sha=sha, trigger_workflow="CI", deadline=float(b.RUN_BUDGET_S)) == 1
    assert d.calls == 0 and f.collected == []
    body = f.posted["1"]
    assert BOT_HEAD.search(body) is None and "refresh failed on [aaaaaaa]" in body and "run budget" in body
    assert "re-refresh queued" in body and b.requeued_at(body, sha) is not None
    assert body.count(b.STATE_PREFIX) == 1 and " requeued=" in body
    assert len(d.dispatches) == 1
    assert d.dispatches[0][1]["inputs"]["head_sha"] == sha
    err = capsys.readouterr().err
    assert "the next sweep takes them" not in err
    assert "one re-refresh per head is queued" in err


def test_budget_skip_log_line_is_worded_per_mode(monkeypatch, tmp_path, capsys):
    """Review 5888188312 nit 2: the sweep defers to the next sweep; a targeted re-refresh (no trigger) marks the PR
    failed and points at the hourly sweep, never claiming a re-refresh was queued."""
    from datetime import timedelta
    sha = "a" * 40
    prs = [_pr(1, sha)]
    f = Fake(monkeypatch, prs, {sha: [_done(5, sha)]}, fresh=_fresh(prs),
             comments={1: [_running_comment(sha, NOW - timedelta(minutes=20))]})
    d = Downloads(monkeypatch, tmp_path, fail=0)
    c = Clock(monkeypatch, d, 1.0)
    c.t = b.RUN_BUDGET_S - 10
    assert b.run_batch("t", REPO, NOW, deadline=float(b.RUN_BUDGET_S)) == 1
    err = capsys.readouterr().err
    assert "the next sweep takes them" in err and "marked refresh failed" not in err
    f.posted.clear()
    assert b.run_batch("t", REPO, NOW, head_sha=sha, trigger_workflow="", deadline=float(b.RUN_BUDGET_S)) == 1
    err = capsys.readouterr().err
    assert "the next sweep takes them" not in err and "re-refresh" not in err.split("\n")[0]
    assert "marked refresh failed; the hourly sweep is next" in err
    assert d.dispatches == []


def test_the_sweep_skipped_by_the_budget_gate_writes_no_marker(monkeypatch, tmp_path):
    from datetime import timedelta
    sha = "a" * 40
    prs = [_pr(1, sha)]
    f = Fake(monkeypatch, prs, {sha: [_done(5, sha)]}, fresh=_fresh(prs),
             comments={1: [_running_comment(sha, NOW - timedelta(minutes=20))]})
    d = Downloads(monkeypatch, tmp_path, fail=0)
    c = Clock(monkeypatch, d, 1.0)
    c.t = b.RUN_BUDGET_S - 10
    assert b.run_batch("t", REPO, NOW, deadline=float(b.RUN_BUDGET_S)) == 1
    assert f.posted == {} and d.dispatches == []


def test_a_large_sweep_in_an_artifact_outage_stops_before_the_job_timeout(monkeypatch, tmp_path):
    """65 open PRs whose downloads all time out: the sweep stops starting PRs once the budget is spent (the next
    sweep takes them), never runs past the budget, and exits red."""
    prs = [_pr(n, f"{n:040x}") for n in range(1, 66)]
    f = Fake(monkeypatch, prs, {p["head"]["sha"]: [_done(p["number"], p["head"]["sha"])] for p in prs},
             fresh=_fresh(prs))
    d = Downloads(monkeypatch, tmp_path, fail=None)
    c = Clock(monkeypatch, d, b.lc.DOWNLOAD_ATTEMPT_MAX_S)
    _main_env(monkeypatch)
    assert b.main() == 1
    assert c.t <= b.RUN_BUDGET_S
    assert 1 <= len(f.collected) < len(prs)
    assert f.posted == {} and d.dispatches == []


def test_a_healthy_sweep_is_not_cut_short(monkeypatch, tmp_path):
    prs = [_pr(n, f"{n:040x}") for n in range(1, 66)]
    f = Fake(monkeypatch, prs, {p["head"]["sha"]: [_done(p["number"], p["head"]["sha"])] for p in prs},
             fresh=_fresh(prs))
    d = Downloads(monkeypatch, tmp_path, fail=0)
    Clock(monkeypatch, d, 1.0)
    _main_env(monkeypatch)
    assert b.main() == 0 and len(f.posted) == 65


def test_no_artifact_download_starts_once_the_budget_cannot_fit_it(monkeypatch, tmp_path):
    """Two review-status artifacts; the first one's timed-out attempt leaves less than one attempt of budget: the
    second artifact's download is never started (it would run past the wall-clock budget)."""
    t = [0.0]
    started = []

    def download(token, repo, artifact, dest_dir):
        started.append(artifact["name"])
        t[0] += b.lc.DOWNLOAD_ATTEMPT_MAX_S
        b.lc.last_download_error = "blob download timed out"

    monkeypatch.setattr(b.lc, "_list_artifacts",
                        lambda *a: [{"name": "review-status-ci-timings"}, {"name": "review-status-docker"}])
    monkeypatch.setattr(b.lc, "_download_artifact", download)
    monkeypatch.setattr(b.lc.tempfile, "gettempdir", lambda: str(tmp_path))
    deadline = 1.5 * b.lc.DOWNLOAD_ATTEMPT_MAX_S
    b.lc.fetch_all_review_statuses("t", "o/r", "1", strict=False, retry_delays=(),
                                   sleep=lambda s: None, deadline=deadline, clock=lambda: t[0])
    assert started == ["review-status-ci-timings"]
    assert t[0] <= deadline


def test_strict_refuses_an_artifact_the_budget_cannot_fit_after_a_good_one(monkeypatch, tmp_path):
    """Strict snapshot: the first artifact downloads, but leaves less than one attempt of budget, so the second is
    refused as unreadable (never started, never silently dropped from the snapshot)."""
    t = [0.0]
    started = []

    def download(token, repo, artifact, dest_dir):
        started.append(artifact["name"])
        t[0] += b.lc.DOWNLOAD_ATTEMPT_MAX_S
        path = Path(dest_dir) / f"{artifact['name']}.json"
        path.write_text(json.dumps([{"source": artifact["name"], "results": []}]), encoding="utf-8")
        return path

    monkeypatch.setattr(b.lc, "_list_artifacts",
                        lambda *a: [{"name": "review-status-ci-timings"}, {"name": "review-status-docker"}])
    monkeypatch.setattr(b.lc, "_download_artifact", download)
    monkeypatch.setattr(b.lc.tempfile, "gettempdir", lambda: str(tmp_path))
    with pytest.raises(RuntimeError, match="review-status-docker.*budget"):
        b.lc.fetch_all_review_statuses("t", "o/r", "1", strict=True, retry_delays=(),
                                       sleep=lambda s: None, deadline=1.5 * b.lc.DOWNLOAD_ATTEMPT_MAX_S,
                                       clock=lambda: t[0])
    assert started == ["review-status-ci-timings"]


def test_a_failed_refresh_that_keeps_a_completed_snapshot_queues_no_re_refresh(monkeypatch, tmp_path):
    from datetime import timedelta
    sha = "a" * 40
    prs = [_pr(1, sha)]
    done = _running_comment(sha, NOW - timedelta(minutes=6))
    done["body"] = done["body"].replace("<sub>running on", "<sub>ran on")
    for trigger in ("CI", DOCKER):
        f = Fake(monkeypatch, prs, {sha: [_done(5, sha)]}, fresh=_fresh(prs), comments={1: [done]})
        d = Downloads(monkeypatch, tmp_path, fail=None)
        assert b.run_batch("t", REPO, NOW, head_sha=sha, trigger_workflow=trigger) == 1
        assert f.posted == {} and d.dispatches == []


def test_ci_and_docker_failures_on_one_head_queue_one_re_refresh(monkeypatch, tmp_path):
    """CI's refresh fails and queues the re-refresh; Docker's later refresh of the same head fails too and must not
    queue a second one. The queued mark survives the re-refresh's own failed write."""
    from datetime import timedelta
    sha = "a" * 40
    prs = [_pr(1, sha)]
    f = Fake(monkeypatch, prs, {sha: [_done(5, sha)]}, fresh=_fresh(prs),
             comments={1: [_running_comment(sha, NOW - timedelta(minutes=6))]})
    d = Downloads(monkeypatch, tmp_path, fail=None)
    assert b.run_batch("t", REPO, NOW, head_sha=sha, trigger_workflow="CI") == 1
    assert len(d.dispatches) == 1
    f.comments = {1: [{"id": 77, "body": f.posted.pop("1")}]}
    assert b.run_batch("t", REPO, NOW + timedelta(minutes=1), head_sha=sha, trigger_workflow=DOCKER) == 1
    assert len(d.dispatches) == 1
    body = f.posted.pop("1")
    assert "re-refresh already queued" in body and body.count(b.STATE_PREFIX) == 1
    # The re-refresh itself fails: still one dispatch, and the queued mark is kept for any later CI/Docker failure.
    f.comments = {1: [{"id": 77, "body": body}]}
    assert b.run_batch("t", REPO, NOW + timedelta(minutes=6), head_sha=sha, trigger_workflow="") == 1
    body = f.posted.pop("1")
    # Its footer points at the hourly sweep (the queued re-refresh was this run), and the state line keeps the mark.
    assert "next refresh: hourly sweep" in body and "already queued" not in body
    assert b.requeued_at(body, sha) is not None
    f.comments = {1: [{"id": 77, "body": body}]}
    assert b.run_batch("t", REPO, NOW + timedelta(minutes=7), head_sha=sha, trigger_workflow="CI") == 1
    assert len(d.dispatches) == 1


def test_a_new_head_or_a_successful_refresh_clears_the_queued_mark(monkeypatch, tmp_path):
    from datetime import timedelta
    old, sha = "1" * 40, "2" * 40
    prs = [_pr(1, sha)]
    f = Fake(monkeypatch, prs, {sha: [_done(5, sha)]}, fresh=_fresh(prs),
             comments={1: [_running_comment(old, NOW - timedelta(minutes=6))]})
    d = Downloads(monkeypatch, tmp_path, fail=None)
    queued_old = b.with_state(_running_comment(old, NOW)["body"], old, NOW - timedelta(minutes=6), "1",
                              failed=NOW - timedelta(minutes=5), requeued=NOW - timedelta(minutes=5))
    f.comments = {1: [{"id": 77, "body": queued_old}]}
    assert b.run_batch("t", REPO, NOW, head_sha=sha, trigger_workflow="CI") == 1
    assert len(d.dispatches) == 1  # another head's mark does not count
    # A successful refresh of the head drops the mark.
    f.comments = {1: [{"id": 77, "body": f.posted.pop("1")}]}
    d.fail, d.calls = 0, 0
    assert b.run_batch("t", REPO, NOW + timedelta(minutes=30), head_sha=sha, trigger_workflow="CI") == 0
    m = b._STATE_RE.search(f.posted["1"])
    assert m and not m.group(4) and not m.group(5)


def test_a_marker_skipped_for_a_moved_head_queues_nothing(monkeypatch, tmp_path):
    sha = "a" * 40
    prs = [_pr(1, sha)]
    fresh = {1: {"state": "open", "head": {"sha": "b" * 40}}}
    Fake(monkeypatch, prs, {sha: [_done(5, sha)]}, fresh=fresh)
    d = Downloads(monkeypatch, tmp_path, fail=None)
    assert b.run_batch("t", REPO, NOW, head_sha=sha, trigger_workflow="CI") == 1
    assert d.dispatches == []
