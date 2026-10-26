"""Known supersession wakes reconcile only with exact job and ancestry evidence."""
from __future__ import annotations

import subprocess

import pytest

from hermes_cli import kanban_pr_acceptance as acceptance
from hermes_cli.kanban_pr_wake import inspect_pr, main

URL = "https://github.com/example/project/pull/44"
SHA = "a" * 40


@pytest.fixture
def evidence(monkeypatch):
    rows = {
        "repos/example/project/pulls/44": {"state": "open", "merged": False, "html_url": URL},
        "repos/example/project/actions/jobs/123": {"head_sha": SHA, "status": "completed", "conclusion": "success"},
        "repos/example/project": {"default_branch": "main"},
        f"repos/example/project/compare/{SHA}...main": {"status": "ahead"},
    }
    monkeypatch.setattr(acceptance, "_api", lambda endpoint, **kw: rows[endpoint])
    return rows


def test_open_superseded_pr_wakes_only_reconciliation(evidence, capsys):
    assert main([URL, "--superseding-commit", SHA, "--verification-job", "123"]) == 0
    output = capsys.readouterr().out
    assert "reconcile" in output and "not acceptance" in output and "still OPEN" in output
    assert inspect_pr(URL)[0] == 1  # No evidence => no inference from an open PR.


@pytest.mark.parametrize("status,conclusion,sha", [
    ("completed", "failure", SHA), ("queued", None, SHA),
    ("completed", "success", "b" * 40), ("completed", "skipped", SHA),
])
def test_job_not_successful_for_exact_sha_does_not_wake(evidence, status, conclusion, sha):
    evidence["repos/example/project/actions/jobs/123"].update(status=status, conclusion=conclusion, head_sha=sha)
    assert inspect_pr(URL, SHA, "123")[0] == 1


@pytest.mark.parametrize("status,rc", [("ahead", 0), ("identical", 0), ("behind", 1), ("diverged", 1), (None, 2)])
def test_containing_default_branch_required(evidence, status, rc):
    evidence[f"repos/example/project/compare/{SHA}...main"]["status"] = status
    assert inspect_pr(URL, SHA, "123")[0] == rc


@pytest.mark.parametrize("sha,job", [(SHA, None), (None, "123"), (SHA.upper(), "123"), (SHA, "0123"), (SHA + "0", "123"), (SHA, "-1")])
def test_bad_evidence_pair_never_reaches_transport(monkeypatch, sha, job):
    monkeypatch.setattr(acceptance, "_api", lambda *a, **kw: pytest.fail("invalid pair reached API"))
    assert inspect_pr(URL, sha, job)[0] == 2


def test_unreadable_supersession_is_not_pending(evidence, monkeypatch):
    def api(endpoint, **kwargs):
        if endpoint.endswith("pulls/44"):
            return evidence[endpoint]
        raise subprocess.CalledProcessError(1, "secret", stderr="SENSITIVE_TOKEN")
    monkeypatch.setattr(acceptance, "_api", api)
    rc, text = inspect_pr(URL, SHA, "123")
    assert rc == 2 and "SENSITIVE_TOKEN" not in text


@pytest.mark.parametrize("value", [{}, [], {"head_sha": SHA}, {"head_sha": SHA, "status": "unknown"}])
def test_malformed_job_is_error(evidence, value):
    evidence["repos/example/project/actions/jobs/123"] = value
    assert inspect_pr(URL, SHA, "123")[0] == 2


def test_closed_abandoned_pr_never_claims_verified_supersession(evidence):
    evidence["repos/example/project/pulls/44"]["state"] = "closed"
    evidence["repos/example/project/actions/jobs/123"]["conclusion"] = "failure"
    rc, text = inspect_pr(URL, SHA, "123")
    assert rc == 0 and "not acceptance" in text
    assert "verified containing-default-branch" not in text
