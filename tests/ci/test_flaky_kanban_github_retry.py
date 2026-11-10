"""Bounded retries for flaky-card GitHub reads, never writes."""
import io
from email.message import Message
from unittest.mock import Mock
from urllib.error import HTTPError, URLError
from urllib.request import Request

import pytest

from scripts.ci import flaky_kanban as fk

URL = "https://api.github.com/repos/example/repo/actions/runs"


def http_error(code):
    return HTTPError(URL, code, "failure", Message(), None)


@pytest.fixture
def transport(monkeypatch):
    opening = Mock()
    sleeping = Mock()
    monkeypatch.setattr(fk.urllib.request, "urlopen", opening)
    monkeypatch.setattr(fk.time, "sleep", sleeping)
    return opening, sleeping


@pytest.mark.parametrize("error", [http_error(502), URLError("dropped"),
                                  TimeoutError("timeout"), ConnectionError("reset")])
def test_get_recovers_after_transient_error(transport, error):
    opening, sleeping = transport
    opening.side_effect = [error, io.BytesIO(b'{"workflow_runs": []}')]
    assert fk._gh(URL, "test-token") == {"workflow_runs": []}
    assert opening.call_count == 2
    assert [c.args for c in sleeping.call_args_list] == [(1,)]
    request = opening.call_args.args[0]
    assert request.full_url == URL
    assert request.get_method() == "GET"
    assert request.get_header("Authorization") == "Bearer test-token"
    assert opening.call_args.kwargs == {"timeout": 60}


@pytest.mark.parametrize("make_error", [lambda: http_error(502), lambda: URLError("dropped"),
                                       lambda: TimeoutError("timeout"), lambda: ConnectionError("reset")])
def test_get_exhaustion_raises_last_error(transport, make_error):
    opening, sleeping = transport
    errors = [make_error() for _ in range(4)]
    opening.side_effect = errors
    with pytest.raises(type(errors[-1])) as caught:
        fk._gh(URL, "test-token")
    assert caught.value is errors[-1]
    assert opening.call_count == 4
    assert [c.args for c in sleeping.call_args_list] == [(1,), (2,), (4,)]


@pytest.mark.parametrize("code", [400, 401, 403, 404, 429])
def test_get_client_error_is_not_retried(transport, code):
    opening, sleeping = transport
    error = http_error(code)
    opening.side_effect = error
    with pytest.raises(HTTPError) as caught:
        fk._gh(URL, "test-token")
    assert caught.value is error
    opening.assert_called_once()
    sleeping.assert_not_called()


@pytest.mark.parametrize("method", ["POST", "PATCH"])
def test_non_get_is_never_retried(transport, monkeypatch, method):
    opening, sleeping = transport
    monkeypatch.setattr(fk.urllib.request, "Request",
                        lambda *a, **kw: Request(*a, **kw, method=method))
    opening.side_effect = http_error(502)
    with pytest.raises(HTTPError):
        fk._gh(URL, "test-token")
    opening.assert_called_once()
    assert opening.call_args.args[0].get_method() == method
    sleeping.assert_not_called()
