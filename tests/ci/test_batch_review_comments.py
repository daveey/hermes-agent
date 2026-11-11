"""Tests for scripts/ci/batch_review_comments.py (no network)."""

from __future__ import annotations

import importlib.util
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

    def __init__(self, monkeypatch, prs, runs_by_sha, fresh=None, write_ok=None, fail_collect=()):
        self.prs, self.runs_by_sha = prs, runs_by_sha
        self.fresh = fresh or {}
        self.write_ok = write_ok or {}
        self.fail_collect = set(fail_collect)
        self.posted: dict[str, str] = {}
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
            raise AssertionError(url)

        def request(url, token):
            n = int(url.rsplit("/", 1)[1])
            return self.fresh[n]

        def collect(token, repo, run_id, watch=None):
            self.collected.append(run_id)
            if run_id in self.fail_collect:
                raise RuntimeError("boom")
            return [], True

        def upsert(token, repo, pr, body, comment_id=None):
            if not self.write_ok.get(pr, True):
                return None
            self.posted[pr] = body
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
    assert "snapshot of" in body and "refreshed hourly, not live" in body
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


def test_workflow_shape():
    yaml = pytest.importorskip("hermes_yaml")
    doc = yaml.safe_load((Path(__file__).resolve().parents[2] / ".github/workflows/ci-review-comment.yml").read_text())
    on = doc.get("on", doc.get(True))
    assert set(on) == {"schedule", "workflow_dispatch"}
    assert on["schedule"] == [{"cron": "17 * * * *"}]
    assert doc["concurrency"]["cancel-in-progress"] is False
    job = doc["jobs"]["comment"]
    assert job["timeout-minutes"] == 15 and job["runs-on"] == "blacksmith-2vcpu-ubuntu-2404"
    assert any("batch_review_comments.py" in (s.get("run") or "") for s in job["steps"])
