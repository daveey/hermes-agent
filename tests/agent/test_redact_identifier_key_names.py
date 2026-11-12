"""Non-secret ``*_key`` identifiers are not masked by the assignment passes. [daveey]

``idempotency_key='review:pr1305:<sha>'`` was masked to ``'review...26b2'`` (and a second pass to ``'***'``)
because the lowercase assignment rule treats any ``*_key=`` name as a credential and the value has a
credential-like shape. A short allowlist of identifier names (dedupe, cache, sort and database keys) keeps
their values; every credential name still masks.
"""
from __future__ import annotations

import pytest

from agent.redact import redact_sensitive_text

SHA = "848af5adae9e29a2ac677f26c732c996997a26b2"
AZURE_KEY = "Zm9vYmFyYmF6cXV4" * 5 + "K3Rlc3Q="  # 88-char base64, the Azure shared-access key shape


def _r(text: str) -> str:
    return redact_sensitive_text(text, force=True)


@pytest.mark.parametrize("text", [
    f"idempotency_key='review:pr1305:{SHA}'",
    f"idempotency_key=review:pr1305:{SHA}",
    f'idempotency_key="core-wt-review:daveey/hermes#1305:{SHA}"',
    f"IDEMPOTENCY_KEY=review:pr1305:{SHA}",
    f"select id from tasks where idempotency_key='review:pr1305:{SHA}' and status='done'",
    f"ssh ubuntu@hermes-hub \"sqlite3 ~/.hermes/kanban.db \\\"select 1 from tasks where "
    f"idempotency_key='review:pr1305:{SHA}'\\\"\"",
    f"cache_key={SHA}",
    f"sort_key=Zz9{SHA}",
    f"partition_key=tenantA:{SHA}",
    f"dedupe_key=selfheal:{SHA}",
    f"dedup_key=selfheal:{SHA}",
])
def test_non_secret_key_names_survive(text):
    assert _r(text) == text


def test_already_elided_idempotency_key_is_not_masked_further():
    assert _r("idempotency_key='review...26b2'") == "idempotency_key='review...26b2'"


@pytest.mark.parametrize("text, secret", [
    ("api_key='sk-" + "Zq9" * 12 + "'", "sk-" + "Zq9" * 12),
    ("AWS_SECRET_ACCESS_KEY=wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY", "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"),
    ("password=Hunter2Hunter2xx", "Hunter2Hunter2xx"),
    ("private_key=MIIEvQIBADANBgkqhkiG9w0BAQEFAASC", "MIIEvQIBADANBgkqhkiG9w0BAQEFAASC"),
    ("secret_key=Qm9vbGVhbkZhbHNlVHJ1ZTEy", "Qm9vbGVhbkZhbHNlVHJ1ZTEy"),
    ("access_key=AKIA1234567890ABCDEF", "AKIA1234567890ABCDEF"),
    ("openai_key=" + "Ab3dE" * 8, "Ab3dE" * 8),
    # Azure shared-access keys are named primary_key / PRIMARY_KEY (88-char base64): never allowlisted
    ("PRIMARY_KEY=" + AZURE_KEY, AZURE_KEY),
    ("export PRIMARY_KEY=" + AZURE_KEY, AZURE_KEY),
    ('primary_key = "' + AZURE_KEY + '"', AZURE_KEY),
    ("cosmos.primary_key=" + AZURE_KEY, AZURE_KEY),
    ("secondary_key=" + AZURE_KEY, AZURE_KEY),
    # a name that only ends in an allowlisted name is not on the list
    ("my_cache_key_secret=" + "Ab3dE" * 8, "Ab3dE" * 8),
])
def test_secret_key_names_still_masked(text, secret):
    assert secret not in _r(text)
