"""Quota fallback retains exact-head, policy and merge-time acceptance."""
import subprocess

import pytest

from hermes_cli import kanban_pr_acceptance as gate
from tests.hermes_cli import test_kanban_pr_acceptance as fixtures


acceptance_github = fixtures.github


@pytest.mark.platforms("linux")
@pytest.mark.parametrize("fault", [None, "failure", "missing", "stale", "head_change"])
def test_exhausted_completion_uses_rest(acceptance_github, monkeypatch, fault):
    github = acceptance_github
    from hermes_cli import kanban_db as kb
    from hermes_cli.kanban_db_connect import connect

    original = gate._api
    calls = []

    def api(endpoint, **kwargs):
        calls.append(endpoint)
        if endpoint == "graphql":
            raise gate._GraphQLRateLimit("GraphQL quota exhausted")
        if endpoint.endswith("/protection/required_status_checks"):
            return {"contexts": ["required"], "checks": [{"context": "required", "app_id": 1}]}
        value = original(endpoint, **kwargs)
        if endpoint == "repos/acme/repo/pulls/7":
            value.update(merged_at="2026-11-02T13:10:04Z", merge_commit_sha=None)
        return value

    monkeypatch.setattr(gate, "_api", api)
    if fault == "failure":
        github["conclusion"] = "failure"
    elif fault:
        github[fault] = True
    with connect() as conn:
        task = kb.create_task(conn, title="REST acceptance", completion_contract="acme/repo")
        ok = kb.complete_task(conn, task, summary="done", metadata={
            "published_pr": "https://github.com/acme/repo/pull/7"})
        assert ok is (fault is None)
        assert (kb.get_task(conn, task).status == "done") is ok
    assert calls.count("graphql") == 1
    assert sum(path == "repos/acme/repo/pulls/7" for path in calls) == 2


@pytest.mark.parametrize("message", [
    "GraphQL: API rate limit already exceeded for user ID 123",
    '{"errors":[{"type":"RATE_LIMITED"}]}',
])
def test_transport_classifies_rate_limit_before_http_auth(monkeypatch, message):
    monkeypatch.setattr(gate, "_gh_command_prefix", lambda home: [])
    monkeypatch.setattr(gate, "_gh_env", lambda home: None)
    monkeypatch.setattr("hermes_platform.resolver.github.resolve_gh", lambda *a: type(
        "Resolved", (), {"found": True, "command": ["gh"]})())

    def fail(*args, **kwargs):
        raise subprocess.CalledProcessError(1, "gh", stderr=message + " (HTTP 403)")

    monkeypatch.setattr(subprocess, "run", fail)
    with pytest.raises(gate._GraphQLRateLimit):
        gate._api("graphql", query="{}")
    with pytest.raises(gate._GateAuthError):
        gate._api("repos/acme/repo/pulls/7")


def test_rest_policy_unreadable_does_not_accept(acceptance_github, monkeypatch):
    original = gate._api

    def api(endpoint, **kwargs):
        if endpoint == "graphql":
            raise gate._GraphQLRateLimit()
        if "/protection/" in endpoint:
            raise gate._GateAuthError("HTTP 404")
        value = original(endpoint, **kwargs)
        if endpoint == "repos/acme/repo/pulls/7":
            value.update(merged_at="2026-11-02T13:10:04Z", merge_commit_sha=None)
        return value

    monkeypatch.setattr(gate, "_api", api)
    receipt = gate.collect_acceptance("acme/repo", "https://github.com/acme/repo/pull/7")
    assert not receipt["ok"]
    assert receipt["classification"] == "auth"
