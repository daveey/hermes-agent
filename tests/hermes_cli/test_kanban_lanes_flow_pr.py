"""Exercise the actual read-only gh subprocess boundary without network."""
import sys
import time

import pytest

from hermes_cli import kanban_db_dispatch_pr_state as shared
from hermes_cli.kanban_lanes_flow_pr import Reader


@pytest.mark.parametrize("state,decision,buckets,expected", [
    ("OPEN", "APPROVED", ["pass"], {"merged": False, "approved": True, "checks": "green"}),
    ("OPEN", "REVIEW_REQUIRED", ["pending"], {"merged": False, "approved": False, "checks": "pending"}),
    ("OPEN", "APPROVED", [], {"merged": False, "approved": True}),
    ("OPEN", "APPROVED", ["fail"], {"merged": False, "approved": True}),
    ("MERGED", "APPROVED", [], {"merged": True}),
    ("CLOSED", "APPROVED", [], {}),
])
def test_real_executable_observations(tmp_path, monkeypatch, state, decision, buckets, expected):
    script = tmp_path / "fixture_gh.py"
    view = {"state": state, "reviewDecision": decision, "headRefOid": "a" * 40}
    checks = [{"bucket": b} for b in buckets]
    script.write_text("import json,sys\n" +
                      f"print(json.dumps({view!r} if 'view' in sys.argv else {checks!r}))\n")
    monkeypatch.setattr(shared, "_gh_argv", lambda: [sys.executable, str(script)])
    reader = Reader()
    assert reader.get("https://github.com/example/repo/pull/1") == expected
    # A cached result must not spawn again, even when executable disappears.
    script.unlink()
    assert reader.get("https://github.com/example/repo/pull/1") == expected


@pytest.mark.parametrize("body", ["print('not json')", "raise SystemExit(1)", "print('[]')"])
def test_unreadable_observation_does_not_guess(tmp_path, monkeypatch, body):
    script = tmp_path / "fixture_gh.py"
    script.write_text(body)
    monkeypatch.setattr(shared, "_gh_argv", lambda: [sys.executable, str(script)])
    assert Reader().get("https://github.com/example/repo/pull/1") == {}


def test_exhausted_budget_never_spawns(monkeypatch):
    reader = Reader()
    reader.deadline = 0
    monkeypatch.setattr(shared, "_gh_argv", lambda: pytest.fail("no work after deadline"))
    assert reader.get("https://github.com/example/repo/pull/1") == {}


def test_missing_executable_is_unknown(monkeypatch):
    monkeypatch.setattr(shared, "_gh_argv", lambda: ["/nonexistent/fixture-gh"])
    assert Reader().get("https://github.com/example/repo/pull/1") == {}


@pytest.mark.parametrize("change", ["headRefOid", "state", "reviewDecision", "missing", "absent_head"])
def test_changed_head_or_review_during_checks_is_unknown(monkeypatch, change):
    first = {"state": "OPEN", "headRefOid": "a" * 40, "reviewDecision": "APPROVED"}
    last = dict(first)
    if change == "missing":
        last = None
    elif change == "absent_head":
        first.pop("headRefOid")
        last.pop("headRefOid")
    else:
        last[change] = "different"
    views = iter([first, last])

    def read(self, argv):
        return [{"bucket": "pass"}] if "checks" in argv else next(views)
    monkeypatch.setattr(Reader, "_read", read)
    assert Reader().get("https://github.com/example/repo/pull/1") == {}


def _review(login, state, oid, bot=False):
    return {"author": {"login": login, "is_bot": bot}, "state": state, "commit": {"oid": oid}}


