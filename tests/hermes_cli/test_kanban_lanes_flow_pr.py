"""Exercise the actual read-only gh subprocess boundary without network."""
import sys

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
    answers = iter([first, [{"bucket": "pass"}], last])
    monkeypatch.setattr(Reader, "_read", lambda self, argv: next(answers))
    assert Reader().get("https://github.com/example/repo/pull/1") == {}
