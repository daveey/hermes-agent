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


# owner/repo#N (2026-11-21): core-ship-backstop files "Retro exact-head review: <owner>/<repo>#N merged without a
# hermes-core-wt/ship receipt". ``state#12``'s rule (no ``#`` after a word character) read no PR in those titles, so
# each retro for another PR collided with any open one and the backstop stalled from 03:41Z (worked around in state
# PR #3876). A ref now carries its repo; an unqualified ref matches any repo.
RETRO_3855 = ("Retro exact-head review: softmax-universe/daveey-hermes#3855 merged without a hermes-core-wt/ship "
              "receipt")
RETRO_3863 = RETRO_3855.replace("#3855", "#3863")


def test_retro_reviews_of_different_owner_repo_prs_are_not_duplicates(worker, tmp_path):
    remote(tmp_path, RETRO_3855)
    assert check(RETRO_3863) is None
    assert ko._different_prs(RETRO_3863, RETRO_3855)


def test_same_owner_repo_pr_still_matches(worker, tmp_path):
    remote(tmp_path, RETRO_3855)
    assert not ko._different_prs(RETRO_3855, RETRO_3855.replace("softmax-universe", "Softmax-Universe"))
    out = check(RETRO_3855)
    assert out and out["code"] == "create_overlap" and "titles share" in out["matches"][0]["why"], out


def test_same_number_in_different_repos_is_different_prs():
    assert ko._different_prs("Retro review a/b#55 merged", "Retro review c/d#55 merged")


def test_unqualified_ref_matches_any_repo():
    assert not ko._different_prs("Retro review a/b#55 merged", "Retro review #55 merged")
    assert not ko._different_prs("Retro review PR 55 merged", "Retro review c/d#55 merged")


def test_pr_refs_carry_the_repo():
    assert ko.pr_refs(RETRO_3855) == {("softmax-universe/daveey-hermes", 3855)}
    assert ko.pr_refs("Owner-1/Repo_x.y#42 and #43") == {("owner-1/repo_x.y", 42), (None, 43)}
    assert ko.pr_refs("see https://github.com/Softmax-Universe/daveey-hermes/pull/3876") == {
        ("softmax-universe/daveey-hermes", 3876)}
    assert ko.pr_refs("Review #2799, PR139: x, PR 173, pr #42 and pull/3050") == {
        (None, 2799), (None, 139), (None, 173), (None, 42), (None, 3050)}
    assert ko.pr_numbers(RETRO_3855) == {3855}


@pytest.mark.parametrize("title", ["foo#12", "##12", "a/b#1", "a/b/c#12", "a/b#1234567", "x hermes-core-wt/ship y"])
def test_not_pr_refs(title):
    assert ko.pr_refs(title) == frozenset()
    assert ko.pr_numbers(title) == frozenset()


def test_dot_git_repo_is_the_same_pr():
    assert not ko._different_prs("Retro review a/b.git#55 merged", "Retro review a/b#55 merged")
    assert not ko._different_prs("see https://github.com/a/b.git/pull/55", "Retro review a/b#55 merged")
    assert ko.pr_refs("a/b.git#55") == {("a/b", 55)}


def test_pr_followed_by_many_spaces_is_linear():
    import time
    start = time.monotonic()
    assert ko.pr_refs("pr" + " " * 20000 + "x") == frozenset()
    assert time.monotonic() - start < 0.5
    assert ko.pr_refs("PR #55") == {(None, 55)} and ko.pr_refs("pr # 55") == {(None, 55)}