@pytest.mark.parametrize("body,oid,bot,expected", [
    ("VERDICT: APPROVE\nIndependent exact-head review.", "a" * 40, False, True),
    ("\nVERDICT: APPROVE\n", "a" * 40, False, True),
    ("**Verdict: APPROVE** at exact head `" + "a" * 40 + "` (independent exact-head review).",
     "a" * 40, False, True),
    ("VERDICT: APPROVED", "a" * 40, False, False),
    ("VERDICT: REQUEST_CHANGES\n", "a" * 40, False, False),
    ("VERDICT: APPROVE\n", "b" * 40, False, False),
    ("VERDICT: APPROVE\n", "a" * 40, True, False),
    ("Quoted verdict below\nVERDICT: APPROVE", "a" * 40, False, False),
    ("Looks approved to me", "a" * 40, False, False),
])
def test_fleet_comment_verdict_same_github_identity(monkeypatch, body, oid, bot, expected):
    review = dict(_review("author", "COMMENTED", oid, bot), body=body)
    view = {"state": "OPEN", "headRefOid": "a" * 40, "reviewDecision": "",
            "author": {"login": "author"}, "reviews": [review]}
    monkeypatch.setattr(Reader, "_read", lambda self, argv: [{"bucket": "pass"}] if "checks" in argv else view)
    from hermes_cli.kanban_lanes_flow import classify_step
    observed = Reader().get("https://github.com/example/repo/pull/1")
    assert observed["approved"] is expected
    assert classify_step({"status": "scheduled", "pr": observed}) == (
        "merge" if expected else "landing_unclassified")


def test_fleet_comment_change_request_supersedes_approval(monkeypatch):
    reviews = [dict(_review("author", "COMMENTED", "a" * 40), body=f"VERDICT: {v}")
               for v in ("APPROVE", "REQUEST_CHANGES")]
    view = {"state": "OPEN", "headRefOid": "a" * 40, "reviewDecision": "",
            "author": {"login": "author"}, "reviews": reviews}
    monkeypatch.setattr(Reader, "_read", lambda self, argv: [{"bucket": "pass"}] if "checks" in argv else view)
    assert Reader().get("https://github.com/example/repo/pull/1")["approved"] is False


@pytest.mark.parametrize("reviews,approved", [
    ([_review("rev", "APPROVED", "a" * 40)], True),
    ([_review("rev", "APPROVED", "b" * 40)], False),                          # older head
    ([_review("rev", "APPROVED", "a" * 40), _review("rev", "CHANGES_REQUESTED", "a" * 40)], False),
    ([_review("rev", "CHANGES_REQUESTED", "a" * 40), _review("rev", "APPROVED", "a" * 40)], True),
    ([_review("rev", "APPROVED", "a" * 40), _review("other", "CHANGES_REQUESTED", "a" * 40)], False),
    ([_review("author", "APPROVED", "a" * 40)], False),                       # self-approval
    ([_review("ci[bot]", "APPROVED", "a" * 40, bot=True)], False),
    ([], False),
])
def test_empty_review_decision_still_reaches_merge_via_independent_approval(monkeypatch, reviews, approved):
    view = {"state": "OPEN", "headRefOid": "a" * 40, "reviewDecision": "", "author": {"login": "author"},
            "reviews": reviews}
    monkeypatch.setattr(Reader, "_read", lambda self, argv: [{"bucket": "pass"}] if "checks" in argv else view)
    assert Reader().get("https://github.com/example/repo/pull/1")["approved"] is approved


def test_slow_brokered_gh_fits_budget_across_many_prs(tmp_path, monkeypatch):
    """Every gh call takes ~1.2 s (fleet: 3-4.5 s); six PRs read serially would need ~21 s."""
    script = tmp_path / "slow_gh.py"
    view = {"state": "OPEN", "reviewDecision": "", "headRefOid": "a" * 40, "author": {"login": "x"},
            "reviews": [_review("rev", "APPROVED", "a" * 40)]}
    script.write_text("import json,sys,time\ntime.sleep(1.2)\n"
                      f"print(json.dumps({view!r} if 'view' in sys.argv else [{{'bucket': 'pass'}}]))\n")
    monkeypatch.setattr(shared, "_gh_argv", lambda: [sys.executable, str(script)])
    urls = [f"https://github.com/example/repo/pull/{n}" for n in range(6)]
    reader = Reader(budget=10)
    start = time.monotonic()
    reader.prefetch(urls)
    assert time.monotonic() - start < 9
    for url in urls:
        assert reader.get(url) == {"merged": False, "approved": True, "checks": "green"}
