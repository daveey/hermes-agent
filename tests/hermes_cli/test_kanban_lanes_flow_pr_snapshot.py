"""Head-bound snapshots retain confirmation while bounding fills to two calls."""
from copy import deepcopy

import pytest

from hermes_cli.kanban_lanes_flow_pr import Reader
from hermes_cli.kanban_lanes_flow_pr_snapshot import QUERY, argv, bucket, parse

URL = "https://github.com/example/repo/pull/1"
HEAD = "a" * 40


def snapshot(view=None, buckets=("pass",)):
    data = dict(view or {"state": "OPEN", "headRefOid": HEAD, "reviewDecision": "APPROVED"})
    reviews = data.get("reviews", [])
    data["reviews"] = {"pageInfo": {"hasPreviousPage": False}, "nodes": reviews}
    conclusions = {"pass": "SUCCESS", "fail": "FAILURE", "pending": "IN_PROGRESS"}
    nodes = [{"__typename": "CheckRun", "isRequired": True,
              "status": "IN_PROGRESS" if b == "pending" else "COMPLETED",
              "conclusion": conclusions[b]} for b in buckets]
    data["commits"] = {"nodes": [{"commit": {"oid": data.get("headRefOid"),
        "statusCheckRollup": {"contexts": {"pageInfo": {"hasNextPage": False}, "nodes": nodes}}}}]}
    return {"data": {"repository": {"pullRequest": data}}}


def test_two_call_fill_has_head_bound_checks_and_sequential_confirmation(monkeypatch):
    calls = []

    def read(self, args):
        calls.append(args)
        if args[:2] == ["api", "graphql"]:
            assert "query=" + QUERY in args
            return snapshot()
        assert args[:3] == ["pr", "view", URL]
        assert len(calls) == 2
        return {"state": "OPEN", "headRefOid": HEAD, "reviewDecision": "APPROVED"}

    monkeypatch.setattr(Reader, "_read", read)
    reader = Reader()
    assert reader.get(URL) == {"merged": False, "approved": True, "checks": "green"}
    assert reader.get(URL)["checks"] == "green"
    assert len(calls) == 2


PR_PATH = ("data", "repository", "pullRequest")
COMMIT_PATH = (*PR_PATH, "commits", "nodes", 0, "commit")
CONTEXT_PATH = (*COMMIT_PATH, "statusCheckRollup", "contexts")


@pytest.mark.parametrize("path,value", [
    (("errors",), [{"message": "partial result"}]),
    ((*PR_PATH, "reviews", "pageInfo", "hasPreviousPage"), True),
    ((*CONTEXT_PATH, "pageInfo", "hasNextPage"), True),
    ((*COMMIT_PATH, "oid"), "b" * 40),
    ((*CONTEXT_PATH, "pageInfo"), {}),
    ((*COMMIT_PATH, "statusCheckRollup"), {}),
])
def test_incomplete_or_wrong_head_snapshot_never_reports_success(monkeypatch, path, value):
    payload = snapshot()
    target = payload
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    monkeypatch.setattr(Reader, "_read", lambda self, args: payload)
    reader = Reader()
    assert reader.get(URL) == {}
    assert reader.states == {}


@pytest.mark.parametrize("state,expected", [("SUCCESS", "pass"), ("NEUTRAL", "pass"),
    ("SKIPPED", "pass"), ("FAILURE", "fail"), ("CANCELLED", "cancel"), ("FUTURE_ENUM", None)])
def test_completed_check_buckets(state, expected):
    assert bucket({"__typename": "CheckRun", "isRequired": True,
                   "status": "COMPLETED", "conclusion": state}) == expected


def test_running_check_with_stale_success_is_pending():
    assert bucket({"__typename": "CheckRun", "isRequired": True,
                   "status": "IN_PROGRESS", "conclusion": "SUCCESS"}) == "pending"


def test_graphql_null_review_matches_cli_empty_decision(monkeypatch):
    view = {"state": "OPEN", "headRefOid": HEAD, "reviewDecision": None}
    payload = snapshot(view)
    monkeypatch.setattr(Reader, "_read", lambda self, args:
        payload if args[:2] == ["api", "graphql"] else dict(view, reviewDecision=""))
    assert Reader().get(URL) == {"merged": False, "approved": False, "checks": "green"}


def test_graphql_bot_actor_cannot_supply_comment_approval(monkeypatch):
    view = {"state": "OPEN", "headRefOid": HEAD, "reviewDecision": "", "author": {"login": "author"},
            "reviews": [{"author": {"login": "automation", "__typename": "Bot"},
                         "state": "COMMENTED", "body": "VERDICT: APPROVE", "commit": {"oid": HEAD}}]}
    payload = snapshot(view)
    monkeypatch.setattr(Reader, "_read", lambda self, args:
        payload if args[:2] == ["api", "graphql"] else view)
    assert Reader().get(URL)["approved"] is False


@pytest.mark.parametrize("confirm_ok", [True, False])
def test_null_rollup_is_observed_without_green_and_still_confirms(monkeypatch, confirm_ok):
    payload = snapshot({"state": "OPEN", "headRefOid": HEAD, "reviewDecision": None})
    commit = payload["data"]["repository"]["pullRequest"]["commits"]["nodes"][0]["commit"]
    commit["statusCheckRollup"] = None
    calls = []

    def read(self, args):
        calls.append(args)
        if args[:2] == ["api", "graphql"]:
            return payload
        assert args == ["pr", "view", URL, "--json", "state,headRefOid,reviewDecision"]
        return {"state": "OPEN", "headRefOid": HEAD if confirm_ok else "b" * 40,
                "reviewDecision": ""}

    monkeypatch.setattr(Reader, "_read", read)
    reader = Reader()
    expected = {"merged": False, "approved": False} if confirm_ok else {}
    assert reader.get(URL) == expected
    assert reader.get(URL) == expected
    assert reader.states == ({URL: "OPEN"} if confirm_ok else {})
    assert len(calls) == 2


def test_missing_rollup_field_is_not_null_evidence():
    payload = snapshot()
    commit = payload["data"]["repository"]["pullRequest"]["commits"]["nodes"][0]["commit"]
    del commit["statusCheckRollup"]
    assert parse(payload) is None


def test_only_required_checks_and_no_empty_green():
    payload = snapshot(buckets=())
    assert parse(payload)[1] == []
    changed = deepcopy(payload)
    nodes = changed["data"]["repository"]["pullRequest"]["commits"]["nodes"]
    nodes[0]["commit"]["statusCheckRollup"]["contexts"]["nodes"] = [
        {"__typename": "StatusContext", "isRequired": False, "state": "FAILURE"},
        {"__typename": "StatusContext", "isRequired": True, "state": "SUCCESS"}]
    assert parse(changed)[1] == [{"bucket": "pass"}]


@pytest.mark.parametrize("url", ["https://example.com/a/b/pull/1", URL + "?x=1", URL + "/files", URL[:-1] + "0"])
def test_malformed_urls_issue_no_provider_query(url):
    assert argv(url) is None
