"""Tavily key shapes: environment-scoped tvly-dev-/tvly-prod- keys (#72925). Split from test_redact.py (file cap)."""

import pytest

from agent.redact import redact_sensitive_text


@pytest.fixture(autouse=True)
def _ensure_redaction_enabled(monkeypatch):
    monkeypatch.delenv("HERMES_REDACT_SECRETS", raising=False)
    monkeypatch.setattr("agent.redact._REDACT_ENABLED", True)


def test_tavily_environment_scoped_keys_redacted():
    # Current Tavily keys carry a tvly-dev-/tvly-prod- segment (#72925).
    for env in ("dev", "prod"):
        key = "tvly-" + env + "-Ab3dE5fG7hJ9kL1mN2pQ4rS6"
        for text in (f"echo {key} done", f'{{"query": "{key}"}}'):
            result = redact_sensitive_text(text)
            assert "E5fG7hJ9kL1m" not in result, result


def test_hyphenated_tavily_names_are_not_keys():
    # Package/file names starting with tvly- are not keys; only an env segment may carry a hyphen.
    for text in ("pip install tvly-python-client", "tvly-config-file"):
        assert redact_sensitive_text(text) == text
