"""Regressions for transport reads and untrusted Actions list boundaries."""
from __future__ import annotations

import http.client
import json
from types import SimpleNamespace
import pytest

from scripts.ci import flaky_artifact as fa
from scripts.ci import flaky_artifact_poll as fap
from scripts.ci import flaky_kanban as fk
from tests.ci.flaky_artifact_fakes import FakeGitHub
from tests.ci.test_flaky_kanban import FakeBoard

SECRET = b"SECRET-partial-response"


class ReadResponse:
    status = 200
    headers = {}

    def __init__(self, result):
        self.result = result

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self, *args):
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def _read_error(kind):
    return http.client.IncompleteRead(SECRET, 100) if kind == "partial" else http.client.HTTPException(SECRET.decode())


@pytest.mark.parametrize("kind", ["partial", "protocol"])
def test_api_get_retries_http_read_failures(monkeypatch, kind):
    replies = [ReadResponse(_read_error(kind)), ReadResponse(b'{"jobs": []}')]
    calls = []

    def open_response(req, **kwargs):
        calls.append(req.get_method())
        return replies.pop(0)

    monkeypatch.setattr(fk.urllib.request, "build_opener", lambda *a: SimpleNamespace(open=open_response))
    monkeypatch.setattr(fk.time, "sleep", lambda _: None)
    assert fk._gh("https://api.github.com/repos/o/r/actions/runs", "tok") == {"jobs": []}
    assert calls == ["GET", "GET"]


@pytest.mark.parametrize("kind", ["partial", "protocol"])
@pytest.mark.parametrize("stage,receipt", [("runs", "FAILED runs_unavailable"),
                                           ("jobs", "WAITING run=5/1 code=jobs_unavailable polls=1"),
                                           ("artifacts", "WAITING run=5/1 code=artifacts_unavailable polls=1")])
def test_exhausted_http_reads_have_fixed_receipts_and_persist_retryable_state(tmp_path, monkeypatch, capsys,
                                                                           kind, stage, receipt):
    fake = FakeGitHub()
    fake.simple()
    failures = []

    def open_response(req, **kwargs):
        url = req.full_url
        match = (stage == "runs" and "/actions/runs?" in url
                 or stage == "jobs" and "/jobs?" in url
                 or stage == "artifacts" and "/artifacts?" in url)
        if match:
            failures.append(url)
            return ReadResponse(_read_error(kind))
        return ReadResponse(json.dumps(fake.gh(url, "tok")).encode())

    monkeypatch.setattr(fk.urllib.request, "build_opener", lambda *a: SimpleNamespace(open=open_response))
    monkeypatch.setattr(fk.time, "sleep", lambda _: None)
    monkeypatch.setattr(fk, "CliBoard", lambda *a, **k: FakeBoard())
    monkeypatch.setattr(fk, "HubRelay", lambda *a, **k: None)
    monkeypatch.setenv("GH_TOKEN", "tok")
    path = tmp_path / "state.json"
    assert fk.main(["--initialize-intake", "--state", str(path)]) == 0
    capsys.readouterr()
    initial = {"cards": {"tests/old.py": {"card": "t_old", "generation": 0}}, "runs": ["4/1"]}
    fk.save_state(path, initial)
    # A run-listing failure is red at once; an unreadable run waits one poll, then turns red if it persists.
    assert fk.main(["--from-github", "--repo", "o/r", "--state", str(path)]) == (1 if stage == "runs" else 0)
    out = capsys.readouterr()
    assert receipt in out.out and SECRET.decode() not in out.out + out.err
    assert len(failures) == 4
    after = fk.load_state(path)
    assert (after["cards"], after["runs"]) == (initial["cards"], initial["runs"])
    if stage != "runs":
        assert fk.main(["--from-github", "--repo", "o/r", "--state", str(path)]) == 1
        assert receipt.replace("WAITING", "FAILED").replace("polls=1", "polls=2") in capsys.readouterr().out
        assert fk.load_state(path)["runs"] == initial["runs"]


