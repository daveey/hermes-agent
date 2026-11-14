"""kanban_create does not call review cards for different PRs duplicates by shared template words. [daveey]

Incident (2026-11-11..14, t_01a1db274c88e2e3): ``hermes-core-wt ship --request-review`` titles every review
"Independent exact-head review PR #N @<sha> (core-wt ship review required)". Those template words are ~80% of each
title, so the title-share rule refused reviews of other PRs (36/33/29 a day, plus ~90/day ``distinct_from``
overrides). Refused reviews re-created as "Review PR139 ..." lost the strong-tier keep pattern. Titles that both name
PRs, with no PR in common, no longer match by shared words; the same PR, or a PR on one side only, is unchanged.
"""
from __future__ import annotations

import json

import pytest

from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_overlap as ko
from tests.tools import test_kanban_create_overlap as helpers
from tests.tools.test_kanban_create_overlap_shared_file import HUB_CARD, WORKER_DUP

home = helpers.home

REVIEW_3050 = "Independent exact-head review PR #3050 @478094efef (core-wt ship review required)"
REVIEW_3056 = ("Independent exact-head review state PR #3056 @6898005a52 "
               "(core-wt ship: lease the topic-branch push)")
PR166_NEW = "Independent exact-head review hermes-mods PR166 M9/M10/M5 tests"
PR166_OPEN = "Independent review mods PR166 exact53e08858: user-steps M9/M10/M5 tests"


def remote(tmp_path, title, *, cid="t_01a1db12b45dec5f"):
    (tmp_path / "hub-state" / "hub.db").unlink(missing_ok=True)
    helpers._fake_replica(tmp_path / "hub-state" / "hub.db", [
        ("hub", {"id": cid, "board": "default", "title": title, "body": "", "status": "running",
                 "gateway": "hub", "sessions": [{"session": "20261114_172000_aaaaaa", "rel": "created"}]})])


def check(title):
    with kbc.connect_closing() as conn:
        refusal, _ = ko.check(conn, {"body": ""}, courier=False, title=title, session_id="worker-7-run",
                              self_tid=None, parents=[])
    return json.loads(refusal) if refusal else None


@pytest.fixture
def worker(home, monkeypatch):
    from hermes_cli import kanban_capability_registry as reg
    monkeypatch.setattr(reg, "self_gateway", lambda: "worker-7")
    return home


def test_reviews_of_different_prs_are_not_duplicates(worker, tmp_path):
    """The 11-14 17:22:14Z refusal: #3050 against open #3056 on 8 shared template words."""
    remote(tmp_path, REVIEW_3056)
    assert check(REVIEW_3050) is None


@pytest.mark.parametrize("pr", ["#3020", "PR 3020", "PR3020", "pr #3020", "pull/3020"])
def test_every_pr_form_tells_reviews_apart(worker, tmp_path, pr):
    """Same template, so each pair shares 8-9 of 10-11 words (#3020 at 17:26:59Z was refused like this)."""
    remote(tmp_path, REVIEW_3050)
    assert check(REVIEW_3050.replace("PR #3050 @478094efef", f"{pr} @1a2b3c4d5e")) is None


def test_same_pr_review_still_matches(worker, tmp_path):
    """The 11-14 03:12:46Z refusal of a second PR166 review against t_01a1d803ac2c9d7c."""
    remote(tmp_path, PR166_OPEN, cid="t_01a1d803ac2c9d7c")
    out = check(PR166_NEW)
    assert out and out["code"] == "create_overlap", out
    (m,) = out["matches"]
    assert m["id"] == "t_01a1d803ac2c9d7c" and "titles share" in m["why"], m


def test_overlapping_pr_sets_still_match(worker, tmp_path):
    remote(tmp_path, REVIEW_3056.replace("PR #3056", "PR #3056 and #3050"))
    out = check(REVIEW_3050)
    assert out and "titles share" in out["matches"][0]["why"], out


def test_pr_on_one_side_only_matches_as_before(worker, tmp_path):
    remote(tmp_path, REVIEW_3056.replace("PR #3056 ", ""))
    out = check(REVIEW_3050)
    assert out and "titles share" in out["matches"][0]["why"], out


def test_run_ci_pair_still_matches(worker, tmp_path):
    """t_01a1d290dc768ba1's pair: no PR on either side, matched by the shared file."""
    remote(tmp_path, HUB_CARD, cid="t_01a1d25511610669")
    out = check(WORKER_DUP)
    assert out and "run_ci.py" in out["matches"][0]["why"], out


def test_shared_file_reason_does_not_join_different_prs(worker, tmp_path):
    remote(tmp_path, f"{HUB_CARD} (PR #3056)", cid="t_01a1d25511610669")
    assert check(f"{WORKER_DUP} (PR #3050)") is None


def test_pr_numbers_forms():
    assert ko.pr_numbers("Review #2799, PR139: x, PR 173, pr #42 and pull/3050") == {2799, 139, 173, 42, 3050}
    assert ko.pr_numbers("@478094efef M9/M10 exact53e08858 #1 #1234567 state#12 spring 2026") == frozenset()
