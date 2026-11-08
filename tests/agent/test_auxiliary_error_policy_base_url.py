"""``classify_attempt`` reads the endpoint an SDK client actually holds (``str`` or ``httpx.URL``)."""
import sys
from types import SimpleNamespace

import httpx
import pytest

from agent import auxiliary_error_policy as aux_errors

_NOUS = "https://inference-api.nousresearch.com/v1"


class _UsageWindow402(Exception):
    """Decisive only on a Nous endpoint: elsewhere the 402 keeps the text rules."""

    status_code = 402
    response = SimpleNamespace(headers={})

    def __init__(self):
        super().__init__("Your included Hermes usage for today is used up. It resets in 5 hours, try again later.")
        self.body = {"error": {"code": "usage_window_and_credits_exhausted", "message": str(self)}}


def _usage_window_402():
    return _UsageWindow402()


class _NotAUrl:
    def __str__(self):
        return _NOUS


@pytest.mark.parametrize("held", [httpx.URL(_NOUS), _NOUS], ids=["httpx.URL", "str"])
def test_client_endpoint_names_the_backend(held):
    verdict = aux_errors.classify_attempt(_usage_window_402(), "", SimpleNamespace(base_url=held), "m",
                                          base_url="https://example.invalid/v1")
    assert verdict.error_context.get("usage_window") is True


def test_an_unknown_base_url_object_falls_back_to_the_route_url():
    verdict = aux_errors.classify_attempt(_usage_window_402(), "", SimpleNamespace(base_url=_NotAUrl()), "m",
                                          base_url="https://example.invalid/v1")
    assert not verdict.error_context.get("usage_window")
    nous = aux_errors.classify_attempt(_usage_window_402(), "", SimpleNamespace(base_url=_NotAUrl()), "m",
                                       base_url=_NOUS)
    assert nous.error_context.get("usage_window") is True


def test_a_str_endpoint_classifies_while_httpx_is_not_loaded(monkeypatch):
    # httpx stays optional: with it unloaded a str endpoint still names the backend.
    monkeypatch.delitem(sys.modules, "httpx")
    verdict = aux_errors.classify_attempt(_usage_window_402(), "", SimpleNamespace(base_url=_NOUS), "m")
    assert verdict.error_context.get("usage_window") is True