@pytest.mark.parametrize("kind", ["partial", "protocol"])
@pytest.mark.parametrize("hop", ["api", "signed"])
def test_download_http_read_failure_is_fixed_and_not_retried(monkeypatch, kind, hop):
    calls = []

    class Opener:
        def open(self, req, **kwargs):
            calls.append(req)
            if hop == "api" or len(calls) == 2:
                return ReadResponse(_read_error(kind))
            response = ReadResponse(b"")
            response.status = 302
            response.headers = {"Location": "https://a.blob.core.windows.net/zip?sig=private"}
            return response

    monkeypatch.setattr(fa.urllib.request, "build_opener", lambda *a: Opener())
    with pytest.raises(fa.ArtifactError) as err:
        fa.download_zip("o/r", 100, "tok")
    assert str(err.value) == "download_unavailable" and err.value.__cause__ is None
    assert len(calls) == (1 if hop == "api" else 2)


@pytest.mark.parametrize("kind", ["partial", "protocol"])
def test_pages_maps_protocol_failure_without_raw_content(kind):
    def fetch(url):
        return ReadResponse(_read_error(kind)).read()

    with pytest.raises(fap.Unavailable) as err:
        fap._pages(fetch, "https://api.github.com/jobs", "jobs", "jobs_unavailable")
    assert str(err.value) == "jobs_unavailable" and err.value.__cause__ is None


@pytest.mark.parametrize("repo,change", [
    ("bad/repo/extra", {}), (None, {}), ("o/r", {"id": "5/" + SECRET.decode()}),
    ("o/r", {"id": True}), ("o/r", {"id": 0}), ("o/r", {"id": fa.MAX_INT + 1}),
    ("o/r", {"run_attempt": "1/" + SECRET.decode()}), ("o/r", {"run_attempt": True}),
    ("o/r", {"run_attempt": 0}), ("o/r", {"run_attempt": fa.MAX_INT + 1}),
    ("o/r", {"head_sha": SECRET.decode()}), ("o/r", {"head_sha": None}),
])
def test_assess_validates_provenance_before_api_requests(repo, change):
    fake = FakeGitHub()
    run = {**fake.run(), **change}
    with pytest.raises(fap.Unavailable) as err:
        fap.assess_run(repo, run, lambda url: fake.gh(url, "tok"), "tok")
    assert str(err.value) == "invalid" and fake.urls == []


@pytest.mark.parametrize("body", [None, [], {}, {"workflow_runs": None}, {"workflow_runs": {}},
                                 {"workflow_runs": [SECRET.decode()]}, {"workflow_runs": [{}]},
                                 {"workflow_runs": [{"id": SECRET.decode()}]},
                                 {"workflow_runs": [{"id": True}]},
                                 {"workflow_runs": [{"id": fa.MAX_INT + 1}]},
                                 {"workflow_runs": [{"id": 5, "run_attempt": SECRET.decode()}]},
                                 {"workflow_runs": [{"id": 5, "run_attempt": False}]},
                                 {"workflow_runs": [{"id": 5}] * 101}])
def test_malformed_run_listing_has_fixed_receipt_and_no_state_change(monkeypatch, body):
    monkeypatch.setattr(fk, "_gh", lambda *a: body)
    state = {"cards": {}, "runs": ["4/1"]}
    board = FakeBoard()
    assert fk.poll_github("o/r", "tok", board, state) == ["FAILED runs_unavailable"]
    assert state == {"cards": {}, "runs": ["4/1"]} and not board.cards


def test_completed_run_listing_is_page_bounded(monkeypatch):
    urls = []

    def fetch(url, token):
        urls.append(url)
        assert len(urls) <= 2, "completed-runs pagination exceeded its bound"
        return {"workflow_runs": [{"id": i + 1} for i in range(100)]}

    monkeypatch.setattr(fk, "_gh", fetch)
    monkeypatch.setattr(fap, "MAX_PAGES", 2)
    assert fk.poll_github("o/r", "tok", FakeBoard(), {"cards": {}, "runs": []}) == ["FAILED runs_unavailable"]
    assert len(urls) == 2


def test_invalid_repo_is_rejected_before_run_listing(monkeypatch):
    calls = []
    monkeypatch.setattr(fk, "_gh", lambda *a: calls.append(a) or {"workflow_runs": []})
    assert fk.poll_github("o/r/" + SECRET.decode(), "tok", FakeBoard(), {"cards": {}, "runs": []}) == ["FAILED runs_unavailable"]
    assert calls == []


@pytest.mark.parametrize("key", ["jobs", "artifacts"])
def test_metadata_page_cannot_exceed_requested_page_size(key):
    with pytest.raises(fap.Unavailable) as err:
        fap._pages(lambda url: {key: [{}] * 101}, "https://api.github.com/list", key, key + "_unavailable")
    assert str(err.value) == key + "_unavailable"
